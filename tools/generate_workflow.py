#!/usr/bin/env python3
"""Regenerate the single Actions entry from locally maintained build templates."""
from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path

import yaml
from workflow_jobs import updater_job, release_job

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
    fields = copy.deepcopy(source['inputs'])
    for key in ('revision', 'trigger_release', 'upload_aux_artifacts'):
        if key not in fields:
            raise ValueError(f'Upstream matrix input changed: {key}')
        fields.pop(key)
    fields['build_time']['default'] = ''
    fields['build_time']['description'] = '构建时间（留空使用当前 UTC）'
    selection = {
        'operation': {'description': '操作', 'required': True, 'type': 'choice',
                      'options': ['编译内核', '仅编译 SukiSU 安装包', '检查上游更新'], 'default': '编译内核'},
        'kernel_versions': {'description': '内核系列（all 或逗号分隔：5.10,5.15,6.1,6.6,6.12）',
                            'required': True, 'type': 'string', 'default': 'all'},
        'targets': {'description': '指定版本（留空全矩阵；例 6.1.145 或 6.1.145@2025-08，逗号分隔）',
                    'required': False, 'type': 'string', 'default': ''},
        'include_lts': {'description': '包含 LTS（默认关闭）', 'required': False, 'type': 'boolean', 'default': False},
        'artifact_mode': {'description': '产物模式', 'required': True, 'type': 'choice',
                          'options': ['仅 AK3', 'AK3 + boot 镜像'], 'default': '仅 AK3'},
        'max_parallel': {'description': '同时构建数量（1–20）', 'required': True, 'type': 'number', 'default': 20},
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
  # KPM patches the final Image after compilation; compress that same image.
  lz4 -l -12 -f ./Image bootimgs/Image.lz4
  lz4 -dc bootimgs/Image.lz4 | cmp - ./Image
fi
'''

PREPARE_AK3 = '''set -euo pipefail
cp ./Image "$ANYKERNEL3/Image"
zip_name="${GKI_ANDROID}-${GKI_KERNEL}.${GKI_SUBLEVEL}-${GKI_PATCH}-AnyKernel3.zip"
(cd "$ANYKERNEL3" && zip -r "$GITHUB_WORKSPACE/$zip_name" ./*)
'''


BUILD_IF = "github.event_name == 'workflow_dispatch' && inputs.operation == '编译内核'"
PREPARE_IF = "github.event_name == 'workflow_dispatch' && (inputs.operation == '编译内核' || inputs.operation == '仅编译 SukiSU 安装包')"
MANAGER_IF = "github.event_name == 'workflow_dispatch' && (inputs.operation == '仅编译 SukiSU 安装包' || (inputs.operation == '编译内核' && inputs.kernelsu_variant == 'SukiSU'))"
KERNEL_JOB_IF = "always() && !cancelled() && needs.prepare-matrix.result == 'success' && (" + BUILD_IF + ")"

# Cache dependency downloads, Gradle task results and reusable Rust objects.
# APK outputs and signing files live outside these explicitly enumerated paths.
MANAGER_GRADLE_CACHE = '~/.gradle/caches/modules-2\n~/.gradle/wrapper\n'
MANAGER_GRADLE_BUILD_CACHE = '~/.gradle/caches/build-cache-1'
MANAGER_GRADLE_BUILD_CACHE_PREFIX = (
    'gki-manager-gradle-build-v1-${{ runner.os }}-${{ runner.arch }}-jdk21-sdk37-ndk29-'
    "${{ hashFiles('manager-source/manager/gradle/wrapper/gradle-wrapper.properties', "
    "'manager-source/manager/gradle/libs.versions.toml') }}-"
)
MANAGER_CARGO_CACHE = (
    '~/.cargo/registry/index\n~/.cargo/registry/cache\n~/.cargo/registry/src\n~/.cargo/git/db\n'
    'manager-source/target/*/release/deps\nmanager-source/target/*/release/build\n'
    'manager-source/target/*/release/.fingerprint\nmanager-source/target/release/deps\n'
    'manager-source/target/release/build\nmanager-source/target/release/.fingerprint\n'
    '!manager-source/target/**/release/deps/ksud-*\n'
    '!manager-source/target/**/release/deps/ksuinit-*\n'
)


def manager_job():
    return {
        'name': '编译配套 SukiSU 管理器 APK', 'if': MANAGER_IF,
        'needs': 'prepare-matrix', 'runs-on': 'ubuntu-24.04', 'timeout-minutes': 60,
        'outputs': {key: '${{ steps.sign.outputs.' + key + ' }}'
                    for key in ('manager_package', 'manager_cert_size', 'manager_cert_sha256', 'apk_sha256',
                                'manager_version_name', 'manager_version_code', 'apk_filename')},
        'steps': [
            {'name': '检出构建工具', 'uses': 'actions/checkout@v6', 'with': {'persist-credentials': False}},
            {'name': '检查固定 APK 签名配置',
             'env': {'SIGNING_CONFIGURED': "${{ secrets.SUKISU_APK_KEYSTORE != '' && secrets.SUKISU_APK_PASSWORD != '' }}"},
             'run': '''if [ "$SIGNING_CONFIGURED" != true ]; then
  echo '::error::请先按 README 配置 SUKISU_APK_KEYSTORE 和 SUKISU_APK_PASSWORD。签名密钥应长期保持不变。'
  exit 1
fi
'''},
            {'name': '下载本次固定的 SukiSU 源码快照', 'uses': 'actions/download-artifact@v8',
             'with': {'name': 'Source-SukiSU-${{ needs.prepare-matrix.outputs.ksu_short }}',
                      'path': 'ksu-source-bundle'}},
            {'name': '检出同一版本的 SukiSU 源码',
             'env': {'KSU_REF': '${{ needs.prepare-matrix.outputs.ksu_ref }}'},
             'run': '''python3 tools/checkout_ksu_snapshot.py --input "$GITHUB_WORKSPACE/ksu-source-bundle" \\
  --output "$GITHUB_WORKSPACE/manager-source" --expected-ref "$KSU_REF"
'''},
            {'name': '准备 Java 21', 'uses': 'actions/setup-java@v5',
             'with': {'distribution': 'temurin', 'java-version': '21'}},
            {'name': '恢复管理器 Gradle 依赖', 'id': 'manager-gradle-cache',
             'uses': 'actions/cache/restore@v5', 'continue-on-error': True, 'timeout-minutes': 3,
             'env': {'SEGMENT_DOWNLOAD_TIMEOUT_MINS': '2'},
             'with': {'path': MANAGER_GRADLE_CACHE,
                      'key': "gki-manager-gradle-v1-${{ runner.os }}-${{ runner.arch }}-jdk21-${{ hashFiles('manager-source/manager/**/*.gradle.kts', 'manager-source/manager/**/gradle.properties', 'manager-source/manager/**/libs.versions.toml', 'manager-source/manager/gradle/wrapper/gradle-wrapper.properties') }}",
                      'restore-keys': 'gki-manager-gradle-v1-${{ runner.os }}-${{ runner.arch }}-jdk21-'}},
            {'name': '恢复管理器 Gradle 编译缓存', 'id': 'manager-gradle-build-cache',
             'uses': 'actions/cache/restore@v5', 'continue-on-error': True, 'timeout-minutes': 3,
             'env': {'SEGMENT_DOWNLOAD_TIMEOUT_MINS': '2'},
             'with': {'path': MANAGER_GRADLE_BUILD_CACHE,
                      'key': MANAGER_GRADLE_BUILD_CACHE_PREFIX + '${{ github.run_id }}-${{ github.run_attempt }}',
                      'restore-keys': MANAGER_GRADLE_BUILD_CACHE_PREFIX}},
            {'name': '准备 Android SDK 与 NDK',
             'run': '''set -euo pipefail
sdkmanager="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
python3 -c "print('y\\n' * 100)" | "$sdkmanager" --licenses > /dev/null
"$sdkmanager" 'platforms;android-37.0' 'build-tools;37.0.0' 'ndk;29.0.14206865'
echo "ANDROID_NDK_HOME=$ANDROID_HOME/ndk/29.0.14206865" >> "$GITHUB_ENV"
'''},
            {'name': '准备 ARM64 Rust 工具链',
             'run': '''rustup toolchain install stable --profile minimal
rustup default stable
rustup target add aarch64-linux-android --toolchain stable
'''},
            {'name': '记录 Rust 编译器缓存版本', 'id': 'manager-rust-version',
             'run': 'echo "version=$(rustc -vV | sha256sum | cut -c1-20)" >> "$GITHUB_OUTPUT"'},
            {'name': '恢复管理器 Cargo 依赖', 'id': 'manager-cargo-cache',
             'uses': 'actions/cache/restore@v5', 'continue-on-error': True, 'timeout-minutes': 3,
             'env': {'SEGMENT_DOWNLOAD_TIMEOUT_MINS': '2'},
             'with': {'path': MANAGER_CARGO_CACHE,
                      'key': "gki-manager-cargo-v1-${{ runner.os }}-${{ runner.arch }}-ndk29-${{ steps.manager-rust-version.outputs.version }}-${{ hashFiles('manager-source/**/Cargo.lock', 'manager-source/**/Cargo.toml', 'manager-source/.github/scripts/setup-rust-build.sh', 'tools/build_sukisu_manager.sh') }}",
                      'restore-keys': 'gki-manager-cargo-v1-${{ runner.os }}-${{ runner.arch }}-ndk29-${{ steps.manager-rust-version.outputs.version }}-'}},
            {'name': '编译管理器与同版本 ksud',
             'id': 'manager-build',
             'env': {'KSU_PACKAGE_NAME': 'com.cyzrqh.gki.sukisu',
                     'ORG_GRADLE_PROJECT_KSU_PACKAGE_NAME': 'com.cyzrqh.gki.sukisu',
                     'ORG_GRADLE_PROJECT_KSU_NAME': 'SukiSU'},
             'run': '''bash -n tools/build_sukisu_manager.sh
bash tools/build_sukisu_manager.sh --source "$GITHUB_WORKSPACE/manager-source" --output "$RUNNER_TEMP/manager-build"
'''},
            {'name': '签名并校验可安装 APK', 'id': 'sign',
             'env': {'SUKISU_APK_KEYSTORE': '${{ secrets.SUKISU_APK_KEYSTORE }}',
                     'SUKISU_APK_PASSWORD': '${{ secrets.SUKISU_APK_PASSWORD }}',
                     'KSU_SHORT': '${{ needs.prepare-matrix.outputs.ksu_short }}'},
             'run': '''python3 tools/sign_sukisu_manager.py --unsigned "$RUNNER_TEMP/manager-build/manager-unsigned.apk" \\
  --output-dir "$GITHUB_WORKSPACE/manager-artifact"
'''},
            {'name': '记录管理器真实版本',
             'env': {'META_FILE': '${{ steps.sign.outputs.apk_filename }}',
                     'META_PACKAGE': '${{ steps.sign.outputs.manager_package }}',
                     'META_KSU_VERSION_CODE': '${{ steps.sign.outputs.manager_version_code }}',
                     'META_KSU_VERSION_NAME': '${{ steps.sign.outputs.manager_version_name }}',
                     'META_KSU_REF': '${{ needs.prepare-matrix.outputs.ksu_ref }}',
                     'META_KSU_BRANCH': '${{ needs.prepare-matrix.outputs.effective_ksu_branch }}',
                     'META_MANAGER_CERT_SIZE': '${{ steps.sign.outputs.manager_cert_size }}',
                     'META_MANAGER_CERT_SHA256': '${{ steps.sign.outputs.manager_cert_sha256 }}',
                     'META_APK_SHA256': '${{ steps.sign.outputs.apk_sha256 }}',
                     'META_ENABLE_SUSFS': '${{ inputs.enable_susfs }}'},
             'run': 'python3 tools/capture_manager_metadata.py --output manager-artifact/manager.json'},
            {'name': '提供内核配对的公开签名信息', 'uses': 'actions/upload-artifact@v6',
             'with': {'name': 'Manager-Identity-${{ needs.prepare-matrix.outputs.ksu_short }}',
                      'path': 'manager-artifact/manager.json', 'if-no-files-found': 'error',
                      'compression-level': 0, 'overwrite': True}},
            {'name': '上传管理器发布中转包', 'uses': 'actions/upload-artifact@v6',
             'with': {'name': 'Release-Manager-${{ steps.sign.outputs.apk_filename }}',
                      'path': 'manager-artifact/*', 'if-no-files-found': 'error', 'compression-level': 0}},
            {'name': '上传 SukiSU 安装包', 'uses': 'actions/upload-artifact@v7',
             'with': {'path': 'manager-artifact/${{ steps.sign.outputs.apk_filename }}',
                      'archive': False, 'if-no-files-found': 'error', 'overwrite': True}},
            # Publish APK identity before optional cache uploads, so dependent
            # kernels can validate the signature without waiting for cache IO.
            {'name': '保存管理器 Gradle 编译缓存', 'uses': 'actions/cache/save@v5',
             'if': "always() && !cancelled() && steps.manager-build.outcome == 'success' && steps.manager-gradle-build-cache.outputs.cache-primary-key != '' && steps.manager-gradle-build-cache.outputs.cache-hit != 'true'",
             'continue-on-error': True, 'timeout-minutes': 3,
             'with': {'path': MANAGER_GRADLE_BUILD_CACHE,
                      'key': '${{ steps.manager-gradle-build-cache.outputs.cache-primary-key }}'}},
            {'name': '保存管理器 Gradle 依赖', 'uses': 'actions/cache/save@v5',
             'if': "always() && !cancelled() && steps.manager-build.outcome != 'skipped' && steps.manager-gradle-cache.outputs.cache-primary-key != '' && steps.manager-gradle-cache.outputs.cache-hit != 'true'",
             'continue-on-error': True, 'timeout-minutes': 3,
             'with': {'path': MANAGER_GRADLE_CACHE,
                      'key': '${{ steps.manager-gradle-cache.outputs.cache-primary-key }}'}},
            {'name': '保存管理器 Cargo 依赖', 'uses': 'actions/cache/save@v5',
             'if': "always() && !cancelled() && steps.manager-build.outcome != 'skipped' && steps.manager-cargo-cache.outputs.cache-primary-key != '' && steps.manager-cargo-cache.outputs.cache-hit != 'true'",
             'continue-on-error': True, 'timeout-minutes': 3,
             'with': {'path': MANAGER_CARGO_CACHE,
                      'key': '${{ steps.manager-cargo-cache.outputs.cache-primary-key }}'}},
        ],
    }


def create_workflow(root):
    from matrix import build_matrix
    source = load_yaml(root / 'templates/kernel-job.yml')
    matrix_source = load_yaml(root / 'templates/menu.yml')
    if set(source) != {'inputs', 'job'}:
        raise ValueError('Local kernel build template has an unsupported schema')
    dispatch = feature_inputs(matrix_source)
    declarations = source['inputs']
    mapping = {key: f'inputs.{key}' for key in dispatch}
    mapping.update({key: f'matrix.{key}' for key in ('android_version', 'kernel_version', 'sub_level', 'os_patch_level', 'revision')})
    mapping.update({'ksu_variant': 'inputs.kernelsu_variant',
                    'ksu_branch': 'needs.prepare-matrix.outputs.effective_ksu_branch'})
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
        'ksu_branch': 'needs.prepare-matrix.outputs.effective_ksu_branch', 'upload_aux_artifacts': 'false',
        'custom_ref': 'vars.KSU_CUSTOM_REF',
        'virtualization_support': "(inputs.virtualization_support == 'off' && 'off' || (matrix.kernel_version == '6.12' && 'on' || (inputs.virtualization_support == 'on' && '678' || inputs.virtualization_support)))",
        'use_kpm': "(inputs.use_kpm && (inputs.kernelsu_variant == 'SukiSU' || (inputs.kernelsu_variant == 'BakaSU' && (needs.prepare-matrix.outputs.effective_ksu_branch == 'Stable(标准)' || needs.prepare-matrix.outputs.effective_ksu_branch == 'Custom(自定义)'))))",
        'kpm_password': "(inputs.use_kpm && (inputs.kernelsu_variant == 'SukiSU' || (inputs.kernelsu_variant == 'BakaSU' && (needs.prepare-matrix.outputs.effective_ksu_branch == 'Stable(标准)' || needs.prepare-matrix.outputs.effective_ksu_branch == 'Custom(自定义)'))) && inputs.kpm_password || '')",
    })
    # Required values are supplied by the matrix / renamed inputs above.
    job = copy.deepcopy(source['job'])
    steps = job['steps']
    guard = step_named(steps, '校验自定义内核版本组合')
    if 'KNOWN_KERNEL_PAIRS=' not in guard.get('run', ''):
        raise ValueError('Upstream kernel whitelist guard changed')
    rows = build_matrix(root)
    # Read the catalog at runtime: official catalog PRs only change data, so
    # newly merged versions must not require regenerating workflow permissions.
    pairs_script = '''KNOWN_KERNEL_PAIRS=$(python3 - <<'PY'
import os,sys
from pathlib import Path
root = Path(os.environ['GITHUB_WORKSPACE'])
sys.path.insert(0, str(root / 'tools'))
from matrix import load_catalog
for entry in load_catalog(root)['entries']:
    if entry['date'] != 'lts':
        print(entry['android_version'], entry['kernel_version'], entry['kernel'].rsplit('.', 1)[1], entry['date'])
PY
)'''
    guard['run'], replaced = re.subn(
        r"KNOWN_KERNEL_PAIRS=\$\(cat <<'EOF'\n.*?\nEOF\n\)",
        lambda match: pairs_script, guard['run'], flags=re.S,
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
    if 'cp "$GITHUB_WORKSPACE/hmbird_patch.c" ./hmbird_patch.c' not in step['run']:
        raise ValueError('OnePlus support must use the locally maintained source file')

    # Fetch SukiSU once before the matrix expands. Every kernel receives the
    # same full-history bundle; a later upstream rebase cannot change its HEAD.
    source_sync_index = steps.index(step_named(steps, '初始化并同步内核源码'))
    steps[source_sync_index:source_sync_index] = [
        {'name': '下载本次固定的 SukiSU 源码快照', 'if': "inputs.ksu_variant == 'SukiSU'",
         'uses': 'actions/download-artifact@v8',
         'with': {'name': 'Source-SukiSU-${{ needs.prepare-matrix.outputs.ksu_short }}',
                  'path': 'ksu-source-bundle'}},
        {'name': '检出内核配套的 SukiSU 快照', 'if': "inputs.ksu_variant == 'SukiSU'",
         'env': {'KSU_REF': '${{ needs.prepare-matrix.outputs.ksu_ref }}'},
         'run': '''python3 tools/checkout_ksu_snapshot.py --input "$GITHUB_WORKSPACE/ksu-source-bundle" \\
  --output "$KERNEL_ROOT/KernelSU" --expected-ref "$KSU_REF"
'''},
    ]

    # Resolve mutable refs once, so all kernels and the APK share one commit.
    branch = step_named(steps, '确定 KernelSU 分支')
    branch['env'] = {
        'BRANCH': '${{ needs.prepare-matrix.outputs.ksu_ref }}',
        'RESOLVED_KSU_REPO': '${{ needs.prepare-matrix.outputs.ksu_repo }}',
        'RESOLVED_KSU_SOURCE_BRANCH': '${{ needs.prepare-matrix.outputs.ksu_source_branch }}',
        'EFFECTIVE_KSU_BRANCH': '${{ needs.prepare-matrix.outputs.effective_ksu_branch }}',
    }
    branch['run'] = '''python3 - <<'PY'
import os
from pathlib import Path
keys = ('BRANCH', 'RESOLVED_KSU_REPO', 'RESOLVED_KSU_SOURCE_BRANCH', 'EFFECTIVE_KSU_BRANCH')
with Path(os.environ['GITHUB_ENV']).open('a', encoding='utf-8') as output:
    for key in keys:
        value = os.environ[key]
        if '\\n' in value or '\\r' in value:
            raise SystemExit('Invalid shared KernelSU source metadata')
        output.write(f'{key}={value}\\n')
PY
'''

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
    integration = step_named(steps, '添加 KernelSU')
    steps.insert(steps.index(integration) + 1, {
        'name': '应用完整 SukiSU SUSFS 2.3 适配',
        'if': "env.SUSFS_COMPAT_VERSION == 'v2.3.0'",
        'run': '''python3 "$GITHUB_WORKSPACE/tools/apply_susfs23.py" \\
  --kernel-root "$KERNEL_ROOT" --susfs-dir "$SUSFS4KSU"
''',
    })
    # The complete 2.3 port supplies the native implementations. Older flat-tree
    # shims must never overwrite them with legacy fallbacks.
    for name in ('确保 KernelSU SUSFS ABI 兼容', '补齐 SukiSU SUSFS 内联 hook 符号',
                 '最终修复 SukiSU/BakaSU 源码兼容'):
        step = step_named(steps, name)
        step['if'] = "env.SUSFS_COMPAT_VERSION != 'v2.3.0' && (" + step['if'] + ')'
    config = step_named(steps, '添加 SUSFS 配置')
    legacy_guard = 'if [ "${{ inputs.ksu_variant }}" == "SukiSU" ]; then'
    if config['run'].count(legacy_guard) != 1:
        raise ValueError('Upstream SUSFS configuration guard changed')
    config['run'] = config['run'].replace(
        legacy_guard, 'if [ "${SUSFS_COMPAT_VERSION:-}" != v2.3.0 ] && [ "${{ inputs.ksu_variant }}" == "SukiSU" ]; then')
    config['run'] += '''
if [ "${SUSFS_COMPAT_VERSION:-}" = v2.3.0 ]; then
  # The real preinit runner requires installed target UAPI headers.
  echo 'CONFIG_HEADERS_INSTALL=y' >> "$DEFCONFIG"
fi
'''
    android16 = step_named(steps, '修复 SukiSU/BakaSU android16-6.12 源码兼容')
    old = 'super_access_c = ksu_dir / "kpm/super_access.c"'
    if android16['run'].count(old) != 1:
        raise ValueError('Upstream KPM 6.12 compatibility source lookup changed')
    android16['run'] = android16['run'].replace(old,
        'super_access_c = next((path for path in (ksu_dir / "extension/kpm/super_access.c", '
        'ksu_dir / "kpm/super_access.c") if path.is_file()), ksu_dir / "kpm/super_access.c")')
    compile_step = step_named(steps, '编译内核')
    steps.insert(steps.index(compile_step), {
        'name': '在内核中记录实际构建配置',
        'run': '''set -euo pipefail
sed -i '/^CONFIG_IKCONFIG=/d; /^# CONFIG_IKCONFIG is not set$/d' "$DEFCONFIG"
echo 'CONFIG_IKCONFIG=y' >> "$DEFCONFIG"
''',
    })
    steps.insert(steps.index(compile_step), {
        'name': '编译前读取本次 APK 的实际签名信息', 'if': "inputs.ksu_variant == 'SukiSU'",
        'env': {'GH_TOKEN': '${{ github.token }}',
                'KSU_REF': '${{ needs.prepare-matrix.outputs.ksu_ref }}',
                'KSU_SHORT': '${{ needs.prepare-matrix.outputs.ksu_short }}'},
        'run': '''python3 "$GITHUB_WORKSPACE/tools/wait_manager_identity.py" \\
  --expected-ref "$KSU_REF" --artifact-name "Manager-Identity-$KSU_SHORT" \\
  --github-env "$GITHUB_ENV"
''',
    })
    steps.insert(steps.index(compile_step), {
        'name': '配对管理器包名与签名', 'if': "inputs.ksu_variant == 'SukiSU'",
        'run': '''python3 "$GITHUB_WORKSPACE/tools/set_manager_signature.py" \\
  --kernel-root "$KERNEL_ROOT" --package "$MANAGER_PACKAGE" \\
  --cert-size "$MANAGER_CERT_SIZE" --cert-sha256 "$MANAGER_CERT_SHA256" \\
  --version-code "$MANAGER_VERSION_CODE"
''',
    })
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
        {'name': '记录内核实际配置', 'id': 'kernel-assets',
         'env': {'META_FILE': '${{ matrix.android_version }}-${{ matrix.kernel_version }}.${{ matrix.sub_level }}-${{ matrix.os_patch_level }}-AnyKernel3.zip',
                 'META_ANDROID_VERSION': '${{ matrix.android_version }}', 'META_KERNEL_VERSION': '${{ matrix.kernel_version }}',
                 'META_SUB_LEVEL': '${{ matrix.sub_level }}', 'META_OS_PATCH_LEVEL': '${{ matrix.os_patch_level }}',
                 'META_KSU_VARIANT': '${{ inputs.ksu_variant }}',
                 'META_KSU_BRANCH': '${{ needs.prepare-matrix.outputs.effective_ksu_branch }}',
                 'META_KSU_REF': '${{ needs.prepare-matrix.outputs.ksu_ref }}',
                 'META_KSU_VERSION_CODE': "${{ inputs.ksu_variant == 'SukiSU' && env.MANAGER_VERSION_CODE || env.KSU_VERSION }}",
                 'META_KSU_VERSION_NAME': "${{ inputs.ksu_variant == 'SukiSU' && env.MANAGER_VERSION_NAME || '' }}",
                 'META_ENABLE_SUSFS': '${{ inputs.enable_susfs }}',
                 'META_EXPECTED_SUSFS_VERSION': '${{ env.SUSFS_COMPAT_VERSION }}',
                 'ARTIFACT_MODE': '${{ inputs.artifact_mode }}'},
         'run': '''python3 tools/capture_build_metadata.py --kernel-root "$KERNEL_ROOT" \\
  --image "$GITHUB_WORKSPACE/Image" --output release-ready/kernel.json
python3 tools/package_release_kernel.py --metadata release-ready/kernel.json \\
  --workspace "$GITHUB_WORKSPACE" --output release-ready --mode "$ARTIFACT_MODE"
'''},
        {'name': '上传 Release 内核文件', 'uses': 'actions/upload-artifact@v6',
         'with': {'name': 'Release-${{ inputs.ksu_variant }}-${{ env.CONFIG }}', 'path': 'release-ready/*',
                  'if-no-files-found': 'error', 'compression-level': 0}},
        {'name': '上传可直接刷入的 AK3 包', 'uses': 'actions/upload-artifact@v7',
         'with': {'path': 'release-ready/${{ steps.kernel-assets.outputs.ak3 }}',
                  'archive': False, 'if-no-files-found': 'error', 'overwrite': True}},
        *[{'name': '上传独立 ' + label + ' 镜像',
           'if': "steps.kernel-assets.outputs." + key + " != ''", 'uses': 'actions/upload-artifact@v7',
           'with': {'path': 'release-ready/${{ steps.kernel-assets.outputs.' + key + ' }}',
                    'archive': False, 'if-no-files-found': 'error', 'overwrite': True}}
          for key, label in (('boot', 'boot'), ('boot_gz', 'gzip boot'), ('boot_lz4', 'LZ4 boot'))],
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
    summary = step_named(job['steps'], '构建信息摘要')
    summary['run'] = re.sub(r'^echo "管理器包名[^\n]+$',
                            'echo "管理器包名    : ${GKI_MANAGER_PACKAGE:-由所选 KernelSU 决定}"',
                            summary['run'], count=1, flags=re.M)
    job['if'] = KERNEL_JOB_IF
    job['needs'] = ['prepare-matrix']
    job['permissions'] = {'contents': 'read', 'actions': 'read'}
    job['strategy'] = {'fail-fast': False, 'max-parallel': '${{ fromJSON(needs.prepare-matrix.outputs.max_parallel) }}',
                       'matrix': '${{ fromJSON(needs.prepare-matrix.outputs.matrix) }}'}
    job['runs-on'] = job['runs-on'].replace("'ubuntu-latest'", "'ubuntu-24.04'")
    job['timeout-minutes'] = 120
    prepare = {
        'name': '选择内核版本与补丁月份', 'runs-on': 'ubuntu-24.04',
        'if': PREPARE_IF,
        'outputs': {'matrix': '${{ steps.matrix.outputs.matrix }}', 'count': '${{ steps.matrix.outputs.count }}',
                    'max_parallel': '${{ steps.matrix.outputs.max_parallel }}',
                    **{key: '${{ steps.sources.outputs.' + key + ' }}' for key in
                       ('ksu_ref', 'ksu_repo', 'ksu_short', 'ksu_source_branch', 'effective_ksu_branch')}},
        'steps': [
            {'name': '检出 gki', 'uses': 'actions/checkout@v6', 'with': {'persist-credentials': False}},
            {'name': '检查并生成矩阵', 'id': 'matrix', 'shell': 'bash', 'if': BUILD_IF,
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
with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
    summary.write(f'- 同时构建上限：{int(value)}（实际调度取决于 GitHub 账号额度与可用机器）\\n')
PY
python3 tools/matrix.py --kernel-versions "$KERNEL_VERSIONS" --targets "$TARGETS" \\
  --include-lts "$INCLUDE_LTS" --github-output --summary
'''},
            {'name': '固定本次 KernelSU 与管理器源码', 'id': 'sources',
             'env': {'KSU_VARIANT': "${{ inputs.operation == '仅编译 SukiSU 安装包' && 'SukiSU' || inputs.kernelsu_variant }}",
                     'KSU_TIER': '${{ inputs.kernelsu_branch }}',
                     'CUSTOM_REF': '${{ vars.KSU_CUSTOM_REF }}', 'ENABLE_SUSFS': '${{ inputs.enable_susfs }}',
                     'GH_TOKEN': '${{ github.token }}'},
             'run': '''python3 tools/resolve_build_sources.py --variant "$KSU_VARIANT" --tier "$KSU_TIER" \\
  --custom-ref "$CUSTOM_REF" --susfs "$ENABLE_SUSFS"
'''},
            {'name': '仅下载一次官方 SukiSU 源码', 'uses': 'actions/checkout@v6',
             'if': "inputs.kernelsu_variant == 'SukiSU' || inputs.operation == '仅编译 SukiSU 安装包'",
             'with': {'repository': 'SukiSU-Ultra/SukiSU-Ultra', 'ref': '${{ steps.sources.outputs.ksu_ref }}',
                      'path': 'ksu-snapshot-source', 'fetch-depth': 0, 'persist-credentials': False}},
            {'name': '展开矩阵前验证并固定源码快照',
             'if': "inputs.kernelsu_variant == 'SukiSU' || inputs.operation == '仅编译 SukiSU 安装包'",
             'env': {'KSU_REF': '${{ steps.sources.outputs.ksu_ref }}',
                     'KSU_TIER': '${{ steps.sources.outputs.effective_ksu_branch }}',
                     'ENABLE_SUSFS': "${{ inputs.operation == '编译内核' && inputs.enable_susfs || false }}"},
             'run': '''python3 tools/snapshot_ksu_source.py --source "$GITHUB_WORKSPACE/ksu-snapshot-source" \\
  --output "$RUNNER_TEMP/ksu-source" --expected-ref "$KSU_REF" --root "$GITHUB_WORKSPACE" \\
  --susfs "$ENABLE_SUSFS" --tier "$KSU_TIER"
'''},
            {'name': '分发本次不可变源码快照', 'uses': 'actions/upload-artifact@v6',
             'if': "inputs.kernelsu_variant == 'SukiSU' || inputs.operation == '仅编译 SukiSU 安装包'",
             'with': {'name': 'Source-SukiSU-${{ steps.sources.outputs.ksu_short }}',
                      'path': '${{ runner.temp }}/ksu-source/*', 'compression-level': 0, 'if-no-files-found': 'error'}},
        ],
    }
    workflow = {
        'name': 'GKI 全属性内核构建矩阵',
        'run-name': "${{ (github.event_name == 'schedule' || inputs.operation == '检查上游更新') && '检查上游更新' || (inputs.operation == '仅编译 SukiSU 安装包' && '仅编译 SukiSU 安装包' || format('{0} · {1} · {2}', inputs.kernelsu_variant, inputs.kernel_versions, inputs.artifact_mode)) }}",
        'on': {'workflow_dispatch': {'inputs': dispatch}, 'schedule': [{'cron': '17 0 * * *'}]},
        'permissions': {'contents': 'read'},
        'jobs': {'prepare-matrix': prepare, 'build-sukisu-manager': manager_job(),
                 'build-kernel': job, 'publish-release': release_job(), 'check-upstream': updater_job()},
    }
    return workflow


def generate(root):
    workflow = create_workflow(root)
    header = ('# Generated by tools/generate_workflow.py; edit the generator to keep changes across updates.\n'
              '# Locally maintained templates and official-source version catalog.\n')
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
