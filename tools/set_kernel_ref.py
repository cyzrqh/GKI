#!/usr/bin/env python3
"""Select the exact GKI common revision without changing other manifest projects."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET


COMMON_URL = "https://android.googlesource.com/kernel/common"
BRANCH_PATTERN = re.compile(r"android[0-9]+-[0-9]+\.[0-9]+-(?:[0-9]{4}-(?:0[1-9]|1[0-2])|lts)\Z")
COMMIT_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")


class KernelRefTransportError(ValueError):
    """The official server could not be queried; this is not a missing ref."""


def _transport_detail(error: Exception) -> str:
    """Keep useful transport codes without echoing credentials or proxy URLs."""
    if isinstance(error, subprocess.TimeoutExpired):
        return "Git query timed out"
    if isinstance(error, HTTPError):
        return f"Gitiles HTTP {error.code}"
    if isinstance(error, subprocess.CalledProcessError):
        raw = error.stderr or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        details = [f"Git transport exit {error.returncode}"]
        if match := re.search(r"(?:HTTP[^\r\n]{0,40}?|error:\s*)([45][0-9]{2})\b", raw, re.I):
            details.append("HTTP " + match[1])
        if match := re.search(r"\bcurl\s+([0-9]{1,3})\b", raw, re.I):
            details.append("curl " + match[1])
        for pattern, label in ((r"TLS|SSL|GnuTLS", "TLS failure"),
                               (r"resolve.*host", "name resolution failed"),
                               (r"connection.*reset", "connection reset"),
                               (r"timed? out|timeout", "connection timed out")):
            if re.search(pattern, raw, re.I):
                details.append(label)
        return ", ".join(details)
    return "Gitiles connection failed"


def _query_git_refs(patterns: tuple[str, ...], timeout: int = 60) -> str:
    """Retry the same official refs, using HTTP/1.1 after a transport failure."""
    for attempt in range(3):
        command = ["git"]
        if attempt:
            command += ["-c", "http.version=HTTP/1.1"]
        command += ["ls-remote", "--refs", COMMON_URL, *patterns]
        try:
            return subprocess.run(command, check=True, text=True, encoding="utf-8",
                                  errors="replace", capture_output=True, timeout=timeout).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            detail = _transport_detail(error)
            print(f"Official kernel ref query {attempt + 1}/3 failed: {detail}", file=sys.stderr)
            if attempt == 2:
                raise KernelRefTransportError("Official Git ref query failed after 3 attempts: " + detail) from None
            time.sleep(2 * (attempt + 1))
    raise AssertionError("Unreachable Git retry state")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _json_pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate field in official Gitiles ref response")
        result[name] = value
    return result


def _gitiles_named_ref(ref: str) -> bool:
    """Read one exact official ref, never a ref prefix or a different mirror."""
    url = COMMON_URL + "/+" + ref + "?format=JSON"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "gki-kernel-ref"})
    for attempt in range(2):
        try:
            with build_opener(_NoRedirect()).open(request, timeout=20) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ValueError("Unexpected official Gitiles ref response")
                raw = response.read(8193)
            break
        except (HTTPError, URLError, OSError) as error:
            if isinstance(error, HTTPError) and error.code == 404:
                # A missing exact live name may have moved to its same-name
                # deprecated alias. That alias still needs its own proof.
                return False
            detail = _transport_detail(error)
            # Redirects, authorization errors and unknown layouts cannot be
            # interpreted as a missing branch or retried against another host.
            retryable = not isinstance(error, HTTPError) or error.code in (408, 429, 500, 502, 503, 504)
            if attempt == 1 or not retryable:
                raise KernelRefTransportError("Official Gitiles ref query failed: " + detail) from None
            time.sleep(2)
    if len(raw) > 8192 or not raw.startswith(b")]}'\n"):
        raise ValueError("Invalid or oversized official Gitiles ref response")
    try:
        data = json.loads(raw[5:], object_pairs_hook=_json_pairs)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid official Gitiles ref JSON") from error
    if not isinstance(data, dict) or set(data) - {ref}:
        raise ValueError("Official Gitiles returned a different kernel ref")
    if ref not in data:
        return False
    entry = data[ref]
    if (not isinstance(entry, dict) or set(entry) != {"value"}
            or not isinstance(entry["value"], str) or not COMMIT_PATTERN.fullmatch(entry["value"])):
        raise ValueError("Official Gitiles ref requires a complete commit ID")
    return True


def validate_ref(ref: str) -> str:
    """Accept full branch/tag names or immutable full commit IDs."""
    if isinstance(ref, str) and re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", ref):
        return ref.lower()
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
    output = _query_git_refs((active, deprecated, f"refs/tags/{branch}_r*"))
    refs: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and COMMIT_PATTERN.fullmatch(fields[0]):
            refs.add(fields[1])
    for preferred in (active, deprecated):
        if preferred in refs:
            return preferred
    tags = [(int(match.group(1)), ref) for ref in refs
            if (match := tag_pattern.fullmatch(ref))]
    if tags:
        return max(tags)[1]
    raise ValueError(f"No live/deprecated branch or release tag found for {branch}")


def resolve_named_kernel_ref(branch: str, preferred_ref: str) -> str:
    """Keep a named GKI branch, allowing only its live/deprecated relocation."""
    validate_branch(branch)
    aliases = (f"refs/heads/{branch}", f"refs/heads/deprecated/{branch}")
    if preferred_ref not in aliases:
        raise ValueError("The named kernel ref must match the selected GKI branch")
    try:
        output = _query_git_refs(aliases)
    except KernelRefTransportError as git_error:
        # Git's smart-HTTP transport can fail even when Gitiles serves the
        # same official repository. Check only the explicitly selected name
        # and its live/deprecated alias. No release tag, month or mirror can
        # be substituted when the selected branch cannot be verified.
        ordered = (preferred_ref, *(alias for alias in aliases if alias != preferred_ref))
        print("Checking the same official kernel branch through Gitiles", file=sys.stderr)
        try:
            for alias in ordered:
                if _gitiles_named_ref(alias):
                    return alias
        except KernelRefTransportError as gitiles_error:
            raise KernelRefTransportError(str(git_error) + "; " + str(gitiles_error)) from None
        raise ValueError(f"No live/deprecated branch found for explicitly selected {branch}") from None
    refs: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if (len(fields) == 2 and COMMIT_PATTERN.fullmatch(fields[0])
                and fields[1] in aliases):
            refs.add(fields[1])
    if preferred_ref in refs:
        return preferred_ref
    for alias in aliases:
        if alias in refs:
            return alias
    raise ValueError(f"No live/deprecated branch found for explicitly selected {branch}")


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
    named_resolver: Callable[[str, str], str] | None = None,
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
    aliases = (f"refs/heads/{branch}", f"refs/heads/deprecated/{branch}")
    if kernel_ref in aliases:
        effective_ref = (named_resolver or resolve_named_kernel_ref)(branch, kernel_ref)
        if effective_ref not in aliases:
            raise ValueError("Named kernel resolution changed the selected GKI branch")
        effective_ref = validate_ref(effective_ref)
    else:
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
