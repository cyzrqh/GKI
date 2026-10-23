#!/usr/bin/env python3
"""Add GKI monthly versions discovered directly from official Android sources."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from matrix import KERNELS, _full_version, _month, _row, load_catalog, validate_catalog


ROOT = Path(__file__).resolve().parents[1]
BASE = "https://android.googlesource.com/kernel/"
SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
MONTHLY = re.compile(r"refs/heads/(deprecated/)?(common-)?(android[0-9]+)-([0-9]+\.[0-9]+)-([0-9]{4}-[0-9]{2})\Z")


class CatalogError(ValueError):
    """Official source metadata is incomplete or disagrees with a known pair."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise CatalogError("Official Android catalog redirects are not permitted")


class OfficialProvider:
    """Small mockable Gitiles provider; URLs never come from external metadata."""

    def get(self, path: str) -> bytes:
        url = BASE + path
        for attempt in range(3):
            try:
                with build_opener(NoRedirect()).open(Request(url, headers={"User-Agent": "gki-kernel-catalog"}), timeout=60) as response:
                    if response.status != 200 or response.geturl() != url:
                        raise CatalogError("Unexpected response from the official Android source")
                    raw = response.read(32 * 1024 * 1024 + 1)
                    if len(raw) > 32 * 1024 * 1024:
                        raise CatalogError("Official Android source response is too large")
                    return raw
            except HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise CatalogError(f"Cannot read the official Android kernel source (HTTP {error.code})") from error
            except (URLError, OSError) as error:
                if attempt == 2:
                    raise CatalogError("Cannot read the official Android kernel source after three attempts") from error
            time.sleep(attempt + 1)
        raise CatalogError("Cannot read the official Android kernel source")

    def refs(self, repository: str) -> dict[str, str]:
        return self._refs(repository, "heads")

    def tags(self) -> dict[str, str]:
        return self._refs("common", "tags")

    def _refs(self, repository: str, namespace: str) -> dict[str, str]:
        if repository not in ("manifest", "common"):
            raise CatalogError("Only official manifest/common repositories are supported")
        # The common repository's full refs include Gerrit change refs
        # and can exceed the response limit or be truncated.
        # Gitiles returns only branch names, relative to refs/heads/, for
        # this namespace endpoint (including deprecated/... branches).
        response = self.get(repository + "/+refs/" + namespace + "?format=JSON")
        try:
            raw = response.decode("utf-8")
            if raw.startswith(")]}'\n"):
                raw = raw[5:]
            document = json.loads(raw)
        except (UnicodeError, ValueError) as error:
            raise CatalogError("Official Android refs are not valid Gitiles JSON") from error
        if not isinstance(document, dict):
            raise CatalogError("Official Android refs must be an object")
        refs = {}
        for branch, value in document.items():
            if not isinstance(branch, str) or not branch:
                raise CatalogError("Official Android branch refs contain an invalid name")
            commit = value.get("value") if isinstance(value, dict) else None
            if not isinstance(commit, str) or not SHA.fullmatch(commit):
                raise CatalogError("Official Android refs contain an invalid commit ID")
            if namespace == "tags" and "peeled" in value:
                commit = value["peeled"]
                if not isinstance(commit, str) or not SHA.fullmatch(commit):
                    raise CatalogError("Official Android tag contains an invalid peeled commit ID")
            ref = "refs/" + namespace + "/" + branch
            refs[ref] = commit.lower()
        return refs

    def makefile(self, commit: str) -> str:
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise CatalogError("Makefile reads require a full official commit ID")
        # A cold snapshot may need historical versions once. Pace those reads
        # instead of bursting through every monthly branch in a minute.
        time.sleep(1)
        raw = self.get("common/+/" + commit.lower() + "/Makefile?format=TEXT")
        try:
            return base64.b64decode(b"".join(raw.split()), validate=True).decode("utf-8")
        except (UnicodeError, binascii.Error, ValueError) as error:
            raise CatalogError("Official kernel Makefile is not valid Gitiles text") from error


