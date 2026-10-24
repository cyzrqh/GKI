"""Synthetic, deliberately non-loadable ELF/KPM bytes test validation, not bootability."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest
import urllib.error
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import apply_kpm_patch as kpm


def payload_fixture():
    data = bytearray(0x3000)
    data[:8] = kpm.MAGIC
    data[8:12] = bytes((0, 9, 13, 0))
    struct.pack_into('<Q', data, 16, 2)
    data[24:42] = b'TEST NOT LOADABLE!!'
    struct.pack_into('<I', data, 0x1000, 0xd503201f)
    data[0x2000:0x2010] = b'fixture-code!!!!'
    return bytes(data)


def tool_fixture():
    data = bytearray(128)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHIQQ', data, 16, 2, 62, 1, 0x400078, 64)
    struct.pack_into('<HHH', data, 52, 64, 56, 1)
    struct.pack_into('<IIQQQQQQ', data, 64, 1, 5, 0, 0x400000, 0, 128, 128, 4096)
    return bytes(data)


def image_fixture():
    data = bytearray(8192)
    struct.pack_into('<IIQQQQQQII', data, 0, 0x91005a4d, 0x1400001f, 0x80000,
                     len(data), 0xa, 0, 0, 0, 0x644d5241, 0x40)
    struct.pack_into('<I', data, 128, 0xd503201f)
    return bytes(data)


def patched_fixture(before, payload):
    offset = (len(before) + 4095) // 4096 * 4096
    data = bytearray(before + bytes(offset - len(before)) + payload + bytes(128))
    struct.pack_into('<I', data, 4, 0x14000000 | ((offset + 4096 - 4) // 4))
    start = (max(len(before), len(data)) + 4095) // 4096 * 4096
    struct.pack_into('<9Q', data, offset + 64 + 8, len(before), len(payload), len(before),
                     12, offset, start, 128, 64, 0x100)
    data[offset + 64 + 160:offset + 64 + 168] = before[:8]
    return bytes(data)


class KpmPatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT.parent / '.gki-upstream-cache' / ('kpm-test-' + uuid.uuid4().hex)
        self.directory.mkdir(parents=True)
        self.cache = self.directory / 'cache'
        self.cache.mkdir()
        self.image = self.directory / 'Image'
        self.before = image_fixture()
        self.image.write_bytes(self.before)
        self.payload = payload_fixture()
        self.tool = tool_fixture()
        self.lock = {'schema_version': 1, 'repository': kpm.REPOSITORY, 'tag': kpm.TAG,
                     'commit': kpm.COMMIT, 'assets': {}}
        for name, data in [('kpimg', self.payload), ('kptools-linux', self.tool)]:
            self.lock['assets'][name] = {'size': len(data), 'sha256': kpm.digest(data),
                'url': f'https://github.com/{kpm.REPOSITORY}/releases/download/{kpm.TAG}/{name}'}
            (self.cache / name).write_bytes(data)
        self.lock_patch = mock.patch.object(kpm, 'load_release', return_value=self.lock)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        self.addCleanup(shutil.rmtree, self.directory)

    def run_tool(self, argv, **kwargs):
        self.assertNotIn('123', argv)
        self.assertEqual(argv[-2:], ['--skey-fd', '0'])
        self.assertEqual(Path(argv[argv.index('-i') + 1]).read_bytes(), self.before)
        self.assertEqual(Path(argv[argv.index('-k') + 1]).read_bytes(), self.payload)
        Path(argv[argv.index('-o') + 1]).write_bytes(patched_fixture(self.before, self.payload))
        return subprocess.CompletedProcess(argv, 0, stdout=b'', stderr=b'')

    def test_real_release_lock_is_complete_and_immutable(self):
        self.lock_patch.stop()
        document = kpm.load_release()
        self.assertEqual(document['commit'], '72bf62025e9c72e07b64c77db0801891f1088429')
        self.assertEqual(document['assets']['kpimg']['size'], 294144)
        self.assertEqual(document['assets']['kptools-linux']['size'], 1672792)

    def test_prepare_reuses_only_verified_assets_and_exports_path(self):
        env = self.directory / 'github-env'
        with mock.patch.object(kpm, 'download') as download:
            self.assertEqual(kpm.prepare(self.cache, env), self.cache)
        download.assert_not_called()
        self.assertEqual(env.read_text(), f'KPM_CACHE_DIR={self.cache}\n')

    def test_corrupt_existing_cache_is_rejected_without_download_or_replacement(self):
        cached = self.cache / 'kpimg'
        cached.write_bytes(b'bad')
        with mock.patch.object(kpm, 'download') as download, self.assertRaises(ValueError):
            kpm.prepare(self.cache)
        download.assert_not_called()
        self.assertEqual(cached.read_bytes(), b'bad')

    def test_public_redirect_download_never_sends_authorization(self):
        url = self.lock['assets']['kpimg']['url']
        redirected = 'https://release-assets.githubusercontent.com/public?signature=public-download-only'
        response = io.BytesIO(self.payload)
        response.status = 200
        response.headers = {'Content-Length': str(len(self.payload))}
        opener = mock.Mock()
        opener.open.side_effect = [urllib.error.HTTPError(url, 302, 'redirect',
                                                        {'Location': redirected}, None), response]
        with mock.patch.object(kpm.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(kpm.download(url, len(self.payload)), self.payload)
        self.assertEqual(opener.open.call_count, 2)
        for call in opener.open.call_args_list:
            request = call.args[0]
            self.assertIsNone(request.get_header('Authorization'))
            self.assertIsNone(request.get_header('Cookie'))

    def test_unsafe_redirect_and_oversized_download_are_rejected(self):
        url = self.lock['assets']['kpimg']['url']
        for location in ('https://example.com/steal', 'http://github.com/payload',
                         'https://user:password@github.com/payload'):
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(url, 302, 'redirect',
                                                             {'Location': location}, None)
            with self.subTest(location=location), mock.patch.object(
                    kpm.urllib.request, 'build_opener', return_value=opener), self.assertRaises(ValueError):
                kpm.download(url, len(self.payload))
            self.assertEqual(opener.open.call_count, 1)
        for data, declared in ((self.payload + b'x', None), (self.payload, '1')):
            response = io.BytesIO(data)
            response.status = 200
            response.headers = {} if declared is None else {'Content-Length': declared}
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch.object(kpm.urllib.request, 'build_opener', return_value=opener), self.assertRaises(ValueError):
                kpm.download(url, len(self.payload))

    def test_prepare_downloads_bounded_assets_and_checks_hash(self):
        for path in self.cache.iterdir():
            path.unlink()
        with mock.patch.object(kpm, 'download', side_effect=[self.payload, self.tool]) as download:
            kpm.prepare(self.cache)
        self.assertEqual(download.call_count, 2)
        self.assertEqual((self.cache / 'kpimg').read_bytes(), self.payload)
        (self.cache / 'kpimg').unlink()
        with mock.patch.object(kpm, 'download', return_value=b'\xaa'):
            with self.assertRaisesRegex(ValueError, 'size/SHA256'):
                kpm.prepare(self.cache)
        self.assertFalse((self.cache / 'kpimg').exists())

    def test_placeholder_and_wrong_architecture_payloads_rejected_even_if_pinned(self):
        for data in (b'\xaa' + bytes(15), bytes(len(self.payload))):
            with self.subTest(size=len(data)), self.assertRaisesRegex(ValueError, 'placeholder'):
                kpm.validate_payload(data)
        for position, value in ((8, 1), (16, 6), (4096, 0)):
            data = bytearray(self.payload)
            if position == 4096:
                data[position:position + 4] = bytes(4)
            else:
                data[position] = value
            with self.subTest(position=position), self.assertRaises(ValueError):
                kpm.validate_payload(bytes(data))

    def test_tool_must_be_real_x86_64_executable_structure(self):
        kpm.validate_tool(self.tool)
        for position, value in ((4, 1), (18, 183), (54, 0)):
            data = bytearray(self.tool)
            data[position] = value
            with self.subTest(position=position), self.assertRaises(ValueError):
                kpm.validate_tool(bytes(data))

    def test_success_replaces_image_only_after_checks_and_writes_public_report(self):
        report = self.directory / 'kpm-report.json'
        with mock.patch.object(kpm.subprocess, 'run', side_effect=self.run_tool) as run:
            result = kpm.patch(self.image, self.cache, report)
        self.assertEqual(run.call_args.kwargs['input'], b'123\n')
        self.assertEqual(self.image.read_bytes(), patched_fixture(self.before, self.payload))
        self.assertEqual(result['pre_image_sha256'], kpm.digest(self.before))
        self.assertEqual(result['post_image_sha256'], kpm.digest(self.image.read_bytes()))
        self.assertEqual(json.loads(report.read_text()), result)
        self.assertEqual(result['layout']['payload_size'], len(self.payload))
        self.assertEqual(result['post_entry']['branch_target'], 12288)
        self.assertFalse(list(self.directory.glob('.kpm-patch-*')))

    def test_custom_password_only_stdin_never_logs_or_report(self):
        secret = 'test-secret-private-value'
        output = io.StringIO()
        def noisy(argv, **kwargs):
            self.assertEqual(kwargs['input'], secret.encode() + b'\n')
            self.assertNotIn(secret, ' '.join(argv))
            result = self.run_tool(argv, **kwargs)
            result.stdout = secret.encode()
            result.stderr = secret.encode()
            return result
        report = self.directory / 'report.json'
        with mock.patch.dict(os.environ, {'KPM_PASSWORD': secret}), mock.patch.object(
                kpm.subprocess, 'run', side_effect=noisy), contextlib.redirect_stdout(output):
            self.assertEqual(kpm.main(['patch', '--image', str(self.image), '--cache-dir', str(self.cache),
                                       '--report', str(report)]), 0)
        self.assertNotIn(secret, output.getvalue() + report.read_text())

    def test_bad_input_or_cached_asset_fails_before_tool(self):
        self.image.write_bytes(self.before[:64])
        with mock.patch.object(kpm.subprocess, 'run') as run, self.assertRaises(ValueError):
            kpm.patch(self.image, self.cache)
        run.assert_not_called()
        self.image.write_bytes(self.before)
        (self.cache / 'kpimg').write_bytes(b'\xaa' + bytes(15))
        with mock.patch.object(kpm.subprocess, 'run') as run, self.assertRaises(ValueError):
            kpm.patch(self.image, self.cache)
        run.assert_not_called()
        self.assertEqual(self.image.read_bytes(), self.before)

    def test_process_failure_and_timeout_preserve_original_and_redact_errors(self):
        for behavior in (subprocess.CompletedProcess([], 1, b'secret', b'secret'),
                         subprocess.TimeoutExpired(['secret'], 180, output=b'secret')):
            with self.subTest(behavior=type(behavior).__name__):
                side = {'side_effect': behavior} if isinstance(behavior, Exception) else {'return_value': behavior}
                with mock.patch.object(kpm.subprocess, 'run', **side), self.assertRaises(ValueError) as error:
                    kpm.patch(self.image, self.cache)
                self.assertNotIn('secret', str(error.exception))
                self.assertEqual(self.image.read_bytes(), self.before)

    def diagnostic(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = kpm.main(['patch', '--image', str(self.image), '--cache-dir', str(self.cache)])
        return status, output.getvalue()

    def test_main_identifies_header_asset_and_layout_failures_without_replacing_image(self):
        self.image.write_bytes(self.before[:64])
        status, message = self.diagnostic()
        self.assertEqual(status, 1)
        self.assertIn('[image-entry]', message)
        self.assertIn('outside Image file', message)
        self.assertEqual(self.image.read_bytes(), self.before[:64])

        self.image.write_bytes(self.before)
        (self.cache / 'kpimg').write_bytes(b'corrupt private bytes')
        status, message = self.diagnostic()
        self.assertEqual(status, 1)
        self.assertIn('[verify-assets]', message)
        self.assertIn('size/SHA256 verification', message)
        self.assertNotIn('private bytes', message)
        self.assertEqual(self.image.read_bytes(), self.before)

        (self.cache / 'kpimg').write_bytes(self.payload)
        def invalid_layout(argv, **kwargs):
            result = self.run_tool(argv, **kwargs)
            output = Path(argv[argv.index('-o') + 1])
            output.write_bytes(output.read_bytes()[:-1])
            return result
        with mock.patch.object(kpm.subprocess, 'run', side_effect=invalid_layout):
            status, message = self.diagnostic()
        self.assertEqual(status, 1)
        self.assertIn('[verify-layout]', message)
        self.assertIn('unexpected payload/extra extent', message)
        self.assertEqual(self.image.read_bytes(), self.before)

    def test_failed_tool_reports_only_exit_code_and_fixed_category_never_its_streams(self):
        secret = 'private-password-only-in-tool-streams'
        failure = subprocess.CompletedProcess([], 7, secret.encode(),
                                               (secret + '\nanalyze_kallsym_info error\n' + secret).encode())
        with mock.patch.dict(os.environ, {'KPM_PASSWORD': secret}), \
                mock.patch.object(kpm.subprocess, 'run', return_value=failure):
            status, message = self.diagnostic()
        self.assertEqual(status, 1)
        self.assertIn('[run-tool]', message)
        self.assertIn('exit=7, class=kernel-symbols', message)
        self.assertNotIn(secret, message)
        self.assertNotIn('analyze_kallsym_info error', message)
        self.assertEqual(self.image.read_bytes(), self.before)

    def test_external_exception_and_unknown_validation_text_are_never_published(self):
        secret = 'private-password-in-exception'
        for failure in (OSError(secret), subprocess.TimeoutExpired([secret], 180, output=secret.encode())):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(kpm.subprocess, 'run', side_effect=failure):
                status, message = self.diagnostic()
            self.assertEqual(status, 1)
            self.assertIn('[run-tool]', message)
            self.assertNotIn(secret, message)
            self.assertEqual(self.image.read_bytes(), self.before)
        with mock.patch.object(kpm, 'validate_image', side_effect=ValueError(secret)):
            status, message = self.diagnostic()
        self.assertEqual(status, 1)
        self.assertIn('[image-entry]', message)
        self.assertNotIn(secret, message)
        self.assertEqual(self.image.read_bytes(), self.before)
        with mock.patch.object(kpm, 'load_release', side_effect=ValueError(secret)):
            status, message = self.diagnostic()
        self.assertEqual(status, 1)
        self.assertIn('[release-lock]', message)
        self.assertNotIn(secret, message)

    def test_false_success_missing_or_truncated_output_preserves_original(self):
        for contents in (None, b'\xaa' + bytes(15), self.before):
            def run(argv, **kwargs):
                if contents is not None:
                    Path(argv[argv.index('-o') + 1]).write_bytes(contents)
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            with self.subTest(contents=contents is None), mock.patch.object(
                    kpm.subprocess, 'run', side_effect=run):
                with self.assertRaises((ValueError, OSError)):
                    kpm.patch(self.image, self.cache)
            self.assertEqual(self.image.read_bytes(), self.before)

    def test_full_payload_code_preset_header_and_guard_must_match(self):
        good = patched_fixture(self.before, self.payload)
        for position in (8, 8192, 8192 + 4096, 8192 + 64 + 8, 8192 + 64 + 160, len(good) - 1):
            bad = bytearray(good)
            bad[position] ^= 1
            with self.subTest(position=position), self.assertRaises(ValueError):
                kpm.verify_patched(self.before, bytes(bad), self.payload)

    def test_out_of_file_branch_and_fake_16byte_payload_are_rejected(self):
        bad = bytearray(self.before + b'\xaa' + bytes(143))
        target = (len(bad) + 4095) // 4096 * 4096
        struct.pack_into('<I', bad, 4, 0x14000000 | ((target - 4) // 4))
        with self.assertRaisesRegex(ValueError, 'outside Image'):
            kpm.verify_patched(self.before, bytes(bad), self.payload)

    def test_atomic_replace_failure_removes_report_and_leaves_original(self):
        report = self.directory / 'report.json'
        with mock.patch.object(kpm.subprocess, 'run', side_effect=self.run_tool), mock.patch.object(
                kpm.os, 'replace', side_effect=OSError('fault')), self.assertRaises(ValueError):
            kpm.patch(self.image, self.cache, report)
        self.assertEqual(self.image.read_bytes(), self.before)
        self.assertFalse(report.exists())

    def test_existing_report_is_not_overwritten_and_image_stays_original(self):
        report = self.directory / 'report.json'
        report.write_text('keep')
        with mock.patch.object(kpm.subprocess, 'run', side_effect=self.run_tool), self.assertRaises(ValueError):
            kpm.patch(self.image, self.cache, report)
        self.assertEqual(report.read_text(), 'keep')
        self.assertEqual(self.image.read_bytes(), self.before)

    def test_changed_original_during_patch_is_never_overwritten(self):
        changed = self.before + b'changed concurrently'
        def run(argv, **kwargs):
            result = self.run_tool(argv, **kwargs)
            self.image.write_bytes(changed)
            return result
        with mock.patch.object(kpm.subprocess, 'run', side_effect=run), self.assertRaisesRegex(ValueError, 'changed'):
            kpm.patch(self.image, self.cache)
        self.assertEqual(self.image.read_bytes(), changed)

    def test_bazel_bin_directory_symlink_resolves_to_real_build_output(self):
        actual = self.directory / 'actual-bazel-output'
        actual.mkdir()
        real_image = actual / 'Image'
        self.image.rename(real_image)
        link = self.directory / 'bazel-bin'
        try:
            link.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Creating directory symlinks requires Windows developer mode/permission')
        with mock.patch.object(kpm.subprocess, 'run', side_effect=self.run_tool):
            result = kpm.patch(link / 'Image', self.cache)
        self.assertTrue(link.is_symlink())
        self.assertEqual(real_image.read_bytes(), patched_fixture(self.before, self.payload))
        self.assertEqual(result['post_image_sha256'], kpm.digest(real_image.read_bytes()))

    def test_invalid_password_is_not_sent_to_subprocess(self):
        for password in ('x' * 64, 'line\nbreak', 'nul\0byte'):
            with self.subTest(length=len(password)), mock.patch.object(kpm.os, 'environ', {'KPM_PASSWORD': password}), mock.patch.object(
                    kpm.subprocess, 'run') as run, self.assertRaises(ValueError):
                kpm.patch(self.image, self.cache)
            run.assert_not_called()

    def test_already_patched_image_is_not_patched_twice(self):
        self.image.write_bytes(patched_fixture(self.before, self.payload))
        with mock.patch.object(kpm.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'already'):
            kpm.patch(self.image, self.cache)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
