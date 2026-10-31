"""Restore CCTV's reviewed SM8650 6.1.141 scheduler call paths on official source.

Retain exported vendor hook declarations/ABI. Only the two scheduler C files
change, to their exact reviewed reference bytes. This is source compatibility,
not a claim that a particular device/firmware has passed a boot test.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import uuid

from oplus_f2fs_compat import applicable, COMMIT, REFERENCE_REPOSITORY, REFERENCE_COMMIT

FILES = {
    'kernel/sched/core.c': (
        '915289597be5715b69746b4990cdde5e9617e3dc4200df910a568360c1fe2582',
        '809c7e27953f6e20d4e422f0e07f14b3314c4a0fb33f89275fa9af6502215073'),
    'kernel/sched/fair.c': (
        '2325235ab79de61f8367677fe1b05c579f8cca60548500be895a920fe836433b',
        '17848f67b66a0adadd7bcd77a91e615adc2466856fc0bd5db1130d55ef3311b0'),
}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _remove(raw: bytes, before: bytes, count: int = 1) -> bytes:
    if raw.count(before) != count:
        raise ValueError('Native lazy-scheduler compatibility context changed')
    return raw.replace(before, b'')


def repair_source(relative: str, raw: bytes) -> bytes:
    original, fixed = FILES[relative]
    if digest(raw) == fixed:
        return raw
    if digest(raw) != original:
        raise ValueError('Review the changed official lazy-scheduler source before building')
    if relative == 'kernel/sched/core.c':
        raw = _remove(raw, b'\tbool skip_schedule = false;\n')
        raw = _remove(raw, b'\n\ttrace_android_vh_lock_delay_schedule(prev, sched_mode, &skip_schedule);\n'
                          b'\n\tif (skip_schedule)\n\t\treturn;\n')
        raw = _remove(raw, b'\ttrace_android_vh_clear_curr_lazy(prev);\n')
    else:
        raw = _remove(raw, b'#include <trace/hooks/dtask.h>\n')
        raw = _remove(raw, b'\t\ttrace_android_vh_resched_curr_lazy(rq_of(cfs_rq), &skip_preempt);\n'
                          b'\n\t\tif(skip_preempt)\n\t\t\treturn;\n\n', 2)
        old = (b'entity_tick(struct cfs_rq *cfs_rq, struct sched_entity *curr, int queued)\n'
               b'{\n\tbool skip_preempt = false;\n')
        if raw.count(old) != 1:
            raise ValueError('Native entity_tick scheduler definition changed')
        raw = raw.replace(old, old.replace(b'\tbool skip_preempt = false;\n', b''), 1)
        raw = _remove(raw, b'\ttrace_android_vh_resched_curr_lazy(rq_of(cfs_rq), &ignore);\n'
                          b'\n\tif(ignore)\n\t\treturn;\n\n')
    if digest(raw) != fixed:
        raise ValueError('Scheduler compatibility output differs from the reviewed CCTV reference')
    return raw


def _read(common: Path, relative: str) -> tuple[Path, bytes]:
    path = Path(common).absolute() / relative
    if (any(p.is_symlink() or bool(getattr(p, 'is_junction', lambda: False)()) for p in (path, *path.parents))
            or not path.is_file() or not 0 < path.stat().st_size <= 1024 * 1024):
        raise ValueError('Scheduler compatibility requires bounded regular official source files')
    return path, path.read_bytes()


def provenance(profile: dict) -> dict:
    return {'schema_version': 1, 'profile_id': profile['id'], 'common_commit': COMMIT,
            'fix': 'restore-reviewed-scheduling-without-lazy-vendor-callbacks',
            'files': {name: values[1] for name, values in FILES.items()},
            'reference_repository': REFERENCE_REPOSITORY, 'reference_commit': REFERENCE_COMMIT}


def prepare_scheduler_compat(common: Path, profile: dict) -> dict:
    if not applicable(profile):
        return {}
    # Validate the complete input set and all outputs before changing any file.
    changes = []
    for name in FILES:
        path, raw = _read(common, name)
        fixed = repair_source(name, raw)
        if fixed != raw:
            changes.append((path, fixed))
    for path, fixed in changes:
        temporary = path.with_name('.gki-scheduler-' + uuid.uuid4().hex)
        try:
            with temporary.open('xb') as stream:
                stream.write(fixed)
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return verify_scheduler_compat(common, profile)


def validate_scheduler_provenance(profile: dict, report: dict) -> dict:
    if not applicable(profile):
        if report != {}:
            raise ValueError('Unexpected lazy-scheduler repair on another kernel/platform')
        return {}
    if not isinstance(report, dict) or type(report.get('schema_version')) is not int or report != provenance(profile):
        raise ValueError('Scheduler repair metadata differs from the reviewed source compatibility fix')
    return report


def verify_scheduler_compat(common: Path, profile: dict) -> dict:
    if not applicable(profile):
        return {}
    for name, (_, fixed) in FILES.items():
        if digest(_read(common, name)[1]) != fixed:
            raise ValueError('Refusing to package SM8650 6.1.141 without its reviewed scheduler repair')
    return provenance(profile)