def monthly_refs(refs: dict[str, str], repository: str) -> dict[tuple[str, str, str], tuple[str, str]]:
    if not isinstance(refs, dict) or not refs:
        raise CatalogError("Official Android refs are empty or invalid")
    rows = {}
    for ref, commit in refs.items():
        if not isinstance(ref, str) or not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise CatalogError("Official Android refs contain invalid ref/commit values")
        match = MONTHLY.fullmatch(ref)
        if not match:
            continue
        deprecated, prefix, android, family, date = match.groups()
        if bool(prefix) != (repository == "manifest"):
            continue
        if family not in KERNELS or KERNELS[family] != android:
            # Official Android also publishes other valid pairs, such as
            # android13-5.10. They are outside this catalog's five menu series.
            continue
        _month(date, "Official branch")
        identity = android, family, date
        # A live branch takes precedence over the same deprecated branch.
        if identity not in rows or not deprecated:
            rows[identity] = ref, commit.lower()
    return rows


def makefile_version(content: str, family: str) -> str:
    if not isinstance(content, str):
        raise CatalogError("Official kernel Makefile must be text")
    content = content.replace("\r\n", "\n")
    components = []
    for name in ("VERSION", "PATCHLEVEL", "SUBLEVEL"):
        declarations = re.findall(r"^[ \t]*" + name + r"[ \t]*[?:+]?=.*$", content, re.M)
        values = re.findall(r"^[ \t]*" + name + r"[ \t]*=[ \t]*(0|[1-9][0-9]*)[ \t]*(?:#[^\r\n]*)?$", content, re.M)
        if len(declarations) != 1 or len(values) != 1:
            raise CatalogError("Official kernel Makefile has missing or ambiguous version fields")
        components.append(values[0])
    return _full_version(".".join(components), family, "Official kernel Makefile")


