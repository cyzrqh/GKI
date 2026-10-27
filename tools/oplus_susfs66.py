"""Port the reviewed SUSFS 6.6 map hooks to their real vendor functions.

Official SM8750 6.6.30 has an older rollup tail without braces or a second
last_vma_end assignment. Keep its mmap-lock/iterator lifecycle, and guard
the real statistics calls instead of allowing fuzz to move SUSFS code.
"""
from __future__ import annotations

import difflib
from pathlib import Path
import re

from oplus_compat import function_text, read_source
from oplus_susfs_headers import adapt_namespace_header


SMAP_ADDITIONS = (
    '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    '\tif (vma->vm_file) {\n'
    '\t\tif (SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
    '\t\t\treturn 0;\n'
    '\t}\n'
    '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n\n')
ROLLUP_ADDITIONS = (
    '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    '\t\tif (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
    '\t\t\tgoto bypass_orig_flow;\n'
    '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    'bypass_orig_flow:\n'
    '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n',
    '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    '\t\t\t\tif (!vma->vm_file || !(SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))) {\n'
    '\t\t\t\t\tsmap_gather_stats(vma, &mss, last_vma_end);\n'
    '\t\t\t\t\tlast_vma_end = vma->vm_end;\n'
    '\t\t\t\t}\n'
    '#else\n'
    '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n')


def additions(hunk: str) -> str:
    return ''.join(line[1:] for line in hunk.splitlines(keepends=True)[1:]
                   if line.startswith('+'))


def adapt_task_mmu(common: Path, patch_bytes: bytes) -> bytes:
    text = patch_bytes.decode('utf-8')
    sections = re.split(r'(?=^diff --git )', text, flags=re.M)
    selected = [index for index, section in enumerate(sections)
                if section.startswith('diff --git a/fs/proc/task_mmu.c b/fs/proc/task_mmu.c\n')]
    if len(selected) != 1:
        raise ValueError('Official SUSFS 6.6 requires one task_mmu section')
    index = selected[0]
    hunks = re.split(r'(?=^@@ )', sections[index], flags=re.M)[1:]
    source = read_source(common / 'fs/proc/task_mmu.c')
    after = source
    show_smap = function_text(source, 'show_smap')
    if (len(re.findall(r'(?m)^\tstruct vm_area_struct \*vma = (?:v|get_data_vma\(v\));$', show_smap)) != 1
            or show_smap.count('\tstruct mem_size_stats mss;\n\n\tmemset(&mss, 0, sizeof(mss));') != 1):
        raise ValueError('Unknown real vendor show_smap declaration and initialization')
    rollup = function_text(source, 'show_smaps_rollup')
    pattern = r'(?m)^([ \t]*)smap_gather_stats\(vma, &mss, (0|last_vma_end)\);$'
    calls = re.findall(pattern, rollup)
    if [argument for _indent, argument in calls] not in (['0', 'last_vma_end'], ['0', '0', 'last_vma_end']):
        raise ValueError('Unknown real vendor show_smaps_rollup gather lifecycle')
    def guard(match):
        indent, argument = match[1], match[2]
        return ('#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n' + indent
                + 'if (!vma->vm_file || !SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
                + '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n' + indent + '\t'
                + 'smap_gather_stats(vma, &mss, ' + argument + ');')
    changed_rollup = re.sub(pattern, guard, rollup)
    smap_count, rollup_count = 0, 0
    for hunk in hunks:
        heading = hunk.splitlines()[0]
        if re.match(r'@@ [^\n]+ @@ static int show_smap\(', heading):
            if (additions(hunk) != SMAP_ADDITIONS or smap_count
                    or any(line.startswith('-') for line in hunk.splitlines()[1:])):
                raise ValueError('Changed official SUSFS 6.6 show_smap additions require review')
            before = '\tstruct mem_size_stats mss;\n\n\tmemset(&mss, 0, sizeof(mss));'
            changed = show_smap.replace(before, '\tstruct mem_size_stats mss;\n\n'
                                       + SMAP_ADDITIONS + '\tmemset(&mss, 0, sizeof(mss));', 1)
            if after.count(show_smap) != 1:
                raise ValueError('Ambiguous native show_smap function')
            after = after.replace(show_smap, changed, 1)
            smap_count += 1
        elif re.match(r'@@ [^\n]+ @@ static int show_smaps_rollup\(', heading):
            if (rollup_count >= 2 or additions(hunk) != ROLLUP_ADDITIONS[rollup_count]
                    or any(line.startswith('-') for line in hunk.splitlines()[1:])):
                raise ValueError('Changed official SUSFS 6.6 rollup additions require review')
            rollup_count += 1
        else:
            old = ''.join(line[1:] for line in hunk.splitlines(keepends=True)[1:]
                          if line.startswith((' ', '-')))
            new = ''.join(line[1:] for line in hunk.splitlines(keepends=True)[1:]
                          if line.startswith((' ', '+')))
            if not old or after.count(old) != 1:
                raise ValueError('Official 6.6 task_mmu context is not unique; refusing fuzzy hook relocation')
            after = after.replace(old, new, 1)
    if smap_count != 1 or rollup_count != 2 or after.count(rollup) != 1:
        raise ValueError('Official SUSFS 6.6 map function hook layout changed')
    after = after.replace(rollup, changed_rollup, 1)
    sections[index] = 'diff --git a/fs/proc/task_mmu.c b/fs/proc/task_mmu.c\n' + ''.join(difflib.unified_diff(
        source.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile='a/fs/proc/task_mmu.c', tofile='b/fs/proc/task_mmu.c'))
    return ''.join(sections).encode('utf-8')


def adapt_susfs66(common: Path, patch_bytes: bytes) -> bytes:
    return adapt_task_mmu(common, adapt_namespace_header(common, patch_bytes, 'trace/hooks/fs.h'))
