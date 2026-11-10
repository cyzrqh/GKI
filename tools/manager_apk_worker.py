"""Build preselected official APKs inside the scheduled upstream check."""
from __future__ import annotations

import copy
import re

from manager_lkm_jobs import lkm_cache_restore_steps
from manager_prebuild import CHANNELS, slot_for
from manager_variants import MANAGERS
from reuse_manager_jobs import ADOPTION, CACHE_PATH, SAVE_KEY, expression


PREPARE = 'prebuild-manager-sources'
NATIVE = 'prebuild-manager-native'
MANAGER = 'prebuild-manager-apks'
DISCOVERY = 'needs.check-upstream.outputs.manager_prebuild_'
SLOTS = tuple(slot_for(variant, tier) for variant in MANAGERS for tier in CHANNELS)
SUBSTITUTIONS = {
    'inputs.kernelsu_variant': 'matrix.variant',
    'inputs.kernelsu_branch': 'matrix.tier',
    'inputs.operation': "'仅编译管理器安装包'",
    'inputs.enable_susfs': 'false',
    "github.event_name == 'workflow_dispatch'": 'true',
}


def substitute(value):
    if isinstance(value, str):
        for before, after in SUBSTITUTIONS.items():
            value = value.replace(before, after)
        value = re.sub(r'needs\.prepare-matrix\.outputs\.(\w+)',
                       lambda match: f"needs.{PREPARE}.outputs[format('{{0}}_{match[1]}', matrix.slot)]", value)
        value = value.replace('prepare-matrix', PREPARE).replace('build-sukisu-lkm', NATIVE)
    elif isinstance(value, list):
        value = [substitute(item) for item in value]
    elif isinstance(value, dict):
        value = {key: substitute(item) for key, item in value.items()}
    return value


def matrix_job(job, matrix, maximum):
    job['strategy'] = {'fail-fast': False, 'max-parallel': maximum,
                       'matrix': expression('fromJSON(' + DISCOVERY + matrix + ')')}


def create_jobs(base):
    ids = ('prepare-matrix', 'build-sukisu-lkm', 'build-sukisu-manager')
    prepare, native, manager = (copy.deepcopy(base['jobs'][name]) for name in ids)
    prepare['name'] = '${{ matrix.variant }} · ${{ matrix.channels }} · 固定 APK 源码与签名'
    for key in ('matrix', 'count', 'full_catalog', 'max_parallel', 'susfs_sources'):
        prepare['outputs'].pop(key, None)
    omit = {'检查并生成矩阵', '校验旧版内核 SUSFS 映射兼容性', '验证共享源码还原及原生符号接口',
            '矩阵展开前统一下载并固定 SUSFS', '自动删除已成功的清理记录'}
    prepare['steps'] = [step for step in prepare['steps'] if step.get('name') not in omit]
    source = next(step for step in prepare['steps'] if step.get('id') == 'sources')
    source['env'].update(EXPECTED_REF='${{ matrix.reference }}', EXPECTED_SOURCE_BRANCH='${{ matrix.source_branch }}')
    source['run'] = '''set -euo pipefail
python3 tools/resolve_build_sources.py --variant "$KSU_VARIANT" --tier "$KSU_TIER" --susfs true
python3 - <<'PY'
import os
from pathlib import Path

values = dict(line.split('=', 1) for line in Path(os.environ['GITHUB_OUTPUT']).read_text().splitlines() if '=' in line)
if (values.get('ksu_ref') != os.environ['EXPECTED_REF']
        or values.get('ksu_source_branch') != os.environ['EXPECTED_SOURCE_BRANCH']):
    raise SystemExit('官方管理器版本在排队期间已变化，请重新检查后构建。')
PY
'''
    prepare['needs'] = ['check-upstream']
    prepare['if'] = f"needs.check-upstream.result == 'success' && {DISCOVERY}pending == 'true'"
    matrix_job(prepare, 'matrix', 6)
    prepare['outputs'] = {
        slot + '_' + field: expression(f"matrix.slot == '{slot}' && ({value[4:-3]}) || ''")
        for slot in SLOTS for field, value in prepare['outputs'].items() if value
    }
    native['needs'] = ['check-upstream', 'prepare-matrix']
    native['if'] = ("!cancelled() && needs.prepare-matrix.result == 'success' && ("
                    + ' || '.join(f"(contains(fromJSON({DISCOVERY}matrix).include.*.slot, '{slot}') && "
                                  f"needs.{PREPARE}.outputs['{slot}_manager_adoption'] == '')" for slot in SLOTS)
                    + ')')
    matrix_job(native, 'lkm_matrix', 24)
    manager['name'] = '${{ matrix.variant }} · ${{ matrix.channels }} · 预编译管理器 APK'
    manager['needs'] = ['check-upstream', 'prepare-matrix', 'build-sukisu-lkm']
    manager['if'] = ("always() && !cancelled() && needs.prepare-matrix.result == 'success' && "
                     "(needs.build-sukisu-lkm.result == 'success' || needs.build-sukisu-lkm.result == 'skipped')")
    matrix_job(manager, 'matrix', 3)
    manager['steps'] = [step for step in manager['steps'] if step.get('id') != 'manager-producer']
    manager.pop('outputs', None)
    manager['steps'].extend(lkm_cache_restore_steps(expression(SAVE_KEY), CACHE_PATH,
        id_prefix='prebuilt-apk-published', name='确认原始 APK 可供后续构建复用', lookup_only=True,
        condition=ADOPTION + " == ''"))
    return dict(zip((PREPARE, NATIVE, MANAGER), substitute([prepare, native, manager])))
