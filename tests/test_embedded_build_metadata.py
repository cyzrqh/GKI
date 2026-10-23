import hashlib
import io
from pathlib import Path
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import embedded_build_metadata as embedded
from apk_fixture import manager_metadata, material, signed_apk


class EmbeddedBuildMetadataTests(unittest.TestCase):
    def test_actual_rsa_signature_certificate_and_content_digest_are_verified(self):
        data = signed_apk()
        metadata = embedded.read_manager_apk(data)
        self.assertEqual(metadata['apk_sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(metadata['manager_cert_sha256'], hashlib.sha256(material()[1]).hexdigest())
        self.assertEqual(metadata['ksu_ref'], 'b' * 40)

    def test_changed_payload_and_signature_are_rejected(self):
        data = signed_apk()
        changed = data.replace(b'APK daemon fixture', b'BAD daemon fixture', 1)
        with self.assertRaisesRegex(ValueError, 'content digest'):
            embedded.read_manager_apk(changed)
        data = bytearray(data)
        # Change a byte in the signing block without touching ZIP payloads.
        start = data.index(b'APK Sig Block 42')
        data[start - 40] ^= 1
        with self.assertRaises(ValueError):
            embedded.read_manager_apk(bytes(data))

    def test_signed_identity_must_match_real_certificate_and_final_filename(self):
        data = signed_apk(manager_metadata(manager_cert_sha256='0' * 64))
        with self.assertRaisesRegex(ValueError, 'actual APK v2 signer'):
            embedded.read_manager_apk(data)
        with self.assertRaisesRegex(ValueError, 'filename'):
            embedded.read_manager_apk(signed_apk(), filename='renamed.apk')

    def test_self_digest_and_duplicate_metadata_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'self-referential'):
            embedded.read_manager_apk(signed_apk(manager_metadata(apk_sha256='0' * 64)))
        with self.assertRaisesRegex(ValueError, 'Duplicate embedded'):
            embedded.read_manager_apk(signed_apk(raw_metadata=b'{"schema_version":1,"schema_version":1}'))

    def test_unsafe_member_is_rejected_even_with_a_valid_rsa_signature(self):
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            embedded.read_manager_apk(signed_apk(extra=True))

    def test_unsigned_or_metadata_only_zip_is_not_an_authenticated_apk(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(embedded.APK_MEMBER, '{}')
        with self.assertRaises(ValueError):
            embedded.read_manager_apk(stream.getvalue())


if __name__ == '__main__':
    unittest.main()
