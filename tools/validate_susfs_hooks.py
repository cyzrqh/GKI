#!/usr/bin/env python3
"""Read-only preflight for SUSFS direct hooks in the patched kernel and KSU trees."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys


HOOKS = (
    "ksu_preinit_handle_post_execveat",
    "ksu_setup_preinit_file",
    "my_setprocattr",
    "my_write_context",
    "my_write_access",
)
HOOK_PATTERN = re.compile(r"\b(" + "|".join(HOOKS) + r")\s*\(")
SKIP_DIRECTORIES = {".git", ".repo", ".upstream", "__pycache__", "out", "dist"}
COMPATIBILITY_MESSAGE = "请使用相互兼容的SUSFS和KernelSU版本。"


@dataclass(frozen=True)
class Location:
    path: Path
    line: int


@dataclass(frozen=True)
class Definition:
    symbol: str
    location: Location
    is_static: bool


@dataclass(frozen=True)
class ValidationResult:
    kernel_source: Path
    ksu_dir: Path | None
    calls: dict[str, tuple[Location, ...]]
    definitions: tuple[Definition, ...]
    missing: dict[str, tuple[Location, ...]]

    @property
    def ok(self) -> bool:
        return not self.missing


def strip_comments_and_literals(source: str) -> str:
    """Blank C comments and literals, retaining offsets and line numbers."""
    result = list(source)
    index = 0
    while index < len(source):
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end < 0 else end + 2
        elif source.startswith("//", index):
            end = index + 2
            while end < len(source):
                if source[end] == "\n":
                    previous = end - 1
                    if previous >= index and source[previous] == "\r":
                        previous -= 1
                    if previous < index or source[previous] != "\\":
                        break
                end += 1
        elif source[index] in "\"'":
            quote = source[index]
            end = index + 1
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                elif source[end] == quote:
                    end += 1
                    break
                else:
                    end += 1
            end = min(end, len(source))
        else:
            index += 1
            continue
        for position in range(index, end):
            if result[position] not in "\r\n":
                result[position] = " "
        index = end
    clean = "".join(result)
    # Preprocessor directives are not C calls/definitions. Retain their newlines.
    lines = clean.splitlines(keepends=True)
    continued = False
    for number, line in enumerate(lines):
        directive = continued or line.lstrip().startswith("#")
        continued = directive and line.rstrip("\r\n").rstrip().endswith("\\")
        if directive:
            lines[number] = "".join(character if character in "\r\n" else " " for character in line)
    return "".join(lines)


def _balanced_end(code: str, opening: int) -> int | None:
    if opening >= len(code) or code[opening] != "(":
        return None
    depth = 0
    for position in range(opening, len(code)):
        if code[position] == "(":
            depth += 1
        elif code[position] == ")":
            depth -= 1
            if depth == 0:
                return position + 1
    return None


def _body_start(code: str, after_parameters: int) -> int | None:
    """Recognize a definition's optional attributes followed by its body."""
    position = after_parameters
    while position < len(code):
        while position < len(code) and code[position].isspace():
            position += 1
        if position >= len(code):
            return None
        if code[position] == "{":
            return position
        token = re.match(r"[A-Za-z_]\w*", code[position:])
        if not token:
            return None
        position += len(token.group())
        while position < len(code) and code[position].isspace():
            position += 1
        if position < len(code) and code[position] == "(":
            end = _balanced_end(code, position)
            if end is None:
                return None
            position = end
    return None


def _local_prototype(prefix: str) -> bool:
    """Exclude ordinary block-scope declarations, not expression calls."""
    if not prefix.strip() or not re.fullmatch(r"[A-Za-z_\w\s*]+", prefix):
        return False
    words = re.findall(r"[A-Za-z_]\w*", prefix)
    return bool(words) and not any(
        word in {"return", "case", "goto", "sizeof", "else", "do", "break", "continue"}
        for word in words
    )


