#!/usr/bin/env python3
"""Apply and verify the audited BakaSU KPM port without changing Git identity."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import uuid

from snapshot_ksu_source import temporary_directory

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'Baka-SU/BakaSU'
AUDITED_REF = 'd5e9e025b8b49155f8014684d64c91742b465f65'
RECEIPT = '.gki-bakasu-kpm-port.json'
API = {'feature_id': 7, 'ioctl': 0xc0204bc9, 'bridge_abi': 2, 'command_size': 32}
TREES = ('kernel', 'uapi', 'userspace', 'manager')
MAX_FILE = 4 * 1024 * 1024
MAX_PATCH = 16 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}\Z')
REF = re.compile(r'[0-9a-f]{40}\Z')


class KpmPortError(ValueError):
    """A locally constructed port-validation diagnostic."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def regular(path: Path, maximum: int) -> bytes:
    if is_link(path) or not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > maximum:
        raise KpmPortError('Expected a bounded regular BakaSU KPM input')
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise KpmPortError('BakaSU KPM input exceeds its bounded size')
    return data


def strict_json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise KpmPortError('Duplicate BakaSU KPM metadata field')
            result[key] = value
        return result
    def reject(_value):
        raise KpmPortError('Non-finite BakaSU KPM metadata number')
    result = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=reject)
    if not isinstance(result, dict):
        raise KpmPortError('BakaSU KPM metadata must be an object')
    return result


def path_name(value: str, *, patch: bool = True) -> str:
    if (not isinstance(value, str) or not value or '\\' in value
            or not re.fullmatch(r'[A-Za-z0-9_./+-]+', value)
            or any(part in ('', '.', '..') for part in value.split('/'))
            or PurePosixPath(value).is_absolute()):
        raise KpmPortError('Unsafe BakaSU KPM relative path')
    parts = value.split('/')
    if parts[0] not in TREES or any(part.casefold() in ('.git', 'agents.md')
            or part.casefold().startswith('readme') for part in parts):
        raise KpmPortError('BakaSU KPM path is outside its reviewed source trees')
    if patch and (len(parts) < 2 or PurePosixPath(value).suffix not in ('.c', '.h', '.rs', '.kt', '.xml')
                  and parts[-1] not in ('Kbuild', 'Kconfig', 'Makefile')):
        raise KpmPortError('BakaSU KPM patch may contain only reviewed code and resources')
    return value


