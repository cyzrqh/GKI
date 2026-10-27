"""Platform/native-release packaging with real test Git identities and APK-v2 signing.

The ARM64 and KPM fixtures are deliberately non-loadable test bytes. These tests
verify source, configuration, guard and transport checks, never device bootability.
"""
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import unittest
from unittest import mock
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import oplus_assets as assets
import oplus_catalog as catalog
from apk_fixture import manager_metadata, signed_apk
from manager_variants import manager_profile
from test_kpm_patch import payload_fixture, tool_fixture, patched_fixture
from validate_arm64_image import validate_image


def posix_shell():
    for name in ('sh', 'bash'):
        if path := shutil.which(name):
            return path
    if git := shutil.which('git'):
        candidate = Path(git).parent.parent / 'usr/bin/sh.exe'
        if candidate.is_file():
            return str(candidate)
    return None


SHELL = posix_shell()


def run_git(path, *arguments):
    result = subprocess.run(['git', '-C', str(path), *arguments], capture_output=True,
                            text=True, encoding='utf-8', timeout=20)
    if result.returncode:
        raise RuntimeError('Test Git identity setup failed: ' + result.stderr)
    return result.stdout.strip()


def initialize(path, files, origin):
    path.mkdir(parents=True)
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    run_git(path, 'init', '--quiet')
    run_git(path, 'config', 'user.name', 'Non-installable fixture')
    run_git(path, 'config', 'user.email', 'test@example.invalid')
    run_git(path, 'config', 'core.autocrlf', 'false')
    run_git(path, 'remote', 'add', 'origin', origin)
    run_git(path, 'add', '.')
    run_git(path, 'commit', '--quiet', '-m', 'Non-installable test identity')
    return run_git(path, 'rev-parse', 'HEAD')


def image_fixture(manager, native='6.1.57-android14-11-test', *, susfs=True, multi=True, extra_config='', kpm=False):
    config = ('CONFIG_ARM64=y\nCONFIG_KSU=y\nCONFIG_LTO_CLANG_THIN=y\nCONFIG_ZRAM=y\n'
              'CONFIG_CRYPTO_LZ4KD=y\nCONFIG_NTSYNC=m\n'
              + ('CONFIG_KPM=y\n' if kpm else '# CONFIG_KPM is not set\n')
              + ('CONFIG_KSU_SUSFS=y\n' if susfs else '# CONFIG_KSU_SUSFS is not set\n')
              + ('CONFIG_KSU_MULTI_MANAGER_SUPPORT=y\n' if multi else '') + extra_config)
    text = (b'Linux version ' + native.encode() + b'\0' + manager['package'].encode() + b'\0'
            + manager['manager_cert_sha256'].encode() + b'\0v2.3.0\0IKCFG_ST'
            + gzip.compress(config.encode(), mtime=0) + b'IKCFG_ED')
    image = bytearray(16384)
    struct.pack_into('<IIQQQQQQII', image, 0, 0x91005a4d, 0x1400001f, 0x80000,
                     len(image), 0xa, 0, 0, 0, 0x644d5241, 0x40)
    struct.pack_into('<I', image, 128, 0xd503201f)
    image[256:256 + len(text)] = text
    return bytes(image)


def rewrite_zip(path, change=None, additions=None, remove=()):
    with zipfile.ZipFile(path) as archive:
        data = {info.filename: archive.read(info) for info in archive.infolist()}
    for name in remove:
        data.pop(name, None)
    if change:
        change(data)
    data.update(additions or {})
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in data.items():
            archive.writestr(name, content)


def change_metadata(data, change):
    document = json.loads(data[assets.MEMBER])
    change(document)
    data[assets.MEMBER] = json.dumps(document).encode()


class OplusAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = ROOT.parent / '.gki-upstream-cache' / ('oplus-assets-tests-' + uuid.uuid4().hex)
        cls.base.mkdir(parents=True)
        cls.source = cls.base / 'source'
        cls.common_ref = initialize(cls.source / 'common', {
            'Makefile': b'VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 57\n',
            'include/linux/susfs.h': b'#define SUSFS_VERSION "v2.3.0"\n'},
            'https://github.com/OnePlusOSS/android_kernel_common_oneplus_sm8650')
        cls.ksu_ref = initialize(cls.source / 'KernelSU', {'fixture': b'not a real kernel source\n'},
                                 'https://github.com/SukiSU-Ultra/SukiSU-Ultra')
        cls.template_source = cls.base / 'template'
        cls.ak3_ref = initialize(cls.template_source, {
            'LICENSE': b'Official-runtime transport fixture, not executable\n',
            'tools/ak3-core.sh': b'# inert fixture, no boot writes\n',
            'META-INF/com/google/android/update-binary': b'# inert test runtime\n',
            'anykernel.sh': b'# This example must never be included\n',
            'Image': b'example must never be included\n'},
            'https://github.com/osm0sis/AnyKernel3')

    @classmethod
    def tearDownClass(cls):
        expected = ROOT.parent / '.gki-upstream-cache'
        if cls.base.resolve().parent != expected.resolve() or not cls.base.name.startswith('oplus-assets-tests-'):
            raise RuntimeError('Refusing cleanup outside this test fixture directory')
        def writable(function, path, error):
            if not isinstance(error, PermissionError):
                raise error
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)
        shutil.rmtree(cls.base, onexc=writable)

    def setUp(self):
        self.directory = self.base / uuid.uuid4().hex
        self.directory.mkdir()
        self.workspace = self.directory / 'kernel_workspace'
        shutil.copytree(self.source, self.workspace)
        self.template = self.directory / 'ak3'
        shutil.copytree(self.template_source, self.template)
        self.profile = copy.deepcopy(next(item for item in catalog.load_profiles() if item['id'] == 'sm8650-6.1.57'))
        self.profile.pop('verified_kernel_ref')
        self.profile.update(kernel_ref=self.common_ref, source_committed_at='2026-10-27T00:00:00Z')
        self.native = '6.1.57-android14-11-test'
        release = self.workspace / 'common/out/include/config/kernel.release'
        release.parent.mkdir(parents=True)
        release.write_text(self.native + '\n', encoding='utf-8')
        self.image = self.workspace / 'common/out/arch/arm64/boot/Image'
        self.image.parent.mkdir(parents=True)
        self.inputs = self.directory / 'native-input'
        self.inputs.mkdir()
        self.output = self.directory / 'reports'
        self.environment = mock.patch.dict(os.environ, {
            'GITHUB_REPOSITORY': 'cyzrqh/gki', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_SHA': 'a' * 40, 'ENABLE_SUSFS': 'true', 'AK3_REF': self.ak3_ref}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.manager = self.use_manager('SukiSU')

    def use_manager(self, variant, **changes):
        code = changes.get('ksu_version_code', '40922')
        manager = manager_metadata(file=f'{variant}_v4.2.0_{code}-release.apk',
                                   package=manager_profile(variant)['package'], ksu_ref=self.ksu_ref,
                                   ksu_variant=variant, **changes)
        apk = self.inputs / manager['file']
        apk.write_bytes(signed_apk(manager))
        manager = assets.read_manager_apk(apk)
        for key, value in {
            'KSU_VARIANT': variant, 'KSU_REF': manager['ksu_ref'], 'KSU_BRANCH': manager['ksu_branch'],
            'MANAGER_VERSION_CODE': manager['ksu_version_code'], 'MANAGER_VERSION_NAME': manager['ksu_version_name'],
            'MANAGER_PACKAGE': manager['package'], 'MANAGER_CERT_SIZE': manager['manager_cert_size'],
            'MANAGER_CERT_SHA256': manager['manager_cert_sha256'], 'MANAGER_APK_FILENAME': manager['file'],
            'MANAGER_APK_SHA256': manager['apk_sha256']}.items():
            os.environ[key] = str(value)
        self.image.write_bytes(image_fixture(manager, self.native))
        return manager

    def pack(self, profile=None, output=None):
        result = assets.package(profile or self.profile, self.workspace, self.image, self.template,
                                output or self.inputs)
        return Path(result['path'])

    def gather(self, matrix=None, variants='SukiSU'):
        return assets.collect(self.inputs, self.output, matrix or {'include': [self.profile]}, variants)

    def test_package_preserves_native_release_and_paired_actual_image(self):
        path = self.pack()
        self.assertEqual(path.name, 'Oplus-Snapdragon-android14-6.1.57-SukiSU-AnyKernel3.zip')
        document = assets.read_kernel(path)
        self.assertEqual(document['schema'], 'gki-oplus-kernel')
        self.assertEqual(document['profile'], self.profile)
        self.assertEqual(document['vendor_sources'], {})
        self.assertEqual(document['kernel_release'], self.native)
        self.assertEqual(document['manager_apk_sha256'], self.manager['apk_sha256'])
        self.assertEqual(document['image_entry'], validate_image(self.image.read_bytes()))
        self.assertTrue(document['features']['susfs'])
        self.assertEqual(document['susfs_version'], 'v2.3.0')
        self.assertEqual(document['features']['ntsync'], '模块')
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read('Image'), self.image.read_bytes())

            self.assertEqual(archive.read('tools/ak3-core.sh'), b'# inert fixture, no boot writes\n')
            self.assertNotIn('META-INF/gki-kernel.json', archive.namelist())
            self.assertNotIn(assets.PAYLOAD_MEMBER, archive.namelist())
            script = archive.read('anykernel.sh').decode()
        self.assertLess(script.index('oplus_soc_match=0'), script.index('split_boot;'))
        self.assertLess(script.index('oplus_running_release='), script.index('split_boot;'))
        self.assertIn('sm8650|sm8650[!0-9]*)', script)
        self.assertIn('6.1.57-android14-11|6.1.57-android14-11-*)', script)
        self.assertIn('BLOCK=boot;', script)
        self.assertIn('PATCH_VBMETA_FLAG=0;', script)
        self.assertIn('IS_SLOT_DEVICE=auto;', script)
        self.assertIn('flash_boot;', script)
        for forbidden in ('BLOCK=auto;', 'NO_VBMETA_PARTITION_PATCH', 'dump_boot;', 'write_boot;',
                          'init_boot', 'vendor_boot', 'flash_generic', 'dtbo'):
            self.assertNotIn(forbidden, script)
        self.assertNotIn('/data', script)
        self.assertNotIn('patch_cmdline', script)
        self.assertNotIn('replace_string', script)

    def test_public_names_keep_both_families_without_cpu_codes_or_collisions(self):
        names = [assets.filename({'platform': platform, 'android_version': 'android15',
                                  'kernel': '6.6.118'}, 'SukiSU')
                 for platform in ('sm8750', 'mt6991')]
        self.assertEqual(names, ['Oplus-Snapdragon-android15-6.6.118-SukiSU-AnyKernel3.zip',
                                 'Oplus-Dimensity-android15-6.6.118-SukiSU-AnyKernel3.zip'])
        self.assertNotEqual(*names)

    def test_collect_validates_true_signed_apk_and_emits_only_reports_and_public_title(self):
        path = self.pack()
        nested = self.inputs / 'downloaded-artifact'
        nested.mkdir()
        path.rename(nested / path.name)
        result = self.gather()
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['managers'], ['SukiSU'])
        self.assertEqual({item.name for item in self.output.iterdir()}, {'SHA256SUMS.txt', 'summary.md', 'title.txt'})
        hashes = self.output.joinpath('SHA256SUMS.txt').read_text(encoding='utf-8')
        self.assertIn(self.manager['apk_sha256'] + '  ' + self.manager['file'], hashes)
        summary = self.output.joinpath('summary.md').read_text(encoding='utf-8')
        self.assertEqual(summary.count('## 构建配置'), 1)
        self.assertIn(self.native, summary)
        self.assertIn('| 骁龙 | 6.1.57 |', summary)
        self.assertNotIn('骁龙 sm8650', summary)
        self.assertNotIn('TCP 拥塞算法', summary)
        self.assertNotIn('构建文件', summary)
        self.assertNotIn('true', summary)
        self.assertNotIn('false', summary)
        self.assertNotIn('null', summary)
        self.assertIn('| SUSFS | 开启 |', summary)
        self.assertIn('| KPM | 关闭 |', summary)
        self.assertEqual('OnePlus · SukiSU 40922 开发版', (self.output / 'title.txt').read_text())
        self.assertNotIn('title.txt', hashes)
        self.assertNotIn('summary.md', hashes)

    def test_title_uses_actual_signed_manager_numeric_code_and_resolved_tier(self):
        for variant, branch, code, label in (('SukiSU', 'Stable(标准)', '41095', '正式版'),
                                            ('SukiSU', 'Latest(最新)', '41099', '最新测试版'),
                                            ('BakaSU', 'Latest(最新)', '35252', '最新测试版')):
            with self.subTest(variant=variant, branch=branch):
                self.inputs.joinpath(self.manager['file']).unlink()
                self.manager = self.use_manager(variant, ksu_branch=branch, ksu_version_code=code)
                path = self.pack()
                with mock.patch.dict(os.environ, {'MANAGER_VERSION_CODE': '999999', 'KSU_BRANCH': 'Custom(自定义)'}):
                    self.gather(variants=variant)
                self.assertEqual('OnePlus · ' + variant + ' ' + code + ' ' + label,
                                 (self.output / 'title.txt').read_text())
                self.assertNotIn('999999', (self.output / 'title.txt').read_text())
                path.unlink()
                shutil.rmtree(self.output)

    def test_actual_manager_and_kernel_tier_or_numeric_version_mismatch_cannot_publish_title(self):
        path = self.pack()
        original = path.read_bytes()
        for field, changed in (('ksu_branch', 'Stable(标准)'), ('ksu_version_code', '41099')):
            path.write_bytes(original)
            rewrite_zip(path, change=lambda data: change_metadata(data, lambda doc: doc.update({field: changed})))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.gather()
            self.assertFalse((self.output / 'title.txt').exists())

    def test_bakasu_has_matching_manager_and_multi_manager_kernel(self):
        self.inputs.joinpath(self.manager['file']).unlink()
        manager = self.use_manager('BakaSU')
        path = self.pack()
        self.assertIn('-BakaSU-AnyKernel3.zip', path.name)
        self.assertEqual(assets.read_kernel(path)['manager_package'], manager_profile('BakaSU')['package'])
        self.assertEqual(self.gather(variants='BakaSU')['managers'], ['BakaSU'])
        self.assertEqual(manager['ksu_variant'], 'BakaSU')

    def test_both_explicit_variants_can_be_collected_without_assuming_two_by_default(self):
        self.pack()
        self.use_manager('BakaSU')
        self.pack()
        result = self.gather(variants='SukiSU,BakaSU')
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['managers'], ['BakaSU', 'SukiSU'])

    def test_source_ref_origin_makefile_and_reviewed_branch_fail_closed(self):
        variants = [dict(self.profile, kernel_ref='f' * 40),
                    dict(self.profile, kernel_repo='cctv18/android_kernel_common_oneplus_sm8650'),
                    dict(self.profile, kernel_branch='oneplus/sm8650_unknown_unreviewed')]
        for profile in variants:
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                self.pack(profile)
        makefile = self.workspace / 'common/Makefile'
        makefile.write_text('VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 118\n')
        with self.assertRaisesRegex(ValueError, 'Makefile'):
            self.pack()
        makefile.write_bytes(b'VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 57\n')
        run_git(self.workspace / 'common', 'remote', 'set-url', 'origin', 'https://github.com/cctv18/mirror')
        with self.assertRaisesRegex(ValueError, 'origin'):
            self.pack()

    def test_native_full_version_android_kmi_and_banner_must_match(self):
        release = self.workspace / 'common/out/include/config/kernel.release'
        for value in ('6.1.57', '6.1.57-android15-11-test', '6.1.58-android14-11-test',
                      '6.1.57-android14-test', '6.1.57-android14-11-different-banner'):
            release.write_text(value + '\n')
            with self.subTest(release=value), self.assertRaises(ValueError):
                self.pack()

    def test_image_must_contain_actual_manager_certificate_package_and_valid_arm64_entry(self):
        original = self.image.read_bytes()
        for needle in (self.manager['package'].encode(), self.manager['manager_cert_sha256'].encode(), b'ARM\x64'):
            changed = original.replace(needle, b'x' * len(needle), 1)
            self.image.write_bytes(changed)
            with self.subTest(needle=needle), self.assertRaises(ValueError):
                self.pack()

    def test_susfs_menu_must_match_actual_configuration_and_fixed_version(self):
        self.image.write_bytes(image_fixture(self.manager, self.native, susfs=False))
        with self.assertRaises(ValueError):
            self.pack()
        os.environ['ENABLE_SUSFS'] = 'false'
        path = self.pack()
        self.assertFalse(assets.read_kernel(path)['features']['susfs'])
        path.unlink()
        self.image.write_bytes(image_fixture(self.manager, self.native))
        with self.assertRaises(ValueError):
            self.pack()
        os.environ['ENABLE_SUSFS'] = 'true'
        self.workspace.joinpath('common/include/linux/susfs.h').write_text('#define SUSFS_VERSION "v2.2.0"\n')
        with self.assertRaises(ValueError):
            self.pack()

    def test_bakasu_without_multi_manager_configuration_is_rejected(self):
        manager = self.use_manager('BakaSU')
        self.image.write_bytes(image_fixture(manager, self.native, multi=False))
        with self.assertRaisesRegex(ValueError, 'paired manager'):
            self.pack()

    def test_template_is_official_pinned_clean_and_safe(self):
        os.environ['AK3_REF'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'pinned'):
            self.pack()
        os.environ['AK3_REF'] = self.ak3_ref
        self.template.joinpath('tools/ak3-core.sh').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changes'):
            self.pack()
        run_git(self.template, 'checkout', '--', 'tools/ak3-core.sh')
        run_git(self.template, 'remote', 'set-url', 'origin', 'https://github.com/unreviewed/AnyKernel3')
        with self.assertRaisesRegex(ValueError, 'official'):
            self.pack()

    def test_packaging_never_overwrites_an_existing_native_artifact(self):
        path = self.pack()
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'replace'):
            self.pack()
        self.assertEqual(path.read_bytes(), original)

    def test_reader_detects_modified_image_script_runtime_and_profile(self):
        path = self.pack()
        original = path.read_bytes()
        changes = [lambda data: data.update(Image=data['Image'] + b'changed'),
                   lambda data: data.update({'anykernel.sh': data['anykernel.sh'].replace(b'PATCH_VBMETA_FLAG=0', b'PATCH_VBMETA_FLAG=1')}),
                   lambda data: data.update({'tools/ak3-core.sh': b'changed runtime'}),
                   lambda data: change_metadata(data, lambda doc: doc['profile'].update(kernel_branch='oneplus/sm8650_unreviewed')),
                   lambda data: change_metadata(data, lambda doc: doc.update(vendor_sources={'repository': 'unreviewed/vendor'}))]
        for change in changes:
            path.write_bytes(original)
            rewrite_zip(path, change=change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                assets.read_kernel(path)

    def test_reader_rejects_extra_files_path_traversal_missing_runtime_and_rename(self):
        path = self.pack()
        original = path.read_bytes()
        for addition, remove in [({'unexpected.txt': b'x'}, ()), ({'../escape': b'x'}, ()),
                                 ({}, ('tools/ak3-core.sh',))]:
            path.write_bytes(original)
            rewrite_zip(path, additions=addition, remove=remove)
            with self.subTest(addition=addition, remove=remove), self.assertRaises(ValueError):
                assets.read_kernel(path)
        path.write_bytes(original)
        renamed = path.with_name(path.name.replace('Snapdragon', 'Dimensity'))
        path.rename(renamed)
        with self.assertRaisesRegex(ValueError, 'filename'):
            assets.read_kernel(renamed)

    def test_reader_rejects_fake_feature_reporting_or_unbound_config(self):
        path = self.pack()
        original = path.read_bytes()
        for field, value in [('features', {'susfs': False}), ('config_sha256', 'b' * 64),
                              ('image_entry', {}), ('ak3_template', None)]:
            path.write_bytes(original)
            rewrite_zip(path, change=lambda data: change_metadata(data, lambda doc: doc.update({field: value})))
            with self.subTest(field=field), self.assertRaises(ValueError):
                assets.read_kernel(path)

    def test_collect_rejects_missing_extra_duplicate_and_wrong_matrix_assets(self):
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.gather()
        path = self.pack()
        extra = self.inputs / 'Metadata-wrapper.zip'
        extra.write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            self.gather()
        extra.unlink()
        nested = self.inputs / 'duplicate'
        nested.mkdir()
        duplicate = nested / path.name
        duplicate.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.gather()
        duplicate.unlink()
        other = copy.deepcopy(self.profile)
        other['kernel_ref'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'prepared source'):
            self.gather({'include': [other]})
        with self.assertRaisesRegex(ValueError, 'duplicates'):
            self.gather({'include': [self.profile, self.profile]})
        with self.assertRaises(ValueError):
            self.gather(variants='BakaSU')

    def test_collect_rejects_stale_run_commit_source_and_actual_signed_apk_bytes(self):
        path = self.pack()
        with mock.patch.dict(os.environ, {'GITHUB_RUN_ID': '124'}), self.assertRaisesRegex(ValueError, 'another run'):
            self.gather()
        apk = self.inputs / self.manager['file']
        replacement = dict(self.manager)
        replacement.pop('apk_sha256')
        replacement['ksu_ref'] = 'e' * 40
        apk.write_bytes(signed_apk(replacement))
        with self.assertRaisesRegex(ValueError, 'actual signed manager'):
            self.gather()
        apk.write_bytes(signed_apk({key: value for key, value in self.manager.items() if key != 'apk_sha256'}))
        original = path.read_bytes()
        rewrite_zip(path, change=lambda data: change_metadata(data, lambda doc: doc.update(producer_head_sha='b' * 40)))
        with self.assertRaisesRegex(ValueError, 'another run'):
            self.gather()
        path.write_bytes(original)

    def test_collect_runs_real_apk_signature_verification(self):
        self.pack()
        apk = self.inputs / self.manager['file']
        data = apk.read_bytes().replace(b'APK dex fixture', b'BAD dex fixture', 1)
        apk.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'digest|signature'):
            self.gather()

    def test_rerun_can_reuse_manager_from_previous_attempt_but_not_newer_attempt(self):
        os.environ['GITHUB_RUN_ATTEMPT'] = '2'
        path = self.pack()
        self.assertEqual(self.gather()['count'], 1)
        shutil.rmtree(self.output)
        rewrite_zip(path, change=lambda data: change_metadata(data, lambda doc: doc.update(producer_run_attempt='1')))
        replacement = dict(self.manager, producer_run_attempt='2')
        replacement.pop('apk_sha256')
        apk = self.inputs / self.manager['file']
        apk.write_bytes(signed_apk(replacement))
        with self.assertRaisesRegex(ValueError, 'predates'):
            self.gather()

    def test_missing_producer_identity_and_unbounded_ikconfig_are_rejected(self):
        with mock.patch.dict(os.environ, {'GITHUB_SHA': ''}), self.assertRaisesRegex(ValueError, 'identity'):
            self.pack()
        for value in (b'CONFIG_ARM64=y\n' + b'x' * (assets.MAX_CONFIG + 1), b'CONFIG_ARM64=y\n'):
            data = b'IKCFG_ST' + gzip.compress(value, mtime=0)
            if len(value) < assets.MAX_CONFIG:
                data += gzip.compress(b'extra gzip member', mtime=0)
            data += b'IKCFG_ED'
            with self.subTest(size=len(value)), self.assertRaisesRegex(ValueError, 'IKCONFIG'):
                assets.bounded_config(data)

    def test_report_output_must_be_empty_and_variants_explicit(self):
        self.pack()
        self.output.mkdir()
        preserved = self.output / 'keep'
        preserved.write_text('unchanged')
        with self.assertRaisesRegex(ValueError, 'empty'):
            self.gather()
        self.assertEqual(preserved.read_text(), 'unchanged')
        for value in (None, '', 'Official', 'SukiSU,SukiSU', 'SukiSU, BakaSU'):
            with self.subTest(variant=value), self.assertRaises(ValueError):
                assets.variants_list(value)

    def prepare_kpm(self, *, pac=False, bridge=False, native_bridge=True):
        if bridge:
            artifact = ROOT.parent / '.gki-upstream-cache/kpm-bridge2-d958-build-artifacts.zip'
            if not artifact.is_file():
                self.skipTest('Optional audited official ABI-2 assets are not cached')
            self.assertEqual(assets.digest(artifact.read_bytes()),
                             'b570fedae955f8bbb5f891dd5328220f31763a5ea41d82fdafef89cd9dc81db9')
            with zipfile.ZipFile(artifact) as archive:
                payload = archive.read('kernel/kpimg')
                tool = archive.read('patch/res/kptools-linux')
            lock = assets.kpm.load_bridge_release()
        else:
            payload, tool = payload_fixture(), tool_fixture()
            lock = {'schema_version': 1, 'repository': assets.kpm.REPOSITORY, 'tag': assets.kpm.TAG,
                    'commit': assets.kpm.COMMIT, 'assets': {}}
            for name, value in [('kpimg', payload), ('kptools-linux', tool)]:
                lock['assets'][name] = {'size': len(value), 'sha256': assets.digest(value)}
        cache = self.directory / 'kpm-cache'
        cache.mkdir()
        cache.joinpath('kpimg').write_bytes(payload)
        os.environ['KPM_CACHE_DIR'] = str(cache)
        before = image_fixture(self.manager, self.native, kpm=native_bridge)
        if pac:
            modified = bytearray(before)
            struct.pack_into('<II', modified, 128, 0xd503233f, 0xd50323bf)
            struct.pack_into('<Q', modified, 16, len(modified) + 8192)
            before = bytes(modified)
        after = bytearray(patched_fixture(before, payload))
        if pac:
            struct.pack_into('<II', after, 128, 0xd503201f, 0xd503201f)
        after = bytes(after)
        self.image.write_bytes(after)
        report = {'schema_version': 1, 'release': {key: lock[key] for key in ('repository', 'tag', 'commit')},
                  'tool': {key: lock['assets']['kptools-linux'][key] for key in ('size', 'sha256')},
                  'payload': {key: lock['assets']['kpimg'][key] for key in ('size', 'sha256')}
                             | assets.kpm.payload_metadata(payload, lock),
                  'pre_image_sha256': assets.digest(before), 'pre_image_size': len(before),
                  'post_image_sha256': assets.digest(after), 'post_image_size': len(after),
                  'pre_entry': validate_image(before), 'post_entry': validate_image(after),
                  'layout': assets.kpm.verify_patched(before, after, payload)}
        report['original_windows'] = assets.kpm.capture_original_windows(before, after, report['layout'])
        report_path = self.workspace / 'common/out/gki-kpm-patch.json'
        report_path.write_text(json.dumps(report))
        if not bridge:
            patch = mock.patch.object(assets.kpm, 'load_release', return_value=lock)
            patch.start()
            self.addCleanup(patch.stop)
        return report_path, report, payload, cache

    def test_real_abi2_payload_is_verified_through_package_and_collect_when_cached(self):
        _, report, payload, _ = self.prepare_kpm(bridge=True)
        self.assertEqual(report['release']['tag'], '0.14.0')
        self.assertEqual(report['payload']['bridge_abi'], 2)
        path = self.pack()
        metadata = assets.read_kernel(path)
        self.assertEqual(metadata['kpm_patch'], report)
        self.assertTrue(metadata['features']['kpm'])
        self.assertEqual(self.gather()['count'], 1)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read(assets.PAYLOAD_MEMBER), payload)
        corrupted = copy.deepcopy(report)
        corrupted['payload']['bridge_abi'] = 1
        with self.assertRaisesRegex(ValueError, 'payload'):
            assets.verify_kpm(self.image.read_bytes(), corrupted, payload)

    def test_external_kpm_payload_is_proved_and_embedded_with_its_native_control_bridge(self):
        _, _, payload, _ = self.prepare_kpm()
        path = self.pack()
        document = assets.read_kernel(path)
        self.assertTrue(document['features']['kpm'])
        self.assertTrue(document['features']['kpm_native_config'])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read(assets.PAYLOAD_MEMBER), payload)
        self.assertEqual(self.gather()['count'], 1)
        self.assertNotIn('kpimg', {item.name for item in self.inputs.iterdir()})

    def test_payload_without_the_native_kpm_control_bridge_cannot_be_published(self):
        self.prepare_kpm(native_bridge=False)
        with self.assertRaisesRegex(ValueError, 'enabled native KPM control bridge'):
            self.pack()

    def test_kpm_report_cannot_claim_unrelated_image_payload_or_layout(self):
        report_path, report, payload, cache = self.prepare_kpm()
        for key, value in [('post_image_sha256', 'b' * 64), ('pre_image_sha256', 'b' * 64),
                           ('layout', {}), ('release', {'tag': 'different'})]:
            changed = dict(report, **{key: value})
            report_path.write_text(json.dumps(changed))
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.pack()
        report_path.write_text(json.dumps(report))
        cache.joinpath('kpimg').write_bytes(payload + b'bad')
        with self.assertRaisesRegex(ValueError, 'payload'):
            self.pack()

    def test_actual_kpm_map_pac_changes_are_preserved_through_package_and_collect(self):
        _, report, _, _ = self.prepare_kpm(pac=True)
        self.assertEqual(len(report['original_windows']), 1)
        path = self.pack()
        self.assertTrue(assets.read_kernel(path)['features']['kpm'])
        self.assertEqual(self.gather()['count'], 1)
        with zipfile.ZipFile(path) as archive:
            metadata = json.loads(archive.read(assets.MEMBER))
            self.assertEqual(metadata['kpm_patch']['original_windows'], report['original_windows'])

    def test_kpm_image_without_report_and_corrupted_embedded_payload_are_rejected(self):
        report_path, _, _, _ = self.prepare_kpm()
        saved = report_path.read_bytes()
        report_path.unlink()
        with self.assertRaisesRegex(ValueError, 'without'):
            self.pack()
        report_path.write_bytes(saved)
        path = self.pack()
        rewrite_zip(path, change=lambda data: data.update({assets.PAYLOAD_MEMBER: data[assets.PAYLOAD_MEMBER] + b'bad'}))
        with self.assertRaisesRegex(ValueError, 'payload'):
            assets.read_kernel(path)


