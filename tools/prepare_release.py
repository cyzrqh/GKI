#!/usr/bin/env python3
"""Prepare native assets and Release or Actions notes from selected artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import zipfile

from embedded_build_metadata import APK_MEMBER, KERNEL_MEMBER, metadata_json, read_manager_apk
from package_release_kernel import AK3_NAME, read_kernel_ak3
from release_notes import asset_names, render_notes
from manager_variants import MANAGERS
from kernel_order import kernel_key, natural_key


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise ValueError('Missing nonempty release asset: ' + path.name)


def embedded(path: Path, member: str) -> bool:
    """Legacy sidecars remain usable, but never downgrade an embedded identity."""
    try:
        with zipfile.ZipFile(path) as archive:
            return member in archive.namelist()
    except zipfile.BadZipFile:
        return False


def combine_sidecar(legacy: dict, actual: dict) -> dict:
    numeric = {'ksu_version_code', 'manager_version_code', 'manager_cert_size', 'sub_level'}
    for key in legacy.keys() & actual.keys():
        before, after = legacy[key], actual[key]
        if key in numeric:
            before, after = str(before), str(after)
        elif key == 'files' and isinstance(before, list) and isinstance(after, list):
            before, after = sorted(before), sorted(after)
        if before != after:
            raise ValueError('Legacy sidecar differs from the embedded release identity: ' + key)
    return {**legacy, **actual}


def validate_bindings(manager: dict | None, kernels: list[tuple[dict, bool]], *, manager_verified: bool) -> None:
    targets = []
    for document, _ in kernels:
        match = AK3_NAME.fullmatch(document.get('file', ''))
        targets.append(tuple(match.groups()) if match else tuple(str(document.get(key, ''))
                       for key in ('android_version', 'kernel_version', 'sub_level', 'os_patch_level')))
    if len(targets) != len(set(targets)):
        raise ValueError('Duplicate kernel build target in the selected release assets')
    paired = [(document, raw) for document, raw in kernels if document.get('ksu_variant') in MANAGERS]
    variants = {document['ksu_variant'] for document, _ in paired}
    if manager is not None:
        manager_variant = manager.get('ksu_variant', 'SukiSU')
        if manager_variant not in MANAGERS:
            raise ValueError('Invalid paired manager variant')
        variants.add(manager_variant)
        if any(raw and document.get('ksu_variant') != manager_variant for document, raw in kernels):
            raise ValueError('The release manager and kernels use different KernelSU variants')
    if len(variants) > 1:
        raise ValueError('The release manager and kernels use different KernelSU variants')
    paired_variant = next(iter(variants), 'SukiSU')
    for key in ('ksu_ref', 'ksu_version_code', 'ksu_version_name'):
        values = {str(document[key]) for document, _ in paired if document.get(key) is not None}
        if manager and manager.get(key) is not None:
            values.add(str(manager[key]))
        if len(values) > 1:
            raise ValueError('The release manager and kernels use different ' + paired_variant + ' ' + key)
    for document, raw in paired:
        if raw and (manager is None or not manager_verified):
            raise ValueError('A raw ' + document['ksu_variant'] + ' kernel requires its verified signed manager APK')
        if manager is None:
            continue  # Older sidecar-only kernel recovery did not record APK identity.
        bindings = {
            'manager_package': manager.get('package', manager.get('manager_package')),
            'manager_cert_size': manager.get('manager_cert_size'),
            'manager_cert_sha256': manager.get('manager_cert_sha256'),
            'manager_apk_filename': manager.get('file', manager.get('filename')),
            'manager_apk_sha256': manager.get('apk_sha256'),
        }
        for key, expected in bindings.items():
            actual = document.get(key)
            if raw and (actual is None or expected is None):
                raise ValueError('The raw kernel lacks a verified manager binding: ' + key)
            if actual is not None and expected is not None and str(actual) != str(expected):
                raise ValueError('The release kernel does not match the signed manager APK: ' + key)


def release_title(manager: dict | None, *, platform: str = 'Android') -> str:
    """Name the release from its actual manager version code and resolved tier."""
    if platform not in ('Android', 'OnePlus'):
        raise ValueError('Release platform must be Android or OnePlus')
    if manager is None:
        return platform + ' Kernel Release'
    variant = manager.get('ksu_variant', 'SukiSU')
    version = manager.get('ksu_version_code')
    if (type(version) not in (str, int) or not re.fullmatch(r'[1-9][0-9]*', str(version))
            or int(version) > 2_100_000_000):
        raise ValueError('A public release title requires the actual numeric manager version code')
    branch = manager.get('ksu_branch') or '版本类型未知'
    label = {'Stable(标准)': '正式版', 'Latest(最新)': '最新测试版',
             'Dev(开发)': '开发版', 'Custom(自定义)': '自定义版'}.get(branch, branch)
    return f'{platform} · {variant} {version} {label}'


def prepare(source: Path, output: Path, *, expected_kernel_count: int | None = None,
            report_kind: str = 'release') -> dict:
    if report_kind not in ('release', 'actions'):
        raise ValueError("Report kind must be 'release' or 'actions'")
    if expected_kernel_count is not None and (type(expected_kernel_count) is not int or expected_kernel_count < 0):
        raise ValueError('Expected kernel count must be a nonnegative integer')
    source, output = Path(source), Path(output)
    if source.is_symlink() or not source.is_dir() or output.is_symlink():
        raise ValueError('Expected an input directory and an unlinked release output')
    paths = sorted(source.rglob('*'))
    if len(paths) > 10000 or any(path.is_symlink() for path in paths):
        raise ValueError('Unexpectedly many or linked release input paths')
    files = [path for path in paths if path.is_file()]
    managers = [path for path in files if path.name == 'manager.json']
    kernels = [path for path in files if path.name == 'kernel.json']
    if len(managers) > 1:
        raise ValueError('Expected at most one manager and at least one release artifact')
    catalog = {}
    for path in files:
        if path.suffix.lower() in {'.apk', '.zip', '.img'}:
            catalog.setdefault(path.name, []).append(path)

    def resolve(name: str) -> Path:
        candidates = catalog.get(name, [])
        if not candidates:
            raise ValueError('Missing nonempty release asset: ' + name)
        if len(candidates) != 1:
            raise ValueError('Duplicate public release filenames: ' + name)
        regular(candidates[0])
        return candidates[0]

    def sidecar(path: Path) -> dict:
        regular(path)
        if path.stat().st_size > 64 * 1024:
            raise ValueError('Release sidecar metadata is too large')
        return metadata_json(path.read_bytes())

    manager, manager_verified = None, False
    kernel_records = []
    entries, claimed = [], set()
    for path in managers:
        document = sidecar(path)
        names = asset_names(document, manager=True)
        if len(names) != 1:
            raise ValueError('Expected at most one manager APK')
        apk = resolve(names[0])
        if embedded(apk, APK_MEMBER):
            document = combine_sidecar(document, read_manager_apk(apk))
            manager_verified = True
        elif document.get('apk_sha256') is not None and document['apk_sha256'] != digest(apk):
            raise ValueError('Legacy manager APK SHA256 does not match its sidecar')
        actual_sha256 = digest(apk)
        if manager_verified and document['apk_sha256'] != actual_sha256:
            raise ValueError('The signed manager APK changed while preparing its release')
        document = {**document, 'apk_sha256': actual_sha256}
        manager = document
        entries.append(apk)
        claimed.add(apk)
    for path in kernels:
        document = sidecar(path)
        names = asset_names(document)
        main = document.get('file', document.get('filename'))
        if not isinstance(main, str) or main not in names or not main.endswith('.zip'):
            raise ValueError('Kernel sidecar requires its primary AnyKernel3 ZIP filename')
        ak3 = resolve(main)
        verified = embedded(ak3, KERNEL_MEMBER)
        if verified:
            document = combine_sidecar(document, read_kernel_ak3(ak3))
            names = asset_names(document)
        selected = [resolve(name) for name in names]
        entries.extend(selected)
        claimed.update(selected)
        kernel_records.append((document, verified))
    for path in files:
        if path in claimed:
            continue
        if path.suffix.lower() == '.apk':
            if manager is not None:
                raise ValueError('Expected at most one manager APK')
            manager = read_manager_apk(path)
            manager_verified = True
            entries.append(path)
            claimed.add(path)
        elif path.name.endswith('-AnyKernel3.zip'):
            document = read_kernel_ak3(path)
            selected = [resolve(name) for name in asset_names(document)]
            entries.extend(selected)
            claimed.update(selected)
            kernel_records.append((document, True))
    if expected_kernel_count is not None and len(kernel_records) != expected_kernel_count:
        raise ValueError(f'Expected {expected_kernel_count} kernel builds, found {len(kernel_records)}; '
                         'refusing an incomplete Release. Preserve the original AnyKernel3 ZIP files.')
    if manager is None and not kernel_records:
        raise ValueError('Expected at most one manager and at least one release artifact')
    unclaimed_boot = [path.name for path in files if path.suffix.lower() == '.img' and path not in claimed]
    if unclaimed_boot:
        raise ValueError('Boot image has no matching kernel metadata: ' + ', '.join(unclaimed_boot))
    for document, raw in kernel_records:
        boot_names = {name for name in asset_names(document) if name.endswith('.img')}
        hashes = document.get('boot_sha256')
        if raw or hashes is not None:
            if not isinstance(hashes, dict) or set(hashes) != boot_names:
                raise ValueError('Kernel boot image SHA256 metadata does not match its selected files')
            for name, expected in hashes.items():
                if (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
                        or digest(resolve(name)) != expected):
                    raise ValueError('Boot image SHA256 does not match its kernel metadata: ' + name)
    validate_bindings(manager, kernel_records, manager_verified=manager_verified)
    kernel_documents = sorted((document for document, _ in kernel_records), key=kernel_key)
    entries.sort(key=lambda path: natural_key(path.name))
    names = [path.name for path in entries]
    if not names or len(names) != len(set(names)):
        raise ValueError('Missing or duplicate public release filenames')
    # Validate the entire input before creating a partially populated output.
    for path in entries:
        regular(path)
        destination = output / 'assets' / path.name
        if destination.exists() or destination.is_symlink():
            raise ValueError('Release output already contains an asset')
    if (output / 'assets').is_symlink():
        raise ValueError('Release output asset directory must not be linked')
    if (output / 'assets').exists() and any((output / 'assets').iterdir()):
        raise ValueError('Release output already contains an asset')
    if any((output / name).exists() or (output / name).is_symlink() for name in ('release.md', 'title.txt')):
        raise ValueError('Release output already contains release metadata')
    expected_hashes = {path.name: digest(path) for path in entries}
    if manager_verified and expected_hashes[manager['file']] != manager['apk_sha256']:
        raise ValueError('The signed manager APK changed while preparing its release')
    body = render_notes(manager, kernel_documents, report_kind=report_kind)
    variant = manager.get('ksu_variant', 'SukiSU') if manager else None
    if report_kind == 'actions':
        title = ('GKI 构建结果 · ' + variant + ' ' + str(manager.get('ksu_version_name') or manager.get('ksu_version_code'))
                 if manager else 'GKI 构建结果')
    else:
        title = release_title(manager)
    assets = output / 'assets'
    assets.mkdir(parents=True, exist_ok=True)
    checksums = []
    for path in entries:
        destination = assets / path.name
        shutil.copyfile(path, destination)
        actual_hash = digest(destination)
        if actual_hash != expected_hashes[path.name]:
            raise ValueError('A release asset changed while copying: ' + path.name)
        checksums.append(actual_hash + '  ' + path.name)
    (assets / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', encoding='utf-8')
    (output / 'release.md').write_text(body, encoding='utf-8')
    (output / 'title.txt').write_text(title, encoding='utf-8')
    return {'assets': len(entries), 'kernels': len(kernel_documents), 'manager': bool(manager)}


def nonnegative_integer(value: str) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('Expected a nonnegative integer') from None
    if count < 0:
        raise argparse.ArgumentTypeError('Expected a nonnegative integer')
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report-kind', choices=('release', 'actions'), default='release')
    parser.add_argument('--expected-kernel-count', type=nonnegative_integer,
                        help='Require this many unique kernel builds before preparing any public files')
    args = parser.parse_args(argv)
    try:
        result = prepare(args.input, args.output, expected_kernel_count=args.expected_kernel_count,
                         report_kind=args.report_kind)
    except (OSError, ValueError) as error:
        print(f'Release preparation failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
