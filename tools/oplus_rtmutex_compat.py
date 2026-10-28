"""Keep the reviewed rtmutex CVE fix usable on official OPlus Linux 6.1.

The fixed remove_waiter and proxy-lock functions were checked against all
eight official 6.1 source refs in the OPlus catalog. Their post-CVE bodies
are identical. No function is accepted by a loose text or version match.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import uuid

from validate_susfs_hooks import strip_comments_and_literals


# OnePlusOSS/android_kernel_common_oneplus_sm8650 at
# e02429a7e0f337d517be2c01e12d1cf153df4542, kernel/locking/{rtmutex,rtmutex_api}.c.
# Same bodies in the other seven fixed official 6.1 catalog refs. The recipe's
# CVE patch is cctv18/oppo_oplus_realme_sm8650 f4e3a8084a7a5f8203e16ea177c8847c7ff517dc
# other_patch/cve-2026-43499-rtmutex-6.1.patch, SHA256 below.
CVE_PATCH_SHA256 = '6e9dc91d68b94e9bd5230bd1099e427bde744bc7337ba26d79d2a053640a211e'
SCOPED_FUNCTION_SHA256 = 'a7ad6049de3223b47c61b01538cd4a6f9112df10a43c5766321f61082c4ea944'
EXPLICIT_FUNCTION_SHA256 = 'cdabd4281c8460e2001d00cbf41ae3936db98494f09f166d320a4d73b93f696d'
PROXY_FUNCTION_SHA256 = 'b9db46c0ed1b1ffff8407bd28abd60e97adf28e6d78fea3e8ab8d74dcb2e470c'
SCOPED_LOCK = ('\tscoped_guard(raw_spinlock, &waiter_task->pi_lock) {\n'
               '\t\trt_mutex_dequeue(lock, waiter);\n'
               '\t\twaiter_task->pi_blocked_on = NULL;\n\t}')
EXPLICIT_LOCK = ('\traw_spin_lock(&waiter_task->pi_lock);\n'
                 '\trt_mutex_dequeue(lock, waiter);\n'
                 '\twaiter_task->pi_blocked_on = NULL;\n'
                 '\traw_spin_unlock(&waiter_task->pi_lock);')


def function_text(source: str, name: str) -> str:
    code = strip_comments_and_literals(source)
    matches = list(re.finditer(r'(?m)^[A-Za-z_][^;{}\n]*\b' + re.escape(name)
                              + r'\s*\([^;{}]*\)\s*\{', code))
    if len(matches) != 1:
        raise ValueError('Expected one reviewed rtmutex function: ' + name)
    opening = code.index('{', matches[0].start(), matches[0].end())
    depth = 0
    for index in range(opening, len(code)):
        depth += (code[index] == '{') - (code[index] == '}')
        if depth == 0:
            return source[matches[0].start():index + 1]
    raise ValueError('Unterminated rtmutex function: ' + name)


def adapt_rtmutex_source(source: str, api: str) -> str:
    proxy = function_text(api, 'rt_mutex_start_proxy_lock')
    if hashlib.sha256(proxy.encode('utf-8')).hexdigest() != PROXY_FUNCTION_SHA256:
        raise ValueError('The reviewed rtmutex negative-result CVE guard is missing or changed')
    function = function_text(source, 'remove_waiter')
    digest = hashlib.sha256(function.encode('utf-8')).hexdigest()
    if digest == EXPLICIT_FUNCTION_SHA256:
        return source
    if digest != SCOPED_FUNCTION_SHA256 or function.count(SCOPED_LOCK) != 1:
        raise ValueError('The reviewed rtmutex waiter-task lifetime/null/priority fix is missing or changed')
    # remove_waiter is called with wait_lock held and interrupts disabled.
    # scoped_guard(raw_spinlock) only acquires/releases this same raw lock;
    # explicit locking preserves the existing IRQ state and whole CVE fix.
    changed = function.replace(SCOPED_LOCK, EXPLICIT_LOCK, 1)
    if hashlib.sha256(changed.encode('utf-8')).hexdigest() != EXPLICIT_FUNCTION_SHA256:
        raise ValueError('Unexpected explicit rtmutex lock adaptation')
    return source.replace(function, changed, 1)


def linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def read_source(common: Path, relative: str) -> tuple[Path, bytes]:
    path = common / relative
    if (any(linked(part) for part in (path, *path.parents) if part.is_relative_to(common))
            or not path.is_file() or not 0 < path.stat().st_size <= 4 * 1024 * 1024):
        raise ValueError('Expected bounded regular official rtmutex compatibility sources')
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('The rtmutex compatibility input exceeded its bound')
    raw.decode('utf-8')
    return path, raw


def prepare_rtmutex_compat(common: Path) -> bool:
    common = Path(common).absolute()
    _, makefile = read_source(common, 'Makefile')
    version = []
    for field in ('VERSION', 'PATCHLEVEL'):
        values = re.findall(r'(?m)^' + field + r'[ \t]*=[ \t]*([0-9]+)[ \t]*$', makefile.decode('utf-8'))
        if len(values) != 1:
            raise ValueError('Official kernel series is missing or ambiguous for rtmutex compatibility')
        version.append(int(values[0]))
    if version != [6, 1]:
        return False
    path, original = read_source(common, 'kernel/locking/rtmutex.c')
    _, api = read_source(common, 'kernel/locking/rtmutex_api.c')
    changed = adapt_rtmutex_source(original.decode('utf-8'), api.decode('utf-8')).encode('utf-8')
    if changed == original:
        return False
    temporary = path.with_name('.oplus-rtmutex-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(changed)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        # Validate the complete read set before the single atomic replacement.
        if (read_source(common, 'Makefile')[1] != makefile
                or read_source(common, 'kernel/locking/rtmutex.c')[1] != original
                or read_source(common, 'kernel/locking/rtmutex_api.c')[1] != api):
            raise ValueError('Official rtmutex sources changed during compatibility preparation')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