def inspect_c_source(path: Path, source: str) -> tuple[list[tuple[str, Location]], list[Definition]]:
    """Find direct function-body calls and genuine function definitions."""
    code = strip_comments_and_literals(source)
    matches = list(HOOK_PATTERN.finditer(code))
    calls: list[tuple[str, Location]] = []
    definitions: list[Definition] = []
    if not matches:
        return calls, definitions
    depth = 0
    top_level_start = 0
    statement_start = 0
    scanned = 0
    for match in matches:
        while scanned < match.start():
            character = code[scanned]
            if character == "{":
                depth += 1
                statement_start = scanned + 1
            elif character == "}":
                depth = max(0, depth - 1)
                statement_start = scanned + 1
                if depth == 0:
                    top_level_start = scanned + 1
            elif character == ";":
                statement_start = scanned + 1
                if depth == 0:
                    top_level_start = scanned + 1
            scanned += 1
        closing = _balanced_end(code, code.index("(", match.start(), match.end()))
        if closing is None:
            continue
        location = Location(path, code.count("\n", 0, match.start()) + 1)
        body = _body_start(code, closing)
        if depth == 0 and body is not None:
            prefix = code[top_level_start:match.start()]
            if prefix.strip() and not re.search(r"\btypedef\b", prefix):
                definitions.append(Definition(match.group(1), location, bool(re.search(r"\bstatic\b", prefix))))
        elif depth > 0 and body is None and not _local_prototype(code[statement_start:match.start()]):
            calls.append((match.group(1), location))
    return calls, definitions


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _c_files(root: Path, excluded: tuple[Path, ...] = ()):
    """Visit resolved source paths once, including legal symlinked source trees."""
    visited_directories: set[Path] = set()
    visited_files: set[Path] = set()
    for current, directories, files in os.walk(root, followlinks=True):
        actual = Path(current).resolve()
        if actual in visited_directories or any(_inside(actual, parent) for parent in excluded):
            directories[:] = []
            continue
        visited_directories.add(actual)
        directories[:] = sorted(name for name in directories
                                 if name not in SKIP_DIRECTORIES and not name.startswith("bazel-"))
        for name in sorted(files):
            if not name.endswith(".c"):
                continue
            candidate = (Path(current) / name).resolve()
            if candidate in visited_files or any(_inside(candidate, parent) for parent in excluded):
                continue
            if candidate.is_file():
                visited_files.add(candidate)
                yield candidate


def resolve_ksu_dir(kernel_root: Path, explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        candidate = explicit if explicit.is_absolute() else kernel_root / explicit
        candidate = candidate.resolve(strict=True)
        if not candidate.is_dir():
            raise ValueError(f"KernelSU path is not a directory: {candidate}")
        return candidate
    for relative in ("common/drivers/kernelsu", "drivers/kernelsu", "KernelSU/kernel"):
        candidate = kernel_root / relative
        if candidate.is_dir():
            return candidate.resolve(strict=True)
    return None


def validate_hooks(kernel_root: Path, ksu_dir: Path | None = None) -> ValidationResult:
    """Validate current source bytes without modifying, executing or importing them."""
    root = Path(kernel_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Kernel root is not a directory: {root}")
    source = (root / "common").resolve() if (root / "common").is_dir() else root
    ksu = resolve_ksu_dir(root, Path(ksu_dir) if ksu_dir is not None else None)
    calls: dict[str, list[Location]] = {}
    definitions: list[Definition] = []
    for path in _c_files(source, (ksu,) if ksu is not None else ()):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(symbol in text for symbol in HOOKS):
            continue
        file_calls, file_definitions = inspect_c_source(path, text)
        definitions.extend(file_definitions)
        for symbol, location in file_calls:
            calls.setdefault(symbol, []).append(location)
    if calls and ksu is not None:
        for path in _c_files(ksu):
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(symbol in text for symbol in calls):
                _, file_definitions = inspect_c_source(path, text)
                definitions.extend(file_definitions)
    globals_available = {definition.symbol for definition in definitions if not definition.is_static}
    local_available = {(definition.symbol, definition.location.path) for definition in definitions}
    missing = {
        symbol: tuple(location for location in locations
                      if symbol not in globals_available and (symbol, location.path) not in local_available)
        for symbol, locations in calls.items()
    }
    return ValidationResult(source, ksu,
                            {symbol: tuple(locations) for symbol, locations in calls.items()},
                            tuple(definitions), {symbol: locations for symbol, locations in missing.items() if locations})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-root", required=True, type=Path)
    parser.add_argument("--ksu-dir", type=Path, help="Optional KSU source directory; relative paths are relative to kernel-root")
    arguments = parser.parse_args(argv)
    try:
        result = validate_hooks(arguments.kernel_root, arguments.ksu_dir)
    except (OSError, ValueError) as error:
        print(f"SUSFS hook validation could not read the source trees: {error}", file=sys.stderr)
        return 1
    if not result.ok:
        print("::error::Missing SUSFS / KernelSU hook implementations: " + ", ".join(sorted(result.missing)), file=sys.stderr)
        for symbol, locations in sorted(result.missing.items()):
            for location in locations:
                print(f"  {symbol}: {location.path}:{location.line}", file=sys.stderr)
        print(COMPATIBILITY_MESSAGE, file=sys.stderr)
        return 1
    if not result.calls:
        print("No SUSFS direct hook calls found; no KernelSU hook implementations are required.")
    else:
        print(f"SUSFS direct hook validation passed: {len(result.calls)} symbol(s); local kernel or global kernel/KernelSU definitions found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
