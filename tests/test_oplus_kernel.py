"""Check OKI integration boundaries without network or kernel compilation."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_kernel as kernel


class OPlusKernelTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ROOT / 'tests' / ('.oplus-kernel-integration-' + uuid.uuid4().hex)
        self.workspace.mkdir()
        self.addCleanup(shutil.rmtree, self.workspace)
        self.root = self.workspace / 'kernel_workspace'
        self.config = self.root / 'common/arch/arm64/configs/gki_defconfig'
        self.config.parent.mkdir(parents=True)
        self.config.write_text('CONFIG_VENDOR_FEATURE=y\nCONFIG_HEADERS_INSTALL=n\nCONFIG_KPM=y\n')
        self.ksu = self.root / 'KernelSU/kernel'
        self.ksu.mkdir(parents=True)
        (self.root / 'KernelSU/.git').mkdir()
        (self.ksu / 'Kconfig').write_text('config KSU\nconfig KPM\nconfig KSU_KPROBES_HOOK\nconfig KSU_TRACEPOINT_HOOK\n')
        self.reference = 'a' * 40
        self.profile = {
            'id': 'sm8650-6.1.141', 'platform': 'sm8650', 'kernel': '6.1.141',
            'android_version': 'android14', 'kernel_version': '6.1', 'sub_level': '141',
            'kernel_repo': 'OnePlusOSS/android_kernel_common_oneplus_sm8650',
            'kernel_branch': 'official/test', 'kernel_ref': 'b' * 40,
            'recipe_repository': 'cctv18/oppo_oplus_realme_sm8650', 'recipe_ref': 'c' * 40,
            'recipe_workflow': '.github/workflows/fastbuild_6.1.141.yml',
            'recipe_sha256': 'd' * 64, 'system': 'test system', 'compatibility': 'test only'}
        self.inputs = {'kernelsu_variant': 'SukiSU', 'kernelsu_branch': '最新版', 'enable_susfs': False, 'use_kpm': False,
                       'use_lz4kd': False, 'use_bbg': False, 'use_bbr': False,
                       'use_proxy_optimization': False, 'use_unicode_bypass': False}

    def environment(self, variant='SukiSU', **updates):
        return {'MANAGER_PACKAGE': kernel.manager_profile(variant)['package'],
                'MANAGER_CERT_SIZE': '710', 'MANAGER_CERT_SHA256': 'e' * 64,
                'MANAGER_VERSION_CODE': '41075', **updates}

    def finalize_mocks(self, variant='SukiSU', **environment):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, self.environment(variant, **environment), clear=True))
        stack.enter_context(mock.patch.object(kernel, 'checked_root', return_value=self.root))
        stack.enter_context(mock.patch.object(kernel, 'command', return_value=self.reference))
        stack.enter_context(mock.patch.object(kernel, 'source_git', side_effect=lambda _path, *args:
            str(self.root / 'KernelSU') if args == ('rev-parse', '--show-toplevel') else self.reference))
        signature = stack.enter_context(mock.patch.object(kernel, 'configure'))
        stack.enter_context(mock.patch('oplus_compat.prepare_vendor_dependencies', create=True))
        stack.enter_context(mock.patch('oplus_vendor_sources.restore_vendor_sources', return_value={}))
        stack.enter_context(mock.patch('oplus_vendor_locking.restore_vendor_locking', return_value={}))
        stack.enter_context(mock.patch('oplus_mtk_cpu.restore_mtk_cpu', return_value={}))
        stack.enter_context(mock.patch('oplus_ksu_compat.prepare_builtin_includes'))
        stack.enter_context(mock.patch('oplus_ksu_compat.prepare_kpm_members'))
        stack.enter_context(mock.patch('ensure_bakasu_symbol_lookup.validate_native_symbol_lookup', return_value=True))
        return signature

    def finalize(self, inputs=None, variant='SukiSU', patches=None, reference=None):
        return kernel.finalize_config(self.workspace, self.profile,
                                      inputs or {**self.inputs, 'kernelsu_variant': variant},
                                      reference or self.reference, variant, patches)

    def test_profile_inputs_and_mutable_manager_refs_reject_before_checkout(self):
        for profile, inputs, reference in (
                ({**self.profile, 'kernel_repo': 'cctv18/mirror'}, self.inputs, self.reference),
                (self.profile, {**self.inputs, 'kernelsu_variant': 'Official'}, self.reference),
                (self.profile, {**self.inputs, 'enable_susfs': 'yes'}, self.reference),
                (self.profile, self.inputs, 'main')):
            with self.subTest(profile=profile['kernel_repo'], reference=reference), \
                    mock.patch.object(kernel, 'checked_root') as checked, \
                    mock.patch.object(kernel, 'checkout_snapshot') as checkout, self.assertRaises(ValueError):
                kernel.integrate_manager(self.workspace, profile, inputs, self.workspace,
                                         reference, 'SukiSU', '{}')
            checked.assert_not_called()
            checkout.assert_not_called()

    def test_package_must_match_current_official_application_id(self):
        for variant, wrong in (('SukiSU', 'me.weishu.kernelsu'), ('BakaSU', 'org.bakasu.bakasu')):
            with self.subTest(variant=variant):
                configure = self.finalize_mocks(variant, MANAGER_PACKAGE=wrong)
                before = self.config.read_bytes()
                with self.assertRaisesRegex(ValueError, 'official application ID'):
                    self.finalize(variant=variant)
                configure.assert_not_called()
                self.assertEqual(before, self.config.read_bytes())

    def test_verified_signature_version_and_official_package_are_passed_together(self):
        for variant in ('SukiSU', 'BakaSU'):
            with self.subTest(variant=variant):
                configure = self.finalize_mocks(variant)
                self.finalize(variant=variant)
                configure.assert_called_once_with(self.root, '710', 'e' * 64,
                    kernel.manager_profile(variant)['package'], '41075')

    def test_config_overrides_remove_all_duplicates_and_keep_vendor_settings(self):
        self.config.write_text('CONFIG_VENDOR_FEATURE=y\nCONFIG_HEADERS_INSTALL=n\n'
                               '# CONFIG_HEADERS_INSTALL is not set\nCONFIG_HEADERS_INSTALL=m\n')
        kernel.config_values(self.config, {'CONFIG_HEADERS_INSTALL': 'y', 'CONFIG_KPM': 'n'})
        result = self.config.read_text()
        self.assertEqual(['CONFIG_HEADERS_INSTALL=y'],
                         [line for line in result.splitlines() if 'CONFIG_HEADERS_INSTALL' in line])
        self.assertIn('CONFIG_VENDOR_FEATURE=y\n', result)
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            kernel.config_values(self.config, {'CONFIG_HEADERS_INSTALL': 'y', 'CONFIG_BAD': '$(shell unsafe)'})
        self.assertEqual(before, self.config.read_bytes())

    def test_finalize_overrides_disabled_headers_and_selects_real_native_hook(self):
        self.finalize_mocks()
        self.finalize()
        text = self.config.read_text()
        for key in ('CONFIG_KSU', 'CONFIG_IKCONFIG', 'CONFIG_IKCONFIG_PROC',
                    'CONFIG_KPROBES', 'CONFIG_HEADERS_INSTALL', 'CONFIG_KSU_KPROBES_HOOK'):
            self.assertEqual([key + '=y'], re.findall(r'^' + key + '=.*$', text, re.M))
        for key in ('CONFIG_KPM', 'CONFIG_KSU_SUSFS'):
            self.assertEqual([key + '=n'], re.findall(r'^' + key + '=.*$', text, re.M))

    def test_vendor_dependencies_are_checked_after_configuration_before_hook_validation(self):
        self.finalize_mocks()
        with mock.patch('oplus_vendor_sources.restore_vendor_sources') as restore, \
                mock.patch('oplus_vendor_locking.restore_vendor_locking') as locking, \
                mock.patch('oplus_mtk_cpu.restore_mtk_cpu') as cpu, \
                mock.patch('oplus_compat.prepare_vendor_dependencies', create=True) as prepare, \
                mock.patch.object(kernel, 'validate_hooks', return_value=SimpleNamespace(calls=1, ok=True)) as hooks:
            order = []
            provenance = {'schema_version': 1, 'files': {'native.c': {'sha256': 'f' * 64}}}
            locking_provenance = {'schema_version': 1, 'files': {'locking.c': {'sha256': 'e' * 64}}}
            restore.side_effect = lambda common, profile: (order.append(('restore', common)) or provenance)
            locking.side_effect = lambda common, profile: (order.append(('locking', common)) or locking_provenance)
            cpu.side_effect = lambda common, profile: (order.append(('cpu', common)) or {})
            prepare.side_effect = lambda common: order.append(('vendor', common))
            hooks.side_effect = lambda *args, **kwargs: (order.append(('hooks', None)) or SimpleNamespace(calls=1, ok=True))
            self.finalize(inputs={**self.inputs, 'enable_susfs': True})
            self.assertEqual([('restore', self.root / 'common'), ('locking', self.root / 'common'),
                              ('cpu', self.root / 'common'), ('vendor', self.root / 'common'), ('hooks', None)], order)
            self.assertEqual(provenance, json.loads((self.root / 'common/out/gki-oplus-vendor-sources.json').read_text()))
            self.assertEqual(locking_provenance, json.loads((self.root / 'common/out/gki-oplus-vendor-locking.json').read_text()))
            self.assertFalse((self.root / 'common/out/gki-oplus-mtk-cpu.json').exists())
            prepare.assert_called_once_with(self.root / 'common')

    def test_kpm_native_bridge_follows_the_same_menu_policy_as_the_external_payload(self):
        for variant in ('SukiSU', 'BakaSU'):
            for tier in ('正式版', '最新版'):
                for requested in (True, False):
                    with self.subTest(variant=variant, tier=tier, requested=requested):
                        self.finalize_mocks(variant)
                        self.finalize(inputs={**self.inputs, 'kernelsu_variant': variant,
                                              'kernelsu_branch': tier, 'use_kpm': requested}, variant=variant)
                        expected = 'y' if requested else 'n'
                        self.assertEqual(['CONFIG_KPM=' + expected],
                                         re.findall(r'^CONFIG_KPM=.*$', self.config.read_text(), re.M))

    def test_susfs_66_restores_the_real_filemap_helper_before_hook_validation(self):
        self.profile.update(id='sm8750-6.6.30', platform='sm8750', kernel='6.6.30',
                            kernel_version='6.6', sub_level='30', android_version='android15',
                            kernel_repo='OnePlusOSS/android_kernel_common_oneplus_sm8750',
                            recipe_repository='cctv18/oppo_oplus_realme_sm8750',
                            recipe_workflow='.github/workflows/fastbuild_6.6.30.yml')
        common = self.root / 'common'
        fixture = ROOT / 'tests/fixtures/susfs-map-compat'
        for relative in ('fs/proc/task_mmu.c', 'include/linux/page_size_compat.h', 'mm/page_size_compat.c'):
            path = common / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fixture / ('2024-07-' + relative.replace('/', '_')), path)
        subprocess.run(['git', 'init', '--quiet', str(common)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(common), '-c', 'core.autocrlf=false', 'apply',
                        str(fixture / 'susfs23-task_mmu.patch')], check=True, capture_output=True)
        task = (common / 'fs/proc/task_mmu.c').read_bytes()
        from ensure_susfs_map_compat import IMPLEMENTATION, DECLARATION
        def verify_ready(*_args, **_kwargs):
            self.assertIn(IMPLEMENTATION, (common / 'mm/page_size_compat.c').read_text(encoding='utf-8'))
            self.assertIn(DECLARATION, (common / 'include/linux/page_size_compat.h').read_text(encoding='utf-8'))
            return SimpleNamespace(calls=1, ok=True)
        self.finalize_mocks()
        with mock.patch.object(kernel, 'validate_hooks', side_effect=verify_ready):
            self.finalize(inputs={**self.inputs, 'enable_susfs': True})
        self.assertEqual(task, (common / 'fs/proc/task_mmu.c').read_bytes())

    def test_enabled_kpm_cannot_silently_ignore_a_missing_native_bridge_symbol(self):
        self.finalize_mocks()
        path = self.ksu / 'Kconfig'
        path.write_text(path.read_text().replace('config KPM\n', ''))
        with self.assertRaisesRegex(ValueError, 'native KPM bridge configuration'):
            self.finalize(inputs={**self.inputs, 'use_kpm': True})

    def test_include_root_adaptation_is_only_for_oplus_sukisu_612(self):
        for version, variant in (('6.1', 'SukiSU'), ('6.6', 'SukiSU'), ('6.12', 'SukiSU'), ('6.12', 'BakaSU')):
            with self.subTest(version=version, variant=variant):
                self.finalize_mocks(variant)
                with mock.patch.object(kernel, 'validate_profile', side_effect=lambda profile: profile), \
                        mock.patch.object(kernel, 'native_inputs'), \
                        mock.patch('oplus_ksu_compat.prepare_builtin_includes') as includes:
                    old = self.profile
                    self.profile = {**old, 'kernel_version': version}
                    try:
                        self.finalize(variant=variant)
                    finally:
                        self.profile = old
                    if version == '6.12' and variant == 'SukiSU':
                        includes.assert_called_once_with(self.root, self.reference)
                    else:
                        includes.assert_not_called()

    def test_kpm_member_adaptation_only_runs_for_enabled_sukisu_612(self):
        for version, variant, enabled in (('6.12', 'SukiSU', True), ('6.12', 'SukiSU', False),
                                          ('6.6', 'SukiSU', True), ('6.12', 'BakaSU', True)):
            with self.subTest(version=version, variant=variant, enabled=enabled):
                self.finalize_mocks(variant)
                with mock.patch.object(kernel, 'validate_profile', side_effect=lambda profile: profile), \
                        mock.patch.object(kernel, 'native_inputs'), \
                        mock.patch('oplus_ksu_compat.prepare_kpm_members') as members:
                    old = self.profile
                    self.profile = {**old, 'kernel_version': version}
                    try:
                        self.finalize(inputs={**self.inputs, 'kernelsu_variant': variant, 'use_kpm': enabled}, variant=variant)
                    finally:
                        self.profile = old
                    if version == '6.12' and variant == 'SukiSU' and enabled:
                        members.assert_called_once_with(self.root, self.reference)
                    else:
                        members.assert_not_called()

    def test_patch_rejects_after_failed_dry_run_without_applying(self):
        patch = self.workspace / 'test.patch'
        patch.write_text('synthetic patch input\n')
        original = self.config.read_bytes()
        with mock.patch.object(kernel, 'command', side_effect=ValueError('dry-run rejected')) as command:
            with self.assertRaisesRegex(ValueError, 'dry-run rejected'):
                kernel.patch_checked(self.root / 'common', patch)
        self.assertEqual(1, command.call_count)
        self.assertIn('--dry-run', command.call_args.args[0])
        self.assertEqual(original, self.config.read_bytes())

    def test_failed_patch_reports_the_file_hunk_and_stage_with_bounded_redacted_diagnostics(self):
        response = subprocess.CompletedProcess([], 1, stdout='x' * 15000 + '\nchecking file fs/namespace.c\n'
                                               'Hunk #1 FAILED at 32.\n', stderr='private-fixture-token')
        output = io.StringIO()
        with mock.patch.object(kernel.subprocess, 'run', return_value=response), \
                mock.patch.dict(os.environ, {'GH_TOKEN': 'private-fixture-token'}), redirect_stderr(output), \
                self.assertRaisesRegex(ValueError, 'patch.*exit 1'):
            kernel.command(['patch', '--forward', '--dry-run', '-p1', '-i', self.workspace / 'susfs.patch'])
        diagnostics = output.getvalue()
        self.assertIn('susfs.patch failed during forward dry-run', diagnostics)
        self.assertIn('checking file fs/namespace.c', diagnostics)
        self.assertIn('Hunk #1 FAILED at 32.', diagnostics)
        self.assertIn('[REDACTED]', diagnostics)
        self.assertNotIn('private-fixture-token', diagnostics)
        self.assertLess(len(diagnostics), 12500)
        output = io.StringIO()
        with mock.patch.object(kernel.subprocess, 'run', return_value=response), redirect_stderr(output), \
                self.assertRaises(ValueError):
            kernel.command(['git', 'status'])
        self.assertEqual('', output.getvalue())

    def test_checked_patch_requires_a_complete_reverse_check_after_application(self):
        patch = self.workspace / 'test.patch'
        original = b'fixed patch input\n'
        patch.write_bytes(original)
        calls = []
        def execute(arguments, *, cwd):
            self.assertEqual(self.root / 'common', cwd)
            prepared = Path(arguments[arguments.index('-i') + 1])
            self.assertEqual(original, prepared.read_bytes())
            calls.append(list(arguments))
            return ''
        with mock.patch.object(kernel, 'command', side_effect=execute):
            kernel.patch_checked(self.root / 'common', patch)
        self.assertEqual(3, len(calls))
        self.assertIn('--forward', calls[0])
        self.assertIn('--dry-run', calls[0])
        self.assertNotIn('--dry-run', calls[1])
        self.assertIn('--reverse', calls[2])
        self.assertIn('--dry-run', calls[2])
        self.assertEqual(original, patch.read_bytes())

    def test_official_namespace_port_is_strict_and_every_file_is_preflighted_before_applying(self):
        fixture = ROOT / 'tests/fixtures/oplus-susfs61'
        source = self.root / 'common/fs/namespace.c'
        source.parent.mkdir(parents=True)
        source.write_bytes((fixture / 'namespace-header.c').read_bytes())
        patch = self.workspace / '50_add_susfs_in_gki-android14-6.1.patch'
        second_section = (b'diff --git a/fs/example.c b/fs/example.c\n'
                          b'--- a/fs/example.c\n+++ b/fs/example.c\n'
                          b'@@ -1 +1 @@\n-before\n+after\n')
        original = (fixture / 'namespace-header.patch').read_bytes() + second_section
        patch.write_bytes(original)
        observed = []
        def execute(arguments, *, cwd):
            content = Path(arguments[arguments.index('-i') + 1]).read_bytes()
            observed.append((arguments, content))
            return ''
        with mock.patch.object(kernel, 'command', side_effect=execute):
            kernel.patch_checked(self.root / 'common', patch)
        self.assertEqual(6, len(observed))
        self.assertTrue(all('--dry-run' in command for command, _ in observed[:2]))
        self.assertTrue(all('--dry-run' not in command for command, _ in observed[2:4]))
        self.assertTrue(all('--reverse' in command for command, _ in observed[4:]))
        for command, content in observed[::2]:
            self.assertIn('--fuzz=0', command)
            self.assertIn(b'fs/namespace.c', content)
            self.assertNotIn(second_section, content)
        for command, content in observed[1::2]:
            self.assertIn('--fuzz=3', command)
            self.assertEqual(second_section, content)
        self.assertEqual(original, patch.read_bytes())

        observed.clear()
        def rejected(arguments, *, cwd):
            execute(arguments, cwd=cwd)
            if len(observed) == 2:
                raise ValueError('another file failed its dry-run')
        with mock.patch.object(kernel, 'command', side_effect=rejected), self.assertRaisesRegex(ValueError, 'another file'):
            kernel.patch_checked(self.root / 'common', patch)
        self.assertEqual(2, len(observed))
        self.assertTrue(all('--dry-run' in command for command, _ in observed))
        self.assertEqual((fixture / 'namespace-header.c').read_bytes(), source.read_bytes())

    def test_disabled_susfs_never_restores_ports_or_downloads_it(self):
        with mock.patch.object(kernel, 'checked_root', return_value=self.root), \
                mock.patch.object(kernel, 'checkout_snapshot') as checkout, \
                mock.patch.object(kernel, 'integrate') as integrate, \
                mock.patch.object(kernel, 'restore_source') as restore, \
                mock.patch.object(kernel, 'read_json') as sources, \
                mock.patch.object(kernel, 'patch_checked') as patch, \
                mock.patch('apply_susfs23.apply_integration') as port:
            self.assertEqual(self.root, kernel.integrate_manager(self.workspace, self.profile, self.inputs,
                self.workspace, self.reference, 'SukiSU', 'invalid-unneeded-susfs-metadata'))
        checkout.assert_called_once_with(self.workspace, self.root / 'KernelSU', self.reference, variant='SukiSU')
        integrate.assert_called_once_with(self.root, self.reference)
        for ignored in (restore, sources, patch, port):
            ignored.assert_not_called()

    def test_both_native_66_hook_files_are_strict_and_preflighted_before_any_apply(self):
        sections = [f'diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n'
                    '@@ -1 +1 @@\n-before\n+after\n'
                    for name in ('fs/namespace.c', 'fs/proc/task_mmu.c', 'fs/example.c')]
        original = ''.join(sections).encode()
        patch = self.workspace / '50_add_susfs_in_gki-android15-6.6.patch'
        patch.write_bytes(original)
        calls = []
        def execute(arguments, *, cwd):
            content = Path(arguments[arguments.index('-i') + 1]).read_bytes()
            calls.append((arguments, content))
            return ''
        with mock.patch('oplus_susfs66.adapt_susfs66', return_value=original), \
                mock.patch.object(kernel, 'command', side_effect=execute):
            kernel.patch_checked(self.root / 'common', patch)
        self.assertEqual(9, len(calls))
        self.assertTrue(all('--dry-run' in command for command, _ in calls[:3]))
        self.assertTrue(all('--dry-run' not in command for command, _ in calls[3:6]))
        self.assertTrue(all('--reverse' in command for command, _ in calls[6:]))
        for index, (command, content) in enumerate(calls):
            self.assertEqual(sections[index % 3].encode(), content)
            self.assertIn('--fuzz=0' if index % 3 < 2 else '--fuzz=3', command)
        calls.clear()
        def rejected(arguments, *, cwd):
            execute(arguments, cwd=cwd)
            if len(calls) == 3:
                raise ValueError('unrelated third file failed its dry-run')
        with mock.patch('oplus_susfs66.adapt_susfs66', return_value=original), \
                mock.patch.object(kernel, 'command', side_effect=rejected), self.assertRaisesRegex(ValueError, 'third file'):
            kernel.patch_checked(self.root / 'common', patch)
        self.assertEqual(3, len(calls))
        self.assertTrue(all('--dry-run' in command for command, _ in calls))
        self.assertEqual(original, patch.read_bytes())

    def test_enabled_susfs_uses_prepared_official_target_and_only_ports_sukisu(self):
        sources = {'android14-6.1': {'ref': 'f' * 40, 'version': 'v2.3.0'}}
        def restored(_snapshot, destination, _reference, _target, _sources, *, variant):
            for relative, text in (
                    ('include/linux/susfs.h', '#define SUSFS_VERSION "v2.3.0"\n'),
                    ('fs/susfs.c', 'int real_susfs_provider(void) { return 1; }\n')):
                path = destination / 'kernel_patches' / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                (self.root / 'common' / relative).parent.mkdir(parents=True, exist_ok=True)
        for variant in ('SukiSU', 'BakaSU'):
            with self.subTest(variant=variant), \
                    mock.patch.object(kernel, 'checked_root', return_value=self.root), \
                    mock.patch.object(kernel, 'checkout_snapshot'), mock.patch.object(kernel, 'integrate'), \
                    mock.patch.object(kernel, 'restore_source', side_effect=restored) as restore, \
                    mock.patch.object(kernel, 'read_json', return_value=sources), \
                    mock.patch.object(kernel, 'patch_checked') as patch, \
                    mock.patch('apply_susfs23.apply_integration') as port:
                kernel.integrate_manager(self.workspace, self.profile,
                    {**self.inputs, 'kernelsu_variant': variant, 'enable_susfs': True}, self.workspace,
                    self.reference, variant, 'prepared-source-map')
                dependency = self.workspace.resolve() / 'official-susfs'
                restore.assert_called_once_with(self.workspace, dependency, self.reference,
                                                'android14-6.1', sources, variant=variant)
                patch.assert_called_once_with(self.root / 'common',
                    dependency / 'kernel_patches/50_add_susfs_in_gki-android14-6.1.patch')
                if variant == 'SukiSU':
                    port.assert_called_once_with(self.root, dependency)
                else:
                    port.assert_not_called()
                self.assertEqual('#define SUSFS_VERSION "v2.3.0"\n',
                                 (self.root / 'common/include/linux/susfs.h').read_text())

    def test_enabled_susfs_requires_matching_nonempty_real_hooks(self):
        self.finalize_mocks()
        selected = {**self.inputs, 'enable_susfs': True}
        for result in (SimpleNamespace(calls=[], ok=True), SimpleNamespace(calls=['real'], ok=False)):
            with self.subTest(result=result), mock.patch.object(kernel, 'validate_hooks', return_value=result) as validate:
                with self.assertRaisesRegex(ValueError, 'hooks do not match'):
                    self.finalize(inputs=selected)
                validate.assert_called_once_with(self.root, variant='SukiSU')

    def test_bakasu_calls_native_guard_without_replacing_future_resolver_functions(self):
        source = self.ksu / 'infra/symbol_resolver.c'
        source.parent.mkdir()
        source.write_text('unsigned long new_official_lookup(const char *name) { return future_resolve(name); }\n')
        before = source.read_bytes()
        self.finalize_mocks('BakaSU')
        with mock.patch('ensure_bakasu_symbol_lookup.validate_native_symbol_lookup', return_value=False) as guard:
            self.finalize(variant='BakaSU')
        guard.assert_called_once_with(self.root, self.reference)
        self.assertEqual(before, source.read_bytes())
        config = self.config.read_text()
        self.assertIn('CONFIG_KSU_TRACEPOINT_HOOK=y\n', config)
        self.assertIn('CONFIG_KSU_MANUAL_HOOK=n\n', config)
        self.assertIn('CONFIG_KSU_MULTI_MANAGER_SUPPORT=y\n', config)

    def test_unicode_patch_requires_fixed_checkout_and_exact_head(self):
        self.finalize_mocks(PATCHES_REF=self.reference)
        selected = {**self.inputs, 'use_unicode_bypass': True}
        with mock.patch.object(kernel, 'patch_checked') as patch:
            with self.assertRaisesRegex(ValueError, 'fixed reference patch checkout'):
                self.finalize(inputs=selected)
            with mock.patch.object(kernel, 'command', return_value='f' * 40):
                with self.assertRaisesRegex(ValueError, 'prepared reference'):
                    self.finalize(inputs=selected, patches=self.workspace)
            patch.assert_not_called()
            self.finalize(inputs=selected, patches=self.workspace)
            patch.assert_called_once_with(self.root / 'common', self.workspace / 'common/unicode_bypass_fix_6.1+.patch')

    def test_bakasu_unknown_native_hook_mode_stops(self):
        (self.ksu / 'Kconfig').write_text('config KSU\nconfig FUTURE_UNKNOWN_HOOK\n')
        self.finalize_mocks('BakaSU')
        with self.assertRaisesRegex(ValueError, 'native hook mode'):
            self.finalize(variant='BakaSU')

    def test_both_phases_reject_menu_variant_mismatch_and_nonimmutable_reference(self):
        for variant, reference in (('BakaSU', self.reference), ('SukiSU', 'main'), ('SukiSU', 'a' * 7)):
            with self.subTest(variant=variant, reference=reference), \
                    mock.patch.object(kernel, 'checked_root') as checked:
                with self.assertRaisesRegex(ValueError, 'variant|immutable source SHA'):
                    kernel.integrate_manager(self.workspace, self.profile, self.inputs,
                        self.workspace, reference, variant, '{}')
                with self.assertRaisesRegex(ValueError, 'variant|immutable source SHA'):
                    kernel.finalize_config(self.workspace, self.profile, self.inputs, reference, variant)
                checked.assert_not_called()

    def test_finalize_rechecks_manager_head_before_any_signature_or_config_write(self):
        signature = self.finalize_mocks()
        before = self.config.read_bytes()
        def wrong_head(_path, *arguments):
            return str(self.root / 'KernelSU') if arguments == ('rev-parse', '--show-toplevel') else 'f' * 40
        with mock.patch.object(kernel, 'source_git', side_effect=wrong_head), \
                self.assertRaisesRegex(ValueError, 'manager HEAD differs'):
            self.finalize()
        signature.assert_not_called()
        self.assertEqual(before, self.config.read_bytes())

    def test_prepare_dependencies_only_resolves_optional_patch_when_enabled(self):
        with mock.patch.object(kernel, 'resolve_dependency', return_value=self.reference) as resolve:
            self.assertEqual({'ak3_ref': self.reference, 'patches_ref': ''}, kernel.prepare_dependencies(False))
            resolve.assert_called_once_with('osm0sis/AnyKernel3')
        with mock.patch.object(kernel, 'resolve_dependency', side_effect=[self.reference, 'b' * 40]) as resolve:
            self.assertEqual({'ak3_ref': self.reference, 'patches_ref': 'b' * 40}, kernel.prepare_dependencies(True))
            self.assertEqual([mock.call('osm0sis/AnyKernel3'), mock.call('Xiaomichael/kernel_patches')],
                             resolve.call_args_list)

    def test_dependency_ref_public_https_allowlist_and_bounded_retry_never_log_token(self):
        secret = 'token-must-never-appear'
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': secret}), \
                mock.patch.object(kernel, 'source_git', side_effect=[ValueError(secret),
                    ValueError(secret), self.reference + '\trefs/heads/master']) as remote, \
                mock.patch.object(kernel.time, 'sleep') as pause, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.reference, kernel.resolve_dependency('osm0sis/AnyKernel3'))
        self.assertNotIn(secret, output.getvalue())
        self.assertEqual([mock.call(2), mock.call(5)], pause.call_args_list)
        self.assertEqual(3, remote.call_count)
        for call in remote.call_args_list:
            self.assertEqual(('ls-remote', '--exit-code', 'https://github.com/osm0sis/AnyKernel3.git',
                              'refs/heads/master'), call.args[1:])
        with mock.patch.object(kernel, 'source_git') as remote, self.assertRaises(ValueError):
            kernel.resolve_dependency('other/MovingBuildScript')
        remote.assert_not_called()

    def test_dependency_resolution_rejects_ambiguous_output_and_sanitizes_last_failure(self):
        for output in ('main\tHEAD', self.reference + '\twrong',
                       self.reference + '\tHEAD\n' + 'b' * 40 + '\tHEAD'):
            with self.subTest(output=output), mock.patch.object(kernel, 'source_git', return_value=output), \
                    self.assertRaisesRegex(ValueError, 'ambiguous or invalid'):
                kernel.resolve_dependency('Xiaomichael/kernel_patches')
        with mock.patch.object(kernel, 'source_git', side_effect=ValueError('secret-from-transport')), \
                mock.patch.object(kernel.time, 'sleep'), redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(ValueError, 'three attempts') as error:
            kernel.resolve_dependency('osm0sis/AnyKernel3')
        self.assertNotIn('secret-from-transport', str(error.exception))

    def test_prepare_deps_cli_standalone_outputs_and_json_input(self):
        output = self.workspace / 'github-output'
        inputs = self.workspace / 'inputs.json'
        inputs.write_text(json.dumps(self.inputs))
        expected = {'ak3_ref': self.reference, 'patches_ref': ''}
        with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}, clear=True), \
                mock.patch.object(kernel, 'prepare_dependencies', return_value=expected) as prepare, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, kernel.main(['--phase', 'prepare-deps', '--inputs', str(inputs)]))
        prepare.assert_called_once_with(False)
        self.assertEqual('ak3_ref=' + self.reference + '\npatches_ref=\n', output.read_text())
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(kernel, 'prepare_dependencies', return_value=expected) as prepare, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, kernel.main(['--phase', 'prepare-deps', '--unicode-bypass', 'true']))
        prepare.assert_called_once_with(True)
        with mock.patch.object(kernel, 'prepare_dependencies') as prepare, redirect_stderr(io.StringIO()):
            self.assertEqual(1, kernel.main(['--phase', 'prepare-deps', '--inputs', str(inputs),
                                             '--unicode-bypass', 'true']))
        prepare.assert_not_called()


if __name__ == '__main__':
    unittest.main()
