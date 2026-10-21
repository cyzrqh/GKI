#!/usr/bin/env python3
"""Import pinned upstream build files without overwriting local customizations.

Examples:
  python tools/sync_upstream.py --check
  python tools/sync_upstream.py --update
  python tools/sync_upstream.py --update --abk-ref <commit-or-branch>
  python tools/sync_upstream.py --update --from-abk-zip abk.zip --from-catalog-zip catalog.zip

The ZIP options import the corresponding already-locked commit without networking
for that source. Archive provenance is the responsibility of the caller. Updates
never commit, push, or dispatch GitHub Actions.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


MANIFEST_PATH = ".upstream/imported-files.json"
WORKFLOW_PATH = ".github/workflows/kernel-full-feature-matrix.yml"
LOCK_PATH = "upstream.lock.json"
ABK_DIRECTORIES = (".github/scripts/", "config/", "ddk/", "modules/", "zram/")
ABK_FILES = ("hmbird_patch.c", "LICENSE", "THIRD_PARTY_NOTICES.md")
ABK_SNAPSHOTS = {
    ".github/workflows/build.yml": ".upstream/abk-build.yml",
    ".github/workflows/kernel-full-feature-matrix.yml": ".upstream/abk-matrix.yml",
}
CATALOG_FILES = (
    "data/android12/5.10.json", "data/android13/5.15.json", "data/android14/6.1.json",
    "data/android15/6.6.json", "data/android16/6.12.json",
)
CATALOG_LICENSE = ".upstream/licenses/zzh-LICENSE"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_IMPORTED_BYTES = 100 * 1024 * 1024


class SyncError(RuntimeError):
    """An actionable validation or upstream synchronization failure."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SyncError(f"Cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"{path.name} must contain a JSON object")
    return value


def read_lock(root: Path) -> dict:
    lock = _read_json(root / LOCK_PATH)
    if lock.get("schema_version") != 1:
        raise SyncError("Unsupported upstream.lock.json schema_version; expected 1")
    for source in ("abk", "catalog"):
        item = lock.get(source)
        if not isinstance(item, dict):
            raise SyncError(f"upstream.lock.json is missing {source}")
        repo = item.get("repository", "")
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise SyncError(f"Invalid {source} repository (expected owner/name)")
        if any(part in {".", ".."} for part in repo.split("/")):
            raise SyncError(f"Invalid {source} repository")
        branch = item.get("branch")
        if not isinstance(branch, str) or not branch or any(ord(c) < 32 for c in branch):
            raise SyncError(f"Invalid {source} branch")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(item.get("commit", ""))):
            raise SyncError(f"{source} commit must be a full 40-character SHA")
    return lock


