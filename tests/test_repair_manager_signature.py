"""Repair uses real RSA/APK-v2 identities; only the external signing SDK is mocked."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import sys
import unittest
from unittest.mock import patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import repair_manager_signature as repair
from apk_fixture import manager_metadata, signed_apk
from embedded_build_metadata import read_manager_apk


def add_legacy_v3_block(data):
    """Add an opaque legacy block while retaining the genuine RSA/v2 signature."""
    eocd = data.rfind(b'PK\x05\x06')
    offset = struct.unpack_from('<I', data, eocd + 16)[0]
    old_size = struct.unpack_from('<Q', data, offset - 24)[0]
    start = offset - old_size - 8
    payload = b'legacy scheme fixture; v2 cryptographic identity stays real'
    pair = struct.pack('<QI', 4 + len(payload), 0xF05368C0) + payload
    size = old_size + len(pair)
    block = struct.pack('<Q', size) + data[start + 8:offset - 24] + pair + struct.pack('<Q', size) + b'APK Sig Block 42'
    end = bytearray(data[eocd:])
    struct.pack_into('<I', end, 16, offset + len(pair))
    return data[:start] + block + data[offset:eocd] + end


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.repair-signature-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.name = 'SukiSU_manager-ci_41065-release.apk'
        self.identity = manager_metadata(file=self.name, ksu_version_name='manager-ci', ksu_version_code=41065,
                                         ksu_branch='Latest(最新)', package=repair.LEGACY_PACKAGE)
        self.input = self.root / self.name
        self.original = add_legacy_v3_block(signed_apk(self.identity))
        self.input.write_bytes(self.original)
        self.digest = hashlib.sha256(self.original).hexdigest()
        self.output = self.root / 'output'
        environment = {'GITHUB_REPOSITORY': 'cyzrqh/gki', 'GITHUB_RUN_ID': '999',
                       'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_SHA': '9' * 40,
                       'KSU_REF': self.identity['ksu_ref'], 'KSU_BRANCH': self.identity['ksu_branch']}
        self.env = patch.dict(os.environ, environment)
        self.env.start(); self.addCleanup(self.env.stop)
        self.cert = patch.multiple(repair, CERTIFICATE_SHA256=self.identity['manager_cert_sha256'],
                                   CERTIFICATE_SIZE=int(self.identity['manager_cert_size']))
        self.cert.start(); self.addCleanup(self.cert.stop)
        self.android = {'manager_package': self.identity['package'], 'manager_version_code': 41065,
                        'manager_version_name': 'manager-ci'}
        self.measure = patch.object(repair, 'measure_apk', return_value=self.android)
        self.measure.start(); self.addCleanup(self.measure.stop)
        self.signer = patch.object(repair.signing, 'sign_manager', side_effect=self.sign_sdk)
        self.mock_signer = self.signer.start(); self.addCleanup(self.signer.stop)
        self.sign_mode = 'valid'

    def sign_sdk(self, unsigned, *, output_dir, build_metadata, legacy_41065=False):
        self.assertTrue(legacy_41065)
        metadata = json.loads(build_metadata.read_text())
        self.assertEqual(metadata['source_apk_filename'], self.name.removesuffix('.apk') + '-unsigned.apk')
        self.assertEqual(repair.signing.apk_filename(build_metadata, self.android), self.name)
        with zipfile.ZipFile(unsigned) as archive:
            self.assertNotIn(repair.APK_MEMBER, archive.namelist())
            self.assertEqual(archive.read('classes.dex'), b'APK dex fixture')
        self.assertNotIn(b'APK Sig Block 42', unsigned.read_bytes())
        identity = {**self.identity, 'producer_run_id': os.environ['GITHUB_RUN_ID'],
                    'producer_run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
                    'producer_head_sha': os.environ['GITHUB_SHA']}
        if self.sign_mode == 'wrong-source': identity['ksu_ref'] = '8' * 40
        raw = signed_apk(identity)
        if self.sign_mode == 'legacy-output': raw = add_legacy_v3_block(raw)
        output_dir.mkdir()
        (output_dir / self.name).write_bytes(raw)
        return {**self.android, 'manager_cert_size': int(identity['manager_cert_size']),
                'manager_cert_sha256': identity['manager_cert_sha256'], 'apk_filename': self.name,
                'apk_sha256': hashlib.sha256(raw).hexdigest()}

    def run_repair(self, **changes):
        return repair.repair(self.input, changes.get('expected_sha256', self.digest),
                             changes.get('expected_ref', self.identity['ksu_ref']), self.output)

    def test_real_legacy_input_is_repaired_to_strict_same_source_certificate_and_payload(self):
        with self.assertRaises(ValueError): read_manager_apk(self.input)
        result = self.run_repair()
        verified = read_manager_apk(self.output / self.name)
        self.assertEqual(verified['ksu_ref'], self.identity['ksu_ref'])
        self.assertEqual(verified['manager_cert_sha256'], self.identity['manager_cert_sha256'])
        self.assertEqual(verified['producer_run_id'], '999')
        self.assertEqual(verified['producer_head_sha'], '9' * 40)
        self.assertEqual(verified['apk_sha256'], result['apk_sha256'])
        self.assertEqual(repair.payload_inventory(self.input), repair.payload_inventory(self.output / self.name))
        self.assertEqual(list(self.output.iterdir()), [self.output / self.name])
        self.assertFalse(list(self.root.glob('.gki-manager-repair-*')))
        self.assertEqual(self.input.read_bytes(), self.original)

    def test_source_hash_ref_and_identity_checks_happen_before_signing(self):
        for values in ({'expected_sha256': 'f' * 64}, {'expected_ref': 'e' * 40},
                       {'expected_sha256': 'moving-name'}, {'expected_ref': 'main'}):
            with self.subTest(values=values), self.assertRaises(ValueError): self.run_repair(**values)
        self.mock_signer.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_temporary_download_alias_keeps_signed_native_output_filename(self):
        self.input = self.input.rename(self.root / 'downloaded-manager.apk')
        self.assertEqual(self.run_repair()['apk_filename'], self.name)
        self.assertEqual(list(self.output.iterdir()), [self.output / self.name])

    def test_fixed_production_certificate_is_enforced_before_signing(self):
        with patch.object(repair, 'CERTIFICATE_SHA256', 'f' * 64):
            with self.assertRaisesRegex(ValueError, 'certificate'): self.run_repair()
        self.mock_signer.assert_not_called()

    def test_valid_hash_cannot_bypass_original_rsa_content_verification(self):
        modified = self.original.replace(b'APK dex fixture', b'APK bad fixture', 1)
        self.input.write_bytes(modified)
        with self.assertRaises(ValueError):
            self.run_repair(expected_sha256=hashlib.sha256(modified).hexdigest())
        self.mock_signer.assert_not_called()

    def test_original_aapt_metadata_must_match_signed_embedded_identity(self):
        with patch.object(repair, 'measure_apk', return_value={**self.android, 'manager_version_code': 41064}):
            with self.assertRaisesRegex(ValueError, 'Actual original APK'): self.run_repair()
        self.mock_signer.assert_not_called()

    def test_new_legacy_signature_or_changed_identity_never_enters_public_output(self):
        for mode in ('legacy-output', 'wrong-source'):
            self.sign_mode = mode
            with self.subTest(mode=mode), self.assertRaises(ValueError): self.run_repair()
            self.assertFalse(self.output.exists())
            self.assertFalse(list(self.root.glob('.gki-manager-repair-*')))

    def test_repair_uses_frozen_input_bytes_after_hash_check(self):
        def measure(path):
            self.assertEqual(path.read_bytes(), self.original)
            self.input.write_bytes(b'input path replaced while SDK is measuring')
            return self.android
        with patch.object(repair, 'measure_apk', side_effect=measure): self.run_repair()
        self.assertEqual(read_manager_apk(self.output / self.name)['ksu_ref'], self.identity['ksu_ref'])

    def test_conflicting_workflow_source_or_nonempty_output_is_refused(self):
        with patch.dict(os.environ, {'KSU_REF': 'c' * 40}):
            with self.assertRaisesRegex(ValueError, 'different original'): self.run_repair()
        self.output.mkdir(); existing = self.output / 'keep.txt'; existing.write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'empty unlinked'): self.run_repair()
        self.assertEqual(existing.read_text(), 'preserve')

    def test_unsigned_copy_strips_only_old_identity_and_top_level_jar_signatures(self):
        source, output = self.root / 'payload.zip', self.root / 'unsigned.zip'
        retained = ('classes.dex', 'META-INF/services/provider', 'META-INF/nested/KEEP.RSA', 'assets/KEEP.RSA')
        stripped = (repair.APK_MEMBER, 'META-INF/MANIFEST.MF', 'META-INF/CERT.SF',
                    'META-INF/CERT.RSA', 'META-INF/CERT.DSA', 'meta-inf/cert.ec')
        with zipfile.ZipFile(source, 'w') as archive:
            archive.comment = b'original ZIP comment'
            for name in retained + stripped:
                info = zipfile.ZipInfo(name, (2026, 10, 24, 12, 0, 0)); info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o640) << 16; info.compress_type = zipfile.ZIP_DEFLATED
                info.comment = b'entry comment'; info.extra = b'\xfe\xca\x00\x00'
                archive.writestr(info, b'payload of ' + name.encode())
        repair.unsigned_copy(source, output)
        with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
            self.assertEqual(set(after.namelist()), set(retained))
            self.assertEqual(after.comment, before.comment)
            for name in retained:
                self.assertEqual(after.read(name), before.read(name))
                for attr in ('date_time', 'create_system', 'external_attr', 'compress_type', 'comment', 'extra'):
                    self.assertEqual(getattr(after.getinfo(name), attr), getattr(before.getinfo(name), attr))

    def test_unsigned_copy_rejects_links_and_unsafe_members(self):
        for name, mode in (('../outside', stat.S_IFREG), ('linked', stat.S_IFLNK)):
            source = self.root / 'unsafe.zip'
            with zipfile.ZipFile(source, 'w') as archive:
                item = zipfile.ZipInfo(name); item.create_system = 3; item.external_attr = (mode | 0o644) << 16
                archive.writestr(item, b'unsafe payload')
            with self.subTest(name=name), self.assertRaises(ValueError):
                repair.unsigned_copy(source, self.root / 'unsafe-output.zip')

    def test_cli_failure_is_nonzero_and_does_not_print_sensitive_exception(self):
        stderr = io.StringIO()
        with patch.object(repair, 'repair', side_effect=ValueError('private-keystore-material')):
            with contextlib.redirect_stderr(stderr):
                result = repair.main(['--input', str(self.input), '--expected-sha256', self.digest,
                                      '--expected-ref', self.identity['ksu_ref'], '--output-dir', str(self.output)])
        self.assertEqual(result, 1)
        self.assertNotIn('private-keystore-material', stderr.getvalue())


if __name__ == '__main__': unittest.main()
