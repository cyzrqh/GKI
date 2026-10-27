import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from manager_lkm_jobs import KMIS


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


class ManagerBuildNativeAssetsTests(unittest.TestCase):
    """Small synthetic ELF headers exercise guards, never the production LKM stage."""
    def setUp(self):
        self.directory = ROOT / 'tests' / ('.manager-native-inputs-' + uuid.uuid4().hex)
        self.assets = self.directory / 'userspace/ksud/bin/aarch64'
        self.assets.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.script = (ROOT / 'tools/build_sukisu_manager.sh').read_text(encoding='utf-8')
        marker = 'python3 - "$source_dir" "$build_tools" <<\'PY\'\n'
        self.inputs = self.script.split(marker, 1)[1].split('\nPY\n', 1)[0]
        marker = 'python3 - "$ksud" "$build_tools" <<\'PY\'\n'
        self.embedded = self.script.split(marker, 1)[1].split('\nPY\n', 1)[0]
        for kmi in KMIS:
            (self.assets / (kmi + '_kernelsu.ko')).write_bytes(self.elf_header(1))

    @staticmethod
    def elf_header(elf_type, machine=183):
        header = bytearray(64)
        header[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<HHI', header, 16, elf_type, machine, 1)
        return bytes(header)

    def run_guard(self, script, path):
        return subprocess.run([sys.executable, '-c', script, str(path), str(ROOT / 'tools')],
                              capture_output=True, text=True, check=False)

    def test_requires_exactly_all_eight_staged_arm64_module_inputs(self):
        good = self.run_guard(self.inputs, self.directory)
        self.assertEqual(0, good.returncode, good.stderr)
        self.assertTrue(all(kmi in good.stdout for kmi in KMIS))
        last = self.assets / (KMIS[-1] + '_kernelsu.ko')
        last.unlink()
        result = self.run_guard(self.inputs, self.directory)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(KMIS[-1], result.stderr)
        last.write_bytes(self.elf_header(1))
        (self.assets / 'android99-9.9_kernelsu.ko').write_bytes(self.elf_header(1))
        result = self.run_guard(self.inputs, self.directory)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('unexpected: android99-9.9_kernelsu.ko', result.stderr)

    def test_wrong_architecture_executable_or_empty_module_fails_before_compilation(self):
        first = self.assets / (KMIS[0] + '_kernelsu.ko')
        for contents in (b'', self.elf_header(1, 62), self.elf_header(3), b'x' * 64):
            with self.subTest(contents=contents[:20]):
                first.write_bytes(contents)
                result = self.run_guard(self.inputs, self.directory)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(KMIS[0], result.stderr)

    def test_native_daemon_requires_all_eight_full_filenames_in_arm64_executable(self):
        # These names model rust-embed metadata, not fabricated production KOs.
        names = b'\0'.join((kmi + '_kernelsu.ko').encode('ascii') for kmi in KMIS)
        daemon = self.directory / 'ksud-unit-fixture'
        daemon.write_bytes(self.elf_header(3) + names)
        result = self.run_guard(self.embedded, daemon)
        self.assertEqual(0, result.returncode, result.stderr)
        for contents in (self.elf_header(3) + names.replace((KMIS[-1] + '_kernelsu.ko').encode(), b''),
                         self.elf_header(1) + names, self.elf_header(3, 62) + names):
            with self.subTest(contents=contents[:20]):
                daemon.write_bytes(contents)
                result = self.run_guard(self.embedded, daemon)
                self.assertNotEqual(0, result.returncode)

    def test_changed_binary_assets_clean_only_ksud_before_build_and_keep_dependency_cache(self):
        clean = 'cargo clean -p ksud --target aarch64-linux-android --release'
        self.assertEqual([clean], [line for line in self.script.splitlines() if line.startswith('cargo clean')])
        self.assertLess(self.script.index('python3 - "$source_dir" "$build_tools"'),
                        self.script.index('cargo build --locked --manifest-path userspace/ksuinit/Cargo.toml'))
        self.assertLess(self.script.index('cp "$CARGO_TARGET_DIR/aarch64-linux-android/release/ksuinit"'),
                        self.script.index(clean))
        self.assertLess(self.script.index(clean), self.script.index('cargo build --locked --target aarch64-linux-android'))
        self.assertLess(self.script.index('python3 - "$ksud" "$build_tools"'),
                        self.script.index('bash ./gradlew'))
        self.assertNotIn('rm -rf', self.script)


class ManagerBuildArtifactTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / 'tests' / ('.manager-build-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.release = self.directory / 'release'
        self.output = self.directory / 'output'
        self.release.mkdir()
        self.output.mkdir()
        self.sdk = self.directory / 'sdk'
        tool = self.sdk / 'build-tools/37.0.0/aapt2'
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b'')
        self.github_output = self.directory / 'github-output.txt'
        self.apk = self.release / 'SukiSU_v4.2.1_41030-release-unsigned.apk'
        # Unit fixture of packaged ARM64 ELF metadata; no real module is built.
        header = bytearray(64)
        header[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<H', header, 18, 183)
        with zipfile.ZipFile(self.apk, 'w') as archive:
            archive.writestr('AndroidManifest.xml', b'manifest')
            archive.writestr('classes.dex', b'dex')
            archive.writestr('lib/arm64-v8a/libkernelsu.so', b'jni library')
            archive.writestr('lib/arm64-v8a/libksud.so', header + b'\0'.join(
                (kmi + '_kernelsu.ko').encode('ascii') for kmi in KMIS))
        script = (ROOT / 'tools/build_sukisu_manager.sh').read_text(encoding='utf-8')
        marker = 'python3 - "$manager_dir/app/build/outputs/apk/release" "$output_dir" "$build_tools" <<\'PY\'\n'
        self.inspect = script.split(marker, 1)[1].split('\nPY\n', 1)[0]
        self.badging = "package: name='com.sukisu.ultra' versionCode='41030' versionName='v4.2.1'\n"

    def inspect_artifact(self, badging=None, returncode=0):
        result = subprocess.CompletedProcess(['aapt2'], returncode, stdout=badging or self.badging, stderr='')
        environment = {'ANDROID_HOME': str(self.sdk), 'GITHUB_OUTPUT': str(self.github_output)}
        with patch.dict(os.environ, environment), patch.object(sys, 'argv',
                ['-', str(self.release), str(self.output), str(ROOT / 'tools')]):
            with patch.object(subprocess, 'run', return_value=result) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    exec(compile(self.inspect, 'build_sukisu_manager.sh:artifact-check', 'exec'), {})
        return run

    def test_exports_source_filename_and_real_aapt_versions(self):
        run = self.inspect_artifact()
        metadata = json.loads((self.output / 'manager-build-metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata, {'source_apk_filename': self.apk.name,
                                    'apk_filename': 'SukiSU_v4.2.1_41030-release.apk',
                                    'manager_package': 'com.sukisu.ultra',
                                    'manager_version_name': 'v4.2.1', 'manager_version_code': 41030})
        self.assertEqual((self.output / 'manager-unsigned.apk').read_bytes(), self.apk.read_bytes())
        self.assertEqual(run.call_args.args[0][-3:], ['dump', 'badging', str(self.apk)])
        outputs = self.github_output.read_text(encoding='utf-8')
        self.assertIn('source_apk_filename=' + self.apk.name + '\n', outputs)
        self.assertIn('apk_filename=SukiSU_v4.2.1_41030-release.apk\n', outputs)
        self.assertIn('manager_version_name=v4.2.1\n', outputs)
        self.assertIn('manager_version_code=41030\n', outputs)

    def test_version_metadata_is_read_from_apk_instead_of_filename(self):
        self.apk = self.apk.rename(self.release / 'SukiSU_oldname-release-unsigned.apk')
        self.inspect_artifact()
        metadata = json.loads((self.output / 'manager-build-metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['manager_version_name'], 'v4.2.1')
        self.assertEqual(metadata['manager_version_code'], 41030)
        self.assertEqual(metadata['apk_filename'], 'SukiSU_oldname-release.apk')

    def test_invalid_or_duplicate_version_metadata_is_not_published(self):
        for badging in [self.badging.replace("versionCode='41030'", "versionCode='0'"),
                        self.badging.replace("versionCode='41030'", "versionCode='2147483648'"),
                        self.badging.replace("versionName='v4.2.1'", "versionName=''"),
                        self.badging.replace("versionName='v4.2.1'", "versionName='bad\nversion'"),
                        self.badging + self.badging,
                        self.badging.replace("com.sukisu.ultra", 'com.invalid-package'),
                        self.badging.replace("com.sukisu.ultra", 'com.example.othermanager'),
                        self.badging.replace("com.sukisu.ultra", 'com.cyzrqh.gki.sukisu')]:
            with self.subTest(badging=badging), self.assertRaises(SystemExit):
                self.inspect_artifact(badging)
            self.assertEqual(list(self.output.iterdir()), [])
            self.assertFalse(self.github_output.exists())

    def test_sdk_failure_and_ambiguous_apk_are_not_published(self):
        with self.assertRaisesRegex(SystemExit, 'aapt2 could not read'):
            self.inspect_artifact(returncode=1)
        shutil.copyfile(self.apk, self.release / 'second-release-unsigned.apk')
        with self.assertRaisesRegex(SystemExit, 'exactly one release APK'):
            self.inspect_artifact()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(self.github_output.exists())

    def test_packaged_daemon_with_empty_or_incomplete_kmi_choices_is_not_published(self):
        with zipfile.ZipFile(self.apk) as archive:
            original = {name: archive.read(name) for name in archive.namelist()}
        for daemon in (original['lib/arm64-v8a/libksud.so'][:64],
                       original['lib/arm64-v8a/libksud.so'].replace(
                           (KMIS[-1] + '_kernelsu.ko').encode('ascii'), b'')):
            with self.subTest(daemon_size=len(daemon)):
                with zipfile.ZipFile(self.apk, 'w') as archive:
                    for name, data in original.items():
                        archive.writestr(name, daemon if name == 'lib/arm64-v8a/libksud.so' else data)
                with self.assertRaisesRegex(SystemExit, 'missing embedded native KMI modules'):
                    self.inspect_artifact()
                self.assertEqual([], list(self.output.iterdir()))
                self.assertFalse(self.github_output.exists())


if __name__ == '__main__':
    unittest.main()
