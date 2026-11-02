"""One visible pipeline, with rows for only the selected managers."""
from __future__ import annotations
import copy
import json
import re
from manager_variants import COMBINED_SELECTION, COMBINED_MANAGERS, MANAGERS


class SharedSteps(list):
    _gki_shared_steps = True


def expression(value):
    return '${{ ' + value + ' }}'


COMBINED = "inputs.kernelsu_variant == '" + COMBINED_SELECTION + "'"
SELECTION = (COMBINED + " && '" + json.dumps(COMBINED_MANAGERS, separators=(',', ':'))
             + "' || format('[\"{0}\"]', inputs.kernelsu_variant)")
VARIANTS = {'variant': expression('fromJSON(' + SELECTION + ')')}


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


def add_combined(workflow, *, oplus):
    root = copy.deepcopy(workflow)
    kernel_id = 'build-oplus-kernel' if oplus else 'build-kernel'
    publish_id = 'verify-oplus-assets' if oplus else 'publish-release'
    jobs = root['jobs']
    original_prepare = copy.deepcopy(jobs['prepare-matrix'])
    original_manager = copy.deepcopy(jobs['build-sukisu-manager'])
    for name in ('prepare-matrix', 'build-sukisu-lkm', 'build-sukisu-manager', kernel_id, publish_id):
        jobs[name] = scoped(jobs[name])
        jobs[name].setdefault('env', {})['KSU_VARIANT'] = expression('matrix.variant')
        jobs[name]['env']['GKI_COMBINED_WORKER'] = expression(COMBINED + " && 'true' || 'false'")

    prepare = jobs['prepare-matrix']
    prepare['if'] = original_prepare['if']
    prepare['name'] = expression(COMBINED + " && format('准备源码与构建目标 · {0}', matrix.variant) || '选择内核版本与补丁月份'")
    prepare['strategy'] = {'fail-fast': False, 'matrix': copy.deepcopy(VARIANTS)}
    prepare['outputs'] = outputs_by_manager(original_prepare['outputs'])
    prepare['outputs'].update({key: expression('steps.selected-layout.outputs.' + key)
        for key in ('kernel_primary', 'kernel_overflow', 'overflow_count', 'ddk_build_matrix')})
    prepare['steps'].append({'name': '只展开所选管理器的构建目标', 'id': 'selected-layout',
        'env': {'KERNEL_MATRIX': '${{ steps.matrix.outputs.matrix }}',
                'DDK_MATRIX': '${{ steps.lkm-matrix.outputs.matrix }}',
                'MANAGER_SELECTION': '${{ inputs.kernelsu_variant }}'},
        'run': 'python3 tools/manager_matrix_layout.py'})
    ready = "always() && !cancelled() && needs.prepare-matrix.result == 'success'"
    lkm = jobs['build-sukisu-lkm']
    lkm['if'] = ready
    lkm['strategy']['matrix'] = '${{ fromJSON(needs.prepare-matrix.outputs.ddk_build_matrix) }}'
    manager = jobs['build-sukisu-manager']
    manager['if'] = ready + " && needs.build-sukisu-lkm.result == 'success'"
    manager['strategy'] = {'fail-fast': False, 'matrix': copy.deepcopy(VARIANTS)}
    manager['outputs'] = outputs_by_manager(original_manager['outputs'])

    kernel = jobs[kernel_id]
    kernel_gate = ready + " && needs.build-sukisu-manager.result == 'success'"
    if not oplus:
        kernel_gate += " && inputs.operation == '编译内核'"
    kernel['if'] = kernel_gate
    kernel['strategy']['matrix'] = '${{ fromJSON(needs.prepare-matrix.outputs.kernel_primary) }}'
    if not oplus:
        kernel['strategy']['max-parallel'] = '${{ inputs.max_parallel }}'
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

    publish = jobs[publish_id]
    publish['name'] = '核验与发行 · ${{ matrix.variant }}'
    publish['needs'] = ['prepare-matrix', 'build-sukisu-manager', kernel_id, overflow_id]
    kernel_success = ("needs." + kernel_id + ".result == 'success' && (needs." + overflow_id
                      + ".result == 'success' || needs." + overflow_id + ".result == 'skipped')")
    if not oplus:
        kernel_success = "inputs.operation == '仅编译管理器安装包' || (" + kernel_success + ')'
    publish['if'] = ready + " && needs.build-sukisu-manager.result == 'success' && (" + kernel_success + ')'
    publish['strategy'] = {'fail-fast': False, 'matrix': copy.deepcopy(VARIANTS)}
    for step in publish['steps']:
        if step.get('uses') == 'actions/download-artifact@v8':
            apk = scoped('${{ needs.build-sukisu-manager.outputs.apk_filename }}')[3:-3].strip()
            pattern = 'Oplus-*-{0}-AnyKernel3.zip' if oplus else '*-{0}-AnyKernel3.zip,*-{0}-boot*.img'
            combined = "format('{{{1}," + pattern + "}}', matrix.variant, " + apk + ')'
            plain = '{{{0},Oplus-*-AnyKernel3.zip}}' if oplus else '{{{0},*-AnyKernel3.zip,*-boot*.img}}'
            fallback = "format('" + plain + "', " + apk + ')'
            step['with']['pattern'] = expression(COMBINED + ' && ' + combined + ' || ' + fallback)
    publish['steps'] = [s for s in publish['steps'] if s.get('uses') != 'actions/upload-artifact@v7']
    checksum_path = 'oplus-report/SHA256SUMS.txt' if oplus else 'release-publish/assets/SHA256SUMS.txt'
    publish['steps'].append({'name': '读取已核验文件的校验值', 'id': 'selected-checksums',
        'env': {'CHECKSUM_FILE': checksum_path}, 'run': '''python3 - <<'PY'
import json,os,pathlib
lines=pathlib.Path(os.environ['CHECKSUM_FILE']).read_text(encoding='utf-8').splitlines()
with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output:
    output.write('checksums='+json.dumps(lines,separators=(',',':'))+'\\n')
PY
'''})
    publish['outputs'] = outputs_by_manager({'checksums': '${{ steps.selected-checksums.outputs.checksums }}'})
    jobs['build-checksums'] = {
        'name': '汇总已成功构建文件的校验清单', 'needs': publish_id,
        'if': "always() && !cancelled() && needs." + publish_id + ".result == 'success'",
        'runs-on': 'ubuntu-24.04', 'permissions': {'contents': 'read'},
        'steps': [{'name': '合并所选管理器的校验清单', 'env': {
            'MANAGERS': expression(SELECTION), 'CHECKSUMS': expression('toJSON(needs.' + publish_id + '.outputs)')},
            'run': '''python3 - <<'PY'
import json,os,pathlib,re
rows={}
outputs=json.loads(os.environ['CHECKSUMS'])
for variant in json.loads(os.environ['MANAGERS']):
    lines=json.loads(outputs[variant+'_checksums'])
    if not isinstance(lines,list) or not lines: raise SystemExit('Missing verified checksums')
    for line in lines:
        match=re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9_.+-]+)',line)
        if not match or match[2] in rows: raise SystemExit('Invalid or duplicate filename')
        rows[match[2]]=match[1]
key=lambda n:[int(p) if p.isdigit() else p for p in re.split(r'(\\d+)',n)]
pathlib.Path('SHA256SUMS.txt').write_text(''.join(rows[n]+'  '+n+'\\n' for n in sorted(rows,key=key)),encoding='utf-8')
PY
'''}, {'name': '上传原文件校验清单', 'uses': 'actions/upload-artifact@v7', 'with': {
            'path': 'SHA256SUMS.txt', 'archive': False, 'if-no-files-found': 'error', 'overwrite': True}}]}
    return root, None
