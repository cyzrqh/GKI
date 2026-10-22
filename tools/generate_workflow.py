#!/usr/bin/env python3
"""Regenerate the only Actions workflow from a pinned ABK source and local overlays."""
from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('.github/workflows/kernel-full-feature-matrix.yml')


class Loader(yaml.SafeLoader):
    pass


# GitHub uses YAML 1.2 booleans; PyYAML's YAML 1.1 would turn `on`/`off` into bool.
Loader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in values if tag != 'tag:yaml.org,2002:bool']
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
Loader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$', re.I), list('tTfF'))


class Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def represent_string(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|' if '\n' in value else None)


Dumper.add_representer(str, represent_string)


def load_yaml(path):
    return yaml.load(Path(path).read_text(encoding='utf-8'), Loader=Loader)


def expr_literal(value):
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if value is None:
        return "''"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def transform(value, mapping):
    if isinstance(value, str):
        def replace(match):
            key = match.group(1)
            if key not in mapping:
                raise ValueError(f'Upstream introduced an unmapped input: {key}')
            return mapping[key]
        return re.sub(r'\binputs\.([A-Za-z_]\w*)\b', replace, value)
    if isinstance(value, list):
        return [transform(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: transform(item, mapping) for key, item in value.items()}
    return value


def step_named(steps, name):
    matches = [step for step in steps if step.get('name') == name]
    if len(matches) != 1:
        raise ValueError(f'Upstream step changed: expected exactly one {name!r}, found {len(matches)}')
    return matches[0]


def feature_inputs(source):
    fields = copy.deepcopy(source['on']['workflow_dispatch']['inputs'])
    for key in ('revision', 'trigger_release', 'upload_aux_artifacts'):
        if key not in fields:
            raise ValueError(f'Upstream matrix input changed: {key}')
        fields.pop(key)
    fields['build_time']['default'] = ''
    fields['build_time']['description'] = '构建时间（留空使用当前 UTC）'
    selection = {
        'operation': {'description': '操作', 'required': True, 'type': 'choice',
                      'options': ['编译内核', '检查上游更新'], 'default': '编译内核'},
        'kernel_versions': {'description': '内核系列（all 或逗号分隔：5.10,5.15,6.1,6.6,6.12）',
                            'required': True, 'type': 'string', 'default': 'all'},
        'targets': {'description': '指定版本（留空全矩阵；例 6.1.145 或 6.1.145@2025-08，逗号分隔）',
                    'required': False, 'type': 'string', 'default': ''},
        'include_lts': {'description': '包含 LTS（默认关闭）', 'required': False, 'type': 'boolean', 'default': False},
        'artifact_mode': {'description': '产物模式', 'required': True, 'type': 'choice',
                          'options': ['仅 AK3', 'AK3 + boot 镜像'], 'default': '仅 AK3'},
        'max_parallel': {'description': '同时构建数量（1–20）', 'required': True, 'type': 'number', 'default': 5},
    }
    result = {**selection, **fields}
    if len(result) > 25:
        raise ValueError(f'Too many workflow_dispatch inputs: {len(result)} (maximum 25)')
    return result


SOURCE_SYNC = '''set -euo pipefail
python3 "$GITHUB_WORKSPACE/.github/scripts/cached-downloads.py" clone \\
  https://github.com/LineageOS/android_prebuilts_gcc_linux-x86_aarch64_aarch64-linux-gnu-6.4.1.git gcc
formatted_branch="${GKI_ANDROID}-${GKI_KERNEL}-${GKI_PATCH}"
"$REPO" init --depth=1 -u https://android.googlesource.com/kernel/manifest \\
  -b "common-${formatted_branch}" --repo-rev=v2.16
python3 "$GITHUB_WORKSPACE/tools/set_kernel_ref.py" \\
  --manifest-root "$KERNEL_ROOT/.repo/manifests" \\
  --kernel-ref "$GKI_KERNEL_REF" --branch "$formatted_branch"
for attempt in 1 2 3; do
  if "$REPO" sync -c -j"$(nproc --all)" --no-tags --fail-fast; then
    exit 0
  fi
  echo "::warning::源码同步失败（${attempt}/3）"
  sleep "$((attempt * 5))"
done
exit 1
'''

PREPARE_IMAGE = '''set -euo pipefail
if [ "$GKI_ANDROID" = android12 ] || [ "$GKI_ANDROID" = android13 ]; then
  src="$KERNEL_ROOT/out/${GKI_ANDROID}-${GKI_KERNEL}/dist"
else
  src="$KERNEL_ROOT/bazel-bin/common/kernel_aarch64"
fi
cp "$src/Image" ./Image
if [ "$ARTIFACT_MODE" != '仅 AK3' ]; then
  mkdir -p bootimgs
  cp "$src/Image" bootimgs/Image
  cp "$src/Image.lz4" bootimgs/Image.lz4
fi
'''

PREPARE_AK3 = '''set -euo pipefail
cp ./Image "$ANYKERNEL3/Image"
if [ "$ARTIFACT_MODE" != '仅 AK3' ]; then
  zip_name="${GKI_ANDROID}-${GKI_KERNEL}.${GKI_SUBLEVEL}-${GKI_PATCH}-AnyKernel3.zip"
  (cd "$ANYKERNEL3" && zip -r "$GITHUB_WORKSPACE/$zip_name" ./*)
fi
'''


BUILD_IF = "github.event_name == 'workflow_dispatch' && inputs.operation == '编译内核'"


def updater_job():
    return {
        'name': '检查上游并准备更新 PR',
        'if': "(github.event_name == 'schedule' || inputs.operation == '检查上游更新') && github.ref_name == github.event.repository.default_branch",
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 20,
        'permissions': {'contents': 'read', 'pull-requests': 'write'},
        'concurrency': {'group': '${{ github.repository }}-upstream-sync', 'cancel-in-progress': False},
        'steps': [
            {'name': '检出默认分支', 'uses': 'actions/checkout@v6',
             'with': {'ref': '${{ github.event.repository.default_branch }}', 'fetch-depth': 0,
                      'persist-credentials': False}},
            {'name': '准备 Python', 'uses': 'actions/setup-python@v6', 'with': {'python-version': '3.12'}},
            {'name': '安装同步工具依赖', 'run': 'python -m pip install -r requirements.txt'},
            {'name': '读取上游版本', 'id': 'probe',
             'env': {'GH_TOKEN': '${{ github.token }}'},
             'run': 'python tools/check_upstream_updates.py --check-only'},
            {'name': '检查更新分支归属和已有 PR', 'id': 'guard',
             'if': "steps.probe.outputs.has_updates == 'true'",
             'env': {'GH_TOKEN': '${{ github.token }}', 'HEADS_MARKER': '${{ steps.probe.outputs.heads_marker }}',
                     'DEFAULT_BRANCH': '${{ github.event.repository.default_branch }}'},
             'run': 'python tools/upstream_pr_guard.py --heads-marker "$HEADS_MARKER" --base "$DEFAULT_BRANCH" --github-output "$GITHUB_OUTPUT"'},
            {'name': '导入并验证相关更新', 'id': 'update',
             'if': "steps.probe.outputs.has_updates == 'true' && steps.guard.outputs.already_pending != 'true'",
             'env': {'GH_TOKEN': '${{ github.token }}', 'CANDIDATES': '${{ steps.probe.outputs.latest_json }}'},
             'run': '''set -euo pipefail
abk_commit=$(python -c 'import json,os; print(json.loads(os.environ["CANDIDATES"])["abk"])')
catalog_commit=$(python -c 'import json,os; print(json.loads(os.environ["CANDIDATES"])["catalog"])')
python tools/check_upstream_updates.py --prepare --abk-commit "$abk_commit" \\
  --catalog-commit "$catalog_commit" --report-dir "$RUNNER_TEMP"
'''},
            {'name': '检查更新令牌是否已配置',
             'if': "steps.update.outputs.has_updates == 'true'",
             'env': {'SYNC_TOKEN': '${{ secrets.UPSTREAM_SYNC_TOKEN }}'},
             'run': '''if [ -z "$SYNC_TOKEN" ]; then
  echo '::error::发现可用更新，但尚未配置 UPSTREAM_SYNC_TOKEN。请按 README 的自动更新设置添加仓库 Secret。'
  exit 1
fi
'''},
            {'name': '创建或更新上游 PR', 'id': 'pr',
             'if': "steps.update.outputs.has_updates == 'true'",
             'uses': 'peter-evans/create-pull-request@v8',
             'with': {'token': '${{ secrets.UPSTREAM_SYNC_TOKEN }}',
                      'branch': 'automation/upstream-sync',
                      'base': '${{ github.event.repository.default_branch }}',
                      'title': '${{ steps.update.outputs.title }}',
                      'body-path': '${{ steps.update.outputs.body_path }}',
                      'commit-message': 'Update upstream build logic and kernel catalog',
                      'assignees': '${{ github.repository_owner }}',
                      'add-paths': '.upstream/\n.github/scripts/\n.github/workflows/kernel-full-feature-matrix.yml\nconfig/\nddk/\nmodules/\nzram/\nhmbird_patch.c\nLICENSE\nTHIRD_PARTY_NOTICES.md\ndata/\nupstream.lock.json\n'}},
            {'name': '提醒仓库所有者审核更新',
             'if': "steps.pr.outputs.pull-request-operation == 'created' || steps.pr.outputs.pull-request-operation == 'updated'",
             'env': {'GH_TOKEN': '${{ github.token }}', 'PR_NUMBER': '${{ steps.pr.outputs.pull-request-number }}',
                     'REPOSITORY_OWNER': '${{ github.repository_owner }}'},
             'run': '''gh api --method POST "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/comments" \\
  --raw-field body="@$REPOSITORY_OWNER 有一批相关上游更新已导入并通过检查，请查看本 PR 的差异后决定是否合并。此次检查没有编译内核。"
'''},
            {'name': '记录检查结果',
             'env': {'PR_URL': '${{ steps.pr.outputs.pull-request-url }}',
                     'ALREADY_PENDING': '${{ steps.guard.outputs.already_pending }}',
                     'EXISTING_PR': '${{ steps.guard.outputs.pull_request_url }}'},
             'run': '''if [ -n "$PR_URL" ]; then
  printf '上游更新已准备：[查看更新 PR](%s)。审核后可手动合并。\\n' "$PR_URL" >> "$GITHUB_STEP_SUMMARY"
elif [ "$ALREADY_PENDING" = true ]; then
  printf '相同上游版本已有待审核 PR：%s；无需重复更新。\\n' "$EXISTING_PR" >> "$GITHUB_STEP_SUMMARY"
else
  echo '没有需要导入的相关上游更新。' >> "$GITHUB_STEP_SUMMARY"
fi
'''},
        ],
    }


def create_workflow(root):
    from matrix import build_matrix
    source = load_yaml(root / '.upstream/abk-build.yml')
    matrix_source = load_yaml(root / '.upstream/abk-matrix.yml')
    if set(source['jobs']) != {'build-kernel'}:
        raise ValueError('Upstream build jobs changed; review the overlay before updating')
    dispatch = feature_inputs(matrix_source)
    declarations = source['on']['workflow_call']['inputs']
    mapping = {key: f'inputs.{key}' for key in dispatch}
    mapping.update({key: f'matrix.{key}' for key in ('android_version', 'kernel_version', 'sub_level', 'os_patch_level', 'revision')})
    mapping.update({'ksu_variant': 'inputs.kernelsu_variant', 'ksu_branch': 'inputs.kernelsu_branch'})
    for key, definition in declarations.items():
        if key in mapping:
            continue
        elif 'default' in definition:
            mapping[key] = expr_literal(definition['default'])
        elif definition.get('required'):
            raise ValueError(f'Unmapped required upstream input: {key}')
        else:
            mapping[key] = "''"
    for key in ('android_version', 'kernel_version', 'sub_level', 'os_patch_level', 'revision'):
        mapping[key] = f'matrix.{key}'
    mapping.update({
        'source_mode': "'aosp'", 'ksu_variant': 'inputs.kernelsu_variant',
        'ksu_branch': 'inputs.kernelsu_branch', 'upload_aux_artifacts': 'false',
        'custom_ref': 'vars.KSU_CUSTOM_REF',
        'virtualization_support': "(inputs.virtualization_support == 'off' && 'off' || (matrix.kernel_version == '6.12' && 'on' || (inputs.virtualization_support == 'on' && '678' || inputs.virtualization_support)))",
        'use_kpm': "(inputs.use_kpm && (inputs.kernelsu_variant == 'SukiSU' || (inputs.kernelsu_variant == 'BakaSU' && (inputs.kernelsu_branch == 'Stable(标准)' || inputs.kernelsu_branch == 'Custom(自定义)'))))",
        'kpm_password': "(inputs.use_kpm && (inputs.kernelsu_variant == 'SukiSU' || (inputs.kernelsu_variant == 'BakaSU' && (inputs.kernelsu_branch == 'Stable(标准)' || inputs.kernelsu_branch == 'Custom(自定义)'))) && inputs.kpm_password || '')",
    })
    # Required values are supplied by the matrix / renamed inputs above.
    job = copy.deepcopy(source['jobs']['build-kernel'])
    steps = job['steps']
    guard = step_named(steps, '校验自定义内核版本组合')
    if 'KNOWN_KERNEL_PAIRS=' not in guard.get('run', ''):
        raise ValueError('Upstream kernel whitelist guard changed')
    rows = build_matrix(root)
    pairs = '\n'.join(' '.join(row[key] for key in ('android_version', 'kernel_version', 'sub_level', 'os_patch_level')) for row in rows)
    guard['run'], replaced = re.subn(
        r"(KNOWN_KERNEL_PAIRS=\$\(cat <<'EOF'\n).*?(\nEOF\n\))",
        lambda match: match.group(1) + pairs + match.group(2), guard['run'], flags=re.S,
    )
    if replaced != 1:
        raise ValueError('Upstream KNOWN_KERNEL_PAIRS block changed')

    step = step_named(steps, '初始化构建环境')
    old = 'CONFIG="${{ inputs.android_version }}-${{ inputs.kernel_version }}-${{ inputs.sub_level }}"'
    if old not in step['run']:
        raise ValueError('Upstream CONFIG naming changed')
    step['run'] = step['run'].replace(old, old[:-1] + '-${{ inputs.os_patch_level }}"')

    step = step_named(steps, '初始化并同步内核源码')
    step['env'] = {'GKI_ANDROID': '${{ matrix.android_version }}', 'GKI_KERNEL': '${{ matrix.kernel_version }}',
                   'GKI_PATCH': '${{ matrix.os_patch_level }}', 'GKI_KERNEL_REF': '${{ matrix.kernel_ref }}'}
    step['run'] = SOURCE_SYNC
    step = step_named(steps, '添加一加 8E 处理器支持')
    old_url = 'curl -LSs "https://github.com/xingguangcuican6666/ABK/raw/refs/heads/dev/hmbird_patch.c" -o hmbird_patch.c'
    if old_url not in step['run']:
        raise ValueError('Upstream hmbird patch fetch changed')
    step['run'] = step['run'].replace(old_url, 'cp "$GITHUB_WORKSPACE/hmbird_patch.c" ./hmbird_patch.c')

    dependencies = step_named(steps, '克隆依赖仓库')
    steps.insert(steps.index(dependencies) + 1, {
        'name': '选择配套的 SUSFS 源码版本',
        'if': "inputs.enable_susfs && inputs.ksu_variant == 'SukiSU'",
        'env': {'PIN_KSU_VARIANT': '${{ inputs.ksu_variant }}',
                'PIN_KSU_REF': '${{ env.BRANCH }}', 'PIN_KSU_TIER': '${{ inputs.ksu_branch }}',
                'PIN_TARGET': '${{ matrix.android_version }}-${{ matrix.kernel_version }}'},
        'run': '''python3 "$GITHUB_WORKSPACE/tools/pin_build_dependencies.py" \\
  --root "$GITHUB_WORKSPACE" --susfs-dir "$SUSFS4KSU" \\
  --ksu-variant "$PIN_KSU_VARIANT" --ksu-ref "$PIN_KSU_REF" \\
  --ksu-tier "$PIN_KSU_TIER" --target "$PIN_TARGET"
''',
    })
    compile_step = step_named(steps, '编译内核')
    steps.insert(steps.index(compile_step), {
        'name': '检查 SUSFS 与 KernelSU 接口配套',
        'if': "inputs.enable_susfs && inputs.ksu_variant != 'None'",
        'run': 'python3 "$GITHUB_WORKSPACE/tools/validate_susfs_hooks.py" --kernel-root "$KERNEL_ROOT"',
    })

    image = step_named(steps, '准备 Boot 镜像')
    image['name'] = '准备 AK3 内核文件'
    image['run'] = PREPARE_IMAGE
    image['env'] = {'GKI_ANDROID': '${{ matrix.android_version }}', 'GKI_KERNEL': '${{ matrix.kernel_version }}',
                    'ARTIFACT_MODE': '${{ inputs.artifact_mode }}'}
    ak3 = step_named(steps, '创建 AnyKernel3 压缩包')
    ak3['name'] = '准备 AnyKernel3 刷机包'
    ak3['run'] = PREPARE_AK3
    ak3['env'] = {**image['env'], 'GKI_SUBLEVEL': '${{ matrix.sub_level }}', 'GKI_PATCH': '${{ matrix.os_patch_level }}'}
    for name in ('构建 Boot 镜像 (Android 12)', '构建 Boot 镜像 (Android 13+)'):
        step = step_named(steps, name)
        step['if'] = "inputs.artifact_mode != '仅 AK3' && (" + step['if'] + ')'
        step['run'] = step['run'].replace('gzip -n -k -f -9 ./Image > ./Image.gz', 'gzip -n -c -9 ./Image > ./Image.gz')
        if name.endswith('(Android 12)'):
            latest_patch = max(row['os_patch_level'] for row in rows if row['kernel_version'] == '5.10')
            step['env'] = {'BOOT_OS_PATCH_LEVEL': "${{ matrix.os_patch_level == 'lts' && '" + latest_patch + "' || matrix.os_patch_level }}"}
            step['run'] = step['run'].replace('-${{ inputs.os_patch_level }}_${{ inputs.revision }}', '-${BOOT_OS_PATCH_LEVEL}_${{ inputs.revision }}')
            step['run'] = step['run'].replace('--os_patch_level "${{ inputs.os_patch_level }}"', '--os_patch_level "$BOOT_OS_PATCH_LEVEL"')
    # Raw AK3 and boot files are the public outputs; App-specific bundle signing is unnecessary.
    bundle = step_named(steps, '打包签名内核 Bundle')
    upload = step_named(steps, '上传构建产物')
    steps.remove(bundle)
    upload_index = steps.index(upload)
    steps[upload_index:upload_index+1] = [
        {'name': '上传可直接刷入的 AK3 包', 'if': "inputs.artifact_mode == '仅 AK3'", 'uses': 'actions/upload-artifact@v6',
         'with': {'name': 'AnyKernel3-${{ inputs.ksu_variant }}-${{ env.CONFIG }}', 'path': 'AnyKernel3/*',
                  'if-no-files-found': 'error', 'compression-level': 9}},
        {'name': '上传 AK3 和 boot 镜像', 'if': "inputs.artifact_mode != '仅 AK3'", 'uses': 'actions/upload-artifact@v6',
         'with': {'name': '${{ inputs.ksu_variant }}_kernel-${{ env.CONFIG }}',
                  'path': '*AnyKernel3.zip\n*.img\n', 'if-no-files-found': 'error', 'compression-level': 9}},
    ]
    step_named(steps, '上传补丁冲突文件')['if'] = "failure() && env.REJ_COUNT > 0"
    # mkbootimg is only needed for boot output; build-tools also supplies signing tools.
    download = step_named(steps, '下载工具链')
    old_line = 'retry_toolchain_clone "$AOSP_MIRROR/platform/system/tools/mkbootimg" mkbootimg'
    if old_line not in download['run']:
        raise ValueError('Upstream mkbootimg clone changed')
    download['run'] = download['run'].replace(old_line, "if [ \"${{ inputs.artifact_mode }}\" != '仅 AK3' ]; then\n  " + old_line + '\nfi')
    cache = step_named(steps, '缓存工具链')
    cache['with']['key'] = "toolchain-${{ runner.os }}-${{ inputs.artifact_mode == '仅 AK3' && 'ak3' || 'boot' }}-v2"

    # Strip App manifest metadata preparation; its hard-coded repository identity is unrelated to GKI.
    for name in ('生成刷写外层 Magisk 依赖清单',):
        steps.remove(step_named(steps, name))
    job = transform(job, mapping)
    filtered_steps = []
    for step in job['steps']:
        if step.get('if') == "'aosp' == 'custom_git'":
            continue
        if step.get('if') == "'aosp' != 'custom_git'":
            step.pop('if')
        filtered_steps.append(step)
    job['steps'] = filtered_steps
    job['if'] = BUILD_IF
    job['needs'] = 'prepare-matrix'
    job['strategy'] = {'fail-fast': False, 'max-parallel': '${{ fromJSON(needs.prepare-matrix.outputs.max_parallel) }}',
                       'matrix': '${{ fromJSON(needs.prepare-matrix.outputs.matrix) }}'}
    job['runs-on'] = job['runs-on'].replace("'ubuntu-latest'", "'ubuntu-24.04'")
    job['timeout-minutes'] = 120
    prepare = {
        'name': '选择内核版本与补丁月份', 'runs-on': 'ubuntu-24.04',
        'if': BUILD_IF,
        'outputs': {'matrix': '${{ steps.matrix.outputs.matrix }}', 'count': '${{ steps.matrix.outputs.count }}',
                    'max_parallel': '${{ steps.matrix.outputs.max_parallel }}'},
        'steps': [
            {'name': '检出 gki', 'uses': 'actions/checkout@v6'},
            {'name': '检查并生成矩阵', 'id': 'matrix', 'shell': 'bash',
             'env': {'KERNEL_VERSIONS': '${{ inputs.kernel_versions }}', 'TARGETS': '${{ inputs.targets }}',
                     'INCLUDE_LTS': '${{ inputs.include_lts }}', 'MAX_PARALLEL': '${{ inputs.max_parallel }}'},
             'run': '''set -euo pipefail
python3 - <<'PY'
import os
value = float(os.environ['MAX_PARALLEL'])
if not value.is_integer() or not 1 <= value <= 20:
    raise SystemExit('同时构建数量必须是 1–20 的整数')
with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
    output.write(f'max_parallel={int(value)}\\n')
PY
python3 tools/matrix.py --kernel-versions "$KERNEL_VERSIONS" --targets "$TARGETS" \\
  --include-lts "$INCLUDE_LTS" --github-output --summary
'''},
        ],
    }
    workflow = {
        'name': 'GKI 全属性内核构建矩阵',
        'run-name': "${{ (github.event_name == 'schedule' || inputs.operation == '检查上游更新') && '检查上游更新' || format('{0} · {1} · {2}', inputs.kernelsu_variant, inputs.kernel_versions, inputs.artifact_mode) }}",
        'on': {'workflow_dispatch': {'inputs': dispatch}, 'schedule': [{'cron': '17 0 * * *'}]},
        'permissions': {'contents': 'read'},
        'jobs': {'prepare-matrix': prepare, 'build-kernel': job, 'check-upstream': updater_job()},
    }
    return workflow


def generate(root):
    workflow = create_workflow(root)
    lock = json.loads((root / 'upstream.lock.json').read_text(encoding='utf-8'))
    header = ('# Generated by tools/generate_workflow.py; edit the generator to keep changes across updates.\n'
              f"# ABK {lock['abk']['commit']}\n# Version catalog {lock['catalog']['commit']}\n")
    content = header + yaml.dump(workflow, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=120)
    # Reparse generated output before replacing the existing file.
    yaml.load(content, Loader=Loader)
    target = root / OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8', newline='\n')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    print(generate(args.root.resolve()))


if __name__ == '__main__':
    main()
