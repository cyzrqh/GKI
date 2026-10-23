#!/usr/bin/env python3
"""Validate and collect direct AK3/boot Release assets for one compiled kernel."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import uuid
import zipfile
import zlib

from capture_build_metadata import kernel_config, state
from embedded_build_metadata import KERNEL_MEMBER, read_member, validate_members

AK3_NAME = re.compile(
    r'(?P<android>android[0-9]+)-(?P<series>[0-9]+\.[0-9]+)\.(?P<sublevel>[0-9]+|X)-'
    r'(?P<patch>[0-9]{4}-(?:0[1-9]|1[0-2])|lts)-AnyKernel3\.zip\Z')
SHA256 = re.compile(r'[0-9a-fA-F]{64}\Z')
SOURCE_REF = re.compile(r'[0-9a-f]{40}\Z')
MAX_AK3_BYTES = 512 * 1024 * 1024
BOOT_SUFFIXES = ('-boot.img', '-boot-gz.img', '-boot-lz4.img')
MODES = ('仅 AK3', 'AK3 + boot 镜像')
REQUIRED = ('Image', 'anykernel.sh', 'META-INF/com/google/android/update-binary')


class PackagingError(ValueError):
    """A kernel asset is incomplete, mismatched, or unsafe to publish."""


def _positive(value, maximum=2**63 - 1) -> bool:
    return (type(value) in (str, int) and re.fullmatch(r'[1-9][0-9]*', str(value)) is not None
            and int(value) <= maximum)


def validate_kernel_document(metadata: dict) -> None:
    """Validate public identity independently of any sidecar or artifact wrapper."""
    name = metadata.get('file')
    match = AK3_NAME.fullmatch(name) if isinstance(name, str) else None
    if (type(metadata.get('schema_version')) is not int or metadata['schema_version'] != 1
            or match is None or any(key in metadata for key in ('sha256', 'ak3_sha256'))):
        raise PackagingError('Invalid or self-referential embedded kernel identity')
    for key, group in (('android_version', 'android'), ('kernel_version', 'series'),
                       ('sub_level', 'sublevel'), ('os_patch_level', 'patch')):
        if type(metadata.get(key)) not in (str, int) or str(metadata[key]) != match[group]:
            raise PackagingError('Embedded kernel target differs from its public filename')
    for field in ('image_sha256', 'config_sha256'):
        if not isinstance(metadata.get(field), str) or not re.fullmatch(r'[0-9a-f]{64}', metadata[field]):
            raise PackagingError('Embedded kernel lacks a captured ' + field)
    if metadata.get('config_source') != 'Image / IKCONFIG' or not isinstance(metadata.get('features'), dict):
        raise PackagingError('Embedded kernel lacks actual Image configuration')
    timestamp = metadata.get('built_at_utc')
    try:
        parsed = dt.datetime.fromisoformat(timestamp) if isinstance(timestamp, str) and len(timestamp) <= 64 else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise PackagingError('Embedded kernel lacks its UTC build timestamp')
    variant = metadata.get('ksu_variant')
    if not isinstance(variant, str) or not variant or len(variant) > 100 or any(ord(c) < 32 for c in variant):
        raise PackagingError('Invalid embedded KernelSU variant')
    if variant != 'None':
        if not isinstance(metadata.get('ksu_ref'), str) or not SOURCE_REF.fullmatch(metadata['ksu_ref']):
            raise PackagingError('Embedded kernel lacks its immutable KernelSU source')
        branch = metadata.get('ksu_branch')
        if (not isinstance(branch, str) or not branch or len(branch) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in branch)
                or not _positive(metadata.get('ksu_version_code'), 2100000000)):
            raise PackagingError('Invalid embedded KernelSU branch or version code')
    if variant == 'SukiSU':
        package, version = metadata.get('manager_package'), metadata.get('ksu_version_name')
        if (not isinstance(package, str) or len(package) > 255
                or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', package)
                or not isinstance(version, str) or not version or len(version) > 128
                or any(not 32 <= ord(c) <= 126 for c in version)
                or not _positive(metadata.get('manager_cert_size'), 4096)
                or int(metadata['manager_cert_size']) < 64):
            raise PackagingError('Invalid embedded SukiSU manager identity')
        for field in ('manager_cert_sha256', 'manager_apk_sha256'):
            if not isinstance(metadata.get(field), str) or not re.fullmatch(r'[0-9a-f]{64}', metadata[field]):
                raise PackagingError('Invalid embedded SukiSU manager digest: ' + field)
        apk = metadata.get('manager_apk_filename')
        if (not isinstance(apk, str) or len(apk) > 250 or '..' in apk
                or not re.fullmatch(r'SukiSU_[A-Za-z0-9_.+-]+-release\.apk', apk)):
            raise PackagingError('Invalid embedded SukiSU manager APK filename')
    files, boots = metadata.get('files'), metadata.get('boot_sha256')
    prefix = name.removesuffix('-AnyKernel3.zip')
    allowed = {prefix + suffix for suffix in BOOT_SUFFIXES}
    if (not isinstance(files, list) or not files or files[0] != name
            or any(not isinstance(item, str) for item in files)
            or len(files) != len(set(files)) or not set(files[1:]) <= allowed
            or not isinstance(boots, dict) or set(boots) != set(files[1:])
            or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in boots.values())):
        raise PackagingError('Embedded kernel boot filenames and digests are inconsistent')
    for field in ('producer_run_id', 'producer_run_attempt'):
        if field in metadata and not _positive(metadata[field]):
            raise PackagingError('Invalid kernel workflow provenance: ' + field)
    if ('producer_head_sha' in metadata and (not isinstance(metadata['producer_head_sha'], str)
            or not SOURCE_REF.fullmatch(metadata['producer_head_sha']))):
        raise PackagingError('Invalid kernel workflow source hash')
    if ('producer_repository' in metadata and (not isinstance(metadata['producer_repository'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', metadata['producer_repository']))):
        raise PackagingError('Invalid kernel workflow repository')


def verify_image_metadata(metadata: dict, image: bytes) -> None:
    if hashlib.sha256(image).hexdigest() != metadata['image_sha256']:
        raise PackagingError('AnyKernel3 Image SHA256 does not match the embedded compiled kernel')
    # The Image itself is bounded above; also bound its nested gzip stream so
    # an untrusted downloaded ZIP cannot expand an IKCONFIG decompression bomb.
    start = image.find(b'IKCFG_ST')
    end = image.find(b'IKCFG_ED', start + 8) if start >= 0 else -1
    if start >= 0 and end >= 0:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            text = decoder.decompress(image[start + 8:end], 8 * 1024 * 1024 + 1)
        except zlib.error as error:
            raise PackagingError('The Image contains invalid IKCONFIG data') from error
        if len(text) > 8 * 1024 * 1024 or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise PackagingError('The Image contains an oversized or ambiguous IKCONFIG stream')
    config, origin, config_sha = kernel_config(Path('.'), image)
    if config_sha != metadata['config_sha256'] or origin != metadata['config_source']:
        raise PackagingError('Embedded kernel configuration SHA256 differs from actual IKCONFIG')
    variant, features = metadata['ksu_variant'], metadata['features']
    if (config.get('CONFIG_KSU') == 'y') != (variant != 'None'):
        raise PackagingError('Embedded KernelSU variant differs from actual IKCONFIG')
    has_backend = any(key.startswith('CONFIG_ZRAM_BACKEND_') for key in config)
    actual = {'susfs': config.get('CONFIG_KSU_SUSFS') == 'y',
              'lto': ('thin' if state(config, 'LTO_CLANG_THIN') is True else
                      'full' if state(config, 'LTO_CLANG_FULL') is True else
                      'none' if state(config, 'LTO_NONE') is True else None),
              'bbg': state(config, 'BBG'), 'zram': state(config, 'ZRAM'),
              'lz4kd': bool(state(config, 'ZRAM')) and state(config, 'ZRAM_BACKEND_LZ4KD' if has_backend else 'CRYPTO_LZ4KD'),
              'kpm': state(config, 'KPM'), 'ntsync': state(config, 'NTSYNC'),
              'rekernel': state(config, 'REKERNEL'), 'virtualization': state(config, 'KVM'),
              'ddk': state(config, 'XINGGUANG_DDK', 'DDK')}
    for field, value in actual.items():
        if field not in features or type(features[field]) is not type(value) or features[field] != value:
            raise PackagingError('Embedded kernel feature differs from actual IKCONFIG: ' + field)
    version = metadata.get('susfs_version')
    if actual['susfs']:
        if not isinstance(version, str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', version) or version.encode() not in image:
            raise PackagingError('Embedded SUSFS version is absent from the actual Image')
    elif version is not None:
        raise PackagingError('Embedded SUSFS version is set for a kernel without SUSFS')
    if variant == 'SukiSU' and any(metadata[field].encode() not in image
                                   for field in ('manager_package', 'manager_cert_sha256')):
        raise PackagingError('The actual Image does not contain its paired SukiSU manager trust identity')


def read_kernel_ak3(source: Path | bytes, *, filename: str | None = None) -> dict:
    """Read final AK3 identity, checking the exact embedded Image and IKCONFIG."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        regular_file(path)
        if path.stat().st_size > MAX_AK3_BYTES:
            raise PackagingError('Unexpected AnyKernel3 ZIP size')
        stream, filename = path, filename or path.name
    else:
        if not 0 < len(source) <= MAX_AK3_BYTES:
            raise PackagingError('Unexpected AnyKernel3 ZIP size')
        stream = io.BytesIO(source)
    try:
        with zipfile.ZipFile(stream) as archive:
            members = validate_members(archive)
            for name in REQUIRED:
                if name not in members or members[name].is_dir() or not members[name].file_size:
                    raise PackagingError('Missing required AnyKernel3 runtime asset: ' + name)
            if (members['Image'].file_size > 256 * 1024 * 1024
                    or sum(item.file_size for item in members.values()) > MAX_AK3_BYTES):
                raise PackagingError('Unexpectedly large AnyKernel3 payload')
            metadata = read_member(archive, KERNEL_MEMBER)
            validate_kernel_document(metadata)
            if filename is not None and filename != metadata['file']:
                raise PackagingError('Embedded kernel filename differs from its final artifact')
            verify_image_metadata(metadata, archive.read('Image'))
            if archive.testzip() is not None:
                raise PackagingError('AnyKernel3 ZIP CRC verification failed')
            return metadata
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise PackagingError('AnyKernel3 ZIP could not pass integrity verification') from error


