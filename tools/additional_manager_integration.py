"""Native trust and SUSFS integration for the four additional root managers.

Next/official integration follows the reviewed OPlus recipes; the generic GKI
and Kow/Fina paths follow WildKernels' native root setup and SUSFS layout.
The existing SukiSU/BakaSU adapters are deliberately left in their own paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from manager_variants import ADDITIONAL_MANAGERS, manager_profile
from prepare_manager_lkm import native_version_code
from set_manager_signature import PACKAGE, atomic_write, replace_package, replace_setting
from snapshot_ksu_source import git, source_identity


def effective_susfs(variant: str, requested: bool) -> bool:
    manager_profile(variant)
    if variant == 'FinaSU' and not requested:
        raise ValueError('FinaSU 官方内核依赖 SUSFS，请启用 SUSFS')
    # KowSU upstream intentionally has no SUSFS support (same policy as WildKernels).
    return requested and variant != 'KowSU'


def configure(root: Path, size: str, digest: str, package: str, code: str, *, variant: str) -> Path:
    if variant not in ADDITIONAL_MANAGERS or package != manager_profile(variant)['package']:
        raise ValueError('Additional manager trust requires the native official application ID')
    if (not PACKAGE.fullmatch(package) or not re.fullmatch(r'[0-9]+', size)
            or not 64 <= int(size) <= 4096 or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or not re.fullmatch(r'[1-9][0-9]*', code)):
        raise ValueError('Invalid verified official APK certificate/version')
    source = root / 'KernelSU'
    reference = git(source, 'rev-parse', 'HEAD')
    identity = source_identity(source, reference)
    expected = native_version_code(source, identity['git_count'], variant=variant)
    if int(code) != expected:
        raise ValueError('Official manager APK and native kernel source versions differ')
    path = source / 'kernel/Kbuild'
    original = path.read_text(encoding='utf-8')
    updated = original
    if variant == 'KernelSU-Next':
        for key, value in (('KSU_NEXT_MANAGER_SIZE', str(int(size))), ('KSU_NEXT_MANAGER_HASH', digest)):
            updated = replace_setting(updated, key, value)
        # Native Next uses the same optional package macro with a different log label.
        anchor = 'ifdef KSU_MANAGER_PACKAGE\n'
        if updated.count(anchor) != 1 or '-DKSU_MANAGER_PACKAGE=\\"$(KSU_MANAGER_PACKAGE)\\"' not in updated:
            raise ValueError('Review the native Next manager package block')
        updated = updated.replace(anchor, 'KSU_MANAGER_PACKAGE := ' + package + '\n' + anchor, 1)
    else:
        for key, value in (('KSU_EXPECTED_SIZE', str(int(size))), ('KSU_EXPECTED_HASH', digest)):
            updated = replace_setting(updated, key, value)
        updated = replace_package(updated, package)
    # Preserve the actual upstream count, independently proved above. Freeze it
    # so the parent kernel Git history cannot change the nested driver's version.
    formula = '$(eval KSU_VERSION=$(shell expr 30000 + $(KSU_GIT_VERSION)))'
    if updated.count(formula) != 1:
        raise ValueError('Review the native manager version formula')
    updated = updated.replace(formula, 'KSU_VERSION := ' + code, 1)
    if updated != original:
        atomic_write(path, updated.encode('utf-8'))
    return path


def integrate_susfs(root: Path, dependency: Path, variant: str) -> None:
    if variant not in ADDITIONAL_MANAGERS:
        return
    source = root / 'KernelSU'
    if variant == 'KowSU':
        raise ValueError('KowSU 官方源码不支持 SUSFS')
    if variant == 'KernelSU-Next':
        name = 'next-susfs23' if (source / 'kernel/preinit/preinit.c').is_file() else 'next-susfs23-stable'
        apply_next_port(source, name=name)
    if variant == 'KernelSU':
        patch = dependency / 'kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch'
        if not patch.is_file():
            raise ValueError('Missing the official KernelSU SUSFS integration patch')
        for reverse in (False, True):
            command = ['git', '-C', str(source), 'apply', '--check', '--recount']
            if reverse:
                command.append('--reverse')
            check = subprocess.run([*command, str(patch.resolve())], capture_output=True, timeout=30)
            if check.returncode == 0:
                if not reverse:
                    subprocess.run(['git', '-C', str(source), 'apply', '--recount', str(patch.resolve())],
                                   check=True, capture_output=True, timeout=30)
                return
        name = 'official-susfs23' if (source / 'kernel/preinit/preinit.c').is_file() else 'official-susfs23-stable'
        apply_next_port(source, name=name)
    files = list((source / 'kernel').rglob('*.c'))
    combined = '\n'.join(p.read_text(encoding='utf-8') for p in files)
    symbols = (('susfs', 'ksu_preinit_handle_post_execveat', 'ksu_setup_preinit_file')
               if (source / 'kernel/preinit/preinit.c').is_file() or variant == 'FinaSU'
               else ('susfs', 'ksu_handle_execveat', 'ksu_handle_input_handle_event'))
    if not all(symbol in combined for symbol in symbols):
        raise ValueError('Selected manager lacks the required native SUSFS implementation')


def verify_legacy_next_lifecycle(source: Path) -> None:
    """Preserve Next's real late-load state without a removed profile initializer."""
    core = (source / 'kernel/core/init.c').read_text(encoding='utf-8')
    header = (source / 'kernel/include/ksu.h').read_text(encoding='utf-8')
    if (len(re.findall(r'^bool ksu_late_loaded\s*;', core, re.M)) != 1
            or len(re.findall(r'^extern bool ksu_late_loaded\s*;', header, re.M)) != 1):
        raise ValueError('Next native late-load state definition/declaration is missing')
    from validate_susfs_hooks import inspect_c_source
    calls, _ = inspect_c_source(source / 'kernel/core/init.c', core, symbols=('ksu_app_profile_init',))
    if calls:
        profile = source / 'kernel/policy/app_profile.c'
        _, definitions = inspect_c_source(profile, profile.read_text(encoding='utf-8'), symbols=('ksu_app_profile_init',))
        if not any(item.symbol == 'ksu_app_profile_init' and not item.is_static for item in definitions):
            raise ValueError('Next lifecycle calls a profile initializer removed by its SUSFS integration')
    from validate_susfs_hooks import strip_comments_and_literals
    supercall = (source / 'kernel/supercall/supercall.c').read_text(encoding='utf-8')
    dispatch = (source / 'kernel/supercall/dispatch.c').read_text(encoding='utf-8')
    declaration = (source / 'kernel/supercall/supercall.h').read_text(encoding='utf-8')
    signature = r'int\s+ksu_handle_toolkit_reboot\(int magic2, unsigned int cmd, void __user \*arg\)'
    if (not re.search(signature + r'\s*\{', supercall)
            or not re.search(signature + r'\s*;', declaration)
            or dispatch.count('return ksu_handle_toolkit_reboot(magic2, cmd, *arg);') != 1):
        raise ValueError('Next native toolkit reboot parameters are not connected to the SUSFS dispatcher')
    fd_handler = supercall.split('int ksu_supercall_reboot_handler(void __user **arg)', 1)[-1]
    fd_handler = fd_handler.split('\nint ksu_handle_toolkit_reboot', 1)[0]
    if re.search(r'\b(magic2|cmd|arg4|reply)\b', strip_comments_and_literals(fd_handler)):
        raise ValueError('Next FD installer incorrectly contains the toolkit command dispatcher')


