import copy
import fnmatch
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import generate_workflow as generator


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = generator.create_workflow(ROOT)
        cls.steps = cls.workflow['jobs']['build-kernel']['steps']

    def find(self, name):
        return next(step for step in self.steps if step.get('name') == name)

    def test_one_dispatch_and_no_remote_workflow_calls(self):
        self.assertEqual(list(self.workflow['on']), ['workflow_dispatch', 'schedule'])
        self.assertEqual(len(self.workflow['on']['workflow_dispatch']['inputs']), 25)
        self.assertTrue(all('uses' not in job for job in self.workflow['jobs'].values()))

    def test_real_boot_patch_check_must_pass_before_signing_and_upload(self):
        steps = self.workflow['jobs']['build-sukisu-manager']['steps']
        index = next(i for i, step in enumerate(steps)
                     if 'tools/verify_manager_boot_patch.py' in step.get('run', ''))
        verify = steps[index]
        self.assertNotIn('continue-on-error', verify)
        self.assertNotIn('if', verify)
        self.assertIn('--module-report "$RUNNER_TEMP/manager-lkm-stage.json"', verify['run'])
        self.assertLess(next(i for i, step in enumerate(steps) if step.get('id') == 'manager-build'), index)
        self.assertLess(index, next(i for i, step in enumerate(steps) if step.get('id') == 'sign'))
        self.assertLess(index, next(i for i, step in enumerate(steps) if step.get('name') == '上传 SukiSU 安装包'))

    def test_sukisu_release_choices_preserve_old_dispatch_and_share_canonical_tier(self):
        fields = self.workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(fields['kernelsu_variant']['default'], 'SukiSU')
        self.assertEqual(fields['kernelsu_branch']['default'], 'SukiSU 最新版')
        self.assertEqual(fields['kernelsu_branch']['options'],
                         ['Stable(标准)', 'Dev(开发)', 'Latest(最新)', 'Custom(自定义)',
                          'SukiSU 正式版', 'SukiSU 最新版'])
        kernel = self.workflow['jobs']['build-kernel']
        self.assertNotIn('inputs.kernelsu_branch', str(kernel))
        canonical = '${{ needs.prepare-matrix.outputs.effective_ksu_branch }}'
        self.assertEqual(self.find('选择配套的 SUSFS 源码版本')['env']['PIN_KSU_TIER'], canonical)
        self.assertEqual(self.find('记录内核实际配置')['env']['META_KSU_BRANCH'], canonical)
        summary = self.find('构建信息摘要')['run']
        self.assertIn('effective_ksu_branch="' + canonical + '"', summary)
        # Alias labels reach only the resolver; every template branch receives
        # its canonical value, preserving BakaSU's Stable/Custom-only KPM rule.
        kpm = next(line for line in summary.splitlines() if 'KPM 功能' in line)
        self.assertIn("inputs.kernelsu_variant == 'BakaSU'", kpm)
        for tier in ('Stable(标准)', 'Custom(自定义)'):
            self.assertIn("needs.prepare-matrix.outputs.effective_ksu_branch == '" + tier + "'", kpm)
        self.assertNotIn("== 'Latest(最新)'", kpm)
        self.assertNotIn("== 'Dev(开发)'", kpm)

    def test_update_events_cannot_start_kernel_compilation(self):
        self.assertEqual(self.workflow['jobs']['prepare-matrix']['if'], generator.PREPARE_IF)
        self.assertEqual(self.workflow['jobs']['build-kernel']['if'], generator.KERNEL_JOB_IF)
        self.assertIn(generator.BUILD_IF, generator.KERNEL_JOB_IF)
        self.assertIn("needs.prepare-matrix.result == 'success'", generator.KERNEL_JOB_IF)
        self.assertIn("needs.build-sukisu-manager.result == 'success'", generator.KERNEL_JOB_IF)
        self.assertIn("github.event_name == 'workflow_dispatch'", generator.BUILD_IF)
        self.assertIn("inputs.operation == '编译内核'", generator.BUILD_IF)
        self.assertEqual(self.workflow['on']['schedule'], [{'cron': '17 0 * * *'}])
        operation = self.workflow['on']['workflow_dispatch']['inputs']['operation']
        self.assertEqual(operation['default'], '编译内核')
        self.assertEqual(operation['options'], ['编译内核', '仅编译 SukiSU 安装包', '检查上游更新', '清理历史编译（保留5次）'])
        self.assertNotIn('仅编译 SukiSU 安装包', generator.BUILD_IF)
        self.assertNotIn('schedule', generator.PREPARE_IF)
        self.assertEqual(self.workflow['jobs']['prepare-matrix']['steps'][1]['if'], generator.BUILD_IF)

    def test_history_cleanup_requires_explicit_default_branch_manual_operation(self):
        cleanup = self.workflow['jobs']['cleanup-history']
        self.assertEqual(cleanup['if'], "github.event_name == 'workflow_dispatch' && inputs.operation == '清理历史编译（保留5次）' && github.ref_name == github.event.repository.default_branch")
        self.assertEqual(cleanup['permissions'], {'contents': 'read', 'actions': 'write'})
        self.assertIs(cleanup['concurrency']['cancel-in-progress'], False)
        self.assertNotIn('needs', cleanup)
        checkout, execute = cleanup['steps']
        self.assertIs(checkout['with']['persist-credentials'], False)
        self.assertEqual(execute['env'], {'GH_TOKEN': '${{ github.token }}'})
        self.assertIn('--keep 5 --execute', execute['run'])
        self.assertIn('--summary "$GITHUB_STEP_SUMMARY"', execute['run'])
        self.assertNotIn('schedule', cleanup['if'])
        self.assertNotIn('upload-artifact', str(cleanup))

    def test_updater_uses_default_branch_and_no_persistent_checkout_token(self):
        job = self.workflow['jobs']['check-upstream']
        self.assertIn('github.ref_name == github.event.repository.default_branch', job['if'])
        self.assertEqual(job['permissions'], {'contents': 'read', 'pull-requests': 'write'})
        checkout = job['steps'][0]
        self.assertIs(checkout['with']['persist-credentials'], False)
        self.assertEqual(checkout['with']['ref'], '${{ github.event.repository.default_branch }}')
        self.assertIs(job['concurrency']['cancel-in-progress'], False)

    def test_updater_validates_before_publishing_and_skips_existing_candidate(self):
        steps = self.workflow['jobs']['check-upstream']['steps']
        indexed = {step['id']: (i, step) for i, step in enumerate(steps) if 'id' in step}
        self.assertLess(indexed['guard'][0], indexed['update'][0])
        self.assertLess(indexed['update'][0], indexed['pr'][0])
        self.assertIn("steps.guard.outputs.already_pending != 'true'", indexed['update'][1]['if'])
        pr = indexed['pr'][1]
        self.assertEqual(pr['if'], "steps.update.outputs.has_updates == 'true'")
        self.assertEqual(pr['with']['branch'], 'automation/upstream-sync')
        self.assertEqual(pr['with']['token'], '${{ secrets.UPSTREAM_SYNC_TOKEN }}')
        self.assertNotIn('tools/', pr['with']['add-paths'])
        self.assertNotIn('tests/', pr['with']['add-paths'])
        self.assertEqual(pr['with']['add-paths'], 'data/kernels.json\ndata/upstream-candidates.json\n')
        sync = indexed['sync'][1]
        self.assertLess(indexed['pr'][0], indexed['sync'][0])
        self.assertIn('tools/auto_merge_official_update.py', sync['run'])
        update = indexed['update'][1]
        self.assertEqual(update['env']['OFFICIAL_CANDIDATES'], '${{ steps.official.outputs.candidates_json }}')
        self.assertEqual(update['env']['CATALOG_CANDIDATES'], '${{ steps.catalog.outputs.candidates_json }}')
        self.assertIn('--candidates-json "$OFFICIAL_CANDIDATES"', update['run'])
        self.assertIn('--apply-candidates-json "$CATALOG_CANDIDATES"', update['run'])
        self.assertNotIn('sync_upstream.py', str(steps))
        self.assertNotIn('xingguangcuican6666', str(steps))

    def test_auto_sync_handles_pending_and_new_pr_without_manual_review_reminders(self):
        steps = self.workflow['jobs']['check-upstream']['steps']
        step = next(step for step in steps if step.get('id') == 'sync')
        self.assertEqual(step['env']['GH_TOKEN'], '${{ secrets.UPSTREAM_SYNC_TOKEN }}')
        self.assertIn("steps.pr.outputs.pull-request-number != ''", step['if'])
        self.assertIn("steps.guard.outputs.already_pending == 'true'", step['if'])
        self.assertIn('--pull-request "$number" --base "$DEFAULT_BRANCH"', step['run'])
        self.assertNotIn('issues/$PR_NUMBER/comments', str(steps))
        self.assertNotIn('assignees', next(item for item in steps if item.get('id') == 'pr')['with'])
        summary = next(item for item in steps if item['name'] == '记录检查结果')
        self.assertEqual(summary['env']['SYNC_MERGED'], '${{ steps.sync.outputs.merged }}')
        self.assertIn('无需手动合并', summary['run'])

    def test_catalog_cache_survives_partial_failures_and_final_status_is_explicit(self):
        steps = self.workflow['jobs']['check-upstream']['steps']
        catalog = next(step for step in steps if step.get('id') == 'catalog')
        official = next(step for step in steps if step.get('id') == 'official')
        self.assertIs(catalog['continue-on-error'], True)
        self.assertIs(official['continue-on-error'], True)
        self.assertIn('--snapshot-file "$RUNNER_TEMP/gki-kernel-catalog/refs.json"', catalog['run'])
        restore = next(step for step in steps if step.get('uses') == 'actions/cache/restore@v5')
        save = next(step for step in steps if step.get('uses') == 'actions/cache/save@v5')
        self.assertEqual(restore['with']['path'], save['with']['path'])
        self.assertEqual(restore['with']['key'], save['with']['key'])
        self.assertEqual(save['if'], 'always() && !cancelled()')
        self.assertIn('restore-keys', restore['with'])
        final = steps[-1]
        self.assertIn("steps.catalog.outcome == 'failure'", final['if'])
        self.assertIn("steps.official.outcome == 'failure'", final['if'])
        self.assertIn('exit 1', final['run'])
        self.assertGreater(steps.index(final), next(i for i, step in enumerate(steps) if step.get('id') == 'pr'))

    def test_partial_probe_keeps_successful_source_candidates_and_defaults_failed_source(self):
        steps = self.workflow['jobs']['check-upstream']['steps']
        probe = next(step for step in steps if step.get('id') == 'probe')
        code = probe['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        directory = ROOT / 'tests' / ('.workflow-probe-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        for official, catalog, changed in (('success', 'failure', True), ('failure', 'success', True),
                                           ('failure', 'failure', False), ('success', 'success', False)):
            with self.subTest(official=official, catalog=catalog, changed=changed):
                output = directory / uuid.uuid4().hex
                environment = {'GITHUB_OUTPUT': str(output), 'OFFICIAL_RESULT': official, 'CATALOG_RESULT': catalog,
                               'OFFICIAL_CHANGED': str(changed).lower(), 'CATALOG_CHANGED': str(changed).lower(),
                               'OFFICIAL_CANDIDATES': '{}' if official == 'success' else '',
                               'CATALOG_CANDIDATES': '[]' if catalog == 'success' else ''}
                with patch.dict(os.environ, environment):
                    exec(compile(code, '<official probe>', 'exec'), {})
                values = dict(line.split('=', 1) for line in output.read_text().splitlines())
                self.assertEqual(values['has_updates'], str(changed).lower())
                self.assertEqual(values['partial_failure'], str(official != 'success' or catalog != 'success').lower())
                self.assertRegex(values['heads_marker'], r'^<!-- gki-official-heads:sha256=[0-9a-f]{64} -->$')

    def test_partial_prepare_body_reports_failed_catalog_without_parsing_empty_json(self):
        step = next(step for step in self.workflow['jobs']['check-upstream']['steps'] if step.get('id') == 'update')
        self.assertIn('if [ "$OFFICIAL_RESULT" = success ]; then', step['run'])
        self.assertIn('if [ "$CATALOG_RESULT" = success ]; then', step['run'])
        code = step['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        directory = ROOT / 'tests' / ('.workflow-partial-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        output = directory / 'output'
        environment = {'RUNNER_TEMP': str(directory), 'GITHUB_OUTPUT': str(output),
                       'OFFICIAL_RESULT': 'success', 'CATALOG_RESULT': 'failure',
                       'HEADS_MARKER': '<!-- gki-official-heads:sha256=' + 'a' * 64 + ' -->',
                       'OFFICIAL_CANDIDATES': '{}', 'CATALOG_CANDIDATES': ''}
        with patch.dict(os.environ, environment):
            exec(compile(code, '<official update body>', 'exec'), {})
        body = (directory / 'official-update.md').read_text(encoding='utf-8')
        self.assertIn('Android GKI：检查失败，未确认有无更新', body)
        self.assertIn('不能视为全部官方源已检查', body)

    def test_custom_ksu_ref_remains_available_through_repository_variable(self):
        sources = next(step for step in self.workflow['jobs']['prepare-matrix']['steps'] if step.get('id') == 'sources')
        self.assertEqual(sources['env']['CUSTOM_REF'], '${{ vars.KSU_CUSTOM_REF }}')
        self.assertIn('tools/resolve_build_sources.py', sources['run'])
        step = self.find('确定 KernelSU 分支')
        self.assertEqual(step['env']['BRANCH'], '${{ needs.prepare-matrix.outputs.ksu_ref }}')
        self.assertNotIn('resolve-ksu-ref.sh', step['run'])

    def test_sukisu_manager_is_built_once_and_kernel_uses_its_signature(self):
        manager = self.workflow['jobs']['build-sukisu-manager']
        self.assertEqual(manager['needs'], ['prepare-matrix', 'build-sukisu-lkm'])
        self.assertNotIn('strategy', manager)
        self.assertEqual(manager['if'], generator.MANAGER_IF)
        checkout = next(step for step in manager['steps'] if step['name'] == '检出同一版本的 SukiSU 源码')
        self.assertIn('checkout_ksu_snapshot.py', checkout['run'])
        self.assertEqual(checkout['env']['KSU_REF'], '${{ needs.prepare-matrix.outputs.ksu_ref }}')
        self.assertNotIn('uses', checkout)
        snapshot = next(step for step in manager['steps']
                        if step.get('with', {}).get('path') == 'ksu-source-bundle')
        self.assertEqual(snapshot['uses'], 'actions/cache/restore@v5')
        self.assertEqual(snapshot['with']['key'], '${{ needs.prepare-matrix.outputs.source_cache_key }}')
        self.assertFalse(snapshot['with']['fail-on-cache-miss'])
        self.assertTrue(snapshot['continue-on-error'])
        build = next(step for step in manager['steps'] if step['name'] == '编译管理器与同版本 ksud')
        self.assertEqual(build['env']['KSU_PACKAGE_NAME'], build['env']['ORG_GRADLE_PROJECT_KSU_PACKAGE_NAME'])
        self.assertEqual(build['env']['KSU_PACKAGE_NAME'], 'com.cyzrqh.gki.sukisu')
        self.assertEqual(build['env']['ORG_GRADLE_PROJECT_KSU_NAME'], 'SukiSU')
        self.assertNotIn('secrets.', str(build))
        sign = next(step for step in manager['steps'] if step.get('id') == 'sign')
        self.assertIn('secrets.SUKISU_APK_KEYSTORE', sign['env']['SUKISU_APK_KEYSTORE'])
        self.assertIn('--build-metadata "$RUNNER_TEMP/manager-build/manager-build-metadata.json"', sign['run'])
        kernel = self.workflow['jobs']['build-kernel']
        self.assertEqual(kernel['needs'], ['prepare-matrix', 'build-sukisu-lkm', 'build-sukisu-manager'])
        self.assertEqual(kernel['permissions'], {'contents': 'read', 'actions': 'read'})
        wait = self.find('编译前读取本次 APK 的实际签名信息')
        self.assertEqual(wait['if'], "inputs.kernelsu_variant == 'SukiSU'")
        self.assertIn('wait_manager_identity.py', wait['run'])
        self.assertIn('--expected-ref "$KSU_REF"', wait['run'])
        self.assertIn('--github-env "$GITHUB_ENV"', wait['run'])
        self.assertNotIn('Manager-Identity-', str(manager))
        self.assertIn('--artifact-name "$MANAGER_ARTIFACT"', wait['run'])
        self.assertEqual(wait['env']['MANAGER_ARTIFACT'], '${{ needs.build-sukisu-manager.outputs.apk_filename }}')
        self.assertIn('KSU_REF', sign['env'])
        upload = next(step for step in manager['steps'] if step['name'] == '上传 SukiSU 安装包')
        self.assertEqual(upload['uses'], 'actions/upload-artifact@v7')
        self.assertIs(upload['with']['archive'], False)
        self.assertEqual(upload['with']['path'], 'manager-artifact/${{ steps.sign.outputs.apk_filename }}')
        self.assertNotIn('name', upload['with'])
        collection = next(step for step in self.workflow['jobs']['publish-release']['steps']
                          if step['name'] == '收集本次构建产物')
        self.assertEqual(collection['with']['pattern'],
                         "{${{ needs.build-sukisu-manager.outputs.apk_filename || '__NO_MANAGER_APK__' }},*-AnyKernel3.zip,*-boot*.img}")
        signature = self.find('配对管理器包名与签名')
        self.assertNotIn('env', signature)  # Use the verified previous step's GITHUB_ENV values.
        self.assertIn('--package "$MANAGER_PACKAGE"', signature['run'])
        names = [step.get('name') for step in self.steps]
        self.assertLess(names.index('初始化并同步内核源码'), names.index(wait['name']))
        self.assertLess(names.index(wait['name']), names.index(signature['name']))
        self.assertLess(names.index(signature['name']), names.index('编译内核'))
        self.assertIn("needs.build-sukisu-manager.result == 'success'", kernel['if'])

    def test_twenty_kernel_jobs_cannot_starve_their_apk_producer(self):
        kernels = self.workflow['jobs']['build-kernel']
        manager = self.workflow['jobs']['build-sukisu-manager']
        self.assertIn('build-sukisu-manager', kernels['needs'])
        self.assertNotIn('build-kernel', manager['needs'])
        self.assertEqual(20, self.workflow['on']['workflow_dispatch']['inputs']['max_parallel']['default'])
        for variant in ('SukiSU', 'None', 'KernelSU', 'BakaSU'):
            for producer in ('success', 'failure', 'cancelled', 'skipped'):
                with self.subTest(variant=variant, producer=producer):
                    values = {
                        'always()': 'True', 'cancelled()': 'False',
                        'needs.prepare-matrix.result': repr('success'),
                        'needs.build-sukisu-lkm.result': repr('success' if variant == 'SukiSU' else 'skipped'),
                        'needs.build-sukisu-manager.result': repr(producer),
                        'github.event_name': repr('workflow_dispatch'),
                        'inputs.operation': repr('编译内核'), 'inputs.kernelsu_variant': repr(variant),
                    }
                    expression = kernels['if']
                    for name, value in values.items():
                        expression = expression.replace(name, value)
                    expression = expression.replace('&&', ' and ').replace('||', ' or ').replace('!False', 'not False')
                    allowed = bool(eval(expression, {'__builtins__': {}}, {}))
                    self.assertEqual(variant != 'SukiSU' or producer == 'success', allowed)

    def test_release_rerun_collects_only_this_attempts_signed_manager_filename(self):
        collection = next(step for step in self.workflow['jobs']['publish-release']['steps']
                          if step['name'] == '收集本次构建产物')
        expression = "${{ needs.build-sukisu-manager.outputs.apk_filename || '__NO_MANAGER_APK__' }}"
        current = 'SukiSU_v4.2.1-r1_41050-release.apk'
        stale = 'SukiSU_v4.2.0_40922-release.apk'
        kernel = 'android14-6.1.145-2025-08-AnyKernel3.zip'
        boot = 'android14-6.1.145-2025-08-boot-lz4.img'
        for selected in (current, ''):
            with self.subTest(manager=selected):
                pattern = collection['with']['pattern'].replace(expression, selected or '__NO_MANAGER_APK__')
                alternatives = pattern[1:-1].split(',')
                matches = {name for name in (current, stale, kernel, boot)
                           if any(fnmatch.fnmatchcase(name, item) for item in alternatives)}
                self.assertEqual({kernel, boot} | ({current} if selected else set()), matches)
                self.assertNotIn('*.apk', alternatives)

    def test_release_keeps_raw_ak3_zip_and_requires_every_selected_kernel(self):
        steps = self.workflow['jobs']['publish-release']['steps']
        collection = next(step for step in steps if step['name'] == '收集本次构建产物')
        self.assertIs(collection['with']['skip-decompress'], True)
        preparation = next(step for step in steps if step['name'] == '整理直接下载文件与实际配置')
        self.assertEqual(preparation['env']['EXPECTED_KERNEL_COUNT'],
                         "${{ inputs.operation == '仅编译 SukiSU 安装包' && '0' || needs.prepare-matrix.outputs.count }}")
        self.assertIn('--expected-kernel-count "$EXPECTED_KERNEL_COUNT"', preparation['run'])
        self.assertEqual(self.workflow['jobs']['prepare-matrix']['outputs']['count'],
                         '${{ steps.matrix.outputs.count }}')

    def test_sukisu_source_is_frozen_and_checked_before_any_kernel_sync(self):
        prepare = self.workflow['jobs']['prepare-matrix']['steps']
        checkout = next(step for step in prepare if step['name'] == '仅下载一次官方 SukiSU 源码')
        self.assertEqual(checkout['with']['ref'], '${{ steps.sources.outputs.ksu_ref }}')
        self.assertEqual(checkout['with']['fetch-depth'], 0)
        self.assertIs(checkout['with']['persist-credentials'], False)
        check = next(step for step in prepare if step['name'] == '展开矩阵前验证并固定源码快照')
        publish = next(step for step in prepare if step['name'] == '保存本次不可变源码缓存')
        self.assertIn('snapshot_ksu_source.py', check['run'])
        self.assertLess(prepare.index(checkout), prepare.index(check))
        self.assertLess(prepare.index(check), prepare.index(publish))
        self.assertEqual(publish['uses'], 'actions/cache/save@v5')
        self.assertEqual(publish['with']['path'], 'ksu-source-bundle')
        self.assertEqual(publish['with']['key'], '${{ steps.source-snapshot.outputs.source_cache_key }}')
        self.assertIn('--output "$GITHUB_WORKSPACE/ksu-source-bundle"', check['run'])
        names = [step['name'] for step in self.steps]
        self.assertLess(names.index('检出内核配套的 SukiSU 快照'), names.index('初始化并同步内核源码'))
        setup = self.find('添加 KernelSU')['run']
        suki = setup.split('"SukiSU")', 1)[1].split('"BakaSU")', 1)[0]
        self.assertIn('integrate_sukisu.py', suki)
        for unstable in ('git clone', 'git pull', 'curl ', 'setup.sh'):
            self.assertNotIn(unstable, suki)

    def test_public_files_are_never_uploaded_with_duplicate_release_wrappers(self):
        for job in self.workflow['jobs'].values():
            for step in job.get('steps', []):
                if not step.get('uses', '').startswith('actions/upload-artifact@'):
                    continue
                self.assertFalse(str(step.get('with', {}).get('name', '')).startswith(
                    ('Source-', 'Release-', 'Manager-Identity-')))
                if step['name'] == '上传补丁冲突文件':
                    self.assertIn('failure()', step['if'])
                    self.assertEqual('patch-rejects/', step['with']['path'])
                else:
                    self.assertIs(step['with'].get('archive'), False,
                                  'Only final raw files and explicit reject diagnostics may be uploaded')
        self.assertNotIn('actions/upload-artifact', str(self.workflow['jobs']['build-sukisu-lkm']))
        manager = self.workflow['jobs']['build-sukisu-manager']
        public = [step for step in manager['steps'] if step.get('uses', '').startswith('actions/upload-artifact@')]
        self.assertEqual(len(public), 1)
        self.assertIs(public[0]['with']['archive'], False)
        modules = self.workflow['jobs']['build-sukisu-lkm']
        self.assertEqual(modules['needs'], 'prepare-matrix')
        self.assertIn('build-sukisu-lkm', manager['needs'])
        self.assertIn('build-sukisu-lkm', self.workflow['jobs']['build-kernel']['needs'])
        self.assertNotIn('build-kernel', modules['needs'])

    def test_source_cache_producer_and_all_consumers_share_exact_path_and_prepared_key(self):
        prepare = self.workflow['jobs']['prepare-matrix']
        snapshot = next(step for step in prepare['steps'] if step.get('id') == 'source-snapshot')
        save = next(step for step in prepare['steps'] if step['name'] == '保存本次不可变源码缓存')
        lookup = next(step for step in prepare['steps'] if step['name'] == '确认所有构建可读取本次源码缓存')
        self.assertEqual('${{ steps.source-snapshot.outputs.source_cache_key }}', prepare['outputs']['source_cache_key'])
        self.assertLess(prepare['steps'].index(snapshot), prepare['steps'].index(save))
        self.assertLess(prepare['steps'].index(save), prepare['steps'].index(lookup))
        self.assertEqual(save['with']['key'], lookup['with']['key'])
        self.assertTrue(lookup['with']['lookup-only'])
        self.assertFalse(lookup['with']['fail-on-cache-miss'])
        for name in ('build-sukisu-lkm', 'build-sukisu-manager', 'build-kernel'):
            consumers = [step for step in self.workflow['jobs'][name]['steps']
                         if step.get('with', {}).get('path') == 'ksu-source-bundle']
            self.assertEqual(1, len(consumers), name)
            consumer = consumers[0]
            self.assertEqual('actions/cache/restore@v5', consumer['uses'])
            self.assertEqual('${{ needs.prepare-matrix.outputs.source_cache_key }}', consumer['with']['key'])
            self.assertFalse(consumer['with']['fail-on-cache-miss'])
            self.assertNotIn('restore-keys', consumer['with'])
            self.assertTrue(consumer['continue-on-error'])
            self.assertNotIn('github.run_attempt', str(consumer['with']))
        for step in (save, lookup):
            self.assertEqual('ksu-source-bundle', step['with']['path'])
            self.assertTrue(step['continue-on-error'])
        self.assertNotIn('Source-SukiSU-', str(self.workflow))

    def test_snapshot_backup_survives_cache_misses_and_is_deleted_only_after_public_files_match(self):
        prepare = self.workflow['jobs']['prepare-matrix']
        self.assertEqual(prepare['permissions'], {'contents': 'write'})
        backup = next(step for step in prepare['steps'] if step.get('id') == 'source-backup')
        save = next(step for step in prepare['steps'] if step['name'] == '保存本次不可变源码缓存')
        self.assertLess(prepare['steps'].index(backup), prepare['steps'].index(save))
        self.assertNotIn('continue-on-error', backup)
        self.assertIn('transfer_ksu_snapshot.py create', backup['run'])
        self.assertEqual(prepare['outputs']['source_backup_release_id'],
                         '${{ steps.source-backup.outputs.backup_release_id }}')
        for name in ('build-sukisu-lkm', 'build-sukisu-manager', 'build-kernel'):
            steps = self.workflow['jobs'][name]['steps']
            restore = next(step for step in steps if step.get('id') == 'source-cache')
            fallback = next(step for step in steps if 'transfer_ksu_snapshot.py restore' in step.get('run', ''))
            checkout = next(step for step in steps if 'checkout_ksu_snapshot.py' in step.get('run', ''))
            self.assertLess(steps.index(restore), steps.index(fallback))
            self.assertLess(steps.index(fallback), steps.index(checkout))
            self.assertIn("steps.source-cache.outputs.cache-hit != 'true'", fallback['if'])
            self.assertNotIn('continue-on-error', fallback)
            self.assertEqual(fallback['env']['SOURCE_BACKUP_RELEASE_ID'],
                             '${{ needs.prepare-matrix.outputs.source_backup_release_id }}')
            self.assertIn('--expected-ref "$KSU_REF"', fallback['run'])
            self.assertEqual(fallback['env']['GH_TOKEN'], '${{ secrets.UPSTREAM_SYNC_TOKEN || github.token }}')
        steps = self.workflow['jobs']['publish-release']['steps']
        checked = next(step for step in steps if 'cleanup_build_artifacts.py' in step.get('run', ''))
        cleanup = next(step for step in steps if 'transfer_ksu_snapshot.py delete' in step.get('run', ''))
        self.assertLess(steps.index(checked), steps.index(cleanup))
        self.assertIn('--published-tag "gki-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"', cleanup['run'])

    def test_source_cache_key_is_stored_by_prepare_with_its_original_run_attempt(self):
        snapshot = next(step for step in self.workflow['jobs']['prepare-matrix']['steps']
                        if step.get('id') == 'source-snapshot')
        code = snapshot['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        directory = ROOT / 'tests' / ('.source-cache-key-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        keys = []
        environment = {'RUNNER_OS': 'Linux', 'RUNNER_ARCH': 'X64', 'GITHUB_RUN_ID': '123',
                       'GITHUB_RUN_ATTEMPT': '1', 'KSU_REF': 'a' * 40}
        for changes in ({}, {'GITHUB_RUN_ATTEMPT': '2'}, {'GITHUB_RUN_ID': '124'}, {'KSU_REF': 'b' * 40}):
            output = directory / uuid.uuid4().hex
            with patch.dict(os.environ, {**environment, **changes, 'GITHUB_OUTPUT': str(output)}):
                exec(compile(code, '<source-cache-key>', 'exec'), {})
            keys.append(dict(line.split('=', 1) for line in output.read_text().splitlines())['source_cache_key'])
        self.assertEqual(4, len(set(keys)))
        self.assertEqual('gki-source-v1-Linux-X64-123-1-' + 'a' * 40, keys[0])

    def test_release_contains_direct_files_and_only_runs_after_success(self):
        release = self.workflow['jobs']['publish-release']
        self.assertEqual(release['permissions'], {'contents': 'write', 'actions': 'write'})
        self.assertIn("needs.build-kernel.result == 'success'", release['if'])
        self.assertIn("needs.build-sukisu-manager.result == 'success'", release['if'])
        self.assertIn("inputs.operation == '仅编译 SukiSU 安装包'", release['if'])
        self.assertNotIn('schedule', release['if'])
        self.assertIn("github.event_name == 'workflow_dispatch'", release['if'])
        publish = next(step for step in release['steps'] if step.get('id') == 'release')
        self.assertIn('--notes-file release-publish/release.md', publish['run'])
        self.assertLess(publish['run'].index('--draft --title'), publish['run'].index('gh release upload'))
        self.assertLess(publish['run'].index('gh release upload'), publish['run'].index('--draft=false'))
        self.assertIn('CONFIG_IKCONFIG=y', self.find('在内核中记录实际构建配置')['run'])
        self.assertIn('capture_build_metadata.py', self.find('记录内核实际配置')['run'])
        checksum = next(step for step in release['steps'] if step['name'] == '上传同名校验文件')
        self.assertIs(checksum['with']['archive'], False)
        self.assertEqual(checksum['with']['path'], 'release-publish/assets/SHA256SUMS.txt')
        cleanup = next(step for step in release['steps'] if 'cleanup_build_artifacts.py' in step.get('run', ''))
        self.assertEqual(cleanup['env']['GH_TOKEN'], '${{ github.token }}')

    def test_manager_caches_match_real_cargo_target_and_exclude_signing_outputs(self):
        steps = self.workflow['jobs']['build-sukisu-manager']['steps']
        source = (ROOT / 'tools/build_sukisu_manager.sh').read_text(encoding='utf-8')
        self.assertIn('export CARGO_TARGET_DIR="$source_dir/target"', source)
        self.assertIn('"$CARGO_TARGET_DIR/aarch64-linux-android/release/ksud"', source)
        sign = next(i for i, step in enumerate(steps) if step.get('id') == 'sign')
        upload = next(i for i, step in enumerate(steps) if step.get('name') == '上传 SukiSU 安装包')
        for label in ('Gradle', 'Cargo'):
            restore = next(step for step in steps if step.get('name') == f'恢复管理器 {label} 依赖')
            save = next(step for step in steps if step.get('name') == f'保存管理器 {label} 依赖')
            self.assertEqual(restore['uses'], 'actions/cache/restore@v5')
            self.assertEqual(save['uses'], 'actions/cache/save@v5')
            self.assertEqual(restore['with']['path'], save['with']['path'])
            self.assertIn('always() && !cancelled()', save['if'])
            self.assertGreater(steps.index(save), sign)
            self.assertGreater(steps.index(save), upload)
            for cache in (restore, save):
                self.assertTrue(cache['continue-on-error'])
                self.assertLessEqual(cache['timeout-minutes'], 3)
                self.assertNotIn('secrets.', str(cache))
                for forbidden in ('manager-artifact', 'manager-build', 'RUNNER_TEMP',
                                  '.p12', '.jks', '.keystore', '*.apk', 'build/outputs'):
                    self.assertNotIn(forbidden, cache['with']['path'])
        self.assertEqual(generator.MANAGER_GRADLE_CACHE,
                         '~/.gradle/caches/modules-2\n~/.gradle/wrapper\n')
        self.assertIn('manager-source/target/*/release/deps', generator.MANAGER_CARGO_CACHE)
        self.assertIn('!manager-source/target/**/release/deps/ksud-*', generator.MANAGER_CARGO_CACHE)
        cargo = next(step for step in steps if step.get('id') == 'manager-cargo-cache')
        self.assertIn('steps.manager-rust-version.outputs.version', cargo['with']['key'])
        self.assertIn('ndk29', cargo['with']['key'])
        self.assertLess(next(i for i, step in enumerate(steps) if step.get('id') == 'manager-rust-version'),
                        steps.index(cargo))

    def test_gradle_task_cache_survives_source_changes_without_caching_signing_outputs(self):
        steps = self.workflow['jobs']['build-sukisu-manager']['steps']
        restore = next(step for step in steps if step.get('id') == 'manager-gradle-build-cache')
        save = next(step for step in steps if step.get('name') == '保存管理器 Gradle 编译缓存')
        self.assertEqual(restore['uses'], 'actions/cache/restore@v5')
        self.assertEqual(save['uses'], 'actions/cache/save@v5')
        self.assertEqual(restore['with']['path'], '~/.gradle/caches/build-cache-1')
        self.assertEqual(save['with']['path'], restore['with']['path'])
        for axis in ('runner.os', 'runner.arch', 'github.run_id', 'github.run_attempt'):
            self.assertIn(axis, restore['with']['key'])
        prefix = restore['with']['restore-keys']
        self.assertNotIn('github.run_id', prefix)
        self.assertNotIn('github.run_attempt', prefix)
        self.assertNotIn('ksu_ref', prefix)
        self.assertIn('jdk21-sdk37-ndk29-', prefix)
        self.assertIn('gradle-wrapper.properties', prefix)
        self.assertIn('libs.versions.toml', prefix)
        self.assertIn("steps.manager-build.outcome == 'success'", save['if'])
        self.assertIn('always() && !cancelled()', save['if'])
        self.assertGreater(steps.index(save), next(i for i, step in enumerate(steps)
                                                  if step.get('name') == '上传 SukiSU 安装包'))
        for cache in (restore, save):
            self.assertTrue(cache['continue-on-error'])
            self.assertLessEqual(cache['timeout-minutes'], 3)
            self.assertNotIn('secrets.', str(cache))
        script = (ROOT / 'tools/build_sukisu_manager.sh').read_text(encoding='utf-8')
        self.assertIn('--build-cache assembleRelease', script)
        self.assertNotIn('--configuration-cache', script)

    def test_bazel_cache_is_scoped_bounded_and_saved_even_on_compile_failure(self):
        restore = self.find('恢复 Bazel 磁盘缓存')
        save = self.find('保存 Bazel 磁盘缓存')
        trim = self.find('限制 Bazel 缓存上传体积')
        compile_step = self.find('编译内核')
        self.assertEqual(restore['with']['path'], '${{ env.BAZEL_DISK_CACHE }}')
        self.assertEqual(save['with']['path'], restore['with']['path'])
        for axis in ('matrix.android_version', 'matrix.kernel_version', 'matrix.sub_level',
                     'matrix.os_patch_level', 'github.run_id', 'github.run_attempt'):
            self.assertIn(axis, restore['with']['key'])
        self.assertNotIn('matrix.sub_level', restore['with']['restore-keys'])
        self.assertIn('--disk_cache="$BAZEL_DISK_CACHE"', compile_step['with']['command'])
        self.assertNotIn('bazel-bin', restore['with']['path'])
        self.assertIn('always() && !cancelled()', trim['if'])
        self.assertIn('always() && !cancelled()', save['if'])
        self.assertEqual(int(trim['env']['BAZEL_CACHE_MAX_BYTES']), 2 * 1024**3)
        self.assertLess(self.steps.index(restore), self.steps.index(compile_step))
        self.assertLess(self.steps.index(compile_step), self.steps.index(trim))
        self.assertLess(self.steps.index(trim), self.steps.index(save))

    def test_bazel_cache_trim_preserves_newest_entries_and_never_prunes_other_roots(self):
        import contextlib
        import io
        step = self.find('限制 Bazel 缓存上传体积')
        script = step['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        directory = ROOT / 'tests' / ('.bazel-cache-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        cache = directory / 'gki-bazel-fixture'
        cache.mkdir()
        files = []
        for index in range(3):
            entry = cache / str(index)
            entry.write_bytes(bytes([index]) * 4)
            os.utime(entry, ns=(1_000_000_000 * (index + 1),) * 2)
            files.append(entry)
        environment = {'BAZEL_DISK_CACHE': str(cache), 'RUNNER_TEMP': str(directory),
                       'BAZEL_CACHE_MAX_BYTES': '8', 'GITHUB_OUTPUT': str(directory / 'output')}
        with patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(script, '<bazel cache trim>', 'exec'), {})
        self.assertFalse(files[0].exists())
        self.assertTrue(files[1].is_file())
        self.assertTrue(files[2].is_file())
        self.assertEqual((directory / 'output').read_text().strip(), 'ready=true')
        # A path with the expected name is still refused when its parent is not
        # the dedicated runner temp directory.
        outside = directory / 'another-root'
        outside.mkdir()
        environment['RUNNER_TEMP'] = str(outside)
        with patch.dict(os.environ, environment), self.assertRaisesRegex(SystemExit, 'outside'):
            exec(compile(script, '<bazel cache trim>', 'exec'), {})
        self.assertTrue(files[1].is_file())
        environment['RUNNER_TEMP'] = str(directory)
        environment['BAZEL_CACHE_MAX_BYTES'] = '0'
        with patch.dict(os.environ, environment), self.assertRaisesRegex(SystemExit, 'positive'):
            exec(compile(script, '<bazel cache trim>', 'exec'), {})
        self.assertTrue(files[1].is_file())

    def test_runtime_has_no_abk_network_or_app_dependency(self):
        text = str(self.workflow)
        self.assertNotIn('xingguangcuican6666/ABK', text)
        names = [step.get('name') for step in self.steps]
        self.assertNotIn('准备 ABK 管理器证书元数据', names)
        self.assertNotIn('校验 ABK 管理器桥接', names)

    def test_susfs23_port_precedes_legacy_patches_and_uses_real_preinit(self):
        names = [step.get('name') for step in self.steps]
        port = self.find('应用完整 SukiSU SUSFS 2.3 适配')
        self.assertEqual(port['if'], "env.SUSFS_COMPAT_VERSION == 'v2.3.0'")
        self.assertEqual(names.index(port['name']), names.index('添加 KernelSU') + 1)
        for name in ('确保 KernelSU SUSFS ABI 兼容', '补齐 SukiSU SUSFS 内联 hook 符号', '最终修复 SukiSU/BakaSU 源码兼容'):
            self.assertIn("env.SUSFS_COMPAT_VERSION != 'v2.3.0'", self.find(name)['if'])
        config = self.find('添加 SUSFS 配置')['run']
        self.assertIn('CONFIG_HEADERS_INSTALL=y', config)
        self.assertIn('"${SUSFS_COMPAT_VERSION:-}" != v2.3.0', config)

    def test_susfs_compatibility_checkout_precedes_any_source_patches(self):
        names = [step.get('name') for step in self.steps]
        pin = self.find('选择配套的 SUSFS 源码版本')
        self.assertLess(names.index('克隆依赖仓库'), names.index(pin['name']))
        self.assertLess(names.index(pin['name']), names.index('应用 SUSFS 补丁'))
        self.assertEqual(pin['env']['PIN_KSU_REF'], '${{ env.BRANCH }}')
        self.assertEqual(pin['env']['PIN_KSU_TIER'], '${{ needs.prepare-matrix.outputs.effective_ksu_branch }}')
        self.assertEqual(pin['if'], "inputs.enable_susfs && inputs.kernelsu_variant == 'SukiSU'")

    def test_historical_susfs23_context_is_prepared_and_verified_around_main_patch(self):
        script = self.find('应用 SUSFS 补丁')['run']
        self.assertLess(script.index('--phase prepare'), script.index('patch -p1 < "50_add_susfs'))
        self.assertLess(script.index('ensure_susfs_headers.py'), script.index('--phase finish'))
        self.assertEqual(script.count('SUSFS_VERSION="2.0.0"'), 1)  # Missing-header fallback only.
        self.assertIn('[ "$SUSFS_VERSION" = "2.3.0" ] && [[ "$rel_path" == */fdinfo.c.patch ]]', script)

    def test_susfs_hook_check_runs_after_patches_before_compiler(self):
        names = [step.get('name') for step in self.steps]
        check = self.find('检查 SUSFS 与 KernelSU 接口配套')
        self.assertLess(names.index('最终修复 SukiSU/BakaSU 源码兼容'), names.index(check['name']))
        self.assertLess(names.index(check['name']), names.index('编译内核'))
        self.assertIn('validate_susfs_hooks.py', check['run'])

    def test_ak3_is_default_and_boot_steps_are_gated(self):
        self.assertEqual(self.workflow['on']['workflow_dispatch']['inputs']['artifact_mode']['default'], '仅 AK3')
        for name in ('构建 Boot 镜像 (Android 12)', '构建 Boot 镜像 (Android 13+)'):
            self.assertIn("inputs.artifact_mode != '仅 AK3'", self.find(name)['if'])
        upload = self.find('上传可直接刷入的 AK3 包')
        self.assertEqual(upload['with']['path'], 'release-ready/${{ steps.kernel-assets.outputs.ak3 }}')
        self.assertIs(upload['with']['archive'], False)
        self.assertNotIn('if', upload)
        self.assertEqual(sum(step.get('name') == '上传可直接刷入的 AK3 包' for step in self.steps), 1)
        for step in self.steps:
            if step.get('uses') == 'actions/upload-artifact@v7':
                self.assertIs(step['with']['archive'], False)
                self.assertNotIn('*', step['with']['path'])
                self.assertNotIn('name', step['with'])
        self.assertNotIn('打包签名内核 Bundle', [step['name'] for step in self.steps])

    def test_ak3_preparation_does_not_require_compressed_image(self):
        run = self.find('准备 AK3 内核文件')['run']
        self.assertLess(run.index("if [ \"$ARTIFACT_MODE\" != '仅 AK3' ]"), run.index('Image.lz4'))
        self.assertIn('lz4 -l -12 -f ./Image bootimgs/Image.lz4', run)
        self.assertIn('lz4 -dc bootimgs/Image.lz4 | cmp - ./Image', run)
        self.assertNotIn('$src/Image.lz4', run)

    def test_kpm_uses_verified_explicit_payload_and_all_images_check_entry_before_upload(self):
        names = [step.get('name') for step in self.steps]
        prepare = self.find('编译前核实官方 KPM 工具及真实载荷')
        patch = self.find('应用 KPM 补丁 (SukiSU/BakaSU)')
        entry = self.find('核对最终 ARM64 启动入口')
        self.assertLess(names.index(prepare['name']), names.index('编译内核'))
        self.assertIn('tools/apply_kpm_patch.py" prepare', prepare['run'])
        self.assertIn('tools/apply_kpm_patch.py" patch', patch['run'])
        self.assertNotIn('patch_linux', patch['run'])
        self.assertNotIn('./patch', patch['run'])
        self.assertIn('--cache-dir "$KPM_CACHE_DIR"', patch['run'])
        # Match the compiler's selected build system, including older trees
        # that may still carry build.sh regardless of their Android label.
        condition = 'if [ -f "$KERNEL_ROOT/build/build.sh" ]; then'
        self.assertIn(condition, patch['run'])
        self.assertIn(condition, self.find('准备 AK3 内核文件')['run'])
        self.assertIn('target="$(realpath -e "$target")"', patch['run'])
        self.assertNotIn('if', entry)
        self.assertIn('tools/validate_arm64_image.py', entry['run'])
        self.assertLess(names.index('准备 AK3 内核文件'), names.index(entry['name']))
        self.assertLess(names.index(entry['name']), names.index('准备 AnyKernel3 刷机包'))
        self.assertLess(names.index(entry['name']), names.index('上传可直接刷入的 AK3 包'))

    def test_month_is_part_of_workspace_and_artifact_identity(self):
        run = self.find('初始化构建环境')['run']
        self.assertIn('${{ matrix.sub_level }}-${{ matrix.os_patch_level }}', run)

    def test_catalog_refs_are_applied_before_repo_sync(self):
        step = self.find('初始化并同步内核源码')
        self.assertEqual(step['env']['GKI_KERNEL_REF'], '${{ matrix.kernel_ref }}')
        self.assertLess(step['run'].index('tools/set_kernel_ref.py'), step['run'].index('tools/optimize_manifest_fetch.py'))
        self.assertLess(step['run'].index('tools/optimize_manifest_fetch.py'), step['run'].index('"$REPO" --trace sync'))
        self.assertEqual(step['env']['GKI_SUBLEVEL'], '${{ matrix.sub_level }}')
        self.assertLess(step['run'].index('"$REPO" --trace sync'), step['run'].index('tools/validate_kernel_source.py'))
        self.assertIn('--family "$GKI_KERNEL" --sublevel "$GKI_SUBLEVEL"', step['run'])

    def test_catalog_guard_runs_after_checkout_before_slow_disk_cleanup(self):
        names = [step.get('name') for step in self.steps]
        self.assertLess(names.index('检出代码仓库'), names.index('校验自定义内核版本组合'))
        self.assertLess(names.index('校验自定义内核版本组合'), names.index('清理磁盘空间'))
        checkout = self.find('检出代码仓库')
        self.assertEqual(checkout['uses'], 'actions/checkout@v6')

    def test_whitelist_reads_new_catalog_versions_without_workflow_regeneration(self):
        import contextlib
        import io
        import json
        import os
        import re
        from unittest.mock import patch
        import matrix
        run = self.find('校验自定义内核版本组合')['run']
        script = re.search(r"KNOWN_KERNEL_PAIRS=\$\(python3 - <<'PY'\n(.*?)\nPY\n\)", run, re.S)[1]
        catalog = json.loads((ROOT / 'data/kernels.json').read_text(encoding='utf-8'))
        catalog['entries'].append({'android_version': 'android14', 'kernel_version': '6.1',
                                   'kernel': '6.1.999', 'date': '2026-12', 'ref': 'a' * 40})
        output = io.StringIO()
        with patch.dict(os.environ, {'GITHUB_WORKSPACE': str(ROOT)}), patch.object(matrix, 'load_catalog', return_value=catalog), contextlib.redirect_stdout(output):
            exec(compile(script, '<runtime-catalog-guard>', 'exec'), {})
        for pair in ('android14 6.1 145 2025-08', 'android15 6.6 143 2026-10',
                     'android16 6.12 92 2026-09', 'android14 6.1 999 2026-12'):
            self.assertIn(pair, output.getvalue())

    def test_no_undeclared_inputs_remain(self):
        declared = set(self.workflow['on']['workflow_dispatch']['inputs'])
        import re
        used = set(re.findall(r'\binputs\.(\w+)', str(self.workflow)))
        self.assertFalse(used - declared, used - declared)

    def test_upstream_shape_changes_fail(self):
        with self.assertRaises(ValueError):
            generator.step_named(self.steps, 'renamed upstream step')
        with self.assertRaises(ValueError):
            generator.transform('${{ inputs.future_required }}', {})

    def test_virtualization_modes_are_normalized_for_each_kernel(self):
        text = str(self.steps)
        self.assertIn("matrix.kernel_version == '6.12' && 'on'", text)
        self.assertIn("inputs.virtualization_support == 'on' && '678'", text)

    def test_android12_lts_boot_header_uses_a_date(self):
        step = self.find('构建 Boot 镜像 (Android 12)')
        self.assertIn("matrix.os_patch_level == 'lts'", step['env']['BOOT_OS_PATCH_LEVEL'])
        self.assertIn('--os_patch_level "$BOOT_OS_PATCH_LEVEL"', step['run'])
        self.assertNotIn('--os_patch_level "${{ matrix.os_patch_level }}"', step['run'])

    def test_parallel_limit_uses_normalized_integer_job_output(self):
        parallel = self.workflow['on']['workflow_dispatch']['inputs']['max_parallel']
        self.assertEqual(parallel['default'], 20)
        job = self.workflow['jobs']['build-kernel']
        self.assertEqual(job['strategy']['max-parallel'], '${{ fromJSON(needs.prepare-matrix.outputs.max_parallel) }}')
        prepare = self.workflow['jobs']['prepare-matrix']
        self.assertEqual(prepare['outputs']['max_parallel'], '${{ steps.matrix.outputs.max_parallel }}')
        self.assertIn('max_parallel={int(value)}', prepare['steps'][1]['run'])

    def test_build_runner_is_pinned_before_ubuntu_latest_migration(self):
        self.assertNotIn('ubuntu-latest', self.workflow['jobs']['build-kernel']['runs-on'])

    def test_serialization_roundtrips_boolean_and_on(self):
        text = generator.yaml.dump(self.workflow, Dumper=generator.Dumper, allow_unicode=True, sort_keys=False)
        parsed = generator.yaml.load(text, Loader=generator.Loader)
        self.assertEqual(parsed, self.workflow)
        self.assertIs(parsed['on']['workflow_dispatch']['inputs']['include_lts']['default'], False)


if __name__ == '__main__':
    unittest.main()