def git(source: Path, *arguments: str, check: bool = True) -> bytes:
    try:
        result = subprocess.run(['git', '--no-replace-objects', '-c', 'core.autocrlf=false',
            '-C', str(source), *arguments], capture_output=True, timeout=60,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1'})
    except (OSError, subprocess.TimeoutExpired):
        raise KpmPortError('Cannot verify the local frozen BakaSU Git source') from None
    if check and result.returncode:
        raise KpmPortError('Exact BakaSU KPM Git validation failed')
    return result.stdout if check else bytes((result.returncode,))


def load_port_manifest() -> tuple[dict, bytes]:
    manifest = strict_json(regular(ROOT / 'config/bakasu-kpm-port.json', 131072))
    if (set(manifest) != {'schema_version', 'repository', 'audited_ref', 'patch', 'files', 'api'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
            or manifest['repository'] != REPOSITORY or manifest['audited_ref'] != AUDITED_REF
            or manifest['api'] != API or any(type(value) is not int for value in manifest['api'].values())):
        raise KpmPortError('Invalid audited BakaSU KPM port identity or additive API')
    spec = manifest['patch']
    if (not isinstance(spec, dict) or set(spec) != {'filename', 'size', 'sha256'}
            or spec['filename'] != 'bakasu-kpm.patch' or type(spec['size']) is not int
            or not 0 < spec['size'] <= MAX_PATCH or not isinstance(spec['sha256'], str)
            or not SHA.fullmatch(spec['sha256'])):
        raise KpmPortError('Invalid fixed BakaSU KPM patch lock')
    files = manifest['files']
    if not isinstance(files, list) or not 1 <= len(files) <= 256:
        raise KpmPortError('Invalid BakaSU KPM file inventory')
    paths = set()
    for item in files:
        if (not isinstance(item, dict)
                or set(item) != {'path', 'kind', 'before_sha256', 'after_sha256', 'after_size'}):
            raise KpmPortError('Invalid BakaSU KPM file identity')
        name = path_name(item['path'])
        if (name in paths or item['kind'] not in ('new', 'update')
                or type(item['after_size']) is not int or not 0 < item['after_size'] <= MAX_FILE
                or not isinstance(item['after_sha256'], str) or not SHA.fullmatch(item['after_sha256'])
                or (item['kind'] == 'new' and item['before_sha256'] is not None)
                or (item['kind'] == 'update' and (not isinstance(item['before_sha256'], str)
                                                or not SHA.fullmatch(item['before_sha256'])))):
            raise KpmPortError('Invalid or duplicate BakaSU KPM file digest')
        paths.add(name)
    patch = regular(ROOT / 'tools/patches' / spec['filename'], MAX_PATCH)
    if len(patch) != spec['size'] or digest(patch) != spec['sha256']:
        raise KpmPortError('BakaSU KPM patch failed its fixed size/SHA256 verification')
    validate_patch(patch, files)
    return manifest, patch


def validate_patch(patch: bytes, files: list[dict]) -> None:
    if b'\0' in patch:
        raise KpmPortError('Binary BakaSU KPM patches are forbidden')
    text = patch.decode('utf-8')
    chunks = re.split(r'(?=^diff --git )', text, flags=re.M)
    if chunks[0] != '':
        raise KpmPortError('BakaSU KPM patch has unexpected leading metadata')
    expected = {item['path']: item for item in files}
    seen = set()
    for chunk in chunks[1:]:
        lines = chunk.splitlines()
        match = re.fullmatch(r'diff --git a/(\S+) b/(\S+)', lines[0])
        if match is None or match[1] != match[2]:
            raise KpmPortError('Renamed or quoted BakaSU KPM patch paths are forbidden')
        name = path_name(match[1])
        if name not in expected or name in seen:
            raise KpmPortError('BakaSU KPM patch paths differ from its fixed inventory')
        seen.add(name)
        hunk = next((index for index, line in enumerate(lines) if line.startswith('@@ ')), None)
        if hunk is None:
            raise KpmPortError('BakaSU KPM source patch needs real text hunks')
        header = lines[1:hunk]
        new = expected[name]['kind'] == 'new'
        allowed = {f'--- {"/dev/null" if new else "a/" + name}', f'+++ b/{name}'}
        if new:
            allowed.add('new file mode 100644')
        for line in header:
            if line in allowed or re.fullmatch(r'index [0-9a-f]{7,40}\.\.[0-9a-f]{7,40}(?: 100644)?', line):
                continue
            raise KpmPortError('Binary, renamed, deleted or mode-changing BakaSU KPM patches are forbidden')
        if (header.count(f'--- {"/dev/null" if new else "a/" + name}') != 1
                or header.count(f'+++ b/{name}') != 1
                or header.count('new file mode 100644') != int(new)):
            raise KpmPortError('BakaSU KPM patch kind does not match its file inventory')
        if any(not line.startswith((' ', '+', '-', '@@ '))
               and line != '\\ No newline at end of file' for line in lines[hunk:]):
            raise KpmPortError('Unexpected BakaSU KPM text patch instruction')
    if seen != set(expected):
        raise KpmPortError('BakaSU KPM patch is incomplete')


def checked_source(source: Path, reference: str) -> tuple[Path, str]:
    source = Path(source).absolute()
    if (not isinstance(reference, str) or not REF.fullmatch(reference) or not source.is_dir()
            or any(is_link(path) for path in (source, *source.parents))):
        raise KpmPortError('BakaSU KPM requires a real frozen Git source directory')
    if Path(os.fsdecode(git(source, 'rev-parse', '--show-toplevel')).strip()).resolve() != source.resolve():
        raise KpmPortError('BakaSU KPM source is not its own canonical Git root')
    if os.fsdecode(git(source, 'rev-parse', '--verify', 'HEAD')).strip() != reference:
        raise KpmPortError('BakaSU KPM source HEAD differs from the frozen reference')
    origin = os.fsdecode(git(source, 'remote', 'get-url', 'origin')).strip()
    if origin not in (f'https://github.com/{REPOSITORY}', f'https://github.com/{REPOSITORY}.git'):
        raise KpmPortError('BakaSU KPM source origin must be the official BakaSU repository')
    tree = os.fsdecode(git(source, 'rev-parse', reference + '^{tree}')).strip()
    if not REF.fullmatch(tree):
        raise KpmPortError('Invalid frozen BakaSU source tree')
    return source, tree


def safe_path(source: Path, relative: str) -> Path:
    path = source / relative
    for item in (path, *path.parents):
        if item == source:
            break
        if is_link(item):
            raise KpmPortError('BakaSU KPM destination traverses a symlink or junction')
    return path


def head_file(source: Path, reference: str, relative: str) -> bytes | None:
    records = git(source, 'ls-tree', '-z', reference, '--', relative).split(b'\0')
    records = [record for record in records if record]
    if not records:
        return None
    if len(records) != 1:
        raise KpmPortError('Ambiguous frozen BakaSU KPM source file')
    metadata, name = records[0].split(b'\t', 1)
    if name.decode('utf-8') != relative or not re.fullmatch(rb'100644 blob [0-9a-f]{40}', metadata):
        raise KpmPortError('BakaSU KPM source input must be a regular text Git blob')
    data = git(source, 'show', reference + ':' + relative)
    if len(data) > MAX_FILE or b'\0' in data:
        raise KpmPortError('Invalid bounded frozen BakaSU KPM source file')
    data.decode('utf-8')
    return data


def scopes(scope) -> tuple[str, ...]:
    if scope is None:
        return TREES
    values = (scope,) if isinstance(scope, str) else tuple(scope)
    if not values:
        raise KpmPortError('BakaSU KPM verification scope cannot be empty')
    return tuple(sorted({path_name(value, patch=False) for value in values}))


def changes(source: Path, scope: tuple[str, ...]) -> set[str]:
    tracked = git(source, 'diff', '--no-ext-diff', '--name-only', '--no-renames', 'HEAD', '-z', '--', *scope)
    # Ignored source files can still be included by Kbuild; do not let a
    # .gitignore rule hide an additional driver or manager trust asset.
    untracked = git(source, 'ls-files', '--others', '-z', '--', *scope)
    modified = {item.decode('utf-8') for item in tracked.split(b'\0') if item}
    for item in untracked.split(b'\0'):
        if not item:
            continue
        name = item.decode('utf-8')
        # Native DDK compilation writes these outputs beside kernel sources.
        # Never exempt tracked changes, arbitrary new C code, or symlinks.
        leaf = name.rsplit('/', 1)[-1]
        generated = (name == 'kernel/kernelsu.mod.c' or name in ('kernel/Module.symvers', 'kernel/modules.order')
                     or name.startswith('kernel/') and re.fullmatch(r'[A-Za-z0-9_.+-]+\.(?:o|ko|mod|cmd|d)', leaf))
        if generated:
            path = safe_path(source, name)
            if path.is_file() and not is_link(path):
                continue
        modified.add(name)
    return modified


def expected_port(source: Path, reference: str, manifest: dict, patch: bytes) -> tuple[dict, dict]:
    before = {}
    with temporary_directory(source.parent, '.bakasu-kpm-proof-') as scratch:
        git(scratch, 'init', '-q')
        for item in manifest['files']:
            name = item['path']
            data = head_file(source, reference, name)
            if (item['kind'] == 'new') != (data is None):
                raise KpmPortError('BakaSU KPM port overlaps an upstream feature; review before updating')
            before[name] = data
            if data is not None:
                destination = scratch / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        prepared = scratch / 'reviewed.patch'
        prepared.write_bytes(patch)
        git(scratch, 'apply', '--check', str(prepared))
        git(scratch, 'apply', '--whitespace=nowarn', str(prepared))
        git(scratch, 'apply', '--reverse', '--check', str(prepared))
        after = {}
        for item in manifest['files']:
            name = item['path']
            data = regular(safe_path(scratch, name), MAX_FILE)
            if b'\0' in data:
                raise KpmPortError('BakaSU KPM port generated a binary file')
            data.decode('utf-8')
            if (item['kind'] == 'new' or digest(before[name]) == item['before_sha256']):
                if digest(data) != item['after_sha256'] or len(data) != item['after_size']:
                    raise KpmPortError('BakaSU KPM patch output differs from its audited file lock')
            after[name] = data
    return before, after


def receipt_for(reference: str, tree: str, manifest: dict, before: dict, after: dict) -> dict:
    result = {'schema_version': 1, 'repository': REPOSITORY, 'source_ref': reference,
              'source_tree': tree, 'audited_ref': manifest['audited_ref'], 'patch': manifest['patch'],
              'api': API, 'files': {name: {'before_sha256': digest(before[name]) if before[name] is not None else None,
                  'after_sha256': digest(data), 'after_size': len(data)} for name, data in sorted(after.items())}}
    result['port_sha256'] = digest(json.dumps(result, sort_keys=True, separators=(',', ':')).encode())
    return result


def check_applied(source: Path, before: dict, after: dict, scope: tuple[str, ...]) -> None:
    for name, data in after.items():
        if regular(safe_path(source, name), MAX_FILE) != data:
            raise KpmPortError('BakaSU KPM source differs from its exact audited port: ' + name)
    expected = {name for name in after if before[name] != after[name]
                and any(name == prefix or name.startswith(prefix + '/') for prefix in scope)}
    if changes(source, scope) != expected:
        raise KpmPortError('Additional source changes are outside the exact BakaSU KPM port')
    # Index edits are never part of this working-tree-only port.
    if git(source, 'diff', '--cached', '--name-only', 'HEAD', '-z', '--', *scope):
        raise KpmPortError('BakaSU KPM port must not alter or conceal staged source changes')


def verify_bakasu_kpm_port(source: Path, reference: str, *, scope=None) -> dict:
    source, tree = checked_source(source, reference)
    manifest, patch = load_port_manifest()
    before, after = expected_port(source, reference, manifest, patch)
    receipt = strict_json(regular(safe_path(source, RECEIPT), 262144))
    expected = receipt_for(reference, tree, manifest, before, after)
    if json.dumps(receipt, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise KpmPortError('BakaSU KPM receipt differs from the actual frozen source and port')
    check_applied(source, before, after, scopes(scope))
    with temporary_directory(source.parent, '.bakasu-kpm-reverse-') as scratch:
        prepared = scratch / 'reviewed.patch'
        prepared.write_bytes(patch)
        git(source, 'apply', '--reverse', '--check', str(prepared))
    return expected


def restore_bakasu_kpm(source: Path, reference: str) -> dict:
    source, tree = checked_source(source, reference)
    if (source / RECEIPT).exists() or is_link(source / RECEIPT):
        return verify_bakasu_kpm_port(source, reference)
    manifest, patch = load_port_manifest()
    before, after = expected_port(source, reference, manifest, patch)
    if changes(source, TREES):
        raise KpmPortError('BakaSU KPM port requires clean, unmodified source trees')
    for name, data in before.items():
        path = safe_path(source, name)
        if (data is None and (path.exists() or is_link(path))) or (data is not None and regular(path, MAX_FILE) != data):
            raise KpmPortError('BakaSU KPM source changed before its atomic preflight')
    result = receipt_for(reference, tree, manifest, before, after)
    receipt = source / RECEIPT
    temporary = source / ('.gki-bakasu-kpm-port-' + uuid.uuid4().hex)
    published = False
    try:
        with temporary_directory(source.parent, '.bakasu-kpm-apply-') as scratch:
            prepared = scratch / 'reviewed.patch'
            prepared.write_bytes(patch)
            git(source, 'apply', '--check', str(prepared))
            git(source, 'apply', '--whitespace=nowarn', str(prepared))
            git(source, 'apply', '--reverse', '--check', str(prepared))
        check_applied(source, before, after, TREES)
        with temporary.open('xb') as stream:
            stream.write((json.dumps(result, sort_keys=True) + '\n').encode())
        os.link(temporary, receipt)
        published = True
    except (OSError, ValueError, UnicodeError):
        failed = False
        for name, original in before.items():
            try:
                path = safe_path(source, name)
                current = regular(path, MAX_FILE) if path.exists() else None
                if current not in (original, after[name]):
                    failed = True
                    continue
                if original is None:
                    if path.exists():
                        path.unlink()
                elif current != original:
                    path.write_bytes(original)
            except (OSError, ValueError):
                failed = True
        if published:
            receipt.unlink(missing_ok=True)
        if failed:
            raise KpmPortError('BakaSU KPM failed; independently changed files were preserved during rollback') from None
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--reference', required=True)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--scope', action='append')
    args = parser.parse_args(argv)
    try:
        result = (verify_bakasu_kpm_port(args.source, args.reference, scope=args.scope) if args.verify
                  else restore_bakasu_kpm(args.source, args.reference))
    except (OSError, ValueError, UnicodeError):
        print('BakaSU KPM port verification failed; the frozen source is not safe to publish.')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
