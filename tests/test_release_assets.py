from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import sys
import unittest
from unittest.mock import patch
import uuid
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import package_release_kernel as assets

IMAGE = b'compiled ARM64 Image with actual IKCONFIG'
IMAGE_SHA = hashlib.sha256(IMAGE).hexdigest()
PREFIX = 'android14-6.1.145-2025-08'


class KernelReleaseAssetTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.release-assets-' + uuid.uuid4().hex)
        self.workspace = self.root / 'workspace'
        self.output = self.workspace / 'release-ready'
        self.output.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.metadata = self.output / 'kernel.json'
        self.document = {'file': PREFIX + '-AnyKernel3.zip', 'image_sha256': IMAGE_SHA,
                         'android_version': 'android14', 'kernel_version': '6.1', 'sub_level': '145',
                         'os_patch_level': '2025-08', 'features': {'susfs': True, 'kpm': False}}
        self.write_metadata(self.document)
        self.ak3 = self.workspace / self.document['file']
        self.make_ak3()

    def write_metadata(self, document):
        self.metadata.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def make_ak3(self, *, missing=None, image=IMAGE, extra=None):
        with zipfile.ZipFile(self.ak3, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, value in [('Image', image), ('anykernel.sh', b'test-anykernel-script'),
                                ('META-INF/com/google/android/update-binary', b'update-binary')]:
                if name != missing:
                    archive.writestr(name, value)
            if extra is not None:
                name, value = extra
                archive.writestr(name, value)

    def package(self, mode='仅 AK3'):
        return assets.package(self.metadata, self.workspace, self.output, mode)

    def assert_no_assets(self, original):
        self.assertEqual(self.metadata.read_bytes(), original)
        self.assertEqual(list(self.output.iterdir()), [self.metadata])

    def test_only_ak3_copies_exact_zip_and_records_no_boot_images_or_extra_zip(self):
        (self.workspace / 'unrelated.img').write_bytes(b'not selected in AK3-only mode')
        result = self.package()
        self.assertEqual(result['files'], [self.ak3.name])
        updated = json.loads(self.metadata.read_text(encoding='utf-8'))
        self.assertEqual(updated['files'], [self.ak3.name])
        self.assertEqual(updated['features'], self.document['features'])
        self.assertEqual((self.output / self.ak3.name).read_bytes(), self.ak3.read_bytes())
        self.assertEqual(sorted(path.suffix for path in self.output.iterdir()), ['.json', '.zip'])
        with zipfile.ZipFile(self.output / self.ak3.name) as archive:
            self.assertEqual(archive.read('Image'), IMAGE)
            self.assertFalse(any(name.endswith('.zip') for name in archive.namelist()))

    def test_boot_mode_copies_only_current_target_generated_boot_variants(self):
        filenames = [PREFIX + suffix for suffix in assets.BOOT_SUFFIXES]
        for name in filenames:
            (self.workspace / name).write_bytes(b'ANDROID!' + name.encode())
        result = self.package('AK3 + boot 镜像')
        self.assertEqual(result['files'], [self.ak3.name] + sorted(filenames))
        for name in result['files']:
            self.assertEqual((self.output / name).read_bytes(), (self.workspace / name).read_bytes())
        self.assertEqual(json.loads(self.metadata.read_text())['files'], result['files'])

    def test_lts_target_names_are_supported_consistently(self):
        self.ak3 = self.ak3.rename(self.workspace / self.ak3.name.replace('2025-08', 'lts'))
        self.document.update(file=self.ak3.name, os_patch_level='lts')
        self.write_metadata(self.document)
        name = self.ak3.name.removesuffix('-AnyKernel3.zip') + '-boot.img'
        (self.workspace / name).write_bytes(b'ANDROID! LTS kernel')
        self.assertIn(name, self.package('AK3 + boot 镜像')['files'])

    def test_matrix_lts_x_sublevel_packages_ak3_and_matching_boot_image(self):
        self.ak3 = self.ak3.rename(self.workspace / 'android14-6.1.X-lts-AnyKernel3.zip')
        self.document.update(file=self.ak3.name, sub_level='X', os_patch_level='lts')
        self.write_metadata(self.document)
        self.assertEqual(self.package()['files'], [self.ak3.name])
        boot = self.workspace / 'android14-6.1.X-lts-boot.img'
        boot.write_bytes(b'ANDROID! LTS kernel')
        self.assertEqual(self.package('AK3 + boot 镜像')['files'], [self.ak3.name, boot.name])
        self.assertEqual((self.output / boot.name).read_bytes(), boot.read_bytes())

    def test_same_existing_assets_are_reused_idempotently(self):
        self.package()
        original = self.metadata.read_bytes()
        with patch.object(assets.os, 'link', side_effect=AssertionError('existing asset must be reused')):
            self.package()
        self.assertEqual(self.metadata.read_bytes(), original)

    def test_missing_required_ak3_entries_and_empty_image_leave_metadata_unchanged(self):
        original = self.metadata.read_bytes()
        for missing in assets.REQUIRED:
            self.make_ak3(missing=missing)
            with self.subTest(missing=missing), self.assertRaises(assets.PackagingError):
                self.package()
            self.assert_no_assets(original)
        self.make_ak3(image=b'')
        with self.assertRaises(assets.PackagingError):
            self.package()
        self.assert_no_assets(original)

    def test_mismatched_image_sha_is_rejected_before_publication(self):
        self.make_ak3(image=b'different kernel')
        original = self.metadata.read_bytes()
        with self.assertRaisesRegex(assets.PackagingError, 'Image SHA256'):
            self.package()
        self.assert_no_assets(original)

    def test_unsafe_or_ambiguous_zip_entries_cannot_be_published(self):
        original = self.metadata.read_bytes()
        for name in ('../outside', '/absolute', 'C:/windows', 'tools/../../outside', 'tools\\outside',
                     'tools//outside', './Image', 'tools/./outside', 'Image'):
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                # Windows ZipInfo normalizes backslashes when authoring a ZIP.
                # Patch equal-length filename bytes to emulate an untrusted ZIP
                # created on another system without changing entry data/CRCs.
                authored_name = name.replace('\\', '/')
                self.make_ak3(extra=(authored_name, b'bad entry'))
                if authored_name != name:
                    self.ak3.write_bytes(self.ak3.read_bytes().replace(authored_name.encode(), name.encode()))
            with self.subTest(name=name), self.assertRaises(assets.PackagingError):
                self.package()
            self.assert_no_assets(original)

    def test_zip_symlink_is_rejected(self):
        with zipfile.ZipFile(self.ak3, 'a') as archive:
            link = zipfile.ZipInfo('tools/linked-file')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, '../../outside')
        original = self.metadata.read_bytes()
        with self.assertRaisesRegex(assets.PackagingError, 'file type'):
            self.package()
        self.assert_no_assets(original)

    def test_zip_crc_corruption_is_rejected(self):
        content = self.ak3.read_bytes().replace(b'test-anykernel-script', b'best-anykernel-script', 1)
        self.ak3.write_bytes(content)
        original = self.metadata.read_bytes()
        with self.assertRaises(assets.PackagingError):
            self.package()
        self.assert_no_assets(original)

    def test_missing_malformed_empty_or_wrong_target_boot_files_are_rejected(self):
        original = self.metadata.read_bytes()
        with self.assertRaisesRegex(assets.PackagingError, 'any boot images'):
            self.package('AK3 + boot 镜像')
        for name, content in [('boot.img', b'ANDROID!'),
                              (PREFIX.replace('6.1.145', '6.1.146') + '-boot.img', b'ANDROID!'),
                              (PREFIX + '-boot-unknown.img', b'ANDROID!'), (PREFIX + '-boot.img', b'')]:
            image = self.workspace / name
            image.write_bytes(content)
            with self.subTest(name=name), self.assertRaises(assets.PackagingError):
                self.package('AK3 + boot 镜像')
            self.assert_no_assets(original)
            image.unlink()

    def test_unrelated_existing_asset_is_never_overwritten(self):
        destination = self.output / self.ak3.name
        destination.write_bytes(b'an unrelated release asset')
        original = self.metadata.read_bytes()
        with self.assertRaisesRegex(assets.PackagingError, 'unrelated'):
            self.package()
        self.assertEqual(destination.read_bytes(), b'an unrelated release asset')
        self.assertEqual(self.metadata.read_bytes(), original)

    def test_copy_or_metadata_commit_failure_rolls_back_new_assets(self):
        original = self.metadata.read_bytes()
        for function in ('copyfile', 'replace'):
            target = assets.shutil if function == 'copyfile' else assets.os
            with self.subTest(function=function), patch.object(target, function, side_effect=OSError('simulated failure')):
                with self.assertRaises(OSError):
                    self.package()
            self.assert_no_assets(original)
            self.assertFalse(list(self.output.glob('*.tmp')))

    def test_publication_failure_rolls_back_only_files_created_by_this_attempt(self):
        image = self.workspace / (PREFIX + '-boot.img')
        image.write_bytes(b'ANDROID!')
        existing = self.output / self.ak3.name
        shutil.copyfile(self.ak3, existing)
        original = self.metadata.read_bytes()
        with patch.object(assets.os, 'link', side_effect=OSError('simulated publication failure')):
            with self.assertRaises(OSError):
                self.package('AK3 + boot 镜像')
        self.assertEqual(existing.read_bytes(), self.ak3.read_bytes())
        self.assertEqual(self.metadata.read_bytes(), original)
        self.assertEqual(set(self.output.iterdir()), {self.metadata, existing})

    def test_failure_after_first_new_publication_removes_the_first_asset(self):
        (self.workspace / (PREFIX + '-boot.img')).write_bytes(b'ANDROID!')
        original = self.metadata.read_bytes()
        publish = assets.os.link
        calls = 0

        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('second publication failed')
            publish(source, destination)

        with patch.object(assets.os, 'link', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'second publication'):
                self.package('AK3 + boot 镜像')
        self.assert_no_assets(original)

    def test_unrelated_second_asset_is_found_before_copying_the_first(self):
        name = PREFIX + '-boot.img'
        (self.workspace / name).write_bytes(b'ANDROID! current build')
        existing = self.output / name
        existing.write_bytes(b'ANDROID! unrelated build')
        original = self.metadata.read_bytes()
        with patch.object(assets.shutil, 'copyfile') as copying:
            with self.assertRaisesRegex(assets.PackagingError, 'unrelated'):
                self.package('AK3 + boot 镜像')
            copying.assert_not_called()
        self.assertEqual(self.metadata.read_bytes(), original)
        self.assertEqual(set(self.output.iterdir()), {self.metadata, existing})

    def test_invalid_metadata_names_hashes_targets_or_duplicate_fields_are_rejected(self):
        for changes in ({'file': '../outside.zip'}, {'file': 'arbitrary.zip'}, {'image_sha256': 'bad'},
                        {'image_sha256': IMAGE_SHA + '\n'}, {'kernel_version': '6.6'}, {'sub_level': 146},
                        {'file': self.document['file'].replace('2025-08', '2025-13')}):
            document = {**self.document, **changes}
            self.write_metadata(document)
            original = self.metadata.read_bytes()
            with self.subTest(changes=changes), self.assertRaises(assets.PackagingError):
                self.package()
            self.assert_no_assets(original)
        self.metadata.write_text('{"file":"first.zip","file":"second.zip"}', encoding='utf-8')
        with self.assertRaisesRegex(assets.PackagingError, 'duplicate JSON'):
            self.package()

    def test_cli_collects_direct_assets_and_emits_public_summary(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            status = assets.main(['--metadata', str(self.metadata), '--workspace', str(self.workspace),
                                  '--output', str(self.output), '--mode', '仅 AK3'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue())['files'], [self.ak3.name])


if __name__ == '__main__':
    unittest.main()
