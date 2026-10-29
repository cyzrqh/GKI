"""Restore the mandatory memory trace header from its paired official tree."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
import uuid

from oplus_compat import native_git
from oplus_mtk_cpu import NoRedirect, checked_bytes, is_link, read, selected_lock

PROFILE = 'mt6897-6.1.75'
TARGET = 'mm/mm-trace.h'
SOURCE = 'vendor/oplus/kernel/mm/mm_osvelte/mm-trace.h'
LINK = '../../' + SOURCE
LINK_BLOB = '7f3c7edd1bc77cf79618f13721dd9673135d2c02'
SPEC = {'size': 4911, 'sha256': 'ff2ee5fce01cf1b89e778391f5a96e16d4bc70b4b69c8902db4f36138c46e9e4',
        'git_blob': 'fb800606ff0ad3b3b67079aa9d13c77692e9dd30'}
VMSCAN_SHA256 = {
    'mt6897-6.1.75': 'adf9d0d6b6abe64900d59e2b32b8fc40c52a52ec73c6d788d524a3b2b9bb4901',
    'mt6897-6.1.128': '83bf58893a7fa49a6b0562a5f97845452b80fc66d808df68c47787b26fddc162',
    'mt6989-6.1.115': '7791cc902afe2d3677eb4ceebd934004e22f1756084f5415c18e7137d70bc88b',
    'mt6989-6.1.134': '00202319696a3cba5d74b4c527a482c4f26ede21d2199603ca5430ab53d4f0a1',
}


def selected(profile):
    if not isinstance(profile, dict):
        raise ValueError('Invalid official memory source profile')
    if profile.get('id') not in VMSCAN_SHA256:
        return None
    # Share the reviewed immutable common/vendor pair with the CPU provider.
    # Its fixed lock digest and exact profile checks prevent branch mixing.
    loaded = selected_lock(profile)
    if loaded is None:
        raise ValueError('Missing reviewed official memory source pair')
    return loaded[0]


def provenance(pair):
    return {'schema_version': 1, 'repository': pair['vendor_repository'], 'commit': pair['vendor_commit'],
            'common_repository': pair['common_repository'], 'common_commit': pair['common_commit'],
            'files': {TARGET: {'source_path': SOURCE, 'size': SPEC['size'],
                              'sha256': SPEC['sha256'], 'source_sha256': SPEC['sha256']}}}


def check_common(common, pair):
    if not common.is_dir() or any(is_link(p) for p in (common, *common.parents)):
        raise ValueError('Official memory common source must be an independent directory')
    if native_git(common, 'rev-parse', '--verify', 'HEAD').strip() != pair['common_commit']:
        raise ValueError('Official memory restoration requires its exact common commit')
    if native_git(common, 'ls-tree', 'HEAD', '--', TARGET).strip() != '120000 blob ' + LINK_BLOB + '\t' + TARGET:
        raise ValueError('Official memory source must track the exact reviewed link')
    for relative in ('mm',):
        if is_link(common / relative) or not (common / relative).is_dir():
            raise ValueError('Official memory source parent cannot be linked')
    profile_id = pair['platform'] + '-' + pair['kernel']
    if hashlib.sha256(read(common / 'mm/vmscan.c')).hexdigest() != VMSCAN_SHA256[profile_id]:
        raise ValueError('Review the changed native memory trace consumer before restoration')


def download_source(pair):
    url = ('https://raw.githubusercontent.com/' + pair['vendor_repository'] + '/'
           + pair['vendor_commit'] + '/' + SOURCE)
    for attempt in range(3):
        try:
            request = Request(url, headers={'User-Agent': 'gki-official-memory', 'Accept': 'application/octet-stream'})
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ValueError('Fixed official memory source changed its status or destination')
                return response.read(SPEC['size'] + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            if status not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise ValueError('Fixed official memory source download failed') from None
        except (OSError, URLError, TimeoutError):
            if attempt == 2:
                raise ValueError('Fixed official memory source transport failed') from None
        time.sleep(attempt + 1)


def check_link(target):
    if not target.is_symlink() or os.readlink(target).replace('\\', '/') != LINK or target.exists():
        raise ValueError('Only the exact dangling official memory header link may be replaced')


def restore_mtk_memory(common, profile):
    pair = selected(profile)
    if pair is None:
        return {}
    common = Path(common).absolute()
    check_common(common, pair)
    target = common / TARGET
    if target.is_file() and not is_link(target):
        checked_bytes(read(target, SPEC['size']), SPEC)
        return provenance(pair)
    check_link(target)
    data = download_source(pair)
    checked_bytes(data, SPEC)
    stage = target.parent / ('.oplus-mm-trace-' + uuid.uuid4().hex)
    try:
        with stage.open('xb') as stream:
            stream.write(data)
        stage.chmod(0o644)
        checked_bytes(read(stage, SPEC['size']), SPEC)
        check_common(common, pair)
        check_link(target)
        # replace(2) replaces the dangling link itself; never follow its vendor destination.
        os.replace(stage, target)
    finally:
        stage.unlink(missing_ok=True)
    return provenance(pair)


def validate_mtk_memory_provenance(profile, report):
    pair = selected(profile)
    expected = provenance(pair) if pair else {}
    if report != expected or expected and type(report.get('schema_version')) is not int:
        raise ValueError('Memory source provenance differs from its paired official source')
    return expected


def verify_mtk_memory_report(common, profile, report):
    expected = validate_mtk_memory_provenance(profile, report)
    if expected:
        common = Path(common).absolute()
        check_common(common, selected(profile))
        checked_bytes(read(common / TARGET, SPEC['size']), SPEC)
    return expected
