#!/usr/bin/env python3
"""Backport the real Android filemap-fold helper needed by SUSFS OPEN_REDIRECT.

The July 2024 Android 15 / 6.6 tree has page-size fixup VMAs but predates the
fold helper used by the current SUSFS patch. Restore the official implementation
and declaration together; never replace it with a no-op or remove the SUSFS call.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

from validate_susfs_hooks import strip_comments_and_literals
from ensure_susfs_headers import without_comments


SYMBOL = '__fold_filemap_fixup_entry'
SIGNATURE = (r'\bvoid\s+' + SYMBOL + r'\s*\(\s*struct\s+vma_iterator\s*\*\s*\w+\s*,'
             r'\s*unsigned\s+long\s*\*\s*\w+\s*\)')
DECLARATION = 'extern void __fold_filemap_fixup_entry(struct vma_iterator *iter, unsigned long *end);'
# GPL-2.0-only, copied from Android common 394605f7e8266e836a8acf09a8deabd4ab6d7e70:
# mm/page_size_compat.c (android15-6.6 / 2024-08), without changing its behavior.
IMPLEMENTATION = '''void __fold_filemap_fixup_entry(struct vma_iterator *iter, unsigned long *end)
{
\tstruct vm_area_struct *next_vma;

\t/* Not emulating page size? */
\tif (!static_branch_unlikely(&page_shift_compat_enabled))
\t\treturn;

\t/* Advance iterator */
\tnext_vma = vma_next(iter);

\t/* If fixup VMA, adjust the end to cover its extent */
\tif (next_vma && (next_vma->vm_flags & __VM_NO_COMPAT)) {
\t\t*end = next_vma->vm_end;
\t\treturn;
\t}

\t/* Rewind iterator */
\tvma_prev(iter);
}
'''


def checked_file(source: Path, relative: str) -> Path:
    path = source / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source):
        raise ValueError('Missing or linked Android page-size compatibility source: ' + relative)
    return path


def ensure_vma_pad_compat(kernel_root: Path) -> tuple[Path, ...]:
    """Preserve native padding where available; old VMAs end at vm_end."""
    root = kernel_root.resolve(strict=True)
    source = root / 'common' if (root / 'common/fs').is_dir() else root
    task = checked_file(source, 'fs/proc/task_mmu.c')
    text = task.read_text(encoding='utf-8')
    code = strip_comments_and_literals(text)
    if not re.search(r'\bVMA_PAD_START\s*\(', code):
        return ()
    native = []
    for relative in ('include/linux/mm.h', 'include/linux/mm_types.h',
                     'include/linux/pgsize_migration.h', 'include/linux/pgsize_migration_inline.h'):
        if (source / relative).exists():
            native.append((relative, checked_file(source, relative).read_text(encoding='utf-8')))
    header_code = {name: strip_comments_and_literals(raw) for name, raw in native}
    padding_api = re.search(r'\bunsigned\s+long\s+vma_pad_pages\s*\(',
                            header_code.get('include/linux/pgsize_migration.h', '')) is not None
    native_macro = any(re.search(r'(?m)^\s*#\s*define\s+VMA_PAD_START\s*\(', without_comments(raw))
                       for _, raw in native)
    # Do not reinterpret a new/unknown native padding representation as an old
    # unpadded VMA. Both known Android APIs are handled without changing MM.
    if not padding_api and not native_macro and any(re.search(r'\b(?:vm_pad_pages|vma_pad_pages|VM_PAD_MASK)\b', without_comments(raw))
                                                   for _, raw in native):
        raise ValueError('Review the changed native Android VMA padding interface')
    expression = ('((vma)->vm_end - (vma_pad_pages(vma) << PAGE_SHIFT))'
                  if padding_api else '((vma)->vm_end)')
    local = re.search(r'(?m)^#define VMA_PAD_START\(vma\) ([^\n]+)$', text)
    if local:
        if local[1] != '((vma)->vm_end)' or not padding_api:
            return ()
        text = text[:local.start(1)] + expression + text[local.end(1):]
    elif not native_macro:
        includes = list(re.finditer(r'(?m)^#include\s+"internal\.h"[ \t]*$', text))
        if not includes:
            includes = list(re.finditer(r'(?m)^#include\s+[<"].*[>"][ \t]*$', text))
        if not includes:
            raise ValueError('Cannot locate native proc maps includes')
        offset = includes[-1].end()
        addition = '\n\n#ifndef VMA_PAD_START\n#define VMA_PAD_START(vma) ' + expression + '\n#endif'
        text = text[:offset] + addition + text[offset:]
    if padding_api and '#include <linux/pgsize_migration.h>' not in without_comments(text):
        anchor = re.search(r'(?m)^#include\s+[<"].*[>"][ \t]*$', text)
        if anchor is None:
            raise ValueError('Cannot include the native VMA padding declaration')
        text = text[:anchor.end()] + '\n#include <linux/pgsize_migration.h>' + text[anchor.end():]
    original = task.read_text(encoding='utf-8')
    if text != original:
        task.write_text(text, encoding='utf-8', newline='\n')
        return (task,)
    return ()


def ensure_map_compat(kernel_root: Path) -> tuple[Path, ...]:
    root = kernel_root.resolve(strict=True)
    source = root / 'common' if (root / 'common/fs').is_dir() else root
    task = checked_file(source, 'fs/proc/task_mmu.c')
    task_code = strip_comments_and_literals(task.read_text(encoding='utf-8'))
    if not re.search(r'\b' + SYMBOL + r'\s*\(', task_code):
        return ()
    header = checked_file(source, 'include/linux/page_size_compat.h')
    implementation = checked_file(source, 'mm/page_size_compat.c')
    header_text = header.read_text(encoding='utf-8')
    implementation_text = implementation.read_text(encoding='utf-8')
    header_code = strip_comments_and_literals(header_text)
    implementation_code = strip_comments_and_literals(implementation_text)
    declared = re.search(SIGNATURE + r'\s*;', header_code) is not None
    inline = re.search(SIGNATURE + r'\s*\{', header_code) is not None
    defined = re.search(SIGNATURE + r'\s*\{', implementation_code) is not None
    if declared and defined or inline:
        return ()
    # Do not append a second definition or silently adapt an unfamiliar API.
    if (SYMBOL in header_code and not (declared or inline)
            or SYMBOL in implementation_code and not defined):
        raise ValueError('Android filemap-fold helper has an unsupported signature')
    if '#include <linux/page_size_compat.h>' not in without_comments(implementation_text):
        raise ValueError('Android page-size implementation does not include its public header')
    if not defined:
        required = (r'\bDEFINE_STATIC_KEY_FALSE\s*\(\s*page_shift_compat_enabled\s*\)',
                    r'\bvoid\s+___filemap_fixup\s*\(',
                    r'\bMAP_FIXED\s*\|\s*__MAP_NO_COMPAT\b')
        if any(not re.search(pattern, implementation_code) for pattern in required):
            raise ValueError('Native Android filemap fixup dependencies are missing')
        if not re.search(r'(?m)^\s*#\s*define\s+__VM_NO_COMPAT\b', without_comments(header_text)):
            raise ValueError('Native Android page-size VMA flag is missing')
        if not re.search(r'\bvma_next\s*\(', task_code):
            raise ValueError('Native Android proc maps iterator is missing')
    changes = []
    if not declared:
        closing = re.search(r'(?m)^#endif\s*/\*\s*__LINUX_PAGE_SIZE_COMPAT_H\s*\*/\s*\Z', header_text)
        if closing is None:
            raise ValueError('Android page-size header has an unsupported include guard')
        addition = ('#ifndef __ASSEMBLY__\nstruct vma_iterator;\n' + DECLARATION
                    + '\n#endif\n\n')
        changes.append((header, header_text[:closing.start()] + addition + header_text[closing.start():]))
    if not defined:
        changes.append((implementation, implementation_text.rstrip() + '\n\n'
                        '/* Android filemap-fold backport for SUSFS OPEN_REDIRECT. */\n' + IMPLEMENTATION))
    # Verify all prerequisites before writing either half of the backport.
    for path, text in changes:
        path.write_text(text, encoding='utf-8', newline='\n')
    return tuple(path for path, _ in changes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        changed = ensure_vma_pad_compat(args.kernel_root) + ensure_map_compat(args.kernel_root)
    except (OSError, ValueError) as error:
        print('::error::SUSFS page-size mapping compatibility failed: ' + str(error), file=sys.stderr)
        return 1
    print('Restored Android proc maps compatibility: ' + ', '.join(map(str, changed)) if changed
          else 'Android proc maps compatibility already satisfied.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
