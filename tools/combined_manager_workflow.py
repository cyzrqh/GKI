"""One visible pipeline, with rows for only the selected managers."""
from __future__ import annotations
import copy
import json
import re
from manager_variants import COMBINED_GROUPS, MANAGERS
from workflow_jobs import release_publish_steps, release_tag_concurrency


class SharedSteps(list):
    _gki_shared_steps = True


def expression(value):
    return '${{ ' + value + ' }}'


COMBINED = '(' + ' || '.join("inputs.kernelsu_variant == '" + selection + "'"
                             for selection in COMBINED_GROUPS) + ')'
VARIANTS = {'variant': expression('fromJSON(needs.select-managers.outputs.active_managers)')}
SELECTION_READY = "needs.select-managers.result == 'success' && needs.select-managers.outputs.active_count != '0'"


def rewrite(value, replacements):
    if isinstance(value, str):
        for before, after in replacements:
            value = value.replace(before, after)
        return value
    if isinstance(value, list):
        return [rewrite(child, replacements) for child in value]
    if isinstance(value, dict):
        return {key: rewrite(child, replacements) for key, child in value.items()}
    return value


def scoped(value):
    if isinstance(value, str):
        value = value.replace('inputs.kernelsu_variant', 'matrix.variant')
        return re.sub(r'needs\.(prepare-matrix|build-sukisu-manager)\.outputs\.(\w+)',
            lambda m: 'needs.' + m[1] + ".outputs[format('{0}_" + m[2] + "', matrix.variant)]", value)
    if isinstance(value, list):
        return [scoped(child) for child in value]
    if isinstance(value, dict):
        return {key: scoped(child) for key, child in value.items()}
    return value


def outputs_by_manager(outputs):
    # Actions omits empty outputs. Unique names prevent matrix completion
    # order from replacing another manager's source or signing identity.
    return {variant + '_' + key: expression("matrix.variant == '" + variant + "' && ("
            + (value.removeprefix('${{ ').removesuffix(' }}') if value else "''") + ") || ''")
            for variant in MANAGERS for key, value in outputs.items()}


def selected_manager_job(original_prepare):
    """Resolve every selected source once, before scheduling expensive work."""
    original_sources = next(step for step in original_prepare['steps'] if step.get('id') == 'sources')
    environment = {key: value for key, value in original_sources['env'].items() if key != 'KSU_VARIANT'}
    environment['MANAGER_SELECTION'] = '${{ inputs.kernelsu_variant }}'
    checkout = next(step for step in original_prepare['steps']
                    if step.get('uses') == 'actions/checkout@v6' and 'repository' not in step.get('with', {}))
    return {
        'name': '选择可构建的管理器', 'if': original_prepare['if'],
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 10,
        'permissions': {'contents': 'read'},
        'outputs': {key: expression('steps.selection.outputs.' + key)
                    for key in ('active_managers', 'active_count', 'resolution')},
        'steps': [copy.deepcopy(checkout), {
            'name': '固定正式版或最新版，跳过未发布正式版的组合成员', 'id': 'selection',
            'env': environment,
            'run': 'python3 tools/resolve_manager_selection.py --selection "$MANAGER_SELECTION" --tier "$KSU_TIER" \\\n'
                   '  --custom-ref "$CUSTOM_REF" --susfs "$ENABLE_SUSFS"',
        }],
    }


def require_selection(job):
    dependencies = job.get('needs', [])
    dependencies = [dependencies] if isinstance(dependencies, str) else list(dependencies)
    if 'select-managers' not in dependencies:
        dependencies.append('select-managers')
    job['needs'] = dependencies


