"""Generate the separate official-source OPlus matrix and native asset flow."""
from __future__ import annotations

import copy
from pathlib import Path

from manager_lkm_jobs import (source_cache_restore, source_backup_restore, source_backup_recache,
                              source_cache_verify)
from workflow_jobs import release_publish_steps

OUTPUT = Path('.github/workflows/oplus-full-feature-matrix.yml')
DISPATCH = "github.event_name == 'workflow_dispatch'"
KPM_IF = "inputs.use_kpm && (inputs.kernelsu_variant == 'SukiSU' || inputs.kernelsu_variant == 'BakaSU')"
INPUTS = {
    'kernelsu_variant': {'description': 'KernelSU 变体', 'required': True, 'type': 'choice',
                        'options': ['SukiSU', 'BakaSU'], 'default': 'SukiSU'},
    'kernelsu_branch': {'description': '版本', 'required': True, 'type': 'choice',
                       'options': ['正式版', '最新版'], 'default': '最新版'},
    'enable_susfs': {'description': '启用 SUSFS（android14/6.1、android15/6.6、android16/6.12）',
                     'required': False, 'type': 'boolean', 'default': True},
    'use_kpm': {'description': '启用 KPM（仅 SukiSU/BakaSU 生效）',
                'required': False, 'type': 'boolean', 'default': True},
    'use_lz4kd': {'description': '启用 lz4kd', 'required': False, 'type': 'boolean', 'default': True},
    'use_bbg': {'description': '启用 BBG 基带防护', 'required': False, 'type': 'boolean', 'default': False},
    'use_bbr': {'description': '启用 BBR', 'required': False, 'type': 'boolean', 'default': False},
    'use_proxy_optimization': {'description': '启用代理相关网络优化（MTK 会自动关闭）',
                               'required': False, 'type': 'boolean', 'default': False},
    'use_unicode_bypass': {'description': '启用 Unicode 零宽绕过修复',
                           'required': False, 'type': 'boolean', 'default': False},
}


def adapt(value):
    """Reuse the tested manager pipeline without importing GKI menu fields."""
    if isinstance(value, str):
        return (value.replace("inputs.operation == '编译内核'", 'true')
                .replace("inputs.operation == '仅编译管理器安装包'", 'false'))
    if isinstance(value, list):
        return [adapt(item) for item in value]
    if isinstance(value, dict):
        return {key: adapt(item) for key, item in value.items()}
    return value


def python_dependencies():
    return {'name': '准备构建配置与 APK 签名验证依赖', 'run':
            "python3 -c 'import yaml, cryptography' || python3 -m pip install --break-system-packages --disable-pip-version-check PyYAML cryptography"}


PROFILE_INPUTS = '''set -euo pipefail
python3 - <<'PY'
import json,os,pathlib,sys
sys.path.insert(0,'tools')
from oplus_assets import profile_identity
from oplus_catalog import strict_json
from oplus_recipe import native_inputs
profile = profile_identity(strict_json(os.environ['OPLUS_PROFILE'].encode()))
inputs = strict_json(os.environ['OPLUS_INPUTS'].encode())
native_inputs(inputs, profile)
directory = pathlib.Path(os.environ['RUNNER_TEMP'])
for name, value in [('oplus-profile.json',profile), ('oplus-inputs.json',inputs)]:
    (directory/name).write_text(json.dumps(value,ensure_ascii=False,sort_keys=True)+'\\n',encoding='utf-8')
PY
'''

NATIVE_ASSETS = '''set -euo pipefail
python3 tools/oplus_assets.py collect --input oplus-input --output oplus-report \\
  --matrix "$OPLUS_MATRIX" --variants "$KSU_VARIANT"
python3 - <<'PY'
import pathlib,shutil,sys
sys.path.insert(0,'tools')
from transfer_ksu_snapshot import native_assets
source = pathlib.Path('oplus-input')
output = pathlib.Path('oplus-publish/assets')
output.mkdir(parents=True)
for path in source.rglob('*'):
    if path.is_symlink():
        raise SystemExit('Refusing linked original assets')
    if path.is_file():
        shutil.copyfile(path, output/path.name)
shutil.copyfile('oplus-report/SHA256SUMS.txt', output/'SHA256SUMS.txt')
native_assets(output.resolve())
PY
'''


