#!/usr/bin/env python3
"""Restore the matched official locking component at its three native links.

The reviewed common kernels build oplus_locking.o and use locking_main.h
unconditionally. Their external vendor links are missing from a common-only
checkout. Materialize only those audited leaves from the paired official
modules commit; keep the native Makefile, scheduler and every option intact.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
LOCK_DIGEST = '3c017b0a9bf423c02594283be23096d175b527221e798db7c3c9b9940690b441'
DESTINATIONS = frozenset(('kernel/locking/oplus_locking.c', 'kernel/locking/locking_main.h',
                          'kernel/locking/sa_common_struct.h'))
PROFILES = frozenset(('sm8650-6.1.75', 'sm8650-6.1.118', 'sm8650-6.1.141',
                      'mt6897-6.1.75', 'mt6989-6.1.115', 'mt6897-6.1.128', 'mt6989-6.1.134'))
COMMON_HEADERS = frozenset(('linux/module.h', 'linux/init.h', 'linux/version.h', 'linux/sched/clock.h',
                           'linux/cgroup.h', 'linux/ftrace.h', 'linux/futex.h', 'linux/proc_fs.h',
                           'linux/seq_file.h', 'linux/errno.h', 'linux/slab.h', 'linux/spinlock.h',
                           'linux/rwsem.h', 'linux/rtmutex.h', 'linux/mutex.h', 'trace/hooks/dtask.h',
                           'trace/hooks/futex.h', 'trace/hooks/sched.h', 'trace/events/sched.h',
                           'trace/events/task.h', 'linux/sched.h'))
# The frozen native Makefile creates this before C compilation. It is not a
# source-tree file and must never be replaced by a synthesized vendor header.
GENERATED_HEADERS = frozenset(('linux/version.h',))
VERSION_HEADER_DECLARATION = b'version_h := include/generated/uapi/linux/version.h\n'
VERSION_HEADER_PRODUCER = b'$(version_h): FORCE\n\t$(call filechk,version.h)\n'


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob(data: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()


def _link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _read(path: Path, size: int) -> bytes:
    try:
        if _link(path) or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('Official locking input must be a regular file')
        with path.open('rb') as stream:
            data = stream.read(size + 1)
        if len(data) > size:
            raise ValueError('Official locking input exceeds its bound')
        return data
    except OSError:
        raise ValueError('Cannot read the required official locking input') from None


def load_locking_lock() -> dict:
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate official locking lock field')
            result[key] = value
        return result
    try:
        value = json.loads(_read(ROOT / 'config/oplus-vendor-locking.json', 32768), object_pairs_hook=unique)
        digest = _hash(json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8'))
    except (ValueError, UnicodeError):
        raise ValueError('Invalid fixed official locking source lock') from None
    if (digest != LOCK_DIGEST or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or set(value.get('profiles', {})) != PROFILES
            or any(set(item.get('files', {})) != DESTINATIONS for item in value['profiles'].values())):
        raise ValueError('Official locking source lock differs from its reviewed manifest')
    return value


def _profile(profile: dict, lock: dict) -> dict:
    selected = lock['profiles'][profile['id']]
    reference = profile.get('kernel_ref', profile.get('verified_kernel_ref'))
    if (profile.get('platform') != selected['platform'] or profile.get('kernel') != selected['kernel']
            or profile.get('kernel_repo') != selected.get('common_repository', lock['common_repository'])
            or reference != selected['common_commit']
            or profile.get('verified_kernel_ref', selected['common_commit']) != selected['common_commit']):
        raise ValueError('Official locking source pairing does not match this frozen profile')
    return selected


def _git(common: Path, *arguments: str) -> bytes:
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(common), *arguments],
                            capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('Cannot verify the official locking common Git snapshot')
    return result.stdout


def _native_blob(common: Path, commit: str, path: str) -> bytes:
    data = _git(common, 'cat-file', 'blob', commit + ':' + path)
    if not 0 < len(data) <= 1024 * 1024:
        raise ValueError('Official locking Git input is empty or oversized')
    return data


def _real_path(common: Path, path: Path) -> None:
    if any(_link(parent) for parent in (path, *path.parents) if parent.is_relative_to(common)):
        raise ValueError('Official locking dependencies cannot traverse filesystem links')


def _check_common(common: Path, profile: dict, lock: dict) -> dict:
    selected = _profile(profile, lock)
    if not common.is_dir() or any(_link(parent) for parent in (common, *common.parents)):
        raise ValueError('Official locking common path must be an independent real directory')
    commit = selected['common_commit']
    if (_git(common, 'rev-parse', '--verify', 'HEAD').decode('ascii').strip() != commit
            or Path(_git(common, 'rev-parse', '--show-toplevel').decode('utf-8').strip()).resolve() != common.resolve()):
        raise ValueError('Official locking restoration requires its exact reviewed common commit')
    directory = common / 'kernel/locking'
    _real_path(common, directory)
    if not directory.is_dir():
        raise ValueError('Official native locking directory is missing')
    makefile = lock['common_makefile']
    for data in (_native_blob(common, commit, makefile['path']),
                 _read(common / makefile['path'], makefile['size'])):
        if len(data) != makefile['size'] or _hash(data) != makefile['sha256'] or _blob(data) != makefile['git_blob']:
            raise ValueError('The native mandatory locking Makefile was changed')
    core_path = common / 'kernel/sched/core.c'
    _real_path(common, core_path)
    for data in (_native_blob(common, commit, 'kernel/sched/core.c'), _read(core_path, 1024 * 1024)):
        if (data.count(b'#include "../locking/locking_main.h"') != 1
                or data.count(b'locking_record_switch_in_cs(prev);') != 1):
            raise ValueError('The native scheduler locking integration changed')
    for path, spec in selected['files'].items():
        target = spec['symlink_target'].encode('ascii')
        expected = b'120000 blob ' + _blob(target).encode('ascii') + b'\t' + path.encode('ascii') + b'\0'
        if (_git(common, 'ls-tree', '-z', commit, '--', path) != expected
                or _native_blob(common, commit, path) != target):
            raise ValueError('Expected the reviewed tracked official locking symlink')
    for data in (_native_blob(common, commit, 'Makefile'), _read(common / 'Makefile', 1024 * 1024)):
        if (data.count(VERSION_HEADER_DECLARATION) != 1 or data.count(VERSION_HEADER_PRODUCER) != 1
                or data.count(b'define filechk_version.h\n') != 1
                or not re.search(rb'archprepare:[^\n]*(?:\\\n[^\n]*)*\$\(version_h\)', data)):
            raise ValueError('The native build-generated linux/version.h producer changed')
    for header in COMMON_HEADERS - GENERATED_HEADERS:
        dependency = common / 'include' / header
        _real_path(common, dependency)
        if not dependency.is_file():
            raise ValueError('Required native locking common header is missing')
    return selected


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *arguments, **keywords):
        return None


def download_locking_source(profile_id: str, destination: str, size: int) -> bytes:
    lock = load_locking_lock()
    if profile_id not in PROFILES or destination not in DESTINATIONS:
        raise ValueError('Unreviewed official locking download request')
    selected = lock['profiles'][profile_id]
    spec = selected['files'][destination]
    if type(size) is not int or size != spec['size']:
        raise ValueError('Unreviewed official locking download size')
    url = ('https://raw.githubusercontent.com/' + selected.get('vendor_repository', lock['vendor_repository'])
           + '/' + selected['vendor_commit']
           + '/' + spec['source_path'])
    request = Request(url, headers={'User-Agent': 'gki-official-locking-source', 'Accept': 'application/octet-stream'})
    opener = build_opener(_NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ValueError('Official locking source changed destination or status')
                data = response.read(size + 1)
            if len(data) != size:
                raise ValueError('Official locking source is truncated or oversized')
            return data
        except HTTPError as error:
            code = error.code
            error.close()
            if code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise ValueError('Fixed official locking source download failed') from None
        except (OSError, URLError, TimeoutError):
            if attempt == 2:
                raise ValueError('Fixed official locking source transport failed') from None
        time.sleep(attempt + 1)
    raise AssertionError('Unreachable official locking retry state')


def verify_source_bytes(sources: dict, selected: dict) -> None:
    if set(sources) != DESTINATIONS:
        raise ValueError('Incomplete official locking source dependency closure')
    for path, spec in selected['files'].items():
        data = sources[path]
        if (not isinstance(data, bytes) or len(data) != spec['size'] or _hash(data) != spec['sha256']
                or _blob(data) != spec['git_blob']):
            raise ValueError('Official locking source failed size, SHA256 or Git blob verification')
    def includes(data):
        return re.findall(rb'^\s*#\s*include\s*([<"])([^>"\n]+)[>"]', data, re.M)
    c_includes = includes(sources['kernel/locking/oplus_locking.c'])
    if ({name.decode('ascii') for bracket, name in c_includes if bracket == b'<'} != COMMON_HEADERS
            or [name for bracket, name in c_includes if bracket == b'"'] != [b'locking_main.h']
            or includes(sources['kernel/locking/locking_main.h']) != [(b'"', b'sa_common_struct.h')]
            or includes(sources['kernel/locking/sa_common_struct.h'])):
        raise ValueError('Official locking source include dependency closure changed')


def _check_links(common: Path, selected: dict) -> None:
    for path, spec in selected['files'].items():
        _check_link(common / path, spec)


def _check_link(target: Path, spec: dict) -> None:
    if (not target.is_symlink() or target.exists()
            or os.readlink(target).replace('\\', '/') != spec['symlink_target']):
        raise ValueError('Only the exact dangling official locking links may be replaced together')


def _installed(common: Path, selected: dict) -> None:
    sources = {path: _read(common / path, spec['size']) for path, spec in selected['files'].items()}
    verify_source_bytes(sources, selected)


def _provenance(lock: dict, selected: dict) -> dict:
    return {'schema_version': 1, 'component': lock['component'],
            'repository': selected.get('vendor_repository', lock['vendor_repository']),
            'branch': selected['vendor_branch'], 'commit': selected['vendor_commit'],
            'common_repository': selected.get('common_repository', lock['common_repository']),
            'common_commit': selected['common_commit'],
            'files': {path: {'source_path': spec['source_path'], 'source_sha256': spec['sha256'],
                             'sha256': spec['sha256'], 'size': spec['size']}
                      for path, spec in selected['files'].items()}}


def restore_vendor_locking(common: Path, profile: dict) -> dict:
    if not isinstance(profile, dict):
        raise ValueError('Invalid official locking profile')
    if profile.get('id') not in PROFILES:
        return {}
    common = Path(common).absolute()
    lock = load_locking_lock()
    selected = _check_common(common, profile, lock)
    if all((common / path).is_file() and not _link(common / path) for path in DESTINATIONS):
        _installed(common, selected)
        return _provenance(lock, selected)
    _check_links(common, selected)
    sources = {path: download_locking_source(profile['id'], path, spec['size']) for path, spec in selected['files'].items()}
    verify_source_bytes(sources, selected)
    stage = common / 'kernel/locking' / ('.gki-locking-' + uuid.uuid4().hex)
    stage.mkdir()
    published = []
    try:
        for path, data in sources.items():
            with (stage / Path(path).name).open('xb') as stream:
                stream.write(data)
        _check_common(common, profile, lock)
        _check_links(common, selected)
        for path in sources:
            _check_link(common / path, selected['files'][path])
            os.replace(stage / Path(path).name, common / path)
            published.append(path)
        _installed(common, selected)
    except BaseException:
        # Roll back only this call's verified leaves. Never follow an original
        # external link, overwrite another process's change or walk vendor/.
        for path in reversed(published):
            target = common / path
            if _read(target, selected['files'][path]['size']) != sources[path]:
                raise ValueError('Official locking output changed independently during rollback') from None
            temporary = stage / (Path(path).name + '.link')
            os.symlink(selected['files'][path]['symlink_target'], temporary)
            os.replace(temporary, target)
        raise
    finally:
        for path in sources:
            (stage / Path(path).name).unlink(missing_ok=True)
            (stage / (Path(path).name + '.link')).unlink(missing_ok=True)
        stage.rmdir()
    return _provenance(lock, selected)


def validate_vendor_locking_provenance(profile: dict, report: dict) -> dict:
    if not isinstance(profile, dict):
        raise ValueError('Invalid official locking profile')
    if profile.get('id') not in PROFILES:
        if report != {}:
            raise ValueError('Unexpected official locking provenance for this profile')
        return {}
    lock = load_locking_lock()
    selected = _profile(profile, lock)
    if (not isinstance(report, dict) or report != _provenance(lock, selected)
            or type(report.get('schema_version')) is not int
            or any(type(spec.get('size')) is not int for spec in report.get('files', {}).values())):
        raise ValueError('Official locking provenance differs from its immutable source lock')
    return _provenance(lock, selected)


def verify_vendor_locking_report(common: Path, profile: dict, report: dict) -> dict:
    validated = validate_vendor_locking_provenance(profile, report)
    if validated:
        common = Path(common).absolute()
        lock = load_locking_lock()
        selected = _check_common(common, profile, lock)
        _installed(common, selected)
    return validated
