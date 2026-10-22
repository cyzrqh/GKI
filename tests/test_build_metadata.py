import gzip
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import capture_build_metadata as metadata
import prepare_release


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
        self.image.write_bytes(b'kernel ' + version + b' IKCFG_ST' + gzip.compress(config.encode()) + b'IKCFG_ED')

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

    def manager(self):
        directory = self.source / 'Release-Manager'
        directory.mkdir()
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


if __name__ == '__main__':
    unittest.main()
