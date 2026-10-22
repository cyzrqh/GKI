#!/usr/bin/env python3
"""Restore missing SUSFS header hunks after patching an Android kernel.

Some GKI revisions change the include context while the function-body hunks
still apply. Keep those complete implementations and restore only their real
header dependencies. Namespace declarations match the official SUSFS patch.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

from validate_susfs_hooks import strip_comments_and_literals


INODE_MACRO = re.compile(r"\b(SUSFS_IS_INODE_[A-Z0-9_]+)\s*\(")
INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]linux/susfs_def\.h[>"]', re.M)
COMMENT_OR_LITERAL = re.compile(r'/\*.*?\*/|//[^\r\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
NAMESPACE_DECLARATIONS = (
    ("susfs_is_current_ksu_domain", r"\bextern\s+bool\s+susfs_is_current_ksu_domain\s*\(\s*void\s*\)\s*;",
     "extern bool susfs_is_current_ksu_domain(void);"),
    ("susfs_is_sdcard_android_data_not_decrypted",
     r"\bextern\s+struct\s+static_key_true\s+susfs_is_sdcard_android_data_not_decrypted\s*;",
     "extern struct static_key_true susfs_is_sdcard_android_data_not_decrypted;"),
    ("CL_COPY_MNT_NS", r"(?m)^\s*#\s*define\s+CL_COPY_MNT_NS\b",
     "#define CL_COPY_MNT_NS BIT(25)"),
)


def without_comments(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group()
        return ''.join('\n' if char == '\n' else ' ' for char in token) if token.startswith(('/*', '//')) else token
    return COMMENT_OR_LITERAL.sub(replace, text)


def insert_after_includes(text: str, addition: str, path: Path) -> str:
    """Insert after unconditional headers, retaining their prerequisite types."""
    clean = without_comments(text)
    code = strip_comments_and_literals(text)
    first_body = code.find('{')
    if first_body < 0:
        first_body = len(text)
    depth = 0
    offset = 0
    anchor = None
    for line in clean.splitlines(keepends=True):
        if offset >= first_body:
            break
        directive = re.match(r'^\s*#\s*(\w+)\b', line)
        if directive:
            name = directive.group(1)
            if name in ('if', 'ifdef', 'ifndef'):
                depth += 1
            elif name == 'endif':
                depth -= 1
            elif name == 'include' and depth == 0:
                anchor = offset + len(line)
        offset += len(line)
    if anchor is None:
        raise ValueError(f"Cannot locate unconditional header includes in {path}")
    return text[:anchor] + '\n' + addition + text[anchor:]


def repaired_source(path: Path, text: str, available_macros: set[str]) -> str:
    clean = without_comments(text)
    code = strip_comments_and_literals(text)
    used = set(INODE_MACRO.findall(code))
    missing = used - available_macros
    if missing:
        raise ValueError(f"susfs_def.h does not provide macros used by {path}: {', '.join(sorted(missing))}")
    declarations = []
    namespace_usage = False
    if path.name == 'namespace.c':
        for symbol, pattern, declaration in NAMESPACE_DECLARATIONS:
            if re.search(r'\b' + re.escape(symbol) + r'\b', code):
                namespace_usage = True
                if not re.search(pattern, clean):
                    declarations.append(declaration)
    if not used and not namespace_usage:
        return text
    blocks = []
    if not INCLUDE.search(clean):
        blocks.append('#ifdef CONFIG_KSU_SUSFS\n#include <linux/susfs_def.h>\n#endif\n')
    if declarations:
        blocks.append('#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n' + '\n'.join(declarations) + '\n#endif\n')
    return insert_after_includes(text, '\n'.join(blocks), path) if blocks else text


def ensure_headers(kernel_root: Path) -> tuple[Path, ...]:
    root = kernel_root.resolve(strict=True)
    source = root / 'common' if (root / 'common/fs').is_dir() else root
    if not (source / 'fs').is_dir():
        raise ValueError(f"Kernel fs source directory not found below {root}")
    header = source / 'include/linux/susfs_def.h'
    if not header.is_file():
        raise ValueError(f"Official SUSFS definitions are missing: {header}")
    macros = set(re.findall(r'(?m)^\s*#\s*define\s+(SUSFS_IS_INODE_[A-Z0-9_]+)\b',
                            without_comments(header.read_text(encoding='utf-8'))))
    changes = []
    for directory in (source / 'fs', source / 'mm'):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob('*.c')):
            if not path.resolve().is_relative_to(source.resolve()):
                raise ValueError(f"Kernel source path leaves the selected source tree: {path}")
            data = path.read_bytes()
            if b'SUSFS_IS_INODE_' not in data and path.name != 'namespace.c':
                continue
            original = data.decode('utf-8')
            updated = repaired_source(path, original, macros)
            if updated != original:
                changes.append((path, updated))
    # Preflight every source before writing so missing upstream definitions fail
    # without leaving half of the source set repaired.
    for path, text in changes:
        path.write_text(text, encoding='utf-8', newline='\n')
    return tuple(path for path, _ in changes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        changed = ensure_headers(args.kernel_root)
    except (OSError, ValueError) as error:
        print(f'::error::SUSFS header integration failed: {error}', file=sys.stderr)
        return 1
    if changed:
        print('Restored SUSFS header dependencies: ' + ', '.join(str(path) for path in changed))
    else:
        print('SUSFS header dependencies already present.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
