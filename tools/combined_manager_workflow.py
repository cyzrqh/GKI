"""Keep three isolated matrices inside each existing public build workflow."""
from __future__ import annotations

import copy
import json
import re

from manager_variants import COMBINED_SELECTION, COMBINED_MANAGERS

CALLERS = ('combo-sukisu', 'combo-bakasu', 'combo-next')


class SharedSteps(list):
    """One YAML sequence, referenced by aliases in the independent build jobs."""
    _gki_shared_steps = True


def bind_steps(job: dict) -> None:
    """Bind job-specific needs once; keep the executable step bodies shared."""
    names = {'prepare-matrix': 'PREPARE', 'build-sukisu-manager': 'MANAGER',
             'build-sukisu-lkm': 'LKM', 'build-kernel': 'KERNEL', 'build-oplus-kernel': 'KERNEL'}
    aliases = {}
    def value(source):
        if isinstance(source, str):
            def replace(match):
                name, field, result = match.groups()
                if name not in names:
                    raise ValueError('Unexpected build dependency in shared steps: ' + name)
                alias = 'GKI_' + names[name] + '_' + (field or result).upper()
                aliases[alias] = '${{ ' + match[0] + ' }}'
                return 'env.' + alias
            source = source.replace('inputs.kernelsu_variant', 'env.KSU_VARIANT')
            return re.sub(r'needs\.([\w-]+)\.(?:outputs\.([A-Za-z_]\w*)|(result))', replace, source)
        if isinstance(source, list):
            return [value(child) for child in source]
        if isinstance(source, dict):
            return {key: value(child) for key, child in source.items()}
        return source
    job['steps'] = value(job['steps'])
    job.setdefault('env', {}).update(aliases)
    job['env'].setdefault('KSU_VARIANT', '${{ inputs.kernelsu_variant }}')
    job['env']['GKI_COMBINED_WORKER'] = 'false'


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


