"""Provider selection and complete native palette coverage for the KPM page."""
import json
from pathlib import Path
import re
import unittest

from additional_manager_kpm_ui import render_kpm_templates
from kpm_theme import detect_kpm_theme, render_kpm_theme


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/kpm-theme-providers.json'
MATERIAL_ROLES = {
    'primary', 'onPrimary', 'primaryContainer', 'onPrimaryContainer', 'inversePrimary',
    'secondary', 'onSecondary', 'secondaryContainer', 'onSecondaryContainer',
    'tertiary', 'onTertiary', 'tertiaryContainer', 'onTertiaryContainer',
    'background', 'onBackground', 'surface', 'onSurface', 'surfaceVariant',
    'onSurfaceVariant', 'surfaceTint', 'inverseSurface', 'inverseOnSurface',
    'error', 'onError', 'errorContainer', 'onErrorContainer', 'outline',
    'outlineVariant', 'scrim', 'surfaceBright', 'surfaceDim',
    'surfaceContainerLowest', 'surfaceContainerLow', 'surfaceContainer',
    'surfaceContainerHigh', 'surfaceContainerHighest',
    'primaryFixed', 'primaryFixedDim', 'onPrimaryFixed', 'onPrimaryFixedVariant',
    'secondaryFixed', 'secondaryFixedDim', 'onSecondaryFixed', 'onSecondaryFixedVariant',
    'tertiaryFixed', 'tertiaryFixedDim', 'onTertiaryFixed', 'onTertiaryFixedVariant',
}


class KpmThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text('utf-8'))

    def test_reviewed_stable_and_latest_native_providers_choose_the_actual_theme(self):
        for source in self.fixture['providers']:
            with self.subTest(manager=source['manager'], tier=source['tier']):
                self.assertRegex(source['commit'], r'^[0-9a-f]{40}$')
                self.assertRegex(source['source_sha256'], r'^[0-9a-f]{64}$')
                self.assertEqual(source['expected'], detect_kpm_theme(
                    source['namespace'], source['theme'], source['ui_mode']))

    def test_switching_managers_use_their_native_runtime_selector_and_package(self):
        for source in self.fixture['providers']:
            if source['expected'] != 'miuix-switch':
                continue
            with self.subTest(manager=source['manager'], tier=source['tier']):
                theme = render_kpm_templates(source['namespace'], 'cached',
                    theme_api=source['expected'])['GkiKpmTheme.kt']
                self.assertIn('import ' + source['namespace'] + '.ui.LocalUiMode', theme)
                self.assertIn('import ' + source['namespace'] + '.ui.UiMode', theme)
                self.assertIn('if (LocalUiMode.current == UiMode.Miuix)', theme)
                self.assertIn('kpmMaterialColors(MiuixTheme.colorScheme)', theme)
                self.assertIn('} else {\n        content()\n', theme)
                self.assertNotIn('__', theme)

    def test_material_managers_inherit_the_exact_parent_theme_without_replacing_it(self):
        for namespace in ('com.rifsxd.ksunext', 'me.weishu.kernelsu', 'org.bakasu.bakasu'):
            with self.subTest(namespace=namespace):
                theme = render_kpm_templates(namespace, 'cached', theme_api='material3')['GkiKpmTheme.kt']
                self.assertIn('internal fun GkiKpmTheme(content: @Composable () -> Unit)', theme)
                self.assertIn('    content()', theme)
                for reset in ('MaterialTheme(', 'ColorScheme(', 'MiuixTheme', 'Color(', 'isSystemInDarkTheme', 'remember'):
                    self.assertNotIn(reset, theme)

    def test_miuix_maps_every_material_role_to_a_real_native_color(self):
        theme = render_kpm_theme('miuix-switch')
        mapping = theme.split('= ColorScheme(\n', 1)[1].split('\n)', 1)[0]
        assignments = re.findall(r'^    (\w+) = native\.(\w+),$', mapping, re.M)
        self.assertEqual(MATERIAL_ROLES, {role for role, _ in assignments})
        self.assertEqual(len(MATERIAL_ROLES), len(assignments))
        native_roles = set(self.fixture['miuix']['properties'])
        self.assertTrue({role for _, role in assignments} <= native_roles)
        self.assertNotIn('Color(', theme)
        self.assertNotIn('lightColorScheme', theme)
        self.assertNotIn('darkColorScheme', theme)
        self.assertNotIn('isSystemInDarkTheme', theme)

    def test_theme_changes_are_observed_each_composition_without_a_cached_seed_palette(self):
        theme = render_kpm_theme('miuix-switch')
        body = theme.split('internal fun GkiKpmTheme(', 1)[1]
        self.assertIn('kpmMaterialColors(MiuixTheme.colorScheme)', body)
        self.assertNotRegex(body, r'\bremember\s*[({]')
        self.assertNotIn('dynamicLightColorScheme', body)
        self.assertNotIn('dynamicDarkColorScheme', body)

    def test_whole_page_and_all_dialogs_are_children_of_the_theme(self):
        screen = render_kpm_templates('example.manager', 'cached')['GkiKpmScreen.kt']
        self.assertIn('GkiKpmTheme { GkiKpmScreenContent(bottomPadding) }', screen)
        content = screen.split('private fun GkiKpmScreenContent(', 1)[1]
        for component in ('Scaffold(', 'TopAppBar(', 'ExtendedFloatingActionButton(',
                          'Card(', 'OutlinedTextField(', 'AlertDialog(', 'LinearProgressIndicator('):
            self.assertIn(component, content)
        self.assertIn('containerColor = MaterialTheme.colorScheme.surface,', content)
        self.assertEqual(2, content.count('containerColor = MaterialTheme.colorScheme.surfaceContainer,'))
        self.assertEqual(2, content.count('contentColor = MaterialTheme.colorScheme.onSurface,'))
        self.assertNotIn('containerColor = MaterialTheme.colorScheme.secondaryContainer', content)
        self.assertIn('tint = MaterialTheme.colorScheme.primary', content)
        self.assertNotRegex(content, r'Color\.(?:White|Black)|Color\(0x')

    def test_theme_rendering_keeps_each_shell_ownership_and_kpm_operation_contract(self):
        for shell in ('cached', 'new', 'bakasu'):
            with self.subTest(shell=shell):
                material = render_kpm_templates('example.manager', shell, 'material3')
                miuix = render_kpm_templates('example.manager', shell, 'miuix-switch')
                self.assertEqual(material['GkiKpmScreen.kt'], miuix['GkiKpmScreen.kt'])
                self.assertEqual(material['GkiKpmModule.kt'], miuix['GkiKpmModule.kt'])
                screen = miuix['GkiKpmScreen.kt']
                for operation in ('"supported", "--json"', 'probe.getInt("abi") == 2',
                                  'if (embed) "install" else "load"', 'command("control", name, arguments)',
                                  'selectedKpmModuleName(target.readBytes())'):
                    self.assertIn(operation, screen)
                self.assertEqual(shell == 'new', 'shell.close()' in screen)

    def test_unknown_theme_layouts_and_wrong_selector_namespaces_require_review(self):
        source = next(p for p in self.fixture['providers'] if p['expected'] == 'miuix-switch')
        for theme, selector in (
            ('UnknownTheme(content)', ''),
            ('MiuixTheme { MaterialTheme { content() } }', ''),
            (source['theme'], ''),
            (source['theme'], source['ui_mode'].replace('package me.weishu.kernelsu.ui', 'package wrong.ui')),
            (source['theme'].replace('LocalUiMode.current', 'newThemeSelection()'), source['ui_mode']),
        ):
            with self.subTest(theme=theme[:70], selector=selector[:70]):
                with self.assertRaisesRegex(ValueError, 'Review the native manager'):
                    detect_kpm_theme(source['namespace'], theme, selector)
        with self.assertRaisesRegex(ValueError, 'Unknown native manager'):
            render_kpm_theme('default-pink')

    def test_direct_miuix_provider_uses_the_current_palette_without_a_selector(self):
        self.assertEqual('miuix', detect_kpm_theme('example.manager', 'MiuixTheme(content = content)'))
        theme = render_kpm_templates('example.manager', 'cached', 'miuix')['GkiKpmTheme.kt']
        self.assertIn('if (true)', theme)
        self.assertNotIn('LocalUiMode', theme)
        self.assertIn('kpmMaterialColors(MiuixTheme.colorScheme)', theme)


if __name__ == '__main__':
    unittest.main()