def recipe_step(phase: str, name: str):
    step = {'name': name, 'run':
            'python3 tools/oplus_recipe.py --phase ' + phase + ' --recipe-root "$GITHUB_WORKSPACE/oplus-reference" \\\n'
            '  --profile "$RUNNER_TEMP/oplus-profile.json" --inputs "$RUNNER_TEMP/oplus-inputs.json" \\\n'
            '  --workspace "$GITHUB_WORKSPACE"\n'}
    if phase == 'build':
        step['id'] = 'oplus-build'
    return step


def prepare_job(base: dict) -> dict:
    prepare = adapt(copy.deepcopy(base['jobs']['prepare-matrix']))
    prepare['name'] = '固定官方平台内核、所选版本管理器与构建依赖'
    prepare['if'] = DISPATCH
    for key in ('full_catalog', 'max_parallel'):
        prepare['outputs'].pop(key, None)
    prepare['outputs'].update({key: '${{ steps.oplus-deps.outputs.' + key + ' }}'
                               for key in ('ak3_ref', 'patches_ref')})
    steps = []
    for step in prepare['steps']:
        if step.get('name') in ('校验旧版内核 SUSFS 映射兼容性', '验证共享源码还原及原生符号接口'):
            continue
        if step.get('id') == 'matrix':
            step = {'name': '从官方分支冻结已审核平台与完整内核版本', 'id': 'matrix',
                    'env': {'GH_TOKEN': '${{ github.token }}'},
                    'run': 'python3 tools/oplus_catalog.py --github-output --summary'}
        if step.get('id') == 'sources':
            step['env']['KSU_TIER'] = '${{ inputs.kernelsu_branch }}'
            step['env']['CUSTOM_REF'] = ''
        steps.append(step)
        if step.get('uses') == 'actions/checkout@v6' and 'repository' not in step.get('with', {}):
            steps += [python_dependencies(), {
                'name': '验证欧加真源码、构建方法与原生配套校验',
                'run': "set -euo pipefail\n"
                       "command -v cc >/dev/null\n"
                       "python3 -m unittest discover -s tests -p 'test_oplus_*.py'\n"
                       "python3 -m unittest discover -s tests -p 'test_susfs_map_compat.py'"}]
        if step.get('id') == 'matrix':
            steps.append({'name': '固定官方 AnyKernel3 与可选 Unicode 补丁', 'id': 'oplus-deps',
                          'env': {'GH_TOKEN': '${{ github.token }}', 'UNICODE_BYPASS': '${{ inputs.use_unicode_bypass }}'},
                          'run': 'python3 tools/oplus_kernel.py --phase prepare-deps --unicode-bypass "$UNICODE_BYPASS"'})
    prepare['steps'] = steps
    return prepare


