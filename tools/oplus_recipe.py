#!/usr/bin/env python3
"""Run reviewed OPlus build-method phases against an official fixed kernel.

CCTV's pinned recipe supplies compiler/configuration methods only. Its kernel
mirror, moving KernelSU setup, cache deletion, uploads and release jobs are
never executed. The parent workflow supplies KernelSU, SUSFS and APK identity.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

from oplus_catalog import OFFICIAL_REPOSITORIES, RECIPE_REPOSITORIES


SHA = re.compile(r'[0-9a-f]{40}\Z')
SERIES = {'6.1': ('android14', '20'), '6.6': ('android15', '18'), '6.12': ('android16', '19')}
INPUTS = {'kernelsu_variant', 'enable_susfs', 'use_kpm', 'use_lz4kd', 'use_bbg',
          'use_bbr', 'use_proxy_optimization', 'use_unicode_bypass'}
CONFIG_NAMES = ('添加SUSFS 配置项', '添加 KSU & 其他配置项', '应用 lz4kd 补丁',
                '启用网络功能增强优化配置', '添加 BBR 等一系列拥塞控制算法',
                '启用内核级基带保护', '添加制作名称')
CORE_NAMES = ('添加KernelSU', '应用 KernelSU & SUSFS 补丁', '添加SUSFS 配置项',
              '添加 KSU & 其他配置项', '添加制作名称', '构建内核', '克隆 AnyKernel3 并打包')
CONDITIONS = {'添加SUSFS 配置项': 'inputs.susfs_enable', '添加 KSU & 其他配置项': '',
              '应用 lz4kd 补丁': 'inputs.lz4kd_enable', '启用网络功能增强优化配置': 'inputs.better_net',
              '添加 BBR 等一系列拥塞控制算法': '', '启用内核级基带保护': 'inputs.baseband_guard',
              '添加制作名称': ''}
BBG_REPOSITORY = 'vc-teahouse/Baseband-guard'
LZ4_REPOSITORY = 'ShirkNeko/SukiSU_patch'
MAX_RECIPE = 2 * 1024 * 1024
EXPRESSION = re.compile(r'\$\{\{\s*(.*?)\s*\}\}')


class RecipeError(ValueError):
    """The selected method cannot be proven to match the reviewed layout."""


@dataclass(frozen=True)
class Step:
    name: str
    script: str


def strict_json(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECIPE:
        raise RecipeError('Missing or oversized OPlus input JSON')
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise RecipeError('Duplicate OPlus input JSON field')
            output[key] = value
        return output
    try:
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as error:
        raise RecipeError('OPlus inputs must be strict UTF-8 JSON') from error


def validate_profile(profile: dict) -> dict:
    fields = ('id', 'platform', 'kernel', 'android_version', 'kernel_version', 'sub_level',
              'kernel_repo', 'kernel_branch', 'kernel_ref', 'recipe_repository', 'recipe_ref',
              'recipe_workflow', 'recipe_sha256', 'system', 'compatibility')
    if not isinstance(profile, dict) or any(not isinstance(profile.get(key), str) for key in fields):
        raise RecipeError('Missing resolved OPlus source profile')
    series = profile['kernel_version']
    if (series not in SERIES or profile['android_version'] != SERIES[series][0]
            or not re.fullmatch(r'(?:0|[1-9][0-9]*)', profile['sub_level'])
            or profile['kernel'] != series + '.' + profile['sub_level']
            or not re.fullmatch(r'(?:sm|mt)[0-9]{4}', profile['platform'])
            or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,99}', profile['id'])
            or profile['kernel_repo'] not in OFFICIAL_REPOSITORIES
            or profile['recipe_repository'] not in RECIPE_REPOSITORIES
            or not SHA.fullmatch(profile['kernel_ref']) or not SHA.fullmatch(profile['recipe_ref'])
            or not re.fullmatch(r'[0-9a-f]{64}', profile['recipe_sha256'])
            or not re.fullmatch(r'\.github/workflows/fastbuild_' + re.escape(profile['kernel'])
                                + r'(?:_gki|_mtk)?\.yml', profile['recipe_workflow'])):
        raise RecipeError('Unsupported or inconsistent resolved OPlus profile')
    return dict(profile)


def bool_value(value) -> bool:
    if value is True or value == 'true':
        return True
    if value is False or value == 'false':
        return False
    raise RecipeError('OPlus feature inputs must be booleans')


def native_inputs(inputs: dict, profile: dict) -> dict[str, str]:
    if not isinstance(inputs, dict) or set(inputs) != INPUTS or inputs['kernelsu_variant'] not in ('SukiSU', 'BakaSU'):
        raise RecipeError('Expected the eight supported OPlus menu inputs')
    flags = {key: bool_value(value) for key, value in inputs.items() if key != 'kernelsu_variant'}
    yes = lambda name: 'true' if flags[name] else 'false'
    return {'ksu_type': 'sukisu' if inputs['kernelsu_variant'] == 'SukiSU' else 'resukisu',
            'susfs_enable': yes('enable_susfs'), 'kpm_enable': yes('use_kpm'),
            'lz4kd_enable': yes('use_lz4kd'), 'baseband_guard': yes('use_bbg'),
            'bbr_enable': yes('use_bbr'),
            'better_net': 'false' if profile['platform'].startswith('mt') else yes('use_proxy_optimization'),
            'lz4_enable': 'false', 'ssg_enable': 'false', 'adios_enable': 'false',
            'droidspaces_enable': 'false', 'rekernel_enable': 'false', 'ccache_update': 'false',
            'ccache_debug': 'false', 'kernel_suffix': '', 'KERNEL_SUFFIX': ''}


def parse_recipe(raw: bytes, profile: dict) -> dict:
    profile = validate_profile(profile)
    if not 0 < len(raw) <= MAX_RECIPE or hashlib.sha256(raw).hexdigest() != profile['recipe_sha256']:
        raise RecipeError('OPlus reference recipe differs from its reviewed SHA256')
    try:
        document = yaml.safe_load(raw.decode('utf-8'))
        environment = document['env']
        steps = document['jobs']['build']['steps']
    except (ValueError, KeyError, TypeError, UnicodeError, yaml.YAMLError) as error:
        raise RecipeError('Unsupported native OPlus recipe structure') from error
    suffix = re.search(r'(_(?:gki|mtk))\.yml\Z', profile['recipe_workflow'])
    expected_sub = profile['sub_level'] + (suffix[1] if suffix else '')
    if (not isinstance(environment, dict) or not isinstance(steps, list) or not 20 <= len(steps) <= 40
            or environment.get('ANDROID_VERSION') != profile['android_version']
            or environment.get('KERNEL_VERSION') != profile['kernel_version']
            or str(environment.get('SUB_VERSION')) != expected_sub
            or not isinstance(environment.get('KERNEL_NAME'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.+-]{1,200}', environment['KERNEL_NAME'])):
        raise RecipeError('Reference recipe target or native kernel release changed')
    for name in dict.fromkeys((*CORE_NAMES, *CONFIG_NAMES)):
        selected = [step for step in steps if isinstance(step, dict) and step.get('name') == name]
        if len(selected) != 1 or not isinstance(selected[0].get('run'), str) or 'uses' in selected[0]:
            raise RecipeError('Reference recipe core build layout changed: ' + name)
    starts = [step for step in steps if isinstance(step, dict)
              and re.fullmatch(r'安装环境依赖\+初始化源码仓库及llvm-[Cc]lang(?:18|19|20)工具链', step.get('name', ''))]
    if len(starts) != 1 or not isinstance(starts[0].get('run'), str):
        raise RecipeError('Reference recipe compiler bootstrap layout changed')
    return document


def git(directory: Path, *args: str) -> str:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('GIT_CONFIG_') and key not in {
                       'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                       'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_SHALLOW_FILE'}}
    environment.update(GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1')
    try:
        result = subprocess.run(['git', '-C', str(directory), '-c', 'core.hooksPath=',
            '-c', 'core.autocrlf=false', '-c', 'credential.helper=', '-c', 'http.extraHeader=', *args],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=240, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        raise RecipeError('OPlus fixed source Git operation could not complete') from None
    if result.returncode:
        raise RecipeError('OPlus fixed source Git operation failed (exit ' + str(result.returncode) + ')')
    return result.stdout.strip()


def load_recipe(root: Path, profile: dict) -> dict:
    profile = validate_profile(profile)
    if root.is_symlink() or not root.is_dir():
        raise RecipeError('Reference recipe must be an independent checkout')
    root = root.resolve(strict=True)
    if git(root, 'rev-parse', 'HEAD') != profile['recipe_ref']:
        raise RecipeError('Reference recipe checkout differs from the fixed method commit')
    path = root / profile['recipe_workflow']
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECIPE:
        raise RecipeError('Reviewed reference workflow is missing')
    return parse_recipe(path.read_bytes(), profile)


def select_step(recipe: dict, name: str) -> dict:
    steps = [step for step in recipe['jobs']['build']['steps'] if step.get('name') == name]
    if len(steps) != 1 or not isinstance(steps[0].get('run'), str):
        raise RecipeError('Expected one native method step: ' + name)
    return steps[0]


def rewrite_own_urls(script: str, profile: dict) -> str:
    fixed = 'https://raw.githubusercontent.com/' + profile['recipe_repository'] + '/' + profile['recipe_ref'] + '/'
    script = script.replace('https://github.com/$GITHUB_REPOSITORY/raw/refs/heads/$GITHUB_REF_NAME/', fixed)
    for repository in RECIPE_REPOSITORIES:
        script = script.replace('https://github.com/' + repository + '/raw/refs/heads/main/', fixed)
        script = script.replace('https://raw.githubusercontent.com/' + repository + '/refs/heads/main/', fixed)
    return script


def render(script: str, recipe: dict, profile: dict, values: dict[str, str]) -> str:
    script = rewrite_own_urls(script, profile)
    mapping = {'github.workspace': '${GITHUB_WORKSPACE}',
               'env.CCACHE_DIR': '${CCACHE_DIR}', 'env.CCACHE_MAXSIZE': '${CCACHE_MAXSIZE}'}
    mapping.update({'env.' + key: str(recipe['env'][key]) for key in
                    ('ANDROID_VERSION', 'KERNEL_VERSION', 'SUB_VERSION', 'KERNEL_NAME')})
    mapping.update({'github.event.inputs.' + key: value for key, value in values.items()})
    mapping.update({'inputs.' + key: value for key, value in values.items()})
    def replace(match):
        key = match[1].strip()
        if key not in mapping:
            raise RecipeError('Unknown expression in selected native OPlus step: ' + key)
        return mapping[key]
    script = EXPRESSION.sub(replace, script)
    if '${{' in script or re.search(r'(^|[\s;&|])gh\s', script):
        raise RecipeError('Unexpanded expression or GitHub mutation command in native phase')
    if re.search(r'(?:KernelSU|BakaSU|ReSukiSU|SukiSU-Ultra)[^\n]*setup\.sh|setup\.sh[^\n]*\|\s*bash', script):
        raise RecipeError('Moving KernelSU or remote setup execution is forbidden in recipe phases')
    return script


def toolchain_script(recipe: dict, profile: dict, values: dict[str, str]) -> Step:
    initial = recipe['jobs']['build']['steps'][0]
    if not str(initial.get('name', '')).startswith('安装环境依赖+初始化源码仓库及llvm-'):
        raise RecipeError('Native initialization is not the first recipe step')
    run = initial['run']
    blocks = [block for block in re.split(r'\n\s*\n', run)
              if re.search(r'mkdir -p (?:clang(?:18|19|20)|rust)\b|aria2c .* -o build-tools\.zip', block)]
    clang = SERIES[profile['kernel_version']][1]
    if len(blocks) != (3 if clang == '19' else 2) or not any('mkdir -p clang' + clang in block for block in blocks):
        raise RecipeError('Native toolchain download groups changed')
    rendered = []
    for block in blocks:
        urls = re.findall(r'https://[^\s"\']+', block)
        if not urls or any(not re.fullmatch(
            r'https://github\.com/cctv18/oneplus_sm8650_toolchain/releases/download/LLVM-Clang(?:18|19|20)-r[0-9]+/(?:clang-r[0-9]+\.zip|rust\.zip|build-tools\.zip)', url)
            for url in urls):
            raise RecipeError('Native toolchain archive destination changed')
        if any(token in block for token in ('sudo', 'common.zip', 'GITHUB_TOKEN', 'gh release', 'rm -rf /')):
            raise RecipeError('Unexpected side effect in a native toolchain group')
        block = re.sub(r'\s*&\s*$', '', block).strip()
        # Each pinned group is one AND-chain, and its failure is made explicit.
        rendered.append('(\n' + render(block, recipe, profile, values) + '\n) || exit 1')
    loops = re.findall(r'(?m)^for f in common/scripts/setlocalversion; do\n.*?^done\s*$', run, re.S)
    if len(loops) != 1 or any(token in loops[0] for token in ('curl ', 'wget ', 'rm ', 'git ')):
        raise RecipeError('Native local-version initialization changed')
    return Step('固定参考工具链与原生本地版本方法', 'cd "$GITHUB_WORKSPACE/kernel_workspace"\n'
                + '\n'.join(rendered) + '\n' + render(loops[0], recipe, profile, values))


def configure_steps(recipe: dict, profile: dict, values: dict[str, str]) -> list[Step]:
    output = []
    # Keep upstream ordering, not this tuple's feature grouping.
    for step in recipe['jobs']['build']['steps']:
        name = step.get('name')
        if name not in CONFIG_NAMES:
            continue
        condition = step.get('if', '')
        if condition != CONDITIONS[name]:
            raise RecipeError('Native feature condition changed: ' + name)
        if condition and values[condition.split('.', 1)[1]] != 'true':
            continue
        script = step['run']
        if name == '应用 lz4kd 补丁' and profile['kernel_version'] in ('6.1', '6.6'):
            # The exact dependency was already resolved and checked out by the
            # phase runner; never let the native fallback clone a moving head.
            script, count = re.subn(r'if \[ ! -d "SukiSU_patch" \]; then\n\s*git clone --depth=1 https://github\.com/ShirkNeko/SukiSU_patch\.git\nfi',
                                   'test -d SukiSU_patch/.git', script)
            if count != 1:
                raise RecipeError('Native LZ4KD dependency layout changed')
        if name == '启用内核级基带保护':
            original = 'curl -sSL https://github.com/cctv18/Baseband-guard/raw/master/setup.sh | bash'
            if script.count(original) != 1:
                raise RecipeError('Native BBG setup layout changed')
            script = script.replace(original, 'bash Baseband-guard/setup.sh "$OPLUS_BBG_REF"', 1)
        # A selected patch must stop on rejection instead of producing a
        # deceptively successful feature configuration.
        script = re.sub(r'(?m)^(\s*patch[^\n]*?)\s*\|\|\s*true\s*$', r'\1', script)
        output.append(Step(name, render(script, recipe, profile, values)))
    if not any(step.name == '添加 KSU & 其他配置项' for step in output):
        raise RecipeError('Native baseline configuration is missing')
    output.append(Step('记录实际构建配置', 'printf "\\nCONFIG_IKCONFIG=y\\nCONFIG_IKCONFIG_PROC=y\\n" '
                       '>> kernel_workspace/common/arch/arm64/configs/gki_defconfig'))
    return output


def build_step(recipe: dict, profile: dict, values: dict[str, str]) -> Step:
    source = select_step(recipe, '构建内核')['run']
    removed = re.sub(r'(?m)^\s*sudo rm -rf /(?:usr/share/dotnet|usr/local/lib/android|opt/ghc|opt/hostedtoolcache/CodeQL)\s*&\s*$', '', source)
    if re.search(r'(?m)^\s*(?:sudo\s+)?rm\s+[^\n]*\s/(?:usr|opt|var|home)(?:/|\s)', removed):
        raise RecipeError('Unexpected global filesystem removal in build method')
    if removed.count('export CCACHE_COMPILERCHECK="none"') != 1:
        raise RecipeError('Native compiler-cache identity setting changed')
    removed = removed.replace('export CCACHE_COMPILERCHECK="none"', 'export CCACHE_COMPILERCHECK="content"')
    if 'gki_defconfig' not in removed or not re.search(r'\bmake\s', removed):
        raise RecipeError('Native make entry changed')
    if profile['kernel_version'] == '6.12' and any(token not in removed for token in
            ('-fdebug-prefix-map=', '-fmacro-prefix-map=', '-ffile-prefix-map=',
             'export RUSTC=', 'export BINDGEN=', 'export LIBCLANG_PATH=', 'cc-wrapper', 'ld-wrapper')):
        raise RecipeError('Native 6.12 Rust/KMI build requirements changed')
    return Step('使用原生编译方法构建内核', render(removed, recipe, profile, values))


def plan_phase(phase: str, recipe: dict, profile: dict, inputs: dict) -> list[Step]:
    profile = validate_profile(profile)
    values = native_inputs(inputs, profile)
    if phase == 'bootstrap':
        return [toolchain_script(recipe, profile, values)]
    if phase == 'configure':
        return configure_steps(recipe, profile, values)
    if phase == 'build':
        return [build_step(recipe, profile, values)]
    raise RecipeError('Unsupported OPlus recipe phase')


def check_kernel(root: Path, profile: dict) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RecipeError('Official OPlus kernel must have its own source directory')
    if git(root, 'rev-parse', 'HEAD') != profile['kernel_ref']:
        raise RecipeError('Official OPlus kernel checkout changed from its fixed SHA')
    path = root / 'Makefile'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECIPE:
        raise RecipeError('Official OPlus Makefile is missing')
    text = path.read_text(encoding='utf-8')
    result = [re.findall(r'(?m)^\s*' + key + r'\s*=\s*([0-9]+)\s*$', text)
              for key in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL')]
    if any(len(values) != 1 for values in result) or '.'.join(values[0] for values in result) != profile['kernel']:
        raise RecipeError('Official OPlus source version differs from the reviewed target')


def clone_fixed(repository: str, reference: str, directory: Path) -> None:
    if (repository not in OFFICIAL_REPOSITORIES | {BBG_REPOSITORY, LZ4_REPOSITORY}
            or not isinstance(reference, str) or not SHA.fullmatch(reference)):
        raise RecipeError('Fixed source checkout requires a reviewed repository and full SHA')
    if directory.exists() or directory.is_symlink() or not directory.parent.is_dir():
        raise RecipeError('Fixed source checkout requires a fresh directory')
    directory.mkdir()
    git(directory, 'init', '--quiet')
    git(directory, 'remote', 'add', 'origin', 'https://github.com/' + repository + '.git')
    git(directory, 'fetch', '--quiet', '--depth=1', '--no-tags', '--no-recurse-submodules', 'origin', reference)
    git(directory, 'checkout', '--quiet', '--detach', reference)
    if git(directory, 'rev-parse', 'HEAD') != reference:
        raise RecipeError('Fixed source checkout returned a different commit')


def dependency(repository: str, destination: Path) -> str:
    if repository not in (BBG_REPOSITORY, LZ4_REPOSITORY):
        raise RecipeError('Unreviewed community feature dependency')
    reference = git(destination.parent, 'ls-remote', 'https://github.com/' + repository + '.git', 'HEAD').split()
    if len(reference) != 2 or not SHA.fullmatch(reference[0]) or reference[1] != 'HEAD':
        raise RecipeError('Community feature dependency returned an invalid fixed ref')
    clone_fixed(repository, reference[0], destination)
    return reference[0]


def run_steps(steps: list[Step], workspace: Path, extra: dict[str, str] | None = None) -> None:
    environment = {**os.environ, 'GITHUB_WORKSPACE': str(workspace), **(extra or {})}
    for step in steps:
        print('OPlus method: ' + step.name, flush=True)
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', step.script], cwd=workspace, env=environment)
        if result.returncode:
            raise RecipeError('OPlus method step failed: ' + step.name)


def execute_phase(phase: str, recipe_root: Path, profile: dict, inputs: dict, workspace: Path) -> None:
    profile = validate_profile(profile)
    if workspace.is_symlink() or not workspace.is_dir():
        raise RecipeError('OPlus workspace must already exist')
    workspace = workspace.resolve(strict=True)
    recipe = load_recipe(recipe_root, profile)
    steps = plan_phase(phase, recipe, profile, inputs)
    kernel_workspace = workspace / 'kernel_workspace'
    common = kernel_workspace / 'common'
    if kernel_workspace.is_symlink() or common.is_symlink():
        raise RecipeError('OPlus sources must stay in their independent workspace directories')
    if phase == 'bootstrap':
        if kernel_workspace.exists() or kernel_workspace.is_symlink():
            raise RecipeError('OPlus bootstrap requires a new kernel_workspace')
        kernel_workspace.mkdir()
        clone_fixed(profile['kernel_repo'], profile['kernel_ref'], common)
        check_kernel(common, profile)
        run_steps(steps, workspace)
        clang = SERIES[profile['kernel_version']][1]
        required = ['clang' + clang + '/bin/clang', 'clang' + clang + '/bin/ld.lld']
        if clang == '19':
            required += ['rust/bin/rustc']
        if (any(not (kernel_workspace / path).is_file() for path in required)
                or not (kernel_workspace / 'build-tools/bin').is_dir()):
            raise RecipeError('Native toolchain archives did not supply the required compiler tools')
        if clang == '19' and not any((kernel_workspace / path).is_file() for path in
                ('clang19/bin/bindgen', 'build-tools/bin/bindgen', 'rust/bin/bindgen')):
            raise RecipeError('Native 6.12 compiler archives did not supply bindgen on their PATH')
    else:
        check_kernel(common, profile)
        extra, records = {}, []
        if phase == 'configure':
            values = native_inputs(inputs, profile)
            if values['lz4kd_enable'] == 'true' and profile['kernel_version'] in ('6.1', '6.6'):
                ref = dependency(LZ4_REPOSITORY, kernel_workspace / 'SukiSU_patch')
                records.append({'repository': LZ4_REPOSITORY, 'ref': ref, 'feature': 'lz4kd'})
            if values['baseband_guard'] == 'true':
                ref = dependency(BBG_REPOSITORY, common / 'Baseband-guard')
                extra['OPLUS_BBG_REF'] = ref
                records.append({'repository': BBG_REPOSITORY, 'ref': ref, 'feature': 'baseband_guard'})
            (workspace / 'oplus-dependency-identities.json').write_text(json.dumps(records, sort_keys=True) + '\n', encoding='utf-8')
        run_steps(steps, workspace, extra)
        for item in records:
            path = common / 'Baseband-guard' if item['feature'] == 'baseband_guard' else kernel_workspace / 'SukiSU_patch'
            if git(path, 'rev-parse', 'HEAD') != item['ref']:
                raise RecipeError('Community feature setup changed its pinned source')
        check_kernel(common, profile)
        if phase == 'build' and not (common / 'out/arch/arm64/boot/Image').is_file():
            raise RecipeError('Native build did not produce an ARM64 Image')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('bootstrap', 'configure', 'build'), required=True)
    parser.add_argument('--recipe-root', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        execute_phase(args.phase, args.recipe_root, strict_json(args.profile), strict_json(args.inputs), args.workspace)
    except (OSError, ValueError, UnicodeError) as error:
        print('OPlus native recipe stopped: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
