#!/usr/bin/env python3
"""Connect a frozen official manager and optional official SUSFS to OKI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from checkout_ksu_snapshot import checkout_snapshot
from integrate_sukisu import integrate
from manager_variants import manager_profile
from oplus_recipe import bool_value, check_kernel, git as source_git, native_inputs, strict_json, validate_profile
from set_manager_signature import configure
from shared_susfs_source import read_json, restore_source
from snapshot_ksu_source import temporary_directory
from validate_susfs_hooks import validate_hooks

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r'[0-9a-f]{40}\Z')
DEPENDENCY_REFS = {'osm0sis/AnyKernel3': 'refs/heads/master',
                   'Xiaomichael/kernel_patches': 'HEAD'}


def resolve_dependency(repository: str) -> str:
    """Read a public allowlisted ref, then use only its immutable commit.

    Public Git transport avoids per-worker GitHub API requests. No token is
    put in a URL, command argument or diagnostic, and no setup code is fetched.
    """
    if repository not in DEPENDENCY_REFS:
        raise ValueError('Unsupported OPlus prepared dependency repository')
    wanted = DEPENDENCY_REFS[repository]
    for attempt in range(3):
        try:
            output = source_git(Path.cwd(), 'ls-remote', '--exit-code',
                                'https://github.com/' + repository + '.git', wanted)
        except (OSError, ValueError):
            if attempt == 2:
                raise ValueError('Cannot resolve the fixed OPlus dependency reference after three attempts') from None
            print('OPlus prepared dependency temporarily unavailable; retrying public upstream', flush=True)
            time.sleep((2, 5)[attempt])
            continue
        fields = output.split()
        if len(fields) != 2 or not SHA.fullmatch(fields[0]) or fields[1] != wanted:
            raise ValueError('The prepared OPlus dependency returned an ambiguous or invalid ref')
        return fields[0]
    raise AssertionError('Unreachable dependency retry state')


def prepare_dependencies(unicode_bypass=False) -> dict[str, str]:
    unicode_bypass = bool_value(unicode_bypass)
    return {'ak3_ref': resolve_dependency('osm0sis/AnyKernel3'),
            'patches_ref': resolve_dependency('Xiaomichael/kernel_patches') if unicode_bypass else ''}


def prepared_outputs(values: dict[str, str]):
    if (not isinstance(values, dict) or set(values) != {'ak3_ref', 'patches_ref'}
            or any(not isinstance(value, str) for value in values.values())
            or not SHA.fullmatch(values['ak3_ref'])
            or values['patches_ref'] and not SHA.fullmatch(values['patches_ref'])):
        raise ValueError('Invalid prepared OPlus dependency output')
    if output := os.environ.get('GITHUB_OUTPUT'):
        path = Path(output)
        if path.is_symlink():
            raise ValueError('OPlus dependency output must be a regular GitHub output file')
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(''.join(name + '=' + value + '\n' for name, value in values.items()))
    print(json.dumps(values, sort_keys=True), flush=True)


def manager_selection(inputs: dict, reference: str, variant: str) -> dict:
    identity = manager_profile(variant)
    if inputs['kernelsu_variant'] != variant:
        raise ValueError('The prepared manager variant differs from the selected OPlus menu variant')
    if not isinstance(reference, str) or not SHA.fullmatch(reference):
        raise ValueError('OPlus manager requires the prepare job immutable source SHA')
    return identity


def verify_manager_checkout(root: Path, reference: str):
    repository = root / 'KernelSU'
    for path in (repository, repository / '.git', repository / 'kernel'):
        if path.is_symlink() or not path.is_dir():
            raise ValueError('The prepared manager must have its own real Git checkout')
    actual_root = source_git(repository, 'rev-parse', '--show-toplevel')
    if Path(actual_root).resolve(strict=True) != repository.resolve(strict=True):
        raise ValueError('The compiled manager is outside its prepared checkout')
    if source_git(repository, 'rev-parse', '--verify', 'HEAD') != reference:
        raise ValueError('The compiled manager HEAD differs from the prepare job immutable source SHA')


def command(arguments, *, cwd=None):
    result = subprocess.run([str(item) for item in arguments], cwd=cwd,
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=240)
    if result.returncode:
        if Path(str(arguments[0])).name.lower() in ('patch', 'patch.exe'):
            phase = 'reverse' if '--reverse' in arguments else 'forward'
            phase += ' dry-run' if '--dry-run' in arguments else ' apply'
            patch_name = Path(str(arguments[arguments.index('-i') + 1])).name if '-i' in arguments else 'unknown.patch'
            diagnostics = result.stdout + '\n' + result.stderr
            for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'SUKISU_APK_PASSWORD', 'SUKISU_APK_KEYSTORE'):
                if secret := os.environ.get(name):
                    diagnostics = diagnostics.replace(secret, '[REDACTED]')
            print(f'OPlus patch {patch_name} failed during {phase}:\n' + diagnostics[-12 * 1024:], file=sys.stderr)
        raise ValueError('OPlus integration command failed: ' + str(arguments[0])
                         + ' (exit ' + str(result.returncode) + ')')
    return result.stdout.strip()


def patch_checked(common: Path, patch: Path):
    if patch.is_symlink() or not patch.is_file() or not 0 < patch.stat().st_size <= 16 * 1024 * 1024:
        raise ValueError('The selected compatibility patch is missing or invalid')
    data = patch.read_bytes()
    strict_files = ()
    if patch.name == '50_add_susfs_in_gki-android14-6.1.patch':
        from oplus_susfs_headers import adapt_namespace_header
        data = adapt_namespace_header(common, data)
        strict_files = ('fs/namespace.c',)
    elif patch.name == '50_add_susfs_in_gki-android15-6.6.patch':
        from oplus_susfs66 import adapt_susfs66
        data = adapt_susfs66(common, data)
        strict_files = ('fs/namespace.c', 'fs/proc/task_mmu.c')
    elif patch.name == '50_add_susfs_in_gki-android16-6.12.patch':
        from oplus_compat import adapt_susfs_patch
        data = adapt_susfs_patch(common, data)
        strict_files = ('fs/proc/task_mmu.c',)
    # Never edit the immutable official SUSFS checkout. Known vendor context
    # adaptations keep all added code, and the complete resulting patch must
    # pass forward and reverse checks rather than ignoring rejected hunks.
    with temporary_directory(common.parent, '.oplus-patch-') as temporary:
        pieces = [(data, False)]
        if strict_files:
            # Preserve the exact vendor function/header port independently of
            # unrelated include-context differences in the other official files.
            pieces = []
            remaining = data
            for strict_file in strict_files:
                pattern = rb'(?ms)^diff --git a/' + re.escape(strict_file.encode()) + rb' b/' + re.escape(strict_file.encode()) + rb'\n.*?(?=^diff --git |\Z)'
                matches = list(re.finditer(pattern, remaining))
                if len(matches) != 1:
                    raise ValueError('The adapted official SUSFS file must have exactly one complete patch section')
                selected = matches[0]
                pieces.append((selected[0], True))
                remaining = remaining[:selected.start()] + remaining[selected.end():]
            if remaining.strip():
                pieces.append((remaining, False))
        commands = []
        for index, (content, strict) in enumerate(pieces):
            prepared = temporary / (str(index) + '-' + patch.name)
            prepared.write_bytes(content)
            commands.append(['patch', '--batch', '--fuzz=0' if strict else '--fuzz=3', '-p1', '-i', prepared])
        # All files pass before any source is modified, including the native
        # show_smap/rollup function port that must never use fuzzy placement.
        for options in commands:
            command([*options, '--forward', '--dry-run'], cwd=common)
        for options in commands:
            command([*options, '--forward'], cwd=common)
        for options in commands:
            command([*options, '--reverse', '--dry-run'], cwd=common)


def checked_root(workspace: Path, profile: dict) -> Path:
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError('OPlus workspace must be an existing real directory')
    root = workspace.resolve(strict=True) / 'kernel_workspace'
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Official kernel workspace has not been prepared')
    check_kernel(root / 'common', profile)
    return root


def config_values(defconfig: Path, values: dict[str, str]):
    if defconfig.is_symlink() or not defconfig.is_file():
        raise ValueError('Missing official gki_defconfig')
    text = defconfig.read_text(encoding='utf-8')
    for key, value in values.items():
        if not re.fullmatch(r'CONFIG_[A-Z0-9_]+', key) or value not in ('y', 'n', 'm'):
            raise ValueError('Invalid explicit OPlus configuration value')
        text = re.sub(r'(?m)^(?:' + key + r'=.*|# ' + key + r' is not set)\r?\n?', '', text)
        text = text.rstrip() + '\n' + key + '=' + value + '\n'
    defconfig.write_text(text, encoding='utf-8', newline='\n')


def integrate_manager(workspace: Path, profile: dict, inputs: dict, snapshot: Path,
                      reference: str, variant: str, susfs_sources: str):
    profile = validate_profile(profile)
    native_inputs(inputs, profile)
    manager_selection(inputs, reference, variant)
    root = checked_root(workspace, profile)
    checkout_snapshot(snapshot, root / 'KernelSU', reference, variant=variant)
    integrate(root, reference)
    from additional_manager_integration import effective_susfs, integrate_susfs
    if effective_susfs(variant, bool_value(inputs['enable_susfs'])):
        dependency = workspace.resolve() / 'official-susfs'
        target = profile['android_version'] + '-' + profile['kernel_version']
        restore_source(snapshot, dependency, reference, target, read_json(susfs_sources), variant=variant)
        header = dependency / 'kernel_patches/include/linux/susfs.h'
        if not re.search(r'^\s*#define\s+SUSFS_VERSION\s+"v2\.3\.0"', header.read_text(encoding='utf-8'), re.M):
            raise ValueError('The OPlus SUSFS integration requires official SUSFS v2.3.0')
        if variant == 'SukiSU':
            from apply_susfs23 import apply_integration
            apply_integration(root, dependency)
        else:
            integrate_susfs(root, dependency, variant)
        patch = dependency / ('kernel_patches/50_add_susfs_in_gki-' + target + '.patch')
        patch_checked(root / 'common', patch)
        for relative in ('fs', 'include/linux'):
            source = dependency / 'kernel_patches' / relative
            for file in source.iterdir():
                if file.is_symlink() or not file.is_file() or file.suffix not in ('.c', '.h'):
                    raise ValueError('Unsupported file in the official SUSFS source export')
                destination = root / 'common' / relative / file.name
                if destination.is_symlink():
                    raise ValueError('Refusing a linked SUSFS destination')
                if destination.exists() and destination.read_bytes() != file.read_bytes():
                    raise ValueError('The vendor source already contains a different SUSFS implementation')
                shutil.copyfile(file, destination)
    return root


def finalize_config(workspace: Path, profile: dict, inputs: dict, reference: str,
                    variant: str, patches: Path | None = None):
    profile = validate_profile(profile)
    native_inputs(inputs, profile)
    identity = manager_selection(inputs, reference, variant)
    root = checked_root(workspace, profile)
    verify_manager_checkout(root, reference)
    if os.environ.get('MANAGER_PACKAGE') != identity['package']:
        raise ValueError('The verified APK must use the selected official application ID')
    from manager_variants import ADDITIONAL_MANAGERS
    from additional_manager_integration import effective_susfs, configure as configure_official
    if variant in ADDITIONAL_MANAGERS:
        configure_official(root, os.environ['MANAGER_CERT_SIZE'], os.environ['MANAGER_CERT_SHA256'],
                           identity['package'], os.environ['MANAGER_VERSION_CODE'], variant=variant)
    else:
        configure(root, os.environ['MANAGER_CERT_SIZE'], os.environ['MANAGER_CERT_SHA256'],
                  identity['package'], os.environ['MANAGER_VERSION_CODE'])
    command([sys.executable, ROOT / '.github/scripts/ensure-ksu-compat.py', root])
    defconfig = root / 'common/arch/arm64/configs/gki_defconfig'
    enable_kpm = bool_value(inputs['use_kpm'])
    values = {'CONFIG_KSU': 'y', 'CONFIG_IKCONFIG': 'y', 'CONFIG_IKCONFIG_PROC': 'y',
              'CONFIG_KPROBES': 'y', 'CONFIG_HEADERS_INSTALL': 'y',
              'CONFIG_KPM': 'y' if enable_kpm else 'n'}
    susfs = effective_susfs(variant, bool_value(inputs['enable_susfs']))
    kconfig = (root / 'KernelSU/kernel/Kconfig').read_text(encoding='utf-8')
    if enable_kpm and not re.search(r'(?m)^config KPM\s*$', kconfig):
        raise ValueError('Selected manager source does not expose its required native KPM bridge configuration')
    values['CONFIG_KSU_SUSFS'] = 'y' if susfs else 'n'
    if variant == 'BakaSU':
        values['CONFIG_KSU_MULTI_MANAGER_SUPPORT'] = 'y'
        if not susfs:
            if not re.search(r'(?m)^config KSU_TRACEPOINT_HOOK\s*$', kconfig):
                raise ValueError('Review the selected BakaSU native hook mode before building OKI')
            values['CONFIG_KSU_TRACEPOINT_HOOK'] = 'y'
            values['CONFIG_KSU_MANUAL_HOOK'] = 'n'
        from ensure_bakasu_symbol_lookup import validate_native_symbol_lookup
        validate_native_symbol_lookup(root, reference)
    elif not susfs and re.search(r'(?m)^config KSU_KPROBES_HOOK\s*$', kconfig):
        values['CONFIG_KSU_KPROBES_HOOK'] = 'y'
    config_values(defconfig, values)
    if profile['kernel_version'] == '6.12' and variant == 'SukiSU':
        from oplus_ksu_compat import prepare_builtin_includes, prepare_kpm_members
        prepare_builtin_includes(root, reference)
        if enable_kpm:
            prepare_kpm_members(root, reference)
    from oplus_cctv import is_cctv
    if profile['kernel_version'] == '6.1' and not is_cctv(profile):
        from oplus_f2fs_compat import prepare_f2fs_compat
        prepare_f2fs_compat(root / 'common', profile)
        from oplus_scheduler_compat import prepare_scheduler_compat
        prepare_scheduler_compat(root / 'common', profile)
        from oplus_vendor_sources import restore_vendor_sources
        from oplus_vendor_locking import restore_vendor_locking
        from oplus_mtk_cpu import restore_mtk_cpu
        from oplus_mtk_memory import restore_mtk_memory
        from oplus_compat import prepare_vendor_dependencies
        for name, restore in (('gki-oplus-vendor-sources.json', restore_vendor_sources),
                              ('gki-oplus-vendor-locking.json', restore_vendor_locking),
                              ('gki-oplus-mtk-cpu.json', restore_mtk_cpu),
                              ('gki-oplus-mtk-memory.json', restore_mtk_memory)):
            if provenance := restore(root / 'common', profile):
                output = root / 'common/out'
                if output.is_symlink() or output.exists() and not output.is_dir():
                    raise ValueError('Vendor-source provenance requires an independent output directory')
                output.mkdir(exist_ok=True)
                report = output / name
                if report.is_symlink():
                    raise ValueError('Vendor-source provenance must be an independent regular report')
                report.write_text(json.dumps(provenance, sort_keys=True) + '\n', encoding='utf-8')
        prepare_vendor_dependencies(root / 'common')
    if bool_value(inputs['use_unicode_bypass']):
        if patches is None or patches.is_symlink():
            raise ValueError('Unicode compatibility requires its fixed reference patch checkout')
        expected = os.environ.get('PATCHES_REF', '')
        if (not SHA.fullmatch(expected)
                or command(['git', '-C', patches, 'rev-parse', 'HEAD']) != expected):
            raise ValueError('Unicode patch checkout differs from its prepared reference')
        patch_checked(root / 'common', patches / 'common/unicode_bypass_fix_6.1+.patch')
    if susfs:
        if profile['kernel_version'] == '6.1':
            if is_cctv(profile):
                from oplus_cctv import prepare_maps
                prepare_maps(root / 'common', profile)
            else:
                from oplus_vma_compat import prepare_vma_compat
                from oplus_mtk_vma_compat import prepare_mtk_vma_compat
                prepare_vma_compat(root / 'common', profile)
                prepare_mtk_vma_compat(root / 'common', profile)
        if profile['kernel_version'] == '6.6':
            from ensure_susfs_map_compat import ensure_map_compat
            ensure_map_compat(root)
        result = validate_hooks(root, variant=variant)
        if not result.calls or not result.ok:
            raise ValueError('The vendor SUSFS hooks do not match the selected manager implementation')
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('prepare-deps', 'integrate', 'finalize'), required=True)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--unicode-bypass', choices=('true', 'false'),
                        help='prepare-deps only: resolve the optional Unicode patch source')
    parser.add_argument('--snapshot', type=Path, default=Path('ksu-source-bundle'))
    parser.add_argument('--patches', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.phase == 'prepare-deps':
            if args.inputs is not None and args.unicode_bypass is not None:
                raise ValueError('Use either --inputs or --unicode-bypass when preparing dependencies')
            selected = False
            if args.inputs is not None:
                document = strict_json(args.inputs)
                if not isinstance(document, dict) or 'use_unicode_bypass' not in document:
                    raise ValueError('The prepare-deps inputs must declare use_unicode_bypass')
                selected = bool_value(document['use_unicode_bypass'])
            elif args.unicode_bypass is not None:
                selected = bool_value(args.unicode_bypass)
            prepared_outputs(prepare_dependencies(selected))
            return 0
        if args.workspace is None or args.profile is None or args.inputs is None or args.unicode_bypass is not None:
            raise ValueError('Kernel integration requires --workspace, --profile and --inputs')
        profile, inputs = strict_json(args.profile), strict_json(args.inputs)
        reference, variant = os.environ['KSU_REF'], os.environ['KSU_VARIANT']
        if args.phase == 'integrate':
            integrate_manager(args.workspace, profile, inputs, args.snapshot.resolve(), reference, variant,
                              os.environ.get('SUSFS_SOURCES', ''))
        else:
            finalize_config(args.workspace, profile, inputs, reference, variant, args.patches)
    except (OSError, ValueError, KeyError) as error:
        print('OPlus kernel integration stopped: ' + str(error), file=sys.stderr)
        return 1
    print('Official kernel / selected manager integration verified: ' + args.phase)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
