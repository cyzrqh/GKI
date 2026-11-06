"""Keep the native SukiSU KPM implementation behind an early real auth gate."""
from pathlib import Path
import tempfile
import unittest

from configure_sukisu_kpm_ui import configure, ENTRY, GUARD, JAVA, MARKER, SCREEN


ROOT = Path(__file__).resolve().parents[1]
NATIVE_SCREEN = '''package com.sukisu.ultra.ui.screen.kpm

import kotlinx.coroutines.CancellationException

@Composable
fun KpmScreen(bottomInnerPadding: Dp = 0.dp) {
    val context = LocalContext.current
    val appContext = context.applicationContext
    val scope = rememberCoroutineScope()
    val viewModel = viewModel<KpmViewModel>()
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        loadKpmModule(uri)
    }
    LaunchedEffect(lifecycleOwner, viewModel) {
        lifecycleOwner.lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
            viewModel.fetchModuleList(silent = true)
        }
    }
    val install = { embed -> handleModuleInstall(context, embed) }
    val unload = { handleModuleUninstall(context) }
    when (LocalUiMode.current) {
        UiMode.Miuix -> KpmMiuix(viewModel, actions, bottomInnerPadding)
        UiMode.Material -> KpmMaterial(viewModel, actions, bottomInnerPadding)
    }
}
'''
NATIVES = '''package com.sukisu.ultra
object Natives {
    val isManager: Boolean
        external get
    fun isPreviewMode(): Boolean = !isManager && rootAvailable()
    fun isFullFeatured(): Boolean = isManager || isPreviewMode()
}
'''
KSU_CLI = '''package com.sukisu.ultra.ui.util
fun rootAvailable(): Boolean {
    val shell = getRootShell()
    return shell.isRoot
}
'''