def _valid_relative(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and not path.startswith("/") and "\\" not in path and ":" not in path and all(
        part not in {".", "..", ""} for part in parts
    ) and str(PurePosixPath(path)) == path


def _is_import_path(path: str) -> bool:
    if not _valid_relative(path):
        return False
    return (
        path in ABK_FILES
        or path in ABK_SNAPSHOTS.values()
        or path in CATALOG_FILES
        or path == CATALOG_LICENSE
        or any(path.startswith(prefix) for prefix in ABK_DIRECTORIES)
    )


def _safe_path(root: Path, relative: str) -> Path:
    if not _valid_relative(relative):
        raise SyncError(f"Unsafe managed path: {relative!r}")
    base = root.resolve()
    target = base.joinpath(*PurePosixPath(relative).parts)
    try:
        target.resolve().relative_to(base)
    except ValueError as exc:
        raise SyncError(f"Managed path escapes the repository: {relative}") from exc
    current = target
    while current != base:
        if current.is_symlink():
            raise SyncError(f"Managed path uses a symbolic link: {relative}")
        current = current.parent
    return target


def read_manifest(root: Path) -> dict[str, str]:
    path = _safe_path(root, MANIFEST_PATH)
    if not path.exists():
        raise SyncError("Missing import manifest. Run --record-current once before updating.")
    document = _read_json(path)
    files = document.get("files")
    if document.get("schema_version") != 1 or not isinstance(files, dict) or not files:
        raise SyncError("Invalid import manifest; expected schema_version 1 and a nonempty files mapping")
    seen = set()
    for name, checksum in files.items():
        if not isinstance(name, str) or not _is_import_path(name):
            raise SyncError(f"Manifest includes a path outside the import whitelist: {name!r}")
        if name.casefold() in seen:
            raise SyncError(f"Case-colliding paths in import manifest: {name}")
        seen.add(name.casefold())
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise SyncError(f"Invalid checksum in import manifest: {name}")
    return files


def _verify_live_files(root: Path, managed: Mapping[str, str], imports: Mapping[str, bytes]) -> None:
    conflicts = []
    for name, checksum in managed.items():
        path = _safe_path(root, name)
        if not path.is_file() or _sha256(path.read_bytes()) != checksum:
            conflicts.append(name)
    for name in imports:
        path = _safe_path(root, name)
        if path.exists() and name not in managed:
            conflicts.append(f"{name} (local file is not managed by the importer)")
    if conflicts:
        listing = "\n  ".join(sorted(set(conflicts)))
        raise SyncError(
            "Local imported files were changed, deleted, or would be overwritten. "
            "Move intentional customizations into the local overlay or restore these files before updating:\n  "
            + listing
        )


def record_current(root: Path) -> dict[str, str]:
    """Initialize hashes for the initial import; never bless later modifications."""
    read_lock(root)
    target = _safe_path(root, MANIFEST_PATH)
    if target.exists():
        raise SyncError("Import manifest already exists; --record-current cannot replace it")
    candidates = set(ABK_FILES) | set(ABK_SNAPSHOTS.values()) | set(CATALOG_FILES) | {CATALOG_LICENSE}
    for prefix in ABK_DIRECTORIES:
        directory = _safe_path(root, prefix.rstrip("/"))
        if directory.exists():
            candidates.update(path.relative_to(root).as_posix() for path in directory.rglob("*") if path.is_file())
    # This optional file is the user's customization unless an upstream archive
    # explicitly imports it during a later update.
    candidates.discard("config/stock_defconfig")
    files = {}
    for name in sorted(candidates):
        path = _safe_path(root, name)
        if path.is_file():
            files[name] = _sha256(path.read_bytes())
    missing = (set(ABK_FILES) | set(ABK_SNAPSHOTS.values()) | set(CATALOG_FILES) | {CATALOG_LICENSE}) - files.keys()
    if missing:
        raise SyncError("Initial import is incomplete; missing: " + ", ".join(sorted(missing)))
    for prefix in ABK_DIRECTORIES:
        if not any(name.startswith(prefix) for name in files):
            raise SyncError(f"Initial import is incomplete; missing support directory: {prefix}")
    _atomic_write(target, _json_bytes({"schema_version": 1, "files": files}))
    return files


def _read_selected_zip(source: Path | bytes, selector) -> dict[str, bytes]:
    """Read GitHub's single-root ZIP structure without extracting archive paths."""
    stream = io.BytesIO(source) if isinstance(source, bytes) else source
    result = {}
    root_name = None
    seen = set()
    total = 0
    try:
        with zipfile.ZipFile(stream) as archive:
            for info in archive.infolist():
                name = info.filename.rstrip("/")
                if not name:
                    continue
                if not _valid_relative(name):
                    raise SyncError(f"Unsafe ZIP member: {info.filename!r}")
                parts = PurePosixPath(name).parts
                if root_name is None:
                    root_name = parts[0]
                if parts[0] != root_name:
                    raise SyncError("Upstream ZIP must have exactly one repository root directory")
                if info.is_dir():
                    continue
                if len(parts) < 2:
                    raise SyncError("Upstream ZIP files must be inside its repository root directory")
                relative = "/".join(parts[1:])
                destination = selector(relative)
                if destination is None:
                    continue
                if not _is_import_path(destination):
                    raise SyncError(f"Importer selected an unexpected destination: {destination}")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise SyncError(f"Imported ZIP member is a symbolic link: {relative}")
                if destination.casefold() in seen:
                    raise SyncError(f"Duplicate or case-colliding imported ZIP member: {relative}")
                seen.add(destination.casefold())
                total += info.file_size
                if total > MAX_IMPORTED_BYTES:
                    raise SyncError("Selected upstream files exceed the import size limit")
                result[destination] = archive.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, SyncError):
            raise
        raise SyncError(f"Cannot read upstream ZIP: {exc}") from exc
    return result


