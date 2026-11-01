"""Use one reusable worker per manager, avoiding the 256-entry matrix limit."""
from __future__ import annotations

import copy
from pathlib import Path

from manager_variants import COMBINED_SELECTION, COMBINED_MANAGERS

CALLERS = ('combo-sukisu', 'combo-bakasu', 'combo-next')


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


def add_combined(workflow: dict, *, oplus: bool) -> tuple[dict, tuple[Path, dict]]:
    root = copy.deepcopy(workflow)
    worker = copy.deepcopy(workflow)
    worker_path = Path('.github/workflows/_oplus-manager-worker.yml' if oplus
                       else '.github/workflows/_gki-manager-worker.yml')
    fields = root['on']['workflow_dispatch']['inputs']
    worker_fields = {}
    for name, definition in fields.items():
        worker_fields[name] = {key: value for key, value in definition.items()
                               if key in ('description', 'required', 'default')}
        worker_fields[name]['type'] = 'string' if definition['type'] == 'choice' else definition['type']
        if worker_fields[name].get('required'):
            worker_fields[name].pop('default', None)
    published_job = 'verify-oplus-assets' if oplus else 'publish-release'
    worker['jobs'].pop(published_job)
    worker.pop('run-name', None)
    worker['name'] = '欧加真单管理器构建执行器' if oplus else 'GKI 单管理器构建执行器'
    worker['env'] = {'GKI_COMBINED_WORKER': 'true'}
    outputs = {}
    prepare_outputs = worker['jobs']['prepare-matrix']['outputs']
    manager_outputs = worker['jobs']['build-sukisu-manager']['outputs']
    for name in prepare_outputs:
        outputs[name] = {'value': '${{ jobs.prepare-matrix.outputs.' + name + ' }}'}
    for name in manager_outputs:
        # The prepare job's certificate describes local builds; the actual
        # producer output is authoritative for both local and official APKs.
        outputs[name] = {'value': '${{ jobs.build-sukisu-manager.outputs.' + name + ' }}'}
    worker['on'] = {'workflow_call': {'inputs': worker_fields, 'outputs': outputs}}
    combo = "inputs.kernelsu_variant == '" + COMBINED_SELECTION + "'"
    root['jobs']['prepare-matrix']['if'] = '(' + root['jobs']['prepare-matrix']['if'] + ') && !(' + combo + ')'
    for caller, variant in zip(CALLERS, COMBINED_MANAGERS):
        root['jobs'][caller] = {
            'name': variant, 'if': combo, 'uses': './' + worker_path.as_posix(),
            'permissions': {'contents': 'write', 'actions': 'write', 'issues': 'write', 'pull-requests': 'write'},
            'with': {key: variant if key == 'kernelsu_variant' else '${{ inputs.' + key + ' }}'
                     for key in fields}, 'secrets': 'inherit'}
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
    return root, (worker_path, worker)
