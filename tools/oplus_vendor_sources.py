#!/usr/bin/env python3
"""Restore one enabled scheduler from its matched, fixed official vendor tree.

The official common commit tracks a dangling kernel/oplus_cpu symlink. Its
gki_defconfig enables OPLUS_SCHED_TUNE, so dropping that reference would change
the native scheduler. Restore the complete enabled component inside common/;
never follow or populate the symlink's external vendor destination.
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
PROFILE = 'sm8650-6.1.141'
COMMON_REPOSITORY = 'OnePlusOSS/android_kernel_common_oneplus_sm8650'
COMMON_COMMIT = 'ebdd1643cfdbf719fb59c6573ce0abe6e794a5cc'
VENDOR_REPOSITORY = 'OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8650'
VENDOR_BRANCH = 'oneplus/sm8650_b_16.0.0_oneplus12'
VENDOR_COMMIT = 'c951f93ec63fbee49d468ee2f9d93ac0e807da67'
SYMLINK_PATH = 'kernel/oplus_cpu'
SYMLINK_TARGET = '../../../vendor/oplus/kernel/cpu'
SOURCE_PREFIX = 'vendor/oplus/kernel/cpu/'
COPYRIGHT = b'# Copyright (C) 2018-2020 Oplus. All rights reserved.\n\n'
FILE_LOCKS = {
    'sched/Kconfig': (6625, '060cea5ba6b6837246d1208b2b867148f500c1a9477a16e3416821159e39dad6',
                      'b88d94710b54915cd808d3729e9c1b76b25a176a'),
    'sched/sched_tune/Makefile': (137, '25fabe5f843267bdd9302c81abde7f3405f7bbe6278016001776ecb16b1423ca',
                                 'af21074748a2fc94b264ab8a6772d8667db827d8'),
    'sched/sched_tune/tune.c': (13405, '35efcc34bcf732e2672874bb8891f28ea8a28561b6a31ef5093b700172158758',
                               '85ee3625f2475344db48267a3fbc856638a14ee5'),
    'sched/sched_tune/tune.h': (2590, '08f53fdfb467cfd3e6580c2e096f43c0b418ed11990e1a43afc110a7b0a3519f',
                               '6c1665186e989a2463130e585bcd901826c357eb'),
}
FRAGMENT_SIZE = 1009
FRAGMENT_SHA256 = 'e6be96ad8f37041216a5b552b89eff86b4296f0ceeb92f61f5918b451c8b70fd'
COMMON_HEADERS = frozenset({
    'include/linux/cgroup.h', 'include/linux/err.h', 'include/linux/kernel.h',
    'include/linux/percpu.h', 'include/linux/printk.h', 'include/linux/rcupdate.h',
    'include/linux/slab.h', 'include/linux/sched/clock.h', 'include/linux/sched/cputime.h',
    'kernel/sched/sched.h', 'include/linux/reciprocal_div.h',
})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_blob(data: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _read(path: Path, maximum: int) -> bytes:
    try:
        if _is_link(path) or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('Official vendor input must be a regular, non-symlink file')
        with path.open('rb') as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('Official vendor input exceeds its bounded size')
        return data
    except OSError:
        raise ValueError('Cannot read the required official vendor input') from None


def load_vendor_lock() -> dict:
    def unique_object(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate official scheduler source lock field')
            value[key] = item
        return value
    try:
        doc = json.loads(_read(ROOT / 'config/oplus-vendor-sched-tune.json', 16384),
                         object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise ValueError('Invalid fixed official OPlus scheduler source lock') from None
    expected = {
        'schema_version': 1, 'profile_id': PROFILE,
        'common_repository': COMMON_REPOSITORY, 'common_commit': COMMON_COMMIT,
        'vendor_repository': VENDOR_REPOSITORY, 'vendor_branch': VENDOR_BRANCH,
        'vendor_commit': VENDOR_COMMIT, 'symlink_path': SYMLINK_PATH,
        'symlink_target': SYMLINK_TARGET,
        'files': {path: {'size': size, 'sha256': sha, 'git_blob': blob}
                  for path, (size, sha, blob) in FILE_LOCKS.items()},
        'kconfig_fragment': {'symbol': 'OPLUS_SCHED_TUNE', 'size': FRAGMENT_SIZE,
                            'sha256': FRAGMENT_SHA256},
    }
    if doc != expected or type(doc.get('schema_version')) is not int:
        raise ValueError('Invalid fixed official OPlus scheduler source lock')
    return doc


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download_source(relative: str, size: int) -> bytes:
    """Only public exact-commit raw bytes; no credentials or setup scripts."""
    if relative not in FILE_LOCKS or size != FILE_LOCKS[relative][0]:
        raise ValueError('Unreviewed official vendor download request')
    url = f'https://raw.githubusercontent.com/{VENDOR_REPOSITORY}/{VENDOR_COMMIT}/{SOURCE_PREFIX}{relative}'
    request = Request(url, headers={'User-Agent': 'gki-official-vendor-component',
                                    'Accept': 'application/octet-stream'})
    opener = build_opener(_NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ValueError('Official vendor source changed destination or status')
                data = response.read(size + 1)
            if len(data) != size:
                raise ValueError('Official vendor source is truncated or oversized')
            return data
        except HTTPError as error:
            status = error.code
            error.close()
            if status not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise ValueError('Fixed official vendor source download failed') from None
        except (OSError, URLError, TimeoutError):
            if attempt == 2:
                raise ValueError('Fixed official vendor source transport failed') from None
        time.sleep(attempt + 1)
    raise AssertionError('Unreachable vendor download retry state')


def derive_sources(sources: dict[str, bytes], lock: dict) -> dict[str, bytes]:
    if set(sources) != set(lock['files']):
        raise ValueError('Incomplete fixed official scheduler source component')
    for relative, spec in lock['files'].items():
        data = sources[relative]
        if (not isinstance(data, bytes) or len(data) != spec['size']
                or _sha256(data) != spec['sha256'] or _git_blob(data) != spec['git_blob']):
            raise ValueError('Official scheduler source failed complete size, SHA256 or Git blob verification')
    kconfig = sources['sched/Kconfig']
    starts = list(re.finditer(rb'^config OPLUS_SCHED_TUNE\n', kconfig, re.M))
    if len(starts) != 1:
        raise ValueError('Official scheduler Kconfig definition is missing or ambiguous')
    tail = kconfig[starts[0].start():]
    next_symbol = re.search(rb'^config ', tail[len(b'config '):], re.M)
    fragment = tail[:next_symbol.start() + len(b'config ')] if next_symbol else tail
    if re.search(rb'^\s*(?:depends\s+on|select|imply|source|rsource)\b', fragment, re.M):
        raise ValueError('Unreviewed external dependency in official scheduler Kconfig')
    fragment = COPYRIGHT + fragment
    if (len(fragment) != lock['kconfig_fragment']['size']
            or _sha256(fragment) != lock['kconfig_fragment']['sha256']):
        raise ValueError('Official scheduler Kconfig fragment failed its derived SHA256')
    data = sources['sched/sched_tune/tune.c']
    includes = re.findall(rb'^\s*#\s*include\s*([<"])([^>"\n]+)[>"]', data, re.M)
    common = {name.decode('ascii') if name.startswith(b'kernel/') else 'include/' + name.decode('ascii')
              for bracket, name in includes if bracket == b'<'}
    local = {name for bracket, name in includes if bracket == b'"'}
    if common != COMMON_HEADERS or local != {b'tune.h'} or re.search(rb'^\s*extern\b', data, re.M):
        raise ValueError('Official scheduler has an unreviewed C include or external vendor dependency')
    if re.search(rb'^\s*#\s*include\b', sources['sched/sched_tune/tune.h'], re.M):
        raise ValueError('Official scheduler header has an unreviewed include dependency')
    makefile = sources['sched/sched_tune/Makefile']
    if (b'obj-$(CONFIG_OPLUS_SCHED_TUNE) += oplus_bsp_schedtune.o' not in makefile
            or b'oplus_bsp_schedtune-y += tune.o' not in makefile):
        raise ValueError('Official scheduler Kbuild component changed')
    return {**sources, 'sched/Kconfig': fragment}


def native_head(common: Path) -> str:
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(common),
                             'rev-parse', '--verify', 'HEAD'], capture_output=True, text=True,
                            encoding='utf-8', timeout=30)
    if result.returncode or result.stdout.strip() != COMMON_COMMIT:
        raise ValueError('Official scheduler vendor snapshot requires its exact reviewed common commit')
    return result.stdout.strip()


def _check_profile(profile: dict) -> None:
    reference = profile.get('kernel_ref', profile.get('verified_kernel_ref'))
    if (profile.get('id') != PROFILE or profile.get('platform') != 'sm8650'
            or profile.get('kernel') != '6.1.141' or profile.get('kernel_repo') != COMMON_REPOSITORY
            or reference != COMMON_COMMIT
            or profile.get('verified_kernel_ref', COMMON_COMMIT) != COMMON_COMMIT):
        raise ValueError('Official scheduler source pairing does not match the reviewed profile')


def _check_common(common: Path, profile: dict) -> None:
    _check_profile(profile)
    if not common.is_dir() or any(_is_link(path) for path in (common, *common.parents)):
        raise ValueError('Official common source path must not traverse symlinks')
    native_head(common)
    kernel = common / 'kernel'
    if _is_link(kernel) or not kernel.is_dir():
        raise ValueError('Official kernel directory must be a real directory')
    config = _read(common / 'arch/arm64/configs/gki_defconfig', 1048576).decode('utf-8')
    values = re.findall(r'^CONFIG_OPLUS_SCHED_TUNE=([^\r\n]*)\r?$', config, re.M)
    if values != ['y']:
        raise ValueError('Restore requires the unchanged enabled native OPLUS_SCHED_TUNE option')
    for relative in COMMON_HEADERS:
        path = common / relative
        if any(_is_link(parent) for parent in (path, *path.parents) if parent.is_relative_to(common)) or not path.is_file():
            raise ValueError('Official scheduler requires its complete common-header dependencies')


def _installed_specs(lock: dict) -> dict:
    specs = {path: {'size': item['size'], 'sha256': item['sha256']}
             for path, item in lock['files'].items()}
    specs['sched/Kconfig'] = {key: lock['kconfig_fragment'][key] for key in ('size', 'sha256')}
    return specs


def verify_installed(target: Path, lock: dict) -> None:
    directories = {'': {'sched'}, 'sched': {'Kconfig', 'sched_tune'},
                   'sched/sched_tune': {'Makefile', 'tune.c', 'tune.h'}}
    for relative, names in directories.items():
        directory = target / relative
        if _is_link(directory) or not directory.is_dir() or {p.name for p in directory.iterdir()} != names:
            raise ValueError('Restored official scheduler directory is incomplete, different or symlinked')
    for relative, spec in _installed_specs(lock).items():
        data = _read(target / relative, spec['size'])
        if len(data) != spec['size'] or _sha256(data) != spec['sha256']:
            raise ValueError('Existing official scheduler file differs from its audited bytes')


def _provenance(lock: dict) -> dict:
    return {
        'schema_version': 1, 'repository': lock['vendor_repository'],
        'branch': lock['vendor_branch'], 'commit': lock['vendor_commit'],
        'common_repository': lock['common_repository'], 'common_commit': lock['common_commit'],
        'files': {SYMLINK_PATH + '/' + relative: {
            'source_path': SOURCE_PREFIX + relative, 'source_sha256': lock['files'][relative]['sha256'],
            'sha256': installed['sha256'], 'size': installed['size']}
            for relative, installed in _installed_specs(lock).items()},
    }


def _check_dangling_link(target: Path) -> None:
    if (not target.is_symlink() or os.readlink(target).replace('\\', '/') != SYMLINK_TARGET
            or target.exists()):
        raise ValueError('Only the exact reviewed dangling official vendor symlink may be replaced')


def restore_vendor_sources(common: Path, profile: dict) -> dict:
    """Return source provenance; other supported profiles need no restoration."""
    if not isinstance(profile, dict):
        raise ValueError('Invalid official scheduler source profile')
    if profile.get('id') != PROFILE:
        return {}
    common = Path(common).absolute()
    lock = load_vendor_lock()
    _check_common(common, profile)
    target = common / SYMLINK_PATH
    if target.is_dir() and not _is_link(target):
        verify_installed(target, lock)
        return _provenance(lock)
    _check_dangling_link(target)
    # Finish every network/hash/dependency check before changing the symlink.
    sources = {relative: download_source(relative, spec['size'])
               for relative, spec in lock['files'].items()}
    installed = derive_sources(sources, lock)
    stage = target.parent / ('.oplus_cpu-restore-' + uuid.uuid4().hex)
    stage.mkdir()
    replaced = False
    try:
        for relative, data in installed.items():
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as stream:
                stream.write(data)
        verify_installed(stage, lock)
        _check_common(common, profile)
        _check_dangling_link(target)
        target.unlink()  # Only this confirmed dangling link; never its target.
        try:
            os.replace(stage, target)
            replaced = True
        except OSError:
            if not target.exists() and not target.is_symlink():
                os.symlink(SYMLINK_TARGET, target, target_is_directory=True)
            raise ValueError('Cannot publish the verified official scheduler directory') from None
    finally:
        if not replaced:
            # The stage belongs to this invocation. Remove only the four known
            # leaves and three directories; no recursive delete or external walk.
            for relative in installed:
                (stage / relative).unlink(missing_ok=True)
            for relative in ('sched/sched_tune', 'sched', ''):
                directory = stage / relative
                if directory.exists():
                    directory.rmdir()
    return _provenance(lock)


def validate_vendor_provenance(profile: dict, report: dict) -> dict:
    """Pure metadata validation; no local source tree or network is required."""
    if not isinstance(profile, dict):
        raise ValueError('Invalid official scheduler source profile')
    if profile.get('id') != PROFILE:
        if report != {}:
            raise ValueError('Unexpected vendor source report for this official kernel profile')
        return {}
    _check_profile(profile)
    lock = load_vendor_lock()
    if (not isinstance(report, dict) or report != _provenance(lock)
            or type(report.get('schema_version')) is not int
            or any(type(item.get('size')) is not int for item in report.get('files', {}).values())):
        raise ValueError('Official scheduler source report differs from the immutable provenance')
    return _provenance(lock)


def verify_vendor_report(common: Path, profile: dict, report: dict) -> dict:
    """Validate metadata and actual installed sources without downloads or edits."""
    validated = validate_vendor_provenance(profile, report)
    if not validated:
        return {}
    lock = load_vendor_lock()
    common = Path(common).absolute()
    _check_common(common, profile)
    verify_installed(common / SYMLINK_PATH, lock)
    return validated
