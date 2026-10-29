"""Use the native pre-padding maps boundary for the fixed OnePlus 6.1.57 tree.

The SUSFS 2.3 OPEN_REDIRECT hunk uses VMA_PAD_START from newer Android
page-size migration code. This official tree predates that representation;
its complete mapping ends at vm_end. Adapt that one expression only after
checking the exact profile, native MM headers, and complete patched function.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import uuid

from validate_susfs_hooks import strip_comments_and_literals


PROFILE = 'sm8650-6.1.57'
REPOSITORY = 'OnePlusOSS/android_kernel_common_oneplus_sm8650'
COMMIT = 'e02429a7e0f337d517be2c01e12d1cf153df4542'
# Exact original official files at COMMIT. Neither header is changed by the
# reviewed SUSFS 6.1 patch. Native vm_area_struct has no padding representation.
NATIVE_HEADERS = {
    'include/linux/mm.h': 'd2d39d5f74b8407e460bf226070ac1a89ebe79fda544ecaf68fdf25ad6dae244',
    'include/linux/mm_types.h': '066d14a64a6da4ad1102ed554652c4c569cf1ebb9e304da667a04d533f93aca0',
}
NATIVE_FUNCTION_SHA256 = '1bc8cb3e52188676d4103d8638118e354fc959b914e9f6c356146121756be97e'
SUSFS_FUNCTION_SHA256 = 'a16a8f120f0f27269d2c14664d2b60c534e9814e924584a10729240fbafeea6e'
PADDED_END = '\t\t\t\tend = VMA_PAD_START(vma);'
NATIVE_END = '\t\t\t\tend = vma->vm_end;'


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def show_map_function(text: str) -> str:
    code = strip_comments_and_literals(text)
    matches = list(re.finditer(r'(?m)^static void\s+show_map_vma\s*\('
                              r'\s*struct seq_file \*m, struct vm_area_struct \*vma\)\s*\{', code))
    if len(matches) != 1:
        raise ValueError('Expected exactly one reviewed native show_map_vma function')
    opening = code.index('{', matches[0].start(), matches[0].end())
    depth = 0
    for position in range(opening, len(code)):
        depth += (code[position] == '{') - (code[position] == '}')
        if depth == 0:
            return text[matches[0].start():position + 1]
    raise ValueError('Unterminated native show_map_vma function')


def adapt_vma_source(text: str) -> str:
    function = show_map_function(text)
    fingerprint = digest(function.encode('utf-8'))
    # Reverse only our exact substitution to recognize an already repaired body.
    if function.count(NATIVE_END) == 1:
        restored = function.replace(NATIVE_END, PADDED_END, 1)
        if digest(restored.encode('utf-8')) == SUSFS_FUNCTION_SHA256:
            return text
    if fingerprint == NATIVE_FUNCTION_SHA256:
        return text
    if fingerprint != SUSFS_FUNCTION_SHA256 or function.count(PADDED_END) != 1:
        raise ValueError('Review the changed SUSFS OPEN_REDIRECT maps function before adapting its boundary')
    changed = function.replace(PADDED_END, NATIVE_END, 1)
    return text.replace(function, changed, 1)


def linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def read_source(common: Path, relative: str) -> tuple[Path, bytes]:
    path = common / relative
    if (any(linked(part) for part in (path, *path.parents)) or not path.is_file()
            or not 0 < path.stat().st_size <= 1024 * 1024):
        raise ValueError('Expected bounded regular native VMA compatibility sources')
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError('Native VMA compatibility source exceeds its bound')
    raw.decode('utf-8')
    return path, raw


def native_head(common: Path) -> None:
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(common),
                             'rev-parse', '--verify', 'HEAD'], capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    if result.returncode or result.stdout.strip() != COMMIT:
        raise ValueError('VMA compatibility requires the exact reviewed official common commit')


def prepare_vma_compat(common: Path, profile: dict) -> bool:
    if not isinstance(profile, dict):
        raise ValueError('Invalid official VMA compatibility profile')
    if profile.get('id') != PROFILE:
        return False
    reference = profile.get('kernel_ref', profile.get('verified_kernel_ref'))
    if (profile.get('kernel_repo') != REPOSITORY or reference != COMMIT
            or profile.get('verified_kernel_ref', reference) != COMMIT
            or profile.get('platform') != 'sm8650' or profile.get('kernel') != '6.1.57'):
        raise ValueError('VMA compatibility source pairing differs from the reviewed profile')
    common = Path(common).absolute()
    native_head(common)
    read_set = {}
    for relative, fingerprint in NATIVE_HEADERS.items():
        raw = read_source(common, relative)[1]
        if digest(raw) != fingerprint:
            raise ValueError('The reviewed native pre-padding MM header is missing or changed')
        read_set[relative] = raw
    # A newly introduced migration implementation must be reviewed as a whole;
    # never hide its real padding with a broad fallback macro.
    for relative in ('include/linux/pgsize_migration.h', 'mm/pgsize_migration.c',
                     'include/linux/page_size_compat.h', 'mm/page_size_compat.c'):
        path = common / relative
        if path.exists() or linked(path):
            raise ValueError('The reviewed legacy native tree unexpectedly contains page-size migration')
    relative = 'fs/proc/task_mmu.c'
    path, original = read_source(common, relative)
    read_set[relative] = original
    changed = adapt_vma_source(original.decode('utf-8')).encode('utf-8')
    if changed == original:
        return False
    temporary = path.with_name('.oplus-vma-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(changed)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        if any(read_source(common, name)[1] != raw for name, raw in read_set.items()):
            raise ValueError('Native VMA sources changed during compatibility preparation')
        native_head(common)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
