"""Preserve the selected official APK's ABI set and native asset inventory."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import zipfile

from manager_lkm_jobs import KMIS
from manager_variants import MANAGERS, selected_variant
from prepare_manager_lkm import public_identity, locked_ddk, read_json, selected_source
from snapshot_ksu_source import git

ABIS = {'arm64-v8a': ('aarch64', 'aarch64-linux-android', 183),
        'armeabi-v7a': ('arm', 'armv7-linux-androideabi', 40),
        'x86_64': ('x86_64', 'x86_64-linux-android', 62),
        'riscv64': ('riscv64', 'riscv64-linux-android', 243)}


def official_abis(source: Path) -> tuple[str, ...]:
    """Read frozen upstream declarations, never our rewritten Gradle worktree."""
    values = set()
    for name in ('manager/build.gradle.kts', 'manager/app/build.gradle.kts'):
        original = git(source, 'show', 'HEAD:' + name)
        for body in re.findall(r'\babiFilters\s*(?:\+=\s*listOf)?\s*\(([^)]*)\)', original):
            items = re.findall(r'"([A-Za-z0-9_-]+)"', body)
            if re.sub(r'"[A-Za-z0-9_-]+"', '', body).strip(' ,\r\n\t'):
                raise ValueError('Review the changed official ABI declaration')
            values.update(items)
    if 'arm64-v8a' not in values or not values <= ABIS.keys():
        raise ValueError('Official APK ABI set is missing or has a newly unsupported architecture')
    return tuple(abi for abi in ABIS if abi in values)


def has_guard(source: Path) -> bool:
    return bool(git(source, 'ls-tree', 'HEAD', '--', 'kernel/susfs/Makefile'))


def original_assets(source: Path, arch: str) -> dict[str, bytes]:
    prefix = 'userspace/ksud/bin/' + arch + '/'
    entries = git(source, 'ls-tree', '-r', 'HEAD', '--', prefix).splitlines()
    result = {}
    for row in entries:
        mode, _, _ = row.split('\t', 1)[0].split()
        name = row.split('\t', 1)[1]
        if mode not in ('100644', '100755'):
            raise ValueError('Official native asset must be a regular Git blob')
        if Path(name).name.startswith('.'):
            continue
        result[name] = subprocess.check_output(['git', '-C', str(source), 'show', 'HEAD:' + name])
    return result


def check_original_assets(source: Path) -> None:
    for abi in official_abis(source):
        for name, raw in original_assets(source, ABIS[abi][0]).items():
            path = source / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
                raise ValueError('An existing official native asset was removed or changed: ' + name)


def extra_components(source: Path, kmi: str) -> tuple[tuple[str, str, str], ...]:
    result = []
    if 'x86_64' in official_abis(source):
        result.append(('x86_64', 'kernelsu', kmi + '_kernelsu.ko'))
    if has_guard(source):
        result.append(('aarch64', 'susfs_guard_lkm', kmi + '_susfs_guard_lkm.ko'))
    return tuple(result)


def inspect_extra(path: Path, arch: str, kind: str, kmi: str, reference: str, identity: dict) -> dict:
    if path.is_symlink() or not path.is_file() or not 64 <= path.stat().st_size <= 64 * 1024 * 1024:
        raise ValueError('Missing or invalid full native module')
    raw = path.read_bytes()
    if raw[:7] != b'\x7fELF\x02\x01\x01':
        raise ValueError('Native component is not a little-endian ELF64 module')
    header = struct.unpack_from('<HHIQQQIHHHHHH', raw, 16)
    machine = 62 if arch == 'x86_64' else 183
    offset, width, count, strings_index = header[5], header[10], header[11], header[12]
    if (header[:3] != (1, machine, 1) or width != 64 or not 0 < strings_index < count <= 65535
            or offset < 64 or offset + width * count > len(raw)):
        raise ValueError('Full native component has an unexpected architecture or section table')
    sections = [struct.unpack_from('<IIQQQQIIQQ', raw, offset + n * width) for n in range(count)]
    def data(section):
        start, size = section[4], section[5]
        if start > len(raw) or size > len(raw) - start:
            raise ValueError('Full native component has an invalid ELF section')
        return raw[start:start + size]
    names = data(sections[strings_index])
    infos, allocated = [], []
    for section in sections:
        end = names.find(b'\0', section[0])
        if end < 0:
            raise ValueError('Full native component has an invalid section name')
        if section[1] == 8:
            continue
        content = data(section)
        if names[section[0]:end] == b'.modinfo':
            infos.append(content)
        if section[2] & 2:
            allocated.append(content)
    if len(infos) != 1:
        raise ValueError('Full native component lacks one real module-info section')
    info = {}
    for item in infos[0].split(b'\0'):
        if b'=' in item:
            key, value = item.split(b'=', 1)
            info.setdefault(key, []).append(value)
    refs = info.get(b'gki_extra_source_revision', [])
    if (info.get(b'name') != [kind.encode()] or not refs
            or any(value != reference.encode() for value in refs)
            or len(info.get(b'vermagic', [])) != 1):
        raise ValueError('Full native module does not match the selected complete source revision')
    release = info[b'vermagic'][0].decode('ascii').split(' ', 1)[0]
    if not re.fullmatch(re.escape(kmi.split('-', 1)[1]) + r'\.[0-9]+[A-Za-z0-9_.+-]*', release):
        raise ValueError('Full native module does not match the selected KMI')
    if kind == 'kernelsu':
        for value in (identity['manager_package'], identity['manager_cert_sha256']):
            if not any(str(value).encode() in section for section in allocated):
                raise ValueError('Full native module is not bound to this APK package and signing certificate')
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
            'vermagic': info[b'vermagic'][0].decode('ascii'), 'arch': arch, 'kind': kind}


def record_extras(source: Path, directory: Path, kmi: str, identity_path: Path, reference: str,
                  image: str) -> dict:
    locked_ddk(kmi, image)
    identity = public_identity(identity_path)
    provenance = selected_source(source, reference)
    rows = []
    for arch, kind, name in extra_components(source, kmi):
        path = directory / arch / name
        rows.append({'file': arch + '/' + name,
                     **inspect_extra(path, arch, kind, kmi, reference, identity)})
    document = {'schema_version': 1, 'kmi': kmi, 'ddk_image': image,
                **identity, **provenance, 'components': rows}
    (directory / 'extra.json').write_text(json.dumps(document, sort_keys=True) + '\n', encoding='utf-8')
    return document


def stage_extras(source: Path, artifacts: Path, identity_path: Path, reference: str,
                 kmis: tuple[str, ...] = KMIS) -> list[dict]:
    identity = public_identity(identity_path)
    provenance = selected_source(source, reference)
    pending, receipts = [], []
    for kmi in kmis:
        directory = artifacts / kmi / 'extras'
        document = read_json(directory / 'extra.json')
        required = extra_components(source, kmi)
        if (document.get('schema_version') != 1 or document.get('kmi') != kmi
                or any(document.get(key) != value for key, value in {**identity, **provenance}.items())
                or len(document.get('components', [])) != len(required)):
            raise ValueError('Full native component provenance does not match this source and certificate')
        locked_ddk(kmi, document.get('ddk_image'))
        for row, (arch, kind, name) in zip(document['components'], required):
            path = directory / arch / name
            inspected = inspect_extra(path, arch, kind, kmi, reference, identity)
            if row != {'file': arch + '/' + name, **inspected}:
                raise ValueError('Full native component changed after its actual build')
            destination = source / 'userspace/ksud/bin' / arch / name
            if destination.is_symlink() or any(p.is_symlink() for p in destination.parents if p.is_relative_to(source)):
                raise ValueError('Refusing a linked native asset destination')
            pending.append((destination, path.read_bytes()))
        receipts.append(document)
    # Validate every KMI before publishing the first asset.
    for path, raw in pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    check_original_assets(source)
    return receipts


def verify_apk(source: Path, apk: Path) -> dict:
    expected = official_abis(source)
    with zipfile.ZipFile(apk) as archive:
        if archive.testzip() is not None:
            raise ValueError('Full native APK is corrupt')
        names = set(archive.namelist())
        actual = {name.split('/')[1] for name in names if name.startswith('lib/') and name.count('/') >= 2}
        if actual != set(expected):
            raise ValueError('APK architecture set differs from the selected official source: ' + str(sorted(actual)))
        records = []
        for abi in expected:
            arch, _, machine = ABIS[abi]
            for name in ('libksud.so', 'libkernelsu.so'):
                raw = archive.read('lib/' + abi + '/' + name)
                elf_class = 1 if arch == 'arm' else 2
                if (len(raw) < (52 if elf_class == 1 else 64)
                        or raw[:7] != b'\x7fELF' + bytes((elf_class, 1, 1))
                        or struct.unpack_from('<H', raw, 16)[0] not in (2, 3)
                        or struct.unpack_from('<H', raw, 18)[0] != machine):
                    raise ValueError('APK is missing an executable native component for ' + abi)
            daemon = archive.read('lib/' + abi + '/libksud.so')
            assets = source / 'userspace/ksud/bin' / arch
            for path in assets.rglob('*'):
                if path.is_file() and not path.name.startswith('.') and path.name.encode() not in daemon:
                    raise ValueError('Packaged daemon lost an official or paired native asset: ' + abi + '/' + path.name)
            # Every native JNI input that upstream builds/copies must survive
            # packaging, including FinaSU's original extra engines.
            jni = source / 'manager/app/src/main/jniLibs' / abi
            for path in jni.rglob('*.so'):
                member = 'lib/' + abi + '/' + path.name
                if member not in names:
                    raise ValueError('APK removed an original JNI component: ' + member)
            records.append({'abi': abi, 'daemon_sha256': hashlib.sha256(daemon).hexdigest()})
    check_original_assets(source)
    return {'abis': list(expected), 'native_components': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('abis', 'record', 'stage', 'verify-apk'))
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--identity', type=Path)
    parser.add_argument('--expected-ref')
    parser.add_argument('--kmi', choices=KMIS)
    parser.add_argument('--ddk-image')
    parser.add_argument('--apk', type=Path)
    args = parser.parse_args()
    if args.operation == 'abis':
        print(' '.join(official_abis(args.source)))
    elif args.operation == 'record':
        print(json.dumps(record_extras(args.source, args.directory, args.kmi, args.identity,
                                     args.expected_ref, args.ddk_image)))
    elif args.operation == 'stage':
        rows = stage_extras(args.source, args.directory, args.identity, args.expected_ref,
                            (args.kmi,) if args.kmi else KMIS)
        print('Validated complete official native components for ' + str(len(rows)) + ' KMIs')
    else:
        print(json.dumps(verify_apk(args.source, args.apk)))


if __name__ == '__main__':
    main()
