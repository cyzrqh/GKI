import contextlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import validate_arm64_image as arm64


def branch(displacement):
    assert displacement % 4 == 0
    assert -(1 << 27) <= displacement < (1 << 27)
    return 0x14000000 | ((displacement // 4) & 0x03FFFFFF)


def image(*, target=128, size=256, efi=True, image_size=256):
    data = bytearray(size)
    code0 = 0x91005A4D if efi else branch(target)
    code1 = branch(target - 4) if efi else 0
    struct.pack_into('<IIQQQQQQII', data, 0, code0, code1, 0x80000,
                     image_size, 0xA, 0, 0, 0, 0x644D5241, 0x40)
    if 64 <= target and target + 4 <= size:
        struct.pack_into('<I', data, target, 0xD503201F)
    return bytes(data)


class Arm64ImageTests(unittest.TestCase):
    def test_efi_branch_uses_instruction_pc_not_image_base(self):
        details = arm64.validate_image(image())
        self.assertEqual(details['branch_offset'], 4)
        self.assertEqual(details['branch_displacement'], 124)
        self.assertEqual(details['branch_target'], 128)
        self.assertEqual(details['text_offset'], 0x80000)
        self.assertEqual(details['file_size'], 256)
        self.assertEqual(details['target_instruction'], 0xD503201F)

    def test_linux_ccmp_efi_signature_falls_through_to_the_real_branch(self):
        data = bytearray(image())
        # Exact Linux efi_signature_nop: ccmp x18, #0, #0xd, pl.
        struct.pack_into('<I', data, 0, 0xFA405A4D)
        details = arm64.validate_image(bytes(data))
        self.assertEqual(details['code0'], 0xFA405A4D)
        self.assertEqual(details['branch_offset'], 4)
        self.assertEqual(details['branch_target'], 128)
        self.assertEqual(details['target_instruction'], 0xD503201F)

    def test_ccmp_efi_signature_still_requires_a_valid_unconditional_branch(self):
        for instruction in (0, 0xD503201F, 0x9400001F, 0xD61F0000):
            data = bytearray(image())
            struct.pack_into('<II', data, 0, 0xFA405A4D, instruction)
            with self.subTest(instruction=instruction), self.assertRaisesRegex(ValueError, 'Unsupported ARM64 boot entry'):
                arm64.validate_image(bytes(data))
        for target, reason in ((4, '64-byte header'), (256, 'outside Image'), (-4, '64-byte header')):
            data = bytearray(image(target=target))
            struct.pack_into('<I', data, 0, 0xFA405A4D)
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, reason):
                arm64.validate_image(bytes(data))
        for instruction in (0, 0xFFFFFFFF):
            data = bytearray(image())
            struct.pack_into('<I', data, 0, 0xFA405A4D)
            struct.pack_into('<I', data, 128, instruction)
            with self.subTest(target_instruction=instruction), self.assertRaisesRegex(ValueError, 'empty/erased'):
                arm64.validate_image(bytes(data))

    def test_efi_signature_is_an_exact_instruction_allowlist_not_arbitrary_mz(self):
        # Similar encodings, including other values starting with MZ, must not
        # acquire fall-through semantics just because code1 is a valid branch.
        for instruction in (0xFA415A4D, 0xFA005A4D, 0xFA405A4C, 0xF8405A4D, 0xD61F5A4D):
            data = bytearray(image())
            struct.pack_into('<I', data, 0, instruction)
            with self.subTest(instruction=instruction), self.assertRaises(ValueError):
                arm64.validate_image(bytes(data))

    def test_direct_code0_branch_takes_precedence_over_code1(self):
        data = bytearray(image(efi=False))
        struct.pack_into('<I', data, 4, branch(-4))
        details = arm64.validate_image(bytes(data))
        self.assertEqual(details['branch_offset'], 0)
        self.assertEqual(details['branch_target'], 128)

    def test_nop_then_branch_falls_through(self):
        data = bytearray(image())
        struct.pack_into('<I', data, 0, 0xD503201F)
        self.assertEqual(arm64.validate_image(bytes(data))['branch_target'], 128)

    def test_actual_failed_entry_value_cannot_reach_payload(self):
        # Exact user release: code1=0x1490f7ff at PC=4 -> 0x243e000.
        # A tiny backing fixture is enough: the encoded target and truncated
        # file relation, rather than a 38 MB zero allocation, cause rejection.
        data = bytearray(image())
        struct.pack_into('<I', data, 4, 0x1490F7FF)
        self.assertEqual(4 + arm64._branch_displacement(0x1490F7FF), 0x243E000)
        self.assertGreater(0x243E000, 0x243D090)
        with self.assertRaisesRegex(ValueError, r'targets 0x243e000 outside Image'):
            arm64.validate_image(bytes(data))

    def test_signed_negative_branch_does_not_wrap_to_positive(self):
        self.assertEqual(arm64._branch_displacement(0x17FFFFFF), -4)
        self.assertEqual(arm64._branch_displacement(0x16000000), -(1 << 27))
        data = bytearray(image())
        struct.pack_into('<I', data, 4, branch(-8))
        with self.assertRaisesRegex(ValueError, 'before executable payload'):
            arm64.validate_image(bytes(data))

    def test_branch_to_header_and_backwards_code1_are_rejected(self):
        for target in (0, 4, 8, 60):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, '64-byte header'):
                arm64.validate_image(image(target=target))

    def test_target_must_have_a_complete_instruction_in_file(self):
        self.assertEqual(arm64.validate_image(image(target=252))['branch_target'], 252)
        for target, size in ((256, 256), (252, 255)):
            with self.subTest(target=target, size=size), self.assertRaisesRegex(ValueError, 'outside Image'):
                arm64.validate_image(image(target=target, size=size))

    def test_zero_and_erased_target_are_rejected(self):
        for instruction in (0, 0xFFFFFFFF):
            data = bytearray(image())
            struct.pack_into('<I', data, 128, instruction)
            with self.subTest(instruction=instruction), self.assertRaisesRegex(ValueError, 'empty/erased'):
                arm64.validate_image(bytes(data))

    def test_image_size_is_memory_extent_not_a_file_length_constraint(self):
        for declared_size in (0, 64, 512):
            with self.subTest(image_size=declared_size):
                self.assertEqual(arm64.validate_image(image(image_size=declared_size))['branch_target'], 128)

    def test_reserved_header_values_do_not_invent_boot_restrictions(self):
        data = bytearray(image())
        struct.pack_into('<QQQ', data, 32, 1, 2, 3)
        details = arm64.validate_image(bytes(data))
        self.assertEqual((details['reserved2'], details['reserved3'], details['reserved4']), (1, 2, 3))

    def test_empty_truncated_non_arm64_and_big_endian_rejected(self):
        for data in (b'', b'\0' * 63):
            with self.subTest(size=len(data)), self.assertRaisesRegex(ValueError, 'header is truncated'):
                arm64.validate_image(data)
        data = bytearray(image())
        data[56:60] = b'ARM!'
        with self.assertRaisesRegex(ValueError, 'no ARM64 magic'):
            arm64.validate_image(bytes(data))
        data = bytearray(image())
        struct.pack_into('<Q', data, 24, 1)
        with self.assertRaisesRegex(ValueError, 'Big-endian'):
            arm64.validate_image(bytes(data))

    def test_unknown_or_control_flow_code0_cannot_hide_behind_code1(self):
        for instruction in (0, 0xFFFFFFFF, 0xD61F0000, 0x94000020):
            data = bytearray(image())
            struct.pack_into('<I', data, 0, instruction)
            with self.subTest(instruction=instruction), self.assertRaisesRegex(ValueError, 'Unsupported ARM64 boot entry'):
                arm64.validate_image(bytes(data))

    def test_bl_code1_is_not_an_unconditional_entry_branch(self):
        data = bytearray(image())
        struct.pack_into('<I', data, 4, 0x9400001F)
        with self.assertRaisesRegex(ValueError, 'Unsupported ARM64 boot entry'):
            arm64.validate_image(bytes(data))

    def test_file_size_limit_is_enforced(self):
        with mock.patch.object(arm64, 'MAX_IMAGE_SIZE', 255):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                arm64.validate_image(image())

    def test_cli_emits_public_json_and_error_status(self):
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            path = Path(stream.name)
        try:
            for data, expected_status in ((image(), 0), (b'invalid', 1)):
                path.write_bytes(data)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    status = arm64.main(['--image', str(path)])
                payload = json.loads(output.getvalue())
                self.assertEqual(status, expected_status)
                self.assertEqual(payload['valid'], expected_status == 0)
                if expected_status:
                    self.assertIn('error', payload)
                else:
                    self.assertEqual(payload['branch_target'], 128)
        finally:
            path.unlink()


class LocalReleaseRegressionTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('GKI_TEST_BAD_AK3') and Path(os.environ['GKI_TEST_BAD_AK3']).is_file(), 'optional failed release unavailable')
    def test_local_failed_release_entry_is_outside_image(self):
        with zipfile.ZipFile(os.environ['GKI_TEST_BAD_AK3']) as archive:
            member = archive.getinfo('Image')
            self.assertLessEqual(member.file_size, arm64.MAX_IMAGE_SIZE)
            data = archive.read(member)
        self.assertEqual(len(data), 0x243D090)
        self.assertEqual(struct.unpack_from('<I', data, 4)[0], 0x1490F7FF)
        with self.assertRaisesRegex(ValueError, r'targets 0x243e000 outside Image'):
            arm64.validate_image(data)

    @unittest.skipUnless(os.environ.get('GKI_TEST_STOCK_BOOT') and Path(os.environ['GKI_TEST_STOCK_BOOT']).is_file(), 'optional stock boot image unavailable')
    def test_local_stock_boot_kernel_entry_remains_valid(self):
        with Path(os.environ['GKI_TEST_STOCK_BOOT']).open('rb') as stream:
            header = stream.read(44)
            self.assertEqual(header[:8], b'ANDROID!')
            self.assertEqual(struct.unpack_from('<I', header, 40)[0], 4)
            kernel_size = struct.unpack_from('<I', header, 8)[0]
            self.assertLessEqual(kernel_size, arm64.MAX_IMAGE_SIZE)
            stream.seek(4096)
            data = stream.read(kernel_size)
        details = arm64.validate_image(data)
        self.assertEqual(details['branch_target'], 0x2480000)
        self.assertEqual(details['code1'], 0x1491FFFF)


if __name__ == '__main__':
    unittest.main()