def apply_next_port(source: Path, *, name: str = 'next-susfs23') -> None:
    if name not in ('next-susfs23', 'official-susfs23', 'next-susfs23-stable', 'official-susfs23-stable'):
        raise ValueError('Unsupported reviewed SUSFS compatibility port')
    directory = Path(__file__).resolve().parent / 'patches'
    patch = directory / (name + '.patch')
    inventory = json.loads((directory / (name + '-base') / 'objects.json').read_text(encoding='utf-8'))
    if hashlib.sha256(patch.read_bytes()).hexdigest() != inventory['patch_sha256']:
        raise ValueError('Next SUSFS port differs from its reviewed digest')
    def verify():
        if name == 'next-susfs23-stable':
            verify_legacy_next_lifecycle(source)
    def call(repo, *args):
        return subprocess.run(['git', '-C', str(repo), '-c', 'core.autocrlf=false', *args],
                              capture_output=True, timeout=120)
    if call(source, 'apply', '--reverse', '--check', str(patch)).returncode == 0:
        verify()
        return
    if call(source, 'apply', '--check', str(patch)).returncode == 0:
        result = call(source, 'apply', str(patch))
        if result.returncode:
            raise ValueError('The reviewed Next SUSFS port could not be applied')
        verify()
        return
    # Rebase the complete reviewed port in a disposable checkout. Never accept
    # --3way --check alone: it may report success while leaving conflicted hunks.
    with tempfile.TemporaryDirectory(prefix='gki-next-port-') as temporary:
        checked = Path(temporary) / 'source'
        result = subprocess.run(['git', 'clone', '--no-hardlinks', '--config', 'core.autocrlf=false',
                                 str(source.resolve()), str(checked)], capture_output=True, timeout=120)
        if result.returncode:
            raise ValueError('Could not prepare the independent Next port validation checkout')
        reference = git(source, 'rev-parse', 'HEAD')
        if call(checked, 'checkout', '--detach', reference).returncode:
            raise ValueError('Next port validation checkout does not contain the selected official source')
        for identity, digest in inventory['objects'].items():
            raw = (directory / (name + '-base') / (digest + '.source')).read_bytes()
            if (hashlib.sha256(raw).hexdigest() != digest
                    or hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != identity):
                raise ValueError('Next SUSFS merge preimage differs from its reviewed digest')
            seeded = subprocess.run(['git', '-C', str(checked), 'hash-object', '-w', '--stdin'],
                                    input=raw, capture_output=True, timeout=20)
            if seeded.returncode or seeded.stdout.strip().decode() != identity:
                raise ValueError('Cannot seed the reviewed Next SUSFS merge preimage')
        merged = call(checked, 'apply', '--3way', str(patch))
        if merged.returncode or call(checked, 'ls-files', '--unmerged').stdout.strip():
            raise ValueError('Next official source conflicts with the reviewed SUSFS port; review required')
        prepared = Path(temporary) / 'rebased.patch'
        prepared.write_bytes(call(checked, 'diff', '--binary', '--full-index', 'HEAD', '--', 'kernel').stdout)
        if (not prepared.stat().st_size or call(source, 'apply', '--check', str(prepared)).returncode
                or call(source, 'apply', str(prepared)).returncode):
            raise ValueError('Rebased Next SUSFS port could not pass the real source dry run')
        verify()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path, required=True)
    parser.add_argument('--variant', choices=ADDITIONAL_MANAGERS, default=os.environ.get('KSU_VARIANT'))
    parser.add_argument('--susfs-dir', type=Path)
    args = parser.parse_args()
    if args.susfs_dir:
        integrate_susfs(args.kernel_root, args.susfs_dir, args.variant)
    else:
        configure(args.kernel_root, os.environ['MANAGER_CERT_SIZE'], os.environ['MANAGER_CERT_SHA256'],
                  os.environ['MANAGER_PACKAGE'], os.environ['MANAGER_VERSION_CODE'], variant=args.variant)


if __name__ == '__main__':
    main()
