#!/usr/bin/env python3
"""Recover exact official SUSFS objects through GitLab's archive API.

The API archive never becomes a fabricated upstream commit. Its original
commit bytes must reproduce the official SHA before an independent carrier
commit may transport them alongside the exact source tree. Only a new SUSFS
consumer is marked shallow; the manager's Git repository remains complete.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from snapshot_ksu_source import SnapshotError, temporary_directory


API = 'https://gitlab.com/api/v4/projects/56471372'
SHA = re.compile(r'[0-9a-f]{40}\Z')
TARGET = re.compile(r'android[0-9]{1,2}-[0-9]{1,2}\.[0-9]{1,2}\Z')
MAX_JSON = 2 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_FILES = 10000
MAX_COMMIT = 1024 * 1024
CARRIER_IDENTITY = b'GKI official SUSFS archive <archive@gki.invalid> 0 +0000'
CARRIER_MESSAGE = b'Official SUSFS archive carrier\n'
FIELDS = {'ref', 'tree', 'date', 'version', 'branch', 'transport_ref'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def git_bytes(directory: Path, *arguments: str, data: bytes | None = None) -> bytes:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('GIT_CONFIG_') and key not in {
                       'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                       'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_SHALLOW_FILE'}}
    environment.update(GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1')
    try:
        result = subprocess.run(
            ['git', '-C', str(directory), '-c', 'core.hooksPath=', '-c', 'core.autocrlf=false',
             '-c', 'core.filemode=true', *arguments], input=data, capture_output=True,
            timeout=180, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SnapshotError('Official SUSFS archive Git operation could not complete') from error
    if result.returncode:
        raise SnapshotError(f'Official SUSFS archive Git {arguments[0]} failed (exit {result.returncode})')
    return result.stdout


def git(directory: Path, *arguments: str, data: bytes | None = None) -> str:
    return git_bytes(directory, *arguments, data=data).decode('utf-8', errors='strict').strip()


def full_sha(value: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise SnapshotError('Official SUSFS identity requires a full lowercase commit SHA')
    return value


def target_name(value: str) -> str:
    if not isinstance(value, str) or not TARGET.fullmatch(value):
        raise SnapshotError('Invalid official SUSFS archive target')
    return value


def request_bytes(url: str, limit: int, *, opener=None) -> bytes:
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'gitlab.com'
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment or not url.startswith(API + '/repository/')):
        raise SnapshotError('Unexpected official SUSFS archive HTTP destination')
    opener = opener or build_opener(NoRedirect())
    for attempt in range(3):
        request = Request(url, headers={'User-Agent': 'gki-official-susfs-archive',
                                       'Accept': 'application/octet-stream'})
        try:
            with opener.open(request, timeout=60) as response:
                if response.status != 200:
                    raise SnapshotError('Official SUSFS archive returned a non-200 response')
                if response.geturl() != url:
                    raise SnapshotError('Official SUSFS archive unexpectedly redirected')
                declared = response.headers.get('Content-Length')
                if declared is not None and (not declared.isdigit() or int(declared) > limit):
                    raise SnapshotError('Official SUSFS archive HTTP response exceeds its limit')
                output = bytearray()
                while True:
                    block = response.read(min(1024 * 1024, limit + 1 - len(output)))
                    if not block:
                        return bytes(output)
                    output.extend(block)
                    if len(output) > limit:
                        raise SnapshotError('Official SUSFS archive HTTP response exceeds its limit')
        except HTTPError as error:
            code = error.code
            error.close()
            if code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise SnapshotError(f'Official SUSFS archive HTTP request failed (HTTP {code})') from None
        except (OSError, URLError, TimeoutError):
            if attempt == 2:
                raise SnapshotError('Official SUSFS archive HTTP transport failed after three attempts') from None
        if attempt < 2:
            time.sleep((5, 10)[attempt])
    raise SnapshotError('Official SUSFS archive HTTP transport failed')


def unique_object(items):
    document = {}
    for key, value in items:
        if key in document:
            raise SnapshotError('Duplicate official SUSFS commit metadata field')
        document[key] = value
    return document


def api_commit(requested: str, target: str) -> dict:
    target_name(target)
    if not isinstance(requested, str):
        raise SnapshotError('Invalid requested official SUSFS archive ref')
    if requested.startswith('refs/heads/'):
        requested = requested[len('refs/heads/'):]
    if requested != 'gki-' + target and not re.fullmatch(r'[0-9a-fA-F]{7,40}', requested):
        raise SnapshotError('Official SUSFS archive requires its target branch or hexadecimal commit')
    url = API + '/repository/commits/' + quote(requested, safe='')
    try:
        document = json.loads(request_bytes(url, MAX_JSON), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as error:
        raise SnapshotError('Official SUSFS commit API returned invalid JSON') from error
    if not isinstance(document, dict):
        raise SnapshotError('Official SUSFS commit API returned invalid metadata')
    reference = full_sha(document.get('id'))
    if re.fullmatch(r'[0-9a-fA-F]{7,40}', requested) and not reference.startswith(requested.lower()):
        raise SnapshotError('Official SUSFS commit API resolved a different requested commit')
    return document


def identity(name: str, email: str, date: str) -> bytes:
    if (not isinstance(name, str) or not name or not isinstance(email, str) or not email
            or any(char in name + email for char in '\x00\r\n<>')):
        raise SnapshotError('Official SUSFS commit has an unsupported author/committer identity')
    parsed = iso_date(date)
    return (name + ' <' + email + '> ' + str(int(parsed.timestamp())) + ' '
            + parsed.strftime('%z')).encode('utf-8')


def iso_date(value: str) -> datetime:
    if (not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.0+)?(?:Z|[+-]\d{2}:\d{2})', value)):
        raise SnapshotError('Official SUSFS commit has an unsupported timestamp')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise SnapshotError('Official SUSFS commit has an invalid timestamp') from error
    if result.tzinfo is None or result.microsecond:
        raise SnapshotError('Official SUSFS commit timestamp is not an exact Git timestamp')
    return result


def original_commit(document: dict, tree: str) -> bytes:
    full_sha(tree)
    full_sha(document.get('id'))
    parents = document.get('parent_ids')
    message = document.get('message')
    if (not isinstance(parents, list) or len(parents) > 20
            or any(not isinstance(parent, str) or not SHA.fullmatch(parent) for parent in parents)
            or len(set(parents)) != len(parents)
            or not isinstance(message, str) or '\x00' in message):
        raise SnapshotError('Official SUSFS commit has unsupported parent/message metadata')
    lines = [b'tree ' + tree.encode('ascii')]
    lines += [b'parent ' + full_sha(parent).encode('ascii') for parent in parents]
    lines.append(b'author ' + identity(document.get('author_name'), document.get('author_email'),
                                     document.get('authored_date')))
    lines.append(b'committer ' + identity(document.get('committer_name'), document.get('committer_email'),
                                        document.get('committed_date')))
    raw = b'\n'.join(lines) + b'\n\n' + message.encode('utf-8')
    if len(raw) > MAX_COMMIT:
        raise SnapshotError('Official SUSFS commit exceeds its size limit')
    return raw


def extract_archive(raw: bytes, output: Path) -> dict[str, bool]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ARCHIVE:
        raise SnapshotError('Official SUSFS archive is empty or exceeds its size limit')
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise SnapshotError('Official SUSFS extraction requires a new output directory')
    output.mkdir()
    names, files, prefix, expanded, count = set(), {}, None, 0, 0
    try:
        # Bound gzip expansion before tarfile parses PAX/long-name headers.
        # A malicious metadata header must not bypass per-file size accounting.
        tar_limit = MAX_EXPANDED + MAX_FILES * 4096 + 1024 * 1024
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
            tar_bytes = compressed.read(tar_limit + 1)
        if len(tar_bytes) > tar_limit:
            raise SnapshotError('Official SUSFS archive tar stream exceeds its size limit')
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode='r:') as archive:
            for member in archive:
                count += 1
                if count > MAX_FILES:
                    raise SnapshotError('Official SUSFS archive contains too many entries')
                name = member.name.rstrip('/')
                if (not name or name.startswith('/') or '\\' in name
                        or any(ord(char) < 32 or ord(char) == 127 for char in name)):
                    raise SnapshotError('Official SUSFS archive contains an unsafe path')
                parts = name.split('/')
                if (any(part in ('', '.', '..') or part.casefold() == '.git' for part in parts)
                        or any(any(char in part for char in '<>:"|?*') for part in parts)
                        or any(part.endswith((' ', '.')) for part in parts)):
                    raise SnapshotError('Official SUSFS archive contains an unsafe path component')
                if prefix is None:
                    prefix = parts[0]
                if parts[0] != prefix:
                    raise SnapshotError('Official SUSFS archive contains multiple root directories')
                relative = '/'.join(parts[1:])
                key = relative.casefold()
                if key in names:
                    raise SnapshotError('Official SUSFS archive contains duplicate paths')
                names.add(key)
                if not member.isdir() and not member.isfile():
                    raise SnapshotError('Official SUSFS archive contains a symlink or special entry')
                if not relative:
                    if not member.isdir():
                        raise SnapshotError('Official SUSFS archive root is not a directory')
                    continue
                destination = output.joinpath(*PurePosixPath(relative).parts)
                if not destination.resolve().is_relative_to(output.resolve()):
                    raise SnapshotError('Official SUSFS archive escapes its extraction directory')
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if not 0 <= member.size <= MAX_FILE:
                    raise SnapshotError('Official SUSFS archive file exceeds its size limit')
                expanded += member.size
                if expanded > MAX_EXPANDED:
                    raise SnapshotError('Official SUSFS expanded archive exceeds its size limit')
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    raise SnapshotError('Official SUSFS archive file path was already created')
                stream = archive.extractfile(member)
                if stream is None:
                    raise SnapshotError('Official SUSFS archive file content is missing')
                content = stream.read(MAX_FILE + 1)
                if len(content) != member.size:
                    raise SnapshotError('Official SUSFS archive file content length differs')
                destination.write_bytes(content)
                executable = bool(member.mode & 0o111)
                destination.chmod(0o755 if executable else 0o644)
                files[relative] = executable
        if not files:
            raise SnapshotError('Official SUSFS archive contains no source files')
    except (tarfile.TarError, OSError, ValueError, EOFError) as error:
        if isinstance(error, SnapshotError):
            raise
        raise SnapshotError('Official SUSFS archive could not be safely extracted') from error
    return files


def carrier_bytes(tree: str) -> bytes:
    return (b'tree ' + full_sha(tree).encode('ascii') + b'\nauthor ' + CARRIER_IDENTITY
            + b'\ncommitter ' + CARRIER_IDENTITY + b'\n\n' + CARRIER_MESSAGE)


def make_carrier(source: Path, files: dict[str, bool], document: dict) -> dict:
    git(source, 'init', '--quiet')
    # Higher-priority local attributes prevent the archived .gitattributes from
    # normalizing bytes or invoking filters while recreating its original tree.
    (source / '.git/info/attributes').write_text('* -text -filter -ident -working-tree-encoding\n', encoding='ascii')
    git(source, 'add', '--force', '--all', '--', '.')
    for name, executable in files.items():
        git(source, 'update-index', '--chmod=+x' if executable else '--chmod=-x', '--', name)
    tree = full_sha(git(source, 'write-tree'))
    raw = original_commit(document, tree)
    reference = full_sha(document['id'])
    if git(source, 'hash-object', '-t', 'commit', '--stdin', data=raw) != reference:
        raise SnapshotError('Official SUSFS archive cannot reproduce the original commit SHA; refusing altered source')
    blob = git(source, 'hash-object', '-w', '--stdin', data=raw)
    root = git(source, 'mktree', data=(f'100644 blob {blob}\tcommit\n040000 tree {tree}\tsource\n').encode('ascii'))
    carrier = git(source, 'hash-object', '-w', '-t', 'commit', '--stdin', data=carrier_bytes(root))
    return {'ref': reference, 'tree': tree, 'transport_ref': full_sha(carrier)}


def carrier_contents(source: Path, carrier: str, reference: str) -> tuple[str, bytes]:
    carrier, reference = full_sha(carrier), full_sha(reference)
    raw_carrier = git_bytes(source, 'cat-file', 'commit', carrier)
    match = re.match(rb'tree ([0-9a-f]{40})\n', raw_carrier)
    if match is None or raw_carrier != carrier_bytes(match.group(1).decode('ascii')):
        raise SnapshotError('Official SUSFS carrier is not the expected independent transport commit')
    tree = match.group(1).decode('ascii')
    entries = {}
    for entry in git_bytes(source, 'ls-tree', '-z', tree).split(b'\x00'):
        if not entry:
            continue
        info, name = entry.split(b'\t', 1)
        if name in entries:
            raise SnapshotError('Official SUSFS carrier has duplicate entries')
        entries[name] = info.split()
    if set(entries) != {b'commit', b'source'}:
        raise SnapshotError('Official SUSFS carrier has unexpected entries')
    if (entries[b'commit'][:2] != [b'100644', b'blob']
            or entries[b'source'][:2] != [b'040000', b'tree']):
        raise SnapshotError('Official SUSFS carrier has invalid object modes')
    commit_blob = entries[b'commit'][2].decode('ascii')
    if not 0 < int(git(source, 'cat-file', '-s', commit_blob)) <= MAX_COMMIT:
        raise SnapshotError('Official SUSFS carrier commit metadata exceeds its limit')
    raw = git_bytes(source, 'cat-file', 'blob', commit_blob)
    original_tree = entries[b'source'][2].decode('ascii')
    if (git(source, 'hash-object', '-t', 'commit', '--stdin', data=raw) != reference
            or not raw.startswith(b'tree ' + original_tree.encode('ascii') + b'\n')):
        raise SnapshotError('Official SUSFS carrier differs from the original commit SHA/tree')
    return original_tree, raw


def carrier_metadata(source: Path, target: str, carrier: str, expected_ref: str) -> dict:
    target_name(target)
    tree, raw = carrier_contents(source, carrier, expected_ref)
    try:
        header, _ = raw.split(b'\n\n', 1)
        committers = [line for line in header.splitlines() if line.startswith(b'committer ')]
        if len(committers) != 1:
            raise ValueError('committer')
        match = re.fullmatch(rb'committer [^\r\n<>]+ <[^\r\n<>]+> (-?[0-9]+) ([+-])([0-9]{2})([0-9]{2})', committers[0])
        if match is None:
            raise ValueError('committer')
        hours, minutes = int(match[3]), int(match[4])
        if hours > 23 or minutes > 59:
            raise ValueError('offset')
        offset = timedelta(hours=hours, minutes=minutes) * (1 if match[2] == b'+' else -1)
        date = datetime.fromtimestamp(int(match[1]), timezone(offset)).strftime('%Y-%m-%d %H:%M:%S %z')
    except (ValueError, OverflowError, OSError) as error:
        raise SnapshotError('Official SUSFS carrier has invalid original commit metadata') from error
    header = git_bytes(source, 'cat-file', 'blob', tree + ':kernel_patches/include/linux/susfs.h').decode('utf-8')
    versions = re.findall(r'^\s*#define\s+SUSFS_VERSION\s+"(v[0-9]+\.[0-9]+\.[0-9]+)"', header, re.M)
    if len(versions) != 1 or git(source, 'cat-file', '-t', tree + ':kernel_patches/50_add_susfs_in_gki-' + target + '.patch') != 'blob':
        raise SnapshotError('Official SUSFS archive is missing its declared version or target patch')
    return {'ref': full_sha(expected_ref), 'tree': tree, 'date': date, 'version': versions[0],
            'branch': 'gki-' + target, 'transport_ref': full_sha(carrier)}


def import_archive(source: Path, target: str, requested: str) -> dict:
    source = source.resolve(strict=True)
    target_name(target)
    before_head = git(source, 'rev-parse', '--verify', 'HEAD')
    if git(source, 'rev-parse', '--is-shallow-repository') != 'false':
        raise SnapshotError('Official SUSFS archive import requires the complete manager repository')
    document = api_commit(requested, target)
    reference = full_sha(document['id'])
    archive = request_bytes(API + '/repository/archive.tar.gz?sha=' + reference, MAX_ARCHIVE)
    with temporary_directory(source.parent, '.gki-official-susfs-') as temporary:
        prepared = temporary / 'source'
        files = extract_archive(archive, prepared)
        identity = make_carrier(prepared, files, document)
        metadata = carrier_metadata(prepared, target, identity['transport_ref'], reference)
        transport = 'refs/gki-susfs/' + target
        git(prepared, 'update-ref', transport, identity['transport_ref'])
        git(source, 'fetch', '--quiet', '--no-tags', '--no-recurse-submodules', str(prepared),
            '+' + transport + ':' + transport)
    if (git(source, 'rev-parse', '--verify', 'HEAD') != before_head
            or git(source, 'rev-parse', '--is-shallow-repository') != 'false'):
        raise SnapshotError('Official SUSFS archive import changed manager HEAD/history')
    return carrier_metadata(source, target, metadata['transport_ref'], reference)


def hydrate_carrier(source: Path, carrier: str, expected_map_item: dict) -> None:
    source = source.resolve(strict=True)
    if not isinstance(expected_map_item, dict) or set(expected_map_item) != FIELDS:
        raise SnapshotError('Official SUSFS carrier requires its exact prepared metadata')
    branch = expected_map_item.get('branch')
    if not isinstance(branch, str) or not branch.startswith('gki-'):
        raise SnapshotError('Official SUSFS carrier has no valid target')
    target = target_name(branch[4:])
    if carrier_metadata(source, target, carrier, expected_map_item.get('ref')) != expected_map_item:
        raise SnapshotError('Official SUSFS carrier differs from the successful prepare job')
    if git(source, 'rev-parse', '--is-bare-repository') != 'false':
        raise SnapshotError('Official SUSFS carrier hydration requires a new non-bare source repository')
    try:
        git(source, 'rev-parse', '--verify', 'HEAD')
    except SnapshotError:
        pass
    else:
        raise SnapshotError('Official SUSFS carrier hydration refuses an existing source HEAD')
    if not (source / '.git').is_dir() or (source / '.git').is_symlink():
        raise SnapshotError('Official SUSFS carrier hydration requires its own Git directory')
    shallow = source / '.git/shallow'
    if shallow.exists() or shallow.is_symlink():
        raise SnapshotError('Official SUSFS carrier hydration refuses an existing shallow boundary')
    tree, raw = carrier_contents(source, carrier, expected_map_item['ref'])
    (source / '.git/info/attributes').write_text('* -text -filter -ident -working-tree-encoding\n', encoding='ascii')
    if git(source, 'hash-object', '-w', '-t', 'commit', '--stdin', data=raw) != expected_map_item['ref']:
        raise SnapshotError('Official SUSFS original commit identity changed during hydration')
    shallow.write_text(expected_map_item['ref'] + '\n', encoding='ascii', newline='\n')
    if git(source, 'rev-parse', expected_map_item['ref'] + '^{tree}') != tree:
        raise SnapshotError('Official SUSFS original tree changed during hydration')
