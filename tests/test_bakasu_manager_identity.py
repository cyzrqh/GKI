"""BakaSU joins the authenticated manager/AK3 path, without SukiSU crossover."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import embedded_build_metadata as embedded
import capture_manager_metadata as capture
import manager_variants as variants
import package_release_kernel as packaging
import prepare_release as release
import sign_sukisu_manager as signing
import set_manager_signature as signature
import wait_manager_identity as identity
from apk_fixture import manager_metadata, signed_apk
from kernel_fixture import kernel_fixture, ak3_bytes


def baka_metadata(**changes):
    return manager_metadata(ksu_variant='BakaSU', package='com.resukisu.resukisu',
                            file='BakaSU_v3.1.6_40922-arm64-v8a-release.apk', **changes)


class BakaManagerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / 'tests' / ('.baka-identity-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)

    def test_profiles_are_allowlisted_independent_and_return_copies(self):
        self.assertEqual('SukiSU-Ultra/SukiSU-Ultra', variants.manager_profile()['repository'])
        self.assertEqual('com.sukisu.ultra', variants.manager_profile()['package'])
        baka = variants.manager_profile('BakaSU')
        self.assertEqual('Baka-SU/BakaSU', baka['repository'])
        self.assertEqual('com.resukisu.resukisu', baka['package'])
        baka['repository'] = 'untrusted/other'
        self.assertEqual('Baka-SU/BakaSU', variants.manager_profile('BakaSU')['repository'])
        for value in ('Official', '', None, 'bakasu', 'BakaSU\n', ['BakaSU']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                variants.manager_profile(value)

    def test_native_stable_and_development_filenames_keep_their_original_names(self):
        actual = {'manager_package': 'com.resukisu.resukisu',
                  'manager_version_name': 'v3.1.6', 'manager_version_code': 40922}
        for name in ('BakaSU_v3.1.6_40922-arm64-v8a-release.apk',
                     'ReSukiSU_v3.1.6_40922-release.apk'):
            path = self.directory / 'build.json'
            path.write_text(json.dumps({**actual, 'apk_filename': name,
                                         'source_apk_filename': name.removesuffix('.apk') + '-unsigned.apk'}))
            self.assertEqual(name, signing.apk_filename(path, actual, variant='BakaSU'))
            with self.assertRaises(signing.SigningError):
                signing.apk_filename(path, actual, variant='SukiSU')
        for name in ('../BakaSU_v3_40922-release.apk', 'BakaSU_v3_40922-release.apk\n',
                     'SukiSU_v3_40922-release.apk', 'BakaSU_v3_40922-release.zip'):
            self.assertFalse(variants.valid_manager_apk_filename(name, 'BakaSU'))

    def test_genuine_v2_signature_and_variant_are_required_for_the_selected_run(self):
        document = baka_metadata()
        raw = signed_apk(document)
        manager = embedded.read_manager_apk(raw, filename=document['file'])
        expected = identity.Expected('cyzrqh/gki', 123, 1, 'a' * 40, 'b' * 40, variant='BakaSU')
        expected.validate()
        public = identity.validate_metadata(manager, expected)
        self.assertEqual('com.resukisu.resukisu', public['MANAGER_PACKAGE'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), public['MANAGER_APK_SHA256'])
        with self.assertRaises(identity.IdentityError):
            identity.validate_metadata(manager, identity.Expected('cyzrqh/gki', 123, 1, 'a' * 40, 'b' * 40))
        with self.assertRaises(identity.IdentityError):
            identity.validate_metadata(embedded.read_manager_apk(signed_apk()), expected)
        for field, value in (('ksu_ref', 'c' * 40), ('producer_head_sha', 'c' * 40),
                             ('producer_repository', 'other/gki'), ('producer_run_id', '124')):
            with self.subTest(field=field), self.assertRaises(identity.IdentityError):
                identity.validate_metadata({**manager, field: value}, expected)

    def test_identity_cli_selects_the_matching_variant_producer_job(self):
        output = self.directory / 'github-env'
        for variant in ('SukiSU', 'BakaSU'):
            args = ['identity', '--expected-ref', 'b' * 40, '--repo', 'cyzrqh/gki',
                    '--run-id', '123', '--run-attempt', '1', '--head-sha', 'a' * 40,
                    '--variant', variant, '--github-env', str(output)]
            with self.subTest(variant=variant), patch.object(sys, 'argv', args), \
                    patch.object(identity, 'wait_for_identity', return_value={'MANAGER_PACKAGE': 'com.example.manager'}) as wait, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, identity.main())
                expected = wait.call_args.args[0]
                self.assertEqual(variant, expected.variant)
                self.assertEqual(f'编译配套 {variant} 管理器 APK', expected.manager_job)

    def test_baka_capture_does_not_reuse_the_sukisu_susfs_compatibility_claim(self):
        metadata = baka_metadata()
        environment = {'KSU_VARIANT': 'BakaSU', 'META_ENABLE_SUSFS': 'true',
                       'META_FILE': metadata['file'], 'META_PACKAGE': metadata['package'],
                       'META_KSU_VERSION_CODE': metadata['ksu_version_code'],
                       'META_KSU_VERSION_NAME': metadata['ksu_version_name'],
                       'META_KSU_REF': metadata['ksu_ref'], 'META_KSU_BRANCH': metadata['ksu_branch'],
                       'META_MANAGER_CERT_SIZE': metadata['manager_cert_size'],
                       'META_MANAGER_CERT_SHA256': metadata['manager_cert_sha256'],
                       'META_APK_SHA256': 'd' * 64,
                       'GITHUB_REPOSITORY': 'cyzrqh/gki', 'GITHUB_RUN_ID': '123',
                       'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40}
        output = self.directory / 'manager.json'
        with patch.dict(os.environ, environment), patch.object(capture, 'load_profile') as profile, \
                patch.object(sys, 'argv', ['capture', '--output', str(output)]):
            capture.main()
            profile.assert_not_called()
        result = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual('BakaSU', result['ksu_variant'])
        self.assertIsNone(result['susfs_version'])

    def test_foreign_filename_variant_or_wrong_native_version_is_rejected(self):
        for changes in ({'file': 'SukiSU_v3.1.6_40922-release.apk'},
                        {'file': 'SukiSU_disguised-release.apk'}, {'file': 'Manager.apk'},
                        {'file': 'BakaSU_v3.1.6_40923-arm64-v8a-release.apk'},
                        {'ksu_variant': 'Official'}):
            document = baka_metadata()
            document.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                embedded.read_manager_apk(signed_apk(document))

    def test_raw_baka_ak3_requires_its_actual_signed_manager_and_image_identity(self):
        manager = embedded.read_manager_apk(signed_apk(baka_metadata()))
        document, image = kernel_fixture(manager)
        document['ksu_variant'] = 'BakaSU'
        verified = packaging.read_kernel_ak3(ak3_bytes(document, image))
        release.validate_bindings(manager, [(verified, True)], manager_verified=True)
        with self.assertRaisesRegex(ValueError, 'verified signed manager'):
            release.validate_bindings(None, [(verified, True)], manager_verified=False)
        foreign = embedded.read_manager_apk(signed_apk())
        with self.assertRaisesRegex(ValueError, 'different KernelSU variants'):
            release.validate_bindings(foreign, [(verified, True)], manager_verified=True)
        for foreign_variant in ('SukiSU', 'Official', 'None'):
            with self.subTest(foreign_variant=foreign_variant), self.assertRaisesRegex(ValueError, 'different KernelSU variants'):
                release.validate_bindings(manager, [({**verified, 'ksu_variant': foreign_variant}, True)], manager_verified=True)
        for changes in ({'manager_apk_sha256': 'f' * 64}, {'ksu_ref': 'f' * 40}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                release.validate_bindings(manager, [({**verified, **changes}, True)], manager_verified=True)
        with self.assertRaisesRegex(ValueError, 'manager trust identity'):
            packaging.read_kernel_ak3(ak3_bytes({**document, 'manager_cert_sha256': 'f' * 64}, image))
        without_support, unsupported_image = kernel_fixture({**manager, 'ksu_variant': 'SukiSU'})
        without_support['ksu_variant'] = 'BakaSU'
        with self.assertRaisesRegex(ValueError, 'multi-manager support'):
            packaging.read_kernel_ak3(ak3_bytes(without_support, unsupported_image))

    def test_real_baka_kbuild_accepts_cert_package_and_version_atomically_and_idempotently(self):
        fixture = ROOT / 'tests/fixtures/bakasu-manager'
        provenance = json.loads((fixture / 'provenance.json').read_text())
        raw = (fixture / 'Kbuild').read_bytes()
        self.assertEqual(provenance['files']['Kbuild']['sha256'], hashlib.sha256(raw).hexdigest())
        path = self.directory / 'KernelSU/kernel/Kbuild'
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        signature.configure(self.directory, '730', 'a' * 64, 'com.resukisu.resukisu', '40922')
        result = path.read_text()
        self.assertIn('KSU_EXPECTED_SIZE := 730\nifdef KSU_EXPECTED_SIZE', result)
        self.assertIn('KSU_EXPECTED_HASH := ' + 'a' * 64 + '\nifdef KSU_EXPECTED_HASH', result)
        self.assertIn('KSU_MANAGER_PACKAGE := com.resukisu.resukisu', result)
        self.assertIn('KSU_VERSION := 40922', result)
        self.assertIn('ifdef CONFIG_KSU_MULTI_MANAGER_SUPPORT', result)
        first = path.read_bytes()
        signature.configure(self.directory, '730', 'a' * 64, 'com.resukisu.resukisu', '40922')
        self.assertEqual(first, path.read_bytes())
        for old, new in (('ccflags-y += -DEXPECTED_SIZE=', 'ccflags-y += -DCHANGED_SIZE='),
                         ('ifdef KSU_EXPECTED_HASH', 'ifdef FUTURE_EXPECTED_HASH'),
                         ('ifdef KSU_MANAGER_PACKAGE', 'ifdef FUTURE_MANAGER_PACKAGE')):
            broken = raw.decode().replace(old, new)
            path.write_text(broken)
            before = path.read_bytes()
            with self.subTest(changed=old), self.assertRaises(ValueError):
                signature.configure(self.directory, '730', 'a' * 64, 'com.resukisu.resukisu', '40922')
            self.assertEqual(before, path.read_bytes())

    def test_reviewed_resukisu_named_tag_keeps_its_native_name_and_accepts_the_certificate(self):
        fixture = ROOT / 'tests/fixtures/bakasu-manager'
        provenance = json.loads((fixture / 'provenance.json').read_text())
        raw = (fixture / 'Kbuild-rc3').read_bytes()
        self.assertEqual(provenance['files']['Kbuild-rc3']['sha256'], hashlib.sha256(raw).hexdigest())
        path = self.directory / 'KernelSU/kernel/Kbuild'
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        signature.configure(self.directory, '730', 'a' * 64, 'com.resukisu.resukisu', '40922')
        result = path.read_text()
        self.assertIn('REPO_NAME := ReSukiSU', result)
        self.assertIn('KSU_EXPECTED_SIZE := 730', result)
        self.assertIn('KSU_EXPECTED_HASH := ' + 'a' * 64, result)
        self.assertIn('KSU_MANAGER_PACKAGE := com.resukisu.resukisu', result)
        self.assertIn('KSU_VERSION := 40922', result)
        first = path.read_bytes()
        signature.configure(self.directory, '730', 'a' * 64, 'com.resukisu.resukisu', '40922')
        self.assertEqual(first, path.read_bytes())

    def test_preparation_preserves_raw_baka_apk_and_ak3_and_uses_baka_title(self):
        source = self.directory / 'input'
        source.mkdir()
        metadata = baka_metadata()
        apk = signed_apk(metadata)
        manager = embedded.read_manager_apk(apk)
        document, image = kernel_fixture(manager)
        document['ksu_variant'] = 'BakaSU'
        ak3 = ak3_bytes(document, image)
        (source / metadata['file']).write_bytes(apk)
        (source / document['file']).write_bytes(ak3)
        output = self.directory / 'output'
        result = release.prepare(source, output, expected_kernel_count=1, report_kind='actions')
        self.assertEqual({'assets': 2, 'kernels': 1, 'manager': True}, result)
        self.assertEqual(apk, (output / 'assets' / metadata['file']).read_bytes())
        self.assertEqual(ak3, (output / 'assets' / document['file']).read_bytes())
        self.assertTrue((output / 'title.txt').read_text(encoding='utf-8').startswith('GKI 构建结果 · BakaSU '))


if __name__ == '__main__':
    unittest.main()
