#!/usr/bin/env python3
"""Freeze one full-history official SukiSU checkout for every job in a run.

Only committed Git objects and public version metadata leave the prepare job.
Git configuration, remote authentication headers and worktree files are never
copied into the bundle. A moving or rewritten upstream cannot change this run.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

from pin_build_dependencies import (CompatibilityError, TIERS, custom_configuration, load_profile,
                                    reviewed_tree_pairs, source_policy)
from manager_variants import MANAGERS, manager_profile


REPOSITORY = "SukiSU-Ultra/SukiSU-Ultra"
BUNDLE_NAME = "SukiSU.bundle"
MANIFEST_NAME = "source.json"
MAX_BUNDLE_SIZE = 1024 * 1024 * 1024
MAX_MANIFEST_SIZE = 16 * 1024
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
ROOT = Path(__file__).resolve().parents[1]


class SnapshotError(ValueError):
    """A snapshot cannot be proven to match the selected upstream source."""


@contextmanager
def temporary_directory(parent: Path, prefix: str):
    # mkdir's normal inherited access also works with restricted Windows
    # runners; tempfile's explicit mode 0700 can exclude their execution user.
    parent = parent.resolve(strict=True)
    directory = parent / (prefix + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        target = directory.resolve()
        if target.parent != parent or not target.name.startswith(prefix) or directory.is_symlink():
            raise SnapshotError("Refusing cleanup outside the temporary SukiSU directory")
        def remove_readonly(function, name, error):
            Path(name).chmod(stat.S_IWRITE | stat.S_IREAD)
            function(name)
        shutil.rmtree(target, onexc=remove_readonly)


def git(directory: Path, *arguments: str, timeout: int = 180) -> str:
    # In particular, never emit Git stderr: checkout configuration may carry
    # authentication, and remote error messages can repeat credential URLs.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_CONFIG_") and key not in {
                       "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                       "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_SHALLOW_FILE"}}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "-c", "core.hooksPath=", "-c", "core.autocrlf=false", *arguments],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SnapshotError(f"SukiSU Git {arguments[0]} could not complete") from error
    if result.returncode:
        raise SnapshotError(f"SukiSU Git {arguments[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


def expected_commit(value: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise SnapshotError("SukiSU snapshot requires a full 40-character commit SHA")
    return value.lower()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_identity(source: Path, reference: str) -> dict:
    if git(source, "rev-parse", "--verify", "HEAD").lower() != reference:
        raise SnapshotError("SukiSU checkout HEAD does not match the selected commit")
    if git(source, "rev-parse", "--is-shallow-repository") != "false":
        raise SnapshotError("SukiSU source history is shallow; native APK version would be incorrect")
    count = git(source, "rev-list", "--count", "HEAD")
    description = git(source, "describe", "--tags", "--always")
    tree = git(source, "rev-parse", "--verify", "HEAD^{tree}").lower()
    if not count.isdigit() or int(count) < 1 or not SHA.fullmatch(tree):
        raise SnapshotError("SukiSU source returned invalid commit history metadata")
    if (not description or len(description) > 1024
            or any(ord(character) < 32 or ord(character) == 127 for character in description)):
        raise SnapshotError("SukiSU source returned an invalid version description")
    return {"git_count": int(count), "git_describe": description, "git_tree": tree}


def check_official_source(source: Path, variant: str = 'SukiSU') -> None:
    # actions/checkout uses an authentication extraheader in .git/config. That
    # header is intentionally left local. Also reject credential-bearing URLs.
    urls = git(source, "remote", "get-url", "--all", "origin").splitlines()
    repository = manager_profile(variant)['repository']
    allowed = {f"https://github.com/{repository}", f"https://github.com/{repository}.git",
               f"git@github.com:{repository}.git", f"git@github.com:{repository}"}
    if not urls or any(url not in allowed for url in urls):
        raise SnapshotError("SukiSU source origin must be the official public repository without URL credentials")
    if git(source, "status", "--porcelain", "--untracked-files=all"):
        raise SnapshotError("SukiSU source checkout must be clean before snapshotting")


def verify_susfs_compatibility(source: Path, root: Path, tier: str, susfs: bool, bundle: Path) -> None:
    if not susfs or tier not in TIERS or custom_configuration(root):
        return
    try:
        profile = load_profile(root)
        reviewed = reviewed_tree_pairs(profile)
        policy = source_policy(profile)
    except CompatibilityError as error:
        raise SnapshotError(str(error)) from error
    reference = git(source, "rev-parse", "HEAD").lower()
    if profile.get("version") != "v2.3.0":
        raise SnapshotError("SukiSU prepare validation requires the SUSFS v2.3.0 compatibility profile")
    if reviewed or policy == "checked-patch":
        found = tuple(git(source, "rev-parse", "--verify", f"HEAD:{path}").lower()
                      for path in ("kernel", "uapi"))
        if found not in reviewed and policy != "checked-patch":
            raise SnapshotError("SukiSU kernel tree changed or its UAPI pair is unreviewed; "
                                "adapt and review the SUSFS 2.3 port before building the matrix")
    elif reference not in {commit.lower() for commit in profile["ksu_commits"]}:
        raise SnapshotError("SukiSU commit has not been reviewed for the SUSFS 2.3 port")
    # Run the complete patch and native lifecycle checks on an isolated copy.
    # The manager and the exported source always retain the original tree.
    patch = (root / "tools/patches/sukisu-susfs23.patch").resolve(strict=True)
    from apply_susfs23 import apply_reviewed_port
    with temporary_directory(source.parent, ".gki-sukisu-port-") as temporary_root:
        checked = temporary_root / "source"
        git(temporary_root, "clone", "--quiet", "--config", "core.autocrlf=false", "--no-checkout", str(bundle), str(checked))
        git(checked, "checkout", "--quiet", "--detach", reference)
        apply_reviewed_port(checked, patch, allow_variants=True)


def snapshot_source(source: Path, output: Path, reference: str, *, root: Path = ROOT,
                    susfs: bool = False, tier: str = "Custom(自定义)", variant: str = 'SukiSU',
                    susfs_sources: dict | None = None) -> dict:
    repository = manager_profile(variant)['repository']
    reference = expected_commit(reference)
    source = source.resolve(strict=True)
    root = root.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise SnapshotError("SukiSU snapshot output already exists; refusing to overwrite it")
    if not output.parent.is_dir():
        raise SnapshotError("SukiSU snapshot parent directory does not exist")
    check_official_source(source, variant)
    identity = source_identity(source, reference)
    dependency_refs = []
    if susfs_sources is not None:
        from shared_susfs_source import NAMESPACE, verify_local_sources
        susfs_sources = verify_local_sources(source, susfs_sources)
        dependency_refs = [NAMESPACE + target for target in susfs_sources]
    with temporary_directory(output.parent, ".gki-snapshot-") as temporary:
        prepared = temporary / "prepared"
        prepared.mkdir()
        bundle = prepared / BUNDLE_NAME
        # Tags preserve upstream git describe/versionName. HEAD includes all
        # ancestors, preserving rev-list/versionCode even after a force push.
        git(source, "bundle", "create", str(bundle), "HEAD", "--tags", *dependency_refs, timeout=300)
        size = bundle.stat().st_size
        if not 0 < size <= MAX_BUNDLE_SIZE:
            raise SnapshotError("SukiSU source bundle exceeds the allowed size")
        # BakaSU has its own native SUSFS integration. Its original source
        # must never receive the separately reviewed SukiSU-only port here.
        if variant == 'SukiSU':
            verify_susfs_compatibility(source, root, tier, susfs, bundle)
        manifest = {"schema_version": 1, "repository": repository, "ref": reference,
                    "bundle_filename": BUNDLE_NAME, "bundle_size": size,
                    "bundle_sha256": file_digest(bundle), **identity}
        if susfs_sources is not None:
            manifest['susfs_sources'] = susfs_sources
        (prepared / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8", newline="\n")
        # Perform the same real restoration every consumer will perform, so a
        # prepare success proves the exported history/version is self-contained.
        from checkout_ksu_snapshot import checkout_snapshot
        checkout_snapshot(prepared, temporary / "verified", reference, variant=variant)
        if output.exists() or output.is_symlink():
            raise SnapshotError("SukiSU snapshot output appeared while preparing; refusing to overwrite it")
        prepared.rename(output)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-ref", required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--susfs", required=True, choices=("true", "false"))
    parser.add_argument("--tier", required=True)
    parser.add_argument('--variant', choices=MANAGERS, default=os.environ.get('KSU_VARIANT', 'SukiSU'))
    parser.add_argument('--susfs-sources', type=Path, help='Prepared shared SUSFS target identities')
    args = parser.parse_args(argv)
    try:
        dependencies = None
        if args.susfs_sources is not None:
            from shared_susfs_source import read_json, validate_sources
            if args.susfs_sources.is_symlink() or not args.susfs_sources.is_file() or args.susfs_sources.stat().st_size > MAX_MANIFEST_SIZE:
                raise SnapshotError('Prepared shared SUSFS metadata file is missing or invalid')
            dependencies = validate_sources(read_json(args.susfs_sources.read_text(encoding='utf-8')))
        manifest = snapshot_source(args.source, args.output, args.expected_ref, root=args.root,
                                   susfs=args.susfs == "true", tier=args.tier, variant=args.variant,
                                   susfs_sources=dependencies)
    except (OSError, ValueError, UnicodeError) as error:
        print(f"::error::SukiSU source snapshot failed: {error}", file=sys.stderr)
        return 1
    print(f"{args.variant} immutable source: {manifest['ref']} ({manifest['git_count']} commits)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
