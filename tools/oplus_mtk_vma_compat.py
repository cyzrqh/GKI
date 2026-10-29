"""Keep SUSFS maps boundaries consistent with the selected MTK native MM.

The 6.1.75 tree predates page-size migration. The 6.1.128 tree contains
unconnected migration source files, but its Kbuild, madvise, mmap and native
maps implementation do not enable that feature. Preserve those native
lifecycles rather than adding an unavailable vma_pad_pages dependency.
The 6.1.115/134 trees have working migration integration and remain untouched.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import uuid

from oplus_vma_compat import digest, linked, read_source, show_map_function


PROFILES = {
    'mt6897-6.1.75': {
        'repository': 'oppo-source/android_kernel_oppo_mt6897',
        'commit': '1bed026fb776f4167c7af2d9358e25b792adaa79',
        'native': {
            'include/linux/mm.h': '898360753467375651f849743dd80c2c4ac728d694fa85dc32a75d23e544a729',
            'include/linux/mm_types.h': '066d14a64a6da4ad1102ed554652c4c569cf1ebb9e304da667a04d533f93aca0',
            'mm/Makefile': '650c865aa1ed82238827808e94d096d75e77a6649b1b216042bfdc8fa220731b',
            'mm/madvise.c': '177b552df9f8df3a76162db009d8a04f825d269eaf9af326d469d25d2d8b003a',
            'mm/mmap.c': 'c7a8d5bd20e7d1cc3f50bce867aeaaa20455c1901d9522e776eff6e37cb2d5fa',
        },
        'absent': ('include/linux/pgsize_migration.h', 'include/linux/pgsize_migration_inline.h',
                   'mm/pgsize_migration.c', 'include/linux/page_size_compat.h', 'mm/page_size_compat.c'),
    },
    'mt6897-6.1.128': {
        'repository': 'OnePlusOSS/android_kernel_oneplus_mt6897',
        'commit': 'bdf1628572ce25c64d3fd33f09a925d54d616454',
        'native': {
            'include/linux/mm.h': '7ddb9b45265b9fa6cfb93d5399a6240a270dd64c796f0a310be3aac5ca2b03d4',
            'include/linux/mm_types.h': '066d14a64a6da4ad1102ed554652c4c569cf1ebb9e304da667a04d533f93aca0',
            'mm/Makefile': '7539ea83bc303aa85ce86787a80c52fc738d7e5de8ba1d716308da386ca17b43',
            'mm/madvise.c': 'db3ee159b233e25898957bfa2fa080911f39f183fe4a13c96f55e4052f0c1c86',
            'mm/mmap.c': 'db587b1135879f9c4acbec638d060357c6ecde7b8075fd603218f5a8d648c56d',
            'include/linux/pgsize_migration.h': '0c0e53268712b99a6faf19f807fc530e02307f13bf2ff0c3b04d84e759542005',
            'include/linux/pgsize_migration_inline.h': '1c318800fa00d56e51cf7a2c279daf89ecc98ad7053a790722864882445109af',
            'mm/pgsize_migration.c': '3eb77846a0e884dbd4c3a2dee2ebf89944e826e9bd982809b95498acdbfa50ac',
        },
        'absent': ('include/linux/page_size_compat.h', 'mm/page_size_compat.c'),
    },
}
NATIVE_FUNCTION_SHA256 = '45f11bed5cf914d7929361d54489f3189e3f5cedfd9675c32f9859b4df1f6d8c'
SUSFS_FUNCTION_SHA256 = '23e97c68e76849cc475c76be97894b23d3114f7dcc03990b3e97171a674753ff'
PADDED_END = '\t\t\t\tend = VMA_PAD_START(vma);'
NATIVE_END = '\t\t\t\tend = vma->vm_end;'


def native_head(common: Path, expected: str) -> None:
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(common),
                             'rev-parse', '--verify', 'HEAD'], capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    if result.returncode or result.stdout.strip() != expected:
        raise ValueError('MTK maps compatibility requires the exact reviewed official common commit')


def check_native(common: Path, selected: dict) -> dict[str, bytes]:
    native_head(common, selected['commit'])
    inputs = {}
    for relative, fingerprint in selected['native'].items():
        raw = read_source(common, relative)[1]
        if digest(raw) != fingerprint:
            raise ValueError('Review the changed native MTK padding lifecycle before adapting maps')
        inputs[relative] = raw
    for relative in selected['absent']:
        path = common / relative
        if path.exists() or linked(path):
            raise ValueError('The reviewed MTK maps source unexpectedly gained page-size migration')
    return inputs


def adapt_mtk_maps(text: str) -> str:
    function = show_map_function(text)
    if function.count(NATIVE_END) == 1:
        restored = function.replace(NATIVE_END, PADDED_END, 1)
        if digest(restored.encode('utf-8')) == SUSFS_FUNCTION_SHA256:
            return text
    fingerprint = digest(function.encode('utf-8'))
    if fingerprint == NATIVE_FUNCTION_SHA256:
        return text
    if fingerprint != SUSFS_FUNCTION_SHA256 or function.count(PADDED_END) != 1:
        raise ValueError('Review the changed MTK SUSFS OPEN_REDIRECT maps function')
    return text.replace(function, function.replace(PADDED_END, NATIVE_END, 1), 1)


def prepare_mtk_vma_compat(common: Path, profile: dict) -> bool:
    if not isinstance(profile, dict):
        raise ValueError('Invalid official MTK maps profile')
    selected = PROFILES.get(profile.get('id'))
    if selected is None:
        return False
    reference = profile.get('kernel_ref', profile.get('verified_kernel_ref'))
    if (profile.get('kernel_repo') != selected['repository'] or reference != selected['commit']
            or profile.get('verified_kernel_ref', reference) != reference
            or profile.get('platform') != 'mt6897'
            or profile.get('kernel') != profile['id'].removeprefix('mt6897-')):
        raise ValueError('MTK maps source pairing differs from the reviewed native profile')
    common = Path(common).absolute()
    inputs = check_native(common, selected)
    relative = 'fs/proc/task_mmu.c'
    target, original = read_source(common, relative)
    inputs[relative] = original
    changed = adapt_mtk_maps(original.decode('utf-8')).encode('utf-8')
    if changed == original:
        return False
    temporary = target.with_name('.oplus-mtk-vma-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(changed)
        temporary.chmod(target.stat().st_mode & 0o777)
        if any(read_source(common, name)[1] != raw for name, raw in inputs.items()):
            raise ValueError('Native MTK maps sources changed during compatibility preparation')
        # Recheck the absence predicates as well as bytes and source identity.
        check_native(common, selected)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True
