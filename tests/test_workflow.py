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
        self.assertEqual(self.workflow['jobs']['prepare-matrix']['if'], generator.PREPARE_IF)
        self.assertEqual(self.workflow['jobs']['build-kernel']['if'], generator.KERNEL_JOB_IF)
        self.assertIn(generator.BUILD_IF, generator.KERNEL_JOB_IF)
        self.assertIn("needs.prepare-matrix.result == 'success'", generator.KERNEL_JOB_IF)
        self.assertIn("needs.build-sukisu-manager.result == 'success'", generator.KERNEL_JOB_IF)
        self.assertIn("inputs.kernelsu_variant != 'SukiSU'", generator.KERNEL_JOB_IF)
        self.assertIn("github.event_name == 'workflow_dispatch'", generator.BUILD_IF)
        self.assertIn("inputs.operation == '编译内核'", generator.BUILD_IF)
        self.assertEqual(self.workflow['on']['schedule'], [{'cron': '17 0 * * *'}])
        operation = self.workflow['on']['workflow_dispatch']['inputs']['operation']
        self.assertEqual(operation['default'], '编译内核')
        self.assertEqual(operation['options'], ['编译内核', '仅编译 SukiSU 安装包', '检查上游更新'])
        self.assertNotIn('仅编译 SukiSU 安装包', generator.BUILD_IF)
        self.assertNotIn('schedule', generator.PREPARE_IF)
        self.assertEqual(self.workflow['jobs']['prepare-matrix']['steps'][1]['if'], generator.BUILD_IF)

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
        self.assertNotIn('auto-merge', str(steps))
        update = indexed['update'][1]
        self.assertEqual(update['env']['OFFICIAL_CANDIDATES'], '${{ steps.official.outputs.candidates_json }}')
        self.assertEqual(update['env']['CATALOG_CANDIDATES'], '${{ steps.catalog.outputs.candidates_json }}')
        self.assertIn('--candidates-json "$OFFICIAL_CANDIDATES"', update['run'])
        self.assertIn('--apply-candidates-json "$CATALOG_CANDIDATES"', update['run'])
        self.assertNotIn('sync_upstream.py', str(steps))
        self.assertNotIn('xingguangcuican6666', str(steps))

    def test_notification_comes_from_bot_only_when_pr_changed(self):
        step = next(step for step in self.workflow['jobs']['check-upstream']['steps']
                    if step['name'] == '提醒所有者审核官方更新')
        self.assertEqual(step['env']['GH_TOKEN'], '${{ github.token }}')
        self.assertIn("pull-request-operation == 'created'", step['if'])
        self.assertIn("pull-request-operation == 'updated'", step['if'])
        self.assertIn('issues/$PR_NUMBER/comments', step['run'])

    def test_custom_ksu_ref_remains_available_through_repository_variable(self):
        sources = next(step for step in self.workflow['jobs']['prepare-matrix']['steps'] if step.get('id') == 'sources')
        self.assertEqual(sources['env']['CUSTOM_REF'], '${{ vars.KSU_CUSTOM_REF }}')
        self.assertIn('tools/resolve_build_sources.py', sources['run'])
        step = self.find('确定 KernelSU 分支')
        self.assertEqual(step['env']['BRANCH'], '${{ needs.prepare-matrix.outputs.ksu_ref }}')
        self.assertNotIn('resolve-ksu-ref.sh', step['run'])

    def test_sukisu_manager_is_built_once_and_kernel_uses_its_signature(self):
        manager = self.workflow['jobs']['build-sukisu-manager']
        self.assertEqual(manager['needs'], 'prepare-matrix')
        self.assertNotIn('strategy', manager)
        self.assertEqual(manager['if'], generator.MANAGER_IF)
        checkout = next(step for step in manager['steps'] if step['name'] == '检出同一版本的 SukiSU 源码')
        self.assertEqual(checkout['with']['ref'], '${{ needs.prepare-matrix.outputs.ksu_ref }}')
        self.assertEqual(checkout['with']['fetch-depth'], 0)
        self.assertIs(checkout['with']['persist-credentials'], False)
        build = next(step for step in manager['steps'] if step['name'] == '编译管理器与同版本 ksud')
        self.assertEqual(build['env']['KSU_PACKAGE_NAME'], build['env']['ORG_GRADLE_PROJECT_KSU_PACKAGE_NAME'])
        self.assertEqual(build['env']['KSU_PACKAGE_NAME'], 'com.cyzrqh.gki.sukisu')
        self.assertEqual(build['env']['ORG_GRADLE_PROJECT_KSU_NAME'], 'SukiSU')
        self.assertNotIn('secrets.', str(build))
        sign = next(step for step in manager['steps'] if step.get('id') == 'sign')
        self.assertIn('secrets.SUKISU_APK_KEYSTORE', sign['env']['SUKISU_APK_KEYSTORE'])
        kernel = self.workflow['jobs']['build-kernel']
        self.assertEqual(kernel['needs'], ['prepare-matrix', 'build-sukisu-manager'])
        signature = self.find('配对管理器包名与签名')
        for key in ('MANAGER_PACKAGE', 'MANAGER_CERT_SIZE', 'MANAGER_CERT_SHA256'):
            self.assertIn('needs.build-sukisu-manager.outputs.', signature['env'][key])
        self.assertIn('--package "$MANAGER_PACKAGE"', signature['run'])
        names = [step.get('name') for step in self.steps]
        self.assertLess(names.index(signature['name']), names.index('编译内核'))

    def test_release_contains_direct_files_and_only_runs_after_success(self):
        release = self.workflow['jobs']['publish-release']
        self.assertEqual(release['permissions'], {'contents': 'write'})
        self.assertIn("needs.build-kernel.result == 'success'", release['if'])
        self.assertIn("inputs.operation == '仅编译 SukiSU 安装包'", release['if'])
        self.assertNotIn('schedule', release['if'])
        self.assertIn("github.event_name == 'workflow_dispatch'", release['if'])
        publish = next(step for step in release['steps'] if step.get('id') == 'release')
        self.assertIn('--notes-file release-publish/release.md', publish['run'])
        self.assertLess(publish['run'].index('--draft --title'), publish['run'].index('gh release upload'))
        self.assertLess(publish['run'].index('gh release upload'), publish['run'].index('--draft=false'))
        self.assertIn('CONFIG_IKCONFIG=y', self.find('在内核中记录实际构建配置')['run'])
        self.assertIn('capture_build_metadata.py', self.find('记录内核实际配置')['run'])

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
        self.assertEqual(pin['env']['PIN_KSU_TIER'], '${{ inputs.kernelsu_branch }}')
        self.assertEqual(pin['if'], "inputs.enable_susfs && inputs.kernelsu_variant == 'SukiSU'")

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
        self.assertEqual(upload['with']['path'], 'AnyKernel3/*')
        self.assertEqual(upload['if'], "inputs.artifact_mode == '仅 AK3'")
        self.assertNotIn('打包签名内核 Bundle', [step['name'] for step in self.steps])

    def test_ak3_preparation_does_not_require_compressed_image(self):
        run = self.find('准备 AK3 内核文件')['run']
        self.assertLess(run.index("if [ \"$ARTIFACT_MODE\" != '仅 AK3' ]"), run.index('Image.lz4'))
        self.assertIn('lz4 -l -12 -f ./Image bootimgs/Image.lz4', run)
        self.assertIn('lz4 -dc bootimgs/Image.lz4 | cmp - ./Image', run)
        self.assertNotIn('$src/Image.lz4', run)

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