def load_snapshot(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise CatalogError("Official ref snapshot must be a small regular JSON file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CatalogError("Cannot read the official ref snapshot") from error
    if (not isinstance(document, dict) or set(document) != {"schema_version", "versions"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or not isinstance(document["versions"], dict) or len(document["versions"]) > 10000):
        raise CatalogError("Unsupported official ref snapshot schema")
    versions = {}
    for commit, full in document["versions"].items():
        if not isinstance(commit, str) or not SHA.fullmatch(commit) or commit != commit.lower():
            raise CatalogError("Official ref snapshot contains an invalid commit ID")
        family = full.rsplit(".", 1)[0] if isinstance(full, str) else ""
        if family not in KERNELS:
            raise CatalogError("Official ref snapshot contains an unsupported kernel family")
        versions[commit] = _full_version(full, family, "Official ref snapshot")
    return versions


def save_snapshot(path: Path | None, versions: dict[str, str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise CatalogError("Official ref snapshot cannot be a symbolic link")
    staged = path.with_name(".refs-" + uuid.uuid4().hex + ".json")
    try:
        with staged.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump({"schema_version": 1, "versions": versions}, stream, sort_keys=True, indent=2)
            stream.write("\n")
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def discover_updates(root: Path, provider=None, *, snapshot_file: Path | None = None) -> tuple[dict, list[dict]]:
    document = load_catalog(root)
    versions = load_snapshot(snapshot_file)
    provider = provider or OfficialProvider()
    manifests = monthly_refs(provider.refs("manifest"), "manifest")
    common = monthly_refs(provider.refs("common"), "common")
    candidates = sorted(manifests.keys() & common.keys())
    if not candidates:
        raise CatalogError("No matching monthly branches found in official manifest/common refs")
    known = {(entry["android_version"], entry["kernel_version"], entry["kernel"], entry["date"])
             for entry in document["entries"]}
    # Full commit refs in the maintained catalog already bind their version to
    # that exact source. Mutable branch names alone are never a cache proof.
    fixed = {}
    tagged = {}
    for entry in document["entries"]:
        if entry["date"] == "lts":
            continue
        ref = entry.get("ref", "")
        if SHA.fullmatch(ref):
            commit = ref.lower()
            if commit in fixed and fixed[commit] != entry["kernel"]:
                raise CatalogError("The catalog assigns different kernel versions to one fixed commit")
            fixed[commit] = entry["kernel"]
        elif ref.startswith("refs/tags/"):
            key = entry["android_version"], entry["kernel_version"], entry["date"]
            tagged.setdefault(key, []).append(entry)
    tags = None
    added = []
    for android, family, date in candidates:
        _, commit = common[(android, family, date)]
        full = versions.get(commit) or fixed.get(commit)
        if full is None and (android, family, date) in tagged:
            if tags is None:
                tags = provider.tags()
            matches = {entry["kernel"] for entry in tagged[(android, family, date)]
                       if tags.get(entry["ref"]) == commit}
            if len(matches) > 1:
                raise CatalogError("Official tags assign different kernel versions to one commit")
            if matches:
                full = matches.pop()
        if full is None:
            full = makefile_version(provider.makefile(commit), family)
        full = _full_version(full, family, "Official ref snapshot")
        if commit not in versions:
            versions[commit] = full
            # Preserve each independently verified read, even if a later
            # request is rate limited. Candidates are recomputed against the
            # catalog on the next run, so unmerged versions are not lost.
            save_snapshot(snapshot_file, versions)
        identity = android, family, full, date
        if identity in known:
            continue
        row = _row(android, family, full, date, commit)
        added.append({"android_version": android, "kernel_version": family, "kernel": full,
                      "date": date, "ref": commit, "revision": row["revision"]})
        known.add(identity)
    # Preserve existing rows, including release tags, historical months and LTS.
    return {**document, "entries": document["entries"] + added}, added


def apply_candidates(root: Path, candidates: object, *, write: bool = True) -> dict:
    """Append exactly the previously probed immutable rows, with no network."""
    if not isinstance(candidates, list):
        raise CatalogError("Offline catalog candidates must be an array")
    validate_catalog({"schema_version": 1, "entries": candidates}, "Offline catalog candidates")
    fields = {"android_version", "kernel_version", "kernel", "date", "ref", "revision"}
    identities = set()
    for entry in candidates:
        if (set(entry) != fields or entry["date"] == "lts"
                or not isinstance(entry.get("ref"), str) or not SHA.fullmatch(entry["ref"])):
            raise CatalogError("Offline candidates must be complete monthly rows pinned to full commit IDs")
        identity = entry["android_version"], entry["kernel_version"], entry["kernel"], entry["date"]
        if identity in identities:
            raise CatalogError("Offline candidates contain a duplicate monthly identity")
        identities.add(identity)
    document = load_catalog(root)
    known = {(entry["android_version"], entry["kernel_version"], entry["kernel"], entry["date"])
             for entry in document["entries"]}
    added = []
    for entry in candidates:
        identity = entry["android_version"], entry["kernel_version"], entry["kernel"], entry["date"]
        if identity not in known:
            added.append({**entry, "ref": entry["ref"].lower()})
            known.add(identity)
    return save_catalog(root, {**document, "entries": document["entries"] + added}, added, write=write)


def save_catalog(root: Path, document: dict, added: list[dict], *, write: bool) -> dict:
    if write and added:
        destination = Path(root) / "data/kernels.json"
        temporary = destination.with_name(".kernels-" + uuid.uuid4().hex + ".json")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {"has_updates": bool(added), "added": len(added), "total": len(document["entries"]),
            "candidates_json": json.dumps(added, ensure_ascii=False, separators=(",", ":"))}


def update_catalog(root: Path, provider=None, *, write: bool = False, snapshot_file: Path | None = None) -> dict:
    document, added = discover_updates(root, provider, snapshot_file=snapshot_file)
    return save_catalog(root, document, added, write=write)


def main(argv=None, *, provider=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Report new official rows without writing (default)")
    mode.add_argument("--update", action="store_true", help="Append verified official rows to data/kernels.json")
    parser.add_argument("--apply-candidates-json", help="Apply previously probed monthly rows offline (implies writing)")
    parser.add_argument("--github-output", nargs="?", const="")
    parser.add_argument("--snapshot-file", type=Path,
                        help="Persist verified commit/version reads for incremental checks (also in --check mode)")
    args = parser.parse_args(argv)
    try:
        if args.apply_candidates_json is not None:
            if args.check:
                raise CatalogError("--check cannot be combined with --apply-candidates-json")
            try:
                candidates = json.loads(args.apply_candidates_json)
            except ValueError as error:
                raise CatalogError("Offline catalog candidates are not valid JSON") from error
            report = apply_candidates(args.root, candidates)
        else:
            report = update_catalog(args.root, provider, write=args.update, snapshot_file=args.snapshot_file)
        if args.github_output is not None:
            output = args.github_output or os.environ.get("GITHUB_OUTPUT")
            if not output:
                raise CatalogError("GITHUB_OUTPUT is unset; provide an explicit output path")
            with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
                for key, value in report.items():
                    stream.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
        print(json.dumps(report, separators=(",", ":")))
    except (ValueError, OSError, UnicodeError) as error:
        print(f"Official kernel catalog update failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