def kernel_job() -> dict:
    prefix = ("oplus-ccache-ecs-v1-${{ runner.os }}-${{ runner.arch }}-"
              "${{ hashFiles('config/kernel-cache-tools.json', 'tools/kernel_compile_cache.py') }}-"
              '${{ matrix.id }}-${{ inputs.kernelsu_variant }}-${{ matrix.recipe_ref }}-')
    steps = [
        {'name': '检出独立构建工具', 'uses': 'actions/checkout@v6', 'with': {'persist-credentials': False}},
        python_dependencies(),
        {'name': '验证并保存当前官方源码与所选菜单输入',
         'env': {'OPLUS_PROFILE': '${{ toJSON(matrix) }}', 'OPLUS_INPUTS': '${{ toJSON(inputs) }}'},
         'run': PROFILE_INPUTS},
        {'name': '核实本次已签名 APK 与成功的管理器任务',
         'env': {'GH_TOKEN': '${{ github.token }}',
                 'MANAGER_ARTIFACT': '${{ needs.build-sukisu-manager.outputs.apk_filename }}',
                 'MANAGER_JOB_ID': '${{ needs.build-sukisu-manager.outputs.manager_job_id }}'},
         'run': '''python3 tools/wait_manager_identity.py --expected-ref "$KSU_REF" \\
  --artifact-name "$MANAGER_ARTIFACT" --manager-job-id "$MANAGER_JOB_ID" --github-env "$GITHUB_ENV"
'''},
        source_cache_restore(), adapt(source_backup_restore()), source_backup_recache(), source_cache_verify(),
        {'name': '检出固定的 CCTV 构建方法参考', 'uses': 'actions/checkout@v6',
         'with': {'repository': '${{ matrix.recipe_repository }}', 'ref': '${{ matrix.recipe_ref }}',
                  'path': 'oplus-reference', 'fetch-depth': 1, 'persist-credentials': False}},
        {'name': '检出固定的官方 AnyKernel3 运行文件', 'uses': 'actions/checkout@v6',
         'with': {'repository': 'osm0sis/AnyKernel3', 'ref': '${{ needs.prepare-matrix.outputs.ak3_ref }}',
                  'path': 'oplus-ak3', 'fetch-depth': 1, 'persist-credentials': False}},
        {'name': '检出固定的 Unicode 修复补丁', 'if': 'inputs.use_unicode_bypass', 'uses': 'actions/checkout@v6',
         'with': {'repository': 'Xiaomichael/kernel_patches', 'ref': '${{ needs.prepare-matrix.outputs.patches_ref }}',
                  'path': 'oplus-patches', 'fetch-depth': 1, 'persist-credentials': False}},
        {'name': '安装原生编译工具与系统开发依赖', 'timeout-minutes': 15, 'run': '''set -euo pipefail
apt_options=(-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=2 -o DPkg::Lock::Timeout=60)
sudo apt-get "${apt_options[@]}" update -qq
sudo apt-get "${apt_options[@]}" install -y --no-install-recommends aria2 bc bison flex make gcc g++ binutils \\
  python-is-python3 libssl-dev libelf-dev libdw-dev zlib1g-dev ccache lz4 zstd xz-utils \\
  zip unzip patch curl wget rsync
mkdir -p "$CCACHE_DIR"
ccache --max-size "$CCACHE_MAXSIZE"
ccache --set-config=compression=true
ccache --set-config=compiler_check=content
'''},
        {'name': '安装固定 ECS 编译缓存工具',
         'run': '''set -euo pipefail
python3 tools/kernel_compile_cache.py install \\
  --output "$RUNNER_TEMP/gki-ccache-ecs" \\
  --github-env "$GITHUB_ENV" --github-path "$GITHUB_PATH"
'''},
        {'name': '恢复本平台与内核的编译对象缓存', 'id': 'oplus-ccache',
         'uses': 'actions/cache/restore@v5', 'timeout-minutes': 2, 'continue-on-error': True,
         'env': {'SEGMENT_DOWNLOAD_TIMEOUT_MINS': '1'},
         'with': {'path': '${{ env.CCACHE_DIR }}',
                  'key': prefix + '${{ github.run_id }}-${{ github.run_attempt }}', 'restore-keys': prefix}},
        recipe_step('bootstrap', '获取官方固定源码及参考配方工具链'),
        {'name': '接入同次管理器源码及官方 SUSFS', 'run': '''python3 tools/oplus_kernel.py --phase integrate \\
  --workspace "$GITHUB_WORKSPACE" --profile "$RUNNER_TEMP/oplus-profile.json" \\
  --inputs "$RUNNER_TEMP/oplus-inputs.json" --snapshot "$GITHUB_WORKSPACE/ksu-source-bundle"
'''},
        recipe_step('configure', '按原生配方应用已选择的功能配置'),
        {'name': '核实管理器证书、原生 Hook 与最终配置', 'run': '''python3 tools/oplus_kernel.py --phase finalize \\
  --workspace "$GITHUB_WORKSPACE" --profile "$RUNNER_TEMP/oplus-profile.json" \\
  --inputs "$RUNNER_TEMP/oplus-inputs.json" --patches "$GITHUB_WORKSPACE/oplus-patches"
'''},
        {'name': '核实官方 KPM 工具和原始载荷', 'if': KPM_IF,
         'run': '''python3 tools/apply_kpm_patch.py prepare --cache-dir "$RUNNER_TEMP/oplus-kpm-official" \\
  --github-env "$GITHUB_ENV" --ksu-source "$GITHUB_WORKSPACE/kernel_workspace/KernelSU"
'''},
        {'name': '开始记录本次 ECS 缓存命中', 'id': 'oplus-cache-measurement',
         'run': '''set -euo pipefail
ccache --zero-stats
ccache --version
echo 'started=true' >> "$GITHUB_OUTPUT"
'''},
        recipe_step('build', '使用原生 Make 与对应 Clang/Rust 编译内核'),
        {'name': '应用并验证独立 KPM 启动载荷', 'if': KPM_IF,
         'run': '''python3 tools/apply_kpm_patch.py patch \\
  --image "$GITHUB_WORKSPACE/kernel_workspace/common/out/arch/arm64/boot/Image" \\
  --cache-dir "$KPM_CACHE_DIR" --report "$GITHUB_WORKSPACE/kernel_workspace/common/out/gki-kpm-patch.json"
'''},
        {'name': '校验实际配置与证书并生成原生 AK3', 'id': 'oplus-assets',
         'run': '''python3 tools/oplus_assets.py package --profile "$RUNNER_TEMP/oplus-profile.json" \\
  --kernel-root "$GITHUB_WORKSPACE/kernel_workspace" \\
  --image "$GITHUB_WORKSPACE/kernel_workspace/common/out/arch/arm64/boot/Image" \\
  --ak3-template "$GITHUB_WORKSPACE/oplus-ak3" --output "$GITHUB_WORKSPACE/oplus-ready"
'''},
        {'name': '上传原文件同名 AK3', 'uses': 'actions/upload-artifact@v7',
         'with': {'path': 'oplus-ready/${{ steps.oplus-assets.outputs.file }}', 'archive': False,
                  'if-no-files-found': 'error', 'overwrite': True}},
        {'name': '限制缓存容量并显示命中情况',
         'if': "always() && !cancelled() && steps.oplus-cache-measurement.outputs.started == 'true'",
         'run': '''set -euo pipefail
ccache --max-size "$CCACHE_MAXSIZE"
ccache --cleanup
report="$RUNNER_TEMP/oplus-ccache-statistics.txt"
ccache --show-stats --verbose | tee "$report"
{
  echo '### 本次欧加真编译缓存'
  echo ''
  echo '以下数据只统计本次编译；更换工具链或配置后，受影响的缓存会重新生成。'
  echo ''
  echo '```text'
  cat "$report"
  echo '```'
} >> "$GITHUB_STEP_SUMMARY"
'''},
        {'name': '保存已成功编译的平台缓存',
         'if': "always() && !cancelled() && steps.oplus-build.outcome == 'success' && steps.oplus-ccache.outputs.cache-primary-key != ''",
         'uses': 'actions/cache/save@v5', 'timeout-minutes': 2, 'continue-on-error': True,
         'with': {'path': '${{ env.CCACHE_DIR }}', 'key': '${{ steps.oplus-ccache.outputs.cache-primary-key }}'}},
    ]
    return {
        'name': "${{ startsWith(matrix.platform, 'mt') && '天玑' || '骁龙' }} · ${{ matrix.kernel }} · ${{ inputs.kernelsu_variant }}",
        'if': "always() && !cancelled() && " + DISPATCH + " && needs.prepare-matrix.result == 'success' && needs.build-sukisu-manager.result == 'success'",
        'needs': ['prepare-matrix', 'build-sukisu-manager'], 'runs-on': 'ubuntu-24.04', 'timeout-minutes': 120,
        'permissions': {'contents': 'read', 'actions': 'read'},
        'defaults': {'run': {'shell': 'bash'}},
        'strategy': {'fail-fast': False, 'max-parallel': 20,
                     'matrix': '${{ fromJSON(needs.prepare-matrix.outputs.matrix) }}'},
        'env': {'KSU_VARIANT': '${{ inputs.kernelsu_variant }}',
                'KSU_REF': '${{ needs.prepare-matrix.outputs.ksu_ref }}',
                'KSU_BRANCH': '${{ needs.prepare-matrix.outputs.effective_ksu_branch }}',
                'ENABLE_SUSFS': '${{ inputs.enable_susfs }}',
                'SUSFS_SOURCES': '${{ needs.prepare-matrix.outputs.susfs_sources }}',
                'AK3_REF': '${{ needs.prepare-matrix.outputs.ak3_ref }}',
                'PATCHES_REF': '${{ needs.prepare-matrix.outputs.patches_ref }}',
                'CCACHE_DIR': '${{ github.workspace }}/.oplus-ccache', 'CCACHE_MAXSIZE': '3G'},
        'steps': steps,
    }


