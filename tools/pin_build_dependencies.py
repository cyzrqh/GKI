#!/usr/bin/env python3
"""Select a complete SUSFS source tree compatible with a known fixed KSU build."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
TARGET = re.compile(r"android[0-9]+-[0-9]+\.[0-9]+\Z")
TIERS = frozenset(("Stable(标准)", "Dev(开发)"))


class CompatibilityError(RuntimeError):
    """Stop before mixing incompatible build dependencies."""


def load_profile(root: Path) -> dict:
    try:
        document = json.loads((root / "build-compatibility.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CompatibilityError("Cannot read build-compatibility.json") from error
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise CompatibilityError("Unsupported build compatibility schema")
    profile = document.get("sukisu_susfs")
    if not isinstance(profile, dict):
        raise CompatibilityError("Missing SukiSU SUSFS compatibility profile")
    commits, tiers, pins, version = (profile.get(name) for name in ("ksu_commits", "tiers", "pins", "version"))
    if (not isinstance(commits, list) or not commits
            or any(not isinstance(value, str) or not SHA.fullmatch(value) for value in commits)):
        raise CompatibilityError("Compatibility KSU commits must be full 40-character SHAs")
    if (not isinstance(tiers, list) or not tiers
            or any(not isinstance(value, str) or value not in TIERS for value in tiers)):
        raise CompatibilityError("Compatibility tiers must be Stable or Dev")
    if not isinstance(version, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise CompatibilityError("Invalid compatibility SUSFS version")
    if (not isinstance(pins, dict) or not pins
            or any(not isinstance(key, str) or not TARGET.fullmatch(key)
                   or not isinstance(value, str) or not SHA.fullmatch(value) for key, value in pins.items())):
        raise CompatibilityError("Compatibility SUSFS pins must map kernel targets to full SHAs")
    return profile


def custom_configuration(root: Path) -> bool:
    path = root / "config/config"
    if not path.is_file():
        return False
    # An explicit custom=true takes precedence, including a deliberately empty
    # per-target ref (which the upstream builder interprets as tracking latest).
    return bool(re.search(r"^\s*custom\s*=\s*true\s*(?:#.*)?$", path.read_text(encoding="utf-8"), re.M | re.I))


def selected_pin(root: Path, variant: str, ksu_ref: str, tier: str, target: str) -> tuple[str, str] | None:
    if not TARGET.fullmatch(target):
        raise CompatibilityError("Invalid Android/kernel target")
    if variant != "SukiSU" or tier not in TIERS:
        return None
    profile = load_profile(root)
    if tier not in profile["tiers"] or ksu_ref.lower() not in {value.lower() for value in profile["ksu_commits"]}:
        return None
    if custom_configuration(root):
        return None
    if target not in profile["pins"]:
        raise CompatibilityError(f"No compatible SUSFS pin for {target}; review build-compatibility.json")
    return profile["pins"][target].lower(), profile["version"]


def git(directory: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *arguments], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CompatibilityError(f"SUSFS git {arguments[0]} could not complete") from error
    if result.returncode:
        # Do not echo a remote's response or URL: either may contain credentials.
        raise CompatibilityError(f"SUSFS git {arguments[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


def pin_dependencies(root: Path, susfs_dir: Path, variant: str, ksu_ref: str, tier: str, target: str) -> bool:
    root = root.resolve()
    pin = selected_pin(root, variant, ksu_ref, tier, target)
    if pin is None:
        print("Preserving the selected upstream/custom SUSFS configuration.")
        return False
    commit, version = pin
    directory = susfs_dir.resolve()
    if directory == root or not directory.is_relative_to(root) or not (directory / ".git").exists():
        raise CompatibilityError("SUSFS directory must be a separate cloned repository inside the workspace")
    git(directory, "fetch", "--depth=1", "origin", commit)
    git(directory, "checkout", "--detach", commit)
    if git(directory, "rev-parse", "HEAD").lower() != commit:
        raise CompatibilityError("SUSFS checkout does not match the compatibility pin")
    header = directory / "kernel_patches/include/linux/susfs.h"
    try:
        text = header.read_text(encoding="utf-8")
    except OSError as error:
        raise CompatibilityError("The pinned SUSFS source is missing susfs.h") from error
    found = re.search(r'^\s*#define\s+SUSFS_VERSION\s+"([^"]+)"', text, re.M)
    if not found or found.group(1) != version:
        raise CompatibilityError(f"Pinned SUSFS must declare {version}")
    date = git(directory, "log", "-1", "--date=format:%Y-%m-%d %H:%M:%S %z", "--format=%cd")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}", date):
        raise CompatibilityError("SUSFS returned an invalid commit date")
    output = os.environ.get("GITHUB_ENV")
    if output:
        with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"SUSFS_LATEST_COMMIT_DATE={date}\nPINNED_SUSFS_SHA={commit}\nSUSFS_COMPAT_VERSION={version}\n")
    print(f"Selected compatible SUSFS {version} for {target}: {commit}")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--susfs-dir", type=Path, required=True)
    parser.add_argument("--ksu-variant", required=True)
    parser.add_argument("--ksu-ref", required=True)
    parser.add_argument("--ksu-tier", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args(argv)
    try:
        pin_dependencies(args.root, args.susfs_dir, args.ksu_variant, args.ksu_ref, args.ksu_tier, args.target)
    except (CompatibilityError, OSError, UnicodeError) as error:
        print(f"SUSFS compatibility selection failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
