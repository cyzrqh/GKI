"""Plan fixed OPlus methods without running downloads or kernel compilation."""
import copy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_recipe as adapter

class OPlusRecipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # These intentionally small, synthetic programs model the reviewed
        # layouts. Upstream source programs are not redistributed as fixtures.
        cls.references = (
            ('6.1', '141', '', 'sm8650'), ('6.6', '118', '', 'sm8750'),
            ('6.6', '89', '_mtk', 'sm8750'), ('6.12', '58', '', 'sm8850'),
            ('6.12', '58', '_mtk', 'sm8850'), ('6.12', '69', '_gki', 'sm8850'))

    def synthetic_recipe(self, series, sublevel, suffix, platform):
        android, clang = adapter.SERIES[series]
        version = {'18': '510928', '19': '536225', '20': '547379'}[clang]
        tools_url = ('https://github.com/cctv18/oneplus_sm8650_toolchain/releases/download/'
                     'LLVM-Clang' + clang + '-r' + version + '/')
        toolchain = ('sudo apt-get install ignored-package\n'
                     'wget https://example.invalid/moving-mirror/common.zip\n\n'
                     'mkdir -p clang' + clang + ' && cd clang' + clang
                     + ' && aria2c ' + tools_url + 'clang-r' + version
                     + '.zip -o clang.zip && unzip clang.zip && cd .. &\n\n')
        if clang == '19':
            toolchain += ('mkdir -p rust && cd rust && aria2c ' + tools_url
                          + 'rust.zip -o rust.zip && unzip rust.zip && cd .. &\n\n')
        toolchain += ('aria2c ' + tools_url + 'build-tools.zip -o build-tools.zip'
                      ' && unzip build-tools.zip &\n\n'
                      'for f in common/scripts/setlocalversion; do\n'
                      '    sed -i \'s/-dirty//g\' "$f"\ndone\n')
        lz4 = ('cd kernel_workspace\n'
               'if [ ! -d "SukiSU_patch" ]; then\n'
               '    git clone --depth=1 https://github.com/ShirkNeko/SukiSU_patch.git\nfi\n'
               'cd common\npatch -p1 < ../SukiSU_patch/test.patch || true\n')
        if clang == '19':
            lz4 = ('cd kernel_workspace/common\n'
                   'wget https://github.com/$GITHUB_REPOSITORY/raw/refs/heads/$GITHUB_REF_NAME/other_patch/lz4kd.patch\n'
                   'patch -p1 < lz4kd.patch || true\n')
        def config(name, script):
            step = {'name': name, 'run': script}
            if adapter.CONDITIONS[name]:
                step['if'] = adapter.CONDITIONS[name]
            return step
        steps = [
            {'name': '安装环境依赖+初始化源码仓库及llvm-Clang' + clang + '工具链', 'run': toolchain},
            {'name': '添加KernelSU', 'run': 'curl https://invalid/KernelSU/setup.sh | bash'},
            {'name': '应用 KernelSU & SUSFS 补丁', 'run': 'echo excluded-susfs-apply'},
            config('添加SUSFS 配置项', 'echo CONFIG_KSU_SUSFS=y >> kernel_workspace/common/arch/arm64/configs/gki_defconfig'),
            config('添加 KSU & 其他配置项', 'echo CONFIG_KSU=y >> kernel_workspace/common/arch/arm64/configs/gki_defconfig'),
            config('应用 lz4kd 补丁', lz4),
            config('启用网络功能增强优化配置', 'echo CONFIG_IP_SET=y >> kernel_workspace/common/arch/arm64/configs/gki_defconfig'),
            config('添加 BBR 等一系列拥塞控制算法', 'if [ "${{ inputs.bbr_enable }}" = "true" ]; then echo CONFIG_TCP_CONG_BBR=y; fi'),
            config('启用内核级基带保护', 'cd kernel_workspace/common\ncurl -sSL https://github.com/cctv18/Baseband-guard/raw/master/setup.sh | bash'),
            config('添加制作名称', 'echo "${{ env.KERNEL_NAME }}"'),
        ]
        if clang == '19':
            steps[4]['run'] += '\npatch -p1 -F 3 < cve-2026-43499-rtmutex-6.12.patch\n'
        build = ('sudo rm -rf /usr/share/dotnet &\n'
                 'export CCACHE_COMPILERCHECK="none"\n'
                 'wget https://github.com/$GITHUB_REPOSITORY/raw/refs/heads/$GITHUB_REF_NAME/wrapper/cc-wrapper\n'
                 'wget https://github.com/$GITHUB_REPOSITORY/raw/refs/heads/$GITHUB_REF_NAME/wrapper/ld-wrapper\n')
        if clang == '19':
            build += ('export RUSTC=rustc\nexport BINDGEN=bindgen\nexport LIBCLANG_PATH=clang19/lib\n'
                      'export KCFLAGS="-fdebug-prefix-map=/source= -fmacro-prefix-map=/source= -ffile-prefix-map=/source="\n')
        build += 'cd kernel_workspace/common\nmake O=out gki_defconfig\nmake O=out -j2\n'
        steps += [{'name': '构建内核', 'run': build},
                  {'name': '克隆 AnyKernel3 并打包', 'run': 'echo excluded-packaging'}]
        steps += [{'name': 'excluded-' + str(index), 'uses': 'ignored/action@fixed'} for index in range(8)]
        return {'env': {'ANDROID_VERSION': android, 'KERNEL_VERSION': series,
                        'SUB_VERSION': sublevel + suffix, 'KERNEL_NAME': 'vendor.native-release.1'},
                'jobs': {'build': {'steps': steps}}}

    def case(self, index=0, *, mtk=None):
        series, sublevel, suffix, platform = self.references[index]
        document = self.synthetic_recipe(series, sublevel, suffix, platform)
        raw = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()
        kernel = series + '.' + sublevel
        repository = 'cctv18/oppo_oplus_realme_' + platform
        if mtk is True or (mtk is None and suffix == '_mtk'):
            platform = 'mt6991' if series == '6.6' else 'mt6993'
        profile = {'id': platform + '-' + kernel, 'platform': platform, 'kernel': kernel,
                   'android_version': document['env']['ANDROID_VERSION'], 'kernel_version': series,
                   'sub_level': sublevel, 'kernel_repo': 'OnePlusOSS/android_kernel_common_oneplus_sm8850',
                   'kernel_branch': 'oneplus/reviewed', 'kernel_ref': 'a' * 40,
                   'recipe_repository': repository, 'recipe_ref': 'b' * 40,
                   'recipe_workflow': '.github/workflows/fastbuild_' + kernel + suffix + '.yml',
                   'recipe_sha256': hashlib.sha256(raw).hexdigest(),
                   'system': 'reviewed test system', 'compatibility': 'not a universal device guarantee',
                   'source_committed_at': '2026-10-27T00:00:00Z'}
        return document, raw, profile

    def inputs(self, **updates):
        return {'kernelsu_variant': 'SukiSU', 'kernelsu_branch': '最新版', 'enable_susfs': False, 'use_kpm': False,
                'use_lz4kd': False, 'use_bbg': False, 'use_bbr': False,
                'use_proxy_optimization': False, 'use_unicode_bypass': False, **updates}

    def test_both_managers_accept_only_the_two_shared_version_choices(self):
        _, _, profile = self.case(0)
        for variant in ('SukiSU', 'BakaSU'):
            stable = adapter.native_inputs(self.inputs(kernelsu_variant=variant, kernelsu_branch='正式版'), profile)
            latest = adapter.native_inputs(self.inputs(kernelsu_variant=variant, kernelsu_branch='最新版'), profile)
            self.assertEqual(stable, latest, 'The frozen parent source owns the tier; native feature flags stay the same')
        for tier in ('Stable(标准)', 'Dev(开发)', 'Custom(自定义)', '', None, True):
            with self.subTest(tier=tier), self.assertRaises(ValueError):
                adapter.native_inputs(self.inputs(kernelsu_branch=tier), profile)

    def test_single_target_is_validated_without_becoming_a_feature_flag(self):
        _, _, profile = self.case(0)
        expected = adapter.native_inputs(self.inputs(), profile)
        for target in (adapter.ALL_TARGETS, adapter.target_label(profile)):
            self.assertEqual(expected, adapter.native_inputs(self.inputs(build_target=target), profile))
        for target in ('天玑 · 6.1.57', '骁龙 · 6.1.999', '', None):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'build target'):
                adapter.native_inputs(self.inputs(build_target=target), profile)

    def refresh(self, document, profile):
        raw = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()
        profile = {**profile, 'recipe_sha256': hashlib.sha256(raw).hexdigest()}
        return adapter.parse_recipe(raw, profile), profile

    def test_three_kernel_generations_and_mtk_gki_render_both_managers(self):
        for index in range(len(self.references)):
            document, raw, profile = self.case(index)
            with self.subTest(reference=profile['recipe_workflow']):
                parsed = adapter.parse_recipe(raw, profile)
                for variant in ('SukiSU', 'BakaSU'):
                    for phase in ('bootstrap', 'configure', 'build'):
                        steps = adapter.plan_phase(phase, parsed, profile, self.inputs(
                            kernelsu_variant=variant, enable_susfs=True, use_kpm=True,
                            use_lz4kd=True, use_bbg=True, use_bbr=True, use_proxy_optimization=True))
                        self.assertTrue(steps)
                        self.assertFalse(any('${{' in step.script for step in steps))

    def test_bootstrap_excludes_mirror_apt_system_removal_and_cache_installation(self):
        for index in range(len(self.references)):
            document, _, profile = self.case(index)
            script = adapter.plan_phase('bootstrap', document, profile, self.inputs())[0].script
            for forbidden in ('apt-get', 'sudo ', '/usr/bin/ccache', 'common.zip', 'android_kernel_',
                              'gh release', 'gh cache', 'KernelSU', 'setup.sh'):
                self.assertNotIn(forbidden, script)
            self.assertNotIn('common/scripts/setlocalversion', script)
            self.assertIn('releases/download/LLVM-Clang', script)
            self.assertIn(') || exit 1', script)

    def test_native_build_preserves_the_workflow_cache_capacity(self):
        for index in range(len(self.references)):
            document, _, profile = self.case(index)
            build = adapter.select_step(document, '构建内核')
            build['run'] = 'export CCACHE_MAXSIZE="3G"\n' + build['run']
            script = adapter.plan_phase('build', document, profile, self.inputs())[0].script
            with self.subTest(reference=profile['recipe_workflow']):
                self.assertNotIn('export CCACHE_MAXSIZE="3G"', script)
                self.assertIn('export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-50G}"', script)
                self.assertIn('export CCACHE_COMPILERCHECK="content"', script)

    def test_feature_mapping_disables_hidden_options_and_mtk_proxy(self):
        _, _, profile = self.case(0)
        selected = self.inputs(use_lz4kd=True, use_bbg=True, use_bbr=True, use_proxy_optimization=True)
        values = adapter.native_inputs(selected, profile)
        self.assertEqual('true', values['lz4kd_enable'])
        self.assertEqual('true', values['better_net'])
        self.assertEqual('true', values['bbr_enable'])
        mtk = {**profile, 'platform': 'mt6989'}
        self.assertEqual('false', adapter.native_inputs(selected, mtk)['better_net'])
        for key in ('lz4_enable', 'adios_enable', 'ssg_enable', 'droidspaces_enable',
                    'rekernel_enable', 'ccache_update', 'ccache_debug'):
            self.assertEqual('false', values[key])

    def test_configure_never_runs_native_ksu_setup_susfs_apply_or_kpm(self):
        for index in range(len(self.references)):
            document, _, profile = self.case(index)
            steps = adapter.plan_phase('configure', document, profile, self.inputs(
                enable_susfs=True, use_kpm=True, use_bbg=True, use_lz4kd=True))
            names = {step.name for step in steps}
            self.assertNotIn('添加KernelSU', names)
            self.assertNotIn('应用 KernelSU & SUSFS 补丁', names)
            self.assertNotIn('应用KPM并修补内核', names)
            self.assertIn('添加SUSFS 配置项', names)
            text = '\n'.join(step.script for step in steps)
            self.assertNotIn('setup.sh | bash', text)
            self.assertNotIn('git clone --depth=1', text)
            self.assertNotRegex(text, r'(?m)^\s*patch[^\n]*\|\|\s*true')
            self.assertIn('CONFIG_IKCONFIG=y', text)
            self.assertIn('CONFIG_IKCONFIG_PROC=y', text)

    def test_own_downloads_are_pinned_without_rewriting_producer_repository(self):
        document, _, profile = self.case(0)
        source = ('wget https://github.com/$GITHUB_REPOSITORY/raw/refs/heads/$GITHUB_REF_NAME/lib/a.so\n'
                  'echo "$GITHUB_REPOSITORY"')
        result = adapter.render(source, document, profile, adapter.native_inputs(self.inputs(), profile))
        self.assertIn('/' + profile['recipe_ref'] + '/lib/a.so', result)
        self.assertIn('echo "$GITHUB_REPOSITORY"', result)
        self.assertNotIn('GITHUB_REPOSITORY=', result)

    def test_unknown_expressions_and_changed_conditions_stop_before_execution(self):
        document, _, profile = self.case(0)
        values = adapter.native_inputs(self.inputs(), profile)
        for expression in ('github.token', 'secrets.GITHUB_TOKEN', 'inputs.arbitrary', "format('unsafe')"):
            with self.subTest(expression=expression), self.assertRaises(adapter.RecipeError):
                adapter.render('echo "${{ ' + expression + ' }}"', document, profile, values)
        source = copy.deepcopy(document)
        adapter.select_step(source, '应用 lz4kd 补丁')['if'] = "inputs.lz4kd_enable || true"
        with self.assertRaises(adapter.RecipeError):
            adapter.plan_phase('configure', source, profile, self.inputs())

    def test_hash_target_variant_and_duplicate_native_steps_are_rejected(self):
        document, raw, profile = self.case(0)
        with self.assertRaises(adapter.RecipeError):
            adapter.parse_recipe(raw + b'\n', profile)
        with self.assertRaises(adapter.RecipeError):
            adapter.validate_profile({**profile, 'kernel_repo': 'cctv18/android_kernel_common_oneplus_sm8650'})
        with self.assertRaises(adapter.RecipeError):
            adapter.native_inputs(self.inputs(kernelsu_variant='Official'), profile)
        with self.assertRaises(adapter.RecipeError):
            adapter.native_inputs(self.inputs(use_bbg='yes'), profile)
        document['jobs']['build']['steps'].append(copy.deepcopy(adapter.select_step(document, '启用内核级基带保护')))
        with self.assertRaises(adapter.RecipeError):
            self.refresh(document, profile)

    def test_native_release_and_612_rust_symbol_version_flags_remain(self):
        for index in range(len(self.references)):
            document, _, profile = self.case(index)
            config = '\n'.join(step.script for step in adapter.plan_phase('configure', document, profile, self.inputs()))
            self.assertNotIn(document['env']['KERNEL_NAME'], config)
            self.assertNotIn('添加制作名称', [step.name for step in adapter.plan_phase(
                'configure', document, profile, self.inputs())])
            script = adapter.plan_phase('build', document, profile, self.inputs())[0].script
            self.assertIn('CCACHE_COMPILERCHECK="content"', script)
            self.assertNotIn('CCACHE_COMPILERCHECK="none"', script)
            self.assertNotIn('sudo rm -rf', script)
            self.assertIn('cc-wrapper', script)
            self.assertIn('ld-wrapper', script)
            if profile['kernel_version'] == '6.12':
                self.assertIn('oplus_compat.py" rtmutex --common .', config)
                self.assertNotIn('patch -p1 -F 3 < cve-2026-43499-rtmutex-6.12.patch', config)
                for flag in ('-fdebug-prefix-map=', '-fmacro-prefix-map=', '-ffile-prefix-map=',
                             'export RUSTC=', 'export BINDGEN=', 'export LIBCLANG_PATH='):
                    self.assertIn(flag, script)

    def test_fixed_checkout_fetches_only_the_selected_immutable_commit(self):
        directory = ROOT / 'tests' / ('.oplus-recipe-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        output = directory / 'source'
        commands = []
        def fake_git(path, *args):
            commands.append(args)
            return 'a' * 40 if args == ('rev-parse', 'HEAD') else ''
        with mock.patch.object(adapter, 'git', side_effect=fake_git):
            adapter.clone_fixed('OnePlusOSS/android_kernel_common_oneplus_sm8850', 'a' * 40, output)
        self.assertIn(('fetch', '--quiet', '--depth=1', '--no-tags', '--no-recurse-submodules', 'origin', 'a' * 40), commands)
        self.assertIn(('checkout', '--quiet', '--detach', 'a' * 40), commands)
        with mock.patch.object(adapter, 'git', side_effect=AssertionError('must refuse before Git')):
            with self.assertRaises(adapter.RecipeError):
                adapter.clone_fixed('OnePlusOSS/android_kernel_common_oneplus_sm8850', 'a' * 40, output)

    def test_lz4_dependency_is_the_single_reviewed_patch_for_the_complete_matrix(self):
        destination = Path('unused-pinned-lz4-fixture')
        with (mock.patch.object(adapter, 'git', side_effect=AssertionError('moving HEAD must not be resolved')),
              mock.patch.object(adapter, 'clone_fixed') as clone):
            reference = adapter.dependency(adapter.LZ4_REPOSITORY, destination)
        self.assertEqual(reference, '1aaf4283e03b0a2861bf8db1e58624028db23a85')
        clone.assert_called_once_with(adapter.LZ4_REPOSITORY, reference, destination)

    def test_official_kernel_head_and_makefile_are_rechecked(self):
        document, _, profile = self.case(0)
        directory = ROOT / 'tests' / ('.oplus-kernel-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        major, minor, sublevel = profile['kernel'].split('.')
        (directory / 'Makefile').write_text(f'VERSION = {major}\nPATCHLEVEL = {minor}\nSUBLEVEL = {sublevel}\n')
        with mock.patch.object(adapter, 'git', return_value=profile['kernel_ref']):
            adapter.check_kernel(directory, profile)
            (directory / 'Makefile').write_text('VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 999\n')
            with self.assertRaises(adapter.RecipeError):
                adapter.check_kernel(directory, profile)
        with mock.patch.object(adapter, 'git', return_value='f' * 40):
            with self.assertRaises(adapter.RecipeError):
                adapter.check_kernel(directory, profile)

    def test_unknown_global_removal_or_remote_setup_is_not_executable(self):
        document, _, profile = self.case(0)
        build = adapter.select_step(document, '构建内核')
        build['run'] += '\nsudo rm -rf /var/lib/custom\n'
        with self.assertRaises(adapter.RecipeError):
            adapter.plan_phase('build', document, profile, self.inputs())
        with self.assertRaises(adapter.RecipeError):
            adapter.render('curl https://host.invalid/KernelSU/setup.sh | bash', document, profile,
                           adapter.native_inputs(self.inputs(), profile))

    def temporary_workspace(self):
        directory = ROOT / 'tests' / ('.oplus-execution-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        return directory

    def test_bootstrap_checks_method_before_mutation_and_clones_official_commit(self):
        document, _, profile = self.case(0)
        workspace = self.temporary_workspace()
        with mock.patch.object(adapter, 'load_recipe', side_effect=adapter.RecipeError('hash changed')):
            with self.assertRaises(adapter.RecipeError):
                adapter.execute_phase('bootstrap', workspace, profile, self.inputs(), workspace)
        self.assertFalse((workspace / 'kernel_workspace').exists())
        clones = []
        def source(repository, reference, destination):
            clones.append((repository, reference))
            destination.mkdir()
            (destination / 'Makefile').write_text('VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 141\n')
        def tools(steps, directory):
            for relative in ('clang20/bin/clang', 'clang20/bin/ld.lld', 'build-tools/bin/placeholder'):
                path = directory / 'kernel_workspace' / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fake compiler, never executed')
        with mock.patch.object(adapter, 'load_recipe', return_value=document), \
                mock.patch.object(adapter, 'clone_fixed', side_effect=source), \
                mock.patch.object(adapter, 'git', return_value=profile['kernel_ref']), \
                mock.patch.object(adapter, 'run_steps', side_effect=tools):
            adapter.execute_phase('bootstrap', workspace, profile, self.inputs(), workspace)
        self.assertEqual([(profile['kernel_repo'], profile['kernel_ref'])], clones)

    def test_configure_records_exact_dependencies_and_rechecks_their_identity(self):
        document, _, profile = self.case(0)
        workspace = self.temporary_workspace()
        common = workspace / 'kernel_workspace/common'
        common.mkdir(parents=True)
        calls = []
        def feature(repository, destination):
            calls.append((repository, destination))
            return 'c' * 40
        with mock.patch.object(adapter, 'load_recipe', return_value=document), \
                mock.patch.object(adapter, 'check_kernel') as kernel_check, \
                mock.patch.object(adapter, 'dependency', side_effect=feature), \
                mock.patch.object(adapter, 'git', return_value='c' * 40), \
                mock.patch.object(adapter, 'run_steps') as shell:
            adapter.execute_phase('configure', workspace, profile,
                                  self.inputs(use_bbg=True, use_lz4kd=True), workspace)
        self.assertEqual(2, kernel_check.call_count)
        self.assertEqual({adapter.LZ4_REPOSITORY, adapter.BBG_REPOSITORY}, {item[0] for item in calls})
        records = json.loads((workspace / 'oplus-dependency-identities.json').read_text())
        self.assertEqual({'lz4kd', 'baseband_guard'}, {item['feature'] for item in records})
        self.assertTrue(all(item['ref'] == 'c' * 40 for item in records))
        self.assertEqual({'OPLUS_BBG_REF': 'c' * 40}, shell.call_args.args[2])
        with mock.patch.object(adapter, 'load_recipe', return_value=document), \
                mock.patch.object(adapter, 'check_kernel'), \
                mock.patch.object(adapter, 'dependency', return_value='c' * 40), \
                mock.patch.object(adapter, 'git', return_value='d' * 40), \
                mock.patch.object(adapter, 'run_steps'), self.assertRaises(adapter.RecipeError):
            adapter.execute_phase('configure', workspace, profile, self.inputs(use_bbg=True), workspace)

    def test_61_build_adapts_the_real_security_fix_before_invoking_make(self):
        from test_oplus_rtmutex_compat import SCOPED_FUNCTION, PROXY_FUNCTION
        document, _, profile = self.case(0)
        self.assertEqual(profile['kernel_version'], '6.1')
        workspace = self.temporary_workspace()
        common = workspace / 'kernel_workspace/common'
        locking = common / 'kernel/locking'
        locking.mkdir(parents=True)
        (common / 'Makefile').write_text('VERSION = 6\nPATCHLEVEL = 1\n', encoding='utf-8', newline='\n')
        source = locking / 'rtmutex.c'
        source.write_text(SCOPED_FUNCTION, encoding='utf-8', newline='\n')
        (locking / 'rtmutex_api.c').write_text(PROXY_FUNCTION, encoding='utf-8', newline='\n')
        native = '6.1.57-android14-11'
        def compiled(*_args):
            text = source.read_text(encoding='utf-8')
            self.assertNotIn('scoped_guard', text)
            self.assertIn('if (!waiter_task) /* never enqueued */', text)
            self.assertIn('raw_spin_unlock(&waiter_task->pi_lock);', text)
            output = common / 'out/arch/arm64/boot/Image'
            output.parent.mkdir(parents=True)
            output.write_bytes(b'not an installable kernel; execution boundary fixture')
            release = common / 'out/include/config/kernel.release'
            release.parent.mkdir(parents=True)
            release.write_text(native, encoding='utf-8')
        with mock.patch.object(adapter, 'load_recipe', return_value=document), \
                mock.patch.object(adapter, 'check_kernel'), \
                mock.patch('oplus_compat.prepare_native_release', return_value=native), \
                mock.patch.object(adapter, 'run_steps', side_effect=compiled) as run:
            adapter.execute_phase('build', workspace, profile, self.inputs(), workspace)
        run.assert_called_once()


class OPlusGitTransportTests(unittest.TestCase):
    def fetch_args(self):
        return ('fetch', '--quiet', '--depth=1', '--no-tags', '--no-recurse-submodules',
                'origin', 'a' * 40)

    def success(self, stdout=''):
        return subprocess.CompletedProcess(['git'], 0, stdout, '')

    def test_large_pinned_fetch_recovers_from_timeout_with_identical_args(self):
        # The failed jobs stopped at 240s before reaching toolchain setup.
        fetched = []
        def process(command, environment, timeout):
            operation = command[command.index('http.extraHeader=') + 1]
            directory = Path(command[command.index('-C') + 1])
            if operation == 'init':
                (directory / '.git').mkdir()
            if operation == 'fetch':
                self.assertEqual(900, timeout)
                self.assertEqual(list(self.fetch_args()), command[-len(self.fetch_args()):])
                self.assertFalse((directory / '.git/shallow.lock').exists())
                fetched.append(directory)
                if len(fetched) == 1:
                    (directory / '.git/shallow.lock').write_text('left by stopped fetch')
                    raise subprocess.TimeoutExpired('git', 900, stderr='private diagnostic must stay hidden')
            return self.success('a' * 40 if operation == 'rev-parse' else '')
        output = io.StringIO()
        with (tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temporary,
              mock.patch.object(adapter, 'run_git_process', side_effect=process),
              mock.patch.object(adapter, 'OWNED_PROCESS_GROUPS', True),
              mock.patch.object(adapter.time, 'sleep') as pause, contextlib.redirect_stderr(output)):
            destination = Path(temporary) / 'source'
            adapter.clone_fixed('OnePlusOSS/android_kernel_common_oneplus_sm8850', 'a' * 40, destination)
            self.assertTrue((destination / '.git').is_dir())
            self.assertFalse((destination / '.git/shallow.lock').exists())
            self.assertEqual([destination], list(Path(temporary).iterdir()))
            self.assertEqual(2, len(fetched))
            self.assertNotEqual(*fetched)
            self.assertTrue(all(not path.exists() for path in fetched))
        pause.assert_called_once_with(5)
        self.assertIn('retrying the same commit in a fresh checkout', output.getvalue())
        self.assertNotIn('private diagnostic', output.getvalue())

    def test_fetch_timeouts_stop_after_the_bounded_attempt_budget(self):
        fetched = []
        def process(command, environment, timeout):
            if 'fetch' in command:
                fetched.append(command)
                raise subprocess.TimeoutExpired('git', 900)
            return self.success()
        with (tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temporary,
              mock.patch.object(adapter, 'run_git_process', side_effect=process),
              mock.patch.object(adapter, 'OWNED_PROCESS_GROUPS', True),
              mock.patch.object(adapter.time, 'sleep') as pause,
              contextlib.redirect_stderr(io.StringIO()),
              self.assertRaisesRegex(adapter.RecipeError, r'fetch stopped \(attempt 2/2\).*timed out after 900s')):
            adapter.clone_fixed('OnePlusOSS/android_kernel_common_oneplus_sm8850', 'a' * 40,
                                Path(temporary) / 'source')
        self.assertEqual(2, len(fetched))
        self.assertEqual([mock.call(5)], pause.call_args_list)

    def test_transient_network_failures_retry_without_exposing_remote_diagnostics(self):
        for diagnostic in ('RPC failed; curl 56 Recv failure: Connection reset by peer',
                           'fatal: early EOF', 'The requested URL returned error: 503',
                           'Could not resolve host: github.com'):
            result = subprocess.CompletedProcess(['git'], 128, '', diagnostic + ' private-header')
            output = io.StringIO()
            with (self.subTest(diagnostic=diagnostic),
                  mock.patch.object(adapter, 'run_git_process', side_effect=[result, self.success()]) as run,
                  mock.patch.object(adapter.time, 'sleep'), contextlib.redirect_stderr(output)):
                self.assertEqual('', adapter.git(Path('fixed-source'), 'ls-remote', 'https://example.invalid', 'HEAD'))
                self.assertEqual(2, run.call_count)
                self.assertNotIn('private-header', output.getvalue())

    def test_permanent_fetch_errors_do_not_retry_or_switch_the_fixed_commit(self):
        for diagnostic in ("fatal: couldn't find remote ref " + 'a' * 40,
                           'fatal: Authentication failed', 'fatal: Permission denied',
                           'fatal: write error: No space left on device',
                           'fatal: protocol error: bad pack header\nfatal: early EOF',
                           'fatal: bad object\nRPC failed; curl 56',
                           'fatal: pack checksum mismatch\nfatal: early EOF',
                           'fatal: repository not found'):
            result = subprocess.CompletedProcess(['git'], 128, '', diagnostic)
            with (self.subTest(diagnostic=diagnostic),
                  mock.patch.object(adapter, 'run_git_process', return_value=result) as run,
                  mock.patch.object(adapter.time, 'sleep') as pause,
                  self.assertRaisesRegex(adapter.RecipeError, r'Git fetch failed \(exit 128\)')):
                adapter.git(Path('fixed-source'), *self.fetch_args())
            run.assert_called_once()
            pause.assert_not_called()

    def test_local_git_timeout_and_missing_executable_are_not_retried(self):
        for failure in (subprocess.TimeoutExpired('git', 240), FileNotFoundError('git unavailable')):
            with (self.subTest(failure=type(failure).__name__),
                  mock.patch.object(adapter, 'run_git_process', side_effect=failure) as run,
                  mock.patch.object(adapter.time, 'sleep') as pause,
                  self.assertRaises(adapter.RecipeError)):
                adapter.git(Path('fixed-source'), 'rev-parse', 'HEAD')
            run.assert_called_once()
            self.assertEqual(240, run.call_args.args[2])
            pause.assert_not_called()

    def test_resolving_feature_head_keeps_the_shorter_timeout_and_same_query(self):
        args = ('ls-remote', 'https://github.com/' + adapter.BBG_REPOSITORY + '.git', 'HEAD')
        with (mock.patch.object(adapter, 'run_git_process', side_effect=[
                subprocess.TimeoutExpired('git', 240), self.success('a' * 40 + '\tHEAD\n')]) as run,
              mock.patch.object(adapter, 'OWNED_PROCESS_GROUPS', True),
              mock.patch.object(adapter.time, 'sleep'), contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual('a' * 40 + '\tHEAD', adapter.git(Path('fixed-source'), *args))
        self.assertEqual(run.call_args_list[0], run.call_args_list[1])
        self.assertEqual(240, run.call_args.args[2])

    def test_linux_timeout_terminates_owned_group_and_reaps_before_retry(self):
        for child_holds_pipes in (False, True):
            events = []
            process = mock.MagicMock()
            process.pid = 12345
            responses = [subprocess.TimeoutExpired('git', 900),
                         subprocess.TimeoutExpired('git', 5) if child_holds_pipes else ('', ''), ('', '')]
            def communicate(timeout=None):
                events.append(('communicate', timeout))
                response = responses.pop(0)
                if isinstance(response, BaseException):
                    raise response
                return response
            process.communicate.side_effect = communicate
            manager = mock.MagicMock()
            manager.__enter__.return_value = process
            def terminate(pid, sig):
                events.append(('killpg', pid, sig))
            with (self.subTest(child_holds_pipes=child_holds_pipes),
                  mock.patch.object(adapter, 'OWNED_PROCESS_GROUPS', True),
                  mock.patch.object(adapter.subprocess, 'Popen', return_value=manager) as popen,
                  mock.patch.object(adapter.os, 'killpg', side_effect=terminate, create=True),
                  self.assertRaises(subprocess.TimeoutExpired)):
                adapter.run_git_process(['git', 'fetch'], {}, 900)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertEqual([('communicate', 900), ('killpg', 12345, adapter.signal.SIGTERM),
                              ('communicate', 5), ('killpg', 12345, getattr(adapter.signal, 'SIGKILL', 9)),
                              ('communicate', None)], events)

    def test_windows_fetch_timeout_is_not_retried_and_cleans_only_its_stage(self):
        with (tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temporary,
              mock.patch.object(adapter, 'OWNED_PROCESS_GROUPS', False),
              mock.patch.object(adapter, 'run_git_process', side_effect=[
                  self.success(), self.success(), subprocess.TimeoutExpired('git', 900)]) as run,
              mock.patch.object(adapter.time, 'sleep') as pause):
            with self.assertRaisesRegex(adapter.RecipeError, r'fetch stopped \(attempt 1/2\)'):
                adapter.clone_fixed('OnePlusOSS/android_kernel_common_oneplus_sm8850', 'a' * 40,
                                    Path(temporary) / 'source')
            self.assertEqual([], list(Path(temporary).iterdir()))
        self.assertEqual(3, run.call_count)
        pause.assert_not_called()

    def test_checkout_or_head_validation_failure_never_retries_or_publishes(self):
        for failure in ('checkout', 'wrong-head'):
            calls = []
            def process(command, environment, timeout):
                operation = command[command.index('http.extraHeader=') + 1]
                calls.append(operation)
                if operation == 'checkout' and failure == 'checkout':
                    return subprocess.CompletedProcess(command, 128, '', 'fatal: bad object')
                return self.success('b' * 40 if operation == 'rev-parse' else '')
            with (self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temporary,
                  mock.patch.object(adapter, 'run_git_process', side_effect=process),
                  mock.patch.object(adapter.time, 'sleep') as pause):
                with self.assertRaises(adapter.RecipeError):
                    adapter.clone_fixed('OnePlusOSS/android_kernel_common_oneplus_sm8850', 'a' * 40,
                                        Path(temporary) / 'source')
                self.assertEqual([], list(Path(temporary).iterdir()))
            self.assertEqual(1, calls.count('fetch'))
            pause.assert_not_called()

    def test_existing_or_concurrently_created_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temporary:
            destination = Path(temporary) / 'source'
            def process(command, environment, timeout):
                if 'rev-parse' in command:
                    destination.mkdir()
                    (destination / 'keep.txt').write_text('caller owned')
                    return self.success('a' * 40)
                return self.success()
            with (mock.patch.object(adapter, 'run_git_process', side_effect=process),
                  self.assertRaisesRegex(adapter.RecipeError, 'created during checkout')):
                adapter.clone_fixed('OnePlusOSS/android_kernel_common_oneplus_sm8850', 'a' * 40, destination)
            self.assertEqual('caller owned', (destination / 'keep.txt').read_text())
            self.assertEqual([destination], list(Path(temporary).iterdir()))
            with (mock.patch.object(adapter, 'run_git_process') as run,
                  self.assertRaisesRegex(adapter.RecipeError, 'fresh directory')):
                adapter.clone_fixed('OnePlusOSS/android_kernel_common_oneplus_sm8850', 'a' * 40, destination)
            run.assert_not_called()

    def test_real_local_git_checkout_keeps_identity_after_staging_is_moved(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temporary:
            source = Path(temporary) / 'upstream'
            source.mkdir()
            adapter.git(source, 'init', '--quiet')
            (source / 'Makefile').write_text('VERSION = 6\nPATCHLEVEL = 12\nSUBLEVEL = 38\n')
            adapter.git(source, 'add', 'Makefile')
            adapter.git(source, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '--quiet', '-m', 'Local checkout fixture')
            reference = adapter.git(source, 'rev-parse', 'HEAD')
            destination = Path(temporary) / 'source'
            original = adapter.run_git_process
            def local_transport(command, environment, timeout):
                if command[-4:-1] == ['remote', 'add', 'origin']:
                    command = [*command[:-1], source.as_uri()]
                return original(command, environment, timeout)
            with mock.patch.object(adapter, 'run_git_process', side_effect=local_transport):
                adapter.clone_fixed('OnePlusOSS/android_kernel_common_oneplus_sm8850', reference, destination)
            self.assertEqual(reference, adapter.git(destination, 'rev-parse', 'HEAD'))
            self.assertEqual((source / 'Makefile').read_bytes(), (destination / 'Makefile').read_bytes())
            self.assertEqual({source, destination}, set(Path(temporary).iterdir()))

    @unittest.skipUnless(adapter.OWNED_PROCESS_GROUPS and sys.platform.startswith('linux'),
                         'Actual POSIX descendant termination requires a Linux host')
    def test_real_linux_timeout_stops_a_descendant_that_ignores_sigterm(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temporary:
            marker = Path(temporary) / 'child.pid'
            program = ('import os,signal,time,sys\n'
                       'if os.fork() == 0:\n'
                       ' signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                       ' open(sys.argv[1], "w").write(str(os.getpid()))\n'
                       ' time.sleep(60)\n'
                       'else:\n'
                       ' time.sleep(60)\n')
            with self.assertRaises(subprocess.TimeoutExpired):
                adapter.run_git_process([sys.executable, '-c', program, str(marker)], dict(adapter.os.environ), 0.5)
            self.assertTrue(marker.is_file())
            child = int(marker.read_text())
            state = Path('/proc') / str(child) / 'stat'
            for _ in range(100):
                if not state.exists() or state.read_text().split()[2] == 'Z':
                    break
                adapter.time.sleep(0.01)
            else:
                self.fail('Timed-out Git descendant is still running')


if __name__ == '__main__':
    unittest.main()