def build_imports_from_zips(abk_zip: Path | bytes, catalog_zip: Path | bytes) -> dict[str, bytes]:
    def abk_selector(name):
        if name in ABK_SNAPSHOTS:
            return ABK_SNAPSHOTS[name]
        if name in ABK_FILES or any(name.startswith(prefix) for prefix in ABK_DIRECTORIES):
            return name
        return None

    def catalog_selector(name):
        if name in CATALOG_FILES:
            return name
        return CATALOG_LICENSE if name == "LICENSE" else None

    abk = _read_selected_zip(abk_zip, abk_selector)
    catalog = _read_selected_zip(catalog_zip, catalog_selector)
    required_abk = set(ABK_FILES) | set(ABK_SNAPSHOTS.values())
    required_catalog = set(CATALOG_FILES) | {CATALOG_LICENSE}
    for label, files, required in (("ABK", abk, required_abk), ("catalog", catalog, required_catalog)):
        missing = required - files.keys()
        if missing:
            raise SyncError(f"{label} upstream layout changed; missing required files: " + ", ".join(sorted(missing)))
    for prefix in ABK_DIRECTORIES:
        if not any(name.startswith(prefix) for name in abk):
            raise SyncError(f"ABK upstream layout changed; missing support directory: {prefix}")
    for name in CATALOG_FILES:
        try:
            value = json.loads(catalog[name].decode("utf-8-sig"))
        except (UnicodeError, ValueError) as exc:
            raise SyncError(f"Invalid upstream version catalog {name}: {exc}") from exc
        if not isinstance(value, (dict, list)) or not value:
            raise SyncError(f"Upstream version catalog is empty or has an unexpected shape: {name}")
    return {**abk, **catalog}


class _NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the API credential to a redirect destination."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is not None:
            for key, _value in redirected.header_items():
                if key.lower() == "authorization":
                    redirected.remove_header(key)
        return redirected


