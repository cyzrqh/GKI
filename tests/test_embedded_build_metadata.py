import hashlib
import io
from pathlib import Path
import sys
import struct
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import embedded_build_metadata as embedded
from apk_fixture import lp, manager_metadata, material, signed_apk


def block_layout(data):
    """Independent fixture writer: v2 excludes the signing block from its digest."""
    eocd = data.rfind(b'PK\x05\x06')
    directory = struct.unpack_from('<I', data, eocd + 16)[0]
    assert data[directory - 16:directory] == b'APK Sig Block 42'
    size = struct.unpack_from('<Q', data, directory - 24)[0]
    start = directory - size - 8
    position, pairs = start + 8, []
    while position < directory - 24:
        length, identity = struct.unpack_from('<QI', data, position)
        pairs.append((identity, data[position + 12:position + 8 + length]))
        position += 8 + length
    return start, directory, eocd, pairs


def rewrite_block(data, pairs, *, trailing=b''):
    start, directory, eocd, _ = block_layout(data)
    records = b''.join(struct.pack('<QI', len(value) + 4, identity) + value for identity, value in pairs) + trailing
    size = len(records) + 24
    block = struct.pack('<Q', size) + records + struct.pack('<Q', size) + b'APK Sig Block 42'
    end = bytearray(data[eocd:])
    struct.pack_into('<I', end, 16, start + len(block))
    return data[:start] + block + data[directory:eocd] + end


def records(data):
    values, position = [], 0
    while position < len(data):
        length = struct.unpack_from('<I', data, position)[0]
        values.append(data[position + 4:position + 4 + length])
        position += length + 4
    assert position == len(data)
    return values


def with_v3_stripping_promise(data):
    """Resign a real v2 record whose signed attributes promise a v3 block."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    _, _, _, pairs = block_layout(data)
    original = dict(pairs)[0x7109871A]
    signer, = records(records(original)[0])
    signed_data, _, public_key = records(signer)
    digests, certificates, _ = records(signed_data)
    signed_data = lp(digests) + lp(certificates) + lp(lp(struct.pack('<II', 0xBEEFF00D, 3)))
    signature = material()[0].sign(signed_data, padding.PKCS1v15(), hashes.SHA256())
    signer = lp(signed_data) + lp(lp(struct.pack('<I', 0x0103) + lp(signature))) + lp(public_key)
    return rewrite_block(data, [(0x7109871A, lp(lp(signer))), (0xF05368C0, b'v3 fixture pair')])


class EmbeddedBuildMetadataTests(unittest.TestCase):
    def test_valid_v2_certificate_can_match_while_native_policy_rejects_v3(self):
        original = signed_apk()
        pairs = block_layout(original)[3]
        legacy = rewrite_block(original, pairs + [(0xF05368C0, b'v3 fixture pair')])
        # The additional pair is outside v2's signed chunk sections. The real
        # RSA signature and certificate remain valid, reproducing the missed
        # native-policy check without mocking any cryptographic verifier.
        self.assertEqual(material()[1], embedded.verify_apk_v2(legacy))
        with self.assertRaisesRegex(ValueError, 'native manager authentication.*0xf05368c0'):
            embedded.verify_sukisu_apk(legacy)
        with self.assertRaisesRegex(ValueError, 'native manager authentication'):
            embedded.read_manager_apk(legacy)
        restored = embedded.read_manager_apk(legacy, allow_legacy_signing_blocks=True)
        self.assertEqual(hashlib.sha256(material()[1]).hexdigest(), restored['manager_cert_sha256'])
        self.assertEqual(hashlib.sha256(legacy).hexdigest(), restored['apk_sha256'])

    def test_native_v2_and_optional_padding_accept_same_rsa_signer(self):
        data = signed_apk()
        self.assertEqual(material()[1], embedded.verify_sukisu_apk(data))
        padded = rewrite_block(data, block_layout(data)[3] + [(0x42726577, b'\0' * 32)])
        self.assertEqual(material()[1], embedded.verify_sukisu_apk(padded))
        self.assertEqual(hashlib.sha256(padded).hexdigest(), embedded.read_manager_apk(padded)['apk_sha256'])

    def test_native_policy_rejects_v31_source_stamp_and_unknown_pairs(self):
        data = signed_apk()
        for identity in (0x1B93AD61, 0x6DFF800D, 0x12345678):
            with self.subTest(identity=hex(identity)):
                candidate = rewrite_block(data, block_layout(data)[3] + [(identity, b'extra fixture pair')])
                self.assertEqual(material()[1], embedded.verify_apk_v2(candidate))
                with self.assertRaisesRegex(ValueError, 'v2-only signature'):
                    embedded.verify_sukisu_apk(candidate)

    def test_strict_policy_preserves_rsa_and_content_digest_verification(self):
        data = signed_apk()
        changed = data.replace(b'APK daemon fixture', b'BAD daemon fixture', 1)
        with self.assertRaisesRegex(ValueError, 'content digest'):
            embedded.verify_sukisu_apk(changed)
        pairs = block_layout(data)[3]
        signer, = records(records(pairs[0][1])[0])
        signed_data, signatures, public_key = records(signer)
        signature_record, = records(signatures)
        signature, = records(signature_record[4:])
        signature = bytes([signature[0] ^ 1]) + signature[1:]
        signer = lp(signed_data) + lp(lp(signature_record[:4] + lp(signature))) + lp(public_key)
        changed = rewrite_block(data, [(0x7109871A, lp(lp(signer)))])
        with self.assertRaisesRegex(ValueError, 'RSA signature verification'):
            embedded.verify_sukisu_apk(changed)

    def test_duplicate_missing_and_truncated_signing_pairs_fail_both_paths(self):
        data = signed_apk()
        pairs = block_layout(data)[3]
        malformed = [rewrite_block(data, pairs + pairs),
                     rewrite_block(data, [(0x42726577, b'padding only')]),
                     rewrite_block(data, pairs, trailing=b'\0\0\0'),
                     rewrite_block(data, pairs, trailing=struct.pack('<QI', 1024, 0x42726577) + b'x')]
        for candidate in malformed:
            for verify in (embedded.verify_apk_v2, embedded.verify_sukisu_apk):
                with self.subTest(verify=verify.__name__, length=len(candidate)), self.assertRaises(ValueError):
                    verify(candidate)

    def test_explicit_legacy_read_keeps_signature_stripping_protection(self):
        promised = with_v3_stripping_promise(signed_apk())
        self.assertEqual(material()[1], embedded.verify_apk_v2(promised))
        self.assertEqual('b' * 40, embedded.read_manager_apk(promised, allow_legacy_signing_blocks=True)['ksu_ref'])
        removed = rewrite_block(promised, [pair for pair in block_layout(promised)[3] if pair[0] != 0xF05368C0])
        for verify in (embedded.verify_apk_v2, embedded.verify_sukisu_apk):
            with self.subTest(verify=verify.__name__), self.assertRaisesRegex(ValueError, 'stripping protection'):
                verify(removed)
        with self.assertRaisesRegex(ValueError, 'stripping protection'):
            embedded.read_manager_apk(removed, allow_legacy_signing_blocks=True)

    def test_legacy_compatibility_requires_an_actual_boolean_opt_in(self):
        with self.assertRaisesRegex(ValueError, 'explicit boolean'):
            embedded.read_manager_apk(signed_apk(), allow_legacy_signing_blocks='false')

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
