"""Retain SUSFS namespace hooks when OKI lacks a reviewed trace include.

The official 6.1 and 6.6 patch header hunks add no use of their existing blk.h
or fs.h context includes. Adapt only a selected, proven missing-context line
with unique native surroundings, retaining every SUSFS added line.
"""
from __future__ import annotations

from pathlib import Path
import re


def adapt_namespace_header(common: Path, patch_bytes: bytes,
                           missing_header: str = 'trace/hooks/blk.h') -> bytes:
    if missing_header not in ('trace/hooks/blk.h', 'trace/hooks/fs.h'):
        raise ValueError('Unreviewed optional namespace trace header')
    text = patch_bytes.decode('utf-8')
    section = re.search(r'(?ms)^diff --git a/fs/namespace\.c b/fs/namespace\.c\n.*?(?=^diff --git |\Z)', text)
    if section is None:
        raise ValueError('The official SUSFS patch lacks its namespace implementation')
    hunks = list(re.finditer(r'(?m)^@@ -(\d+),(\d+) \+(\d+),(\d+) @@[^\n]*\n', section[0]))
    if not hunks:
        raise ValueError('The official SUSFS namespace patch lacks its header hunk')
    first = hunks[0]
    end = hunks[1].start() if len(hunks) > 1 else len(section[0])
    hunk = section[0][first.end():end]
    trace_context = ' #include <' + missing_header + '>\n'
    if trace_context not in hunk:
        return patch_bytes
    if (hunk.count(trace_context) != 1 or any(line.startswith('-') for line in hunk.splitlines())
            or '+#define CL_COPY_MNT_NS BIT(25)' not in hunk
            or '+extern bool susfs_is_current_ksu_domain(void);\n' not in hunk
            or '+extern struct static_key_true susfs_is_sdcard_android_data_not_decrypted;\n' not in hunk):
        raise ValueError('Review the changed SUSFS namespace header hunk before adapting OKI')
    source = common / 'fs/namespace.c'
    if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= 1024 * 1024:
        raise ValueError('The official OKI namespace source is missing or linked')
    original = source.read_text(encoding='utf-8')
    context = ''.join(line[1:] for line in hunk.splitlines(keepends=True) if line.startswith(' '))
    if original.count(context) == 1:
        return patch_bytes
    adapted_context = context.replace(trace_context[1:], '')
    if original.count(adapted_context) != 1:
        raise ValueError('Unknown OKI namespace header context; no SUSFS hook was changed')
    header = (f'@@ -{first[1]},{int(first[2]) - 1} +{first[3]},{int(first[4]) - 1} @@\n')
    replacement = header + hunk.replace(trace_context, '')
    changed = section[0][:first.start()] + replacement + section[0][end:]
    return (text[:section.start()] + changed + text[section.end():]).encode('utf-8')