class SukiKpmUiTests(unittest.TestCase):
    def source(self, root, screen=NATIVE_SCREEN, newline='\n'):
        for relative, text in (('kernel/Kbuild', 'REPO_NAME   := SukiSU-Ultra\n'),
                               (JAVA + '/Natives.kt', NATIVES),
                               (JAVA + '/ui/util/KsuCli.kt', KSU_CLI), (SCREEN, screen)):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.replace('\n', newline).encode('utf-8'))
        return root / SCREEN

    def test_authorization_precedes_all_native_work_and_other_bytes_are_preserved(self):
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=repr(newline)), tempfile.TemporaryDirectory(prefix='.sukisu-kpm-', dir=ROOT / 'tests') as directory:
                root = Path(directory)
                page = self.source(root, newline=newline)
                original = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                self.assertTrue(configure(root))
                updated = page.read_bytes()
                gate = GUARD.replace('\n', newline).encode('utf-8')
                self.assertEqual(original[page], updated.replace(gate, b'', 1))
                self.assertEqual(1, updated.count(MARKER.encode()))
                normalized = updated.decode('utf-8').replace('\r\n', '\n')
                start = normalized.index('if (!kpmAuthorized) return')
                for work in ('val context =', 'rememberCoroutineScope()', 'viewModel<KpmViewModel>()',
                             'rememberLauncherForActivityResult(', 'LaunchedEffect('):
                    self.assertLess(start, normalized.index(work))
                self.assertIn('Natives.isManager &&\n            com.sukisu.ultra.Natives.isFullFeatured() &&\n'
                              '            com.sukisu.ultra.ui.util.rootAvailable()', normalized)
                self.assertIn('catch (cancelled: CancellationException) {\n        throw cancelled', normalized)
                self.assertEqual(1, normalized.count('catch (_: Exception)'))
                for path, before in original.items():
                    if path != page:
                        self.assertEqual(before, path.read_bytes())
                self.assertFalse(configure(root))
                self.assertEqual(updated, page.read_bytes())

    def test_altered_or_partial_guard_is_rejected_without_changing_source(self):
        for guard in (GUARD.replace('com.sukisu.ultra.Natives.isManager &&\n', ''),
                      GUARD.replace('throw cancelled', 'false'),
                      GUARD.replace(' &&\n            com.sukisu.ultra.ui.util.rootAvailable()', '')):
            with self.subTest(guard=guard), tempfile.TemporaryDirectory(prefix='.sukisu-kpm-', dir=ROOT / 'tests') as directory:
                root = Path(directory)
                page = self.source(root, NATIVE_SCREEN.replace(ENTRY, ENTRY + guard, 1))
                original = page.read_bytes()
                with self.assertRaisesRegex(ValueError, 'altered'):
                    configure(root)
                self.assertEqual(original, page.read_bytes())

    def test_unknown_entry_theme_or_authorization_api_requires_review(self):
        for changed in (
            NATIVE_SCREEN.replace(ENTRY, ENTRY + '    startProbe()\n'),
            NATIVE_SCREEN.replace('KpmMiuix(viewModel, actions, bottomInnerPadding)', 'NewKpmPage()'),
            NATIVE_SCREEN.replace('val viewModel = viewModel<KpmViewModel>()', 'val viewModel = differentModel()'),
            NATIVE_SCREEN.replace('val appContext =', 'val kpmAuthorized ='),
        ):
            with self.subTest(screen=changed), tempfile.TemporaryDirectory(prefix='.sukisu-kpm-', dir=ROOT / 'tests') as directory:
                root = Path(directory)
                page = self.source(root, changed)
                original = page.read_bytes()
                with self.assertRaisesRegex(ValueError, 'Review'):
                    configure(root)
                self.assertEqual(original, page.read_bytes())
        with tempfile.TemporaryDirectory(prefix='.sukisu-kpm-', dir=ROOT / 'tests') as directory:
            root = Path(directory)
            page = self.source(root)
            (root / JAVA / 'Natives.kt').write_text(NATIVES.replace('val isManager:', 'val newIdentity:'), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'authorization APIs'):
                configure(root)
            self.assertEqual(NATIVE_SCREEN, page.read_text('utf-8'))

    def test_explicit_native_root_check_is_required_even_when_legacy_full_layout_is_true(self):
        # The real isFullFeatured API has a legacy-kernel branch that returns
        # true without checking root. It must not be the last authorization gate.
        with tempfile.TemporaryDirectory(prefix='.sukisu-kpm-', dir=ROOT / 'tests') as directory:
            root = Path(directory)
            page = self.source(root)
            (root / JAVA / 'Natives.kt').write_text(NATIVES.replace(
                'isManager || isPreviewMode()', 'true'), encoding='utf-8')
            self.assertTrue(configure(root))
            guard = page.read_text('utf-8').split('val kpmAuthorized = try {', 1)[1].split('} catch', 1)[0]
            checks = [part.strip() for part in guard.strip().split('&&')]
            self.assertEqual(['com.sukisu.ultra.Natives.isManager',
                              'com.sukisu.ultra.Natives.isFullFeatured()',
                              'com.sukisu.ultra.ui.util.rootAvailable()'], checks)

    def test_changed_or_missing_native_root_availability_api_requires_review(self):
        for changed in (KSU_CLI.replace('fun rootAvailable()', 'fun renamedRootAvailable()'),
                        KSU_CLI.replace('package com.sukisu.ultra.ui.util', 'package another.manager'),
                        KSU_CLI + '\nfun rootAvailable(): Boolean = true\n'):
            with self.subTest(api=changed), tempfile.TemporaryDirectory(prefix='.sukisu-kpm-', dir=ROOT / 'tests') as directory:
                root = Path(directory)
                page = self.source(root)
                (root / JAVA / 'ui/util/KsuCli.kt').write_text(changed, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'root availability API'):
                    configure(root)
                self.assertEqual(NATIVE_SCREEN, page.read_text('utf-8'))

    def test_source_builder_runs_the_gate_before_native_and_apk_compilation(self):
        script = (ROOT / 'tools/build_sukisu_manager.sh').read_text('utf-8')
        call = 'python3 "$build_tools/configure_sukisu_kpm_ui.py" --source "$source_dir"'
        self.assertEqual(1, script.count(call))
        self.assertLess(script.index(call), script.index('bash "$build_tools/build_manager_native.sh"'))
        self.assertLess(script.index(call), script.index('assembleRelease'))


if __name__ == '__main__':
    unittest.main()