def collect_job():
    return {
        'name': '整理原生文件与详细配置（全量成功才发行）',
        'if': "always() && !cancelled() && needs.prepare-matrix.result == 'success' && needs.build-sukisu-manager.result == 'success' && needs.build-oplus-kernel.result == 'success'",
        'needs': ['prepare-matrix', 'build-sukisu-manager', 'build-oplus-kernel'],
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 15,
        'permissions': {'contents': 'write', 'actions': 'write'},
        'env': {'KSU_VARIANT': '${{ inputs.kernelsu_variant }}'},
        'steps': [
            {'name': '检出本次原生校验工具', 'uses': 'actions/checkout@v6', 'with': {'persist-credentials': False}},
            python_dependencies(),
            {'name': '下载本次原文件 APK 与全部平台 AK3', 'uses': 'actions/download-artifact@v8',
             'with': {'pattern': '{${{ needs.build-sukisu-manager.outputs.apk_filename }},Oplus-*-AnyKernel3.zip}',
                      'path': 'oplus-input', 'merge-multiple': False, 'skip-decompress': True}},
            {'name': '逐件核对官方源、真实签名、完整矩阵与实际配置', 'id': 'assets',
             'env': {'OPLUS_MATRIX': '${{ needs.prepare-matrix.outputs.matrix }}'}, 'run': NATIVE_ASSETS},
            {'name': '上传原文件同名校验清单', 'uses': 'actions/upload-artifact@v7',
             'with': {'path': 'oplus-report/SHA256SUMS.txt', 'archive': False,
                      'if-no-files-found': 'error', 'overwrite': True}},
            *release_publish_steps('success()', assets_directory='oplus-publish/assets',
                                   notes_file='oplus-report/summary.md', title_file='oplus-report/title.txt'),
            {'name': '显示统一的详细构建配置', 'if': "always() && !cancelled() && steps.assets.outcome == 'success'",
             'run': 'cat oplus-report/summary.md >> "$GITHUB_STEP_SUMMARY"'},
        ],
    }


def create_workflow(root: Path, base: dict | None = None) -> dict:
    if base is None:
        from generate_workflow import create_workflow as create_gki
        base = create_gki(root)
    lkm = adapt(copy.deepcopy(base['jobs']['build-sukisu-lkm']))
    manager = adapt(copy.deepcopy(base['jobs']['build-sukisu-manager']))
    lkm['if'] = manager['if'] = DISPATCH
    fields = copy.deepcopy(INPUTS)
    fields['kernelsu_branch'] = copy.deepcopy(base['on']['workflow_dispatch']['inputs']['kernelsu_branch'])
    return {
        'name': '欧加真全属性内核构建矩阵',
        'run-name': '欧加真 · ${{ inputs.kernelsu_variant }} · ${{ inputs.kernelsu_branch }} · 官方完整内核矩阵',
        'on': {'workflow_dispatch': {'inputs': fields}},
        'permissions': {'contents': 'read'},
        'jobs': {'prepare-matrix': prepare_job(base), 'build-sukisu-lkm': lkm,
                 'build-sukisu-manager': manager, 'build-oplus-kernel': kernel_job(),
                 'verify-oplus-assets': collect_job()},
    }