def add_combined(workflow, *, oplus):
    root = copy.deepcopy(workflow)
    kernel_id = 'build-oplus-kernel' if oplus else 'build-kernel'
    publish_id = 'verify-oplus-assets' if oplus else 'publish-release'
    jobs = root['jobs']
    original_prepare = copy.deepcopy(jobs['prepare-matrix'])
    original_manager = copy.deepcopy(jobs['build-sukisu-manager'])
    # Keep a single lightweight discovery stage. Empty active selections are
    # successful no-op runs, so no empty manager matrix reaches a runner.
    jobs = {'select-managers': selected_manager_job(original_prepare), **jobs}
    root['jobs'] = jobs
    for name in ('prepare-matrix', 'build-sukisu-lkm', 'build-sukisu-manager', kernel_id):
        jobs[name] = scoped(jobs[name])
        jobs[name].setdefault('env', {})['KSU_VARIANT'] = expression('matrix.variant')
        jobs[name]['env']['GKI_COMBINED_WORKER'] = expression(COMBINED + " && 'true' || 'false'")

    prepare = jobs['prepare-matrix']
    require_selection(prepare)
    prepare['if'] = SELECTION_READY + ' && (' + original_prepare['if'] + ')'
    prepare['name'] = expression(COMBINED + " && format('准备源码与构建目标 · {0}', matrix.variant) || '选择内核版本与补丁月份'")
    prepare['strategy'] = {'fail-fast': False, 'matrix': copy.deepcopy(VARIANTS)}
    prepare['outputs'] = outputs_by_manager(original_prepare['outputs'])
    prepare['outputs'].update({key: expression('steps.selected-layout.outputs.' + key)
        for key in ('kernel_primary', 'kernel_overflow', 'overflow_count', 'ddk_build_matrix')})
    sources = next(step for step in prepare['steps'] if step.get('id') == 'sources')
    sources['env']['MANAGER_RESOLUTION'] = '${{ needs.select-managers.outputs.resolution }}'
    sources['run'] = 'python3 tools/resolve_manager_selection.py --source-for "$KSU_VARIANT"'
    prepare['steps'].append({'name': '只展开所选管理器的构建目标', 'id': 'selected-layout',
        'env': {'KERNEL_MATRIX': '${{ steps.matrix.outputs.matrix }}',
                'DDK_MATRIX': '${{ steps.lkm-matrix.outputs.matrix }}',
                'MANAGER_SELECTION': '${{ inputs.kernelsu_variant }}',
                'MANAGER_RESOLUTION': '${{ needs.select-managers.outputs.resolution }}'},
        'run': 'python3 tools/manager_matrix_layout.py'})
    ready = "always() && !cancelled() && needs.prepare-matrix.result == 'success'"
    lkm = jobs['build-sukisu-lkm']
    lkm['if'] = ready
    lkm['strategy']['matrix'] = '${{ fromJSON(needs.prepare-matrix.outputs.ddk_build_matrix) }}'
    manager = jobs['build-sukisu-manager']
    require_selection(manager)
    manager['if'] = ready + ' && ' + SELECTION_READY + " && needs.build-sukisu-lkm.result == 'success'"
    manager['strategy'] = {'fail-fast': False, 'matrix': copy.deepcopy(VARIANTS)}
    manager['outputs'] = outputs_by_manager(original_manager['outputs'])

    kernel = jobs[kernel_id]
    kernel_gate = ready + " && needs.build-sukisu-manager.result == 'success'"
    if not oplus:
        kernel_gate += " && inputs.operation == '编译内核'"
    kernel['if'] = kernel_gate
    kernel['strategy']['matrix'] = '${{ fromJSON(needs.prepare-matrix.outputs.kernel_primary) }}'
    if not oplus:
        kernel['strategy']['max-parallel'] = "${{ fromJSON(format('{0}', inputs.max_parallel)) }}"
    if oplus:
        profile = next(s for s in kernel['steps'] if 'OPLUS_PROFILE' in s.get('env', {}))
        before = "profile = profile_identity(strict_json(os.environ['OPLUS_PROFILE'].encode()))"
        after = ("profile_fields = strict_json(os.environ['OPLUS_PROFILE'].encode())\n"
                 "profile_fields.pop('variant')\nprofile = profile_identity(profile_fields)")
        if profile['run'].count(before) != 1:
            raise ValueError('Review the OPlus profile validation before routing managers')
        profile['run'] = profile['run'].replace(before, after, 1)
    kernel['steps'] = SharedSteps(kernel['steps'])
    overflow_id = kernel_id + '-overflow'
    overflow = copy.deepcopy(kernel)
    overflow['name'] = '扩展内核批次（仅联合全量超过 256 个目标时执行）'
    overflow['if'] += " && needs.prepare-matrix.outputs.overflow_count != '0'"
    overflow['strategy']['matrix'] = '${{ fromJSON(needs.prepare-matrix.outputs.kernel_overflow) }}'
    overflow['steps'] = kernel['steps']
    jobs[overflow_id] = overflow

    kernel_success = ("needs." + kernel_id + ".result == 'success' && ((needs.prepare-matrix.outputs.overflow_count == '0'"
                      " && needs." + overflow_id + ".result == 'skipped') || (needs.prepare-matrix.outputs.overflow_count != '0'"
                      " && needs." + overflow_id + ".result == 'success'))")
    if not oplus:
        kernel_success = "inputs.operation == '仅编译管理器安装包' || (" + kernel_success + ')'
    release_environment = {
        'MANAGER_SELECTION': '${{ inputs.kernelsu_variant }}',
        'MANAGER_RESOLUTION': '${{ needs.select-managers.outputs.resolution }}',
        'RELEASE_PLATFORM': 'oplus' if oplus else 'gki',
        'OPERATION': '编译内核' if oplus else '${{ inputs.operation }}',
    }
    # Pass only the small per-manager identities, not the complete prepare
    # output object: a large full matrix can exceed Linux's per-variable limit.
    for variant in MANAGERS:
        prefix = 'MANAGER_' + re.sub(r'[^A-Za-z0-9]', '_', variant).upper() + '_'
        for field, output, job_name, fallback in (
                ('APK_FILENAME', 'apk_filename', 'build-sukisu-manager', None),
                ('METADATA', 'manager_metadata', 'build-sukisu-manager', None),
                ('EXPECTED_COUNT', 'count', 'prepare-matrix', "'0'"),
                ('FULL_CATALOG', 'full_catalog', 'prepare-matrix', "'false'")):
            value = 'needs.' + job_name + ".outputs['" + variant.lower() + '_' + output + "']"
            release_environment[prefix + field] = expression(value + (' || ' + fallback if fallback else ''))
    if oplus:
        normalized = {variant: variant.lower() for variant in MANAGERS}
        first = ("fromJSON('" + json.dumps(normalized, separators=(',', ':'))
                 + "')[fromJSON(needs.select-managers.outputs.active_managers)[0]]")
        release_environment['OPLUS_MATRIX'] = expression(
            "needs.prepare-matrix.outputs[format('{0}_matrix', " + first + ')]')
    jobs[publish_id] = {
        'name': '核验与发行',
        'needs': ['select-managers', 'prepare-matrix', 'build-sukisu-manager', kernel_id, overflow_id],
        'if': ready + ' && ' + SELECTION_READY + " && needs.build-sukisu-manager.result == 'success' && (" + kernel_success + ')',
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 30,
        'permissions': {'contents': 'write', 'actions': 'write'},
        'concurrency': release_tag_concurrency(),
        'steps': [
            {'name': '检出发布工具', 'uses': 'actions/checkout@v6', 'with': {'persist-credentials': False}},
            {'name': '准备发行内容与签名验证依赖',
             'run': "python3 -c 'import yaml, cryptography' || python3 -m pip install --break-system-packages --disable-pip-version-check PyYAML cryptography"},
            {'name': '收集本次原文件 APK 与内核', 'uses': 'actions/download-artifact@v8',
             'with': {'pattern': '{*.apk,*-AnyKernel3.zip,*-boot*.img}', 'path': 'release-input',
                      'merge-multiple': False, 'skip-decompress': True}},
            {'name': '逐管理器核验并汇总发行文件', 'id': 'selected-release', 'env': release_environment,
             'run': 'python3 tools/prepare_selected_release.py --input release-input --output release-publish'},
            {'name': '上传原文件校验清单', 'uses': 'actions/upload-artifact@v7',
             'with': {'path': 'release-publish/assets/SHA256SUMS.txt', 'archive': False,
                      'if-no-files-found': 'error', 'overwrite': True}},
            *release_publish_steps("steps.selected-release.outputs.publish == 'true'"),
            {'name': '显示构建配置与发行结果', 'if': 'always() && !cancelled()',
             'env': {'PREPARED': '${{ steps.selected-release.outcome }}',
                     'PUBLISH': '${{ steps.selected-release.outputs.publish }}',
                     'RELEASE_RESULT': '${{ steps.release.outcome }}'},
             'run': '''if [ "$PREPARED" = success ]; then
  if [ "$RELEASE_RESULT" = success ]; then
    echo '所选管理器的完整内核目录全部成功，已统一发布 Release。' >> "$GITHUB_STEP_SUMMARY"
  elif [ "$PUBLISH" != true ]; then
    echo '本次构建范围不满足完整目录发行条件，原文件和校验清单在 Actions 下载。' >> "$GITHUB_STEP_SUMMARY"
  else
    echo '文件核验已完成，发行步骤未完成，请查看对应日志。' >> "$GITHUB_STEP_SUMMARY"
  fi
  printf '\\n' >> "$GITHUB_STEP_SUMMARY"
  cat release-publish/release.md >> "$GITHUB_STEP_SUMMARY"
else
  echo '构建产物核验未完成，请查看对应日志。' >> "$GITHUB_STEP_SUMMARY"
fi
'''},
        ],
    }
    return root, None
