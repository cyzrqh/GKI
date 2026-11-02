"""Complete the optional priority UID API in the selected SUSFS 2.3 sources."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

from validate_susfs_hooks import resolve_ksu_dir, strip_comments_and_literals

OPTION = 'KSU_SUSFS_PRIORITY_UID_FILTER'
# Native SUSFS b0e474642cf77ee797004a454fa441362723d0ba Kconfig entry.
CONFIG = '''
config KSU_SUSFS_PRIORITY_UID_FILTER
\tbool "Enable to allow adding user-defined uids that will be hidden from getpriority/setpriority/ioprio_get/ioprio_set syscall"
\tdepends on KSU_SUSFS
\tdefault y
\thelp
\t  - Allow hiding user-defined uids from syscalls like getpriority/setpriority/ioprio_get/ioprio_set.
\t  - Effective on all processes with uid >= 10000.
'''
CASE = re.compile(r'(?m)^([ \t]*)case CMD_SUSFS_SET_PRIORITY_UID_FILTER:\s*\n'
                  r'[ \t]*susfs_set_priority_uid_filter\(arg\);\s*\n[ \t]*return 0;[^\S\n]*\n')


def complete_priority(root: Path) -> tuple[Path, ...]:
    root = root.resolve(strict=True)
    common = root / 'common' if (root / 'common/fs').is_dir() else root
    header = common / 'include/linux/susfs.h'
    if not header.is_file() or not re.search(r'(?m)^#ifdef CONFIG_' + OPTION + r'\s*$',
                                             header.read_text(encoding='utf-8')):
        return ()  # Older pinned SUSFS exposes this API unconditionally.
    ksu = resolve_ksu_dir(root)
    if ksu is None:
        raise ValueError('Missing KernelSU source for the conditional SUSFS priority API')
    updates = {}
    kconfig = ksu / 'Kconfig'
    text = kconfig.read_text(encoding='utf-8')
    if not re.search(r'(?m)^config ' + OPTION + r'\s*$', text):
        if not re.search(r'(?m)^config KSU_SUSFS\s*$', text):
            raise ValueError('The priority UID feature requires the real SUSFS configuration')
        updates[kconfig] = text.rstrip() + '\n' + CONFIG
    implementation = common / 'fs/susfs.c'
    text = implementation.read_text(encoding='utf-8')
    if not re.search(r'\bvoid\s+susfs_set_priority_uid_filter\s*\(', strip_comments_and_literals(text)):
        raise ValueError('The selected SUSFS header lacks its real priority UID implementation')
    # Native helpers are also present when the optional filter is disabled.
    # Mark only these two helpers optional; keep -Werror for every other function.
    updated = re.sub(r'\bstatic (struct cred \*|void )(?=susfs_(?:switch_to_ksu_domain|restore_creds)\s*\()',
                     r'static __maybe_unused \1', text)
    if updated != text:
        updates[implementation] = updated
    for path in ksu.rglob('*.c'):
        text = path.read_text(encoding='utf-8', errors='surrogateescape')
        if not re.search(r'\bsusfs_set_priority_uid_filter\s*\(', strip_comments_and_literals(text)):
            continue
        matches = list(CASE.finditer(text))
        if len(matches) != 1:
            raise ValueError('Review the changed SUSFS priority dispatcher: ' + str(path))
        match = matches[0]
        before = text[:match.start()].rstrip().splitlines()
        after = text[match.end():].lstrip().splitlines()
        if (before and before[-1].strip() == '#ifdef CONFIG_' + OPTION
                and after and after[0].startswith('#endif')):
            continue
        updates[path] = (text[:match.start()] + '#ifdef CONFIG_' + OPTION + '\n' + match[0]
                         + '#endif // CONFIG_' + OPTION + '\n' + text[match.end():])
    # Discover unsupported dispatcher shapes before modifying any source file.
    for path, text in updates.items():
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('SUSFS compatibility source leaves the selected kernel tree')
    for path, text in updates.items():
        path.write_text(text, encoding='utf-8', errors='surrogateescape', newline='\n')
    return tuple(updates)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', required=True, type=Path)
    args = parser.parse_args()
    changes = complete_priority(args.kernel_root)
    print('Completed conditional SUSFS priority API: ' + str(len(changes)) + ' source files')
