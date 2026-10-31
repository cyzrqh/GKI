"""Immutable CCTV OPlus schemes, separate from historical official-source builds.

The mirror is part of the scheme: substituting an official branch changes the
vendor fixes and is not an equivalent build. Manager sources remain independent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

import oplus_catalog as catalog

ROOT = Path(__file__).resolve().parents[1]
SCHEME = 'cctv-oplus-v1'
AK3_REPOSITORY = 'cctv18/AnyKernel3'
AK3_REF = '091ee586e1a5d5c1c1c957d48fe7c61638d04dc4'
AK3_SCRIPT_SHA256 = '86b434c116b17c24efa18d79ed59bd39a9391da6df45c71ce15141fa60769827'
SOURCE_REPOSITORIES = frozenset('cctv18/' + name for name in (
    'android_kernel_common_oneplus_sm8650', 'android_kernel_common_oneplus_sm8750',
    'android_kernel_common_oneplus_sm8850', 'android_kernel_common_oneplus_sm8845',
    'android_kernel_oneplus_mt6897', 'android_kernel_oneplus_mt6989',
    'android_kernel_oneplus_mt6991', 'android_kernel_oneplus_mt6993',
    'android_kernel_oppo_mt6993', 'android_gki_kernel_common'))
FIELDS = (catalog.FIELDS - {'verified_kernel_ref'}) | {
    'kernel_ref', 'source_committed_at', 'cctv_scheme'}


def is_cctv(profile):
    return isinstance(profile, dict) and 'cctv_scheme' in profile


def family(platform):
    return '通用 GKI' if platform == 'gki' else catalog.platform_family(platform)


def order(row):
    return tuple(map(int, row['kernel'].split('.'))), {
        '骁龙': 0, '天玑': 1, '通用 GKI': 2}[family(row['platform'])]


def label(row):
    return family(row['platform']) + ' · ' + row['kernel']


def load_profiles(root=ROOT):
    root = Path(root)
    document = catalog.strict_json((root / 'config/oplus-cctv-profiles.json').read_bytes())
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'profiles'}
            or type(document['schema_version']) is not int or document['schema_version'] != 1
            or not isinstance(document['profiles'], list) or not 1 <= len(document['profiles']) <= 128):
        raise catalog.CatalogError('Invalid pinned CCTV scheme catalog')
    rows, unique, recipes = [], set(), set()
    for row in document['profiles']:
        if (not isinstance(row, dict) or set(row) != FIELDS
                or any(not isinstance(row[key], str) for key in FIELDS)
                or row['cctv_scheme'] != SCHEME or row['kernel_repo'] not in SOURCE_REPOSITORIES
                or row['recipe_repository'] not in catalog.RECIPE_REPOSITORIES
                or not catalog.KERNEL.fullmatch(row['kernel'])
                or row['kernel_version'] != '.'.join(row['kernel'].split('.')[:2])
                or row['android_version'] != catalog.ANDROID[row['kernel_version']]
                or row['sub_level'] != row['kernel'].split('.')[-1]
                or row['recipe_repository'] != catalog.RECIPE_FOR_SERIES[row['kernel_version']]
                or not all(catalog.SHA.fullmatch(row[key]) for key in ('kernel_ref', 'recipe_ref'))
                or not catalog.SHA256.fullmatch(row['recipe_sha256'])
                or row['id'] != row['platform'] + '-' + row['kernel']):
            raise catalog.CatalogError('Inconsistent pinned CCTV scheme identity')
        platform = row['platform']
        if platform == 'gki':
            expected_suffix = '_gki'
            if row['kernel_repo'] != 'cctv18/android_gki_kernel_common':
                raise catalog.CatalogError('Generic GKI must use its own CCTV source')
        else:
            family(platform)
            if not row['kernel_repo'].endswith('_' + platform):
                raise catalog.CatalogError('CCTV platform and kernel source differ')
            expected_suffix = '_mtk' if row['recipe_workflow'].endswith('_mtk.yml') else ''
        if row['recipe_workflow'] != '.github/workflows/fastbuild_' + row['kernel'] + expected_suffix + '.yml':
            raise catalog.CatalogError('CCTV recipe does not match its platform and version')
        branch = row['kernel_branch']
        if (not re.fullmatch(r'[A-Za-z0-9_./-]{1,255}', branch)
                or any(part in ('', '.', '..') for part in branch.split('/'))):
            raise catalog.CatalogError('Unsafe CCTV source branch')
        for key in ('system', 'compatibility'):
            if not row[key] or len(row[key]) > 1024 or any(ord(c) < 32 for c in row[key]):
                raise catalog.CatalogError('Invalid CCTV source description')
        from datetime import datetime
        if datetime.fromisoformat(row['source_committed_at'].replace('Z', '+00:00')).tzinfo is None:
            raise catalog.CatalogError('CCTV source requires a timestamp with timezone')
        identity = family(platform), row['kernel']
        if identity in unique or row['recipe_workflow'] in recipes:
            raise catalog.CatalogError('Duplicate CCTV platform/version or recipe')
        unique.add(identity)
        recipes.add(row['recipe_workflow'])
        raw = (root / 'third_party/cctv-oplus/recipes' / Path(row['recipe_workflow']).name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['recipe_sha256']:
            raise catalog.CatalogError('Vendored CCTV recipe differs from its fixed checksum')
        rows.append(dict(row))
    return sorted(rows, key=order)


def validate_profile(profile):
    if not isinstance(profile, dict):
        raise catalog.CatalogError('Expected a pinned CCTV source profile')
    expected = next((row for row in load_profiles() if row['id'] == profile.get('id')), None)
    if expected is None or any(profile.get(key) != expected[key] for key in FIELDS):
        raise catalog.CatalogError('CCTV source profile differs from its reviewed scheme')
    return expected


def target_choices(profiles=None):
    return [catalog.ALL_TARGETS] + [label(row) for row in sorted(
        load_profiles() if profiles is None else profiles, key=order)]


def source_provenance(profile):
    row = validate_profile(profile)
    return {key: row[key] for key in ('cctv_scheme', 'kernel_repo', 'kernel_ref',
            'recipe_repository', 'recipe_ref', 'recipe_workflow', 'recipe_sha256')}


def prepare_maps(common, profile):
    """Retain native pre-padding MM semantics with official SUSFS 2.3.

    Only two old mirrors need this expression adapter. Use the already
    reviewed exact MM bytes and complete function fingerprints, not fuzz.
    """
    import os
    import uuid
    from oplus_recipe import git
    from oplus_vma_compat import read_source, digest
    row = validate_profile(profile)
    if row['id'] not in ('sm8650-6.1.57', 'mt6897-6.1.128'):
        return False
    if git(common, 'rev-parse', 'HEAD') != row['kernel_ref']:
        raise catalog.CatalogError('CCTV MM source changed from the fixed commit')
    if row['id'] == 'sm8650-6.1.57':
        from oplus_vma_compat import NATIVE_HEADERS, adapt_vma_source
        fingerprints, adapt = NATIVE_HEADERS, adapt_vma_source
        absent = ('include/linux/pgsize_migration.h', 'mm/pgsize_migration.c',
                  'include/linux/page_size_compat.h', 'mm/page_size_compat.c')
    else:
        from oplus_mtk_vma_compat import PROFILES, adapt_mtk_maps
        fingerprints, adapt = PROFILES[row['id']]['native'], adapt_mtk_maps
        absent = PROFILES[row['id']]['absent']
    if any((common / name).exists() or (common / name).is_symlink() for name in absent):
        raise catalog.CatalogError('Review added CCTV MM migration implementation before adapting SUSFS')
    inputs = {}
    for name, expected in fingerprints.items():
        data = read_source(common, name)[1]
        if digest(data) != expected:
            raise catalog.CatalogError('Review changed CCTV MM headers/lifecycle before adapting SUSFS')
        inputs[name] = data
    path, original = read_source(common, 'fs/proc/task_mmu.c')
    changed = adapt(original.decode('utf-8')).encode('utf-8')
    if changed == original:
        return False
    temporary = path.with_name('.cctv-maps-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(changed)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        if (read_source(common, 'fs/proc/task_mmu.c')[1] != original
                or any(read_source(common, name)[1] != raw for name, raw in inputs.items())
                or git(common, 'rev-parse', 'HEAD') != row['kernel_ref']):
            raise catalog.CatalogError('CCTV MM source changed during preparation')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def installer_script(profile, variant, prefix):
    validate_profile(profile)
    if variant not in ('SukiSU', 'BakaSU') or not re.fullmatch(
            re.escape(profile['kernel'] + '-' + profile['android_version']) + r'-[0-9]+', prefix):
        raise catalog.CatalogError('Invalid native CCTV installer target')
    path = ROOT / 'third_party/cctv-oplus/anykernel.sh'
    if hashlib.sha256(path.read_bytes()).hexdigest() != AK3_SCRIPT_SHA256:
        raise catalog.CatalogError('Vendored CCTV AnyKernel3 script checksum changed')
    raw = path.read_text(encoding='utf-8')
    # Preserve the native split/unpack/write/flash sequence and both optional
    # module installers. Add the real compiled version guard before split_boot.
    marker = '# boot install\nsplit_boot\n'
    if raw.count(marker) != 1:
        raise catalog.CatalogError('Pinned CCTV installer layout changed')
    guard = ('# boot install\n'
             'case "$(uname -r)" in\n'
             f'  {prefix}|{prefix}-*) ;;\n'
             f'  *) abort "Kernel/Android/KMI mismatch: requires {prefix}" ;;\n'
             'esac\nsplit_boot\n')
    return raw.replace(marker, guard, 1).encode('utf-8')


def discover_matrix(root=ROOT, *, target=catalog.ALL_TARGETS):
    rows = load_profiles(root)
    if target != catalog.ALL_TARGETS:
        rows = [row for row in rows if label(row) == target]
        if len(rows) != 1:
            raise catalog.CatalogError('Unknown CCTV platform/kernel target')
    return {'include': rows}


def main():
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', default=catalog.ALL_TARGETS)
    parser.add_argument('--github-output', action='store_true')
    parser.add_argument('--summary', action='store_true')
    parser.add_argument('--prepare-deps', action='store_true')
    parser.add_argument('--unicode-bypass', choices=('true', 'false'), default='false')
    args = parser.parse_args()
    if args.prepare_deps:
        from oplus_kernel import resolve_dependency, prepared_outputs
        prepared_outputs({'ak3_ref': AK3_REF, 'patches_ref':
            resolve_dependency('Xiaomichael/kernel_patches') if args.unicode_bypass == 'true' else ''})
        return 0
    matrix = discover_matrix(target=args.target)
    if args.github_output:
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('matrix=' + json.dumps(matrix, ensure_ascii=False, separators=(',', ':')) + '\n')
            stream.write('full_catalog=' + str(args.target == catalog.ALL_TARGETS).lower() + '\n')
    if args.summary:
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write('### 本次 CCTV 欧加真方案\n\n')
            stream.write('使用对应配方的 CCTV 内核树；管理器及配套 APK 仍使用所选官方源码。\n\n')
            stream.write('| 目标 | 内核源码提交 | 构建配方提交 |\n|---|---|---|\n')
            for row in matrix['include']:
                stream.write(f"| {label(row)} | `{row['kernel_ref']}` | `{row['recipe_ref']}` |\n")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
