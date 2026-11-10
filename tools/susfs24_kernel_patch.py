#!/usr/bin/env python3
"""Apply the official SUSFS v2.4 Linux hooks without publishing partial patches."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
from pathlib import Path
import re
import shutil
import sys

from checked_kernel_patch import patch_checked
from historical_susfs23 import prepared_source, restored_source, validate_restored
from oplus_compat import function_text
from snapshot_ksu_source import temporary_directory
from validate_susfs_hooks import strip_comments_and_literals


TARGETS = ('android12-5.10', 'android13-5.15', 'android14-6.1',
           'android15-6.6', 'android16-6.12')
EXPORTS = ('fs/susfs.c', 'include/linux/susfs.h', 'include/linux/susfs_def.h')
# These are the unchanged 2.3 fdinfo hunks whose three output paths the
# historical adapter already validates, including the native event mask.
LEGACY_FDINFO = {
    'android12-5.10': 'fd0bdebe9611dd5995e4515a1c91e35a84ac64c1d6ad88d61041ce421908654d',
    'android13-5.15': 'e21c8f5b8fd3e7a07be235ca8d4b23d30ea0a9cbd03845b8c15c1a5aa8c94289',
}
OPTIONAL_CONTEXT = {
    'android13-5.15': {
        'fs/namespace.c': ('linux/mnt_idmapping.h', 'trace/hooks/blk.h'),
        'fs/open.c': ('linux/mnt_idmapping.h',),
        'fs/proc/task_mmu.c': ('trace/hooks/mm.h',),
    },
    'android15-6.6': {
        'fs/proc/base.c': ('linux/dma-buf.h',),
        'mm/memory.c': ('linux/zswap.h',),
    },
    'android16-6.12': {
        'fs/exec.c': ('linux/dma-buf.h',),
    },
}
SECTION = re.compile(r'(?m)^diff --git a/([a-zA-Z0-9_./-]+) b/\1\n')
HUNK = re.compile(r'(?m)^@@ -(\d+),(\d+) \+(\d+),(\d+) @@([^\n]*)\n')
POLICY_SERVICES = 'security/selinux/ss/services.c'
POLICY_ARGS = 'struct policydb *policydb, struct context *scontext, struct context *tcontext, u16 tclass'
LEGACY_POLICY = (
    ('security_dump_masked_av', 'u32 permissions, const char *reason', 'permissions, reason'),
    ('context_struct_compute_av', 'struct av_decision *avd, struct extended_perms *xperms', 'avd, xperms'),
)


def patch_sections(patch: str) -> dict[str, str]:
    """Require simple modifications to bounded, named Linux source files."""
    sections = {}
    for section in re.split(r'(?=^diff --git )', patch, flags=re.M):
        if not section.strip():
            continue
        match = SECTION.match(section)
        if match is None:
            raise ValueError('Unrecognized official SUSFS patch section')
        relative = match[1]
        if relative in sections or any(part in ('', '.', '..') for part in relative.split('/')):
            raise ValueError(f'Invalid or duplicated SUSFS source path: {relative}')
        header = section.split('\n@@ ', 1)[0]
        if (re.findall(r'(?m)^--- (.*)$', header) != [f'a/{relative}']
                or re.findall(r'(?m)^\+\+\+ (.*)$', header) != [f'b/{relative}']):
            raise ValueError(f'SUSFS patch must modify the same native file: {relative}')
        sections[relative] = section
    if not sections:
        raise ValueError('Empty official SUSFS patch')
    return sections


def source_path(root: Path, relative: str, *, required: bool = True) -> Path:
    path = root
    for part in relative.split('/'):
        path /= part
        if path.is_symlink():
            raise ValueError(f'Refusing linked SUSFS source path: {relative}')
    if (required or path.exists()) and (not path.is_file() or not 0 < path.stat().st_size <= 4 * 1024 * 1024):
        raise ValueError(f'Missing or invalid SUSFS source file: {relative}')
    return path


def adapt_header(section: str, source: str, headers: tuple[str, ...]) -> str:
    first = HUNK.search(section)
    if first is None:
        raise ValueError('Missing official SUSFS header hunk')
    second = HUNK.search(section, first.end())
    end = second.start() if second else len(section)
    hunk = section[first.end():end]
    lines = hunk.splitlines(keepends=True)
    optional = [f' #include <{header}>\n' for header in headers]
    omitted = [line for line in optional if line in lines and line[1:] not in source]
    if not omitted:
        return section
    adapted = [line for line in lines if line not in omitted]
    # One 6.6 vendor tree puts its trace hook after CREATE_TRACE_POINTS is
    # undefined. Preserve that native order and bind the hunk at kmem.h.
    if (section.startswith('diff --git a/mm/memory.c ')
            and adapted[-1] == ' #include <trace/hooks/mm.h>\n'
            and '#include <trace/events/kmem.h>\n\n#undef CREATE_TRACE_POINTS\n'
                '#include <trace/hooks/mm.h>\n' in source):
        adapted.pop()
    context = ''.join(line[1:] for line in adapted if line.startswith(' '))
    if (any(lines.count(line) != 1 for line in omitted)
            or any(line.startswith('-') for line in lines)
            or '+#include <linux/susfs_def.h>\n' not in lines
            or not context or source.count(context) != 1):
        raise ValueError('Unknown native source layout in official SUSFS header hunk: '
                         + section.splitlines()[0])
    removed = len(lines) - len(adapted)
    heading = (f'@@ -{first[1]},{int(first[2]) - removed} '
               f'+{first[3]},{int(first[4]) - removed} @@{first[5]}\n')
    return section[:first.start()] + heading + ''.join(adapted) + section[end:]


def guard_preinit(section: str) -> str:
    # Upstream's ccflag is local to the KernelSU build directory, so it
    # cannot control calls compiled separately by Linux fs/Makefile.
    opening = '+#ifndef KSU_NO_PREINIT\n'
    closing = '+#endif // #ifndef KSU_NO_PREINIT\n'
    if section.count(opening) != 4 or section.count(closing) != 4:
        raise ValueError('Unknown SUSFS v2.4 native preinit hook layout in fs/exec.c')
    guard = '#ifdef CONFIG_KSU_SUSFS_NATIVE_PREINIT'
    return section.replace(opening, '+' + guard + '\n').replace(closing, '+#endif // ' + guard + '\n')


def restore_legacy_policy_services(common: Path, manager: Path) -> None:
    hide = manager / 'kernel/feature/selinux_hide.c'
    if not hide.exists() and not hide.is_symlink():
        return
    code = strip_comments_and_literals(source_path(manager, 'kernel/feature/selinux_hide.c').read_text(encoding='utf-8'))
    if not any(re.search(r'\bextern\s+void\s+' + name + r'_fn\s*\(', code) for name, _, _ in LEGACY_POLICY):
        return
    # The reviewed stable ports use the original native policy on a supplied
    # policydb. The v2.4 live-policy spoof helper has a different contract.
    from additional_manager_integration import verify_stable_policy_bridge
    verify_stable_policy_bridge(manager)
    path = source_path(common, POLICY_SERVICES)
    text = path.read_bytes().decode('utf-8')
    services = strip_comments_and_literals(text)
    manager_code = ' '.join(code.split())
    updated = text
    for name, tail, arguments in LEGACY_POLICY:
        native = function_text(text, name)
        signature = native[:native.index('{')].strip()
        if ' '.join(signature.split()) != f'static void {name}({POLICY_ARGS}, {tail})':
            raise ValueError(f'Review changed native SELinux service signature: {name}')
        declaration = signature.replace(f'static void {name}(', f'void {name}_fn(', 1)
        if f'extern {" ".join(declaration.split())};' not in manager_code:
            raise ValueError(f'Legacy SUSFS manager and native SELinux ABI differ: {name}_fn')
        wrapper = ('\n\n#ifdef CONFIG_KSU_SUSFS\n' + declaration + ';\n'
                   + declaration + '\n{\n'
                   + f'\t{name}(policydb, scontext, tcontext, tclass, {arguments});\n'
                   + '}\n#endif // #ifdef CONFIG_KSU_SUSFS')
        if text.count(native) != 1 or re.search(r'\b' + name + r'_fn\b', services):
            raise ValueError(f'Conflicting native SUSFS policy service: {name}_fn')
        updated = updated.replace(native, native + wrapper, 1)
    path.write_bytes(updated.encode('utf-8'))


def apply_susfs24(kernel_root: Path, susfs_dir: Path, target: str, *,
                  apply_patch: Callable[[Path, Path], None] | None = None) -> None:
    if target not in TARGETS or kernel_root.is_symlink() or susfs_dir.is_symlink():
        raise ValueError(f'Unsupported SUSFS v2.4 kernel target: {target}')
    kernel_root = kernel_root.resolve(strict=True)
    susfs_dir = susfs_dir.resolve(strict=True)
    export_root = susfs_dir / 'kernel_patches'
    if export_root.is_symlink():
        raise ValueError('Refusing linked official SUSFS patch directory')
    patch_name = f'50_add_susfs_in_gki-{target}.patch'
    patch = source_path(export_root, patch_name)
    exports = {name: source_path(export_root, name).read_bytes() for name in EXPORTS}
    if not re.search(rb'^#define[ \t]+SUSFS_VERSION[ \t]+"v2\.4\.0"[ \t]*$',
                     exports['include/linux/susfs.h'], re.M):
        raise ValueError('SUSFS v2.4 Linux hooks require official v2.4.0 source')

    sections = patch_sections(patch.read_bytes().decode('utf-8'))
    if not {'fs/exec.c', 'fs/notify/fdinfo.c', 'block/ioprio.c',
            'security/selinux/status.c'}.issubset(sections) or sections.keys() & exports.keys():
        raise ValueError('Incomplete or inconsistent official SUSFS v2.4 Linux patch')
    sections['fs/exec.c'] = guard_preinit(sections['fs/exec.c'])
    if apply_patch is None:
        apply_patch = patch_checked

    with temporary_directory(kernel_root.parent, '.susfs24-') as temporary:
        stage = temporary / 'common'
        stage.mkdir()
        originals = {}
        for relative in sections:
            data = source_path(kernel_root, relative).read_bytes()
            originals[relative] = data
            destination = stage / relative
            destination.parent.mkdir(exist_ok=True, parents=True)
            destination.write_bytes(data)
        for relative, headers in OPTIONAL_CONTEXT.get(target, {}).items():
            sections[relative] = adapt_header(sections[relative], originals[relative].decode('utf-8'), headers)

        fdinfo = stage / 'fs/notify/fdinfo.c'
        native_block = None
        if target in LEGACY_FDINFO:
            if hashlib.sha256(sections['fs/notify/fdinfo.c'].encode('utf-8')).hexdigest() != LEGACY_FDINFO[target]:
                raise ValueError('Review changed official SUSFS fdinfo hooks before adapting historical GKI')
            prepared, native_block = prepared_source(originals['fs/notify/fdinfo.c'].decode('utf-8'))
            fdinfo.write_bytes(prepared.encode('utf-8'))

        staged_patch = temporary / patch_name
        staged_patch.write_bytes(''.join(sections.values()).encode('utf-8'))
        apply_patch(stage, staged_patch)
        if target in ('android15-6.6', 'android16-6.12'):
            restore_legacy_policy_services(stage, kernel_root.parent / 'KernelSU')
        if target in LEGACY_FDINFO:
            text = fdinfo.read_bytes().decode('utf-8')
            if native_block is not None:
                text = restored_source(text, native_block)
                fdinfo.write_bytes(text.encode('utf-8'))
            validate_restored(text, native_block)

        for relative, before in originals.items():
            if source_path(kernel_root, relative).read_bytes() != before:
                raise ValueError(f'Native source changed during SUSFS v2.4 preflight: {relative}')
            if source_path(stage, relative).read_bytes() == before:
                raise ValueError(f'SUSFS v2.4 patch did not change its expected source: {relative}')
        for relative, data in exports.items():
            path = source_path(kernel_root, relative, required=False)
            if path.exists() and path.read_bytes() != data:
                raise ValueError(f'Native source already contains a different SUSFS export: {relative}')
        for relative in originals:
            shutil.copyfile(stage / relative, kernel_root / relative)
        for relative, data in exports.items():
            path = kernel_root / relative
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_bytes(data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path, required=True)
    parser.add_argument('--susfs-dir', type=Path, required=True)
    parser.add_argument('--target', choices=TARGETS, required=True)
    args = parser.parse_args(argv)
    try:
        apply_susfs24(args.kernel_root, args.susfs_dir, args.target)
    except (OSError, ValueError) as error:
        print(f'::error::SUSFS v2.4 Linux patch failed for {args.target}: {error}', file=sys.stderr)
        return 1
    print(f'Applied and checked the complete official SUSFS v2.4 Linux patch for {args.target}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
