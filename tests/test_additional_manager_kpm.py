import unittest
from pathlib import Path
from unittest.mock import patch

from additional_manager_kpm import FEATURE_ID, template_files
from generate_workflow import create_workflow
from oplus_workflow import create_workflow as create_oplus
from manager_variants import ADDITIONAL_MANAGERS
from additional_manager_kpm_ui import manager_ui_plan

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

    def test_next_uses_collected_output_and_closes_its_native_shell_on_errors(self):
        java = 'manager/app/src/main/java/example/manager'
        files = {
            java + '/ui/MainActivity.kt': 'package example.manager.ui\n',
            java + '/ui/util/KsuCli.kt': 'fun createRootShell() = nativeShell()\n',
            java + '/ui/screen/BottomBarDestination.kt':
                'import com.ramcosta.composedestinations.spec.DirectionDestinationSpec\n'
                'enum class BottomBarDestination {\n    SuperUser(original),\n}\n',
            'manager/app/src/main/res/values/strings.xml': '<resources></resources>',
        }
        def git_output(args, **kwargs):
            if 'ls-tree' in args:
                return '\n'.join(files)
            return files[args[-1].split(':', 1)[1]].encode()
        with patch('additional_manager_kpm_ui.subprocess.check_output', side_effect=git_output):
            plan = manager_ui_plan(Path('official-next'), 'a' * 40, 'KernelSU-Next')
        screen = plan[java + '/ui/kpm/GkiKpmScreen.kt'].decode()
        self.assertIn('.to(ArrayList<String>(), ArrayList<String>()).exec()', screen)
        self.assertIn('val shell = createRootShell()\n        try {', screen)
        self.assertIn('} finally {\n            shell.close()', screen)
        self.assertIn('check(shell.isRoot)', screen)
        self.assertIn('check(result.isSuccess)', screen)
        self.assertIn('check(text.isNotEmpty())', screen)
        self.assertNotIn('JSONObject(command(', screen)
        self.assertIn('JSONObject(jsonCommand(', screen)
        self.assertIn('FloatingActionButton', screen)
        self.assertIn('ActivityResultContracts.GetContent()', screen)
        self.assertIn('if (embed) "install" else "load"', screen)
        self.assertIn('selectedKpmModuleName(target.readBytes())', screen)
        self.assertIn('text = { Text("KPM 安装") }', screen)
        self.assertIn('"installed", "--json"', screen)
        self.assertIn('"uninstall", name', screen)
        self.assertIn('module.file.delete()', screen)
        nav = plan[java + '/ui/screen/BottomBarDestination.kt'].decode()
        self.assertIn('Icons.Outlined.Layers, true)', nav)


if __name__ == '__main__':
    unittest.main()
