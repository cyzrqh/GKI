"""Add the reviewed ABI-2 KPM backend to the four source-built managers.

The existing SukiSU and BakaSU ports are deliberately not changed. Unknown
upstream layouts fail before building; enabling a Kconfig symbol alone is never
accepted as evidence that the loader or management interface exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from manager_variants import ADDITIONAL_MANAGERS, manager_profile
from restore_bakasu_kpm import load_port_manifest

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = '.gki-additional-kpm.json'
FEATURE_ID = 10004


def git(source: Path, *args: str) -> bytes:
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(source), *args],
                            capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError('Cannot inspect the fixed additional-manager source: ' + args[0])
    return result.stdout


def replace_one(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError('Review changed upstream KPM integration anchor: ' + old[:100])
    return text.replace(old, new, 1)


def template_files() -> dict[str, str]:
    # This verifies the original reviewed size, digest and full file inventory.
    _, patch = load_port_manifest()
    result = {}
    for section in re.split(r'(?=^diff --git )', patch.decode(), flags=re.M):
        match = re.match(r'diff --git a/(\S+) b/\1\n', section)
        if match and '\nnew file mode ' in section:
            result[match[1]] = ''.join(line[1:] + '\n' for line in section.splitlines()
                                      if line.startswith('+') and not line.startswith('+++'))
    return result


def source_plan(source: Path, reference: str, variant: str, *, composed: bool = False) -> dict[str, bytes]:
    if variant not in ADDITIONAL_MANAGERS or not re.fullmatch('[0-9a-f]{40}', reference):
        raise ValueError('KPM source port requires one fixed additional manager')
    if git(source, 'rev-parse', 'HEAD').decode().strip() != reference:
        raise ValueError('KPM source HEAD differs from the selected upstream')
    origin = git(source, 'remote', 'get-url', 'origin').decode().strip().removesuffix('.git')
    if origin != 'https://github.com/' + manager_profile(variant)['repository']:
        raise ValueError('KPM source is not the selected official repository')
    tree = git(source, 'ls-tree', '-r', '--name-only', reference).decode().splitlines()
    originals = {}
    plan = {}

    def read(name):
        if name not in tree:
            raise ValueError('Missing native source input: ' + name)
        if name not in originals:
            path = source / name
            if composed and name.startswith(('kernel/', 'uapi/')):
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Composed KPM input must be a regular source file')
                data = path.read_bytes()
            else:
                data = git(source, 'show', reference + ':' + name)
            if len(data) > 4 * 1024 * 1024 or b'\0' in data:
                raise ValueError('Invalid bounded native source input')
            originals[name] = data.decode('utf-8')
        return plan.get(name, originals[name].encode()).decode()

    def write(name, text):
        if Path(name).is_absolute() or any(p in ('..', '.git') for p in Path(name).parts):
            raise ValueError('Unsafe source-port destination')
        plan[name] = text.encode()

    templates = template_files()
    for name in ('kernel/kpm/kpm.c', 'kernel/kpm/kpm.h', 'kernel/kpm/bridge.h', 'uapi/kpm.h'):
        if name in tree:
            raise ValueError('An upstream KPM implementation now exists; review it before porting')
        write(name, templates[name].replace('GKI_BAKASU_KPM_PORT_V1', 'GKI_ADDITIONAL_KPM_V1'))

    name = 'kernel/Kbuild'
    write(name, replace_one(read(name), 'kernelsu-objs += policy/feature.o',
        'kernelsu-objs += policy/feature.o\n'
        'ifeq ($(CONFIG_KPM),y)\nkernelsu-objs += kpm/kpm.o\n'
        'ifdef KBUILD_EXTMOD\nccflags-y += -DCONFIG_KPM=1\nendif\nendif'))
    name = 'kernel/Kconfig'
    text = read(name)
    if re.search(r'^config KPM\b', text, re.M):
        raise ValueError('Upstream KPM Kconfig requires review')
    # Insert in the outer menu, without moving any native feature definitions.
    anchor = text.rfind('\nendmenu')
    if anchor < 0:
        raise ValueError('Missing native outer KernelSU menu')
    config = ('\nconfig KPM\n\tbool "KPM (verified KernelPatch ABI-2 bridge)"\n'
              '\tdepends on KSU && ARM64 && 64BIT\n\tdefault n\n'
              '\tselect KALLSYMS\n\tselect KALLSYMS_ALL\n'
              '\thelp\n\t  Enable the manager/root-authorized KPM interface.\n'
              '\t  Requires the matching verified payload in the built kernel.\n')
    write(name, text[:anchor] + config + text[anchor:])
    name = 'kernel/core/init.c'
    text = replace_one(read(name), '#include "policy/feature.h"',
                       '#include "policy/feature.h"\n#include "kpm/kpm.h"')
    text = replace_one(text, 'ksu_feature_init();', 'ksu_feature_init();\n    ksu_kpm_init();')
    text = replace_one(text, 'ksu_feature_exit();', 'ksu_kpm_exit();\n    ksu_feature_exit();')
    write(name, text)
    name = 'uapi/feature.h'
    text = read(name)
    if re.search(r'\bKSU_FEATURE_KPM\b|=\s*10004\b', text):
        raise ValueError('Native KPM feature slot is occupied; upstream review required')
    write(name, replace_one(text, '    KSU_FEATURE_MAX',
                            f'    KSU_FEATURE_KPM = {FEATURE_ID},\n    KSU_FEATURE_MAX'))
    name = 'kernel/policy/feature.c'
    text = read(name)
    match = re.search(r'(static const char \*const feature_names\[KSU_FEATURE_MAX\] = \{)', text)
    if not match:
        raise ValueError('Native feature registry layout changed')
    write(name, replace_one(text, match[0], match[0] + '\n    [KSU_FEATURE_KPM] = "kpm",'))
    name = 'uapi/supercall.h'
    text = read(name)
    if re.search(r"['\"]K['\"],\s*201\b|\bKSU_IOCTL_KPM\b", text):
        raise ValueError('Native ioctl 201 is occupied; review required')
    text = replace_one(text, '#include "uapi/app_profile.h"',
                       '#include "uapi/app_profile.h"\n#include "uapi/kpm.h"')
    if '#undef DEFINE_KSU_UAPI_CONST' in text:
        text = replace_one(text, '#undef DEFINE_KSU_UAPI_CONST',
            "DEFINE_KSU_UAPI_CONST(__u32, KSU_IOCTL_KPM, _IOWR('K', 201, struct ksu_kpm_cmd))\n"
            '#undef DEFINE_KSU_UAPI_CONST')
    else:
        anchor = text.rfind('\n#endif')
        if anchor < 0:
            raise ValueError('Native supercall header guard changed')
        text = text[:anchor] + "\nstatic const __u32 KSU_IOCTL_KPM = _IOWR('K', 201, struct ksu_kpm_cmd);\n" + text[anchor:]
    write(name, text)
    name = 'kernel/supercall/dispatch.c'
    text = replace_one(read(name), '#include "policy/feature.h"',
                       '#include "policy/feature.h"\n#include "kpm/kpm.h"')
    text, count = re.subn(r'(static const struct ksu_ioctl_cmd_map ksu_ioctl_handlers\[\] = \{)',
        r'\1\n    { .cmd = KSU_IOCTL_KPM, .name = "KPM", .handler = do_kpm, .perm_check = manager_or_root },', text)
    if count != 1:
        raise ValueError('Native authorized ioctl dispatcher changed')
    write(name, text)

    # All four official daemons use the flat Android source layout. Preserve
    # their native install path and boot stages; add only the reviewed KPM CLI.
    rust = 'userspace/ksud/src/'
    for name in ('kpm.rs', 'kpm_store.rs'):
        if rust + name in tree:
            raise ValueError('Upstream daemon now has KPM; review before porting')
        text = templates['userspace/ksud/src/android/' + name]
        text = text.replace('use super::{kpm_store, ksucalls, uapi};',
                            'use super::{kpm_store, ksucalls, ksu_uapi as uapi};')
        text = text.replace('uapi::KSU_IOCTL_KPM_RUST', 'uapi::KSU_IOCTL_KPM')
        if name == 'kpm.rs':
            utils = read('userspace/ksud/src/utils.rs')
            signature = re.search(r'pub fn install\(([^)]*)\)', utils)
            if not signature:
                raise ValueError('Native boot daemon installation API changed')
            if signature[1].count('Option<PathBuf>') == 1:
                text = replace_one(text, 'super::utils::install(None, None)?;', 'super::utils::install(None)?;')
            elif signature[1].count('Option<PathBuf>') != 2:
                raise ValueError('Review native daemon install arguments')
        write(rust + name, text)
    name = rust + 'main.rs'
    write(name, replace_one(read(name), 'mod ksucalls;', 'mod ksucalls;\n'
        '#[cfg(all(target_os = "android", target_arch = "aarch64"))]\nmod kpm;\n'
        '#[cfg(all(target_os = "android", target_arch = "aarch64"))]\nmod kpm_store;'))
    name = rust + 'ksucalls.rs'
    text = read(name)
    if 'DRIVER_FD.get_or_init(|| init_driver_fd().unwrap_or(-1))' not in text:
        raise ValueError('Native driver FD initialization changed')
    write(name, text + '\n#[cfg(target_arch = "aarch64")]\n'
        'pub(crate) fn kpm_io(cmd: &mut crate::ksu_uapi::ksu_kpm_cmd) -> Result<i32> {\n'
        '    let fd = *DRIVER_FD.get_or_init(|| init_driver_fd().unwrap_or(-1));\n'
        '    anyhow::ensure!(fd >= 0, "Cannot obtain the authorized KernelSU driver");\n'
        '    let result = unsafe { libc::ioctl(fd, crate::ksu_uapi::KSU_IOCTL_KPM as i32, std::ptr::from_mut(cmd)) };\n'
        '    if result < 0 { return Err(std::io::Error::last_os_error().into()); }\n'
        '    Ok(result)\n}\n')
    name = rust + 'cli.rs'
    text = replace_one(read(name), 'enum Commands {', 'enum Commands {\n'
        '    /// Manage KPM modules through the verified ABI-2 bridge\n'
        '    #[cfg(target_arch = "aarch64")]\n    Kpm(crate::kpm::KpmArgs),\n')
    # Insert into the native top-level match, never a nested Debug match.
    matches = list(re.finditer(r'match (?:args|cli)\.command\s*\{', text))
    if len(matches) != 1:
        raise ValueError('Native top-level daemon CLI dispatcher changed')
    write(name, replace_one(text, matches[0][0], matches[0][0] + '\n'
        '        #[cfg(target_arch = "aarch64")]\n'
        '        Commands::Kpm(arguments) => crate::kpm::run(arguments),'))
    name = rust + 'init_event.rs'
    text = read(name)
    anchor = '    // execute metamodule post-fs-data script first (priority)'
    boot = ('    #[cfg(target_arch = "aarch64")]\n'
            '    if !is_safe_mode() {\n'
            '        if let Err(error) = crate::kpm::booted_load() {\n'
            '            warn!("KPM: saved modules could not load: {error}");\n'
            '        }\n    }\n\n')
    write(name, replace_one(text, anchor, boot + anchor))
    from additional_manager_kpm_ui import manager_ui_plan
    plan.update(manager_ui_plan(source, reference, variant))
    return plan


def port(source: Path, reference: str, variant: str) -> dict:
    source = source.resolve(strict=True)
    plan = source_plan(source, reference, variant)
    inventory = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(plan.items())}
    for name, data in plan.items():
        path = source / name
        if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(source)):
            raise ValueError('Refusing linked KPM source output')
        original = subprocess.run(['git', '-C', str(source), 'show', reference + ':' + name], capture_output=True)
        expected = original.stdout if original.returncode == 0 else None
        if path.exists() and path.read_bytes() not in (expected, data):
            raise ValueError('KPM source has unrelated local edits: ' + name)
    # The whole plan validates before the first source mutation.
    for name, data in plan.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    receipt = {'schema_version': 1, 'variant': variant, 'reference': reference,
               'feature_id': FEATURE_ID, 'bridge_abi': 2, 'files': inventory}
    (source / RECEIPT).write_text(json.dumps(receipt, sort_keys=True) + '\n', encoding='utf-8')
    return receipt


def verify(source: Path, reference: str, variant: str, *, scopes=None) -> dict:
    plan = source_plan(source, reference, variant)
    for name, expected in plan.items():
        if scopes and not any(name == s or name.startswith(s.rstrip('/') + '/') for s in scopes):
            continue
        path = source / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            raise ValueError('Incomplete or changed KPM source port: ' + name)
    inventory = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(plan.items())
                 if not scopes or any(name == s or name.startswith(s.rstrip('/') + '/') for s in scopes)}
    return {'sha256': hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(),
            'files': inventory}


def with_susfs(source: Path, variant: str, apply_susfs) -> None:
    """Compose two reviewed source ports without accepting an ambiguous merge.

    The KPM interface is additive, so remove only its exactly verified kernel
    changes, apply the original SUSFS port, then add KPM to that resulting tree.
    Any failure restores the entire previous kernel/uapi source tree in place.
    """
    reference = git(source, 'rev-parse', 'HEAD').decode().strip()
    plan = source_plan(source, reference, variant)
    selected = {name: data for name, data in plan.items() if name.startswith(('kernel/', 'uapi/'))}
    verify(source, reference, variant, scopes=('kernel', 'uapi'))
    before = {}
    for tree in ('kernel', 'uapi'):
        for path in (source / tree).rglob('*'):
            if path.is_symlink():
                raise ValueError('Refusing linked source while composing native ports')
            if path.is_file():
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError('Unexpectedly large native source input')
                before[str(path.relative_to(source)).replace('\\', '/')] = path.read_bytes()
    try:
        for name in selected:
            original = subprocess.run(['git', '-C', str(source), 'show', reference + ':' + name], capture_output=True)
            path = source / name
            if original.returncode == 0:
                path.write_bytes(original.stdout)
            else:
                path.unlink()
        apply_susfs()
        merged = source_plan(source, reference, variant, composed=True)
        for name, data in merged.items():
            if name.startswith(('kernel/', 'uapi/')):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
    except BaseException:
        for tree in ('kernel', 'uapi'):
            for path in (source / tree).rglob('*'):
                if path.is_file() and str(path.relative_to(source)).replace('\\', '/') not in before:
                    path.unlink()
        for name, data in before.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--reference', required=True)
    parser.add_argument('--variant', required=True, choices=ADDITIONAL_MANAGERS)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    result = (verify if args.verify else port)(args.source, args.reference, args.variant)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
