"""Build one previously selected official APK through the shared workflow jobs."""
from __future__ import annotations

import copy

from manager_lkm_jobs import lkm_cache_restore_steps
from reuse_manager_jobs import ADOPTION, CACHE_PATH, SAVE_KEY, expression


OUTPUT = '.github/workflows/_manager-apk-worker.yml'
SUBSTITUTIONS = {
    'inputs.kernelsu_variant': 'inputs.variant',
    'inputs.kernelsu_branch': 'inputs.tier',
    'inputs.operation': "'仅编译管理器安装包'",
    'inputs.enable_susfs': 'false',
    "github.event_name == 'workflow_dispatch'": 'true',
}


def substitute(value):
    if isinstance(value, str):
        for before, after in SUBSTITUTIONS.items():
            value = value.replace(before, after)
    elif isinstance(value, list):
        value = [substitute(item) for item in value]
    elif isinstance(value, dict):
        value = {key: substitute(item) for key, item in value.items()}
    return value


def create_workflow(base):
    ids = ('prepare-matrix', 'build-sukisu-lkm', 'build-sukisu-manager')
    jobs = substitute(copy.deepcopy({name: base['jobs'][name] for name in ids}))
    prepare = jobs['prepare-matrix']
    prepare['name'] = '固定官方管理器源码与签名'
    for key in ('matrix', 'count', 'full_catalog', 'max_parallel', 'susfs_sources'):
        prepare['outputs'].pop(key, None)
    omit = {'检查并生成矩阵', '校验旧版内核 SUSFS 映射兼容性', '验证共享源码还原及原生符号接口',
            '矩阵展开前统一下载并固定 SUSFS', '自动删除已成功的清理记录'}
    prepare['steps'] = [step for step in prepare['steps'] if step.get('name') not in omit]
    source = next(step for step in prepare['steps'] if step.get('id') == 'sources')
    source['env'].update(EXPECTED_REF='${{ inputs.reference }}', EXPECTED_SOURCE_BRANCH='${{ inputs.source_branch }}')
    source['run'] = '''set -euo pipefail
python3 tools/resolve_build_sources.py --variant "$KSU_VARIANT" --tier "$KSU_TIER" --susfs false
python3 - <<'PY'
import os
from pathlib import Path

values = dict(line.split('=', 1) for line in Path(os.environ['GITHUB_OUTPUT']).read_text().splitlines() if '=' in line)
if (values.get('ksu_ref') != os.environ['EXPECTED_REF']
        or values.get('ksu_source_branch') != os.environ['EXPECTED_SOURCE_BRANCH']):
    raise SystemExit('官方管理器版本在排队期间已变化，请重新检查后构建。')
PY
'''
    # Only the default branch may publish reusable APKs signed with the
    # repository key. Both discovery callers hand over official source IDs.
    prepare['if'] = ("github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && "
                     "(inputs.tier == 'Stable(标准)' || inputs.tier == 'Latest(最新)')")
    manager = jobs['build-sukisu-manager']
    manager['steps'] = [step for step in manager['steps'] if step.get('id') != 'manager-producer']
    manager['outputs'].pop('manager_job_id')
    manager['steps'].extend(lkm_cache_restore_steps(expression(SAVE_KEY), CACHE_PATH,
        id_prefix='prebuilt-apk-published', name='确认原始 APK 可供后续构建复用', lookup_only=True,
        condition=ADOPTION + " == ''"))
    return {
        'name': '配套管理器 APK 构建',
        'on': {'workflow_call': {'inputs': {name: {'type': 'string', 'required': True} for name in
                                            ('variant', 'tier', 'reference', 'source_branch')}}},
        'permissions': {'contents': 'read', 'actions': 'write'},
        'jobs': jobs,
    }
