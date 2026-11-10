"""Check the build's actual SUSFS 2.4 manager adapter on isolated source copies."""
from __future__ import annotations

import hashlib
from pathlib import Path

from manager_variants import manager_profile
from official_susfs_archive import git_bytes, import_archive
from pin_build_dependencies import SUSFS_24
from shared_susfs_source import NAMESPACE, fetch, metadata, verify_local_sources
from snapshot_ksu_source import SnapshotError, git, temporary_directory


# These are the manager-side inputs. The target-specific Linux patch is checked
# separately against the actual Android or OPlus tree at kernel build time.
ADAPTER_INPUTS = (
    'kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch',
    'kernel_patches/fs/susfs.c',
    'kernel_patches/include/linux/susfs.h',
    'kernel_patches/include/linux/susfs_def.h',
)


def fetch_pins(source: Path, pins: dict[str, str]) -> dict:
    try:
        fetch(source, ['+' + ref + ':' + NAMESPACE + target for target, ref in sorted(pins.items())], attempts=1)
    except SnapshotError:
        sources = {target: import_archive(source, target, ref) for target, ref in sorted(pins.items())}
    else:
        sources = {target: metadata(source, target, NAMESPACE + target) for target in sorted(pins)}
    for target, item in sources.items():
        if item['ref'] != pins[target] or item['version'] != SUSFS_24:
            raise SnapshotError('Reviewed SUSFS 2.4 pin does not match its official tree for ' + target)
    return sources


def manager_inputs(source: Path, sources: dict) -> dict:
    selected = {target: row for target, row in sources.items() if row['version'] == SUSFS_24}
    if not selected:
        return {}
    verify_local_sources(source, selected)
    return {target: {name: git_bytes(source, 'cat-file', 'blob', item['tree'] + ':' + name)
                     for name in ADAPTER_INPUTS} for target, item in sorted(selected.items())}


def verify_manager(source: Path, reference: str, variant: str, sources: dict) -> tuple[str, ...]:
    return verify_inputs(source, reference, variant, manager_inputs(source, sources))


def verify_inputs(source: Path, reference: str, variant: str, selected: dict) -> tuple[str, ...]:
    checked = set()
    for target, inputs in sorted(selected.items()):
        identity = tuple(hashlib.sha256(value).digest() for value in inputs.values())
        if identity in checked:
            continue
        try:
            with temporary_directory(source.parent, '.s24-') as work:
                manager = work / 'KernelSU'
                git(work, 'clone', '--quiet', '--shared', '--no-checkout', str(source), str(manager))
                git(manager, 'checkout', '--quiet', '--detach', reference)
                repository = manager_profile(variant)['repository']
                git(manager, 'remote', 'set-url', 'origin', f'https://github.com/{repository}.git')
                dependency = work / 'susfs'
                for name, data in inputs.items():
                    output = dependency / name
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(data)
                if variant in ('SukiSU', 'BakaSU'):
                    if variant == 'BakaSU':
                        from restore_bakasu_kpm import restore_bakasu_kpm
                        restore_bakasu_kpm(manager, reference)
                    from suki_baka_susfs24 import integrate
                    integrate(manager, dependency, variant)
                else:
                    from additional_manager_kpm import port
                    from additional_manager_integration import integrate_susfs
                    port(manager, reference, variant)
                    integrate_susfs(work, dependency, variant)
        except (OSError, ValueError, RuntimeError) as error:
            raise SnapshotError(f'{variant} {target} {SUSFS_24} 配套源码验证失败：{error}') from error
        checked.add(identity)
    return tuple(sorted(selected))
