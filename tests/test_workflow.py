import copy
import fnmatch
import itertools
import json
import os
import shutil
import subprocess
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
        cls.updater = generator.updater_workflow()
        cls.steps = cls.workflow['jobs']['build-kernel']['steps']

    def find(self, name):
        return next(step for step in self.steps if step.get('name') == name)

    def test_one_dispatch_and_no_remote_workflow_calls(self):
        self.assertEqual(list(self.workflow['on']), ['workflow_dispatch'])
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
        self.assertLess(index, next(i for i, step in enumerate(steps) if step.get('name') == '上传管理器安装包'))

    def test_bakasu_notice_uses_paired_public_certificate_before_manager_build(self):
        steps = self.workflow['jobs']['build-sukisu-manager']['steps']
        index = next(i for i, step in enumerate(steps)
                     if 'tools/configure_bakasu_manager_notice.py' in step.get('run', ''))
        configure = steps[index]
        self.assertEqual("inputs.kernelsu_variant == 'BakaSU'", configure['if'])
        self.assertNotIn('continue-on-error', configure)
        self.assertIn('--identity "$RUNNER_TEMP/manager-public-certificate.json"', configure['run'])
        self.assertIn("-p 'test_bakasu_manager_notice.py'", configure['run'])
        self.assertLess(next(i for i, step in enumerate(steps)
                             if step.get('name') == '准备本次模块与 APK 的公开签名信息'), index)
        self.assertLess(index, next(i for i, step in enumerate(steps) if step.get('id') == 'manager-build'))

    def test_manager_choices_only_expose_two_variants_and_two_shared_versions(self):
        fields = self.workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(fields['kernelsu_variant']['default'], 'SukiSU')
        self.assertEqual(fields['kernelsu_variant']['options'], ['SukiSU', 'BakaSU'])
        self.assertEqual(fields['kernelsu_branch']['default'], '最新版')
        self.assertEqual(fields['kernelsu_branch']['description'], '版本')
        self.assertEqual(fields['kernelsu_branch']['options'], ['正式版', '最新版'])
        kernel = self.workflow['jobs']['build-kernel']
        self.assertNotIn('inputs.kernelsu_branch', str(kernel))
        canonical = '${{ needs.prepare-matrix.outputs.effective_ksu_branch }}'
        self.assertEqual(self.find('选择配套的 SUSFS 源码版本')['env']['PIN_KSU_TIER'], canonical)
        self.assertEqual(self.find('记录内核实际配置')['env']['META_KSU_BRANCH'], canonical)
        summary = self.find('构建信息摘要')['run']
        self.assertIn('effective_ksu_branch="' + canonical + '"', summary)
        # Both supported managers now expose the restored native KPM bridge;
        # source tier selection must not silently disable BakaSU Latest.
        kpm = next(line for line in summary.splitlines() if 'KPM 功能' in line)
        self.assertIn("inputs.kernelsu_variant == 'BakaSU'", kpm)
        self.assertIn("inputs.kernelsu_variant == 'SukiSU'", kpm)
        self.assertNotIn('effective_ksu_branch', kpm)
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
        self.assertNotIn('schedule', self.workflow['on'])
        operation = self.workflow['on']['workflow_dispatch']['inputs']['operation']
        self.assertEqual(operation['default'], '编译内核')
        self.assertEqual(operation['options'], ['编译内核', '仅编译管理器安装包'])
        self.assertNotIn('仅编译管理器安装包', generator.BUILD_IF)
        self.assertNotIn('schedule', generator.PREPARE_IF)
        matrix = next(step for step in self.workflow['jobs']['prepare-matrix']['steps'] if step.get('id') == 'matrix')
        self.assertEqual(matrix['if'], generator.BUILD_IF)

    def test_rekernel_source_failure_stops_before_modifying_the_kernel(self):
        step = self.find('应用 Re-Kernel')
        script = step['run']
        self.assertEqual(step['if'], 'inputs.use_rekernel')
        self.assertTrue(script.startswith('set -euo pipefail\n'))
        self.assertIn('TMP_REKERNEL="$RUNNER_TEMP/rekernel-source"', script)
        acquisition = 'python3 "$GITHUB_WORKSPACE/tools/rekernel_source.py" --output "$TMP_REKERNEL"'
        self.assertIn(acquisition, script)
        self.assertLess(script.index(acquisition), script.index('rm -rf common/drivers/rekernel'))
        self.assertLess(script.index('test -f "$REKERNEL_SRC/rekernel.h"'),
                        script.index('rm -rf common/drivers/rekernel'))
        self.assertNotIn('git clone', script)
        self.assertNotIn('/tmp/rekernel', script)
        self.assertNotIn('|| true', script)
        self.assertNotIn('continue-on-error', step)
        self.assertIn('tools/historical_rekernel.py', script)

    def test_actual_rekernel_script_preserves_existing_driver_when_download_fails(self):
        shell = shutil.which('bash') or shutil.which('sh')
        if not shell and (git := shutil.which('git')):
            candidate = Path(git).parent.parent / 'usr/bin/sh.exe'
            shell = str(candidate) if candidate.is_file() else None
        if not shell:
            self.skipTest('Bash is required to execute the Re-Kernel failure path')
        temporary = ROOT / 'tests' / ('.rekernel-download-' + uuid.uuid4().hex)
        driver = temporary / 'common/drivers/rekernel'
        driver.mkdir(parents=True)
        sentinel = driver / 'original.c'
        sentinel.write_text('original source\n', encoding='utf-8')
        try:
            result = subprocess.run(
                [shell, '-c', 'python3() { return 91; }\n' + self.find('应用 Re-Kernel')['run']],
                cwd=temporary, capture_output=True, text=True, encoding='utf-8', timeout=15,
                env={**os.environ, 'RUNNER_TEMP': '/inert-runner-temp',
                     'GITHUB_WORKSPACE': '/inert-workspace', 'BASH_ENV': ''})
            self.assertEqual(91, result.returncode, result.stdout + result.stderr)
            self.assertEqual('original source\n', sentinel.read_text(encoding='utf-8'))
            self.assertEqual([sentinel], list(driver.iterdir()))
        finally:
            if temporary.resolve().parent != (ROOT / 'tests').resolve() or temporary.is_symlink():
                raise RuntimeError('Refusing cleanup outside the Re-Kernel test directory')
            shutil.rmtree(temporary)

    def test_both_selected_managers_build_for_kernel_and_apk_only_operations(self):
        for variant, operation, event in itertools.product(
                ('SukiSU', 'BakaSU', 'Official', 'None'),
                ('编译内核', '仅编译管理器安装包', '检查上游更新'), ('workflow_dispatch', 'push')):
            condition = generator.MANAGER_IF.replace('inputs.kernelsu_variant', repr(variant))
            condition = condition.replace('inputs.operation', repr(operation)).replace('github.event_name', repr(event))
            allowed = bool(eval(condition.replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}}, {}))
            with self.subTest(variant=variant, operation=operation, event=event):
                self.assertEqual(event == 'workflow_dispatch' and variant in ('SukiSU', 'BakaSU')
                                 and operation in ('编译内核', '仅编译管理器安装包'), allowed)

    def test_selected_variant_reaches_all_snapshot_signing_and_release_consumers(self):
        for name in ('prepare-matrix', 'build-sukisu-lkm', 'build-sukisu-manager', 'build-kernel', 'publish-release'):
            with self.subTest(job=name):
                job = self.workflow['jobs'][name]
                self.assertEqual(job['env']['KSU_VARIANT'], '${{ inputs.kernelsu_variant }}')
        prepare = self.workflow['jobs']['prepare-matrix']
        source = next(step for step in prepare['steps'] if step.get('id') == 'sources')
        self.assertEqual(source['env']['KSU_VARIANT'], '${{ inputs.kernelsu_variant }}')

    def test_history_cleanup_requires_explicit_default_branch_manual_operation(self):
        standalone = generator.cleanup_history_workflow()
        self.assertEqual(standalone['name'], '清理历史记录（保留2条成功及发行记录）')
        self.assertEqual(standalone['run-name'], standalone['name'])
        self.assertEqual(standalone['on'], {'workflow_dispatch': {}})
        self.assertEqual(set(standalone['jobs']), {'cleanup-history'})
        self.assertNotIn('cleanup-history', self.workflow['jobs'])
        self.assertNotIn('清理历史编译', self.workflow['run-name'])
        cleanup = standalone['jobs']['cleanup-history']
        self.assertEqual(cleanup['if'], "github.event_name == 'workflow_dispatch' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)")
        self.assertEqual(cleanup['permissions'], {'contents': 'read', 'actions': 'write'})
        self.assertIs(cleanup['concurrency']['cancel-in-progress'], False)
        self.assertNotIn('needs', cleanup)
        checkout, execute = cleanup['steps']
        self.assertIn('发行记录和发行版全部保留', execute['name'])
        self.assertEqual(checkout['with']['ref'], '${{ github.event.repository.default_branch }}')
        self.assertIs(checkout['with']['persist-credentials'], False)
        self.assertEqual(execute['env'], {'GH_TOKEN': '${{ github.token }}'})
        self.assertIn('--keep 2 --exclude-workflow cleanup-build-history.yml --execute', execute['run'])
        self.assertIn('--workflow all', execute['run'])
        self.assertIn('--summary "$GITHUB_STEP_SUMMARY"', execute['run'])
        self.assertNotIn('schedule', cleanup['if'])
        self.assertNotIn('upload-artifact', str(cleanup))

    def test_build_and_sync_remove_successful_cleanup_records_without_another_workflow(self):
        for name, job in (('prepare-matrix', self.workflow['jobs']['prepare-matrix']),
                          ('check-upstream', self.updater['jobs']['check-upstream'])):
            with self.subTest(job=name):
                steps = [step for step in job['steps']
                         if 'remove_successful_cleanup_runs.py' in step.get('run', '')]
                self.assertEqual(1, len(steps))
                self.assertIs(steps[0]['continue-on-error'], True)
                self.assertEqual('always() && !cancelled()', steps[0]['if'])
                self.assertIn('--execute', steps[0]['run'])
                self.assertEqual({'GH_TOKEN': '${{ github.token }}'}, steps[0]['env'])
                self.assertEqual('write', job['permissions']['actions'])
        self.assertNotIn('workflow_run', self.workflow['on'])
        self.assertNotIn('workflow_run', self.updater['on'])

    def test_susfs_map_compatibility_is_checked_with_a_real_compiler_before_expanding_jobs(self):
        prepare = self.workflow['jobs']['prepare-matrix']
        steps = prepare['steps']
        check = next(step for step in steps if step['name'] == '校验旧版内核 SUSFS 映射兼容性')
        self.assertIn("inputs.operation == '编译内核'", check['if'])
        self.assertIn('inputs.enable_susfs', check['if'])
        self.assertIn('command -v cc', check['run'])
        self.assertIn('test_susfs_map_compat.py', check['run'])
        source = next(step for step in steps if step.get('id') == 'sources')
        self.assertLess(steps.index(check), steps.index(source))
        self.assertNotIn('continue-on-error', check)

    def test_updater_uses_default_branch_and_no_persistent_checkout_token(self):
        job = self.updater['jobs']['check-upstream']
        self.assertEqual(self.updater['on']['schedule'], [{'cron': '13 */3 * * *'}])
        operation = self.updater['on']['workflow_dispatch']['inputs']['operation']
        self.assertEqual(operation['options'], ['仅检查并提醒', '同步官方更新'])
        self.assertEqual(operation['default'], '仅检查并提醒')
        self.assertEqual(set(self.updater['jobs']), {'check-upstream'})
        self.assertNotIn('check-upstream', self.workflow['jobs'])
        self.assertEqual(job['if'], "(github.event_name == 'workflow_dispatch' || github.event_name == 'schedule') && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)")
        self.assertNotIn('schedule', self.updater['run-name'])
        self.assertIn('inputs.operation', self.updater['run-name'])
        self.assertEqual(job['permissions'], {'contents': 'read', 'pull-requests': 'write', 'actions': 'write'})
        checkout = job['steps'][0]
        self.assertIs(checkout['with']['persist-credentials'], False)
        self.assertEqual(checkout['with']['ref'], '${{ github.event.repository.default_branch }}')
        self.assertIs(job['concurrency']['cancel-in-progress'], False)

    def test_updater_validates_before_publishing_and_skips_existing_candidate(self):
        steps = self.updater['jobs']['check-upstream']['steps']
        indexed = {step['id']: (i, step) for i, step in enumerate(steps) if 'id' in step}
        self.assertLess(indexed['guard'][0], indexed['update'][0])
        self.assertLess(indexed['update'][0], indexed['pr'][0])
        self.assertEqual("steps.probe.outputs.has_updates == 'true'", indexed['update'][1]['if'])
        self.assertIn('official_sync_report.py --root . --base-ref HEAD', indexed['update'][1]['run'])
        pr = indexed['pr'][1]
        self.assertEqual(pr['if'], "steps.update.outputs.has_updates == 'true' && steps.guard.outputs.already_pending != 'true'")
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

    def test_update_check_removes_old_checks_without_running_during_manual_sync(self):
        steps = self.updater['jobs']['check-upstream']['steps']
        cleanup = next(step for step in steps if step.get('id') == 'previous-checks')
        probe = next(step for step in steps if step.get('id') == 'official')
        self.assertLess(steps.index(cleanup), steps.index(probe))
        self.assertGreater(steps.index(cleanup), 0)
        self.assertTrue(cleanup['continue-on-error'])
        self.assertEqual(cleanup['env']['GH_TOKEN'], '${{ github.token }}')
        self.assertIn('remove_previous_update_checks.py', cleanup['run'])
        self.assertIn('--execute', cleanup['run'])
        for event, operation, allowed in (('schedule', '', True),
                                           ('workflow_dispatch', '仅检查并提醒', True),
                                           ('workflow_dispatch', '同步官方更新', False),
                                           ('push', '仅检查并提醒', False)):
            expression = cleanup['if'].replace('github.event_name', repr(event)).replace('inputs.operation', repr(operation))
            with self.subTest(event=event, operation=operation):
                self.assertEqual(allowed, bool(eval(expression.replace('&&', ' and ').replace('||', ' or '),
                                                   {'__builtins__': {}}, {})))
        for document in (self.workflow, generator.cleanup_history_workflow()):
            self.assertNotIn('remove_previous_update_checks.py', str(document))

    def test_only_explicit_manual_sync_can_merge_pending_or_new_pr(self):
        steps = self.updater['jobs']['check-upstream']['steps']
        step = next(step for step in steps if step.get('id') == 'sync')
        self.assertEqual(step['env']['GH_TOKEN'], '${{ secrets.UPSTREAM_SYNC_TOKEN }}')
        self.assertIn("steps.pr.outputs.pull-request-number != ''", step['if'])
        self.assertIn("steps.guard.outputs.already_pending == 'true'", step['if'])
        self.assertIn('--pull-request "$number" --base "$DEFAULT_BRANCH"', step['run'])
        self.assertNotIn('issues/$PR_NUMBER/comments', str(steps))
        self.assertNotIn('assignees', next(item for item in steps if item.get('id') == 'pr')['with'])
        summary = next(item for item in steps if item['name'] == '记录检查结果')
        self.assertEqual(summary['env']['SYNC_MERGED'], '${{ steps.sync.outputs.merged }}')
        self.assertIn('等待你手动同步', summary['run'])
        self.assertIn('official-sync-report.md', summary['run'])
        notify = next(item for item in steps if item.get('id') == 'notify')
        self.assertEqual(notify['env']['GH_TOKEN'], '${{ github.token }}')
        self.assertIn('notify_official_sync.py', notify['run'])
        for event in ('schedule', 'workflow_dispatch'):
            for operation in ('', '仅检查并提醒', '同步官方更新'):
                for pending in (False, True):
                    values = {'github.event_name': repr(event), 'inputs.operation': repr(operation),
                              'steps.probe.outputs.has_updates': repr('true'),
                              'steps.pr.outputs.pull-request-number': repr('' if pending else '12'),
                              'steps.guard.outputs.already_pending': repr(str(pending).lower())}
                    expression = step['if']
                    for name, value in values.items():
                        expression = expression.replace(name, value)
                    allowed = bool(eval(expression.replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}}, {}))
                    with self.subTest(event=event, operation=operation, pending=pending):
                        self.assertEqual(allowed, event == 'workflow_dispatch' and operation == '同步官方更新')

    def test_catalog_cache_survives_partial_failures_and_final_status_is_explicit(self):
        steps = self.updater['jobs']['check-upstream']['steps']
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
        steps = self.updater['jobs']['check-upstream']['steps']
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
        step = next(step for step in self.updater['jobs']['check-upstream']['steps'] if step.get('id') == 'update')
        self.assertIn('if [ "$OFFICIAL_RESULT" = success ]; then', step['run'])
        self.assertIn('if [ "$CATALOG_RESULT" = success ]; then', step['run'])
        code = step['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        directory = ROOT / 'tests' / ('.workflow-partial-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        output = directory / 'output'
        (directory / 'official-sync-report.md').write_text('## 本次变更\n\nSukiSU 提交：aaaa → bbbb\n', encoding='utf-8')
        environment = {'RUNNER_TEMP': str(directory), 'GITHUB_OUTPUT': str(output),
                       'OFFICIAL_RESULT': 'success', 'CATALOG_RESULT': 'failure',
                       'HEADS_MARKER': '<!-- gki-official-heads:sha256=' + 'a' * 64 + ' -->',
                       'OFFICIAL_CANDIDATES': '{}', 'CATALOG_CANDIDATES': ''}
        with patch.dict(os.environ, environment):
            exec(compile(code, '<official update body>', 'exec'), {})
        body = (directory / 'official-update.md').read_text(encoding='utf-8')
        self.assertIn('Android GKI：检查失败，未确认有无更新', body)
        self.assertIn('不能视为全部官方源已检查', body)
        self.assertIn('SukiSU 提交：aaaa → bbbb', body)
        self.assertIn('不会因检查而自动合并', body)

    def test_custom_ksu_ref_remains_available_through_repository_variable(self):
        sources = next(step for step in self.workflow['jobs']['prepare-matrix']['steps'] if step.get('id') == 'sources')
        self.assertEqual(sources['env']['CUSTOM_REF'], '${{ vars.KSU_CUSTOM_REF }}')
        self.assertIn('tools/resolve_build_sources.py', sources['run'])
        step = self.find('确定 KernelSU 分支')
        self.assertEqual(step['env']['BRANCH'], '${{ needs.prepare-matrix.outputs.ksu_ref }}')
        self.assertNotIn('resolve-ksu-ref.sh', step['run'])

    def test_selected_manager_is_built_once_and_kernel_uses_its_signature(self):
        manager = self.workflow['jobs']['build-sukisu-manager']
        self.assertEqual(manager['name'], '编译配套 ${{ inputs.kernelsu_variant }} 管理器 APK')
        self.assertEqual(manager['needs'], ['prepare-matrix', 'build-sukisu-lkm'])
        self.assertNotIn('strategy', manager)
        self.assertEqual(manager['if'], generator.MANAGER_IF)
        self.assertEqual({'contents': 'read', 'actions': 'read'}, manager['permissions'])
        self.assertEqual('${{ steps.manager-producer.outputs.manager_job_id }}', manager['outputs']['manager_job_id'])
        producer = next(step for step in manager['steps'] if step.get('id') == 'manager-producer')
        self.assertEqual('python3 tools/wait_manager_identity.py --record-producer-job', producer['run'])
        self.assertEqual('${{ github.token }}', producer['env']['GH_TOKEN'])
        self.assertLess(manager['steps'].index(producer),
                        next(index for index, step in enumerate(manager['steps']) if step.get('id') == 'manager-build'))
        self.assertNotIn('continue-on-error', producer)
        checkout = next(step for step in manager['steps'] if step['name'] == '检出同一版本的管理器源码')
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
        self.assertEqual(build['env']['KSU_PACKAGE_NAME'], '${{ needs.prepare-matrix.outputs.manager_package }}')
        self.assertEqual(build['env']['ORG_GRADLE_PROJECT_KSU_NAME'], '${{ inputs.kernelsu_variant }}')
        self.assertIn('BakaSU) script=tools/build_bakasu_manager.sh', build['run'])
        self.assertIn('SukiSU) script=tools/build_sukisu_manager.sh', build['run'])
        self.assertNotIn('secrets.', str(build))
        sign = next(step for step in manager['steps'] if step.get('id') == 'sign')
        self.assertIn('secrets.SUKISU_APK_KEYSTORE', sign['env']['SUKISU_APK_KEYSTORE'])
        self.assertIn('--build-metadata "$RUNNER_TEMP/manager-build/manager-build-metadata.json"', sign['run'])
        kernel = self.workflow['jobs']['build-kernel']
        self.assertEqual(kernel['needs'], ['prepare-matrix', 'build-sukisu-lkm', 'build-sukisu-manager'])
        self.assertEqual(kernel['permissions'], {'contents': 'read', 'actions': 'read'})
        wait = self.find('编译前读取本次 APK 的实际签名信息')
        self.assertEqual(wait['if'], "inputs.kernelsu_variant == 'SukiSU' || inputs.kernelsu_variant == 'BakaSU'")
        self.assertIn('wait_manager_identity.py', wait['run'])
        self.assertIn('--expected-ref "$KSU_REF"', wait['run'])
        self.assertIn('--manager-job-id "$MANAGER_JOB_ID"', wait['run'])
        self.assertEqual('${{ needs.build-sukisu-manager.outputs.manager_job_id }}', wait['env']['MANAGER_JOB_ID'])
        self.assertIn('--github-env "$GITHUB_ENV"', wait['run'])
        self.assertNotIn('Manager-Identity-', str(manager))
        self.assertIn('--artifact-name "$MANAGER_ARTIFACT"', wait['run'])
        self.assertEqual(wait['env']['MANAGER_ARTIFACT'], '${{ needs.build-sukisu-manager.outputs.apk_filename }}')
        self.assertIn('KSU_REF', sign['env'])
        upload = next(step for step in manager['steps'] if step['name'] == '上传管理器安装包')
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
                        'needs.build-sukisu-lkm.result': repr('success' if variant in ('SukiSU', 'BakaSU') else 'skipped'),
                        'needs.build-sukisu-manager.result': repr(producer),
                        'github.event_name': repr('workflow_dispatch'),
                        'inputs.operation': repr('编译内核'), 'inputs.kernelsu_variant': repr(variant),
                    }
                    expression = kernels['if']
                    for name, value in values.items():
                        expression = expression.replace(name, value)
                    expression = expression.replace('&&', ' and ').replace('||', ' or ').replace('!False', 'not False')
                    allowed = bool(eval(expression, {'__builtins__': {}}, {}))
                    self.assertEqual(variant not in ('SukiSU', 'BakaSU') or producer == 'success', allowed)

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
                         "${{ inputs.operation == '仅编译管理器安装包' && '0' || needs.prepare-matrix.outputs.count }}")
        self.assertIn('--expected-kernel-count "$EXPECTED_KERNEL_COUNT"', preparation['run'])
        self.assertIn('--report-kind "$REPORT_KIND"', preparation['run'])
        self.assertEqual(preparation['env']['REPORT_KIND'],
                         "${{ inputs.operation == '编译内核' && needs.prepare-matrix.outputs.full_catalog == 'true' && 'release' || 'actions' }}")
        self.assertEqual(self.workflow['jobs']['prepare-matrix']['outputs']['count'],
                         '${{ steps.matrix.outputs.count }}')
        self.assertEqual(self.workflow['jobs']['prepare-matrix']['outputs']['full_catalog'],
                         '${{ steps.matrix.outputs.full_catalog }}')

    def test_only_complete_successful_catalog_can_publish_a_release(self):
        job = self.workflow['jobs']['publish-release']
        publish = next(step for step in job['steps'] if step.get('id') == 'release')
        statuses = ('success', 'failure', 'cancelled', 'skipped')
        for operation, variant, full, prepare, manager, kernels, cancelled in itertools.product(
                ('编译内核', '仅编译管理器安装包', '检查上游更新'), ('SukiSU', 'BakaSU'),
                ('true', 'false', '', 'True'), ('success', 'failure'), statuses, statuses, (False, True)):
            with self.subTest(operation=operation, variant=variant, full=full,
                              prepare=prepare, manager=manager, kernels=kernels, cancelled=cancelled):
                values = {
                    'always()': 'True', 'cancelled()': repr(cancelled),
                    'github.event_name': repr('workflow_dispatch'),
                    'inputs.operation': repr(operation), 'inputs.kernelsu_variant': repr(variant),
                    'needs.prepare-matrix.result': repr(prepare),
                    'needs.prepare-matrix.outputs.full_catalog': repr(full),
                    'needs.build-sukisu-manager.result': repr(manager),
                    'needs.build-kernel.result': repr(kernels),
                }
                def evaluate(expression):
                    for name, value in values.items():
                        expression = expression.replace(name, value)
                    expression = expression.replace('&&', ' and ').replace('||', ' or ')
                    expression = expression.replace('!True', 'not True').replace('!False', 'not False')
                    return bool(eval(expression, {'__builtins__': {}}, {}))
                actual = evaluate(job['if']) and evaluate(publish['if'])
                expected = (not cancelled and prepare == 'success' and operation == '编译内核'
                            and full == 'true' and kernels == 'success'
                            and manager == 'success')
                self.assertEqual(expected, actual)

    def test_non_release_builds_keep_checksums_and_configuration_without_source_drafts(self):
        steps = self.workflow['jobs']['publish-release']['steps']
        publish = next(step for step in steps if step.get('id') == 'release')
        checksum = next(step for step in steps if step['name'] == '上传同名校验文件')
        self.assertNotIn('if', checksum)
        self.assertLess(steps.index(checksum), steps.index(publish))
        cleanup = next(step for step in steps if 'cleanup_build_artifacts.py' in step.get('run', ''))
        self.assertEqual(cleanup['if'], "steps.release.outcome == 'success'")
        self.assertFalse(any('delete-actions' in step.get('run', '') for step in steps))
        self.assertNotIn('source_backup_release_id', str(self.workflow))
        summary = next(step for step in steps if step['name'] == '显示构建配置与发行结果')
        self.assertIn('cat release-publish/release.md', summary['run'])

    def test_selected_source_is_frozen_and_checked_before_any_kernel_sync(self):
        prepare = self.workflow['jobs']['prepare-matrix']['steps']
        checkout = next(step for step in prepare if step.get('with', {}).get('path') == 'ksu-snapshot-source')
        self.assertEqual(checkout['with']['repository'], '${{ steps.sources.outputs.ksu_repo }}')
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
        self.assertLess(names.index('检出内核配套的管理器快照'), names.index('初始化并同步内核源码'))
        setup = self.find('添加 KernelSU')['run']
        # Both paired variants share the already restored source. Inspect
        # their case bodies, retaining the prohibition on mutable setup fetches.
        import re
        cases = re.findall(r'(?ms)^\s*("SukiSU"(?:\|"BakaSU")?|"BakaSU")\)\s*(.*?)^\s*;;', setup)
        self.assertTrue(cases)
        self.assertIn('SukiSU', ''.join(pattern for pattern, _ in cases))
        self.assertIn('BakaSU', ''.join(pattern for pattern, _ in cases))
        for pattern, body in cases:
            with self.subTest(pattern=pattern):
                self.assertIn('integrate_sukisu.py', body)
                for unstable in ('git clone', 'git pull', 'curl ', 'setup.sh'):
                    self.assertNotIn(unstable, body)

    def test_frozen_bakasu_symbol_contract_blocks_all_fanout_before_source_publication(self):
        from oplus_workflow import create_workflow as create_oplus
        for document in (self.workflow, create_oplus(ROOT, self.workflow)):
            with self.subTest(workflow=document['name']):
                prepare = document['jobs']['prepare-matrix']
                steps = prepare['steps']
                snapshot = next(step for step in steps if step.get('id') == 'source-snapshot')
                script = snapshot['run']
                freeze = script.index('python3 tools/snapshot_ksu_source.py ')
                restore = script.index('python3 tools/checkout_ksu_snapshot.py ')
                guard = script.index('python3 tools/ensure_bakasu_symbol_lookup.py ')
                outputs = script.index('python3 - <<\'PY\'')
                self.assertLess(freeze, restore)
                self.assertLess(restore, guard)
                self.assertLess(guard, outputs)
                self.assertIn('if [ "$KSU_VARIANT" = BakaSU ]; then\n', script[freeze:restore])
                self.assertIn('--input "$GITHUB_WORKSPACE/ksu-source-bundle"', script[restore:guard])
                self.assertIn('--output "$RUNNER_TEMP/bakasu-symbol-preflight"', script[restore:guard])
                self.assertIn('--expected-ref "$KSU_REF" --variant BakaSU', script[restore:guard])
                self.assertIn('--kernel-root "$RUNNER_TEMP/bakasu-symbol-preflight"', script[guard:outputs])
                self.assertIn('--ksu-dir kernel --expected-ref "$KSU_REF"', script[guard:outputs])
                self.assertIn('\nfi\n', script[guard:outputs])
                self.assertTrue(script.startswith('set -euo pipefail\n'))
                for forbidden in ('continue-on-error', '|| true', 'git clone', 'git fetch', 'git pull', 'curl ', 'wget '):
                    self.assertNotIn(forbidden, script[restore:outputs])
                self.assertNotIn('continue-on-error', snapshot)
                identity = next(step for step in steps if step.get('id') == 'source-identity')
                cache = next(step for step in steps if step.get('uses') == 'actions/cache/save@v5')
                self.assertLess(steps.index(snapshot), steps.index(identity))
                self.assertLess(steps.index(identity), steps.index(cache))
                for name, job in document['jobs'].items():
                    if name.startswith('build-'):
                        self.assertIn('prepare-matrix', job['needs'])

    def test_actual_prepare_script_stops_on_freeze_restore_or_symbol_failure_and_skips_suki(self):
        from oplus_workflow import create_workflow as create_oplus
        shell = shutil.which('bash') or shutil.which('sh')
        if not shell and (git := shutil.which('git')):
            candidate = Path(git).parent.parent / 'usr/bin/sh.exe'
            shell = str(candidate) if candidate.is_file() else None
        if not shell:
            self.skipTest('Bash is required for inert source preflight sequencing')
        commands = ['tools/snapshot_ksu_source.py', 'tools/checkout_ksu_snapshot.py',
                    'tools/ensure_bakasu_symbol_lookup.py']
        stub = '''python3() {
  if [ "$1" = - ]; then
    while IFS= read -r line; do :; done
    printf 'publish-source-cache-key\\n'
  else
    printf '%s\\n' "$1"
  fi
  if [ "$1" = "$FAIL_COMMAND" ]; then return 91; fi
  return 0
}
'''
        scenarios = [('BakaSU', '', commands + ['publish-source-cache-key'], 0),
                     ('SukiSU', commands[2], commands[:1] + ['publish-source-cache-key'], 0)]
        scenarios += [('BakaSU', command, commands[:index + 1], 91)
                      for index, command in enumerate(commands)]
        for document in (self.workflow, create_oplus(ROOT, self.workflow)):
            script = next(step['run'] for step in document['jobs']['prepare-matrix']['steps']
                          if step.get('id') == 'source-snapshot')
            for variant, failure, expected, status in scenarios:
                with self.subTest(workflow=document['name'], variant=variant, failure=failure):
                    environment = {**os.environ, 'KSU_VARIANT': variant, 'KSU_REF': 'a' * 40,
                                   'KSU_TIER': 'Latest', 'ENABLE_SUSFS': 'false',
                                   'GITHUB_WORKSPACE': '/inert-workspace', 'RUNNER_TEMP': '/inert-temp',
                                   'FAIL_COMMAND': failure}
                    result = subprocess.run([shell, '-c', stub + script], env=environment,
                                            capture_output=True, text=True, encoding='utf-8', timeout=15)
                    self.assertEqual(status, result.returncode, result.stdout + result.stderr)
                    self.assertEqual(expected, result.stdout.splitlines(), result.stderr)

    def test_public_files_are_never_uploaded_with_duplicate_release_wrappers(self):
        for job in self.workflow['jobs'].values():
            for step in job.get('steps', []):
                if not step.get('uses', '').startswith('actions/upload-artifact@'):
                    continue
                self.assertFalse(str(step.get('with', {}).get('name', '')).startswith(
                    ('Source-', 'Release-', 'Manager-Identity-', 'SukiSU-Metadata-')))
                uploaded = str(step.get('with', {}).get('path', ''))
                self.assertNotIn('SukiSU-Metadata-', uploaded)
                for path in uploaded.splitlines():
                    self.assertNotIn(Path(path).name, ('kernel.json', 'manager.json'))
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
                         if step.get('with', {}).get('path') == 'ksu-source-bundle'
                         and step.get('uses') == 'actions/cache/restore@v5']
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

    def test_source_cache_misses_restore_the_exact_official_identity_without_source_releases(self):
        prepare = self.workflow['jobs']['prepare-matrix']
        self.assertEqual(prepare['permissions'], {'contents': 'read', 'actions': 'write'})
        self.assertNotIn('source_backup_release_id', prepare['outputs'])
        self.assertFalse(any(step.get('id') == 'source-backup' for step in prepare['steps']))
        for name in ('build-sukisu-lkm', 'build-sukisu-manager', 'build-kernel'):
            steps = self.workflow['jobs'][name]['steps']
            restore = next(step for step in steps if step.get('id') == 'source-cache')
            fallback = next(step for step in steps if 'transfer_ksu_snapshot.py restore-official' in step.get('run', ''))
            verify = next(step for step in steps if 'transfer_ksu_snapshot.py verify-cache' in step.get('run', ''))
            checkout = next(step for step in steps if 'checkout_ksu_snapshot.py' in step.get('run', ''))
            self.assertLess(steps.index(restore), steps.index(fallback))
            self.assertLess(steps.index(fallback), steps.index(checkout))
            self.assertIn("steps.source-cache.outputs.cache-hit != 'true'", fallback['if'])
            self.assertNotIn('continue-on-error', fallback)
            self.assertNotIn('SOURCE_BACKUP_RELEASE_ID', fallback['env'])
            self.assertNotIn('--release-id', fallback['run'])
            self.assertIn('--expected-ref "$KSU_REF"', fallback['run'])
            self.assertEqual(fallback['env']['GH_TOKEN'], '${{ secrets.UPSTREAM_SYNC_TOKEN || github.token }}')
            self.assertEqual('${{ needs.prepare-matrix.outputs.source_identity }}', fallback['env']['PREPARED_SOURCE_IDENTITY'])
            self.assertIn('--prepared-identity "$PREPARED_SOURCE_IDENTITY"', fallback['run'])
            self.assertIn('--susfs "$ENABLE_SUSFS" --tier "$KSU_TIER"', fallback['run'])
            recache = next(step for step in steps if step['name'] == '重新缓存本次已验证的固定源码')
            self.assertLess(steps.index(fallback), steps.index(recache))
            self.assertLess(steps.index(recache), steps.index(checkout))
            self.assertEqual('${{ needs.prepare-matrix.outputs.source_cache_key }}', recache['with']['key'])
            self.assertIn("steps.source-backup.outcome == 'success'", recache['if'])
            self.assertLess(steps.index(recache), steps.index(verify))
            self.assertLess(steps.index(verify), steps.index(checkout))
            self.assertNotIn('continue-on-error', verify)
            self.assertNotIn('cache-hit', verify.get('if', ''), 'Cache hits must pass the same identity verification')
            self.assertEqual('${{ needs.prepare-matrix.outputs.source_identity }}', verify['env']['PREPARED_SOURCE_IDENTITY'])
            self.assertIn('--source "$GITHUB_WORKSPACE/ksu-source-bundle"', verify['run'])
            self.assertIn('--expected-ref "$KSU_REF"', verify['run'])
            self.assertIn('--prepared-identity "$PREPARED_SOURCE_IDENTITY"', verify['run'])
        for job in self.workflow['jobs'].values():
            for step in job.get('steps', []):
                script = step.get('run', '')
                for forbidden in ('transfer_ksu_snapshot.py create', 'transfer_ksu_snapshot.py restore \\\n',
                                  'transfer_ksu_snapshot.py delete', 'SOURCE_BACKUP_RELEASE_ID'):
                    self.assertNotIn(forbidden, script)

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

    def test_prepare_exports_original_source_tree_history_and_native_version_for_recovery(self):
        prepare = self.workflow['jobs']['prepare-matrix']
        step = next(step for step in prepare['steps'] if step.get('id') == 'source-identity')
        save = next(step for step in prepare['steps'] if step['name'] == '保存本次不可变源码缓存')
        self.assertLess(prepare['steps'].index(step), prepare['steps'].index(save))
        self.assertEqual('${{ steps.source-identity.outputs.source_identity }}', prepare['outputs']['source_identity'])
        code = step['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        directory = ROOT / 'tests' / ('.source-identity-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        identity = {'ref': 'a' * 40, 'git_tree': 'b' * 40, 'git_count': 1072,
                    'git_describe': 'v4.2.1-r2-6-g72eea67'}
        for variant in ('SukiSU', 'BakaSU'):
            output = directory / variant
            with self.subTest(variant=variant), \
                    patch.dict(os.environ, {'KSU_REF': 'a' * 40, 'KSU_VARIANT': variant,
                                           'GITHUB_OUTPUT': str(output)}), \
                    patch('checkout_ksu_snapshot.read_manifest', return_value={**identity, 'bundle_sha256': 'c' * 64}) as manifest:
                exec(compile(code, '<source-identity>', 'exec'), {})
            manifest.assert_called_once_with(Path('ksu-source-bundle'), 'a' * 40, variant=variant)
            self.assertEqual(identity, json.loads(output.read_text().split('=', 1)[1]))

    def test_release_contains_direct_files_and_only_runs_after_success(self):
        release = self.workflow['jobs']['publish-release']
        self.assertEqual(release['permissions'], {'contents': 'write', 'actions': 'write'})
        self.assertIn("needs.build-kernel.result == 'success'", release['if'])
        self.assertIn("needs.build-sukisu-manager.result == 'success'", release['if'])
        self.assertIn("inputs.operation == '仅编译管理器安装包'", release['if'])
        self.assertNotIn('schedule', release['if'])
        self.assertIn("github.event_name == 'workflow_dispatch'", release['if'])
        publish = next(step for step in release['steps'] if step.get('id') == 'release')
        allocate = next(step for step in release['steps'] if step.get('id') == 'release-tag')
        self.assertEqual(allocate['if'], publish['if'])
        self.assertIn("needs.prepare-matrix.outputs.full_catalog == 'true'", allocate['if'])
        self.assertIn('release_tags.py allocate --notes release-publish/release.md', allocate['run'])
        self.assertLess(release['steps'].index(allocate), release['steps'].index(publish))
        self.assertEqual(publish['env']['RELEASE_TAG'], '${{ steps.release-tag.outputs.release_tag }}')
        self.assertIn('release_tag=%s', publish['run'])
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
        self.assertEqual(cleanup['env']['RELEASE_TAG'], '${{ steps.release.outputs.release_tag }}')
        self.assertIn('--release-tag "$RELEASE_TAG"', cleanup['run'])

    def test_manager_caches_match_real_cargo_target_and_exclude_signing_outputs(self):
        steps = self.workflow['jobs']['build-sukisu-manager']['steps']
        source = (ROOT / 'tools/build_sukisu_manager.sh').read_text(encoding='utf-8')
        self.assertIn('export CARGO_TARGET_DIR="$source_dir/target"', source)
        self.assertIn('"$CARGO_TARGET_DIR/aarch64-linux-android/release/ksud"', source)
        sign = next(i for i, step in enumerate(steps) if step.get('id') == 'sign')
        upload = next(i for i, step in enumerate(steps) if step.get('name') == '上传管理器安装包')
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
        self.assertIn("'v2-bakasu' || 'v1'", cargo['with']['key'])
        self.assertIn('tools/build_bakasu_manager.sh', cargo['with']['key'])
        sdk = next(step for step in steps if step['name'] == '准备 Android SDK 与 NDK')['run']
        self.assertIn("'ndk;29.0.14206865'", sdk)
        self.assertIn("'ndk;30.0.16248370'", sdk)
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
                                                  if step.get('name') == '上传管理器安装包'))
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
        prefixes = restore['with']['restore-keys'].splitlines()
        self.assertEqual(len(prefixes), 3)
        self.assertIn('matrix.os_patch_level', prefixes[0])
        self.assertIn('matrix.sub_level', prefixes[0])
        self.assertIn('matrix.sub_level', prefixes[1])
        self.assertNotIn('matrix.os_patch_level', prefixes[1])
        self.assertNotIn('matrix.sub_level', prefixes[2])
        self.assertTrue(restore['with']['key'].startswith(prefixes[0]))
        self.assertTrue(prefixes[0].startswith(prefixes[1]))
        self.assertTrue(prefixes[1].startswith(prefixes[2]))
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

    def test_susfs_is_fixed_once_and_workers_restore_before_dependency_downloads(self):
        prepare = self.workflow['jobs']['prepare-matrix']
        shared = next(step for step in prepare['steps'] if step.get('id') == 'shared-susfs')
        snapshot = next(step for step in prepare['steps'] if step.get('id') == 'source-snapshot')
        self.assertEqual(shared['if'], generator.BUILD_IF + ' && inputs.enable_susfs')
        self.assertLess(prepare['steps'].index(shared), prepare['steps'].index(snapshot))
        self.assertEqual(shared['env']['SUSFS_MATRIX'], '${{ steps.matrix.outputs.matrix }}')
        self.assertIn('shared_susfs_source.py prepare', shared['run'])
        self.assertIn('--susfs-sources "$RUNNER_TEMP/susfs-sources.json"', snapshot['run'])
        self.assertEqual(prepare['outputs']['susfs_sources'], '${{ steps.shared-susfs.outputs.susfs_sources }}')
        dependencies = self.find('克隆依赖仓库')
        restore = self.find('还原本次固定源码缓存')
        self.assertLess(self.steps.index(restore), self.steps.index(dependencies))
        self.assertIn('shared_susfs_source.py" restore', dependencies['run'])
        self.assertIn('--expected-sources "$SUSFS_SOURCES"', dependencies['run'])
        self.assertNotIn('gitlab.com/simonpunk', dependencies['run'])
        self.assertEqual(dependencies['env']['SUSFS_SOURCES'], '${{ needs.prepare-matrix.outputs.susfs_sources }}')

    def test_bakasu_keeps_native_resolver_and_checks_it_before_compilation(self):
        check = self.find('核实 BakaSU 原生符号接口')
        compiler = self.find('编译内核')
        self.assertEqual(check['if'], "inputs.kernelsu_variant == 'BakaSU'")
        self.assertLess(self.steps.index(check), self.steps.index(compiler))
        command = compiler['with']['command']
        case = command.split('case "${{ inputs.kernelsu_variant }}" in', 1)[1]
        header = case.split('echo "编译前兜底修复', 1)[0]
        self.assertIn('"Official")', header)
        self.assertNotIn('"BakaSU"', header)

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

    def test_boot_tool_download_cache_and_key_generation_run_only_for_boot_outputs(self):
        for name in ('缓存工具链', '下载工具链', '生成签名密钥'):
            condition = self.find(name)['if']
            for mode in ('仅 AK3', 'AK3 + boot 镜像'):
                value = condition.replace('inputs.artifact_mode', repr(mode))
                value = value.replace('env.ABK_LOCAL_CACHE_ENABLED', repr('false'))
                value = value.replace('steps.cache-toolchain.outputs.cache-hit', repr('false'))
                with self.subTest(step=name, mode=mode):
                    self.assertEqual(mode != '仅 AK3', eval(value.replace('&&', ' and '), {'__builtins__': {}}, {}))
        self.assertIn("env.ABK_LOCAL_CACHE_ENABLED != 'true'", self.find('缓存工具链')['if'])
        self.assertIn("steps.cache-toolchain.outputs.cache-hit != 'true'", self.find('下载工具链')['if'])
        manager = self.workflow['jobs']['build-sukisu-manager']
        sign = next(step for step in manager['steps'] if step.get('id') == 'sign')
        self.assertNotIn('artifact_mode', sign.get('if', ''))

    def test_disposable_clone_histories_are_not_downloaded_for_every_kernel(self):
        self.assertIn('aarch64-linux-gnu-6.4.1.git gcc --depth 1', self.find('初始化并同步内核源码')['run'])
        self.assertIn('AnyKernel3.git AnyKernel3 --ref "$ANYKERNEL_BRANCH" --depth 1', self.find('克隆依赖仓库')['run'])

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
        self.assertIn('--ksu-source "$KERNEL_ROOT/KernelSU"', prepare['run'])
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
        matrix = next(step for step in prepare['steps'] if step.get('id') == 'matrix')
        self.assertIn('max_parallel={int(value)}', matrix['run'])

    def test_build_runner_is_pinned_before_ubuntu_latest_migration(self):
        self.assertNotIn('ubuntu-latest', self.workflow['jobs']['build-kernel']['runs-on'])

    def test_serialization_roundtrips_boolean_and_on(self):
        text = generator.yaml.dump(self.workflow, Dumper=generator.Dumper, allow_unicode=True, sort_keys=False)
        parsed = generator.yaml.load(text, Loader=generator.Loader)
        self.assertEqual(parsed, self.workflow)
        self.assertIs(parsed['on']['workflow_dispatch']['inputs']['include_lts']['default'], False)


if __name__ == '__main__':
    unittest.main()
