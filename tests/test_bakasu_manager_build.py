"""BakaSU source/packaging guards; synthetic ELF fixtures are not native builds."""
import contextlib
import copy
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

SCRIPT = (ROOT / 'tools/build_bakasu_manager.sh').read_text(encoding='utf-8')


def snippet(marker):
    return SCRIPT.split(marker + " <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]


def elf(elf_type=3, machine=183):
    data = bytearray(64)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', data, 16, elf_type, machine, 1)
    return bytes(data)


class BakasuManagerSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / 'tests' / ('.bakasu-source-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.gradle = self.directory / 'build.gradle.kts'
        self.transform = snippet('python3 - "$manager_dir/app/build.gradle.kts"')
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
        ndk { abiFilters += listOf("arm64-v8a", "x86_64", "armeabi-v7a", "riscv64") }
    }
    splits {
        abi {
            isEnable = isReleaseTask
            reset()
            include("arm64-v8a", "x86_64", "armeabi-v7a", "riscv64")
            isUniversalApk = true
        }
    }
}
base { archivesName.set("BakaSU_${managerVersionName}_$managerVersionCode") }
'''

    def transform_source(self, source):
        self.gradle.write_text(source, encoding='utf-8')
        return subprocess.run([sys.executable, '-c', self.transform, str(self.gradle)],
                              capture_output=True, text=True, check=False)

    def test_one_unsigned_arm64_split_keeps_original_version_and_filename_definition(self):
        result = self.transform_source(self.source)
        self.assertEqual(0, result.returncode, result.stderr)
        output = self.gradle.read_text(encoding='utf-8')
        self.assertIn('abiFilters.clear()', output)
        self.assertNotIn('abiFilters +=', output)
        self.assertIn('include("arm64-v8a")', output)
        self.assertIn('isUniversalApk = false', output)
        self.assertNotIn('x86_64', output)
        self.assertNotIn('lsplugin.apksign', output)
        self.assertNotIn('KEYSTORE_FILE', output)
        self.assertEqual(1, output.count('signingConfig = null'))
        self.assertIn('archivesName.set("BakaSU_${managerVersionName}_$managerVersionCode")', output)

    def test_default_ndk_filters_are_empty_while_the_only_apk_split_is_arm64(self):
        result = self.transform_source(self.source)
        self.assertEqual(0, result.returncode, result.stderr)
        output = self.gradle.read_text(encoding='utf-8')
        self.assertEqual(1, output.count('abiFilters.clear()'))
        self.assertNotRegex(output, r'abiFilters\s*(?:\+=|=|\.add)')
        self.assertEqual(1, output.count('include("arm64-v8a")'))
        self.assertEqual(1, output.count('isUniversalApk = false'))
        self.assertNotIn('isUniversalApk = true', output)
        self.assertNotIn('include("x86_64"', output)

    def test_unknown_signing_or_split_layout_is_rejected_without_partial_rewrite(self):
        for source in (self.source.replace('KEY_PASSWORD', 'UNKNOWN_PASSWORD'),
                       self.source + 'val signingConfig = unknown()\n',
                       self.source.replace('isEnable = isReleaseTask', 'isEnable = futureValue'),
                       self.source.replace('isUniversalApk = true', 'isUniversalApk = unknown'),
                       self.source.replace('include("arm64-v8a",', 'include(futureAbi,'),
                       self.source.replace('splits {', 'futureSplits {')):
            with self.subTest(source=source):
                result = self.transform_source(source)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(source, self.gradle.read_text(encoding='utf-8'))

    def test_separate_crates_use_nightly_and_the_same_package_in_daemon_and_apk(self):
        self.assertIn('export KSU_PACKAGE_NAME=com.cyzrqh.gki.bakasu', SCRIPT)
        self.assertIn('assembleRelease -PKSU_PACKAGE_NAME="$KSU_PACKAGE_NAME"', SCRIPT)
        self.assertIn('cargo +nightly build --locked --manifest-path userspace/ksuinit/Cargo.toml', SCRIPT)
        self.assertIn('cargo +nightly clean --manifest-path userspace/ksud/Cargo.toml -p ksud', SCRIPT)
        self.assertIn('cargo +nightly build --locked --target aarch64-linux-android --release --manifest-path userspace/ksud/Cargo.toml', SCRIPT)
        self.assertNotIn('for required in Cargo.toml', SCRIPT)
        self.assertNotIn('curl ', SCRIPT)
        self.assertNotIn('wget ', SCRIPT)

    def test_native_rust_uses_r29_in_a_subshell_and_leaves_gradle_ndk_unchanged(self):
        self.assertIn('native_ndk_dir="$ANDROID_HOME/ndk/29.0.14206865"', SCRIPT)
        self.assertIn('[ -d "$native_ndk_dir" ]', SCRIPT)
        native_start = SCRIPT.index('(\nexport ANDROID_NDK_HOME="$native_ndk_dir"\n')
        native_end = SCRIPT.index('\n)\n\ncd "$manager_dir"', native_start)
        native = SCRIPT[native_start:native_end]
        self.assertIn('export ANDROID_NDK_ROOT="$native_ndk_dir"', native)
        helper = 'source .github/scripts/setup-rust-build.sh aarch64-linux-android 26'
        self.assertEqual(1, SCRIPT.count(helper))
        self.assertIn(helper, native)
        self.assertLess(native.index('export ANDROID_NDK_HOME='), native.index(helper))
        self.assertIn('builtins="$("$CLANG_PATH" --print-resource-dir)', native)
        self.assertIn('cargo +nightly build --locked --manifest-path userspace/ksuinit/Cargo.toml', native)
        self.assertIn('--manifest-path userspace/ksud/Cargo.toml', native)
        self.assertIn('cp "$ksud" "$manager_dir/app/src/main/jniLibs/arm64-v8a/libksud.so"', native)
        self.assertNotIn('bash ./gradlew', native)
        gradle = SCRIPT[native_end:]
        self.assertIn('bash ./gradlew', gradle)
        self.assertNotIn('export ANDROID_NDK_HOME=', gradle)
        self.assertNotIn('export ANDROID_NDK_ROOT=', gradle)
        self.assertNotIn('allow-multiple-definition', SCRIPT)
        self.assertNotIn('rust_eh_personality', SCRIPT)

    def test_native_inputs_refuse_missing_or_non_arm64_modules(self):
        assets = self.directory / 'userspace/ksud/bin/aarch64'
        assets.mkdir(parents=True)
        for kmi in KMIS:
            (assets / (kmi + '_kernelsu.ko')).write_bytes(elf(1))
        guard = snippet('python3 - "$source_dir" "$build_tools"')
        arguments = [sys.executable, '-c', guard, str(self.directory), str(ROOT / 'tools')]
        result = subprocess.run(arguments, capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        selected = assets / (KMIS[-1] + '_kernelsu.ko')
        for contents in (b'', elf(1, 62), elf(3)):
            selected.write_bytes(contents)
            with self.subTest(contents=contents):
                result = subprocess.run(arguments, capture_output=True, text=True, check=False)
                self.assertNotEqual(0, result.returncode)
        selected.unlink()
        result = subprocess.run(arguments, capture_output=True, text=True, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(KMIS[-1], result.stderr)


class BakasuManagerArtifactTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / 'tests' / ('.bakasu-artifact-' + uuid.uuid4().hex)
        self.release = self.directory / 'release'
        self.output = self.directory / 'output'
        self.release.mkdir(parents=True)
        self.output.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.sdk = self.directory / 'sdk'
        aapt = self.sdk / 'build-tools/37.0.0/aapt2'
        aapt.parent.mkdir(parents=True)
        aapt.write_bytes(b'')
        self.apath = self.release / 'BakaSU_v4.2.0-rc3_35252-arm64-v8a-release-unsigned.apk'
        self.names = b'\0'.join((kmi + '_kernelsu.ko').encode('ascii') for kmi in KMIS)
        self.contents = {'AndroidManifest.xml': b'manifest', 'classes.dex': b'dex',
                         'lib/arm64-v8a/libksud.so': elf() + self.names,
                         'lib/arm64-v8a/libkernelsu.so': b'jni fixture',
                         'lib/arm64-v8a/libadbroot.so': b'adbroot fixture'}
        self.write_apk()
        self.inspect = snippet('python3 - "$manager_dir/app/build/outputs/apk/release" "$output_dir" "$build_tools"')
        self.badging = "package: name='com.cyzrqh.gki.bakasu' versionCode='35252' versionName='v4.2.0-rc3'\n"

    def write_apk(self, contents=None):
        with zipfile.ZipFile(self.apath, 'w') as archive:
            for name, value in (self.contents if contents is None else contents).items():
                archive.writestr(name, value)

    def inspect_apk(self, badging=None):
        result = subprocess.CompletedProcess(['aapt2'], 0, stdout=self.badging if badging is None else badging, stderr='')
        with patch.dict(os.environ, {'ANDROID_HOME': str(self.sdk)}), \
                patch.object(sys, 'argv', ['-', str(self.release), str(self.output), str(ROOT / 'tools')]), \
                patch.object(subprocess, 'run', return_value=result), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(self.inspect, 'build_bakasu_manager.sh:artifact-check', 'exec'), {})

    def test_keeps_official_filename_and_real_apk_version_metadata(self):
        self.inspect_apk()
        actual = json.loads((self.output / 'manager-build-metadata.json').read_text(encoding='utf-8'))
        self.assertEqual({'source_apk_filename': self.apath.name,
                          'apk_filename': self.apath.name.removesuffix('-unsigned.apk') + '.apk',
                          'manager_package': 'com.cyzrqh.gki.bakasu',
                          'manager_version_name': 'v4.2.0-rc3', 'manager_version_code': 35252}, actual)
        self.assertEqual(self.apath.read_bytes(), (self.output / 'manager-unsigned.apk').read_bytes())

    def test_former_resukisu_official_filename_is_kept_without_rebranding(self):
        self.apath = self.apath.rename(self.release / self.apath.name.replace('BakaSU_', 'ReSukiSU_', 1))
        self.inspect_apk()
        actual = json.loads((self.output / 'manager-build-metadata.json').read_text(encoding='utf-8'))
        self.assertTrue(actual['apk_filename'].startswith('ReSukiSU_'))

    def test_another_package_or_invalid_version_is_not_exported(self):
        for badging in (self.badging.replace('com.cyzrqh.gki.bakasu', 'com.resukisu.resukisu'),
                        self.badging.replace("versionCode='35252'", "versionCode='0'"),
                        self.badging.replace("versionName='v4.2.0-rc3'", "versionName=''"),
                        self.badging + self.badging):
            with self.subTest(badging=badging), self.assertRaises(SystemExit):
                self.inspect_apk(badging)
            self.assertEqual([], list(self.output.iterdir()))

    def test_daemon_missing_a_kmi_or_native_abi_is_not_exported(self):
        for change in ({'lib/arm64-v8a/libksud.so': elf()},
                       {'lib/arm64-v8a/libksud.so': elf(machine=62) + self.names},
                       {'lib/x86_64/libksud.so': b'unexpected ABI'}):
            contents = {**self.contents, **change}
            self.write_apk(contents)
            with self.subTest(change=list(change)), self.assertRaises(SystemExit):
                self.inspect_apk()
            self.assertEqual([], list(self.output.iterdir()))

    def test_incomplete_apk_or_multiple_outputs_fail_before_export(self):
        contents = copy.deepcopy(self.contents)
        del contents['lib/arm64-v8a/libadbroot.so']
        self.write_apk(contents)
        with self.assertRaisesRegex(SystemExit, 'Incomplete manager APK'):
            self.inspect_apk()
        self.write_apk()
        shutil.copyfile(self.apath, self.release / 'duplicate-release-unsigned.apk')
        with self.assertRaisesRegex(SystemExit, 'exactly one release APK'):
            self.inspect_apk()
        self.assertEqual([], list(self.output.iterdir()))


if __name__ == '__main__':
    unittest.main()
