import copy
import sys
import unittest
from pathlib import Path

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

    def test_update_events_cannot_start_kernel_compilation(self):
        for name in ('prepare-matrix', 'build-kernel'):
            self.assertEqual(self.workflow['jobs'][name]['if'], generator.BUILD_IF)
        self.assertIn("github.event_name == 'workflow_dispatch'", generator.BUILD_IF)
        self.assertIn("inputs.operation == '编译内核'", generator.BUILD_IF)
        self.assertEqual(self.workflow['on']['schedule'], [{'cron': '17 0 * * *'}])
        operation = self.workflow['on']['workflow_dispatch']['inputs']['operation']
        self.assertEqual(operation['default'], '编译内核')
        self.assertEqual(operation['options'], ['编译内核', '检查上游更新'])

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
        self.assertIn('.github/workflows/kernel-full-feature-matrix.yml', pr['with']['add-paths'])
        self.assertNotIn('auto-merge', str(steps))
        update = indexed['update'][1]
        self.assertEqual(update['env']['CANDIDATES'], '${{ steps.probe.outputs.latest_json }}')
        self.assertIn('--abk-commit "$abk_commit"', update['run'])
        self.assertIn('--catalog-commit "$catalog_commit"', update['run'])

    def test_notification_comes_from_bot_only_when_pr_changed(self):
        step = next(step for step in self.workflow['jobs']['check-upstream']['steps']
                    if step['name'] == '提醒仓库所有者审核更新')
        self.assertEqual(step['env']['GH_TOKEN'], '${{ github.token }}')
        self.assertIn("pull-request-operation == 'created'", step['if'])
        self.assertIn("pull-request-operation == 'updated'", step['if'])
        self.assertIn('issues/$PR_NUMBER/comments', step['run'])

    def test_custom_ksu_ref_remains_available_through_repository_variable(self):
        step = self.find('确定 KernelSU 分支')
        self.assertEqual(step['env']['CUSTOM_REF'], '${{ vars.KSU_CUSTOM_REF }}')

    def test_ak3_is_default_and_boot_steps_are_gated(self):
        self.assertEqual(self.workflow['on']['workflow_dispatch']['inputs']['artifact_mode']['default'], '仅 AK3')
        for name in ('构建 Boot 镜像 (Android 12)', '构建 Boot 镜像 (Android 13+)'):
            self.assertIn("inputs.artifact_mode != '仅 AK3'", self.find(name)['if'])
        upload = self.find('上传可直接刷入的 AK3 包')
        self.assertEqual(upload['with']['path'], 'AnyKernel3/*')
        self.assertEqual(upload['if'], "inputs.artifact_mode == '仅 AK3'")
        self.assertNotIn('打包签名内核 Bundle', [step['name'] for step in self.steps])

    def test_ak3_preparation_does_not_require_compressed_image(self):
        run = self.find('准备 AK3 内核文件')['run']
        self.assertLess(run.index("if [ \"$ARTIFACT_MODE\" != '仅 AK3' ]"), run.index('Image.lz4'))

    def test_month_is_part_of_workspace_and_artifact_identity(self):
        run = self.find('初始化构建环境')['run']
        self.assertIn('${{ matrix.sub_level }}-${{ matrix.os_patch_level }}', run)

    def test_catalog_refs_are_applied_before_repo_sync(self):
        step = self.find('初始化并同步内核源码')
        self.assertEqual(step['env']['GKI_KERNEL_REF'], '${{ matrix.kernel_ref }}')
        self.assertLess(step['run'].index('tools/set_kernel_ref.py'), step['run'].index('"$REPO" sync'))

    def test_whitelist_contains_new_months_and_latest_versions(self):
        run = self.find('校验自定义内核版本组合')['run']
        for pair in ('android14 6.1 145 2025-08', 'android15 6.6 143 2026-10', 'android16 6.12 92 2026-09'):
            self.assertIn(pair, run)

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
