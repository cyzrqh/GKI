from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import set_manager_signature as signature


class ManagerSignatureTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.signature-' + uuid.uuid4().hex)
        self.path = self.root / 'KernelSU/kernel/Kbuild'
        self.path.parent.mkdir(parents=True)
        self.text = (
            'keep := yes\n'
            'KSU_IS_PR_BUILD ?= 0\n'
            'ifeq ($(KSU_IS_PR_BUILD),0)\n'
            'KSU_PACKAGE_NAME ?= com.sukisu.ultra\n'
            'else\n'
            'KSU_PACKAGE_NAME ?= com.sukisu.ultra.pr\n'
            'endif\n'
            'KSU_EXPECTED_SIZE ?= 0x35c\n'
            'KSU_EXPECTED_HASH ?= old\n'
            'ccflags-y += -DKSU_PACKAGE_NAME=\\"$(KSU_PACKAGE_NAME)\\"\n'
            'ccflags-y += -DEXPECTED_HASH=\\"$(KSU_EXPECTED_HASH)\\"\n'
        )
        self.path.write_bytes(self.text.encode('utf-8'))
        self.addCleanup(shutil.rmtree, self.root)

    def expected(self, package=signature.DEFAULT_PACKAGE):
        return self.text.replace('com.sukisu.ultra.pr', package).replace('com.sukisu.ultra', package).replace(
            'KSU_EXPECTED_SIZE ?= 0x35c', 'KSU_EXPECTED_SIZE := 750').replace(
            'KSU_EXPECTED_HASH ?= old', 'KSU_EXPECTED_HASH := ' + 'a' * 64)

    def assert_unchanged_on_failure(self, text, package=signature.DEFAULT_PACKAGE):
        self.path.write_bytes(text.encode('utf-8'))
        with self.assertRaises(ValueError):
            signature.configure(self.root, '750', 'a' * 64, package)
        self.assertEqual(self.path.read_bytes(), text.encode('utf-8'))

    def test_sets_actual_apk_certificate_and_package_and_is_idempotent(self):
        signature.configure(self.root, '750', 'A' * 64)
        self.assertEqual(self.path.read_text(), self.expected())
        with patch.object(signature, 'atomic_write', side_effect=AssertionError('unnecessary write')):
            signature.configure(self.root, '750', 'a' * 64)
        self.assertEqual(self.path.read_text(), self.expected())

    def test_custom_package_is_used_in_both_native_branches(self):
        package = 'com.example.SukiSU_test'
        signature.configure(self.root, '750', 'a' * 64, package)
        self.assertEqual(self.path.read_text(), self.expected(package))
        signature.configure(self.root, '750', 'a' * 64, 'com.example.updated')
        self.assertEqual(self.path.read_text(), self.expected('com.example.updated'))

    def test_preserves_crlf_and_other_native_logic(self):
        self.path.write_bytes(self.text.replace('\n', '\r\n').encode('utf-8'))
        signature.configure(self.root, '750', 'a' * 64)
        self.assertEqual(self.path.read_bytes(), self.expected().replace('\n', '\r\n').encode('utf-8'))

    def test_rejects_invalid_or_injected_certificate_metadata(self):
        for size, digest in [('0', 'a' * 64), ('99999', 'a' * 64), ('750\nexport X=1', 'a' * 64),
                             ('750', 'bad'), ('750', 'a' * 64 + '\n')]:
            with self.subTest(size=size, digest=digest), self.assertRaises(ValueError):
                signature.configure(self.root, size, digest)
            self.assertEqual(self.path.read_text(), self.text)

    def test_rejects_invalid_or_injected_package(self):
        for package in ['', 'sukisu', '.com.example', 'com..example', 'com.example.', 'com.1example',
                        'com.example-manager', 'com.示例.app', 'com.example\nexport X=1',
                        'com.example; echo bad', '$(shell command)', 'com.example\n']:
            with self.subTest(package=package):
                self.assert_unchanged_on_failure(self.text, package)

    def test_certificate_layout_changes_do_not_partially_write(self):
        for text in [self.text.replace('KSU_EXPECTED_HASH', 'FUTURE_HASH'),
                     self.text + 'KSU_EXPECTED_SIZE := 123\n',
                     self.text + 'export KSU_EXPECTED_HASH := duplicate\n',
                     self.text.replace('KSU_EXPECTED_SIZE ?=', 'KSU_EXPECTED_SIZE +='),
                     self.text.replace('KSU_EXPECTED_HASH ?=', 'override KSU_EXPECTED_HASH ?=')]:
            with self.subTest(text=text):
                self.assert_unchanged_on_failure(text)

    def test_package_layout_changes_do_not_partially_write(self):
        for text in [self.text.replace('KSU_PACKAGE_NAME ?= com.sukisu.ultra.pr\n', ''),
                     self.text + 'KSU_PACKAGE_NAME ?= com.example.duplicate\n',
                     self.text + 'export KSU_PACKAGE_NAME := com.example.duplicate\n',
                     self.text.replace('KSU_PACKAGE_NAME ?=', 'KSU_PACKAGE_NAME :='),
                     self.text.replace('ifeq ($(KSU_IS_PR_BUILD),0)', 'ifeq ($(OTHER_CONDITION),0)'),
                     self.text.replace('else\n', 'else ifeq ($(OTHER_CONDITION),1)\n'),
                     self.text.replace('com.sukisu.ultra.pr', '$(DYNAMIC_PACKAGE)'),
                     self.text + self.text]:
            with self.subTest(text=text):
                self.assert_unchanged_on_failure(text)

    def test_atomic_replacement_failure_keeps_original_and_removes_temporary_file(self):
        with patch.object(signature.os, 'replace', side_effect=OSError('simulated replacement failure')):
            with self.assertRaisesRegex(OSError, 'simulated replacement failure'):
                signature.configure(self.root, '750', 'a' * 64)
        self.assertEqual(self.path.read_text(), self.text)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_atomic_replacement_preserves_file_permissions(self):
        before = stat.S_IMODE(self.path.stat().st_mode)
        signature.configure(self.root, '750', 'a' * 64)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_supports_driver_source_locations(self):
        self.path.unlink()
        for candidate in ['common/drivers/kernelsu/Kbuild', 'drivers/kernelsu/Kbuild']:
            with self.subTest(candidate=candidate):
                path = self.root / candidate
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.text.encode('utf-8'))
                self.assertEqual(signature.configure(self.root, '750', 'a' * 64), path.resolve())
                self.assertEqual(path.read_text(), self.expected())
                path.unlink()

    def test_missing_kbuild_is_explicit(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, 'Kbuild not found'):
            signature.configure(self.root, '750', 'a' * 64)

    def test_cli_accepts_actual_package_and_certificate_aliases(self):
        package = 'com.example.actual'
        for size_flag, digest_flag in [('--certificate-size', '--certificate-sha256'),
                                       ('--cert-size', '--cert-sha256')]:
            with self.subTest(size_flag=size_flag):
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'tools/set_manager_signature.py'), '--kernel-root', str(self.root),
                     size_flag, '750', digest_flag, 'a' * 64, '--package', package],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.path.read_text(), self.expected(package))


if __name__ == '__main__':
    unittest.main()