def _request(url: str, *, accept: str) -> bytes:
    headers = {"User-Agent": "independent-gki-upstream-sync/1", "Accept": accept}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    parsed = urllib.parse.urlparse(url)
    if token and parsed.scheme == "https" and parsed.netloc == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_NoAuthRedirectHandler())
    try:
        with opener.open(request, timeout=45) as response:
            content = response.read(MAX_DOWNLOAD_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise SyncError(f"GitHub request failed (HTTP {exc.code}) for {url}; check the ref, access, or API rate limit") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise SyncError(f"Cannot reach GitHub for {url}: {exc}") from exc
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise SyncError("GitHub response exceeds the download size limit")
    return content


def resolve_commit(repository: str, ref: str) -> str:
    encoded_ref = urllib.parse.quote(ref, safe="")
    url = f"https://api.github.com/repos/{repository}/commits/{encoded_ref}"
    try:
        result = json.loads(_request(url, accept="application/vnd.github+json"))
        commit = result["sha"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SyncError(f"GitHub returned an invalid commit response for {repository}@{ref}") from exc
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise SyncError(f"GitHub returned an invalid commit SHA for {repository}@{ref}")
    return commit.lower()


def download_zip(repository: str, commit: str) -> bytes:
    return _request(f"https://codeload.github.com/{repository}/zip/{commit}", accept="application/zip")


def check_upstream(lock: dict) -> dict[str, str]:
    latest = {}
    for source in ("abk", "catalog"):
        item = lock[source]
        latest[source] = resolve_commit(item["repository"], item["branch"])
        status = "up to date" if item["commit"].lower() == latest[source] else "update available"
        print(f"{source}: {item['repository']} ({item['branch']})\n  locked: {item['commit']}\n  latest: {latest[source]} [{status}]")
    return latest


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".upstream-sync-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _commit_changes(root: Path, writes: Mapping[str, bytes], deletes: set[str]) -> None:
    """Replace validated files, restoring old bytes if an ordinary I/O error occurs."""
    previous = {}
    changed = []
    try:
        for name in sorted(set(writes) | deletes):
            path = _safe_path(root, name)
            previous[name] = path.read_bytes() if path.is_file() else None
            if name in writes:
                if previous[name] == writes[name]:
                    continue
                _atomic_write(path, writes[name])
            elif path.exists():
                path.unlink()
            changed.append(name)
    except OSError as exc:
        rollback_errors = []
        for name in reversed(changed):
            try:
                path = _safe_path(root, name)
                if previous[name] is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, previous[name])
            except OSError as rollback_exc:
                rollback_errors.append(f"{name}: {rollback_exc}")
        suffix = " Rollback errors: " + "; ".join(rollback_errors) if rollback_errors else " Existing file contents were restored."
        raise SyncError(f"Could not save the upstream update: {exc}.{suffix}") from exc


@contextlib.contextmanager
def _staging_tree(root: Path):
    # A plain workspace directory also works with restricted Windows accounts,
    # where tempfile's private-directory ACL can exclude the sandbox identity.
    relative = ".upstream/.sync-stage-" + uuid.uuid4().hex
    temporary = _safe_path(root, relative)
    temporary.mkdir(parents=True)
    try:
        yield temporary / "repository"
    finally:
        checked = _safe_path(root, relative)
        if checked != temporary or checked.parent != (root / ".upstream"):
            raise SyncError("Refusing to remove an unexpected staging directory")
        shutil.rmtree(checked)


def apply_imports(root: Path, imports: Mapping[str, bytes], lock: dict) -> dict[str, int]:
    root = root.resolve()
    managed = read_manifest(root)
    if not imports or any(not _is_import_path(name) for name in imports):
        raise SyncError("Import plan contains an empty selection or paths outside the whitelist")
    _verify_live_files(root, managed, imports)
    old_lock = _safe_path(root, LOCK_PATH).read_bytes()
    old_manifest = _safe_path(root, MANIFEST_PATH).read_bytes()
    workflow = _safe_path(root, WORKFLOW_PATH)
    old_workflow = workflow.read_bytes() if workflow.is_file() else None
    stale = set(managed) - imports.keys()
    manifest = {"schema_version": 1, "files": {name: _sha256(content) for name, content in sorted(imports.items())}}
    with _staging_tree(root) as staging:
        shutil.copytree(root, staging, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", ".pytest_cache", ".sync-stage-*"))
        for name in stale:
            _safe_path(staging, name).unlink()
        for name, content in imports.items():
            path = _safe_path(staging, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (staging / LOCK_PATH).write_bytes(_json_bytes(lock))
        (staging / MANIFEST_PATH).write_bytes(_json_bytes(manifest))
        generator = staging / "tools/generate_workflow.py"
        if not generator.is_file():
            raise SyncError("Local generator tools/generate_workflow.py is missing; no files were updated")
        try:
            completed = subprocess.run(
                [sys.executable, str(generator), "--root", str(staging)],
                cwd=staging, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SyncError(f"Staged workflow generation could not complete; no files were updated: {exc}") from exc
        if completed.returncode:
            diagnostic = (completed.stdout + "\n" + completed.stderr).strip()[-6000:]
            raise SyncError("Upstream changes are incompatible with the local workflow generator; no files were updated.\n" + diagnostic)
        generated = _safe_path(staging, WORKFLOW_PATH)
        if not generated.is_file():
            raise SyncError(f"Generator did not create {WORKFLOW_PATH}; no files were updated")
        writes = dict(imports)
        writes[WORKFLOW_PATH] = generated.read_bytes()
        writes[LOCK_PATH] = _json_bytes(lock)
        writes[MANIFEST_PATH] = _json_bytes(manifest)
        # Recheck after validation so an edit made while staging cannot be lost.
        _verify_live_files(root, managed, imports)
        if (root / LOCK_PATH).read_bytes() != old_lock or (root / MANIFEST_PATH).read_bytes() != old_manifest:
            raise SyncError("Lock or manifest changed while validating; rerun the update")
        current_workflow = workflow.read_bytes() if workflow.is_file() else None
        if current_workflow != old_workflow:
            raise SyncError("Generated workflow changed while validating; rerun the update")
        counts = {
            "added": len(imports.keys() - managed.keys()),
            "changed": sum(name in managed and managed[name] != _sha256(content) for name, content in imports.items()),
            "removed": len(stale),
        }
        _commit_changes(root, writes, stale)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="Repository root")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Compare locked commits to upstream branches; never write files")
    action.add_argument("--update", action="store_true", help="Stage, validate, and import upstream updates locally")
    action.add_argument("--record-current", action="store_true", help="Initialize the imported-file hash manifest once")
    parser.add_argument("--abk-ref", help="ABK branch, tag, or full commit to resolve instead of its configured branch")
    parser.add_argument("--catalog-ref", help="Catalog branch, tag, or full commit to resolve")
    parser.add_argument("--from-abk-zip", type=Path, help="Import a local ABK ZIP for its already-locked commit")
    parser.add_argument("--from-catalog-zip", type=Path, help="Import a local catalog ZIP for its already-locked commit")
    args = parser.parse_args(argv)
    if not args.update and any((args.abk_ref, args.catalog_ref, args.from_abk_zip, args.from_catalog_zip)):
        parser.error("Ref and ZIP options require --update")
    if (args.abk_ref and args.from_abk_zip) or (args.catalog_ref and args.from_catalog_zip):
        parser.error("A source cannot combine a local ZIP with a ref override; ZIP imports use its locked commit")
    try:
        root = args.root.resolve()
        lock = read_lock(root)
        if args.check:
            check_upstream(lock)
            return 0
        if args.record_current:
            files = record_current(root)
            print(f"Recorded {len(files)} imported files. Local overlays and config/stock_defconfig are excluded.")
            return 0
        managed = read_manifest(root)
        _verify_live_files(root, managed, {})
        updated_lock = copy.deepcopy(lock)
        archives = {}
        for source, local_zip, override in (
            ("abk", args.from_abk_zip, args.abk_ref),
            ("catalog", args.from_catalog_zip, args.catalog_ref),
        ):
            item = updated_lock[source]
            if local_zip:
                archives[source] = local_zip
            else:
                item["commit"] = resolve_commit(item["repository"], override or item["branch"])
                archives[source] = download_zip(item["repository"], item["commit"])
            print(f"{source}: preparing {item['repository']}@{item['commit']}")
        imports = build_imports_from_zips(archives["abk"], archives["catalog"])
        counts = apply_imports(root, imports, updated_lock)
        print(f"Validated local update: {counts['added']} added, {counts['changed']} changed, {counts['removed']} removed imported files.")
        print("Workflow regenerated. Review the diff before committing; nothing was committed, pushed, or run on GitHub.")
        return 0
    except (SyncError, OSError) as exc:
        print(f"Upstream sync stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
