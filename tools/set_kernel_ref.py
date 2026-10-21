#!/usr/bin/env python3
"""Select the exact GKI common revision without changing other manifest projects."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from typing import Callable
import xml.etree.ElementTree as ET


COMMON_URL = "https://android.googlesource.com/kernel/common"
BRANCH_PATTERN = re.compile(r"android[0-9]+-[0-9]+\.[0-9]+-(?:[0-9]{4}-(?:0[1-9]|1[0-2])|lts)\Z")


def validate_ref(ref: str) -> str:
    """Accept full branch/tag names, excluding Git's special revision syntax."""
    if not isinstance(ref, str) or not re.fullmatch(r"refs/(?:heads|tags)/[A-Za-z0-9._/-]+", ref):
        raise ValueError(f"Invalid kernel ref: {ref!r}")
    parts = ref.split("/")
    if (".." in ref or "//" in ref or ref.endswith(("/", "."))
            or any(not part or part.startswith(".") or part.endswith(".lock") for part in parts)):
        raise ValueError(f"Invalid kernel ref: {ref!r}")
    return ref


def validate_branch(branch: str) -> str:
    if not isinstance(branch, str) or not BRANCH_PATTERN.fullmatch(branch):
        raise ValueError(f"Invalid GKI branch: {branch!r}")
    return branch


def resolve_kernel_ref(branch: str) -> str:
    """Prefer a live branch, then its deprecated branch, then the latest r tag."""
    validate_branch(branch)
    active = f"refs/heads/{branch}"
    deprecated = f"refs/heads/deprecated/{branch}"
    tag_pattern = re.compile(rf"refs/tags/{re.escape(branch)}_r([0-9]+)\Z")
    result = subprocess.run(
        ["git", "ls-remote", "--refs", COMMON_URL, active, deprecated,
         f"refs/tags/{branch}_r*"],
        check=True, text=True, encoding="utf-8", capture_output=True, timeout=120,
    )
    refs: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and re.fullmatch(r"[0-9a-fA-F]{40,64}", fields[0]):
            refs.add(fields[1])
    for preferred in (active, deprecated):
        if preferred in refs:
            return preferred
    tags = [(int(match.group(1)), ref) for ref in refs
            if (match := tag_pattern.fullmatch(ref))]
    if tags:
        return max(tags)[1]
    raise ValueError(f"No live/deprecated branch or release tag found for {branch}")


def _manifest_path(root: Path, name: str) -> Path:
    pure = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or pure.is_absolute()
            or any(part in ("..", ".git") for part in pure.parts)
            or pure.suffix.lower() != ".xml"):
        raise ValueError(f"Invalid manifest include: {name!r}")
    path = root.joinpath(*pure.parts)
    # Check each component before resolving so links cannot silently leave the tree.
    cursor = root
    for part in pure.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Manifest symlinks are not supported: {name!r}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"Missing or out-of-tree manifest include: {name!r}")
    return resolved


def _load_manifests(root: Path) -> list[tuple[Path, ET.ElementTree]]:
    loaded: dict[Path, ET.ElementTree] = {}
    visiting: set[Path] = set()

    def visit(name: str) -> None:
        path = _manifest_path(root, name)
        if path in visiting:
            raise ValueError(f"Cyclic manifest include: {name!r}")
        if path in loaded:
            return
        visiting.add(path)
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        tree = ET.parse(path, parser=parser)
        if tree.getroot().tag != "manifest":
            raise ValueError(f"Expected <manifest> in {name!r}")
        for include in tree.getroot().findall("include"):
            visit(include.get("name", ""))
        visiting.remove(path)
        loaded[path] = tree

    visit("default.xml")
    return list(loaded.items())


def set_kernel_ref(
    manifest_root: Path | str,
    kernel_ref: str,
    branch: str,
    resolver: Callable[[str], str] | None = None,
) -> tuple[str, int]:
    """Patch common projects reachable from default.xml, validating before writes."""
    validate_branch(branch)
    if kernel_ref:
        validate_ref(kernel_ref)
    root = Path(manifest_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Manifest root is not a directory: {root}")
    manifests = _load_manifests(root)
    changed: list[tuple[Path, ET.ElementTree]] = []
    count = 0
    for path, tree in manifests:
        common = [p for p in tree.getroot().findall("project")
                  if p.get("path") == "common" or p.get("name") == "kernel/common"]
        if common:
            changed.append((path, tree))
            count += len(common)
    if not count:
        raise ValueError("No common kernel project found in default.xml or its includes")
    effective_ref = validate_ref(kernel_ref or (resolver or resolve_kernel_ref)(branch))

    # Resolve and validate every include and ref before touching any source file.
    staged: list[tuple[Path, Path]] = []
    try:
        for path, tree in changed:
            for project in tree.getroot().findall("project"):
                if project.get("path") == "common" or project.get("name") == "kernel/common":
                    project.set("revision", effective_ref)
            fd, temporary = tempfile.mkstemp(prefix=".gki-manifest-", suffix=".xml", dir=path.parent)
            os.close(fd)
            temporary_path = Path(temporary)
            staged.append((temporary_path, path))
            tree.write(temporary_path, encoding="utf-8", xml_declaration=True)
        for temporary_path, destination in staged:
            os.replace(temporary_path, destination)
    finally:
        for temporary_path, _ in staged:
            temporary_path.unlink(missing_ok=True)
    return effective_ref, count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-root", required=True, type=Path)
    parser.add_argument("--kernel-ref", default="")
    parser.add_argument("--branch", required=True)
    args = parser.parse_args()
    try:
        ref, count = set_kernel_ref(args.manifest_root, args.kernel_ref, args.branch)
    except (ValueError, OSError, ET.ParseError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Kernel manifest update failed: {error}\n")
    print(f"Set {count} common project(s) to {ref}")


if __name__ == "__main__":
    main()