@unittest.skipUnless(SHELL, 'A local POSIX shell is required for inert installer stubs')
class OplusInstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT.parent / '.gki-upstream-cache' / ('oplus-installer-test-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.profile = {'platform': 'sm8650', 'kernel': '6.1.57'}
        (self.directory / 'Image').write_bytes(b'inert replacement kernel, not installable')
        (self.directory / 'stock-ramdisk.cpio').write_bytes(b'original ramdisk must remain byte-for-byte')
        (self.directory / 'tools').mkdir()
        (self.directory / 'partitions').mkdir()
        for partition in ('boot', 'boot_a', 'boot_b', 'init_boot_a', 'vendor_boot_a', 'vendor_kernel_boot_a', 'dtbo_a'):
            (self.directory / 'partitions' / partition).write_bytes(b'unchanged original ' + partition.encode())
        # Deliberately inert contracts, not the upstream runtime. No Android
        # devices or real block paths are ever read or written in these tests.
        (self.directory / 'tools/ak3-core.sh').write_text('''
abort() { printf 'abort %s\n' "$*" >> trace; exit 1; }
ui_print() { :; }
getprop() { case "$1" in ro.soc.model) printf '%s\n' "$TEST_SOC";; esac; }
uname() { printf '%s\n' "$TEST_RELEASE"; }
[ "$BLOCK" = boot ] || abort "unexpected partition selection";
[ "$IS_SLOT_DEVICE" = auto ] || abort "slot detection disabled";
BLOCK="partitions/boot${TEST_SLOT}";
split_boot() {
  printf 'split %s\n' "$BLOCK" >> trace;
  mkdir -p split_img;
  if [ "$TEST_RAMDISK" = present ]; then cp stock-ramdisk.cpio split_img/ramdisk.cpio; fi;
}
flash_boot() {
  case "$PATCH_VBMETA_FLAG" in
    auto|"") [ "$PATCHVBMETAFLAG" ] || PATCHVBMETAFLAG=false;;
    1) PATCHVBMETAFLAG=true;;
    *) PATCHVBMETAFLAG=false;;
  esac;
  [ "$PATCHVBMETAFLAG" = false ] || abort "AVB patching was enabled";
  [ ! -f ramdisk-new.cpio ] || abort "ramdisk was repacked";
  printf 'flash %s\n' "$BLOCK" >> trace;
  cp Image "$BLOCK.kernel";
  if [ -f split_img/ramdisk.cpio ]; then cp split_img/ramdisk.cpio flashed-ramdisk.cpio; fi;
}
dump_boot() { abort "dump_boot attempted ramdisk extraction"; }
write_boot() { abort "write_boot attempted ramdisk repack or extra partitions"; }
''', encoding='utf-8', newline='\n')
        (self.directory / 'anykernel.sh').write_bytes(assets.anykernel_script(
            self.profile, 'SukiSU', '6.1.57-android14-11'))

    def run_installer(self, *, soc='SM8650', release='6.1.57-android14-11-stock', slot='_a', ramdisk='absent'):
        trace = self.directory / 'trace'
        trace.unlink(missing_ok=True)
        environment = {**os.environ, 'TEST_SOC': soc, 'TEST_RELEASE': release,
                       'TEST_SLOT': slot, 'TEST_RAMDISK': ramdisk, 'PATCHVBMETAFLAG': 'true',
                       'PATH': str(Path(SHELL).parent) + os.pathsep + os.environ.get('PATH', '')}
        result = subprocess.run([SHELL, './anykernel.sh'], cwd=self.directory, env=environment,
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        return result, trace.read_text() if trace.exists() else ''

    def test_ramdisk_free_boot_uses_only_active_boot_with_original_avb_flags(self):
        result, trace = self.run_installer(ramdisk='absent')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('split partitions/boot_a\nflash partitions/boot_a\n', trace)
        self.assertFalse((self.directory / 'flashed-ramdisk.cpio').exists())
        self.assertEqual((self.directory / 'Image').read_bytes(),
                         (self.directory / 'partitions/boot_a.kernel').read_bytes())
        for partition in ('init_boot_a', 'vendor_boot_a', 'vendor_kernel_boot_a', 'dtbo_a'):
            self.assertEqual(b'unchanged original ' + partition.encode(),
                             (self.directory / 'partitions' / partition).read_bytes())
            self.assertFalse((self.directory / 'partitions' / (partition + '.kernel')).exists())

    def test_existing_ramdisk_is_preserved_and_slot_b_boot_is_used(self):
        result, trace = self.run_installer(ramdisk='present', slot='_b', soc='SM8650-AB')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('split partitions/boot_b\nflash partitions/boot_b\n', trace)
        self.assertEqual((self.directory / 'stock-ramdisk.cpio').read_bytes(),
                         (self.directory / 'flashed-ramdisk.cpio').read_bytes())
        self.assertFalse((self.directory / 'ramdisk-new.cpio').exists())

    def test_known_platform_and_kmi_allow_unslotted_boot(self):
        result, trace = self.run_installer(slot='', release='6.1.57-android14-11')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('split partitions/boot\nflash partitions/boot\n', trace)

    def test_wrong_unknown_soc_or_full_kernel_android_kmi_abort_before_boot_access(self):
        selections = [('SM8750', '6.1.57-android14-11-test'), ('SM86500', '6.1.57-android14-11-test'),
                      ('', '6.1.57-android14-11-test'), ('SM8650', '6.1.58-android14-11-test'),
                      ('SM8650', '6.1.57-android15-11-test'), ('SM8650', '6.1.57-android14-12-test')]
        for soc, release in selections:
            with self.subTest(soc=soc, release=release):
                result, trace = self.run_installer(soc=soc, release=release)
                self.assertNotEqual(0, result.returncode)
                self.assertTrue(trace.startswith('abort '), result.stdout + result.stderr)
                self.assertNotIn('split ', trace)
                self.assertNotIn('flash ', trace)

    def test_feature_summary_uses_chinese_states_without_json_quotes(self):
        self.assertEqual('开启', assets.feature_label(True))
        self.assertEqual('关闭', assets.feature_label(False))
        self.assertEqual('未检测', assets.feature_label(None))
        self.assertEqual('模块', assets.feature_label('模块'))
        self.assertEqual('thin', assets.feature_label('thin'))


if __name__ == '__main__':
    unittest.main()
