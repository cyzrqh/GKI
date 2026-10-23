#!/usr/bin/env python3
"""Apply the complete SUSFS 2.3 fdinfo hooks to historical GKI source.

Older fdinfo implementations print a local event mask instead of calling
inotify_mark_user_mask(). Temporarily adapt that context for the official 2.3
patch, then restore the original event-mask semantics in every added branch.
The source backup lives outside common/ and is never used as executable code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from validate_susfs_hooks import strip_comments_and_literals


STATE_NAME = '.gki-susfs23-fdinfo.json'
LEGACY_DECLARATION = re.compile(
    r'(?m)^[ \t]*u32\s+mask\s*=\s*mark->mask\s*&\s*IN_ALL_EVENTS\s*;[ \t]*\n')
LEGACY_ARGUMENTS = 'mask, mark->ignored_mask'
MODERN_ARGUMENTS = 'inotify_mark_user_mask(mark)'
LEGACY_FORMAT = 'ignored_mask:%x'
MODERN_FORMAT = 'ignored_mask:0'


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def function_span(text: str, name: str) -> tuple[int, int]:
    code = strip_comments_and_literals(text)
    match = re.search(r'\bstatic\s+void\s+' + re.escape(name) + r'\s*\(', code)
    if match is None:
        raise ValueError(f'Cannot locate {name} in fdinfo.c')
    opening = code.find('{', match.end())
    if opening < 0:
        raise ValueError(f'Cannot locate the body of {name}')
    depth = 0
    for position in range(opening, len(code)):
        if code[position] == '{':
            depth += 1
        elif code[position] == '}':
            depth -= 1
            if depth == 0:
                return match.start(), position + 1
    raise ValueError(f'Unterminated {name} in fdinfo.c')


def inotify_body(text: str) -> str:
    start, end = function_span(text, 'inotify_fdinfo')
    return text[start:end]


def validate_hooks(text: str) -> None:
    """Check real 2.3 hook bodies, not merely Kconfig or extern declarations."""
    body = inotify_body(text)
    code = strip_comments_and_literals(body)
    required = (
        'susfs_is_current_app_uid()',
        'susfs_is_inode_sus_kstat(inode, &is_fuse)',
        'susfs_sus_kstat_spoof_inotify_fdinfo(&ino, &dev)',
        'susfs_is_current_proc_umounted()',
        'real_mount(file->f_path.mnt)',
        'DEFAULT_KSU_MNT_ID',
        'kern_path(dpath, 0, &path)',
        'd_backing_inode(path.dentry)',
    )
    compact = re.sub(r'\s+', '', code)
    missing = [symbol for symbol in required if re.sub(r'\s+', '', symbol) not in compact]
    for guard in ('CONFIG_KSU_SUSFS_SUS_KSTAT', 'CONFIG_KSU_SUSFS_SUS_MOUNT'):
        if not re.search(r'(?m)^\s*#\s*ifdef\s+' + guard + r'\b', body):
            missing.append(guard)
    start, end = function_span(text, 'show_fdinfo')
    if not re.search(r'\bshow\s*\(\s*m\s*,\s*mark\s*,\s*f\s*\)',
                     strip_comments_and_literals(text[start:end])):
        missing.append('show(m, mark, f)')
    if missing:
        raise ValueError('Incomplete SUSFS 2.3 fdinfo hooks: ' + ', '.join(missing))


def legacy_context(body: str) -> tuple[str, int, int]:
    declarations = list(LEGACY_DECLARATION.finditer(body))
    if len(declarations) != 1:
        raise ValueError('Unknown historical fdinfo mask declaration; refusing to change it')
    declaration = declarations[0]
    comment_start = body.rfind('/*', 0, declaration.start())
    comment_end = body.find('*/', comment_start) if comment_start >= 0 else -1
    if (comment_end < 0 or 'IN_ALL_EVENTS' not in body[comment_start:comment_end]
            or 'FS_EVENT_ON_CHILD' not in body[comment_start:comment_end]
            or body[comment_end + 2:declaration.start()].strip()):
        raise ValueError('Unknown historical fdinfo event-mask comment context')
    start = body.rfind('\n', 0, comment_start) + 1
    return body[start:declaration.end()], start, declaration.end()


def prepared_source(original: str) -> tuple[str, str | None]:
    start, end = function_span(original, 'inotify_fdinfo')
    body = original[start:end]
    # Modern source already matches the official patch and needs no rewrite.
    if (body.count(MODERN_ARGUMENTS) == 1 and body.count(MODERN_FORMAT) == 1
            and LEGACY_ARGUMENTS not in body and not LEGACY_DECLARATION.search(body)):
        return original, None
    if (body.count(LEGACY_ARGUMENTS) != 1 or body.count(LEGACY_FORMAT) != 1
            or MODERN_ARGUMENTS in body):
        raise ValueError('Unknown fdinfo source layout; refusing a partial SUSFS integration')
    block, block_start, block_end = legacy_context(body)
    body = body[:block_start] + body[block_end:]
    body = body.replace(LEGACY_ARGUMENTS, MODERN_ARGUMENTS).replace(LEGACY_FORMAT, MODERN_FORMAT)
    return original[:start] + body + original[end:], block


def restored_source(patched: str, native_block: str) -> str:
    validate_hooks(patched)
    start, end = function_span(patched, 'inotify_fdinfo')
    body = patched[start:end]
    # Original flow plus the KSTAT and MOUNT branches each print one mask.
    if body.count(MODERN_ARGUMENTS) != 3 or body.count(MODERN_FORMAT) != 3:
        raise ValueError('Unknown SUSFS 2.3 fdinfo output branches; refusing to change event semantics')
    if LEGACY_DECLARATION.search(body) or LEGACY_ARGUMENTS in body:
        raise ValueError('Unexpected mask declaration after applying the SUSFS 2.3 patch')
    anchors = list(re.finditer(r'(?m)^[ \t]*if\s*\(\s*inode\s*\)\s*\{[ \t]*\n', body))
    if len(anchors) != 1:
        raise ValueError('Cannot restore the native mask inside the fdinfo inode scope')
    position = anchors[0].end()
    body = body[:position] + native_block + body[position:]
    body = body.replace(MODERN_ARGUMENTS, LEGACY_ARGUMENTS).replace(MODERN_FORMAT, LEGACY_FORMAT)
    restored = patched[:start] + body + patched[end:]
    validate_restored(restored, native_block)
    return restored


def validate_restored(text: str, native_block: str | None) -> None:
    validate_hooks(text)
    body = inotify_body(text)
    if native_block is None:
        if (body.count(MODERN_ARGUMENTS) != 3 or body.count(MODERN_FORMAT) != 3
                or LEGACY_ARGUMENTS in body or LEGACY_DECLARATION.search(body)):
            raise ValueError('Modern fdinfo event-mask semantics changed unexpectedly')
    elif (body.count(native_block) != 1 or len(LEGACY_DECLARATION.findall(body)) != 1
          or body.count(LEGACY_ARGUMENTS) != 3 or body.count(LEGACY_FORMAT) != 3
          or MODERN_ARGUMENTS in body):
        raise ValueError('Historical fdinfo event-mask semantics were not fully restored')


def run_phase(kernel_root: Path, version: str, kernel: str, sublevel: str, phase: str) -> str:
    if phase not in {'prepare', 'finish'}:
        raise ValueError('phase must be prepare or finish')
    if version.removeprefix('v') != '2.3.0' or kernel not in {'5.10', '5.15'}:
        return 'Not applicable to this SUSFS/kernel combination.'
    if not re.fullmatch(r'(?:0|[1-9][0-9]*|X)', sublevel):
        raise ValueError('Invalid kernel sublevel')
    root = Path(kernel_root).resolve(strict=True)
    source = root / 'common' if (root / 'common/fs').is_dir() else root
    target = source / 'fs/notify/fdinfo.c'
    if not target.resolve(strict=True).is_relative_to(root):
        raise ValueError('fdinfo.c leaves the selected kernel root')
    state_path = root / STATE_NAME
    if state_path.is_symlink():
        raise ValueError('The fdinfo backup must not be a symlink')
    current = target.read_bytes().decode('utf-8')
    state = None
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('schema_version') != 1
                or state.get('kernel') != kernel or state.get('sublevel') != sublevel
                or not isinstance(state.get('original_source'), str)
                or digest(state['original_source']) != state.get('original_sha256')
                or state.get('phase') not in {'prepared', 'finished'}):
            raise ValueError('Invalid or mismatched historical fdinfo backup')
    if phase == 'prepare':
        if state is not None:
            if state['phase'] == 'finished':
                validate_restored(current, state['native_block'])
                return 'SUSFS 2.3 fdinfo was already completed.'
            if digest(current) == state['prepared_sha256']:
                return 'SUSFS 2.3 fdinfo context was already prepared.'
            validate_hooks(current)
            return 'The official SUSFS 2.3 fdinfo hooks are ready for restoration.'
        prepared, native_block = prepared_source(current)
        state = {'schema_version': 1, 'kernel': kernel, 'sublevel': sublevel,
                 'original_source': current, 'original_sha256': digest(current),
                 'prepared_sha256': digest(prepared), 'native_block': native_block,
                 'phase': 'prepared'}
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if prepared != current:
            target.write_bytes(prepared.encode('utf-8'))
        return 'Prepared historical fdinfo context.' if native_block else 'Modern fdinfo context needs no rewrite.'
    if state is None:
        raise ValueError('Missing historical fdinfo backup; prepare must precede the official patch')
    if state['phase'] == 'finished':
        validate_restored(current, state['native_block'])
        return 'SUSFS 2.3 fdinfo was already completed.'
    restored = restored_source(current, state['native_block']) if state['native_block'] else current
    validate_restored(restored, state['native_block'])
    if restored != current:
        target.write_bytes(restored.encode('utf-8'))
    state.update(phase='finished', finished_sha256=digest(restored))
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 'Verified complete SUSFS 2.3 fdinfo hooks and native event-mask semantics.'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--kernel', required=True)
    parser.add_argument('--sublevel', required=True)
    parser.add_argument('--phase', choices=('prepare', 'finish'), required=True)
    args = parser.parse_args(argv)
    try:
        print(run_phase(args.kernel_root, args.version, args.kernel, args.sublevel, args.phase))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'::error::Historical SUSFS 2.3 fdinfo integration failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
