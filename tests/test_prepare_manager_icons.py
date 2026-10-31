"""A trimmed JAR must remain visible to Kotlin without changing manager identity."""
import pathlib
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from prepare_manager_icons import prepare_manager_icons


class ManagerIconsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = pathlib.Path(self.temporary.name)
        self.gradle = self.source / 'manager/app/build.gradle.kts'
        self.gradle.parent.mkdir(parents=True)
        self.catalog = self.source / 'manager/gradle/libs.versions.toml'
        self.catalog.parent.mkdir()
        self.catalog.write_text('[libraries]\nandroidx-compose-material-icons-extended = '
                                '{ group = "androidx.compose.material", name = "material-icons-extended" }\n')
        self.jar = self.source / 'manager/app/libs/material-icons-custom.jar'
        self.jar.parent.mkdir()
        self.gradle.write_text('val packageName = "com.sukisu.ultra"\n'
                               'dependencies {\n'
                               '    implementation(libs.androidx.compose.material.icons.core)\n'
                               '    implementation(files("libs/material-icons-custom.jar"))\n}\n')
        self.original = self.gradle.read_text()
        self.write_jar()

    def write_jar(self, metadata=None):
        with zipfile.ZipFile(self.jar, 'w') as archive:
            archive.writestr('androidx/compose/material/icons/filled/ExtensionKt.class', b'unit fixture')
            if metadata is not None:
                archive.writestr('META-INF/material-icons-extended.kotlin_module', metadata)

    def test_missing_index_switches_to_official_alias_and_is_idempotent(self):
        jar = self.jar.read_bytes()
        self.assertTrue(prepare_manager_icons(self.source))
        output = self.gradle.read_text()
        self.assertIn('implementation(libs.androidx.compose.material.icons.extended)', output)
        self.assertNotIn('implementation(files(', output)
        self.assertIn('com.sukisu.ultra', output)
        self.assertIn('implementation(libs.androidx.compose.material.icons.core)', output)
        self.assertEqual(jar, self.jar.read_bytes())
        self.assertFalse(prepare_manager_icons(self.source))
        self.assertEqual(output, self.gradle.read_text())

    def test_fixed_upstream_jar_and_legacy_extended_dependency_are_preserved(self):
        self.write_jar(b'nonempty module index fixture')
        self.assertFalse(prepare_manager_icons(self.source))
        self.assertEqual(self.original, self.gradle.read_text())
        legacy = self.original.replace('files("libs/material-icons-custom.jar")',
                                       'libs.androidx.compose.material.icons.extended')
        self.gradle.write_text(legacy)
        self.jar.unlink()
        self.assertFalse(prepare_manager_icons(self.source))
        self.assertEqual(legacy, self.gradle.read_text())

    def test_empty_index_is_not_accepted_and_existing_extended_is_not_duplicated(self):
        self.write_jar(b'')
        self.gradle.write_text(self.original.replace('dependencies {', 'dependencies {\n'
                               '    implementation(libs.androidx.compose.material.icons.extended)'))
        self.assertTrue(prepare_manager_icons(self.source))
        self.assertEqual(1, self.gradle.read_text().count('implementation(libs.androidx.compose.material.icons.extended)'))

    def test_dry_run_validates_without_modifying_exported_official_source(self):
        self.assertTrue(prepare_manager_icons(self.source, apply=False))
        self.assertEqual(self.original, self.gradle.read_text())

    def test_unknown_dependency_and_duplicate_declaration_fail_without_partial_changes(self):
        self.catalog.write_text('[libraries]\nandroidx-compose-material-icons-extended = '
                                '{ module = "other.publisher:unknown" }\n')
        with self.assertRaisesRegex(ValueError, 'official extended'):
            prepare_manager_icons(self.source)
        self.assertEqual(self.original, self.gradle.read_text())
        self.gradle.write_text(self.original + '\nimplementation(files("libs/material-icons-custom.jar"))\n')
        original = self.gradle.read_text()
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            prepare_manager_icons(self.source)
        self.assertEqual(original, self.gradle.read_text())

    def test_missing_or_corrupt_jar_is_rejected(self):
        for raw in (b'', b'not a jar'):
            self.jar.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                prepare_manager_icons(self.source)
        self.assertEqual(self.original, self.gradle.read_text())

    def test_build_and_manual_sync_use_the_same_preparation(self):
        build = (ROOT / 'tools/build_sukisu_manager.sh').read_text('utf-8')
        self.assertIn('python3 "$build_tools/prepare_manager_icons.py" --source "$source_dir"', build)
        self.assertLess(build.index('prepare_manager_icons.py'), build.index('cargo build'))
        sync = (ROOT / 'tools/verify_manager_update.py').read_text('utf-8')
        self.assertIn('prepare_manager_icons(source)', sync)


if __name__ == '__main__':
    unittest.main()
