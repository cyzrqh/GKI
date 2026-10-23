#!/usr/bin/env python3
"""Backport real Android Binder vendor hooks required by ReKernel on old GKI.

The hook declarations, event sites and GPL exports come from official Android
5.10/5.15 Binder sources; see tests/fixtures/historical-rekernel/README.md.
No callback is stubbed or disabled. Existing complete implementations are
verified and preserved. The old allocator's padding/check order is retained.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import tempfile

from validate_susfs_hooks import strip_comments_and_literals


PREFIX = 'android_vh_binder_'
HOOKS = ('alloc_new_buf_locked', 'reply', 'trans', 'preset')
PATHS = ('include/trace/hooks/binder.h', 'drivers/android/binder.c',
         'drivers/android/binder_alloc.c', 'drivers/android/vendor_hooks.c')


def compact(text: str) -> str:
    return re.sub(r'\s+', '', strip_comments_and_literals(text))


def declaration(hook: str, family: str) -> str:
    if hook == 'alloc_new_buf_locked':
        prototype = ('size_t size, struct binder_alloc *alloc, int is_async' if family == '5.10'
                     else 'size_t size, size_t *free_async_space, int is_async')
        arguments = 'size, alloc, is_async' if family == '5.10' else 'size, free_async_space, is_async'
    elif hook == 'preset':
        prototype, arguments = 'struct hlist_head *hhead, struct mutex *lock', 'hhead, lock'
    else:
        prototype = ('struct binder_proc *target_proc, struct binder_proc *proc,\n'
                     '\t\tstruct binder_thread *thread, struct binder_transaction_data *tr')
        arguments = 'target_proc, proc, thread, tr'
    return f'DECLARE_HOOK({PREFIX}{hook},\n\tTP_PROTO({prototype}),\n\tTP_ARGS({arguments}));\n'


def call(hook: str, family: str) -> str:
    arguments = {
        'alloc_new_buf_locked': ('size, alloc, is_async' if family == '5.10'
                                 else 'size, &alloc->free_async_space, is_async'),
        'reply': 'target_proc, proc, thread, tr',
        'trans': 'target_proc, proc, thread, tr',
        'preset': '&binder_procs, &binder_procs_lock',
    }[hook]
    return f'trace_{PREFIX}{hook}({arguments});'


def function_span(text: str, name: str) -> tuple[int, int]:
    code = strip_comments_and_literals(text)
    pattern = (r'(?m)^[ \t]*static[ \t]+(?:void|int|struct[ \t]+binder_buffer[ \t]*\*)'
               r'[ \t]*' + re.escape(name) + r'\s*\([^;{}]*\)\s*\{')
    matches = list(re.finditer(pattern, code))
    if len(matches) != 1:
        raise ValueError(f'Expected one real {name} implementation')
    opening = code.index('{', matches[0].start())
    depth = 0
    for index in range(opening, len(code)):
        depth += (code[index] == '{') - (code[index] == '}')
        if depth == 0:
            return matches[0].start(), index + 1
    raise ValueError(f'Unterminated {name} implementation')


def insert_at_anchor(text: str, function: str, pattern: str, insertion: str, *, after: bool) -> str:
    start, end = function_span(text, function)
    body = text[start:end]
    matches = list(re.finditer(pattern, strip_comments_and_literals(body)))
    if len(matches) != 1:
        raise ValueError(f'Unknown {function} event site; refusing a partial ReKernel backport')
    match = matches[0]
    position = start + (match.end() if after else match.start())
    return text[:position] + insertion + text[position:]


def checked_call(text: str, hook: str, family: str, function: str) -> bool:
    name = 'trace_' + PREFIX + hook
    code = strip_comments_and_literals(text)
    matches = list(re.finditer(r'\b' + name + r'\s*\([^;]*\)\s*;', code))
    if not matches:
        return False
    if len(matches) != 1 or compact(matches[0][0]) != compact(call(hook, family)):
        raise ValueError(f'Unexpected {name} callback contract')
    start, end = function_span(text, function)
    if not start <= matches[0].start() < matches[0].end() <= end:
        raise ValueError(f'{name} is not connected to its real {function} event')
    return True


def add_calls(binder: str, allocator: str, family: str) -> tuple[str, str]:
    if not checked_call(allocator, 'alloc_new_buf_locked', family, 'binder_alloc_new_buf_locked'):
        # Old official 5.10 places this notification before its async-space
        # check and pads zero-size buffers afterwards. Retain that order on
        # old 5.15 too, rather than importing unrelated allocator changes.
        pattern = (r'(?m)^\tif \(is_async &&\n'
                   r'\t[ \t]+alloc->free_async_space < size \+ sizeof\(struct binder_buffer\)\) \{')
        allocator = insert_at_anchor(allocator, 'binder_alloc_new_buf_locked', pattern,
                                     '\t' + call('alloc_new_buf_locked', family) + '\n', after=False)
    include = '#include <trace/hooks/binder.h>'
    includes = re.findall(r'(?m)^\s*#\s*include\s*<trace/hooks/binder\.h>\s*$', allocator)
    if len(includes) > 1:
        raise ValueError('Duplicate Binder vendor hook include in binder_alloc.c')
    if not includes:
        anchor = '#include "binder_trace.h"\n'
        if allocator.count(anchor) != 1:
            raise ValueError('Unknown binder_alloc.c header layout')
        allocator = allocator.replace(anchor, anchor + include + '\n', 1)

    if not checked_call(binder, 'reply', family, 'binder_transaction'):
        pattern = (r'(?m)^\t\ttarget_proc = target_thread->proc;\n'
                   r'\t\ttarget_proc->tmp_ref\+\+;\n'
                   r'\t\tbinder_inner_proc_unlock\(target_thread->proc\);\n')
        binder = insert_at_anchor(binder, 'binder_transaction', pattern,
                                  '\t\t' + call('reply', family) + '\n', after=True)
    if not checked_call(binder, 'trans', family, 'binder_transaction'):
        pattern = r'(?m)^\t\tif \(security_binder_transaction\(proc->cred,\n\t+target_proc->cred\) < 0\) \{'
        binder = insert_at_anchor(binder, 'binder_transaction', pattern,
                                  '\t\t' + call('trans', family) + '\n', after=False)
    if not checked_call(binder, 'preset', family, 'binder_open'):
        pattern = (r'(?m)^\thlist_add_head\(&proc->proc_node, &binder_procs\);\n'
                   r'\tmutex_unlock\(&binder_procs_lock\);\n')
        binder = insert_at_anchor(binder, 'binder_open', pattern,
                                  '\t' + call('preset', family) + '\n', after=True)
    return binder, allocator


def add_declarations(header: str, family: str) -> str:
    missing = []
    code = strip_comments_and_literals(header)
    for hook in HOOKS:
        symbol = PREFIX + hook
        matches = list(re.finditer(r'\bDECLARE_\w+\s*\(\s*' + symbol + r'\s*,[^;]*;', code))
        if not matches:
            missing.append(declaration(hook, family))
        elif len(matches) != 1 or compact(matches[0][0]) != compact(declaration(hook, family)):
            raise ValueError(f'Unexpected Binder hook declaration: {symbol}')
    if not missing:
        return header
    anchor = 'DECLARE_HOOK(android_vh_binder_transaction_init,'
    if header.count(anchor) != 1:
        raise ValueError('Unknown Binder hook header layout')
    return header.replace(anchor, ''.join(missing) + anchor, 1)


def add_exports(exports: str) -> str:
    if not re.search(r'(?m)^\s*#\s*define\s+CREATE_TRACE_POINTS\b', exports):
        raise ValueError('vendor_hooks.c does not instantiate the real tracepoints')
    if not re.search(r'(?m)^\s*#\s*include\s*<trace/hooks/binder\.h>', exports):
        raise ValueError('vendor_hooks.c does not include Binder hook declarations')
    missing = []
    code = strip_comments_and_literals(exports)
    for hook in HOOKS:
        entry = f'EXPORT_TRACEPOINT_SYMBOL_GPL({PREFIX}{hook});'
        matches = re.findall(r'\bEXPORT_TRACEPOINT_SYMBOL(?:_GPL)?\s*\(\s*' + PREFIX + hook + r'\s*\)\s*;', code)
        if not matches:
            missing.append(entry + '\n')
        elif len(matches) != 1 or compact(matches[0]) != compact(entry):
            raise ValueError(f'Unexpected Binder tracepoint export: {hook}')
    if not missing:
        return exports
    anchor = 'EXPORT_TRACEPOINT_SYMBOL_GPL(android_vh_binder_transaction_init);'
    if exports.count(anchor) != 1:
        raise ValueError('Unknown Binder tracepoint export layout')
    return exports.replace(anchor, ''.join(missing) + anchor, 1)


def read_regular(path: Path, root: Path) -> bytes:
    if not path.resolve(strict=True).is_relative_to(root) or path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f'Expected a regular kernel source file within the selected root: {path}')
    return path.read_bytes()


def atomic_write(path: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_compat(kernel_root: Path) -> tuple[str, tuple[str, ...]]:
    root = kernel_root.resolve(strict=True)
    source = root / 'common' if (root / 'common/Makefile').is_file() else root
    makefile = read_regular(source / 'Makefile', root).decode('utf-8')
    version = []
    for name in ('VERSION', 'PATCHLEVEL'):
        values = re.findall(r'(?m)^' + name + r'\s*=\s*(\d+)\s*$', makefile)
        if len(values) != 1:
            raise ValueError(f'Cannot determine kernel {name}')
        version.append(str(int(values[0])))
    family = '.'.join(version)
    if family not in {'5.10', '5.15'}:
        return family, ()
    originals = {item: read_regular(source / item, root) for item in PATHS}
    texts = {item: data.decode('utf-8').replace('\r\n', '\n') for item, data in originals.items()}
    header, binder, allocator, exports = (texts[item] for item in PATHS)
    header = add_declarations(header, family)
    binder, allocator = add_calls(binder, allocator, family)
    exports = add_exports(exports)
    updated = dict(zip(PATHS, (header, binder, allocator, exports)))
    # Recheck all callbacks before any write, including independently existing
    # pieces. An unexpected ABI or event layout must not yield a half-port.
    for hook in HOOKS:
        target, function = ((allocator, 'binder_alloc_new_buf_locked') if hook == 'alloc_new_buf_locked'
                            else (binder, 'binder_open' if hook == 'preset' else 'binder_transaction'))
        if not checked_call(target, hook, family, function):
            raise ValueError(f'Missing real Binder event: {hook}')
    updates = []
    for item, text in updated.items():
        if text != texts[item]:
            if b'\r\n' in originals[item]:
                text = text.replace('\n', '\r\n')
            updates.append((item, text.encode('utf-8')))
    for item, content in updates:
        atomic_write(source / item, content)
    return family, tuple(item for item, _ in updates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path, required=True)
    arguments = parser.parse_args()
    try:
        family, changed = apply_compat(arguments.kernel_root)
    except (OSError, ValueError, UnicodeError) as error:
        raise SystemExit(f'ReKernel Binder compatibility failed: {error}') from None
    if family not in {'5.10', '5.15'}:
        print(f'Kernel {family}: historical Binder backport not required.')
    else:
        print(f'Kernel {family}: verified all four real ReKernel Binder hooks; updated {len(changed)} files.')


if __name__ == '__main__':
    main()
