from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


class ManagerBuildSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / 'tests' / ('.manager-build-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.gradle = self.directory / 'build.gradle.kts'
        script = (ROOT / 'tools/build_sukisu_manager.sh').read_text(encoding='utf-8')
        self.transform = script.split('python3 - "$manager_dir/app/build.gradle.kts" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
        self.source = '''plugins {
    alias(libs.plugins.agp.app)
    alias(libs.plugins.lsplugin.apksign)
}
apksign {
    storeFileProperty = "KEYSTORE_FILE"
    storePasswordProperty = "KEYSTORE_PASSWORD"
    keyAliasProperty = "KEY_ALIAS"
    keyPasswordProperty = "KEY_PASSWORD"
}
android {
    buildTypes {
        release {
            isMinifyEnabled = true
        }
    }
    defaultConfig {
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }
}
protobuf { untouched() }
'''

    def run_transform(self, source):
        self.gradle.write_text(source, encoding='utf-8')
        return subprocess.run([sys.executable, '-c', self.transform, str(self.gradle)],
                              capture_output=True, text=True, check=False)

    def test_keeps_only_arm64_and_removes_debug_signing_fallback(self):
        result = self.run_transform(self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.gradle.read_text(encoding='utf-8')
        self.assertIn('abiFilters += listOf("arm64-v8a")', output)
        self.assertNotIn('x86_64', output)
        self.assertNotIn('lsplugin.apksign', output)
        self.assertNotIn('KEYSTORE_FILE', output)
        self.assertEqual(output.count('signingConfig = null'), 1)
        self.assertIn('isMinifyEnabled = true', output)
        self.assertIn('protobuf { untouched() }', output)

    def test_signing_layout_drift_fails_without_partial_rewrite(self):
        variants = [self.source.replace('KEY_PASSWORD', 'NEW_PASSWORD'),
                    self.source.replace('keyAliasProperty', 'futureAliasProperty'),
                    self.source + 'apksign {}\n',
                    self.source + 'val signingConfig = dangerous()\n',
                    self.source.replace('release {', 'futureRelease {')]
        for source in variants:
            with self.subTest(source=source):
                result = self.run_transform(source)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.gradle.read_text(encoding='utf-8'), source)

    def test_ambiguous_or_incomplete_abi_is_not_rewritten(self):
        for source in [self.source.replace('"arm64-v8a", ', ''),
                       self.source.replace('"x86_64"', 'extraAbi'),
                       self.source + 'abiFilters += listOf("arm64-v8a")\n']:
            with self.subTest(source=source):
                result = self.run_transform(source)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.gradle.read_text(encoding='utf-8'), source)


if __name__ == '__main__':
    unittest.main()
