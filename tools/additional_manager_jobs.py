"""Add official-APK producers without changing the existing local manager builds."""
from __future__ import annotations

import copy

from manager_variants import LOCAL_MANAGERS, ADDITIONAL_MANAGERS


def variant_condition(variants):
    return '(' + ' || '.join("inputs.kernelsu_variant == '" + value + "'" for value in variants) + ')'


LOCAL_IF = variant_condition(LOCAL_MANAGERS)
ADDITIONAL_IF = variant_condition(ADDITIONAL_MANAGERS)
ALL_IF = '(' + LOCAL_IF + ' || ' + ADDITIONAL_IF + ')'


def extend_manager_job(original: dict) -> dict:
    job = copy.deepcopy(original)
    job['if'] = ("always() && !cancelled() && needs.prepare-matrix.result == 'success' && "
                 "(" + ADDITIONAL_IF + " || (" + LOCAL_IF + " && needs.build-sukisu-lkm.result == 'success'))")
    checked_out = False
    for step in job['steps']:
        name = step.get('name', '')
        if name == '检出同一版本的管理器源码':
            checked_out = True
            continue
        if name == '检查固定 APK 签名配置' or checked_out and name != '上传管理器安装包':
            condition = step.get('if')
            if condition not in ("inputs.kernelsu_variant == 'SukiSU'", "inputs.kernelsu_variant == 'BakaSU'"):
                step['if'] = '(' + condition + ') && ' + LOCAL_IF if condition else LOCAL_IF
        if name == '上传管理器安装包':
            step['with']['path'] = 'manager-artifact/${{ steps.sign.outputs.apk_filename || steps.official-apk.outputs.apk_filename }}'
    for key in ('manager_package', 'manager_cert_size', 'manager_cert_sha256', 'apk_sha256',
                'manager_version_name', 'manager_version_code', 'apk_filename'):
        job['outputs'][key] = '${{ steps.sign.outputs.' + key + ' || steps.official-apk.outputs.' + key + ' }}'
    job['outputs']['manager_metadata'] = '${{ steps.official-apk.outputs.manager_metadata }}'
    index = next(i for i, step in enumerate(job['steps']) if step.get('name') == '上传管理器安装包')
    job['steps'].insert(index, {
        'name': '获取并验证官方同版本管理器 APK', 'id': 'official-apk', 'if': ADDITIONAL_IF,
        'timeout-minutes': 15,
        'env': {'GH_TOKEN': '${{ secrets.UPSTREAM_SYNC_TOKEN || github.token }}',
                'KSU_REF': '${{ needs.prepare-matrix.outputs.ksu_ref }}',
                'KSU_TIER': '${{ needs.prepare-matrix.outputs.effective_ksu_branch }}',
                'KSU_SOURCE_BRANCH': '${{ needs.prepare-matrix.outputs.ksu_source_branch }}'},
        'run': '''python3 tools/official_manager_apk.py --source manager-source --output manager-artifact \\
  --variant "$KSU_VARIANT" --expected-ref "$KSU_REF" --tier "$KSU_TIER" \\
  --source-branch "$KSU_SOURCE_BRANCH"
'''})
    return job
