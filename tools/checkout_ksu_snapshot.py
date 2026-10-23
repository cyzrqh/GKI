#!/usr/bin/env python3
"""Restore and verify the immutable SukiSU source prepared for this run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from snapshot_ksu_source import (BUNDLE_NAME, MANIFEST_NAME, MAX_BUNDLE_SIZE,
                                 MAX_MANIFEST_SIZE, REPOSITORY, SHA, SnapshotError,
                                 expected_commit, file_digest, git, source_identity,
                                 temporary_directory)


def read_manifest(directory: Path, reference: str) -> dict:
    manifest_path = directory / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > MAX_MANIFEST_SIZE:
        raise SnapshotError("SukiSU snapshot manifest is missing or invalid")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise SnapshotError("SukiSU snapshot manifest is not valid JSON") from error
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or manifest.get("repository") != REPOSITORY
            or manifest.get("ref") != reference or manifest.get("bundle_filename") != BUNDLE_NAME):
        raise SnapshotError("SukiSU snapshot manifest does not match the selected official source")
    if (type(manifest.get("bundle_size")) is not int or not 0 < manifest["bundle_size"] <= MAX_BUNDLE_SIZE
            or not isinstance(manifest.get("bundle_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["bundle_sha256"])
            or type(manifest.get("git_count")) is not int or manifest["git_count"] < 1
            or not isinstance(manifest.get("git_tree"), str) or not SHA.fullmatch(manifest["git_tree"])
            or not isinstance(manifest.get("git_describe"), str) or not manifest["git_describe"]
            or len(manifest["git_describe"]) > 1024
            or any(ord(character) < 32 or ord(character) == 127 for character in manifest["git_describe"])):
        raise SnapshotError("SukiSU snapshot has invalid bundle or version metadata")
    return manifest


def checkout_snapshot(directory: Path, output: Path, reference: str) -> dict:
    reference = expected_commit(reference)
    directory = directory.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise SnapshotError("SukiSU checkout output already exists; refusing to overwrite it")
    if not output.parent.is_dir():
        raise SnapshotError("SukiSU checkout parent directory does not exist")
    manifest = read_manifest(directory, reference)
    bundle = directory / BUNDLE_NAME
    if (bundle.is_symlink() or not bundle.is_file()
            or bundle.stat().st_size != manifest["bundle_size"]
            or file_digest(bundle) != manifest["bundle_sha256"]):
        raise SnapshotError("SukiSU source bundle size or SHA-256 does not match its manifest")
    with temporary_directory(output.parent, ".gki-checkout-") as temporary_root:
        verifier = temporary_root / "verify.git"
        verifier.mkdir()
        git(verifier, "init", "--quiet", "--bare")
        # Verification in an empty repository rejects thin/incremental bundles
        # whose prerequisites a later runner would need to fetch upstream.
        git(verifier, "bundle", "verify", str(bundle), timeout=300)
        prepared = temporary_root / "prepared"
        git(temporary_root, "clone", "--quiet", "--config", "core.autocrlf=false", "--no-checkout", str(bundle), str(prepared), timeout=300)
        git(prepared, "checkout", "--quiet", "--detach", reference)
        identity = source_identity(prepared, reference)
        if any(identity[field] != manifest[field] for field in identity):
            raise SnapshotError("SukiSU snapshot history or version does not match the prepared source")
        git(prepared, "fsck", "--connectivity-only", "--no-dangling", timeout=300)
        # Point origin at the public repository only after fully offline restore.
        # No subsequent fetch is needed or performed by this tool.
        git(prepared, "remote", "set-url", "origin", f"https://github.com/{REPOSITORY}.git")
        if output.exists() or output.is_symlink():
            raise SnapshotError("SukiSU checkout output appeared while preparing; refusing to overwrite it")
        prepared.rename(output)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-ref", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = checkout_snapshot(args.input, args.output, args.expected_ref)
    except (OSError, ValueError, UnicodeError) as error:
        print(f"::error::SukiSU snapshot checkout failed: {error}", file=sys.stderr)
        return 1
    print(f"SukiSU restored from this run's immutable source: {manifest['ref']} ({manifest['git_count']} commits)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
