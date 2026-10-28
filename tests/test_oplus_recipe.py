"""Plan fixed OPlus methods without running downloads or kernel compilation."""
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
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


if __name__ == '__main__':
    unittest.main()
