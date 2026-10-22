#!/usr/bin/env python3
"""Validate and collect direct AK3/boot Release assets for one compiled kernel."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import uuid
import zipfile

AK3_NAME = re.compile(
    r'(?P<android>android[0-9]+)-(?P<series>[0-9]+\.[0-9]+)\.(?P<sublevel>[0-9]+|X)-'
    r'(?P<patch>[0-9]{4}-(?:0[1-9]|1[0-2])|lts)-AnyKernel3\.zip\Z')
SHA256 = re.compile(r'[0-9a-fA-F]{64}\Z')
BOOT_SUFFIXES = ('-boot.img', '-boot-gz.img', '-boot-lz4.img')
MODES = ('仅 AK3', 'AK3 + boot 镜像')
REQUIRED = ('Image', 'anykernel.sh', 'META-INF/com/google/android/update-binary')


class PackagingError(ValueError):
    """A kernel asset is incomplete, mismatched, or unsafe to publish."""


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
    if path.is_symlink() or not path.is_file():
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
    for source in assets:
        check_destination(output / source.name, hashes[source.name])
    updated = {**metadata, 'files': [path.name for path in assets]}
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
        # Verify the exact ZIP bytes being published as well as the input ZIP.
        validate_ak3(staging / metadata['file'], metadata['image_sha256'])
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
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
