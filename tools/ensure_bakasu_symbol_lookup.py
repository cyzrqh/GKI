#!/usr/bin/env python3
"""Read-only contract check for BakaSU's complete native symbol resolver."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import restore_bakasu_kpm as kpm_port
from validate_susfs_hooks import strip_comments_and_literals


SYMBOL = "ksu_lookup_kernel_symbols"
PARAMETERS = r"const\s+char\s*\*\s*const\s*\*\s*names\s*,\s*unsigned\s+long\s*\*\s*addrs\s*,\s*u32\s+count"
SIGNATURES = {
    "find_kernel_symbol_exact": r"unsigned\s+long\s+(?:__nocfi\s+)?find_kernel_symbol_exact\s*\(\s*const\s+char\s*\*\s*symbol_name\s*\)",
    "ksu_resolve_symbol_for_functable_hook": r"void\s*\*\s*ksu_resolve_symbol_for_functable_hook\s*\(\s*const\s+char\s*\*\s*symbol_name\s*\)",
    "ksu_init_symbol_resolver": r"void\s+(?:__init\s+)?ksu_init_symbol_resolver\s*\(\s*(?:void\s*)?\)",
    SYMBOL: rf"int\s+(?:__nocfi\s+)?{SYMBOL}\s*\(\s*{PARAMETERS}\s*\)",
}


def _read(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
        raise ValueError(f"Missing or unsupported native BakaSU source: {path}")
    return path.read_text(encoding="utf-8")


def _conditional_depth(source: str, position: int) -> int:
    depth = 0
    for line in source[:position].splitlines():
        if re.match(r"\s*#\s*(?:if|ifdef|ifndef)\b", line):
            depth += 1
        elif re.match(r"\s*#\s*endif\b", line):
            depth -= 1
        if depth < 0:
            raise ValueError("Unbalanced native symbol resolver preprocessor guards")
    return depth


def native_function(source: str, symbol: str) -> str:
    """Extract the reviewed fixture's C body for regression tests only.

    Production validation below uses immutable Git bytes, not these historical
    signatures, so a legitimate future official implementation remains valid.
    """
    code = strip_comments_and_literals(source)
    pattern = re.compile(r"(?m)^[ \t]*" + SIGNATURES[symbol] + r"\s*\{")
    matches = list(pattern.finditer(code))
    if len(matches) != 1 or _conditional_depth(source, matches[0].start()) != 0:
        raise ValueError(f"Native BakaSU requires one unconditional global {symbol} implementation")
    match = matches[0]
    opening = code.index("{", match.start(), match.end())
    depth = 0
    for position in range(opening, len(code)):
        if code[position] == "{":
            depth += 1
        elif code[position] == "}":
            depth -= 1
            if depth == 0:
                if not code[opening + 1:position].strip():
                    raise ValueError(f"Native BakaSU {symbol} must not be an empty stub")
                return source[match.start():position + 1]
    raise ValueError(f"Unterminated native BakaSU {symbol} implementation")


def _resolver_objects(build: str) -> tuple[tuple[int, str], ...]:
    depth = 0
    matches = []
    for line in build.splitlines():
        if re.match(r"\s*(?:ifeq|ifneq|ifdef|ifndef)\b", line):
            depth += 1
        elif re.match(r"\s*endif\b", line):
            depth -= 1
        if depth < 0:
            raise ValueError("Unbalanced BakaSU Kbuild conditionals")
        if (not line.lstrip().startswith('#')
                and re.search(r"\binfra/symbol_resolver\.o\b", line)):
            matches.append((depth, ' '.join(line.split())))
    if depth:
        raise ValueError("Unbalanced BakaSU Kbuild conditionals")
    return tuple(matches)


def _git(directory: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(['git', '--no-replace-objects', '-C', str(directory), *arguments],
                                capture_output=True, timeout=30,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Cannot read the immutable official BakaSU Git objects") from error
    if result.returncode or len(result.stdout) > 262144:
        raise ValueError("Cannot read the immutable official BakaSU Git objects")
    return result.stdout


def _validate_dispatch(repository: Path, expected_ref: str, path: Path, original: bytes) -> None:
    """Accept the native caller or the exact, source-bound reviewed KPM caller.

    Later SUSFS and manager-signature integration may legitimately change
    Kbuild/Kconfig, so replay the fixed port against immutable Git objects;
    don't revalidate every already-integrated working-tree file here. The
    receipt still binds the complete port, while this guard checks its caller.
    """
    receipt_path = repository / kpm_port.RECEIPT
    actual = path.read_bytes()
    if not receipt_path.exists() and not kpm_port.is_link(receipt_path):
        if actual == original:
            return
        raise ValueError("Compiled BakaSU supercall/dispatch.c differs from the selected official commit "
                         "without an audited KPM port receipt; preserve the native source")
    source, tree = kpm_port.checked_source(repository, expected_ref)
    manifest, patch = kpm_port.load_port_manifest()
    before, after = kpm_port.expected_port(source, expected_ref, manifest, patch)
    receipt = kpm_port.strict_json(kpm_port.regular(kpm_port.safe_path(source, kpm_port.RECEIPT), 262144))
    expected_receipt = kpm_port.receipt_for(expected_ref, tree, manifest, before, after)
    if json.dumps(receipt, sort_keys=True) != json.dumps(expected_receipt, sort_keys=True):
        raise ValueError("BakaSU KPM receipt differs from the actual frozen source and port")
    relative = path.relative_to(repository).as_posix()
    if (relative != 'kernel/supercall/dispatch.c' or before.get(relative) != original
            or relative not in after or actual != after[relative]):
        raise ValueError("Compiled BakaSU supercall/dispatch.c differs from the exact audited KPM port")


def validate_native_symbol_lookup(kernel_root: Path, expected_ref: str, ksu_dir: Path | None = None) -> bool:
    """Check the native provider/caller/link contract without writing sources.

    The selected official commit is the algorithm's source of truth. Compare
    the actual compiled resolver with those immutable Git blobs rather than
    constraining future official function signatures or implementation details.
    The dispatch caller may also contain the exact reviewed KPM port.
    """
    if not isinstance(expected_ref, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', expected_ref):
        raise ValueError("Expected BakaSU ref must be a full immutable commit SHA")
    expected_ref = expected_ref.lower()
    root = kernel_root.resolve(strict=True)
    if ksu_dir is None:
        candidates = (root / item for item in ("common/drivers/kernelsu", "drivers/kernelsu", "KernelSU/kernel", "kernel"))
        ksu = next((item.resolve() for item in candidates if (item / "Kbuild").is_file()), None)
        if ksu is None:
            raise ValueError("Native BakaSU source directory was not found")
    else:
        ksu = (ksu_dir if ksu_dir.is_absolute() else root / ksu_dir).resolve(strict=True)
    repository = Path(_git(ksu, 'rev-parse', '--show-toplevel').decode('utf-8').strip()).resolve(strict=True)
    if _git(repository, 'rev-parse', 'HEAD').decode('ascii').strip().lower() != expected_ref:
        raise ValueError("BakaSU Git HEAD does not match the selected immutable official commit")
    if not ksu.is_relative_to(repository):
        raise ValueError("Compiled BakaSU sources are outside the selected official Git repository")
    originals = {}
    for relative in ('Kbuild', 'infra/symbol_resolver.c', 'infra/symbol_resolver.h', 'supercall/dispatch.c'):
        path = ksu / relative
        _read(path)
        blob = _git(repository, 'show', f'{expected_ref}:{path.relative_to(repository).as_posix()}')
        originals[relative] = blob.decode('utf-8')
        if relative == 'supercall/dispatch.c':
            _validate_dispatch(repository, expected_ref, path, blob)
        elif relative != 'Kbuild' and path.read_bytes() != blob:
            raise ValueError(f"Compiled BakaSU {relative} differs from the selected official commit; preserve the native source")
    build = _read(ksu / 'Kbuild')
    original_build = originals['Kbuild']
    if (not re.search(r"(?m)^REPO_NAME\s*:=\s*(?:BakaSU|ReSukiSU)\s*$", original_build)
            or not re.search(r"(?m)^REPO_NAME\s*:=\s*(?:BakaSU|ReSukiSU)\s*$", build)):
        raise ValueError("Symbol lookup validation requires the selected native BakaSU source")
    rules = _resolver_objects(original_build)
    if not rules or _resolver_objects(build) != rules:
        raise ValueError("BakaSU Kbuild must preserve the selected official symbol resolver object rules")
    return bool(re.search(rf'\b{SYMBOL}\s*\(', strip_comments_and_literals(originals['supercall/dispatch.c'])))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-root", required=True, type=Path)
    parser.add_argument("--expected-ref", required=True)
    parser.add_argument("--ksu-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        batch = validate_native_symbol_lookup(args.kernel_root, args.expected_ref, args.ksu_dir)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"::error::Native BakaSU symbol lookup validation failed: {error}", file=sys.stderr)
        return 1
    print("Native BakaSU symbol lookup contract passed; official resolver was preserved"
          + (" (including batch lookup)." if batch else " (legacy native API)."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
