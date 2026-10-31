"""Exercise real pinned CCTV recipes, source selection and installer behaviour."""
import copy
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_cctv as cctv
import oplus_recipe as recipe
import oplus_assets as assets
sys.path.insert(0, str(ROOT / 'tests'))
from test_oplus_assets import initialize, image_fixture, SHELL, rewrite_zip, change_metadata
from apk_fixture import manager_metadata, signed_apk
from manager_variants import manager_profile


class CctvSchemes(unittest.TestCase):
    def setUp(self):
        self.rows = cctv.load_profiles()
        (ROOT.parent / '.gki-upstream-cache').mkdir(exist_ok=True)

    def inputs(self, row, variant='SukiSU', enabled=True):
        return dict(kernelsu_variant=variant, kernelsu_branch='最新版',
                    enable_susfs=enabled, use_kpm=enabled, use_lz4kd=enabled,
                    use_bbg=False, use_bbr=False, use_proxy_optimization=False,
                    use_unicode_bypass=False, build_target=cctv.label(row))

    def test_all_twenty_two_real_recipes_have_distinct_correct_source_schemes(self):
        self.assertEqual(22, len(self.rows))
        self.assertEqual(22, len({cctv.label(row) for row in self.rows}))
        self.assertEqual(20, sum(row['platform'] != 'gki' for row in self.rows))
        self.assertEqual(['6.12.23', '6.12.69'], [row['kernel'] for row in self.rows if row['platform'] == 'gki'])
        self.assertEqual(7, sum(row['kernel_version'] == '6.1' for row in self.rows))
        self.assertEqual(8, sum(row['kernel_version'] == '6.6' for row in self.rows))
        for row in self.rows:
            self.assertTrue(row['kernel_repo'].startswith('cctv18/'))
            self.assertEqual(row, assets.profile_identity(row))
            self.assertEqual(row, recipe.validate_profile(row))

    def test_source_ref_platform_and_recipe_cannot_be_substituted(self):
        row = next(row for row in self.rows if row['id'] == 'sm8650-6.1.141')
        for field, value in dict(kernel_repo='OnePlusOSS/android_kernel_common_oneplus_sm8650',
                kernel_ref='a' * 40, recipe_ref='b' * 40, platform='mt6989',
                recipe_workflow='.github/workflows/fastbuild_6.1.118.yml').items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                recipe.validate_profile({**row, field: value})

    def test_selecting_one_target_never_reintroduces_other_kernels(self):
        for row in self.rows:
            self.assertEqual({'include': [row]}, cctv.discover_matrix(target=cctv.label(row)))
        with self.assertRaises(ValueError):
            cctv.discover_matrix(target='6.1.141')
        self.assertEqual({'include': self.rows}, cctv.discover_matrix())

    def test_every_real_recipe_renders_both_managers_with_enabled_or_disabled_features(self):
        for row in self.rows:
            raw = (ROOT / 'third_party/cctv-oplus/recipes' / Path(row['recipe_workflow']).name).read_bytes()
            document = recipe.parse_recipe(raw, row)
            for variant in ('SukiSU', 'BakaSU'):
                for enabled in (False, True):
                    with self.subTest(id=row['id'], variant=variant, enabled=enabled):
                        inputs = self.inputs(row, variant, enabled)
                        self.assertEqual('sukisu' if variant == 'SukiSU' else 'bakasu',
                                         recipe.native_inputs(inputs, row)['ksu_type'])
                        for phase in ('bootstrap', 'configure', 'build'):
                            steps = recipe.plan_phase(phase, document, row, inputs)
                            self.assertTrue(steps)
                            text = '\n'.join(step.script for step in steps)
                            self.assertNotIn('${{', text)
                            self.assertNotIn('curl -LSs', text)
                            self.assertNotIn('sudo rm -rf /usr', text)
                            self.assertNotIn('setup.sh | bash', text)

    def test_recipe_checksum_rejects_changed_code(self):
        row = self.rows[0]
        path = ROOT / 'third_party/cctv-oplus/recipes' / Path(row['recipe_workflow']).name
        with self.assertRaises(ValueError):
            recipe.parse_recipe(path.read_bytes() + b'\n# changed\n', row)

    def test_native_installer_keeps_cctv_ramdisk_and_module_installation_paths(self):
        for row in self.rows:
            prefix = row['kernel'] + '-' + row['android_version'] + '-11'
            text = assets.anykernel_script(row, 'BakaSU', prefix).decode()
            self.assertIn('NO_MAGISK_CHECK=1', text)
            self.assertIn('unpack_ramdisk\n    write_boot', text)
            self.assertIn('else\n    flash_boot', text)
            self.assertIn('/data/adb/ksud module install', text)
            self.assertLess(text.index('Kernel/Android/KMI mismatch'), text.index('\nsplit_boot\n'))

    def test_native_and_historical_provenance_are_kept_separate(self):
        for row in self.rows:
            identity = cctv.source_provenance(row)
            self.assertEqual(identity['kernel_ref'], row['kernel_ref'])
            self.assertEqual(identity['recipe_sha256'], row['recipe_sha256'])
            self.assertNotIn('OnePlusOSS', str(identity))
            self.assertEqual(assets.filename(row, 'SukiSU').count(row['kernel']), 1)

    @unittest.skipUnless(SHELL, 'Requires POSIX shell')
    def test_native_installer_executes_both_ramdisk_paths_and_refuses_wrong_release(self):
        row = next(row for row in self.rows if row['id'] == 'sm8650-6.1.141')
        for ramdisk, release, expected in (
                ('yes', '6.1.141-android14-11-stock', ['split', 'unpack', 'write']),
                ('no', '6.1.141-android14-11-stock', ['split', 'flash']),
                ('yes', '6.1.118-android14-11-stock', ['abort']),
                ('no', '6.1.141-android15-11-stock', ['abort'])):
            with self.subTest(ramdisk=ramdisk, release=release), tempfile.TemporaryDirectory(
                    dir=ROOT.parent / '.gki-upstream-cache', prefix='cctv-installer-') as tmp:
                directory = Path(tmp)
                (directory / 'tools').mkdir()
                (directory / 'split_img').mkdir()
                (directory / 'tools/ak3-core.sh').write_text('''AKHOME="$PWD"
ui_print() { :; }
sync() { :; }
sleep() { :; }
chmod() { :; }
uname() { echo "$TEST_RELEASE"; }
abort() { echo abort >> trace; exit 1; }
split_boot() {
 echo split >> trace
 if [ "$TEST_RAMDISK" = yes ]; then echo fixture > split_img/ramdisk.cpio; fi
}
unpack_ramdisk() { echo unpack >> trace; }
write_boot() { echo write >> trace; }
flash_boot() { echo flash >> trace; }
''', encoding='utf-8', newline='\n')
                (directory / 'anykernel.sh').write_bytes(cctv.installer_script(
                    row, 'SukiSU', '6.1.141-android14-11'))
                result = subprocess.run([SHELL, 'anykernel.sh'], cwd=directory, capture_output=True,
                    text=True, timeout=15, env={**os.environ, 'TEST_RELEASE': release, 'TEST_RAMDISK': ramdisk})
                self.assertEqual(expected, (directory / 'trace').read_text().splitlines(),
                                 result.stdout + result.stderr)

    def test_native_package_and_collect_keep_real_apk_signature_source_and_digests(self):
        for variant in ('SukiSU', 'BakaSU'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory(
                    dir=ROOT.parent / '.gki-upstream-cache', prefix='cctv-assets-') as tmp:
                directory = Path(tmp)
                root = directory / 'kernel_workspace'
                row = copy.deepcopy(self.rows[0])
                row['kernel_ref'] = initialize(root / 'common', {
                    'Makefile': b'VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 57\n',
                    'include/linux/susfs.h': b'#define SUSFS_VERSION "v2.3.0"\n'},
                    'https://github.com/' + row['kernel_repo'])
                ksu_ref = initialize(root / 'KernelSU', {'fixture': b'non-loadable source fixture'},
                                    'https://github.com/' + manager_profile(variant)['repository'])
                ak3 = directory / 'ak3'
                ak3_ref = initialize(ak3, {
                    'LICENSE': b'Non-executable fixture\n',
                    'tools/ak3-core.sh': b'# inert runtime\n',
                    'META-INF/com/google/android/update-binary': b'# inert installer\n',
                    'anykernel.sh': (ROOT / 'third_party/cctv-oplus/anykernel.sh').read_bytes()},
                    'https://github.com/cctv18/AnyKernel3')
                manager = manager_metadata(file=f'{variant}_v4.3.0_41100-release.apk',
                    package=manager_profile(variant)['package'], ksu_ref=ksu_ref,
                    ksu_variant=variant, ksu_version_name='v4.3.0', ksu_version_code='41100')
                source = directory / 'assets'
                source.mkdir()
                apk = source / manager['file']
                apk.write_bytes(signed_apk(manager))
                manager = assets.read_manager_apk(apk)
                env = {'GITHUB_REPOSITORY': 'cyzrqh/gki', 'GITHUB_RUN_ID': '123',
                    'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40, 'ENABLE_SUSFS': 'true',
                    'KSU_VARIANT': variant, 'KSU_REF': ksu_ref, 'KSU_BRANCH': manager['ksu_branch'],
                    'MANAGER_VERSION_NAME': 'v4.3.0', 'MANAGER_VERSION_CODE': '41100'}
                env.update(MANAGER_PACKAGE=manager['package'],
                    MANAGER_CERT_SIZE=str(manager['manager_cert_size']),
                    MANAGER_CERT_SHA256=manager['manager_cert_sha256'],
                    MANAGER_APK_FILENAME=manager['file'], MANAGER_APK_SHA256=manager['apk_sha256'])
                image = root / 'common/out/arch/arm64/boot/Image'
                image.parent.mkdir(parents=True)
                native = '6.1.57-android14-11-test'
                image.write_bytes(image_fixture(manager, native))
                release = root / 'common/out/include/config/kernel.release'
                release.parent.mkdir(parents=True)
                release.write_text(native)
                with mock.patch.dict(os.environ, env), mock.patch.object(cctv, 'load_profiles', return_value=[row]), \
                        mock.patch.object(cctv, 'AK3_REF', ak3_ref):
                    packaged = assets.package(row, root, image, ak3, source)
                    path = Path(packaged['path'])
                    document = assets.read_kernel(path)
                    self.assertEqual(manager['apk_sha256'], document['manager_apk_sha256'])
                    self.assertEqual(manager['manager_cert_sha256'], document['manager_cert_sha256'])
                    self.assertEqual(cctv.source_provenance(row), document['cctv_source_tree'])
                    self.assertEqual('cctv18/AnyKernel3', document['ak3_template']['repository'])
                    result = assets.collect(source, directory / 'report', {'include': [row]}, variant)
                    self.assertEqual(1, result['count'])
                    rewrite_zip(path, change=lambda data: change_metadata(data, lambda doc:
                        doc.update(manager_apk_sha256='0' * 64)))
                    with self.assertRaisesRegex(ValueError, 'signed manager'):
                        assets.collect(source, directory / 'bad-report', {'include': [row]}, variant)


if __name__ == '__main__':
    unittest.main()