def embedded_document(metadata: dict, assets: list[Path], hashes: dict[str, str]) -> dict:
    result = {**metadata, 'schema_version': 1, 'files': [asset.name for asset in assets],
              'boot_sha256': {asset.name: hashes[asset.name] for asset in assets[1:]}}
    if result.get('ksu_variant') == 'SukiSU':
        for field in ('manager_package', 'manager_cert_size', 'manager_cert_sha256',
                      'manager_apk_filename', 'manager_apk_sha256'):
            expected = os.environ.get(field.upper(), '')
            if expected:
                if field in result and str(result[field]) != expected:
                    raise PackagingError('Captured manager identity differs from the verified paired APK: ' + field)
                result[field] = expected
    for field, env in (('producer_repository', 'GITHUB_REPOSITORY'), ('producer_run_id', 'GITHUB_RUN_ID'),
                       ('producer_run_attempt', 'GITHUB_RUN_ATTEMPT'), ('producer_head_sha', 'GITHUB_SHA')):
        if os.environ.get(env):
            if field in result and str(result[field]) != os.environ[env]:
                raise PackagingError('Captured kernel workflow provenance changed: ' + field)
            result[field] = os.environ[env]
    validate_kernel_document(result)
    return result


def embed_kernel_metadata(path: Path, metadata: dict) -> None:
    """Rewrite one staged ZIP without changing any kernel/runtime member bytes."""
    with zipfile.ZipFile(path) as original:
        members = validate_members(original)
        if KERNEL_MEMBER in members:
            old = read_member(original, KERNEL_MEMBER)
            if old != metadata:
                raise PackagingError('AnyKernel3 already embeds a different build identity')
            verify_image_metadata(metadata, original.read('Image'))
            return
        verify_image_metadata(metadata, original.read('Image'))
        temporary = path.with_name(path.name + '.metadata.tmp')
        try:
            with zipfile.ZipFile(temporary, 'w') as target:
                target.comment = original.comment
                for entry in original.infolist():
                    with original.open(entry) as source, target.open(entry, 'w') as destination:
                        shutil.copyfileobj(source, destination, 1024 * 1024)
                info = zipfile.ZipInfo(KERNEL_MEMBER, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                target.writestr(info, (json.dumps(metadata, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode())
            # The original ZIP must close before replacement on Windows.
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise PackagingError('Expected a nonempty regular asset file: ' + path.name)


def validate_ak3(path: Path, image_sha256: str) -> None:
    regular_file(path)
    try:
        with zipfile.ZipFile(path) as archive:
            members = {}
            for entry in archive.infolist():
                name = entry.filename
                components = name.rstrip('/').split('/')
                # Check original spellings as PurePosixPath normalizes ./ and //.
                if (not name or entry.orig_filename != name or '\\' in name or name.startswith('/') or ':' in name
                        or any(part in ('', '.', '..') for part in components)
                        or any(ord(char) < 32 or ord(char) == 127 for char in name)
                        or PurePosixPath(name).is_absolute()):
                    raise PackagingError('Unsafe path in the AnyKernel3 ZIP')
                if name in members or (name.rstrip('/') + '/') in members or name.rstrip('/') in members:
                    raise PackagingError('Duplicate path in the AnyKernel3 ZIP')
                mode = entry.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or entry.flag_bits & 1:
                    raise PackagingError('AnyKernel3 ZIP contains an unsupported file type or encrypted entry')
                members[name] = entry
            for name in REQUIRED:
                entry = members.get(name)
                if (entry is None or entry.is_dir() or not entry.file_size
                        or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)):
                    raise PackagingError('AnyKernel3 ZIP is missing a nonempty root asset: ' + name)
            if members['Image'].file_size > 256 * 1024 * 1024:
                raise PackagingError('Unexpectedly large kernel Image in AnyKernel3 ZIP')
            digest = hashlib.sha256()
            with archive.open('Image') as image:
                for chunk in iter(lambda: image.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != image_sha256.lower():
                raise PackagingError('AnyKernel3 Image SHA256 does not match the captured compiled kernel')
            if archive.testzip() is not None:
                raise PackagingError('AnyKernel3 ZIP CRC verification failed')
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise PackagingError('AnyKernel3 ZIP could not pass integrity verification') from error


def read_metadata(path: Path) -> tuple[dict, bytes]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise PackagingError('Kernel metadata must be a regular JSON file')
    raw = path.read_bytes()

    def unique_object(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PackagingError('Kernel metadata contains duplicate JSON fields')
            result[key] = value
        return result

    def reject_constant(_):
        raise PackagingError('Kernel metadata contains a non-finite number')

    metadata = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object, parse_constant=reject_constant)
    if not isinstance(metadata, dict):
        raise PackagingError('Kernel metadata must contain one JSON object')
    name = metadata.get('file')
    match = AK3_NAME.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        raise PackagingError('Kernel metadata file must use the public AnyKernel3 target filename')
    digest = metadata.get('image_sha256')
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise PackagingError('Kernel metadata must contain the captured Image SHA256')
    for key, group in (('android_version', 'android'), ('kernel_version', 'series'),
                       ('sub_level', 'sublevel'), ('os_patch_level', 'patch')):
        if metadata.get(key) is not None and str(metadata[key]) != match[group]:
            raise PackagingError('Kernel metadata target does not match its public asset filename')
    return metadata, raw


def selected_assets(workspace: Path, metadata: dict, mode: str) -> list[Path]:
    zip_path = workspace / metadata['file']
    validate_ak3(zip_path, metadata['image_sha256'])
    assets = [zip_path]
    if mode == 'AK3 + boot 镜像':
        prefix = metadata['file'].removesuffix('-AnyKernel3.zip')
        allowed = {prefix + suffix for suffix in BOOT_SUFFIXES}
        images = sorted(workspace.glob('*.img'))
        if not images:
            raise PackagingError('AK3 + boot mode did not produce any boot images')
        for image in images:
            if image.name not in allowed:
                raise PackagingError('Unexpected or mismatched public boot image filename: ' + image.name)
            regular_file(image)
        assets.extend(images)
    return assets


def check_destination(path: Path, expected_sha256: str) -> bool:
    """Reuse exactly the same asset on a retry; never overwrite another asset."""
    if path.is_symlink():
        raise PackagingError('Refusing to replace a linked release asset: ' + path.name)
    if not path.exists():
        return False
    regular_file(path)
    if sha256_file(path) != expected_sha256:
        raise PackagingError('Refusing to overwrite an unrelated release asset: ' + path.name)
    return True


def atomic_metadata(path: Path, original: bytes, updated: dict) -> None:
    if path.is_symlink() or path.read_bytes() != original:
        raise PackagingError('Kernel metadata changed during asset packaging')
    staged = path.with_name('.kernel-metadata-' + uuid.uuid4().hex + '.tmp')
    try:
        with staged.open('xb') as stream:
            stream.write((json.dumps(updated, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def package(metadata_path: Path, workspace: Path, output: Path, mode: str) -> dict:
    if mode not in MODES:
        raise PackagingError('Unknown kernel asset mode')
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir() or output.is_symlink():
        raise PackagingError('Expected a workspace directory and an unlinked output directory')
    metadata, original = read_metadata(metadata_path)
    assets = selected_assets(workspace, metadata, mode)
    hashes = {path.name: sha256_file(path) for path in assets}
    updated = embedded_document(metadata, assets, hashes)
    for source in assets[1:]:
        check_destination(output / source.name, hashes[source.name])
    existing_ak3 = output / assets[0].name
    if existing_ak3.exists() or existing_ak3.is_symlink():
        try:
            matching = read_kernel_ak3(existing_ak3) == updated
        except (OSError, ValueError):
            matching = False
        if not matching:
            raise PackagingError('Refusing to overwrite an unrelated release asset: ' + existing_ak3.name)
    output.mkdir(parents=True, exist_ok=True)
    staging = output / ('.kernel-assets-' + uuid.uuid4().hex)
    staging.mkdir()
    created = []
    try:
        for source in assets:
            staged = staging / source.name
            shutil.copyfile(source, staged)
            regular_file(staged)
            if sha256_file(staged) != hashes[source.name]:
                raise PackagingError('Kernel asset changed while it was being copied')
        # Public identity travels inside the final ZIP, never in a second ZIP.
        staged_ak3 = staging / metadata['file']
        embed_kernel_metadata(staged_ak3, updated)
        read_kernel_ak3(staged_ak3)
        hashes[metadata['file']] = sha256_file(staged_ak3)
        for source in assets:
            destination = output / source.name
            if not check_destination(destination, hashes[source.name]):
                # Staging lives on the same filesystem. A hard link publishes
                # atomically and fails if another writer created this name.
                os.link(staging / source.name, destination)
                created.append(destination)
        atomic_metadata(metadata_path, original, updated)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    finally:
        # The directory is flat and contains only our explicitly staged assets.
        for path in staging.iterdir():
            path.unlink()
        staging.rmdir()
    return {'files': updated['files'], 'sha256': hashes}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=MODES, required=True)
    args = parser.parse_args(argv)
    try:
        result = package(args.metadata, args.workspace, args.output, args.mode)
    except (OSError, ValueError, UnicodeError) as error:
        print('Kernel Release asset packaging failed: ' + str(error), file=sys.stderr)
        return 1
    if os.environ.get('GITHUB_OUTPUT'):
        files = result['files']
        outputs = {'ak3': next(name for name in files if name.endswith('-AnyKernel3.zip'))}
        for key, suffix in (('boot', '-boot.img'), ('boot_gz', '-boot-gz.img'), ('boot_lz4', '-boot-lz4.img')):
            outputs[key] = next((name for name in files if name.endswith(suffix)), '')
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as stream:
            for key, value in outputs.items():
                stream.write(f'{key}={value}\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
