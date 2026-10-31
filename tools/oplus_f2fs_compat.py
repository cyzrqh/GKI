"""Revert the reviewed SM8650 6.1.141 checkpoint-thread boot regression.

The source remains the fixed official OnePlus tree. Only checkpoint.c is
changed, to the exact bytes used by CCTV's reviewed reference. Keep the
F2FS on-disk/compression implementation and block-layer exported ABI intact.
See OnePlusOSS/android_kernel_common_oneplus_sm8650/issues/35.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import uuid

PROFILE = 'sm8650-6.1.141'
REPOSITORY = 'OnePlusOSS/android_kernel_common_oneplus_sm8650'
COMMIT = 'ebdd1643cfdbf719fb59c6573ce0abe6e794a5cc'
REFERENCE_REPOSITORY = 'cctv18/android_kernel_common_oneplus_sm8650'
REFERENCE_COMMIT = '7a244ff186208e81791a0792d5032b63dbad2eda'
RELATIVE = 'fs/f2fs/checkpoint.c'
ORIGINAL = '487cf07dcd6d37bff2a5108a551823cca9c0ce5e7feb885ab6e9cc883dfa675e'
FIXED = '9a6a8f60bf910b1c7d8621b1a17d15b0929d93ab66fbcdfe2c7ec8a109022df3'
BEFORE = b'''\tif (cprc->f2fs_issue_ckpt) {
\t\tbool prio_changed = false;
\t\tint orig_nice = 0;

\t\tif (test_task_ux(current)) {
\t\t\tprio_changed = true;
\t\t\torig_nice = task_nice(cprc->f2fs_issue_ckpt);
\t\t\tsched_set_fifo_low(cprc->f2fs_issue_ckpt);
\t\t}

\t\twait_for_completion(&req.wait);

\t\tif (prio_changed)
\t\t\tsched_set_normal(cprc->f2fs_issue_ckpt, orig_nice);
\t} else {
\t\tflush_remained_ckpt_reqs(sbi, &req);
\t}
'''
AFTER = b'''\tif (cprc->f2fs_issue_ckpt)
\t\twait_for_completion(&req.wait);
\telse
\t\tflush_remained_ckpt_reqs(sbi, &req);
'''


def applicable(profile: dict) -> bool:
    if profile.get('id') != PROFILE:
        return False
    if (profile.get('platform') != 'sm8650' or profile.get('kernel') != '6.1.141'
            or profile.get('kernel_repo') != REPOSITORY or profile.get('kernel_ref') != COMMIT):
        raise ValueError('Review the changed official SM8650 6.1.141 F2FS source before building')
    return True


def read_checkpoint(common: Path) -> tuple[Path, bytes]:
    common = Path(common).absolute()
    path = common / RELATIVE
    if (any(part.is_symlink() or bool(getattr(part, 'is_junction', lambda: False)())
            for part in (path, *path.parents))
            or not path.is_file() or path.stat().st_size > 256 * 1024):
        raise ValueError('F2FS compatibility requires a bounded regular official checkpoint source')
    return path, path.read_bytes()


def provenance(profile: dict) -> dict:
    return {'schema_version': 1, 'profile_id': profile['id'], 'common_commit': COMMIT,
            'fix': 'revert-checkpoint-thread-priority-boost',
            'file': RELATIVE, 'sha256': FIXED,
            'reference_repository': REFERENCE_REPOSITORY, 'reference_commit': REFERENCE_COMMIT}


def prepare_f2fs_compat(common: Path, profile: dict) -> dict:
    if not applicable(profile):
        return {}
    path, raw = read_checkpoint(common)
    digest = hashlib.sha256(raw).hexdigest()
    if digest == FIXED:
        return provenance(profile)
    if (digest != ORIGINAL or raw.count(BEFORE) != 1
            or raw.count(b'#include <linux/blk-mq.h>\n') != 1):
        raise ValueError('Unknown F2FS checkpoint source; boot-compatibility patch was not applied')
    fixed = raw.replace(BEFORE, AFTER, 1).replace(b'#include <linux/blk-mq.h>\n', b'', 1)
    if hashlib.sha256(fixed).hexdigest() != FIXED:
        raise ValueError('F2FS checkpoint repair differs from the reviewed boot-compatible source')
    temporary = path.with_name('.gki-f2fs-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as output:
            output.write(fixed)
        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return verify_f2fs_compat(common, profile)


def validate_f2fs_provenance(profile: dict, report: dict) -> dict:
    if not applicable(profile):
        if report != {}:
            raise ValueError('Unexpected F2FS boot repair on another platform or kernel')
        return {}
    if not isinstance(report, dict) or type(report.get('schema_version')) is not int or report != provenance(profile):
        raise ValueError('F2FS boot-repair metadata is missing or differs from the reviewed fix')
    return report


def verify_f2fs_compat(common: Path, profile: dict) -> dict:
    if not applicable(profile):
        return {}
    _, raw = read_checkpoint(common)
    if hashlib.sha256(raw).hexdigest() != FIXED:
        raise ValueError('Refusing to package SM8650 6.1.141 without the reviewed F2FS boot repair')
    return provenance(profile)
