"""Resolve the real built-in SukiSU include root for vendor Kbuild 6.12.

6.12 Makefile.build already prefixes src with VPATH. Upstream's fallback
srctree/src therefore adds that prefix twice for an O=out built-in driver.
Use the currently included Kbuild file's real directory for built-in builds;
leave the upstream external-module branch and every compiler flag intact.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

from integrate_sukisu import stage
from validate_susfs_hooks import strip_comments_and_literals

INCLUDE_FLAGS = 'ccflags-y += -I$(KSU_KERNEL_DIR) -I$(KSU_KERNEL_DIR)/include\n'
FALLBACK = ('ifndef KSU_KERNEL_DIR\n'
            'ifeq ($(filter /%,$(src)),)\n'
            'KSU_KERNEL_DIR := $(srctree)/$(src)\n'
            'else\n'
            'KSU_KERNEL_DIR := $(src)\n'
            'endif\n'
            'endif\n')
BUILTIN_ROOT = ('# GKI: src already contains VPATH on vendor Kbuild 6.12.\n'
                'ifeq ($(KBUILD_EXTMOD),)\n'
                'KSU_KERNEL_DIR := $(realpath $(dir $(lastword $(MAKEFILE_LIST))))\n'
                'endif\n')
CB_MEMBER = 'DEFINE_MEMBER(netlink_kernel_cfg, cb_mutex)\n'
CB_GUARD = '#ifdef KSU_NETLINK_CFG_HAS_CB_MUTEX\n' + CB_MEMBER + '#endif\n'
CB_KBUILD = ("ksu_netlink_cfg := $(shell sed -n '/struct netlink_kernel_cfg {/,/^};/p' $(srctree)/include/linux/netlink.h)\n"
             'ifneq ($(findstring cb_mutex,$(ksu_netlink_cfg)),)\n'
             'ccflags-y += -DKSU_NETLINK_CFG_HAS_CB_MUTEX=1\n'
             'endif\n')


def git_blob(repository: Path, reference: str, name: str) -> bytes:
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(repository),
                             'cat-file', 'blob', reference + ':' + name],
                            capture_output=True, timeout=30)
    if result.returncode or not 0 < len(result.stdout) <= 1024 * 1024:
        raise ValueError('Cannot read the frozen SukiSU source blob: ' + name)
    return result.stdout


def tracked_header(repository: Path, path: Path, reference: str) -> bytes:
    resolved = path.resolve(strict=True)
    if (not resolved.is_relative_to(repository) or not resolved.is_file()
            or not 0 < resolved.stat().st_size <= 1024 * 1024):
        raise ValueError('SukiSU include must resolve to its own bounded official source header')
    name = resolved.relative_to(repository).as_posix()
    official = git_blob(repository, reference, name)
    if official != resolved.read_bytes():
        raise ValueError('SukiSU source header differs from the frozen official Git blob: ' + name)
    return official


def prepare_builtin_includes(root: Path, reference: str) -> bool:
    if not isinstance(reference, str) or not re.fullmatch(r'[0-9a-f]{40}', reference):
        raise ValueError('SukiSU includes require a frozen full source SHA')
    root = root.resolve(strict=True)
    repository = root / 'KernelSU'
    kernel = repository / 'kernel'
    for path in (repository, repository / '.git', kernel):
        if path.is_symlink() or not path.is_dir() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('SukiSU include root must be its own real frozen checkout')
    link = root / 'common/drivers/kernelsu'
    linked = link.is_symlink()
    junction = hasattr(link, 'is_junction') and link.is_junction()
    if (not (linked or junction) or linked and os.path.isabs(os.readlink(link))
            or link.resolve(strict=True) != kernel):
        raise ValueError('Built-in SukiSU driver must point to the frozen manager kernel sources')
    path = kernel / 'Kbuild'
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 1024 * 1024:
        raise ValueError('Expected the real frozen SukiSU Kbuild file')
    before = path.read_bytes()
    text = before.decode('utf-8')
    native = git_blob(repository, reference, 'kernel/Kbuild').decode('utf-8')
    if 'KSU_KERNEL_DIR' not in native and 'KSU_KERNEL_DIR' not in text:
        # Older stable layouts use their native flat-header include flags.
        return False
    if any(value.count(INCLUDE_FLAGS) != 1 or value.count(FALLBACK) != 1 for value in (native, text)):
        raise ValueError('Review the changed SukiSU include-root layout before vendor 6.12 compilation')
    for header in (kernel / 'include/uapi/app_profile.h', kernel / 'include/klog.h'):
        tracked_header(repository, header, reference)
    if BUILTIN_ROOT in text:
        if text.count(BUILTIN_ROOT) != 1 or BUILTIN_ROOT + FALLBACK not in text:
            raise ValueError('Unexpected or duplicate built-in SukiSU include-root adaptation')
        return False
    changed = text.replace(FALLBACK, BUILTIN_ROOT + FALLBACK, 1).encode('utf-8')
    temporary = stage(path, changed)
    try:
        if path.read_bytes() != before:
            raise ValueError('SukiSU Kbuild changed while validating its include-root adaptation')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def netlink_has_cb_mutex(text: str) -> bool:
    code = strip_comments_and_literals(text)
    starts = list(re.finditer(r'\bstruct[ \t\n]+netlink_kernel_cfg[ \t\n]*\{', code))
    if len(starts) != 1:
        raise ValueError('Expected exactly one real target netlink_kernel_cfg definition')
    opening = starts[0].end() - 1
    depth = 0
    for index in range(opening, len(code)):
        if code[index] == '{':
            depth += 1
        elif code[index] == '}':
            depth -= 1
            if depth == 0:
                body = code[opening + 1:index]
                return bool(re.search(r'\bcb_mutex\s*(?:\[[^\]]*\]\s*)?;', body))
    raise ValueError('Unterminated target netlink_kernel_cfg definition')


def prepare_kpm_members(root: Path, reference: str) -> bool:
    """Remove only a frozen unguarded probe of a truly absent target member."""
    if not isinstance(reference, str) or not re.fullmatch(r'[0-9a-f]{40}', reference):
        raise ValueError('Native KPM compatibility requires a frozen full source SHA')
    root = root.resolve(strict=True)
    repository = root / 'KernelSU'
    kernel = repository / 'kernel'
    for path in (repository, repository / '.git', kernel):
        if path.is_symlink() or not path.is_dir() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Native KPM must use its own real frozen source checkout')
    def read(path):
        if (path.is_symlink() or not path.is_file()
                or not path.resolve(strict=True).is_relative_to(root)
                or not 0 < path.stat().st_size <= 1024 * 1024):
            raise ValueError('Expected a bounded real native KPM compatibility source')
        return path.read_bytes()
    paths = [path for path in (kernel / 'extension/kpm/super_access.c', kernel / 'kpm/super_access.c')
             if path.exists()]
    if len(paths) != 1:
        raise ValueError('Expected one frozen native KPM super_access implementation')
    path = paths[0]
    original = git_blob(repository, reference, path.relative_to(repository).as_posix())
    before = read(path)
    native, current = original.decode('utf-8'), before.decode('utf-8')
    if original != before and original.replace(CB_MEMBER.encode(), b'', 1) != before:
        raise ValueError('Native KPM source differs from its frozen implementation and reviewed member adaptation')
    blocks = list(re.finditer(r'(?ms)^DYNAMIC_STRUCT_BEGIN\(netlink_kernel_cfg\)\n(.*?)'
                             r'^DYNAMIC_STRUCT_END\(netlink_kernel_cfg\)\n', native))
    if len(blocks) != 1 or native.count(CB_MEMBER) != 1 or blocks[0][1].count(CB_MEMBER) != 1:
        raise ValueError('Review the changed native KPM netlink member probe layout')
    present = netlink_has_cb_mutex(read(root / 'common/include/linux/netlink.h').decode('utf-8'))
    if CB_GUARD in blocks[0][1]:
        # New SukiSU probes the real target header in its own Kbuild. Keep
        # that guard and both member probes; never strip the guarded line.
        if before != original or native.count(CB_GUARD) != 1:
            raise ValueError('The native guarded KPM member implementation was modified')
        for data in (git_blob(repository, reference, 'kernel/Kbuild'), read(kernel / 'Kbuild')):
            if data.decode('utf-8').count(CB_KBUILD) != 1:
                raise ValueError('The guarded KPM member requires its native target-header Kbuild detection')
        return False
    preceding = blocks[0][1].split(CB_MEMBER, 1)[0]
    if re.search(r'(?m)^\s*#\s*(?:if|ifdef|ifndef|else|elif)\b', preceding):
        raise ValueError('Unreviewed conditional native KPM cb_mutex probe')
    if present:
        if before != original:
            raise ValueError('The present target cb_mutex probe must not be removed')
        return False
    changed = original.replace(CB_MEMBER.encode(), b'', 1)
    if before == changed:
        return False
    temporary = stage(path, changed)
    try:
        if path.read_bytes() != before:
            raise ValueError('Native KPM source changed while validating its member adaptation')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
