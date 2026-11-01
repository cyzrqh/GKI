from pathlib import Path
import tempfile
import unittest

from manager_ui_defaults import apply_defaults, plan_defaults


class ManagerDefaultsTests(unittest.TestCase):
    def source(self, root, text, name='SettingsRepositoryImpl.kt'):
        path = root / 'manager/app/src/main/java/example' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_manager_default_off_keeps_module_default_and_saved_preference_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.source(root, '''
override var checkUpdate: Boolean
    get() = prefs.getBoolean("check_update", true)
    set(value) = prefs.edit { putBoolean("check_update", value) }
override var checkModuleUpdate: Boolean
    get() = prefs.getBoolean("module_check_update", true)
    set(value) = prefs.edit { putBoolean("module_check_update", value) }
''')
            apply_defaults(root, 'KernelSU')
            text = path.read_text()
            self.assertIn('getBoolean("check_update", false)', text)
            self.assertIn('getBoolean("module_check_update", true)', text)
            self.assertIn('putBoolean("check_update", value)', text)
            self.assertEqual([], apply_defaults(root, 'KernelSU'))

    def test_baka_module_setting_and_refresh_do_not_inherit_disabled_manager_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.source(root, '''
checkManagerUpdate = settings.getBoolean("check_update", true)
checkBetaUpdate = settings.getBoolean("check_beta_update", true)
val module = settings.getBoolean("check_module_update", settings.getBoolean("check_update", true))
''', 'SettingsPlatformRepository.kt')
            modules = self.source(root, '''
val enabled = getBooleanPreference(
    PREF_CHECK_MODULE_UPDATE,
    getBooleanPreference(PREF_CHECK_UPDATE, true),
)
refreshInstalledModules(manual, enabled)
''', 'ModuleViewModel.kt')
            apply_defaults(root, 'BakaSU')
            self.assertIn('getBoolean("check_beta_update", false)', settings.read_text())
            self.assertIn('getBoolean("check_module_update", true)', settings.read_text())
            self.assertNotIn('getBooleanPreference(PREF_CHECK_UPDATE', modules.read_text())
            self.assertIn('refreshInstalledModules(manual, enabled)', modules.read_text())

    def test_unknown_upstream_layout_fails_before_partial_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.source(root, 'val checkModuleUpdate: Boolean = true')
            with self.assertRaises(ValueError):
                apply_defaults(root, 'KowSU')
            self.assertEqual('val checkModuleUpdate: Boolean = true', path.read_text())

    def test_remove_only_requested_kpm_notice_clause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.source(root, 'prefs.getBoolean("check_update", true)')
            path = root / 'manager/app/src/main/res/values-zh-rCN/strings.xml'
            path.parent.mkdir(parents=True)
            path.write_text('<resources><string name="kpm_manual_only">加载仅本次开机有效；嵌入会保存模块与参数，并在重启后自动加载。</string></resources>', encoding='utf-8')
            apply_defaults(root, 'BakaSU')
            self.assertNotIn('加载仅本次开机有效', path.read_text(encoding='utf-8'))
            self.assertIn('嵌入会保存模块与参数，并在重启后自动加载。', path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
