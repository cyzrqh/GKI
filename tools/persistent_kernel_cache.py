#!/usr/bin/env python3
"""Keep compilation objects in the owner's dedicated private cache repository.

This transports cache data only. The compiler/configuration checks still decide
whether any object can be reused; this never exports a kernel or changes a build.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from snapshot_ksu_source import temporary_directory
from transfer_ksu_snapshot import GitHub, NoRedirect, PUBLIC_DOWNLOAD_HOSTS, TransferError

MARKER = '<!-- gki-persistent-objects:v1 -->\n'
MAX_ARCHIVE = 1900 * 1024 * 1024
MAX_EXPANDED = 8 * 1024 * 1024 * 1024
MAX_FILES = 250000
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def checked_root(path: Path, *, create=False) -> Path:
    path = path.absolute()
    if any(is_link(item) for item in (path, *path.parents)):
        raise ValueError('Cache directory must not traverse links')
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError('Cache directory is unavailable')
    return path


def key_identity(key: str, kind: str) -> str:
    if (kind not in ('ccache', 'bazel') or not isinstance(key, str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,400}', key)):
        raise ValueError('Invalid compilation cache identity')
    return 'objects-v1-' + hashlib.sha256((kind + ':' + key).encode()).hexdigest()[:40]


def validate_repo(repository: str, producer: str) -> None:
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', producer)
            or repository.casefold() != (producer.split('/')[0] + '/gki-cache').casefold()
            or repository.casefold() == producer.casefold()):
        raise ValueError('Persistent objects must stay in the owner\'s dedicated gki-cache repository')


def valid_name(name: str) -> bool:
    parts = name.split('/')
    return (0 < len(name) <= 4096 and not name.startswith('/') and '\\' not in name and ':' not in name
            and all(part not in ('', '.', '..') for part in parts)
            and not any(ord(char) < 32 or ord(char) == 127 for char in name))


def excluded(name: str) -> bool:
    parts = name.split('/')
    return (any(part in ('tmp', 'temp') for part in parts)
            or parts[-1] in ('ccache.conf', 'stats', 'stats.lock', 'CACHEDIR.TAG')
            or parts[-1].endswith(('.lock', '.tmp')))


def pack(directory: Path, output: Path) -> dict:
    directory = checked_root(directory)
    files, expanded = [], 0
    for item in sorted(directory.rglob('*')):
        if is_link(item):
            raise ValueError('Compilation caches must contain only regular files and directories')
        if item.is_dir():
            continue
        name = item.relative_to(directory).as_posix()
        if not valid_name(name) or not item.is_file():
            raise ValueError('Unsafe compilation cache file')
        if excluded(name):
            continue
        expanded += item.stat().st_size
        files.append((item, name))
        if expanded > MAX_EXPANDED or len(files) > MAX_FILES:
            raise ValueError('Compilation cache exceeds its data budget')
    if not files:
        raise ValueError('No compilation objects to preserve')
    # Result entries are already compressed by ccache. Fast gzip avoids a
    # second expensive compression pass while retaining portable extraction.
    with output.open('xb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
            for item, name in files:
                info = tarfile.TarInfo(name)
                info.size = item.stat().st_size
                info.mode = 0o600
                info.mtime = 0
                with item.open('rb') as stream:
                    archive.addfile(info, stream)
    size = output.stat().st_size
    if size > MAX_ARCHIVE:
        raise ValueError('Compilation cache archive exceeds the release-asset budget')
    return {'size': size, 'sha256': digest(output), 'expanded': expanded, 'files': len(files)}


def unpack(archive_path: Path, directory: Path, expected: dict) -> None:
    if (archive_path.stat().st_size != expected['size'] or digest(archive_path) != expected['sha256']):
        raise ValueError('Persistent cache archive integrity check failed')
    directory = checked_root(directory, create=True)
    with temporary_directory(directory.parent, '.gki-object-restore-') as staging:
        names, expanded = set(), 0
        with tarfile.open(archive_path, 'r|gz') as archive:
            for member in archive:
                if (not member.isfile() or not valid_name(member.name) or excluded(member.name)
                        or member.name in names or member.size < 0):
                    raise ValueError('Unsafe or duplicate persistent cache archive entry')
                names.add(member.name)
                expanded += member.size
                if expanded > expected['expanded'] or len(names) > expected['files']:
                    raise ValueError('Persistent cache archive exceeds its declared extent')
                target = staging.joinpath(*PurePosixPath(member.name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open('xb') as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                target.chmod(0o600)
        if len(names) != expected['files'] or expanded != expected['expanded']:
            raise ValueError('Persistent cache archive is incomplete')
        # Publish only after every archive entry has passed validation. Never
        # import executable ccache.conf or delete an existing local cache.
        for name in sorted(names):
            target = directory.joinpath(*PurePosixPath(name).parts)
            if any(is_link(item) for item in (target, *target.parents)):
                raise ValueError('Local compilation cache contains a linked destination')
            if target.exists() and not target.is_file():
                raise ValueError('Local compilation cache destination is not a regular file')
        for name in sorted(names):
            target = directory.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging.joinpath(*PurePosixPath(name).parts), target)


class CacheGitHub(GitHub):
    def __init__(self, repository: str, token: str):
        if not token or any(ord(char) < 32 or ord(char) == 127 for char in token):
            raise ValueError('A dedicated cache repository token is required')
        self.repository, self.token = repository, token
        self.opener = build_opener(NoRedirect())

    def stream(self, path: str, file: Path, *, upload=False) -> None:
        if upload:
            url = 'https://uploads.github.com/repos/' + self.repository + path
        else:
            url = 'https://api.github.com/repos/' + self.repository + path
        headers = {'User-Agent': 'gki-persistent-objects', 'Authorization': 'Bearer ' + self.token,
                   'Accept': 'application/vnd.github+json' if upload else 'application/octet-stream'}
        if upload:
            headers.update({'Content-Type': 'application/octet-stream', 'Content-Length': str(file.stat().st_size)})
        try:
            if upload:
                with file.open('rb') as data:
                    with self.opener.open(Request(url, data=data, headers=headers, method='POST'), timeout=120) as response:
                        response.read(16384)
                return
            try:
                response = self.opener.open(Request(url, headers=headers), timeout=120)
            except HTTPError as error:
                if error.code not in (301, 302, 303, 307, 308):
                    raise
                target = error.headers.get('Location', '')
                error.close()
                parsed = urlsplit(target)
                if (parsed.scheme != 'https' or parsed.hostname not in PUBLIC_DOWNLOAD_HOSTS
                        or parsed.username or parsed.password or parsed.port not in (None, 443)):
                    raise ValueError('Unexpected persistent object download destination')
                # Private release assets use presigned public URLs. Never send
                # the repository credential to the storage service.
                response = self.opener.open(Request(target, headers={'User-Agent': 'gki-persistent-objects'}), timeout=120)
            size = 0
            with response, file.open('xb') as output:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_ARCHIVE:
                        raise ValueError('Persistent object download exceeds its size bound')
                    output.write(block)
        except HTTPError as error:
            raise TransferError(f'Persistent cache transfer HTTP {error.code}') from None
        except (OSError, URLError, http.client.HTTPException):
            raise TransferError('Persistent cache transfer could not complete') from None


def control(release: dict, key: str, kind: str, producer: str, *, pending=False) -> tuple[dict, dict | None]:
    if (not isinstance(release, dict) or type(release.get('id')) is not int or release['id'] < 1
            or release.get('tag_name') != key_identity(key, kind)
            or (release.get('draft') is not False and not (pending and release.get('draft') is True))
            or release.get('prerelease') is not True or not isinstance(release.get('body'), str)
            or not release['body'].startswith(MARKER) or len(release['body']) > 16384):
        raise ValueError('Persistent cache release identity differs')
    value = json.loads(release['body'][len(MARKER):])
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'key', 'kind', 'producer', 'archive'}
            or value['schema_version'] != 1 or type(value['schema_version']) is not int
            or value['key'] != key or value['kind'] != kind or not isinstance(value['producer'], str)
            or value['producer'].casefold() != producer.casefold()):
        raise ValueError('Persistent cache ownership metadata differs')
    entry = value['archive']
    if (not isinstance(entry, dict) or set(entry) != {'name', 'size', 'sha256', 'expanded', 'files'}
            or not isinstance(entry['sha256'], str) or not DIGEST.fullmatch(entry['sha256'])
            or entry['name'] != 'objects-' + entry['sha256'] + '.tar.gz'
            or any(type(entry[name]) is not int for name in ('size', 'expanded', 'files'))
            or not 0 < entry['size'] <= MAX_ARCHIVE or not 0 <= entry['expanded'] <= MAX_EXPANDED
            or not 0 < entry['files'] <= MAX_FILES):
        raise ValueError('Invalid persistent cache archive descriptor')
    assets = release.get('assets')
    if not isinstance(assets, list) or any(not isinstance(asset, dict) for asset in assets):
        raise ValueError('Persistent cache assets are unavailable')
    matched = [asset for asset in assets if asset.get('name') == entry['name']]
    if not matched and pending and release.get('draft') is True:
        return entry, None
    if (len(matched) != 1 or type(matched[0].get('id')) is not int or matched[0]['id'] < 1
            or matched[0].get('size') != entry['size'] or matched[0].get('state') != 'uploaded'):
        raise ValueError('Persistent cache asset is missing or incomplete')
    return entry, matched[0]


def restore(api, directory: Path, key: str, kind: str, producer: str) -> bool:
    release = api.json('/releases/tags/' + key_identity(key, kind), missing=True)
    if release is None:
        return False
    entry, asset = control(release, key, kind, producer)
    directory = checked_root(directory, create=True)
    for attempt in range(3):
        try:
            with temporary_directory(directory.parent, '.gki-object-download-') as staging:
                archive = staging / 'objects.tar.gz'
                api.stream('/releases/assets/' + str(asset['id']), archive)
                unpack(archive, directory, entry)
            break
        except TransferError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    return True


def find_for_save(api, tag: str) -> dict | None:
    release = api.json('/releases/tags/' + tag, missing=True)
    if release is not None:
        return release
    # GitHub's by-tag endpoint only returns published releases. A runner
    # interrupted during its first upload leaves a draft without a tag ref;
    # recover that exact draft through the authenticated releases listing.
    for page in range(1, 21):
        listing = api.json(f'/releases?per_page=100&page={page}')
        if not isinstance(listing, list) or any(not isinstance(item, dict) for item in listing):
            raise ValueError('Persistent cache release listing is unavailable')
        matched = [item for item in listing if item.get('tag_name') == tag]
        if len(matched) > 1:
            raise ValueError('Multiple pending snapshots have the same cache identity')
        if matched:
            return matched[0]
        if len(listing) < 100:
            return None
    raise ValueError('Persistent cache release listing exceeds its lookup budget')


def save(api, directory: Path, key: str, kind: str, producer: str) -> bool:
    directory = checked_root(directory)
    tag = key_identity(key, kind)
    with temporary_directory(directory.parent, '.gki-object-upload-') as staging:
        archive = staging / 'objects.tar.gz'
        entry = pack(directory, archive)
        entry['name'] = 'objects-' + entry['sha256'] + '.tar.gz'
        descriptor = {'schema_version': 1, 'key': key, 'kind': kind, 'producer': producer, 'archive': entry}
        body = MARKER + json.dumps(descriptor, sort_keys=True, separators=(',', ':'))
        release = find_for_save(api, tag)
        old_assets = []
        if release is None:
            # A draft is hidden even in the private cache repo until its first
            # archive is complete. It never appears in the GKI release list.
            release = api.json('/releases', method='POST', document={'tag_name': tag,
                'target_commitish': 'main', 'name': key, 'body': body, 'draft': True,
                'prerelease': True, 'make_latest': 'false'})
        else:
            control(release, key, kind, producer, pending=True)
            old_assets = list(release['assets'])
        if not isinstance(release, dict) or type(release.get('id')) is not int:
            raise ValueError('Cannot prepare the dedicated cache release')
        current = next((asset for asset in old_assets if asset.get('name') == entry['name']), None)
        if current is None:
            api.stream('/releases/' + str(release['id']) + '/assets?' + urlencode({'name': entry['name']}), archive, upload=True)
        api.json('/releases/' + str(release['id']), method='PATCH',
                 document={'body': body, 'draft': False, 'prerelease': True, 'make_latest': 'false'})
        verified = api.json('/releases/tags/' + tag)
        final_entry, _ = control(verified, key, kind, producer)
        if final_entry != entry:
            return False  # Another successful run published a newer snapshot.
        # Only superseded object assets from this exact cache identity may be
        # removed. No builds, kernel Releases or unrelated repository files.
        for asset in old_assets:
            if (type(asset.get('id')) is int and asset.get('name') != entry['name']
                    and re.fullmatch(r'objects-[0-9a-f]{64}\.tar\.gz', str(asset.get('name', '')))):
                api.json('/releases/assets/' + str(asset['id']), method='DELETE')
        return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('restore', 'save'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--kind', required=True, choices=('ccache', 'bazel'))
    args = parser.parse_args(argv)
    started = time.monotonic()
    repository = os.environ.get('CACHE_REPOSITORY', '')
    producer = os.environ.get('GITHUB_REPOSITORY', '')
    token = os.environ.get('CACHE_TOKEN', '')
    state = 'unavailable'
    try:
        validate_repo(repository, producer)
        key_identity(args.key, args.kind)
        api = CacheGitHub(repository, token)
        result = (restore if args.operation == 'restore' else save)(api, args.directory, args.key, args.kind, producer)
        state = ('restored' if result else 'miss') if args.operation == 'restore' else ('saved' if result else 'superseded')
        print('Persistent compilation cache: ' + state)
    except (OSError, ValueError, TransferError, tarfile.TarError, EOFError):
        # Cache outages must not turn a valid kernel into a failed build. Do
        # not log raw remote content, signed asset URLs, or credential values.
        print('::warning::Persistent compilation cache unavailable. Ensure UPSTREAM_SYNC_TOKEN has Contents read/write access to gki-cache; continuing with local/Actions cache.')
    if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(summary).open('a', encoding='utf-8') as output:
            output.write(f'\n独立编译缓存（{args.kind} / {args.operation}）：**{state}**，耗时 {time.monotonic() - started:.1f} 秒。\n')
    if destination := os.environ.get('GITHUB_OUTPUT'):
        with Path(destination).open('a', encoding='utf-8') as output:
            output.write('restored=' + str(state == 'restored').lower() + '\nsaved='
                         + str(state == 'saved').lower() + '\nstate=' + state + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
