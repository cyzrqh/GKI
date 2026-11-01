"""Extend native single-manager jobs without replacing their tested build recipes."""
from __future__ import annotations

import copy

from additional_manager_jobs import ALL_IF, LOCAL_IF, ADDITIONAL_IF
from manager_variants import MANAGER_SELECTIONS

RECEIPT = '${{ needs.build-sukisu-manager.outputs.manager_metadata }}'
EFFECTIVE_SUSFS = "(inputs.enable_susfs && inputs.kernelsu_variant != 'KowSU')"


def effective_inputs(value):
    if isinstance(value, str):
        return (value.replace(EFFECTIVE_SUSFS, '__GKI_EFFECTIVE_SUSFS__')
                .replace('inputs.enable_susfs', EFFECTIVE_SUSFS)
                .replace('__GKI_EFFECTIVE_SUSFS__', EFFECTIVE_SUSFS))
    if isinstance(value, list):
        return [effective_inputs(child) for child in value]
    if isinstance(value, dict):
        return {key: effective_inputs(child) for key, child in value.items()}
    return value


def extend_gki(workflow: dict) -> dict:
    fields = workflow['on']['workflow_dispatch']['inputs']
    fields['kernelsu_variant']['options'] = list(MANAGER_SELECTIONS)
    job = workflow['jobs']['build-kernel']
    job['env']['GKI_MANAGER_METADATA'] = RECEIPT
    source_steps = {'恢复本次不可变源码缓存', '恢复本次不可变源码备份', '核实共享源码缓存',
                    '检出内核配套的管理器快照', '编译前读取本次 APK 的实际签名信息',
                    '从官方固定提交恢复并核实源码', '核对固定源码与准备任务的完整身份'}
    old = "inputs.kernelsu_variant == 'SukiSU' || inputs.kernelsu_variant == 'BakaSU'"
    for step in job['steps']:
        # Only immutable-source/identity steps: KPM remains limited to its two native bridges.
        if step.get('name') in source_steps or any(word in step.get('name', '') for word in ('源码缓存', '源码备份')):
            if 'if' in step:
                step['if'] = step['if'].replace(old, ALL_IF)
        if step.get('name') == '添加 KernelSU':
            integration = ('python3 "$GITHUB_WORKSPACE/tools/integrate_sukisu.py" '
                           '--kernel-root "$KERNEL_ROOT" --expected-ref "$BRANCH"')
            step['run'] = ('set -euo pipefail\ncase "$KSU_VARIANT" in\n'
                           '  "SukiSU")\n    ' + integration + '\n    ;;\n'
                           '  "BakaSU")\n    ' + integration + '\n    ;;\n'
                           '  "KernelSU-Next"|"KernelSU"|"KowSU"|"FinaSU")\n    ' + integration + '\n    ;;\n'
                           '  *) echo "Unsupported paired manager" >&2; exit 1 ;;\nesac\n')
        if step.get('name') == '配对管理器包名与签名':
            step['if'] = LOCAL_IF
    position = next(i for i, step in enumerate(job['steps']) if step.get('name') == '应用 SUSFS 补丁')
    job['steps'].insert(position, {
        'name': '接入新增管理器的原生 SUSFS 接口',
        'if': 'inputs.enable_susfs && ' + ADDITIONAL_IF,
        'run': 'python3 tools/additional_manager_integration.py --kernel-root "$KERNEL_ROOT" --susfs-dir "$SUSFS4KSU"'})
    position = next(i for i, step in enumerate(job['steps']) if step.get('name') == '配对管理器包名与签名')
    job['steps'].insert(position + 1, {
        'name': '绑定官方原始 APK 的证书与版本', 'if': ADDITIONAL_IF,
        'run': 'python3 tools/additional_manager_integration.py --kernel-root "$KERNEL_ROOT"'})
    # In combined workers, each target has a manager-qualified file basename.
    ak3 = next(step for step in job['steps'] if step.get('name') == '准备 AnyKernel3 刷机包')
    ak3['run'] = ak3['run'].replace('-AnyKernel3.zip"', '${GKI_MANAGER_SUFFIX:-}-AnyKernel3.zip"')
    capture = next(step for step in job['steps'] if step.get('name') == '记录内核实际配置')
    capture['run'] = ('if [ "${GKI_COMBINED_WORKER:-false}" = true ]; then\n'
                      '  META_FILE="${META_FILE%-AnyKernel3.zip}-${KSU_VARIANT}-AnyKernel3.zip"\n'
                      '  export META_FILE\n'
                      '  for image in ./*-boot*.img; do\n'
                      '    [ -f "$image" ] || continue\n'
                      '    base="${image%%-boot*}"; rest="${image#"$base"}"\n'
                      '    mv -- "$image" "${base}-${KSU_VARIANT}${rest}"\n'
                      '  done\nfi\n' + capture['run'])
    job['steps'].insert(0, {'name': '设置组合构建文件名', 'if': "env.GKI_COMBINED_WORKER == 'true'",
                           'run': 'echo "GKI_MANAGER_SUFFIX=-${KSU_VARIANT}" >> "$GITHUB_ENV"'})
    workflow['jobs']['publish-release']['env']['GKI_MANAGER_METADATA'] = RECEIPT
    return effective_inputs(workflow)


def extend_oplus(workflow: dict) -> dict:
    for job_name in ('build-oplus-kernel', 'verify-oplus-assets'):
        workflow['jobs'][job_name]['env']['GKI_MANAGER_METADATA'] = RECEIPT
    return effective_inputs(workflow)
