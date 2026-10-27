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
    memory_size = struct.unpack_from('<Q', before, 16)[0]
    start = (max(memory_size, len(data)) + 4095) // 4096 * 4096
    struct.pack_into('<9Q', data, offset + 64 + 8, len(before), len(payload), memory_size,
                     12, offset, start, 128, 64, 0x100)
    data[offset + 64 + 160:offset + 64 + 168] = before[:8]
    if payload[8:12] == bytes((0, 0, 14, 0)):
        resized = (start + len(payload) + 128 + 4095) // 4096 * 4096
        struct.pack_into('<Q', data, 16, resized)
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
        profile = json.loads((self.cache / kpm.PROFILE_MANIFEST).read_text())
        self.assertEqual(profile, {'schema_version': 1, 'release': self.lock})

    def bridge_fixture(self):
        # Synthetic bytes exercise profile selection/transport, never bootability.
        payload = bytearray(self.payload)
        payload[8:12] = bytes((0, 0, 14, 0))
        self.payload = bytes(payload)
        (self.cache / 'kpimg').write_bytes(self.payload)
        bridge = json.loads(json.dumps(self.lock))
        bridge['tag'] = '0.14.0'
        bridge['commit'] = 'd958d2582fe3b7eaaeaf238b3bf9af4507a81174'
        bridge['assets']['kpimg']['sha256'] = kpm.digest(self.payload)
        for name, spec in bridge['assets'].items():
            spec['url'] = f'https://github.com/{kpm.REPOSITORY}/releases/download/0.14.0/{name}'
        return bridge

    def test_frozen_source_bridge_profile_is_bound_to_cache_and_patch_report(self):
        bridge = self.bridge_fixture()
        source = self.directory / 'frozen-sukisu'
        contract = {'version': '0.14.0', 'flags': 2, 'bridge_abi': 2}
        with mock.patch.object(kpm, 'resolve_profile', return_value=bridge) as resolve, \
                mock.patch.object(kpm, 'load_bridge_release', return_value=bridge), \
                mock.patch.object(kpm, 'validate_bridge_payload', return_value=contract) as validate, \
                mock.patch.object(kpm, 'download') as download, \
                mock.patch.object(kpm.subprocess, 'run', side_effect=self.run_tool):
            kpm.prepare(self.cache, ksu_source=source)
            resolve.assert_called_once_with(source)
            report = kpm.patch(self.image, self.cache)
        download.assert_not_called()
        self.assertGreaterEqual(validate.call_count, 2)
        self.assertEqual(report['release'], {key: bridge[key] for key in ('repository', 'tag', 'commit')})
        self.assertEqual(report['payload']['bridge_abi'], 2)
        self.assertEqual(report['payload']['version'], '0.14.0')
        self.assertEqual(report['pre_image_sha256'], kpm.digest(self.before))
        self.assertEqual(report['post_image_sha256'], kpm.digest(self.image.read_bytes()))
        self.assertEqual(report['layout']['payload_size'], len(self.payload))
        self.assertEqual(json.loads((self.cache / kpm.PROFILE_MANIFEST).read_text())['release'], bridge)

    def test_unknown_source_contract_fails_before_cache_changes_or_download(self):
        with mock.patch.object(kpm, 'resolve_profile', side_effect=ValueError('unsupported KPM ABI')), \
                mock.patch.object(kpm, 'download') as download, self.assertRaises(ValueError):
            kpm.prepare(self.cache, ksu_source=self.directory / 'unknown-source')
        download.assert_not_called()
        self.assertEqual((self.cache / 'kpimg').read_bytes(), self.payload)
        self.assertFalse((self.cache / kpm.PROFILE_MANIFEST).exists())

    def test_report_provenance_selects_only_exact_locked_release(self):
        bridge = self.bridge_fixture()
        with mock.patch.object(kpm, 'load_bridge_release', return_value=bridge):
            for release in (self.lock, bridge):
                provenance = {key: release[key] for key in ('repository', 'tag', 'commit')}
                self.assertEqual(kpm.release_for_provenance(provenance), release)
            for bad in (None, {}, {'repository': kpm.REPOSITORY, 'tag': '0.14.0', 'commit': '0' * 40},
                        {'repository': kpm.REPOSITORY, 'tag': kpm.TAG, 'commit': kpm.COMMIT, 'extra': 1}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    kpm.release_for_provenance(bad)

    def test_bridge_assets_without_profile_never_fall_back_to_legacy_contract(self):
        self.bridge_fixture()
        with mock.patch.object(kpm.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'size/SHA256'):
            kpm.patch(self.image, self.cache)
        run.assert_not_called()
        self.assertEqual(self.image.read_bytes(), self.before)

    def test_manifest_rejects_unlocked_assets_source_and_duplicate_keys(self):
        kpm.prepare(self.cache)
        manifest = self.cache / kpm.PROFILE_MANIFEST
        good = json.loads(manifest.read_text())
        for field, value in (('commit', '0' * 40), ('tag', 'latest'), ('repository', 'untrusted/payload')):
            bad = json.loads(json.dumps(good))
            bad['release'][field] = value
            manifest.write_text(json.dumps(bad))
            with self.subTest(field=field), mock.patch.object(kpm.subprocess, 'run') as run, \
                    self.assertRaises(ValueError):
                kpm.patch(self.image, self.cache)
            run.assert_not_called()
        for field, value in (('sha256', '0' * 64), ('url', 'https://example.com/kpimg')):
            bad = json.loads(json.dumps(good))
            bad['release']['assets']['kpimg'][field] = value
            manifest.write_text(json.dumps(bad))
            with self.subTest(field=field), mock.patch.object(kpm.subprocess, 'run') as run, \
                    self.assertRaises(ValueError):
                kpm.patch(self.image, self.cache)
            run.assert_not_called()
        for nested in ('schema_version', 'size'):
            bad = json.loads(json.dumps(good))
            if nested == 'schema_version':
                bad['release']['schema_version'] = True
            else:
                bad['release']['assets']['kpimg']['size'] = float(len(self.payload))
            manifest.write_text(json.dumps(bad))
            with self.subTest(invalid_type=nested), self.assertRaises(ValueError):
                kpm.patch(self.image, self.cache)
        manifest.write_text('{"schema_version":1,"schema_version":1,"release":' + json.dumps(self.lock) + '}')
        with mock.patch.object(kpm.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'Duplicate'):
            kpm.patch(self.image, self.cache)
        run.assert_not_called()
        self.assertEqual(self.image.read_bytes(), self.before)

    def test_prepare_refuses_to_switch_contract_in_an_existing_cache(self):
        kpm.prepare(self.cache)
        bridge = json.loads(json.dumps(self.lock))
        bridge.update(tag='0.14.0', commit='d958d2582fe3b7eaaeaf238b3bf9af4507a81174')
        with mock.patch.object(kpm, 'resolve_profile', return_value=bridge), \
                mock.patch.object(kpm, 'load_bridge_release', return_value=bridge), \
                mock.patch.object(kpm, 'download') as download, self.assertRaisesRegex(ValueError, 'different'):
            kpm.prepare(self.cache, ksu_source=self.directory / 'bridge-source')
        download.assert_not_called()
        self.assertEqual((self.cache / 'kpimg').read_bytes(), self.payload)

    def test_prepare_cli_passes_frozen_source_for_contract_detection(self):
        source = self.directory / 'source'
        with mock.patch.object(kpm, 'prepare') as prepare, contextlib.redirect_stdout(io.StringIO()):
            status = kpm.main(['prepare', '--cache-dir', str(self.cache), '--ksu-source', str(source)])
        self.assertEqual(status, 0)
        prepare.assert_called_once_with(self.cache, None, source)

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

    def test_linux_ccmp_efi_signature_survives_verified_kpm_patching(self):
        before = bytearray(self.before)
        struct.pack_into('<I', before, 0, 0xFA405A4D)
        self.before = bytes(before)
        self.image.write_bytes(self.before)
        with mock.patch.object(kpm.subprocess, 'run', side_effect=self.run_tool):
            result = kpm.patch(self.image, self.cache)
        self.assertEqual(result['pre_entry']['code0'], 0xFA405A4D)
        self.assertEqual(result['post_entry']['code0'], 0xFA405A4D)
        self.assertEqual(result['post_entry']['branch_target'], result['layout']['payload_offset'] + kpm.PAGE)
        self.assertEqual(self.image.read_bytes()[8:64], self.before[8:64])
        self.assertEqual(self.image.read_bytes(), patched_fixture(self.before, self.payload))

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
        for error_stream in ('stdout', 'stderr'):
            streams = {'stdout': secret.encode(), 'stderr': secret.encode()}
            streams[error_stream] = (secret + '\nanalyze_kallsym_info error\n' + secret).encode()
            failure = subprocess.CompletedProcess([], 7, **streams)
            with self.subTest(error_stream=error_stream), \
                    mock.patch.dict(os.environ, {'KPM_PASSWORD': secret}), \
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

    def test_original_map_pac_instructions_survive_independent_image_reconstruction(self):
        before = bytearray(self.before)
        struct.pack_into('<II', before, 128, 0xd503233f, 0xd50323bf)
        struct.pack_into('<Q', before, 16, len(before) + 8192)
        after = bytearray(patched_fixture(before, self.payload))
        struct.pack_into('<II', after, 128, 0xd503201f, 0xd503201f)
        layout = kpm.verify_patched(before, after, self.payload)
        windows = kpm.capture_original_windows(before, after, layout)
        report = {'pre_image_size':len(before), 'pre_entry':kpm.image_entry(before),
                  'layout':layout, 'original_windows':windows}
        self.assertEqual(kpm.reconstruct_original_image(after, report), before)
        self.assertEqual(len(windows), 1)
        # Restoring only the header, as before this fix, loses PAC/AUT bytes.
        self.assertNotEqual(kpm.reconstruct_original_image(after, dict(report, original_windows=[])), before)

    def test_bridge_layout_resizes_only_the_exact_official_image_header_field(self):
        self.bridge_fixture()
        contract = {'version': '0.14.0', 'flags': 2, 'bridge_abi': 2}
        after = patched_fixture(self.before, self.payload)
        with mock.patch.object(kpm, 'validate_bridge_payload', return_value=contract):
            layout = kpm.verify_patched(self.before, after, self.payload)
            self.assertEqual(layout['payload_contract'], kpm.BRIDGE_CONTRACT)
            original_size = struct.unpack_from('<Q', self.before, 16)[0]
            expected_size = (layout['start_offset'] + len(self.payload) + 128 + 4095) // 4096 * 4096
            self.assertEqual(struct.unpack_from('<Q', after, 16)[0], expected_size)
            self.assertGreater(expected_size, original_size)
            report = {'pre_image_size':len(self.before), 'pre_entry':kpm.image_entry(self.before),
                      'layout':layout, 'original_windows':[]}
            self.assertEqual(kpm.reconstruct_original_image(after, report), self.before)
            for position in (8, 16, 24, 8192 + 64 + 24):
                bad = bytearray(after)
                bad[position] ^= 1
                with self.subTest(position=position), self.assertRaises(ValueError):
                    kpm.verify_patched(self.before, bytes(bad), self.payload)
            for position in (16, 8192 + 64 + 24):
                bad = bytearray(after)
                bad[position] ^= 1
                with self.subTest(restoration_position=position), self.assertRaises(ValueError):
                    kpm.reconstruct_original_image(bytes(bad), report)

    def test_legacy_layout_rejects_new_bridge_resize_behavior(self):
        after = bytearray(patched_fixture(self.before, self.payload))
        struct.pack_into('<Q', after, 16, len(after) + 4096)
        with self.assertRaisesRegex(ValueError, 'size header'):
            kpm.verify_patched(self.before, bytes(after), self.payload)

    def test_bridge_map_supports_only_its_bounded_8192_byte_official_sync_window(self):
        self.bridge_fixture()
        before = bytearray(self.before + bytes(8192))
        struct.pack_into('<Q', before, 16, len(before))
        for position in (4096, 12284):
            struct.pack_into('<I', before, position, 0xd503233f)
        after = bytearray(patched_fixture(bytes(before), self.payload))
        offset = 16384
        struct.pack_into('<QQ', after, offset + 64 + 64, 4096, 0x1000)
        for position in (4096, 12284):
            struct.pack_into('<I', after, position, 0xd503201f)
        contract = {'version': '0.14.0', 'flags': 2, 'bridge_abi': 2}
        with mock.patch.object(kpm, 'validate_bridge_payload', return_value=contract):
            layout = kpm.verify_patched(bytes(before), bytes(after), self.payload)
            windows = kpm.capture_original_windows(bytes(before), bytes(after), layout)
            self.assertEqual(len(bytes.fromhex(windows[0]['before_hex'])), 8192)
            report = {'pre_image_size':len(before), 'pre_entry':kpm.image_entry(before),
                      'layout':layout, 'original_windows':windows}
            self.assertEqual(kpm.reconstruct_original_image(bytes(after), report), bytes(before))
            bad = bytearray(after)
            struct.pack_into('<Q', bad, offset + 64 + 72, 0x1001)
            with self.assertRaisesRegex(ValueError, 'preset'):
                kpm.verify_patched(bytes(before), bytes(bad), self.payload)
            # An otherwise identical old contract cannot claim the larger map.
            old = dict(layout)
            old.pop('payload_contract')
            with self.assertRaisesRegex(ValueError, 'bounded map'):
                kpm.reconstruct_original_image(bytes(after), dict(report, layout=old))
            unknown = dict(layout, payload_contract='arbitrary-next-version')
            with self.assertRaisesRegex(ValueError, 'unknown payload'):
                kpm.reconstruct_original_image(bytes(after), dict(report, layout=unknown))

    def test_original_pi_map_transformation_is_bound_to_the_official_pattern_and_kernel(self):
        before = bytearray(self.before)
        before[1024:1056] = b'Linux version 6.12.58-test'.ljust(32, b'\0')
        before[4096:4108] = kpm.PI_MAP_BEFORE
        after = bytearray(patched_fixture(before, self.payload))
        after[4096:4108] = kpm.PI_MAP_AFTER
        layout = kpm.verify_patched(before, after, self.payload)
        windows = kpm.capture_original_windows(before, after, layout)
        report = {'pre_image_size':len(before), 'pre_entry':kpm.image_entry(before),
                  'layout':layout, 'original_windows':windows}
        self.assertEqual(kpm.reconstruct_original_image(after, report), before)
        wrong = bytearray(before)
        wrong[1024:1056] = b'Linux version 6.1.118-test'.ljust(32, b'\0')
        changed = bytearray(patched_fixture(wrong, self.payload))
        changed[4096:4108] = kpm.PI_MAP_AFTER
        with self.assertRaisesRegex(ValueError, 'not applicable'):
            kpm.capture_original_windows(wrong, changed, kpm.verify_patched(wrong, changed, self.payload))
        for version, supported in (('6.7.0', False), ('6.7.1', True), ('6.8.0', True)):
            candidate = bytearray(before)
            candidate[1024:1056] = ('Linux version ' + version + '-test').encode().ljust(32, b'\0')
            patched = bytearray(patched_fixture(candidate, self.payload))
            patched[4096:4108] = kpm.PI_MAP_AFTER
            native_layout = kpm.verify_patched(candidate, patched, self.payload)
            with self.subTest(version=version):
                if supported:
                    self.assertEqual(len(kpm.capture_original_windows(candidate, patched, native_layout)), 1)
                else:
                    with self.assertRaisesRegex(ValueError, 'not applicable'):
                        kpm.capture_original_windows(candidate, patched, native_layout)

    def test_unexpected_kernel_modification_is_never_accepted_as_an_original_window(self):
        after = bytearray(patched_fixture(self.before, self.payload))
        after[4096] = 0x55
        layout = kpm.verify_patched(self.before, after, self.payload)
        with self.assertRaisesRegex(ValueError, 'outside'):
            kpm.capture_original_windows(self.before, after, layout)
        after[4096] = 0
        after[256] = 0x55
        with self.assertRaisesRegex(ValueError, 'PAC/NOP'):
            kpm.capture_original_windows(self.before, after, layout)

    def test_original_windows_reject_duplicate_overlap_oversize_and_nonofficial_bytes(self):
        after = patched_fixture(self.before, self.payload)
        layout = kpm.verify_patched(self.before, after, self.payload)
        base = {'pre_image_size':len(self.before), 'pre_entry':kpm.image_entry(self.before), 'layout':layout}
        for windows in ([{'offset':4096,'before_hex':'00'}],
                        [{'offset':64,'before_hex':'00' * 5121}],
                        [{'offset':64,'before_hex':'00' * 512}] * 2,
                        [{'offset':True,'before_hex':'00'}],
                        [{'offset':64,'before_hex':'zz'}],
                        [{'offset':64,'before_hex':'00','extra':1}]):
            with self.subTest(windows=windows[:1]), self.assertRaises(ValueError):
                kpm.reconstruct_original_image(after, dict(base, original_windows=windows))

    def map_fixture(self, remaining):
        # The native selector floor-aligns a usable text anchor to 16 bytes.
        # Choose a fixed aligned anchor, with EOF 0x1000..0x13ff bytes later.
        anchor = 0x1000
        before = bytearray(self.before[:anchor] + bytes(remaining))
        struct.pack_into('<Q', before, 16, len(before))
        after = bytearray(patched_fixture(bytes(before), self.payload))
        setup = ((len(before) + kpm.PAGE - 1) // kpm.PAGE * kpm.PAGE) + 64
        struct.pack_into('<QQ', after, setup + 64, anchor, 0xa00)
        # Model official patch.c's min(2 * map_max_size, filelen-map_start)
        # sync without extending the kernel into padding or the KPM payload.
        sync_size = min(0x1400, len(before) - anchor)
        after[anchor:anchor + sync_size] = b'\x1f\x20\x03\xd5' * (sync_size // 4) + \
            b'\x1f\x20\x03\xd5'[:sync_size % 4]
        return bytes(before), bytes(after)

    def test_official_mapping_tail_accepts_clamped_sync_but_stays_inside_original_file(self):
        for remaining in (0x1000, 0x1001, 0x13f0, 0x13ff):
            before, after = self.map_fixture(remaining)
            with self.subTest(remaining=remaining):
                layout = kpm.verify_patched(before, after, self.payload)
                self.assertEqual(layout['map_offset'], 0x1000)
                self.assertEqual(layout['map_max_size'], 0xa00)
                self.assertLessEqual(layout['map_offset'] + kpm.PAGE, len(before))
                self.assertGreater(layout['map_offset'] + 2 * layout['map_max_size'], len(before))
        for remaining in (0xff0, 0xfff):
            before, after = self.map_fixture(remaining)
            with self.subTest(rejected_remaining=remaining), self.assertRaisesRegex(ValueError, 'preset'):
                kpm.verify_patched(before, after, self.payload)

    def test_out_of_bounds_mapping_from_tool_never_replaces_original_image(self):
        before, invalid = self.map_fixture(0xff0)
        self.image.write_bytes(before)
        def invalid_map(argv, **kwargs):
            Path(argv[argv.index('-o') + 1]).write_bytes(invalid)
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        with mock.patch.object(kpm.subprocess, 'run', side_effect=invalid_map):
            status, message = self.diagnostic()
        self.assertEqual(status, 1)
        self.assertIn('[verify-layout]', message)
        self.assertEqual(self.image.read_bytes(), before)

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
