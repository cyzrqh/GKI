"""Complete the optional priority UID API in the selected SUSFS 2.3 sources."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

from ensure_susfs_headers import without_comments
from validate_susfs_hooks import inspect_c_source, resolve_ksu_dir, strip_comments_and_literals

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
PARSER = 'susfs_parse_priority_app_strings'
SETTER = 'susfs_set_priority_uid_filter'
PARSER_CALL = re.compile(r'(?m)^[ \t]*susfs_parse_priority_app_strings\s*\(\s*\)\s*;[^\S\n]*(?:\n|$)')


def priority_api(header: str, implementation: str) -> str:
    """Distinguish no API, complete legacy API, and complete conditional API."""
    signatures = {
        SETTER: r'void\s+' + SETTER + r'\s*\(\s*void\s*(?:__user\s*)?\*\s*\*\s*\w+\s*\)',
        PARSER: r'void\s+' + PARSER + r'\s*\(\s*(?:void\s*)?\)',
    }
    declarations = strip_comments_and_literals(header)
    definitions = strip_comments_and_literals(implementation)
    conditional = re.search(r'(?m)^\s*#\s*ifdef\s+CONFIG_' + OPTION + r'\s*$',
                            without_comments(header)) is not None
    used = any(re.search(r'\b' + symbol + r'\b', text)
               for symbol in signatures for text in (declarations, definitions))
    if not used and not conditional:
        return 'absent'
    for symbol, signature in signatures.items():
        if (len(re.findall(r'\b' + signature + r'\s*;', declarations)) != 1
                or len(re.findall(r'(?m)^[ \t]*' + signature + r'\s*\{', definitions)) != 1):
            raise ValueError('The selected SUSFS priority API is incomplete: ' + symbol)
    return 'conditional' if conditional else 'unconditional'


def guarded(text: str, match: re.Match) -> bool:
    before = text[:match.start()].rstrip().splitlines()
    after = text[match.end():].lstrip().splitlines()
    return bool(before and before[-1].strip() == '#ifdef CONFIG_' + OPTION
                and after and after[0].startswith('#endif'))


def guard_consumer(path: Path, text: str) -> str:
    """Keep refresh and dispatch calls under the same real optional feature."""
    calls, _ = inspect_c_source(path, text, symbols=(SETTER, PARSER))
    code = strip_comments_and_literals(text)
    setter_calls = [call for call in calls if call[0] == SETTER]
    parser_calls = [call for call in calls if call[0] == PARSER]
    matches = []
    if setter_calls:
        cases = list(CASE.finditer(code))
        if len(cases) != len(setter_calls) or len(cases) != 1:
            raise ValueError('Review the changed SUSFS priority dispatcher: ' + str(path))
        matches.extend(cases)
    if parser_calls:
        refreshes = list(PARSER_CALL.finditer(code))
        if len(refreshes) != len(parser_calls):
            raise ValueError('Review the changed SUSFS priority refresh: ' + str(path))
        matches.extend(refreshes)
    for match in sorted(matches, key=lambda item: item.start(), reverse=True):
        if not guarded(text, match):
            text = (text[:match.start()] + '#ifdef CONFIG_' + OPTION + '\n' + text[match.start():match.end()]
                    + '#endif // CONFIG_' + OPTION + '\n' + text[match.end():])
    return text


def complete_priority(root: Path) -> tuple[Path, ...]:
    root = root.resolve(strict=True)
    common = root / 'common' if (root / 'common/fs').is_dir() else root
    header = common / 'include/linux/susfs.h'
    if not header.is_file():
        return ()
    implementation = common / 'fs/susfs.c'
    original = implementation.read_text(encoding='utf-8')
    state = priority_api(header.read_text(encoding='utf-8'), original)
    if state == 'unconditional':
        return ()  # Preserve complete old APIs that really exist unconditionally.
    ksu = resolve_ksu_dir(root)
    if ksu is None:
        raise ValueError('Missing KernelSU source for the conditional SUSFS priority API')
    updates = {}
    kconfig = ksu / 'Kconfig'
    text = kconfig.read_text(encoding='utf-8')
    has_option = re.search(r'(?m)^config ' + OPTION + r'\s*$', text) is not None
    if state == 'absent' and has_option:
        raise ValueError('The selected SUSFS source lacks its advertised priority UID implementation')
    if state == 'conditional' and not has_option:
        if not re.search(r'(?m)^config KSU_SUSFS\s*$', text):
            raise ValueError('The priority UID feature requires the real SUSFS configuration')
        updates[kconfig] = text.rstrip() + '\n' + CONFIG
    # Native helpers are also present when the optional filter is disabled.
    # Mark only these two helpers optional; keep -Werror for every other function.
    updated = (re.sub(r'\bstatic (struct cred \*|void )(?=susfs_(?:switch_to_ksu_domain|restore_creds)\s*\()',
                      r'static __maybe_unused \1', original) if state == 'conditional' else original)
    if updated != original:
        updates[implementation] = updated
    for path in sorted(ksu.rglob('*.c')):
        text = path.read_text(encoding='utf-8', errors='surrogateescape')
        updated = guard_consumer(path, text)
        if updated != text:
            updates[path] = updated
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
