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
from oplus_mtk_cpu import NoRedirect, checked_bytes, is_link, read

PROFILE = 'mt6897-6.1.75'
COMMON = 'oppo-source/android_kernel_oppo_mt6897'
COMMON_REF = '1bed026fb776f4167c7af2d9358e25b792adaa79'
VENDOR = 'oppo-source/android_kernel_modules_and_devicetree_oppo_mt6897'
VENDOR_REF = '0c86228a7a06d2e32df8187ad753613a2159a5fa'
TARGET = 'mm/mm-trace.h'
SOURCE = 'vendor/oplus/kernel/mm/mm_osvelte/mm-trace.h'
LINK = '../../' + SOURCE
LINK_BLOB = '7f3c7edd1bc77cf79618f13721dd9673135d2c02'
SPEC = {'size': 4911, 'sha256': 'ff2ee5fce01cf1b89e778391f5a96e16d4bc70b4b69c8902db4f36138c46e9e4',
        'git_blob': 'fb800606ff0ad3b3b67079aa9d13c77692e9dd30'}
VMSCAN_SHA256 = 'adf9d0d6b6abe64900d59e2b32b8fc40c52a52ec73c6d788d524a3b2b9bb4901'


def selected(profile):
    if not isinstance(profile, dict):
        raise ValueError('Invalid official memory source profile')
    if profile.get('id') != PROFILE:
        return False
    reference = profile.get('kernel_ref', profile.get('verified_kernel_ref'))
    if (profile.get('kernel_repo') != COMMON or reference != COMMON_REF
            or profile.get('verified_kernel_ref', reference) != reference
            or profile.get('platform') != 'mt6897' or profile.get('kernel') != '6.1.75'):
        raise ValueError('Official memory source pairing differs from its reviewed profile')
    return True


def provenance():
    return {'schema_version': 1, 'repository': VENDOR, 'commit': VENDOR_REF,
            'common_repository': COMMON, 'common_commit': COMMON_REF,
            'files': {TARGET: {'source_path': SOURCE, 'size': SPEC['size'],
                              'sha256': SPEC['sha256'], 'source_sha256': SPEC['sha256']}}}


def check_common(common):
    if not common.is_dir() or any(is_link(p) for p in (common, *common.parents)):
        raise ValueError('Official memory common source must be an independent directory')
    if native_git(common, 'rev-parse', '--verify', 'HEAD').strip() != COMMON_REF:
        raise ValueError('Official memory restoration requires its exact common commit')
    if native_git(common, 'ls-tree', 'HEAD', '--', TARGET).strip() != '120000 blob ' + LINK_BLOB + '\t' + TARGET:
        raise ValueError('Official memory source must track the exact reviewed link')
    for relative in ('mm',):
        if is_link(common / relative) or not (common / relative).is_dir():
            raise ValueError('Official memory source parent cannot be linked')
    if hashlib.sha256(read(common / 'mm/vmscan.c')).hexdigest() != VMSCAN_SHA256:
        raise ValueError('Review the changed native memory trace consumer before restoration')


def download_source():
    url = 'https://raw.githubusercontent.com/' + VENDOR + '/' + VENDOR_REF + '/' + SOURCE
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
    if not selected(profile):
        return {}
    common = Path(common).absolute()
    check_common(common)
    target = common / TARGET
    if target.is_file() and not is_link(target):
        checked_bytes(read(target, SPEC['size']), SPEC)
        return provenance()
    check_link(target)
    data = download_source()
    checked_bytes(data, SPEC)
    stage = target.parent / ('.oplus-mm-trace-' + uuid.uuid4().hex)
    try:
        with stage.open('xb') as stream:
            stream.write(data)
        stage.chmod(0o644)
        checked_bytes(read(stage, SPEC['size']), SPEC)
        check_common(common)
        check_link(target)
        # replace(2) replaces the dangling link itself; never follow its vendor destination.
        os.replace(stage, target)
    finally:
        stage.unlink(missing_ok=True)
    return provenance()


def validate_mtk_memory_provenance(profile, report):
    expected = provenance() if selected(profile) else {}
    if report != expected or expected and type(report.get('schema_version')) is not int:
        raise ValueError('Memory source provenance differs from its paired official source')
    return expected


def verify_mtk_memory_report(common, profile, report):
    expected = validate_mtk_memory_provenance(profile, report)
    if expected:
        common = Path(common).absolute()
        check_common(common)
        checked_bytes(read(common / TARGET, SPEC['size']), SPEC)
    return expected
