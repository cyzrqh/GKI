"""Exercise the independent OPlus workflow and its frozen manager data flow."""
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
import oplus_cctv as cctv


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

    def test_original_feature_menu_and_shared_manager_version_choice_keep_their_defaults(self):
        self.assertEqual(self.document['name'], '欧加真全属性内核构建矩阵')
        self.assertEqual(list(self.document['on']), ['workflow_dispatch'])
        fields = self.document['on']['workflow_dispatch']['inputs']
        self.assertEqual(set(fields), recipe.INPUTS | {'build_target'})
        self.assertEqual(len(fields), 10)
        from manager_variants import MANAGER_SELECTIONS
        self.assertEqual(fields['kernelsu_variant']['options'], list(MANAGER_SELECTIONS))
        self.assertEqual(fields['kernelsu_variant']['default'], 'SukiSU')
        self.assertEqual([key for key in fields if fields[key]['default'] is True],
                         ['enable_susfs', 'use_kpm', 'use_lz4kd'])
        self.assertEqual(fields['kernelsu_branch'], self.base['on']['workflow_dispatch']['inputs']['kernelsu_branch'])
        self.assertEqual(fields['kernelsu_branch']['options'], ['正式版', '最新版'])
        self.assertNotIn('models', fields)
        self.assertNotIn('schedule', self.document['on'])

    def test_single_kernel_menu_is_generated_from_reviewed_platform_family_catalog(self):
        field = self.document['on']['workflow_dispatch']['inputs']['build_target']
        self.assertEqual(field['type'], 'choice')
        self.assertEqual(field['default'], '全部内核')
        self.assertEqual(field['options'], cctv.target_choices(cctv.load_profiles(ROOT)))
        self.assertIn('骁龙 · 6.1.141', field['options'])
        self.assertIn('骁龙 · 6.6.89', field['options'])
        self.assertIn('天玑 · 6.6.89', field['options'])
        self.assertEqual(len(field['options']), len(set(field['options'])))
        self.assertFalse(any(re.search(r'\b(?:sm|mt)\d{4}\b', label) for label in field['options']))
        self.assertIn('inputs.build_target', self.document['run-name'])

    def test_every_expression_uses_declared_menu_and_no_remote_workflow(self):
        text = json.dumps(self.document)
        expressions = re.findall(r'\$\{\{(.*?)\}\}', text)
        conditions = [job.get('if', '') for job in self.document['jobs'].values()]
        conditions += [step.get('if', '') for job in self.document['jobs'].values() for step in job.get('steps', [])]
        fields = set(re.findall(r'\binputs\.(\w+)', ' '.join(expressions + conditions)))
        self.assertFalse(fields - set(workflow.INPUTS), fields)
        self.assertNotIn('inputs.operation', text)
        self.assertIn('inputs.kernelsu_branch', text)
        self.assertTrue(all('uses' not in job for job in self.document['jobs'].values()))
        self.assertNotIn('workflow_call', self.document['on'])

    def test_kernel_only_ecs_installation_and_current_run_statistics_keep_manager_flow(self):
        steps = self.document['jobs']['build-oplus-kernel']['steps']
        install = next(step for step in steps if step.get('name') == '安装固定 ECS 编译缓存工具')
        restore = next(step for step in steps if step.get('id') == 'oplus-ccache')
        start = next(step for step in steps if step.get('id') == 'oplus-cache-measurement')
        build = next(step for step in steps if step.get('id') == 'oplus-build')
        stats = next(step for step in steps if step.get('name') == '限制缓存容量并显示命中情况')
        self.assertIn('tools/kernel_compile_cache.py install', install['run'])
        self.assertLess(steps.index(install), steps.index(restore))
        self.assertLess(steps.index(restore), steps.index(start))
        self.assertLess(steps.index(start), steps.index(build))
        self.assertIn('ccache --zero-stats', start['run'])
        self.assertIn("steps.oplus-cache-measurement.outputs.started == 'true'", stats['if'])
        self.assertIn('"$GITHUB_STEP_SUMMARY"', stats['run'])
        self.assertIn('oplus-ccache-ecs-v1-', restore['with']['key'])
        self.assertIn("hashFiles('config/kernel-cache-tools.json', 'tools/kernel_compile_cache.py')", restore['with']['key'])
        for name in ('build-sukisu-manager', 'build-sukisu-lkm'):
            self.assertNotIn('kernel_compile_cache.py', json.dumps(self.document['jobs'][name]))

    def test_base_gki_pipeline_is_preserved_and_manager_lkm_are_shared(self):
        before = copy.deepcopy(self.base)
        workflow.create_workflow(ROOT, self.base)
        self.assertEqual(self.base, before)
        for name in ('build-sukisu-manager', 'build-sukisu-lkm'):
            expected = workflow.adapt(copy.deepcopy(self.base['jobs'][name]))
            if name == 'build-sukisu-lkm':
                from additional_manager_jobs import ALL_IF
                expected['if'] = workflow.DISPATCH + ' && ' + ALL_IF
            self.assertEqual(self.document['jobs'][name], expected)
        manager = self.document['jobs']['build-sukisu-manager']
        self.assertIn('build-sukisu-lkm', manager['needs'])
        self.assertTrue(any('verify_manager_boot_patch.py' in item.get('run', '') for item in manager['steps']))
        upload = next(item for item in manager['steps'] if item.get('name') == '上传管理器安装包')
        self.assertIs(upload['with']['archive'], False)

    def test_official_catalog_and_selected_manager_are_fixed_before_snapshot_and_matrix_jobs(self):
        prepare = self.document['jobs']['prepare-matrix']
        indexed = {item['id']: (index, item) for index, item in enumerate(prepare['steps']) if 'id' in item}
        self.assertEqual(indexed['matrix'][1]['run'], 'python3 tools/oplus_cctv.py --target "$OPLUS_TARGET" --github-output --summary')
        self.assertEqual(indexed['matrix'][1]['env']['OPLUS_TARGET'], '${{ inputs.build_target }}')
        self.assertLess(indexed['matrix'][0], indexed['sources'][0])
        self.assertLess(indexed['sources'][0], indexed['source-snapshot'][0])
        self.assertEqual(indexed['sources'][1]['env']['KSU_TIER'], '${{ inputs.kernelsu_branch }}')
        self.assertEqual(indexed['sources'][1]['env']['CUSTOM_REF'], '')
        self.assertIn('prepare-deps', indexed['oplus-deps'][1]['run'])
        self.assertIn('--unicode-bypass "$UNICODE_BYPASS"', indexed['oplus-deps'][1]['run'])
        self.assertEqual(indexed['oplus-deps'][1]['env']['UNICODE_BYPASS'], '${{ inputs.use_unicode_bypass }}')
        for key in ('ak3_ref', 'patches_ref'):
            self.assertEqual(prepare['outputs'][key], '${{ steps.oplus-deps.outputs.' + key + ' }}')
        self.assertNotIn('max_parallel', prepare['outputs'])
        self.assertEqual('${{ steps.matrix.outputs.full_catalog }}', prepare['outputs']['full_catalog'])
        source = indexed['shared-susfs'][1]
        self.assertEqual(source['env']['SUSFS_MATRIX'], '${{ steps.matrix.outputs.matrix }}')
        self.assertIn('inputs.enable_susfs', source['if'])
        self.assertLess(indexed['shared-susfs'][0], indexed['source-snapshot'][0])

    def test_version_choice_inherits_the_gki_default_without_replacing_the_selected_tier(self):
        base = copy.deepcopy(self.base)
        base['on']['workflow_dispatch']['inputs']['kernelsu_branch']['default'] = '正式版'
        document = workflow.create_workflow(ROOT, base)
        self.assertEqual('正式版', document['on']['workflow_dispatch']['inputs']['kernelsu_branch']['default'])
        self.assertEqual('${{ inputs.kernelsu_branch }}', workflow.adapt('${{ inputs.kernelsu_branch }}'))
        prepare = document['jobs']['prepare-matrix']
        self.assertEqual('${{ steps.sources.outputs.effective_ksu_branch }}', prepare['outputs']['effective_ksu_branch'])
        for job_name in ('build-sukisu-lkm', 'build-sukisu-manager'):
            checkout = next(step for step in document['jobs'][job_name]['steps']
                            if 'checkout_ksu_snapshot.py' in step.get('run', ''))
            self.assertEqual('${{ needs.prepare-matrix.outputs.ksu_ref }}', checkout['env']['KSU_REF'])
        worker = document['jobs']['build-oplus-kernel']
        self.assertEqual('${{ needs.prepare-matrix.outputs.ksu_ref }}', worker['env']['KSU_REF'])
        self.assertEqual('${{ needs.prepare-matrix.outputs.effective_ksu_branch }}', worker['env']['KSU_BRANCH'])

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
        self.assertNotIn('--release-id', fallback['run'])
        self.assertNotIn('SOURCE_BACKUP_RELEASE_ID', fallback['env'])
        self.assertIn('--prepared-identity "$PREPARED_SOURCE_IDENTITY"', fallback['run'])
        verification = workflow.source_cache_verify()
        self.assertIn(verification, worker['steps'])
        self.assertLess(worker['steps'].index(fallback), worker['steps'].index(verification))
        bootstrap = next(step for step in worker['steps'] if '--phase bootstrap' in step.get('run', ''))
        self.assertLess(worker['steps'].index(verification), worker['steps'].index(bootstrap))
        self.assertEqual(worker['env']['SUSFS_SOURCES'], '${{ needs.prepare-matrix.outputs.susfs_sources }}')
        self.assertEqual(worker['strategy']['matrix'], '${{ fromJSON(needs.prepare-matrix.outputs.matrix) }}')
        self.assertFalse(worker['strategy']['fail-fast'])
        self.assertIn("startsWith(matrix.platform, 'mt') && '天玑' || '骁龙'", worker['name'])
        self.assertNotIn('matrix.platform }}', worker['name'])
        self.assertEqual(worker['strategy']['max-parallel'], 20)

    def test_native_dependency_and_filemap_behavior_tests_precede_source_preparation(self):
        steps = self.document['jobs']['prepare-matrix']['steps']
        preflight = next(step for step in steps if step['name'] == '验证欧加真源码、构建方法与原生配套校验')
        sources = next(step for step in steps if step.get('id') == 'sources')
        self.assertIn('command -v cc', preflight['run'])
        self.assertIn("-p 'test_oplus_*.py'", preflight['run'])
        self.assertIn("-p 'test_susfs_map_compat.py'", preflight['run'])
        self.assertNotIn('continue-on-error', preflight)
        self.assertLess(steps.index(preflight), steps.index(sources))

    def test_all_eight_manager_modules_finish_exact_cache_retries_before_packaging(self):
        from manager_lkm_jobs import KMIS, lkm_cache_key
        for document in (self.base, self.document):
            steps = document['jobs']['build-sukisu-manager']['steps']
            package = next(i for i, step in enumerate(steps) if 'prepare_manager_lkm.py stage' in step.get('run', ''))
            ids = [step['id'] for step in steps if 'id' in step]
            self.assertEqual(len(ids), len(set(ids)))
            for index, kmi in enumerate(KMIS):
                prefix = 'manager-lkm-' + str(index)
                expected = lkm_cache_key(repr(kmi), f'fromJSON(needs.prepare-matrix.outputs.ddk_matrix).include[{index}].ddk_short')
                attempts = [step for step in steps if step.get('id') in
                            (prefix, prefix + '-retry-1', prefix + '-retry-2')]
                self.assertEqual(3, len(attempts))
                for step in attempts:
                    self.assertEqual(expected, step['with']['key'])
                    self.assertEqual('manager-lkm/' + kmi, step['with']['path'])
                    self.assertIs(step['with']['lookup-only'], False)
                    self.assertNotIn('restore-keys', step['with'])
                gate = next(step for step in steps if step.get('id') == prefix + '-ready')
                self.assertEqual(expected, gate['env']['EXPECTED_CACHE_KEY'])
                self.assertNotIn('continue-on-error', gate)
                self.assertLess(steps.index(attempts[-1]), steps.index(gate))
                self.assertLess(steps.index(gate), package)

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
        self.assertEqual(kpm['if'], workflow.KPM_IF)
        self.assertEqual(worker['steps'][locate('apply_kpm_patch.py prepare')]['if'], workflow.KPM_IF)
        self.assertIn('--ksu-source "$GITHUB_WORKSPACE/kernel_workspace/KernelSU"',
                      worker['steps'][locate('apply_kpm_patch.py prepare')]['run'])
        self.assertEqual('inputs.use_kpm', workflow.KPM_IF)
        self.assertNotIn('effective_ksu_branch', workflow.KPM_IF)
        upload = next(item for item in worker['steps'] if item.get('uses') == 'actions/upload-artifact@v7')
        self.assertIs(upload['with']['archive'], False)
        self.assertNotIn('name', upload['with'])
        self.assertEqual(upload['with']['path'], 'oplus-ready/${{ steps.oplus-assets.outputs.file }}')
        self.assertEqual(sum('actions/upload-artifact' in item.get('uses', '') for item in worker['steps']), 1)
        self.assertEqual(worker['steps'][locate('--phase build')]['id'], 'oplus-build')
        cache = next(item for item in worker['steps'] if item.get('uses') == 'actions/cache/save@v5'
                     and item.get('with', {}).get('path') == '${{ env.CCACHE_DIR }}')
        self.assertIn("always() && !cancelled() && steps.oplus-build.outcome == 'success'", cache['if'])
        self.assertNotIn('oplus-assets.outcome', cache['if'])

    def test_worker_uses_reviewed_method_and_official_ak3_and_optional_fixed_unicode(self):
        worker = self.document['jobs']['build-oplus-kernel']
        checkouts = {step.get('with', {}).get('path'): step for step in worker['steps'] if step.get('uses') == 'actions/checkout@v6'}
        self.assertEqual(checkouts['oplus-reference']['with']['ref'], '${{ matrix.recipe_ref }}')
        self.assertEqual(checkouts['oplus-reference']['with']['repository'], '${{ matrix.recipe_repository }}')
        self.assertEqual(checkouts['oplus-ak3']['with']['repository'], cctv.AK3_REPOSITORY)
        self.assertEqual(checkouts['oplus-ak3']['with']['ref'], cctv.AK3_REF)
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
        self.assertIn('bash tools/install_oplus_dependencies.sh', install['run'])
        installation = (ROOT / 'tools/install_oplus_dependencies.sh').read_text('utf-8')
        for setting in ('Acquire::http::Timeout=30', 'Acquire::https::Timeout=30', 'Acquire::Retries=1', 'DPkg::Lock::Timeout=45'):
            self.assertIn(setting, installation)
        for package in ('aria2', 'libdw-dev', 'libelf-dev', 'libssl-dev', 'ccache', 'bison', 'flex', 'zstd', 'bc', 'patch', 'unzip'):
            self.assertIn(package, installation)
        self.assertNotIn('apt purge', installation)
        self.assertNotIn('rm -rf /', json.dumps(worker))
        self.assertEqual(worker['env']['CCACHE_DIR'], '${{ github.workspace }}/.oplus-ccache')
        restore = next(item for item in worker['steps'] if item.get('id') == 'oplus-ccache')
        save = next(item for item in worker['steps'] if item.get('uses') == 'actions/cache/save@v5' and item.get('name') == '保存已成功编译的平台缓存')
        self.assertIn('${{ matrix.id }}', restore['with']['key'])
        self.assertIn('${{ matrix.recipe_ref }}', restore['with']['key'])
        self.assertIn('${{ github.run_id }}-${{ github.run_attempt }}', restore['with']['key'])
        self.assertEqual(save['with']['key'], '${{ steps.oplus-ccache.outputs.cache-primary-key }}')
        self.assertEqual(save['timeout-minutes'], 2)
        self.assertIn("steps.oplus-build.outcome == 'success'", save['if'])

    def test_profile_and_selected_input_heredoc_really_validates_and_writes_json(self):
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

    def test_native_aggregation_verifies_real_signed_apk_ak3_and_outputs_only_reports(self):
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
                exec(compile(heredoc(workflow.NATIVE_ASSETS), '<stage verified native originals>', 'exec'), {})
            finally:
                os.chdir(previous)
            self.assertEqual(result['count'], 1)
            reports = fixture.directory / 'oplus-report'
            self.assertEqual({path.name for path in reports.iterdir()}, {'SHA256SUMS.txt', 'summary.md', 'title.txt'})
            checksums = (reports / 'SHA256SUMS.txt').read_text()
            self.assertIn(fixture.manager['file'], checksums)
            self.assertIn('Oplus-Snapdragon-android14-6.1.57-SukiSU-AnyKernel3.zip', checksums)
            self.assertFalse((fixture.directory / 'oplus-verified-assets').exists())
            from transfer_ksu_snapshot import native_assets
            staged = native_assets((fixture.directory / 'oplus-publish/assets').resolve())
            self.assertEqual(set(staged), {fixture.manager['file'], 'SHA256SUMS.txt',
                                          'Oplus-Snapdragon-android14-6.1.57-SukiSU-AnyKernel3.zip'})
            self.assertNotIn('title.txt', staged)
            self.assertNotIn('summary.md', staged)
        finally:
            fixture.doCleanups()
            OplusAssetsTests.tearDownClass()

    def test_collect_publishes_only_after_complete_native_matrix_verification(self):
        aggregate = self.document['jobs']['verify-oplus-assets']
        self.assertIn("needs.build-oplus-kernel.result == 'success'", aggregate['if'])
        self.assertIn("needs.prepare-matrix.result == 'success'", aggregate['if'])
        self.assertIn("needs.build-sukisu-manager.result == 'success'", aggregate['if'])
        self.assertEqual(aggregate['permissions'], {'contents': 'write', 'actions': 'write'})
        download = next(item for item in aggregate['steps'] if item.get('uses') == 'actions/download-artifact@v8')
        self.assertIs(download['with']['skip-decompress'], True)
        self.assertIs(download['with']['merge-multiple'], False)
        self.assertIn('Oplus-*-AnyKernel3.zip', download['with']['pattern'])
        collect = next(item for item in aggregate['steps'] if item.get('id') == 'assets')
        self.assertIn('--variants "$KSU_VARIANT"', collect['run'])
        self.assertEqual(collect['env']['OPLUS_MATRIX'], '${{ needs.prepare-matrix.outputs.matrix }}')
        steps = aggregate['steps']
        upload = next(item for item in steps if item.get('uses') == 'actions/upload-artifact@v7')
        self.assertLess(steps.index(collect), steps.index(upload))
        self.assertEqual(upload['with']['path'], 'oplus-report/SHA256SUMS.txt')
        self.assertIs(upload['with']['archive'], False)
        self.assertNotIn('publish-release', self.document['jobs'])
        expected = workflow.release_publish_steps("success() && needs.prepare-matrix.outputs.full_catalog == 'true'", assets_directory='oplus-publish/assets',
                                                   notes_file='oplus-report/summary.md', title_file='oplus-report/title.txt')
        publish_steps = [step for step in steps if step.get('id') in ('release-tag', 'release')
                         or 'cleanup_build_artifacts.py' in step.get('run', '')]
        self.assertEqual(publish_steps, expected)
        self.assertLess(steps.index(upload), steps.index(publish_steps[0]))
        self.assertIn('--notes op', publish_steps[0]['run'])
        self.assertIn('oplus-publish/assets/*', publish_steps[1]['run'])
        self.assertIn('--release-tag "$RELEASE_TAG"', publish_steps[-1]['run'])
        self.assertNotIn('transfer_ksu_snapshot.py create', json.dumps(self.document))
        self.assertNotIn('source_backup_release_id', json.dumps(self.document))
        self.assertNotIn('delete-actions', json.dumps(self.document))
        self.assertIn('native_assets(output.resolve())', collect['run'])
        self.assertNotIn('oplus-verified-assets', collect['run'])
        summary = steps[-1]
        self.assertIn('oplus-report/summary.md', summary['run'])
        self.assertIn("steps.assets.outcome == 'success'", summary['if'])

    def test_generated_fourth_workflow_is_reproducible_yaml_with_boolean_on(self):
        current = generator.load_yaml(ROOT / workflow.OUTPUT)
        from combined_manager_workflow import add_combined
        self.assertEqual(current, add_combined(self.document, oplus=True)[0])
        self.assertIs(current['on']['workflow_dispatch']['inputs']['enable_susfs']['default'], True)
        self.assertIs(current['jobs']['build-oplus-kernel']['strategy']['fail-fast'], False)


if __name__ == '__main__':
    unittest.main()
