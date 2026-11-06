import tempfile
from pathlib import Path
import unittest

from additional_manager_kpm import template_files
from configure_bakasu_kpm_ui import configure, JAVA, SCREEN, WRAPPER


class BakaKpmUiTests(unittest.TestCase):
    def fixture(self, root):
        kbuild = root / 'kernel/Kbuild'
        kbuild.parent.mkdir()
        kbuild.write_text('REPO_NAME := BakaSU\n')
        screen = root / SCREEN
        screen.parent.mkdir(parents=True)
        screen.write_text(template_files()[SCREEN], encoding='utf-8')

    def test_common_page_preserves_native_authorization_and_shared_shell_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            configure(root)
            self.assertEqual(WRAPPER, (root / SCREEN).read_text(encoding='utf-8'))
            text = (root / JAVA / 'ui/kpm/GkiKpmScreen.kt').read_text(encoding='utf-8')
            self.assertIn('Natives.isManager && org.bakasu.bakasu.Natives.isFullFeatured()', text)
            self.assertIn('koinInject<KsuCliRepository>()', text)
            self.assertIn('val shell = kpmRootOrUnavailable { cli.getRootShell() }', text)
            self.assertNotIn('shell.close()', text)
            self.assertIn('selectedKpmModuleName(target.readBytes())', text)
            self.assertIn('Text("KPM 安装")', text)
            self.assertNotIn('点击 + 添加模块', text)
            before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            configure(root)
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_unrecognized_page_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / SCREEN).write_text('new upstream UI', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Review changed'):
                configure(root)
            self.assertEqual('new upstream UI', (root / SCREEN).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