def add_combined(workflow: dict, *, oplus: bool) -> tuple[dict, None]:
    root = copy.deepcopy(workflow)
    published_job = 'verify-oplus-assets' if oplus else 'publish-release'
    kernel_job = 'build-oplus-kernel' if oplus else 'build-kernel'
    core = ('prepare-matrix', 'build-sukisu-lkm', 'build-sukisu-manager', kernel_job)
    for name in core:
        bind_steps(root['jobs'][name])
    blueprints = {name: copy.deepcopy(root['jobs'][name]) for name in core}
    combo = "inputs.kernelsu_variant == '" + COMBINED_SELECTION + "'"
    root['jobs']['prepare-matrix']['if'] = '(' + root['jobs']['prepare-matrix']['if'] + ') && !(' + combo + ')'
    for caller, variant in zip(CALLERS, COMBINED_MANAGERS):
        ids = {name: caller + '-' + suffix for name, suffix in zip(core, ('prepare', 'lkm', 'manager', 'kernel'))}
        replacements = [('needs.' + name + '.', 'needs.' + ids[name] + '.') for name in core]
        replacements.append(('inputs.kernelsu_variant', "'" + variant + "'"))
        for name in core:
            job = copy.deepcopy(blueprints[name])
            steps = job.pop('steps')
            job = rewrite(job, replacements)
            if 'needs' in job:
                dependencies = [job['needs']] if isinstance(job['needs'], str) else job['needs']
                job['needs'] = [ids[dependency] for dependency in dependencies]
            job['steps'] = steps
            job['env']['GKI_COMBINED_WORKER'] = 'true'
            job['if'] = '(' + job['if'] + ') && (' + combo + ')'
            root['jobs'][ids[name]] = job
        outputs = {key: '${{ needs.' + ids['prepare-matrix'] + '.outputs.' + key + ' }}'
                   for key in blueprints['prepare-matrix']['outputs']}
        outputs.update({key: '${{ needs.' + ids['build-sukisu-manager'] + '.outputs.' + key + ' }}'
                        for key in blueprints['build-sukisu-manager']['outputs']})
        kernel_done = "needs." + ids[kernel_job] + ".result == 'success'"
        if not oplus:
            kernel_done = "(inputs.operation == '仅编译管理器安装包' || " + kernel_done + ')'
        root['jobs'][caller] = {
            'name': '核实 ' + variant + ' 分支完成', 'needs': list(ids.values()),
            'if': "always() && !cancelled() && (" + combo + ") && needs." + ids['prepare-matrix']
                  + ".result == 'success' && needs." + ids['build-sukisu-manager'] + ".result == 'success' && " + kernel_done,
            'outputs': outputs, 'runs-on': 'ubuntu-24.04', 'permissions': {'contents': 'read'},
            'steps': [{'name': '确认本管理器全部目标完成', 'run': 'true'}]}
    completed = "always() && !cancelled() && " + ' && '.join(
        "needs." + caller + ".result == 'success'" for caller in CALLERS)
    for caller, variant in zip(CALLERS, COMBINED_MANAGERS):
        publisher = rewrite(copy.deepcopy(root['jobs'][published_job]), (
            ('needs.prepare-matrix.outputs.', 'needs.' + caller + '.outputs.'),
            ('needs.build-sukisu-manager.outputs.', 'needs.' + caller + '.outputs.'),
            ('inputs.kernelsu_variant', "'" + variant + "'")))
        publisher['name'] = '核验与发行 ' + variant
        publisher['needs'] = list(CALLERS)
        publisher['if'] = completed
        publisher['outputs'] = {'checksums': '${{ steps.combined-checksums.outputs.checksums }}'}
        for step in publisher['steps']:
            if step.get('uses') == 'actions/download-artifact@v8':
                apk = '${{ needs.' + caller + '.outputs.apk_filename }}'
                glob = 'Oplus-*-' + variant + '-AnyKernel3.zip' if oplus else '*-' + variant + '-AnyKernel3.zip,*-' + variant + '-boot*.img'
                step['with']['pattern'] = '{' + apk + ',' + glob + '}'
        publisher['steps'] = [step for step in publisher['steps']
                              if step.get('uses') != 'actions/upload-artifact@v7']
        checksum_path = 'oplus-report/SHA256SUMS.txt' if oplus else 'release-publish/assets/SHA256SUMS.txt'
        publisher['steps'].append({
            'name': '汇总该管理器已验证文件的校验值', 'id': 'combined-checksums',
            'env': {'CHECKSUM_FILE': checksum_path}, 'run': r'''python3 - <<'PY'
import json,os,pathlib
lines=pathlib.Path(os.environ['CHECKSUM_FILE']).read_text(encoding='utf-8').splitlines()
with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output:
    output.write('checksums='+json.dumps(lines,separators=(',',':'))+'\n')
PY
'''})
        root['jobs']['publish-' + caller] = publisher
    publishers = ['publish-' + name for name in CALLERS]
    root['jobs']['combined-checksums'] = {
        'name': '生成三管理器统一校验清单', 'needs': publishers,
        'if': "always() && !cancelled() && " + ' && '.join("needs." + job + ".result == 'success'" for job in publishers),
        'runs-on': 'ubuntu-24.04', 'permissions': {'contents': 'read'},
        'steps': [
            {'name': '生成同名校验文件', 'env': {
                'CHECKSUMS_' + str(i): '${{ needs.' + name + '.outputs.checksums }}'
                for i, name in enumerate(publishers)},
             'run': r'''python3 - <<'PY'
import json,os,pathlib,re
rows={}
for i in range(3):
    lines=json.loads(os.environ['CHECKSUMS_'+str(i)])
    if not isinstance(lines,list) or not lines: raise SystemExit('Missing verified checksums')
    for line in lines:
        match=re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9_.+-]+)',line)
        if not match or match[2] in rows: raise SystemExit('Invalid or duplicate combined filename')
        rows[match[2]]=match[1]
key=lambda n:[int(p) if p.isdigit() else p for p in re.split(r'(\d+)',n)]
pathlib.Path('SHA256SUMS.txt').write_text(''.join(rows[n]+'  '+n+'\n' for n in sorted(rows,key=key)),encoding='utf-8')
PY
'''},
            {'name': '上传原文件校验清单', 'uses': 'actions/upload-artifact@v7',
             'with': {'path': 'SHA256SUMS.txt', 'archive': False, 'if-no-files-found': 'error', 'overwrite': True}}]}
    pool = {}
    for name in (*core, *(caller + '-' + suffix for caller in CALLERS for suffix in ('prepare', 'lkm', 'manager', 'kernel'))):
        job = root['jobs'][name]
        identity = json.dumps(job['steps'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        job['steps'] = pool.setdefault(identity, SharedSteps(job['steps']))
    return root, None
