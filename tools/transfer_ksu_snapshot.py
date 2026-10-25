#!/usr/bin/env python3
"""Keep a run-bound SukiSU snapshot in a temporary draft Release, outside Artifacts.

Actions cache is only an accelerator. Failed runs retain their draft backup;
a matching published build or verified original Actions files allow this tool
to delete that exact draft after the source consumers finish successfully.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from checkout_ksu_snapshot import read_manifest
from cleanup_build_artifacts import validate_release
from snapshot_ksu_source import (BUNDLE_NAME, MANIFEST_NAME, MAX_BUNDLE_SIZE, MAX_MANIFEST_SIZE,
                                 REPOSITORY, ROOT, SnapshotError, expected_commit, file_digest,
                                 git, snapshot_source, source_identity, temporary_directory)


MARKER = '<!-- gki-internal-source:v1 -->\n'
MAX_JSON = 2 * 1024 * 1024
PUBLIC_DOWNLOAD_HOSTS = {'release-assets.githubusercontent.com', 'objects.githubusercontent.com',
                         'github-releases.githubusercontent.com'}
FILES = {BUNDLE_NAME, MANIFEST_NAME}
MAX_PUBLIC_ASSETS = 1000
PUBLIC_ASSET_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+@()-]{0,249}\Z')


class TransferError(ValueError):
    pass


class BackupUnavailable(TransferError):
    """The transport is unavailable; this does not waive any integrity check."""


def integer(value, label: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise TransferError('Invalid ' + label)
    return int(value)


@dataclass(frozen=True)
class Expected:
    repository: str
    run_id: int
    run_attempt: int
    head_sha: str
    ksu_ref: str

    def validate(self):
        if (not isinstance(self.repository, str) or not re.fullmatch(
                r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', self.repository)
                or self.repository.split('/')[1] in ('.', '..')):
            raise TransferError('Invalid snapshot repository')
        integer(self.run_id, 'workflow run ID')
        integer(self.run_attempt, 'workflow attempt')
        if self.head_sha != expected_commit(self.head_sha) or self.ksu_ref != expected_commit(self.ksu_ref):
            raise TransferError('Snapshot identities must be lowercase full commit hashes')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def unique_object(items):
    document = {}
    for key, value in items:
        if key in document:
            raise TransferError('Duplicate snapshot backup metadata field')
        document[key] = value
    return document


class GitHub:
    """Send credentials only to fixed GitHub API hosts, never to asset redirects."""
    def __init__(self, repository: str, *, opener=None):
        self.repository = repository
        self.token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN', '')
        if not self.token or any(ord(char) < 32 or ord(char) == 127 for char in self.token):
            raise TransferError('A repository Actions token is required for the source backup')
        self.opener = opener or build_opener(NoRedirect())

    def request(self, url: str, *, method='GET', data=None, limit=MAX_JSON,
                authenticated=True, binary=False, binary_body=False, missing=False) -> bytes | None:
        parsed = urlparse(url)
        allowed = {'api.github.com', 'uploads.github.com'} if authenticated else PUBLIC_DOWNLOAD_HOSTS
        if (parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment):
            raise TransferError('Unexpected snapshot backup HTTP destination')
        operation = method + (' ' + parsed.path if authenticated else ' public release asset')
        # Release asset uploads have a binary body but a JSON API response.
        # Only asset downloads request an octet-stream response.
        headers = {'User-Agent': 'gki-source-backup',
                   'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.token
        if data is not None:
            headers['Content-Type'] = 'application/octet-stream' if binary_body else 'application/json'
        request = Request(url, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=90)
        except HTTPError as error:
            if missing and error.code == 404:
                return None
            if authenticated and binary and method == 'GET' and error.code in (301, 302, 303, 307, 308):
                location = error.headers.get('Location', '')
                error.close()
                # A fresh, unauthenticated request also uses NoRedirect. Thus
                # even a second redirect cannot leak the repository token.
                return self.request(location, limit=limit, authenticated=False, binary=True)
            kind = BackupUnavailable if method == 'GET' and error.code in (404, 408, 429, 500, 502, 503, 504) else TransferError
            raise kind(f'Snapshot backup {operation} failed (HTTP {error.code})') from None
        except (OSError, URLError, ValueError):
            kind = BackupUnavailable if method == 'GET' else TransferError
            raise kind('Snapshot backup ' + operation + ' could not complete') from None
        try:
            with response:
                raw = response.read(limit + 1)
            if len(raw) > limit:
                raise TransferError('Snapshot backup HTTP response exceeds its size limit')
            return raw
        except TransferError:
            raise
        except (OSError, URLError, ValueError):
            kind = BackupUnavailable if method == 'GET' else TransferError
            raise kind('Snapshot backup ' + operation + ' response could not be read safely') from None

    def json(self, path: str, *, method='GET', document=None, missing=False):
        if not path.startswith('/') or any(char in path for char in '\r\n#'):
            raise TransferError('Invalid snapshot API path')
        raw = self.request('https://api.github.com/repos/' + self.repository + path, method=method,
                           data=None if document is None else json.dumps(document, ensure_ascii=True).encode(),
                           missing=missing)
        if raw is None or not raw:
            return None
        try:
            return json.loads(raw, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            raise TransferError('Snapshot backup API returned invalid JSON') from None

    def upload(self, release_id: int, name: str, data: bytes):
        self.request('https://uploads.github.com/repos/' + self.repository + '/releases/'
                     + str(release_id) + '/assets?' + urlencode({'name': name}),
                     method='POST', data=data, binary_body=True)

    def download(self, asset_id: int, limit: int) -> bytes:
        result = self.request('https://api.github.com/repos/' + self.repository + '/releases/assets/'
                              + str(asset_id), binary=True, limit=limit)
        if not isinstance(result, bytes):
            raise TransferError('Snapshot backup asset was not downloaded')
        return result


def snapshot_files(directory: Path, reference: str) -> tuple[dict, dict[str, bytes]]:
    if directory.is_symlink() or not directory.is_dir() or {path.name for path in directory.iterdir()} != FILES:
        raise TransferError('Expected only the two regular source snapshot files')
    manifest = read_manifest(directory, reference)
    bundle = directory / BUNDLE_NAME
    if (bundle.is_symlink() or not bundle.is_file() or bundle.stat().st_size != manifest['bundle_size']
            or file_digest(bundle) != manifest['bundle_sha256']):
        raise TransferError('Snapshot bundle does not match its verified manifest')
    contents = {name: (directory / name).read_bytes() for name in FILES}
    if len(contents[BUNDLE_NAME]) != manifest['bundle_size'] or hashlib.sha256(contents[BUNDLE_NAME]).hexdigest() != manifest['bundle_sha256']:
        raise TransferError('Snapshot bundle changed while preparing its backup')
    if json.loads(contents[MANIFEST_NAME], object_pairs_hook=unique_object) != manifest:
        raise TransferError('Snapshot manifest changed while preparing its backup')
    return manifest, contents


def control_document(expected: Expected, files: dict[str, bytes]) -> dict:
    return {'schema_version': 1, 'repository': expected.repository.lower(),
            'run_id': int(expected.run_id), 'run_attempt': int(expected.run_attempt),
            'head_sha': expected.head_sha, 'ksu_ref': expected.ksu_ref,
            'files': {name: {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                      for name, raw in sorted(files.items())}}


def tag_for(control: dict) -> str:
    return f"gki-internal-source-{control['run_id']}-{control['run_attempt']}-{control['ksu_ref'][:12]}"


def title_for(control: dict) -> str:
    return f"GKI 内部临时源码 · 运行 {control['run_id']} / {control['run_attempt']}"


def validate_backup(release: dict, expected: Expected, release_id: int, *, require_assets=True) -> tuple[dict, dict]:
    expected.validate()
    if (not isinstance(release, dict) or release.get('id') != release_id or type(release.get('id')) is not int
            or release.get('draft') is not True or release.get('prerelease') is not False
            or release.get('published_at') is not None or release.get('target_commitish') != expected.head_sha):
        raise TransferError('Refusing a source backup that is not this build commit’s temporary draft')
    body = release.get('body')
    if not isinstance(body, str) or not body.startswith(MARKER) or len(body.encode()) > 16384:
        raise TransferError('The draft is missing its internal snapshot ownership marker')
    try:
        control = json.loads(body[len(MARKER):], object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise TransferError('The draft has invalid snapshot ownership metadata') from None
    if (not isinstance(control, dict) or set(control) != {'schema_version', 'repository', 'run_id',
            'run_attempt', 'head_sha', 'ksu_ref', 'files'} or type(control.get('schema_version')) is not int
            or control['schema_version'] != 1 or control.get('repository') != expected.repository.lower()
            or type(control.get('run_id')) is not int or control['run_id'] != int(expected.run_id)
            or type(control.get('run_attempt')) is not int or not 1 <= control['run_attempt'] <= int(expected.run_attempt)
            or control.get('head_sha') != expected.head_sha or control.get('ksu_ref') != expected.ksu_ref):
        raise TransferError('Snapshot backup belongs to a different repository, run, attempt, head or source')
    if release.get('tag_name') != tag_for(control) or release.get('name') != title_for(control):
        raise TransferError('Snapshot backup draft name/tag ownership does not match')
    descriptors = control.get('files')
    if not isinstance(descriptors, dict) or set(descriptors) != FILES:
        raise TransferError('Snapshot backup must describe exactly its two source files')
    for name, descriptor in descriptors.items():
        limit = MAX_BUNDLE_SIZE if name == BUNDLE_NAME else MAX_MANIFEST_SIZE
        if (not isinstance(descriptor, dict) or set(descriptor) != {'size', 'sha256'}
                or type(descriptor.get('size')) is not int or not 0 < descriptor['size'] <= limit
                or not isinstance(descriptor.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', descriptor['sha256'])):
            raise TransferError('Snapshot backup file integrity metadata is invalid')
    assets = release.get('assets')
    if not isinstance(assets, list):
        raise TransferError('Snapshot backup is missing its asset inventory')
    selected = {}
    for asset in assets:
        if not isinstance(asset, dict) or asset.get('name') not in FILES or asset['name'] in selected:
            raise TransferError('The temporary snapshot draft contains unexpected or duplicate assets')
        name = asset['name']
        integer(asset.get('id'), 'snapshot asset ID')
        if (type(asset.get('size')) is not int or asset['size'] != descriptors[name]['size']
                or asset.get('state') != 'uploaded'
                or asset.get('digest') not in (None, 'sha256:' + descriptors[name]['sha256'])):
            raise TransferError('The source backup asset does not match its integrity metadata')
        selected[name] = asset
    if require_assets and set(selected) != FILES:
        raise TransferError('The source backup is incomplete; preserving its draft for recovery')
    return control, selected


def create(source: Path, expected: Expected, api: GitHub) -> dict:
    expected.validate()
    _, files = snapshot_files(source, expected.ksu_ref)
    control = control_document(expected, files)
    release = api.json('/releases', method='POST', document={
        'tag_name': tag_for(control), 'target_commitish': expected.head_sha, 'name': title_for(control),
        'body': MARKER + json.dumps(control, sort_keys=True, ensure_ascii=True),
        'draft': True, 'prerelease': False, 'make_latest': 'false'})
    release_id = integer(release.get('id') if isinstance(release, dict) else None, 'backup release ID')
    validate_backup(release, expected, release_id, require_assets=False)
    # Never delete a partial upload on failure: the owning run may recover it.
    for name in sorted(FILES):
        api.upload(release_id, name, files[name])
    stored = api.json('/releases/' + str(release_id))
    validate_backup(stored, expected, release_id)
    return {'backup_release_id': release_id, 'backup_release_tag': tag_for(control)}


@contextmanager
def recovery_directory(parent: Path):
    """Clean only our known files; preserve both directories on rollback failure."""
    directory = parent / ('.gki-source-recovery-' + uuid.uuid4().hex)
    directory.mkdir()
    state = {'path': directory, 'preserve': False}
    try:
        yield state
    finally:
        if not state['preserve']:
            # Preflight every path before deleting any file. Never recursively
            # remove a previous cache, including after a failed installation.
            if directory.is_symlink() or directory.resolve().parent != parent:
                raise TransferError('Refusing cleanup outside the source recovery directory')
            children = list(directory.iterdir())
            for child in children:
                if (child.name not in {'verified', 'partial-cache'} or child.is_symlink() or not child.is_dir()
                        or any(path.name not in FILES or path.is_symlink() or not path.is_file()
                               for path in child.iterdir())):
                    raise TransferError('Unknown recovery files preserved at ' + str(directory))
            for child in children:
                for path in child.iterdir():
                    path.unlink()
                child.rmdir()
            directory.rmdir()


def validate_recovery_output(output: Path) -> None:
    if output.name != 'ksu-source-bundle' or output.is_symlink() or not output.parent.is_dir():
        raise TransferError('Snapshot recovery must use the dedicated ksu-source-bundle directory')
    if output.exists() and (not output.is_dir() or any(
            path.name not in FILES or path.is_symlink() or not path.is_file() for path in output.iterdir())):
        raise TransferError('Refusing to overwrite unknown files or links in the source cache directory')
    if output.parent.resolve() != output.parent:
        raise TransferError('Snapshot recovery parent directory must not be linked')


def install_snapshot(staged: Path, output: Path, recovery: dict) -> None:
    """Install verified files atomically, retaining the previous cache on error."""
    previous = recovery['path'] / 'partial-cache'
    validate_recovery_output(output)
    if output.exists():
        output.rename(previous)
    try:
        staged.rename(output)
    except BaseException as install_error:
        if previous.exists():
            try:
                if output.exists() or output.is_symlink():
                    raise OSError('The output path is occupied')
                previous.rename(output)
            except OSError:
                recovery['preserve'] = True
                raise TransferError('Snapshot installation and rollback failed; original cache preserved at '
                                    + str(previous) + '; verified backup retained at ' + str(staged)) from install_error
        raise


def prepared_identity(document: dict, reference: str) -> dict:
    """Keep native version and tree identities bound to the successful prepare job."""
    if (not isinstance(document, dict) or set(document) != {'ref', 'git_tree', 'git_count', 'git_describe'}
            or document.get('ref') != reference
            or not isinstance(document.get('git_tree'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', document['git_tree'])
            or type(document.get('git_count')) is not int or document['git_count'] < 1
            or not isinstance(document.get('git_describe'), str) or not 1 <= len(document['git_describe']) <= 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in document['git_describe'])):
        raise TransferError('Official recovery requires this prepare job’s exact source and native version identity')
    return dict(document)


def restore_official(output: Path, expected: Expected, identity: dict, *, root: Path,
                     susfs: bool, tier: str) -> dict:
    """Rebuild an unavailable snapshot from the fixed public commit, never a branch."""
    identity = prepared_identity(identity, expected.ksu_ref)
    output = output.absolute()
    validate_recovery_output(output)
    with temporary_directory(output.parent, '.gki-official-source-') as temporary:
        source = temporary / 'official'
        source.mkdir()
        git(source, 'init', '--quiet')
        git(source, 'remote', 'add', 'origin', 'https://github.com/' + REPOSITORY + '.git')
        # No depth limit: both the native versionCode and versionName depend on
        # complete history/tags. A missing or rewritten commit fails closed.
        for attempt in range(3):
            try:
                git(source, 'fetch', '--quiet', '--no-recurse-submodules', '--tags', 'origin', expected.ksu_ref, timeout=120)
                break
            except SnapshotError:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        git(source, 'checkout', '--quiet', '--detach', expected.ksu_ref)
        actual = {'ref': expected.ksu_ref, **source_identity(source, expected.ksu_ref)}
        if actual != identity:
            raise TransferError('Official source tree, history or version tags changed since prepare; refusing a mixed build')
        recreated = temporary / 'recreated'
        manifest = snapshot_source(source, recreated, expected.ksu_ref, root=root, susfs=susfs, tier=tier)
        if {field: manifest[field] for field in identity} != identity:
            raise TransferError('Recovered source identity changed during verification')
        with recovery_directory(output.parent) as recovery:
            staged = recovery['path'] / 'verified'
            staged.mkdir()
            for name in sorted(FILES):
                shutil.copyfile(recreated / name, staged / name)
            snapshot_files(staged, expected.ksu_ref)
            install_snapshot(staged, output, recovery)
    return {'restored': True, 'ksu_ref': expected.ksu_ref, 'official_fallback': True}


def restore(release_id: int, output: Path, expected: Expected, api: GitHub, *,
            identity: dict | None = None, root: Path = ROOT, susfs: bool = False,
            tier: str = 'Custom(自定义)') -> dict:
    expected.validate()
    release_id = integer(release_id, 'backup release ID')
    if identity is not None:
        prepared_identity(identity, expected.ksu_ref)
    try:
        return restore_backup(release_id, output, expected, api)
    except BackupUnavailable:
        if identity is None:
            raise
        result = restore_official(output, expected, identity, root=root, susfs=susfs, tier=tier)
        return {'backup_release_id': release_id, **result}


def restore_backup(release_id: int, output: Path, expected: Expected, api: GitHub) -> dict:
    release = api.json('/releases/' + str(release_id), missing=True)
    if release is None:
        raise BackupUnavailable('This run’s temporary source backup is unavailable (HTTP 404)')
    control, assets = validate_backup(release, expected, release_id)
    output = output.absolute()
    validate_recovery_output(output)
    with recovery_directory(output.parent) as recovery:
        temporary = recovery['path']
        staged = temporary / 'verified'
        staged.mkdir()
        for name in sorted(FILES):
            expected_file = control['files'][name]
            raw = api.download(integer(assets[name]['id'], 'snapshot asset ID'), expected_file['size'])
            if len(raw) != expected_file['size'] or hashlib.sha256(raw).hexdigest() != expected_file['sha256']:
                raise TransferError('The downloaded source backup failed SHA256 verification: ' + name)
            (staged / name).write_bytes(raw)
        snapshot_files(staged, expected.ksu_ref)
        install_snapshot(staged, output, recovery)
    return {'backup_release_id': release_id, 'restored': True, 'ksu_ref': expected.ksu_ref}


def delete(release_id: int, published_tag: str, expected: Expected, api: GitHub) -> dict:
    expected.validate()
    release_id = integer(release_id, 'backup release ID')
    if published_tag != f'gki-{expected.run_id}-{expected.run_attempt}':
        raise TransferError('Source cleanup requires this attempt’s exact published build tag')
    published = api.json('/releases/tags/' + published_tag)
    if (not isinstance(published, dict) or published.get('tag_name') != published_tag
            or published.get('draft') is not False or published.get('prerelease') is not False
            or not published.get('published_at') or published.get('target_commitish') != expected.head_sha
            or not isinstance(published.get('assets'), list) or not published['assets']):
        raise TransferError('Refusing source cleanup before the matching final Release is published')
    # Match the existing final-file verification step's public asset contract;
    # an empty/partial or unrelated published release cannot unlock cleanup.
    validate_release(published, expected.head_sha)
    release = api.json('/releases/' + str(release_id), missing=True)
    if release is None:
        return {'backup_release_id': release_id, 'deleted': False, 'already_absent': True}
    validate_backup(release, expected, release_id)
    api.json('/releases/' + str(release_id), method='DELETE')
    return {'backup_release_id': release_id, 'deleted': True}


def native_assets(directory: Path) -> dict[str, dict]:
    """Read only direct native outputs, bound to their complete checksum list."""
    directory = Path(directory).absolute()
    if (directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory
            or any(parent.is_symlink() for parent in directory.parents)):
        raise TransferError('Actions cleanup requires an unlinked native assets directory')
    paths = list(directory.iterdir())
    if not 2 <= len(paths) <= MAX_PUBLIC_ASSETS:
        raise TransferError('Actions cleanup requires checksums and native build files')
    assets = {}
    for path in paths:
        name = path.name
        if (not PUBLIC_ASSET_NAME.fullmatch(name) or '..' in name
                or not (name == 'SHA256SUMS.txt' or name.endswith(('.apk', '.img', '-AnyKernel3.zip')))
                or path.is_symlink() or not path.is_file()):
            raise TransferError('Actions cleanup assets must be native APK, AK3, boot or checksum files')
        before = path.stat()
        if not 0 < before.st_size <= 2 * 1024 * 1024 * 1024:
            raise TransferError('Actions cleanup native asset has an invalid size')
        digest = file_digest(path)
        after = path.stat()
        if (path.is_symlink() or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise TransferError('Actions cleanup native asset changed during verification')
        assets[name] = {'size': before.st_size, 'digest': 'sha256:' + digest}
    if 'SHA256SUMS.txt' not in assets or assets['SHA256SUMS.txt']['size'] > 512 * 1024:
        raise TransferError('Actions cleanup requires a bounded SHA256SUMS.txt')
    try:
        content = (directory / 'SHA256SUMS.txt').read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        raise TransferError('Actions cleanup checksum file could not be read') from None
    checksums = {}
    for line in content.splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+@()-]{0,249})', line)
        if (match is None or match[2] == 'SHA256SUMS.txt' or match[2] in checksums
                or match[2] not in assets or assets[match[2]]['digest'] != 'sha256:' + match[1]):
            raise TransferError('Actions cleanup checksum list differs from its native assets')
        checksums[match[2]] = match[1]
    if set(checksums) != set(assets) - {'SHA256SUMS.txt'}:
        raise TransferError('Actions cleanup checksum list is incomplete')
    if file_digest(directory / 'SHA256SUMS.txt') != assets['SHA256SUMS.txt']['digest'][7:]:
        raise TransferError('Actions cleanup checksum file changed during verification')
    return assets


def validate_actions_run(run: dict, expected: Expected) -> None:
    if (not isinstance(run, dict) or type(run.get('id')) is not int or run['id'] != expected.run_id
            or type(run.get('run_attempt')) is not int or run['run_attempt'] != expected.run_attempt
            or run.get('head_sha') != expected.head_sha or not isinstance(run.get('head_repository'), dict)
            or str(run['head_repository'].get('full_name', '')).lower() != expected.repository.lower()
            or run.get('status') not in ('in_progress', 'completed')
            or (run.get('status') == 'completed' and run.get('conclusion') != 'success')):
        raise TransferError('Actions cleanup run repository, ID, attempt, head or state does not match')


def validate_actions_assets(expected: Expected, assets: dict[str, dict], api: GitHub) -> None:
    run_path = '/actions/runs/' + str(expected.run_id)
    validate_actions_run(api.json(run_path), expected)
    artifacts, total = [], None
    for page in range(1, 12):
        document = api.json(run_path + f'/artifacts?per_page=100&page={page}')
        batch = document.get('artifacts') if isinstance(document, dict) else None
        count = document.get('total_count') if isinstance(document, dict) else None
        if (not isinstance(batch, list) or len(batch) > 100 or type(count) is not int
                or not 0 <= count <= MAX_PUBLIC_ASSETS or (total is not None and count != total)
                or any(not isinstance(item, dict) for item in batch)):
            raise TransferError('Actions cleanup artifact inventory is invalid or changed')
        total = count
        artifacts.extend(batch)
        if len(artifacts) > total:
            raise TransferError('Actions cleanup artifact inventory exceeds its reported count')
        if len(batch) < 100:
            break
    else:
        raise TransferError('Actions cleanup artifact pagination did not finish')
    ids = [item.get('id') for item in artifacts]
    if (len(artifacts) != total or any(type(value) is not int or value <= 0 for value in ids)
            or len(ids) != len(set(ids))):
        raise TransferError('Actions cleanup artifact inventory is incomplete or duplicated')
    for name, asset in assets.items():
        matches = [item for item in artifacts if item.get('name') == name]
        if (len(matches) != 1 or matches[0].get('expired') is not False
                or type(matches[0].get('size_in_bytes')) is not int
                or matches[0]['size_in_bytes'] != asset['size'] or matches[0].get('digest') != asset['digest']):
            raise TransferError('Actions cleanup original file is missing or has the wrong size/SHA256: ' + name)
    for item in artifacts:
        name = item.get('name')
        if (isinstance(name, str) and (name.endswith(('.apk', '.img', '-AnyKernel3.zip')) or name == 'SHA256SUMS.txt')
                and name not in assets):
            raise TransferError('Actions cleanup found a native file outside its verified asset inventory')
    # Failed-job reruns can start while files are being checked. Never use the
    # previous attempt's uploaded files to unlock deletion during a new attempt.
    validate_actions_run(api.json(run_path), expected)


def delete_actions(release_id: int, assets_directory: Path, expected: Expected, api: GitHub) -> dict:
    expected.validate()
    release_id = integer(release_id, 'backup release ID')
    assets = native_assets(assets_directory)
    validate_actions_assets(expected, assets, api)
    release = api.json('/releases/' + str(release_id), missing=True)
    if release is None:
        return {'backup_release_id': release_id, 'deleted': False, 'already_absent': True}
    # A matching public Release is never a deletion target. The complete
    # existing ownership/source check must identify this independent draft.
    validate_backup(release, expected, release_id)
    api.json('/releases/' + str(release_id), method='DELETE')
    return {'backup_release_id': release_id, 'deleted': True, 'verified_actions_files': len(assets)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    for name in ('create', 'restore', 'delete', 'delete-actions'):
        command = commands.add_parser(name)
        command.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''))
        command.add_argument('--run-id', default=os.environ.get('GITHUB_RUN_ID', ''))
        command.add_argument('--run-attempt', default=os.environ.get('GITHUB_RUN_ATTEMPT', ''))
        command.add_argument('--head-sha', default=os.environ.get('GITHUB_SHA', ''))
        command.add_argument('--expected-ref', required=True)
        if name == 'create':
            command.add_argument('--source', required=True, type=Path)
        else:
            command.add_argument('--release-id', required=True)
        if name == 'restore':
            command.add_argument('--output', required=True, type=Path)
            command.add_argument('--prepared-identity', help='Prepare-bound identity enabling exact official recovery')
            command.add_argument('--root', type=Path, default=ROOT)
            command.add_argument('--susfs', choices=('true', 'false'), default='false')
            command.add_argument('--tier', default='Custom(自定义)')
        if name == 'delete':
            command.add_argument('--published-tag', required=True)
        if name == 'delete-actions':
            command.add_argument('--assets-directory', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        expected = Expected(args.repository, integer(args.run_id, 'workflow run ID'),
                            integer(args.run_attempt, 'workflow attempt'), args.head_sha, args.expected_ref)
        expected.validate()
        api = GitHub(expected.repository)
        if args.operation == 'create':
            result = create(args.source, expected, api)
        elif args.operation == 'restore':
            identity = (json.loads(args.prepared_identity, object_pairs_hook=unique_object)
                        if args.prepared_identity is not None else None)
            result = restore(args.release_id, args.output, expected, api, identity=identity,
                             root=args.root, susfs=args.susfs == 'true', tier=args.tier)
        elif args.operation == 'delete':
            result = delete(args.release_id, args.published_tag, expected, api)
        else:
            result = delete_actions(args.release_id, args.assets_directory, expected, api)
        if args.operation == 'create' and os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as output:
                for key, value in result.items():
                    output.write(f'{key}={value}\n')
    except (OSError, ValueError, UnicodeError) as error:
        message = str(error)
        for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
            token = os.environ.get(key, '')
            if token:
                message = message.replace(token, '[REDACTED]')
        print('::error::SukiSU source backup stopped: ' + message, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
