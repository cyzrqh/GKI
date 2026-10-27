"""Exercise the independent eight-input OPlus workflow and its real data flow."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import generate_workflow as generator
import oplus_workflow as workflow
import oplus_recipe as recipe
import oplus_kernel as kernel
import oplus_catalog as catalog
from transfer_ksu_snapshot import native_assets


def heredoc(script):
    return script.split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]


class OplusWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = generator.create_workflow(ROOT)
        cls.document = workflow.create_workflow(ROOT, cls.base)

    def setUp(self):
        self.directory = ROOT.parent / '.gki-upstream-cache' / ('oplus-workflow-test-' + uuid.uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)

    def test_exact_eight_menu_fields_and_defaults_from_requested_menu(self):
        self.assertEqual(self.document['name'], '欧加真全属性内核构建矩阵')
        self.assertEqual(list(self.document['on']), ['workflow_dispatch'])
        fields = self.document['on']['workflow_dispatch']['inputs']
        self.assertEqual(set(fields), recipe.INPUTS)
        self.assertEqual(len(fields), 8)
        self.assertEqual(fields['kernelsu_variant']['options'], ['SukiSU', 'BakaSU'])
        self.assertEqual(fields['kernelsu_variant']['default'], 'SukiSU')
        self.assertEqual([key for key in fields if fields[key]['default'] is True], ['use_bbg', 'use_proxy_optimization'])
        self.assertNotIn('kernelsu_branch', fields)
        self.assertNotIn('models', fields)
        self.assertNotIn('schedule', self.document['on'])

    def test_every_expression_uses_declared_menu_and_no_remote_workflow(self):
        text = json.dumps(self.document)
        expressions = re.findall(r'\$\{\{(.*?)\}\}', text)
        conditions = [job.get('if', '') for job in self.document['jobs'].values()]
        conditions += [step.get('if', '') for job in self.document['jobs'].values() for step in job.get('steps', [])]
        fields = set(re.findall(r'\binputs\.(\w+)', ' '.join(expressions + conditions)))
        self.assertFalse(fields - set(workflow.INPUTS), fields)
        self.assertNotIn('inputs.operation', text)
        self.assertNotIn('inputs.kernelsu_branch', text)
        self.assertTrue(all('uses' not in job for job in self.document['jobs'].values()))
        self.assertNotIn('workflow_call', self.document['on'])

    def test_base_gki_pipeline_is_preserved_and_manager_lkm_are_shared(self):
        before = copy.deepcopy(self.base)
        workflow.create_workflow(ROOT, self.base)
        self.assertEqual(self.base, before)
        for name in ('build-sukisu-manager', 'build-sukisu-lkm'):
            expected = workflow.adapt(copy.deepcopy(self.base['jobs'][name]))
            expected['if'] = workflow.DISPATCH
            self.assertEqual(self.document['jobs'][name], expected)
        manager = self.document['jobs']['build-sukisu-manager']
        self.assertIn('build-sukisu-lkm', manager['needs'])
        self.assertTrue(any('verify_manager_boot_patch.py' in item.get('run', '') for item in manager['steps']))
        upload = next(item for item in manager['steps'] if item.get('name') == '上传管理器安装包')
        self.assertIs(upload['with']['archive'], False)

    def test_official_catalog_and_latest_manager_are_fixed_before_snapshot_and_matrix_jobs(self):
        prepare = self.document['jobs']['prepare-matrix']
        indexed = {item['id']: (index, item) for index, item in enumerate(prepare['steps']) if 'id' in item}
        self.assertEqual(indexed['matrix'][1]['run'], 'python3 tools/oplus_catalog.py --github-output --summary')
        self.assertLess(indexed['matrix'][0], indexed['sources'][0])
        self.assertLess(indexed['sources'][0], indexed['source-snapshot'][0])
        self.assertEqual(indexed['sources'][1]['env']['KSU_TIER'], '最新版')
        self.assertEqual(indexed['sources'][1]['env']['CUSTOM_REF'], '')
        self.assertIn('prepare-deps', indexed['oplus-deps'][1]['run'])
        self.assertIn('--unicode-bypass "$UNICODE_BYPASS"', indexed['oplus-deps'][1]['run'])
        self.assertEqual(indexed['oplus-deps'][1]['env']['UNICODE_BYPASS'], '${{ inputs.use_unicode_bypass }}')
        for key in ('ak3_ref', 'patches_ref'):
            self.assertEqual(prepare['outputs'][key], '${{ steps.oplus-deps.outputs.' + key + ' }}')
        self.assertNotIn('max_parallel', prepare['outputs'])
        self.assertNotIn('full_catalog', prepare['outputs'])
        source = indexed['shared-susfs'][1]
        self.assertEqual(source['env']['SUSFS_MATRIX'], '${{ steps.matrix.outputs.matrix }}')
        self.assertIn('inputs.enable_susfs', source['if'])
        self.assertLess(indexed['shared-susfs'][0], indexed['source-snapshot'][0])

    def test_worker_waits_for_signed_manager_and_restores_same_snapshot(self):
        worker = self.document['jobs']['build-oplus-kernel']
        self.assertEqual(worker['needs'], ['prepare-matrix', 'build-sukisu-manager'])
        self.assertIn("needs.build-sukisu-manager.result == 'success'", worker['if'])
        self.assertIn('!cancelled()', worker['if'])
        self.assertEqual(worker['permissions'], {'contents': 'read', 'actions': 'read'})
        wait = next(step for step in worker['steps'] if 'wait_manager_identity.py' in step.get('run', ''))
        self.assertIn('--manager-job-id "$MANAGER_JOB_ID"', wait['run'])
        self.assertEqual(wait['env']['MANAGER_JOB_ID'], '${{ needs.build-sukisu-manager.outputs.manager_job_id }}')
        self.assertEqual(wait['env']['MANAGER_ARTIFACT'], '${{ needs.build-sukisu-manager.outputs.apk_filename }}')
        fallback = next(step for step in worker['steps'] if 'transfer_ksu_snapshot.py restore' in step.get('run', ''))
        self.assertNotIn('inputs.operation', json.dumps(fallback))
        self.assertIn('inputs.enable_susfs', fallback['env']['ENABLE_SUSFS'])
        self.assertEqual(worker['env']['SUSFS_SOURCES'], '${{ needs.prepare-matrix.outputs.susfs_sources }}')
        self.assertEqual(worker['strategy']['matrix'], '${{ fromJSON(needs.prepare-matrix.outputs.matrix) }}')
        self.assertFalse(worker['strategy']['fail-fast'])

    def test_worker_orders_real_native_phases_then_verified_payload_package_upload(self):
        worker = self.document['jobs']['build-oplus-kernel']
        commands = [step.get('run', '') for step in worker['steps']]
        def locate(token):
            return next(index for index, command in enumerate(commands) if token in command)
        order = [locate(token) for token in ('--phase bootstrap', '--phase integrate', '--phase configure',
                  '--phase finalize', 'apply_kpm_patch.py prepare', '--phase build', 'apply_kpm_patch.py patch',
                  'oplus_assets.py package')]
        self.assertEqual(order, sorted(order))
        for index in order:
            self.assertNotIn('continue-on-error', worker['steps'][index])
        for phase in ('bootstrap', 'configure', 'build'):
            self.assertIn('--workspace "$GITHUB_WORKSPACE"', commands[locate('--phase ' + phase)])
        package = worker['steps'][locate('oplus_assets.py package')]
        self.assertIn('--kernel-root "$GITHUB_WORKSPACE/kernel_workspace"', package['run'])
        self.assertIn('common/out/arch/arm64/boot/Image', package['run'])
        kpm = worker['steps'][locate('apply_kpm_patch.py patch')]
        self.assertIn('common/out/gki-kpm-patch.json', kpm['run'])
        self.assertEqual(kpm['if'], 'inputs.use_kpm')
        self.assertEqual(worker['steps'][locate('apply_kpm_patch.py prepare')]['if'], 'inputs.use_kpm')
        upload = next(item for item in worker['steps'] if item.get('uses') == 'actions/upload-artifact@v7')
        self.assertIs(upload['with']['archive'], False)
        self.assertNotIn('name', upload['with'])
        self.assertEqual(upload['with']['path'], 'oplus-ready/${{ steps.oplus-assets.outputs.file }}')
        self.assertEqual(sum('actions/upload-artifact' in item.get('uses', '') for item in worker['steps']), 1)

    def test_worker_uses_reviewed_method_and_official_ak3_and_optional_fixed_unicode(self):
        worker = self.document['jobs']['build-oplus-kernel']
        checkouts = {step.get('with', {}).get('path'): step for step in worker['steps'] if step.get('uses') == 'actions/checkout@v6'}
        self.assertEqual(checkouts['oplus-reference']['with']['ref'], '${{ matrix.recipe_ref }}')
        self.assertEqual(checkouts['oplus-reference']['with']['repository'], '${{ matrix.recipe_repository }}')
        self.assertEqual(checkouts['oplus-ak3']['with']['repository'], 'osm0sis/AnyKernel3')
        self.assertEqual(checkouts['oplus-ak3']['with']['ref'], '${{ needs.prepare-matrix.outputs.ak3_ref }}')
        self.assertEqual(checkouts['oplus-patches']['with']['repository'], 'Xiaomichael/kernel_patches')
        self.assertEqual(checkouts['oplus-patches']['with']['ref'], '${{ needs.prepare-matrix.outputs.patches_ref }}')
        self.assertEqual(checkouts['oplus-patches']['if'], 'inputs.use_unicode_bypass')
        for step in checkouts.values():
            self.assertIs(step['with']['persist-credentials'], False)
        self.assertNotIn('AnyKernel3-master', json.dumps(worker))

    def test_native_dependency_set_and_bounded_isolated_cache_cover_all_series(self):
        worker = self.document['jobs']['build-oplus-kernel']
        install = next(step for step in worker['steps'] if step.get('name') == '安装原生编译工具与系统开发依赖')
        self.assertEqual(install['timeout-minutes'], 15)
        for setting in ('Acquire::http::Timeout=30', 'Acquire::https::Timeout=30', 'Acquire::Retries=2', 'DPkg::Lock::Timeout=60'):
            self.assertIn(setting, install['run'])
        for package in ('aria2', 'libdw-dev', 'libelf-dev', 'libssl-dev', 'ccache', 'bison', 'flex', 'zstd', 'bc', 'patch', 'unzip'):
            self.assertIn(package, install['run'])
        self.assertNotIn('apt purge', install['run'])
        self.assertNotIn('rm -rf /', json.dumps(worker))
        self.assertEqual(worker['env']['CCACHE_DIR'], '${{ github.workspace }}/.oplus-ccache')
        restore = next(item for item in worker['steps'] if item.get('id') == 'oplus-ccache')
        save = next(item for item in worker['steps'] if item.get('uses') == 'actions/cache/save@v5' and item.get('name') == '保存已成功编译的平台缓存')
        self.assertIn('${{ matrix.id }}', restore['with']['key'])
        self.assertIn('${{ matrix.recipe_ref }}', restore['with']['key'])
        self.assertIn('${{ github.run_id }}-${{ github.run_attempt }}', restore['with']['key'])
        self.assertEqual(save['with']['key'], '${{ steps.oplus-ccache.outputs.cache-primary-key }}')
        self.assertEqual(save['timeout-minutes'], 2)
        self.assertIn("steps.oplus-assets.outcome == 'success'", save['if'])

    def test_profile_and_eight_input_heredoc_really_validates_and_writes_json(self):
        profile = copy.deepcopy(catalog.load_profiles()[0])
        profile['kernel_ref'] = profile.pop('verified_kernel_ref')
        profile['source_committed_at'] = '2026-10-27T00:00:00Z'
        inputs = {name: field['default'] for name, field in workflow.INPUTS.items()}
        env = {'RUNNER_TEMP': str(self.directory), 'OPLUS_PROFILE': json.dumps(profile), 'OPLUS_INPUTS': json.dumps(inputs)}
        with mock.patch.dict(os.environ, env):
            exec(compile(heredoc(workflow.PROFILE_INPUTS), '<workflow frozen inputs>', 'exec'), {})
        self.assertEqual(json.loads(self.directory.joinpath('oplus-profile.json').read_text()), profile)
        self.assertEqual(json.loads(self.directory.joinpath('oplus-inputs.json').read_text()), inputs)
        inputs['unreviewed_extra_flag'] = True
        with mock.patch.dict(os.environ, {**env, 'OPLUS_INPUTS': json.dumps(inputs)}), self.assertRaises(ValueError):
            exec(compile(heredoc(workflow.PROFILE_INPUTS), '<workflow rejects extra menu>', 'exec'), {})

    def test_cli_phase_argument_paths_reach_real_parsers_in_native_order(self):
        profile = copy.deepcopy(catalog.load_profiles()[0])
        profile['kernel_ref'] = profile.pop('verified_kernel_ref')
        profile['source_committed_at'] = '2026-10-27T00:00:00Z'
        inputs = {name: field['default'] for name, field in workflow.INPUTS.items()}
        profile_path, inputs_path = self.directory / 'oplus-profile.json', self.directory / 'oplus-inputs.json'
        profile_path.write_text(json.dumps(profile))
        inputs_path.write_text(json.dumps(inputs))
        for phase in ('bootstrap', 'configure', 'build'):
            with self.subTest(phase=phase), mock.patch.object(recipe, 'execute_phase') as runner:
                result = recipe.main(['--phase', phase, '--profile', str(profile_path), '--inputs', str(inputs_path),
                                      '--workspace', str(self.directory), '--recipe-root', str(self.directory / 'reference')])
                self.assertEqual(result, 0)
                self.assertEqual(runner.call_args.args[:1], (phase,))
                self.assertEqual(runner.call_args.args[2:4], (profile, inputs))
        for phase, function in [('integrate', 'integrate_manager'), ('finalize', 'finalize_config')]:
            with self.subTest(phase=phase), mock.patch.object(kernel, function) as runner, mock.patch.dict(
                    os.environ, {'KSU_REF': 'b' * 40, 'KSU_VARIANT': 'BakaSU'}):
                result = kernel.main(['--phase', phase, '--profile', str(profile_path), '--inputs', str(inputs_path),
                                      '--workspace', str(self.directory), '--snapshot', str(self.directory / 'bundle'),
                                      '--patches', str(self.directory / 'patches')])
                self.assertEqual(result, 0)
                self.assertEqual(runner.call_args.args[:3], (self.directory, profile, inputs))

    def test_native_aggregation_copies_real_signature_verified_outputs_for_delete_proof(self):
        # Reuse true test-only RSA signatures and Git/Image fixtures instead of
        # hand-writing an already-trusted metadata sidecar for the workflow.
        from test_oplus_assets import OplusAssetsTests
        OplusAssetsTests.setUpClass()
        fixture = OplusAssetsTests('test_package_preserves_native_release_and_paired_actual_image')
        try:
            fixture.setUp()
            fixture.pack()
            fixture.inputs.rename(fixture.directory / 'oplus-input')
            fixture.inputs = fixture.directory / 'oplus-input'
            previous = Path.cwd()
            try:
                os.chdir(fixture.directory)
                import oplus_assets
                result = oplus_assets.collect(fixture.inputs, fixture.directory / 'oplus-report',
                                              {'include': [fixture.profile]}, 'SukiSU')
                exec(compile(heredoc(workflow.NATIVE_ASSETS), '<workflow native cleanup proof>', 'exec'), {})
                files = native_assets((fixture.directory / 'oplus-verified-assets').resolve())
            finally:
                os.chdir(previous)
            self.assertEqual(result['count'], 1)
            self.assertEqual(len(files), 3)
            self.assertEqual(set(files), {fixture.manager['file'], 'SHA256SUMS.txt',
                                          'Oplus-sm8650-android14-6.1.57-SukiSU-AnyKernel3.zip'})
            self.assertNotIn('summary.md', files)
        finally:
            fixture.doCleanups()
            OplusAssetsTests.tearDownClass()

    def test_collect_is_all_success_only_native_downloads_and_never_public_release(self):
        aggregate = self.document['jobs']['verify-oplus-assets']
        self.assertIn("needs.build-oplus-kernel.result == 'success'", aggregate['if'])
        download = next(item for item in aggregate['steps'] if item.get('uses') == 'actions/download-artifact@v8')
        self.assertIs(download['with']['skip-decompress'], True)
        self.assertIs(download['with']['merge-multiple'], False)
        self.assertIn('Oplus-*-AnyKernel3.zip', download['with']['pattern'])
        collect = next(item for item in aggregate['steps'] if item.get('id') == 'assets')
        self.assertIn('--variants "$KSU_VARIANT"', collect['run'])
        self.assertEqual(collect['env']['OPLUS_MATRIX'], '${{ needs.prepare-matrix.outputs.matrix }}')
        cleanup = next(item for item in aggregate['steps'] if 'delete-actions' in item.get('run', ''))
        self.assertIn('oplus-verified-assets', cleanup['run'])
        steps = aggregate['steps']
        upload = next(item for item in steps if item.get('uses') == 'actions/upload-artifact@v7')
        self.assertLess(steps.index(collect), steps.index(upload))
        self.assertLess(steps.index(upload), steps.index(cleanup))
        self.assertEqual(upload['with']['path'], 'oplus-report/SHA256SUMS.txt')
        self.assertIs(upload['with']['archive'], False)
        self.assertNotIn('publish-release', self.document['jobs'])
        self.assertNotIn('gh release create', json.dumps(self.document))
        summary = steps[-1]
        self.assertIn('oplus-report/summary.md', summary['run'])
        self.assertIn("steps.assets.outcome == 'success'", summary['if'])

    def test_generated_fourth_workflow_is_reproducible_yaml_with_boolean_on(self):
        current = generator.load_yaml(ROOT / workflow.OUTPUT)
        self.assertEqual(current, self.document)
        self.assertIs(current['on']['workflow_dispatch']['inputs']['enable_susfs']['default'], False)
        self.assertIs(current['jobs']['build-oplus-kernel']['strategy']['fail-fast'], False)


if __name__ == '__main__':
    unittest.main()
