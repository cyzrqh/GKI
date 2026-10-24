import gzip
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import unittest
from unittest.mock import patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import capture_build_metadata as metadata
import prepare_release
from apk_fixture import manager_metadata, signed_apk
from kernel_fixture import ak3_bytes, kernel_fixture, arm64_fixture


class BuiltMetadataTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.metadata-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.image = self.root / 'Image'
        header = self.root / 'common/include/linux/susfs.h'
        header.parent.mkdir(parents=True)
        header.write_text('#define SUSFS_VERSION "v2.3.0"\n')
        source = self.root / 'common/fs/exec.c'
        source.parent.mkdir(parents=True)
        source.write_text('int execute(void) { return ksu_setup_preinit_file(0, 0); }\n')
        self.config = ('CONFIG_ARM64=y\nCONFIG_KSU=y\nCONFIG_KSU_SUSFS=y\nCONFIG_LTO_CLANG_THIN=y\n'
                       'CONFIG_XINGGUANG_DDK=y\nCONFIG_ZRAM=y\nCONFIG_CRYPTO_LZ4=y\n'
                       '# CONFIG_ZRAM_BACKEND_LZ4 is not set\nCONFIG_ZRAM_BACKEND_ZSTD=y\n')
        self.context = {'file': 'android14-6.1.145-2025-09-AnyKernel3.zip', 'ksu_variant': 'SukiSU',
                        'ksu_ref': 'a' * 40, 'enable_susfs': 'true', 'expected_susfs_version': 'v2.3.0'}
        self.make_image(self.config)

    def make_image(self, config, *, version=b'v2.3.0'):
        self.image.write_bytes(arm64_fixture(b'kernel ' + version + b' IKCFG_ST' + gzip.compress(config.encode()) + b'IKCFG_ED'))

    def collect(self):
        result = subprocess.CompletedProcess([], 0, stdout='a' * 40 + '\n', stderr='')
        with patch.object(metadata.subprocess, 'run', return_value=result):
            return metadata.collect(self.root, self.image, self.context)

    def test_config_comes_from_image_and_reports_actual_feature_symbols(self):
        result = self.collect()
        self.assertEqual(result['config_source'], 'Image / IKCONFIG')
        self.assertEqual(result['susfs_version'], 'v2.3.0')
        self.assertTrue(result['features']['ddk'])
        self.assertEqual(result['features']['lto'], 'thin')
        self.assertEqual(result['features']['hook'], 'SUSFS Inline Hooks')
        self.assertEqual(result['features']['ZRAM 压缩算法'], 'ZSTD')
        self.assertNotIn('Magic', result['features']['mount'])

    def test_disabled_zram_never_reports_crypto_algorithms(self):
        self.make_image(self.config.replace('CONFIG_ZRAM=y', '# CONFIG_ZRAM is not set'))
        self.assertEqual(self.collect()['features']['ZRAM 压缩算法'], '无')

    def test_missing_ikconfig_refuses_stale_or_requested_config(self):
        self.image.write_bytes(b'plain Image without config')
        stale = self.root / 'out/stale/common/.config'
        stale.parent.mkdir(parents=True)
        stale.write_text(self.config)
        with self.assertRaisesRegex(ValueError, 'missing IKCONFIG'):
            self.collect()

    def test_entry_jump_outside_image_is_rejected_despite_valid_ikconfig(self):
        image = bytearray(self.image.read_bytes())
        struct.pack_into('<I', image, 0, 0x14000400)  # B to offset 4096, past this inert fixture.
        self.image.write_bytes(image)
        with self.assertRaisesRegex(ValueError, 'outside Image'):
            self.collect()

    def test_corrupt_ikconfig_fails_instead_of_falling_back(self):
        self.image.write_bytes(b'IKCFG_STnot gzipIKCFG_ED')
        with self.assertRaisesRegex(ValueError, 'invalid IKCONFIG'):
            self.collect()

    def test_requested_susfs_must_really_be_compiled(self):
        self.make_image(self.config.replace('CONFIG_KSU_SUSFS=y', '# CONFIG_KSU_SUSFS is not set'))
        with self.assertRaisesRegex(ValueError, 'not enabled'):
            self.collect()

    def test_declared_susfs_version_must_be_present_in_binary(self):
        self.make_image(self.config, version=b'v2.2.0')
        with self.assertRaisesRegex(ValueError, 'compiled SUSFS version'):
            self.collect()

    def test_compiled_ksu_and_actual_checkout_must_match(self):
        self.make_image(self.config.replace('CONFIG_KSU=y', '# CONFIG_KSU is not set'))
        with self.assertRaisesRegex(ValueError, 'KernelSU state'):
            self.collect()
        self.make_image(self.config)
        result = subprocess.CompletedProcess([], 0, stdout='b' * 40, stderr='')
        with patch.object(metadata.subprocess, 'run', return_value=result), self.assertRaisesRegex(ValueError, 'actual KernelSU'):
            metadata.collect(self.root, self.image, self.context)


class DirectReleasePreparationTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.release-prep-' + uuid.uuid4().hex)
        self.source = self.root / 'input'
        self.source.mkdir(parents=True)
        self.output = self.root / 'public'
        self.addCleanup(shutil.rmtree, self.root)

    def manager(self, *, flat=False):
        directory = self.source if flat else self.source / 'Release-Manager'
        directory.mkdir(exist_ok=True)
        document = {'file': 'SukiSU_v4.2.1_41030-release.apk', 'ksu_variant': 'SukiSU',
                    'ksu_version_name': 'v4.2.1', 'ksu_version_code': '41030'}
        (directory / 'manager.json').write_text(json.dumps(document))
        (directory / document['file']).write_bytes(b'verified APK')
        return directory, document

    def test_direct_apk_is_copied_without_outer_zip_and_notes_include_actual_version(self):
        _, document = self.manager()
        report = prepare_release.prepare(self.source, self.output)
        self.assertEqual(report, {'assets': 1, 'kernels': 0, 'manager': True})
        self.assertEqual((self.output / 'assets' / document['file']).read_bytes(), b'verified APK')
        self.assertFalse(list((self.output / 'assets').glob('*.zip')))
        self.assertIn('v4.2.1', (self.output / 'release.md').read_text(encoding='utf-8'))

    def test_single_artifact_download_flat_manager_layout_is_supported(self):
        _, document = self.manager(flat=True)
        report = prepare_release.prepare(self.source, self.output)
        self.assertEqual(report, {'assets': 1, 'kernels': 0, 'manager': True})
        self.assertEqual((self.output / 'assets' / document['file']).read_bytes(), b'verified APK')
        self.assertIn('没有内核构建', (self.output / 'release.md').read_text(encoding='utf-8'))
        self.assertEqual(list((self.output / 'assets').glob('*.zip')), [])

    def kernel(self, *, flat=False):
        directory = self.source if flat else self.source / 'Release-SukiSU-android14-6.1-145-2025-09'
        directory.mkdir(exist_ok=True)
        document = {'file': 'android14-6.1.145-2025-09-AnyKernel3.zip',
                    'files': ['android14-6.1.145-2025-09-AnyKernel3.zip'],
                    'android_version': 'android14', 'kernel_version': '6.1', 'sub_level': '145',
                    'os_patch_level': '2025-09', 'ksu_variant': 'SukiSU', 'susfs_version': 'v2.3.0',
                    'features': {'susfs': True, 'lto': 'thin', 'kpm': True}}
        (directory / 'kernel.json').write_text(json.dumps(document), encoding='utf-8')
        (directory / document['file']).write_bytes(b'verified kernel asset')
        return directory, document

    def test_single_kernel_artifact_flat_layout_retains_actual_configuration(self):
        _, document = self.kernel(flat=True)
        report = prepare_release.prepare(self.source, self.output)
        self.assertEqual(report, {'assets': 1, 'kernels': 1, 'manager': False})
        self.assertEqual((self.output / 'assets' / document['file']).read_bytes(), b'verified kernel asset')
        notes = (self.output / 'release.md').read_text(encoding='utf-8')
        self.assertIn('| LTO | thin |', notes)
        self.assertIn('| KPM | 启用 |', notes)
        self.assertIn('v2.3.0', notes)

    def test_multiple_artifact_named_directories_keep_manager_and_kernel_separate(self):
        _, manager = self.manager()
        _, kernel = self.kernel()
        report = prepare_release.prepare(self.source, self.output)
        self.assertEqual(report, {'assets': 2, 'kernels': 1, 'manager': True})
        self.assertEqual({path.name for path in (self.output / 'assets').iterdir()},
                         {manager['file'], kernel['file'], 'SHA256SUMS.txt'})
        checksums = (self.output / 'assets' / 'SHA256SUMS.txt').read_text(encoding='utf-8')
        self.assertIn(manager['file'], checksums)
        self.assertIn(kernel['file'], checksums)

    def test_flat_and_named_manager_metadata_cannot_hide_duplicate_managers(self):
        self.manager(flat=True)
        self.manager()
        with self.assertRaisesRegex(ValueError, 'at most one manager'):
            prepare_release.prepare(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_asset_or_multiple_managers_is_rejected_before_output(self):
        directory, document = self.manager()
        (directory / document['file']).unlink()
        with self.assertRaisesRegex(ValueError, 'Missing nonempty'):
            prepare_release.prepare(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_destination_is_rejected_before_any_asset_is_copied(self):
        _, document = self.manager()
        (self.output / 'assets').mkdir(parents=True)
        existing = self.output / 'assets' / document['file']
        existing.write_bytes(b'user file')
        with self.assertRaisesRegex(ValueError, 'already contains'):
            prepare_release.prepare(self.source, self.output)
        self.assertEqual(list((self.output / 'assets').iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b'user file')


class RawReleasePreparationTests(unittest.TestCase):
    """Exercise genuine APK-v2 verification and Image/IKCONFIG parsing end to end."""
    def setUp(self):
        self.root = ROOT / 'tests' / ('.raw-release-prep-' + uuid.uuid4().hex)
        self.source = self.root / 'input'
        self.source.mkdir(parents=True)
        self.output = self.root / 'public'
        self.addCleanup(shutil.rmtree, self.root)
        self.manager = manager_metadata()
        self.apk_bytes = signed_apk(self.manager)
        self.manager['apk_sha256'] = hashlib.sha256(self.apk_bytes).hexdigest()

    def write_asset(self, name, content, *, flat=False):
        directory = self.source if flat else self.source / name
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_bytes(content)
        return path

    def make_raw(self, *, kernel=True, boot=False, flat=False, kernel_manager=None):
        apk = self.write_asset(self.manager['file'], self.apk_bytes, flat=flat)
        if not kernel:
            return apk, None, None
        document, image = kernel_fixture(manager=self.manager if kernel_manager is None else kernel_manager)
        if boot:
            name = document['file'].removesuffix('-AnyKernel3.zip') + '-boot.img'
            contents = b'ANDROID! unit boot payload bound by actual SHA256'
            self.write_asset(name, contents, flat=flat)
            document['files'] = [document['file'], name]
            document['boot_sha256'] = {name: hashlib.sha256(contents).hexdigest()}
        ak3 = self.write_asset(document['file'], ak3_bytes(document, image), flat=flat)
        return apk, ak3, document

    def assert_no_output(self):
        self.assertFalse(self.output.exists())

    def test_raw_signed_apk_only_is_published_as_identical_original_bytes(self):
        apk, _, _ = self.make_raw(kernel=False, flat=True)
        result = prepare_release.prepare(self.source, self.output)
        self.assertEqual({'assets': 1, 'kernels': 0, 'manager': True}, result)
        self.assertEqual(self.apk_bytes, (self.output / 'assets' / apk.name).read_bytes())
        self.assertEqual({apk.name, 'SHA256SUMS.txt'}, {path.name for path in (self.output / 'assets').iterdir()})
        self.assertIn(self.manager['apk_sha256'] + '  ' + apk.name,
                      (self.output / 'assets/SHA256SUMS.txt').read_text())

    def test_separate_raw_artifact_directories_rejoin_apk_ak3_and_boot_without_wrappers(self):
        apk, ak3, document = self.make_raw(boot=True)
        originals = {path.name: path.read_bytes() for path in self.source.rglob('*') if path.is_file()}
        report = prepare_release.prepare(self.source, self.output)
        self.assertEqual({'assets': 3, 'kernels': 1, 'manager': True}, report)
        for name, content in originals.items():
            self.assertEqual(content, (self.output / 'assets' / name).read_bytes())
        names = {path.name for path in (self.output / 'assets').iterdir()}
        self.assertEqual(set(originals) | {'SHA256SUMS.txt'}, names)
        self.assertEqual([ak3.name], [name for name in names if name.endswith('.zip')])
        notes = (self.output / 'release.md').read_text(encoding='utf-8')
        self.assertIn('| LTO | thin |', notes)
        self.assertIn(document['susfs_version'], notes)
        self.assertIn('1 个内核目标', notes)

    def test_sidecars_and_matching_embedded_metadata_count_each_build_once(self):
        apk, ak3, document = self.make_raw(flat=True)
        (self.source / 'manager.json').write_text(json.dumps({key: value for key, value in self.manager.items()
                                                           if key != 'apk_sha256'}), encoding='utf-8')
        (self.source / 'kernel.json').write_text(json.dumps(document), encoding='utf-8')
        result = prepare_release.prepare(self.source, self.output)
        self.assertEqual({'assets': 2, 'kernels': 1, 'manager': True}, result)
        self.assertEqual({apk.name, ak3.name, 'SHA256SUMS.txt'},
                         {path.name for path in (self.output / 'assets').iterdir()})

    def test_conflicting_legacy_sidecar_cannot_override_a_valid_signed_apk(self):
        self.make_raw(flat=True)
        legacy = {**self.manager, 'ksu_version_code': '1'}
        (self.source / 'manager.json').write_text(json.dumps(legacy), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'sidecar differs'):
            prepare_release.prepare(self.source, self.output)
        self.assert_no_output()

    def test_kernel_must_match_actual_manager_source_certificate_and_apk_bytes(self):
        changes = [('ksu_ref', 'c' * 40), ('ksu_version_code', '40923'),
                   ('ksu_version_name', '4.2.1'), ('package', 'com.example.other'),
                   ('manager_cert_size', str(int(self.manager['manager_cert_size']) + 1)),
                   ('manager_cert_sha256', 'd' * 64), ('apk_sha256', 'e' * 64),
                   ('file', 'SukiSU_different-release.apk')]
        for field, value in changes:
            with self.subTest(field=field):
                other = {**self.manager, field: value}
                self.make_raw(flat=True, kernel_manager=other)
                with self.assertRaises(ValueError):
                    prepare_release.prepare(self.source, self.output)
                self.assert_no_output()
                for path in self.source.iterdir():
                    path.unlink()

    def test_missing_changed_or_unclaimed_boot_is_rejected_before_any_output(self):
        _, _, document = self.make_raw(boot=True, flat=True)
        name = next(iter(document['boot_sha256']))
        boot = self.source / name
        original = boot.read_bytes()
        boot.write_bytes(b'ANDROID! wrong kernel boot image')
        with self.assertRaisesRegex(ValueError, 'Boot image SHA256'):
            prepare_release.prepare(self.source, self.output)
        self.assert_no_output()
        boot.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing nonempty'):
            prepare_release.prepare(self.source, self.output)
        self.assert_no_output()
        boot.write_bytes(original)
        (self.source / 'android99-9.9.1-2025-01-boot.img').write_bytes(b'unrelated boot')
        with self.assertRaisesRegex(ValueError, 'no matching kernel'):
            prepare_release.prepare(self.source, self.output)
        self.assert_no_output()

    def test_raw_sukisu_kernel_requires_its_verified_signed_manager(self):
        apk, _, _ = self.make_raw(flat=True)
        apk.unlink()
        with self.assertRaisesRegex(ValueError, 'verified signed manager'):
            prepare_release.prepare(self.source, self.output)
        self.assert_no_output()

    def test_duplicate_target_files_or_invalid_apk_signature_leave_no_partial_release(self):
        apk, ak3, _ = self.make_raw(flat=True)
        other = self.source / 'duplicate-artifact'
        other.mkdir()
        duplicate = other / ak3.name
        shutil.copyfile(ak3, duplicate)
        with self.assertRaisesRegex(ValueError, 'Duplicate public release'):
            prepare_release.prepare(self.source, self.output)
        self.assert_no_output()
        duplicate.unlink()
        apk.write_bytes(self.apk_bytes.replace(b'APK dex fixture', b'APK dex fixturE'))
        with self.assertRaisesRegex(ValueError, 'digest verification'):
            prepare_release.prepare(self.source, self.output)
        self.assert_no_output()

    def test_actual_image_and_ikconfig_are_revalidated_without_reader_mocks(self):
        _, ak3, document = self.make_raw(flat=True)
        with zipfile.ZipFile(ak3) as archive:
            data = {name: archive.read(name) for name in archive.namelist()}
        data['Image'] = b'kernel image with missing IKCONFIG'
        document['image_sha256'] = hashlib.sha256(data['Image']).hexdigest()
        data['META-INF/gki/build.json'] = json.dumps(document).encode('utf-8')
        with zipfile.ZipFile(ak3, 'w') as archive:
            for name, value in data.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(ValueError, 'IKCONFIG'):
            prepare_release.prepare(self.source, self.output)
        self.assert_no_output()

    def test_release_rechecks_entry_even_with_matching_image_hash_and_ikconfig(self):
        _, ak3, document = self.make_raw(flat=True)
        with zipfile.ZipFile(ak3) as archive:
            data = {name: archive.read(name) for name in archive.namelist()}
        image = bytearray(data['Image'])
        struct.pack_into('<I', image, 0, 0x14000400)
        data['Image'] = bytes(image)
        document['image_sha256'] = hashlib.sha256(image).hexdigest()
        data['META-INF/gki/build.json'] = json.dumps(document).encode('utf-8')
        with zipfile.ZipFile(ak3, 'w') as archive:
            for name, value in data.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(ValueError, 'outside Image'):
            prepare_release.prepare(self.source, self.output, expected_kernel_count=1)
        self.assert_no_output()

    def test_multiple_targets_keep_one_complete_configuration_table(self):
        self.make_raw(flat=True)
        document, image = kernel_fixture(manager=self.manager,
                                         file='android15-6.6.98-2025-09-AnyKernel3.zip')
        self.write_asset(document['file'], ak3_bytes(document, image), flat=True)
        report = prepare_release.prepare(self.source, self.output)
        self.assertEqual({'assets': 3, 'kernels': 2, 'manager': True}, report)
        notes = (self.output / 'release.md').read_text(encoding='utf-8')
        self.assertEqual(1, notes.count('| LTO |'))
        self.assertIn('Android 14 / 6.1', notes)
        self.assertIn('Android 15 / 6.6', notes)
        self.assertIn('2 个内核目标', notes)

    def test_existing_unrelated_output_cannot_be_accidentally_included_in_release(self):
        self.make_raw(kernel=False, flat=True)
        assets = self.output / 'assets'
        assets.mkdir(parents=True)
        unrelated = assets / 'unrelated-AnyKernel3.zip'
        unrelated.write_bytes(b'user-owned file')
        with self.assertRaisesRegex(ValueError, 'already contains'):
            prepare_release.prepare(self.source, self.output)
        self.assertEqual([unrelated], list(assets.iterdir()))
        self.assertEqual(b'user-owned file', unrelated.read_bytes())

    def test_decompressed_ak3_cannot_silently_publish_an_apk_only_kernel_run(self):
        _, ak3, _ = self.make_raw(flat=True)
        # Reproduce download-artifact's default ZIP expansion using only the
        # known synthetic fixture members. The original public ZIP disappears.
        expanded = self.source / 'expanded-raw-artifact'
        with zipfile.ZipFile(ak3) as archive:
            for name in archive.namelist():
                target = expanded / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        ak3.unlink()
        with self.assertRaisesRegex(ValueError, 'Expected 1 kernel builds, found 0'):
            prepare_release.prepare(self.source, self.output, expected_kernel_count=1)
        self.assert_no_output()

    def test_expected_kernel_count_rejects_missing_extra_and_accepts_exact_targets(self):
        self.make_raw(flat=True)
        for expected in (0, 2):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, 'kernel builds, found 1'):
                prepare_release.prepare(self.source, self.output, expected_kernel_count=expected)
            self.assert_no_output()
        result = prepare_release.prepare(self.source, self.output, expected_kernel_count=1)
        self.assertEqual(1, result['kernels'])

    def test_apk_only_explicit_zero_count_and_cli_validation_remain_supported(self):
        self.make_raw(kernel=False, flat=True)
        arguments = ['--input', str(self.source), '--output', str(self.output), '--expected-kernel-count']
        with contextlib.redirect_stderr(io.StringIO()):
            for value in ('-1', '1.5', 'not-a-count'):
                with self.subTest(value=value), self.assertRaises(SystemExit) as error:
                    prepare_release.main(arguments + [value])
                self.assertEqual(2, error.exception.code)
                self.assert_no_output()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, prepare_release.main(arguments + ['1']))
        self.assert_no_output()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(0, prepare_release.main(arguments + ['0']))
        self.assertEqual({'assets': 1, 'kernels': 0, 'manager': True}, json.loads(stdout.getvalue()))

    def test_programmatic_expected_count_does_not_accept_bool_float_or_negative(self):
        self.make_raw(kernel=False, flat=True)
        for count in (True, False, 0.0, -1, '1'):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, 'nonnegative integer'):
                prepare_release.prepare(self.source, self.output, expected_kernel_count=count)
            self.assert_no_output()


if __name__ == '__main__':
    unittest.main()
