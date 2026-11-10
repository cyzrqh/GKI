from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from apk_fixture import manager_metadata, signed_apk
from embedded_build_metadata import official_receipt_from_environment, read_manager_apk
from manager_lkm_jobs import KMIS
from reusable_manager_apk import (cache_key, main, manager_selection, native_proof, recipe_hash,
                                  stage, validate_adoption)
from sign_sukisu_manager import SigningError, sign_manager
from test_additional_managers import manifest


class ReusableManagerApkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache, self.output = self.root / 'cache', self.root / 'output'
        self.cache.mkdir()
        self.source_identity = {'ref': 'b' * 40, 'git_tree': '1' * 40,
                                'git_count': 512, 'git_describe': 'v4.2.0'}
        self.identity = manager_metadata(ksu_branch='Stable(标准)', manager_recipe_sha256='c' * 64,
                                         manager_source_identity=self.source_identity,
                                         manager_native_proof={'schema_version': 1, 'supported_kmis': sorted(KMIS),
                                                               'lkm_report_sha256': 'd' * 64,
                                                               'boot_report_sha256': 'e' * 64})
        self.consumer = {'repository': 'cyzrqh/gki', 'run_id': 456, 'run_attempt': 2, 'head_sha': 'f' * 40}
        self.selection = manager_selection('SukiSU', 'Latest(最新)', self.identity['ksu_ref'],
                                           self.identity['manager_cert_size'], self.identity['manager_cert_sha256'],
                                           self.identity['manager_recipe_sha256'], self.source_identity)

    def cache_apk(self, identity=None, *, android_package='com.sukisu.ultra', android_version='4.2.0'):
        identity = self.identity if identity is None else identity
        package = manifest(android_package, code=40922, version=android_version)
        data = signed_apk(identity, manifest_bytes=package)
        (self.cache / identity['file']).write_bytes(data)
        return data

    def test_cross_channel_hit_preserves_signed_apk_and_original_producer(self):
        original = self.cache_apk()
        result = stage(self.cache, self.output, consumer=self.consumer, selection=self.selection)
        self.assertEqual(result['manager_apk_reused'], 'true')
        self.assertEqual(result['apk_sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(result['apk_filename'], self.identity['file'])
        self.assertEqual((self.output / result['apk_filename']).read_bytes(), original)
        receipt = json.loads(result['manager_adoption'])
        signed = read_manager_apk(original, filename=self.identity['file'])
        self.assertEqual(receipt['producer'], signed)
        accepted = validate_adoption(receipt, signed, consumer=self.consumer, selection=self.selection)
        self.assertEqual(accepted['producer_run_id'], 456)
        self.assertEqual(accepted['producer_run_attempt'], 2)
        self.assertEqual(accepted['ksu_branch'], 'Latest(最新)')
        self.assertEqual(signed['producer_run_id'], '123')
        self.assertEqual(signed['ksu_branch'], 'Stable(标准)')
        for key, replacement in (('consumer', {**self.consumer, 'run_attempt': 1}),
                                 ('producer', {**signed, 'apk_sha256': '0' * 64}),
                                 ('selection', {**self.selection, 'ksu_branch': 'Stable(标准)'})):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'adoption'):
                validate_adoption({**receipt, key: replacement}, signed, consumer=self.consumer,
                                  selection=self.selection)
        incomplete = {key: value for key, value in self.selection.items() if key != 'ksu_ref'}
        with self.assertRaisesRegex(ValueError, 'selection fields'):
            validate_adoption({**receipt, 'selection': incomplete}, signed, consumer=self.consumer,
                              selection=incomplete)

    def test_wrong_signer_source_recipe_or_native_proof_cannot_reuse(self):
        self.cache_apk()
        for key, value in (('manager_recipe_sha256', '1' * 64),
                           ('manager_cert_sha256', '2' * 64), ('manager_cert_size', 1024)):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'differs'):
                stage(self.cache, self.output, consumer=self.consumer, selection={**self.selection, key: value})
        other_ref = '1' * 40
        with self.assertRaisesRegex(ValueError, 'differs'):
            stage(self.cache, self.output, consumer=self.consumer, selection={
                **self.selection, 'ksu_ref': other_ref,
                'manager_source_identity': {**self.source_identity, 'ref': other_ref}})
        without_proof = {key: value for key, value in self.identity.items() if key != 'manager_native_proof'}
        self.cache_apk(without_proof)
        with self.assertRaisesRegex(ValueError, 'native build proof'):
            stage(self.cache, self.output, consumer=self.consumer, selection=self.selection)
        self.assertFalse(self.output.exists())

    def test_current_run_consumers_recheck_cached_bytes_and_preserve_original_run(self):
        original = self.cache_apk()
        adopted = stage(self.cache, self.output, consumer=self.consumer, selection=self.selection)
        environment = {'GITHUB_REPOSITORY': self.consumer['repository'], 'GITHUB_SHA': self.consumer['head_sha'],
                       'GITHUB_RUN_ID': str(self.consumer['run_id']), 'GITHUB_RUN_ATTEMPT': '3',
                       'GKI_MANAGER_METADATA': adopted['manager_adoption']}
        with patch.dict('os.environ', environment):
            actual = read_manager_apk(original, filename=self.identity['file'],
                                      external_metadata=official_receipt_from_environment())
        self.assertEqual(actual['ksu_branch'], self.selection['ksu_branch'])
        self.assertEqual(actual['producer_run_attempt'], 2)
        self.assertEqual(actual['original_apk_producer']['producer_run_id'], '123')
        with patch.dict('os.environ', {**environment, 'GITHUB_RUN_ID': '457'}):
            with self.assertRaisesRegex(ValueError, 'another workflow'):
                official_receipt_from_environment()

    def test_manifest_mismatch_and_modified_signed_bytes_fail_closed(self):
        data = self.cache_apk(android_version='4.2.1')
        with self.assertRaisesRegex(ValueError, 'manifest differs'):
            stage(self.cache, self.output, consumer=self.consumer, selection=self.selection)
        modified = data.replace(b'APK dex fixture', b'APK dex altered', 1)
        (self.cache / self.identity['file']).write_bytes(modified)
        with self.assertRaisesRegex(ValueError, 'v2 content digest'):
            stage(self.cache, self.output, consumer=self.consumer, selection=self.selection)
        self.assertFalse(self.output.exists())

    def test_cache_identity_tracks_recipe_and_exact_commit_but_excludes_channel(self):
        key = cache_key(self.selection)
        self.assertEqual(key, cache_key({**self.selection, 'ksu_branch': 'Stable(标准)'}))
        self.assertNotEqual(key, cache_key({**self.selection, 'ksu_ref': '0' * 40}))
        self.assertNotEqual(key, cache_key({**self.selection, 'manager_recipe_sha256': '0' * 64}))
        self.assertRegex(recipe_hash(ROOT), r'^[0-9a-f]{64}$')

    def test_new_tag_at_same_head_invalidates_earlier_apk_but_susfs_selection_does_not(self):
        self.cache_apk()
        new_tag = {**self.source_identity, 'git_describe': 'v4.2.1'}
        changed = {**self.selection, 'manager_source_identity': new_tag}
        self.assertNotEqual(cache_key(self.selection), cache_key(changed))
        with self.assertRaisesRegex(ValueError, 'differs'):
            stage(self.cache, self.output, consumer=self.consumer, selection=changed)
        enabled_susfs = manager_selection('SukiSU', 'Stable(标准)', self.selection['ksu_ref'],
                                         self.selection['manager_cert_size'], self.selection['manager_cert_sha256'],
                                         self.selection['manager_recipe_sha256'],
                                         {**self.source_identity, 'susfs_sources': {'synthetic': 'selection'}})
        self.assertEqual(cache_key(self.selection), cache_key(enabled_susfs))

    def test_cli_missing_cache_signals_cold_build(self):
        output = self.root / 'github-output'
        with patch.dict('os.environ', {'GITHUB_REPOSITORY': 'cyzrqh/gki', 'GITHUB_SHA': 'f' * 40,
                                      'GITHUB_RUN_ID': '456', 'GITHUB_RUN_ATTEMPT': '2',
                                      'GITHUB_OUTPUT': str(output)}):
            result = main(['stage', '--root', str(ROOT), '--variant', 'SukiSU', '--channel', 'Latest(最新)',
                           '--ksu-ref', self.selection['ksu_ref'], '--cert-size', str(self.selection['manager_cert_size']),
                           '--cert-sha256', self.selection['manager_cert_sha256'],
                           '--source-identity', json.dumps(self.source_identity), '--cache-dir', str(self.cache),
                           '--output-dir', str(self.output)])
        self.assertEqual(result, 0)
        self.assertIn('manager_apk_reused=false\n', output.read_text())
        self.assertNotIn('manager_adoption=', output.read_text())
        self.assertFalse(self.output.exists())

    def test_native_attestation_binds_all_boot_results_to_verified_lkm_records(self):
        common = {'schema_version': 1, 'repository': 'SukiSU-Ultra/SukiSU-Ultra',
                  'ksu_ref': self.identity['ksu_ref'], 'git_tree': '1' * 40, 'git_count': 512,
                  'git_describe': 'v4.2.0', 'ksu_version_code': 40922}
        public = {'manager_package': self.identity['package'], 'manager_cert_size': self.selection['manager_cert_size'],
                  'manager_cert_sha256': self.selection['manager_cert_sha256']}
        lkm = {**common, **public, 'mode': 'native-lkm', 'supported_kmis': sorted(KMIS),
               'modules': [{**common, **public, 'kmi': kmi, 'arch': 'aarch64',
                            'file': kmi + '_kernelsu.ko', 'sha256': '2' * 64} for kmi in KMIS]}
        boot = {**common, 'verification': 'official-native-cli-synthetic-image-patch-and-restore',
                'synthetic_fixture': True, 'fixture_bootable': False, 'device_boot_test': False,
                'supported_kmis': ['aarch64/' + kmi for kmi in sorted(KMIS)],
                'fixture_sha256': '3' * 64, 'host_ksud_sha256': '4' * 64,
                'results': [{'kmi': kmi, 'module_sha256': '2' * 64, 'image_sha256': '5' * 64,
                             'restored_sha256': '6' * 64, 'ksuinit_sha256': '7' * 64,
                             'original_init_sha256': '8' * 64} for kmi in KMIS]}
        lkm_path, boot_path = self.root / 'lkm.json', self.root / 'boot.json'
        lkm_path.write_text(json.dumps(lkm), encoding='utf-8')
        boot_path.write_text(json.dumps(boot), encoding='utf-8')
        identity = {**self.identity, 'manager_cert_size': self.selection['manager_cert_size']}
        proof = native_proof(lkm_path, boot_path, identity)
        self.assertEqual(proof['boot_report_sha256'], hashlib.sha256(boot_path.read_bytes()).hexdigest())
        self.assertEqual(proof['lkm_report_sha256'], hashlib.sha256(lkm_path.read_bytes()).hexdigest())
        self.assertEqual(proof['supported_kmis'], sorted(KMIS))
        with self.assertRaisesRegex(ValueError, 'selected source'):
            native_proof(lkm_path, boot_path, {**identity, 'manager_source_identity': {
                **self.source_identity, 'git_describe': 'v4.2.1'}})
        different_module = deepcopy(boot)
        different_module['results'][0]['module_sha256'] = '9' * 64
        boot_path.write_text(json.dumps(different_module), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'staged native module'):
            native_proof(lkm_path, boot_path, identity)

    def test_signer_rejects_partial_attestation_before_opening_private_key(self):
        with patch('sign_sukisu_manager.signing_material') as material:
            with self.assertRaisesRegex(SigningError, 'both native verification reports'):
                sign_manager(self.root / 'unsigned.apk', self.root / 'signed.apk',
                             manager_recipe_sha256=self.selection['manager_recipe_sha256'])
        material.assert_not_called()


if __name__ == '__main__':
    unittest.main()
