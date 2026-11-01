import unittest
from pathlib import Path

from additional_manager_kpm import FEATURE_ID, template_files
from generate_workflow import create_workflow
from oplus_workflow import create_workflow as create_oplus
from manager_variants import ADDITIONAL_MANAGERS

ROOT = Path(__file__).resolve().parents[1]


class AdditionalKpmTests(unittest.TestCase):
    def test_additive_interface_keeps_original_operation_and_permission_contract(self):
        source = template_files()
        self.assertGreater(FEATURE_ID, 10)  # FinaSU already owns IDs 5..10.
        self.assertIn('SUKISU_KPM_ABI_VERSION 2', source['kernel/kpm/bridge.h'])
        self.assertIn('result_code', source['uapi/kpm.h'])
        self.assertIn('copy_from_user', source['kernel/kpm/kpm.c'])
        self.assertNotIn('always_allow', source['kernel/kpm/kpm.c'])

    def test_both_matrices_build_modified_apks_and_do_not_silently_skip_kpm(self):
        single = create_workflow(ROOT)
        for workflow in (single, create_oplus(ROOT, single)):
            jobs = workflow['jobs']
            manager = jobs['build-sukisu-manager']
            names = [step.get('name', '') for step in manager['steps']]
            self.assertNotIn('获取并验证官方同版本管理器 APK', names)
            self.assertIn('编译管理器与同版本 ksud', names)
            build = next(step for step in manager['steps'] if step.get('id') == 'manager-build')
            self.assertIn('tools/build_additional_manager.sh', build['run'])
            self.assertIn('needs.build-sukisu-lkm.result', manager['if'])
            for variant in ADDITIONAL_MANAGERS:
                self.assertIn(variant, jobs['build-sukisu-lkm']['if'])
        kernel = single['jobs']['build-kernel']
        step = next(step for step in kernel['steps'] if step.get('name') == '应用 KPM 补丁 (全部管理器)')
        self.assertEqual('inputs.use_kpm', step['if'])

    def test_all_source_builders_apply_manager_only_update_defaults(self):
        for script in ('build_sukisu_manager.sh', 'build_bakasu_manager.sh', 'build_additional_manager.sh'):
            self.assertIn('manager_ui_defaults.py', (ROOT / 'tools' / script).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
