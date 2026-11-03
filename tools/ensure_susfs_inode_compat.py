#!/usr/bin/env python3
"""Backport the real inode user-namespace accessor required by SUSFS.

Early Android 13 / 5.15 trees have i_uid_into_mnt() but not i_user_ns().
Keep SUSFS's UID checks and the kernel's mapping implementation intact;
restore the upstream accessor only when an actual SUSFS call needs it.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

from ensure_susfs_headers import without_comments
from validate_susfs_hooks import strip_comments_and_literals


SYMBOL = 'i_user_ns'
SIGNATURE = (r'\b(?:static\s+)?(?:inline\s+)?struct\s+user_namespace\s*\*\s*'
             r'i_user_ns\s*\(\s*const\s+struct\s+inode\s*\*\s*\w*\s*\)\s*(?:\{|;)')
# GPL-2.0-only: Android common 257f970022c5274f7fb59baea8c48212d4e64dad,
# include/linux/fs.h, without changing the native namespace semantics.
IMPLEMENTATION = '''static inline struct user_namespace *i_user_ns(const struct inode *inode)
{
\treturn inode->i_sb->s_user_ns;
}
'''
ANCHOR = re.compile(r'(?m)^static\s+inline\s+uid_t\s+i_uid_read\s*\(\s*const\s+struct\s+inode\s*\*\s*\w+\s*\)\s*\{')


def checked_file(source: Path, relative: str) -> Path:
    path = source / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source):
        raise ValueError('Missing or linked inode compatibility source: ' + relative)
    return path


def struct_body(code: str, name: str) -> tuple[str, int]:
    """Read a single native struct, ignoring fields inside nested aggregates."""
    starts = list(re.finditer(r'\bstruct\s+' + re.escape(name) + r'\s*\{', code))
    if len(starts) != 1:
        raise ValueError('Cannot identify native struct ' + name)
    opening = starts[0].end() - 1
    depth = 1
    body = list(code[opening + 1:])
    for index, character in enumerate(body):
        if character == '{':
            depth += 1
        elif character == '}':
            depth -= 1
            if depth == 0:
                return ''.join(body[:index]), opening + index + 2
        if depth > 1 or character in '{}':
            body[index] = ' '
    raise ValueError('Unterminated native struct ' + name)


def ensure_inode_compat(kernel_root: Path) -> tuple[Path, ...]:
    root = kernel_root.resolve(strict=True)
    source = root / 'common' if (root / 'common/fs').is_dir() else root
    if not (source / 'fs/susfs.c').exists():
        return ()
    susfs = checked_file(source, 'fs/susfs.c')
    susfs_code = strip_comments_and_literals(susfs.read_text(encoding='utf-8'))
    if not re.search(r'\bi_user_ns\s*\(', susfs_code):
        return ()
    header = checked_file(source, 'include/linux/fs.h')
    text = header.read_text(encoding='utf-8')
    code = strip_comments_and_literals(text)
    if re.search(SIGNATURE, code):
        return ()
    # Do not shadow a changed native API or a vendor macro implementation.
    if re.search(r'\bi_user_ns\b', code) or re.search(
            r'(?m)^\s*#\s*define\s+i_user_ns\b', without_comments(text)):
        raise ValueError('Review the changed native i_user_ns interface')
    inode, inode_end = struct_body(code, 'inode')
    superblock, superblock_end = struct_body(code, 'super_block')
    if not re.search(r'\bstruct\s+super_block\s*\*\s*i_sb\s*;', inode):
        raise ValueError('Native inode does not provide its super_block pointer')
    if not re.search(r'\bstruct\s+user_namespace\s*\*\s*s_user_ns\s*;', superblock):
        raise ValueError('Native super_block does not provide its user namespace')
    anchors = list(ANCHOR.finditer(code))
    if len(anchors) != 1 or anchors[0].start() < max(inode_end, superblock_end):
        raise ValueError('Cannot locate the native inode UID accessor after complete structs')
    offset = anchors[0].start()
    updated = text[:offset] + IMPLEMENTATION + '\n' + text[offset:]
    header.write_text(updated, encoding='utf-8', newline='\n')
    return (header,)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path, required=True)
    args = parser.parse_args()
    try:
        changed = ensure_inode_compat(args.kernel_root)
    except (OSError, ValueError) as error:
        print('::error::SUSFS inode compatibility: ' + str(error), file=sys.stderr)
        return 1
    print('Restored native inode user-namespace accessor' if changed else 'Native inode namespace API is ready')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
