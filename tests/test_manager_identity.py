import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import capture_manager_metadata as capture
import wait_manager_identity as identity
from apk_fixture import manager_metadata, signed_apk


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


class FakeReader:
    def __init__(self, expected, rounds, archives, *, current_job=None, latest_job=None):
        self.expected = expected
        self.rounds = iter(rounds)
        self.last_round = []
        self.archives = archives
        self.current_job = current_job or {'name': expected.manager_job, 'status': 'in_progress',
                                           'run_attempt': expected.run_attempt}
        self.latest_job = latest_job
        self.paths = []
        self.bad_run = None

    def get(self, suffix, *, binary=False):
        self.paths.append(suffix)
        if suffix == f'actions/runs/{self.expected.run_id}/attempts/{self.expected.run_attempt}':
            return self.bad_run or {'id': self.expected.run_id, 'head_sha': self.expected.head_sha,
                                   'run_attempt': self.expected.run_attempt,
                                   'repository': {'full_name': self.expected.repository}}
        if '/attempts/' in suffix and '/jobs?' in suffix:
            return {'jobs': [] if self.current_job == 'absent' else [self.current_job]}
        if '/jobs?filter=latest' in suffix:
            return {'jobs': [self.latest_job] if self.latest_job else []}
        if '/artifacts?' in suffix:
            self.last_round = next(self.rounds, self.last_round)
            return {'artifacts': self.last_round}
        if suffix.startswith('actions/artifacts/') and suffix.endswith('/zip'):
            self.assert_binary = binary
            return self.archives[int(suffix.split('/')[2])]
        raise AssertionError(suffix)


class ManagerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.expected = identity.Expected('cyzrqh/gki', 123, 1, 'a' * 40, 'b' * 40,
                                          'SukiSU_v4.2.0_40922-release.apk')
        self.metadata = manager_metadata(apk_sha256='d' * 64)

    def zip(self, metadata=None, *, member='assets/gki-build.json', extra=False):
        public = dict(self.metadata if metadata is None else metadata)
        public.pop('apk_sha256', None)
        return signed_apk(public, member=member, extra=extra)

    def artifact(self, artifact_id=7, **updates):
        value = {'id': artifact_id, 'name': self.expected.artifact_name, 'size_in_bytes': 500,
                 'expired': False, 'workflow_run': {'id': 123, 'head_sha': 'a' * 40},
                 'digest': 'sha256:' + hashlib.sha256(self.zip()).hexdigest()}
        value.update(updates)
        return value

    def wait(self, reader, **kwargs):
        self.clock = Clock()
        return identity.wait_for_identity(self.expected, reader, monotonic=self.clock.monotonic,
                                          sleep=self.clock.sleep, **kwargs)

    def test_signed_public_identity_is_available_while_apk_job_still_runs(self):
        reader = FakeReader(self.expected, [[self.artifact()]], {7: self.zip()})
        result = self.wait(reader)
        self.assertEqual(result['MANAGER_CERT_SIZE'], self.metadata['manager_cert_size'])
        self.assertEqual(result['MANAGER_CERT_SHA256'], self.metadata['manager_cert_sha256'])
        self.assertEqual(result['MANAGER_VERSION_CODE'], '40922')
        self.assertEqual(result['MANAGER_APK_FILENAME'], self.metadata['file'])
        self.assertNotIn('producer_run_id', result)
        self.assertEqual(self.clock.waits, [])
        self.assertTrue(reader.assert_binary)

    def test_waits_for_pending_identity_then_downloads_current_artifact(self):
        reader = FakeReader(self.expected, [[], [], [self.artifact()]], {7: self.zip()})
        self.assertEqual(self.wait(reader)['MANAGER_PACKAGE'], self.metadata['package'])
        self.assertEqual(self.clock.waits, [15, 15])

    def test_only_final_apk_artifact_is_used_and_self_hash_is_computed(self):
        self.expected = identity.Expected('cyzrqh/gki', 123, 1, 'a' * 40, 'b' * 40)
        apk = self.zip()
        artifact = self.artifact(name=self.metadata['file'])
        legacy = self.artifact(8, name='Manager-Identity-bbbbbbb')
        reader = FakeReader(self.expected, [[legacy, artifact]], {7: apk, 8: b'old manager JSON zip'})
        values = self.wait(reader)
        self.assertEqual(values['MANAGER_APK_SHA256'], hashlib.sha256(apk).hexdigest())
        self.assertNotIn('actions/artifacts/8/zip', reader.paths)

    def test_github_artifact_digest_is_checked_before_using_the_apk(self):
        for digest in (None, 'bad', 'sha256:' + '0' * 64):
            reader = FakeReader(self.expected, [[self.artifact(digest=digest)]], {7: self.zip()})
            with self.subTest(digest=digest), self.assertRaisesRegex(identity.IdentityError, 'GitHub digest'):
                self.wait(reader)

    def test_api_single_apk_transport_archive_preserves_exact_signed_file(self):
        raw = self.zip()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(self.metadata['file'], raw)
        values = identity.read_identity_archive(stream.getvalue(), self.expected)
        self.assertEqual(values['MANAGER_APK_SHA256'], hashlib.sha256(raw).hexdigest())

    def test_failed_cancelled_and_skipped_apk_producers_stop_without_wait(self):
        for conclusion in ('failure', 'cancelled', 'skipped', 'timed_out', 'action_required'):
            with self.subTest(conclusion=conclusion):
                job = {'name': self.expected.manager_job, 'status': 'completed', 'conclusion': conclusion,
                       'run_attempt': 1}
                reader = FakeReader(self.expected, [[], [self.artifact()]], {7: self.zip()}, current_job=job)
                with self.assertRaisesRegex(identity.IdentityError, 'APK job failed'):
                    self.wait(reader)
                self.assertEqual(self.clock.waits, [])
                self.assertFalse(any('/artifacts' in path for path in reader.paths))

    def test_timeout_is_bounded_and_last_sleep_never_exceeds_deadline(self):
        reader = FakeReader(self.expected, [[]], {})
        with self.assertRaisesRegex(identity.IdentityError, 'Timed out'):
            self.wait(reader, timeout=20)
        self.assertEqual(self.clock.waits, [15, 5])
        with self.assertRaisesRegex(identity.IdentityError, '20 minutes'):
            self.wait(reader, timeout=1201)

    def test_rejects_wrong_run_and_workflow_commit_before_accepting_artifact(self):
        for update in ({'id': 321}, {'head_sha': 'f' * 40}, {'run_attempt': 2},
                       {'repository': {'full_name': 'attacker/gki'}}):
            with self.subTest(update=update):
                reader = FakeReader(self.expected, [[self.artifact()]], {7: self.zip()})
                reader.bad_run = {'id': 123, 'head_sha': 'a' * 40, 'run_attempt': 1,
                                  'repository': {'full_name': 'cyzrqh/gki'}, **update}
                with self.assertRaisesRegex(identity.IdentityError, 'Workflow API returned'):
                    self.wait(reader)

    def test_rejects_wrong_artifact_run_or_commit_before_downloading(self):
        for producer in ({'id': 456, 'head_sha': 'a' * 40}, {'id': 123, 'head_sha': 'e' * 40}, None):
            reader = FakeReader(self.expected, [[self.artifact(workflow_run=producer)]], {7: self.zip()})
            with self.assertRaisesRegex(identity.IdentityError, 'different workflow'):
                self.wait(reader)
            self.assertFalse(any(path.endswith('/zip') for path in reader.paths))

    def test_rejects_large_artifact_without_download(self):
        reader = FakeReader(self.expected, [[self.artifact(size_in_bytes=identity.MAX_ARCHIVE_BYTES + 1)]], {7: self.zip()})
        with self.assertRaisesRegex(identity.IdentityError, 'artifact size'):
            self.wait(reader)
        self.assertFalse(any(path.endswith('/zip') for path in reader.paths))

    def test_rejects_multiple_current_identity_artifacts(self):
        reader = FakeReader(self.expected, [[self.artifact(), self.artifact(8)]], {7: self.zip()})
        with self.assertRaisesRegex(identity.IdentityError, 'More than one'):
            self.wait(reader)

    def test_checks_producer_source_and_apk_metadata(self):
        cases = {'producer_repository': 'another/gki', 'producer_run_id': 999,
                 'producer_head_sha': 'f' * 40, 'ksu_ref': 'e' * 40, 'ksu_variant': 'KernelSU',
                 'package': 'com.bad\nGH_TOKEN=leaked', 'file': '../manager.apk',
                 'manager_cert_size': 63, 'manager_cert_sha256': 'abc', 'apk_sha256': 'not-a-hash',
                 'ksu_version_code': 0, 'ksu_version_name': ''}
        for key, bad in cases.items():
            with self.subTest(key=key):
                changed = {**self.metadata, key: bad}
                with self.assertRaises(identity.IdentityError):
                    identity.validate_metadata(changed, self.expected)

    def test_rejects_missing_fields_and_control_characters_everywhere(self):
        for key in self.metadata.keys() - {'schema_version', 'ksu_branch'}:
            with self.subTest(missing=key):
                changed = copy.deepcopy(self.metadata)
                del changed[key]
                with self.assertRaises(identity.IdentityError):
                    identity.validate_metadata(changed, self.expected)
        for bad in ('x\r\nINJECT=y', 'x\x00y', 'x\x1by', 'x\x7fy'):
            with self.assertRaises(identity.IdentityError):
                identity.validate_metadata({**self.metadata, 'optional': bad}, self.expected)

    def test_rejects_unsafe_zip_paths_extra_members_and_oversized_payloads(self):
        for raw in (self.zip(member='../manager.json'), self.zip(extra=True), b'bad zip',
                    b'x' * 65537, self.zip({**self.metadata, 'padding': 'x' * 17000})):
            with self.subTest(length=len(raw)):
                with self.assertRaises(identity.IdentityError):
                    identity.read_identity_archive(raw, self.expected)

    def test_rejects_duplicate_json_keys(self):
        with self.assertRaises(identity.IdentityError):
            identity.read_identity_archive(signed_apk(raw_metadata=b'{"package":"com.good.app","package":"com.evil.app"}'), self.expected)

    def test_previous_attempt_is_reused_only_with_retained_successful_manager_job(self):
        self.expected = identity.Expected('cyzrqh/gki', 123, 2, 'a' * 40, 'b' * 40,
                                          self.metadata['file'])
        prior_job = {'name': self.expected.manager_job, 'status': 'completed',
                     'conclusion': 'success', 'run_attempt': 1}
        reader = FakeReader(self.expected, [[self.artifact()]], {7: self.zip()},
                            current_job='absent', latest_job=prior_job)
        self.assertEqual(self.wait(reader)['MANAGER_CERT_SHA256'], self.metadata['manager_cert_sha256'])
        self.assertEqual(self.clock.waits, [])

    def test_rerun_manager_in_progress_waits_for_replacement_instead_of_using_old_certificate(self):
        self.expected = identity.Expected('cyzrqh/gki', 123, 2, 'a' * 40, 'b' * 40,
                                          self.metadata['file'])
        current = {**self.metadata, 'producer_run_attempt': '2'}
        current_digest = 'sha256:' + hashlib.sha256(self.zip(current)).hexdigest()
        reader = FakeReader(self.expected, [[self.artifact()], [self.artifact()], [self.artifact(8, digest=current_digest)]],
                            {7: self.zip(), 8: self.zip(current)})
        self.assertEqual(self.wait(reader)['MANAGER_CERT_SHA256'], self.metadata['manager_cert_sha256'])
        self.assertEqual(self.clock.waits, [15, 15])
        self.assertEqual(reader.paths.count('actions/artifacts/7/zip'), 1)

    def test_old_identity_not_reused_for_different_successful_manager_attempt(self):
        self.expected = identity.Expected('cyzrqh/gki', 123, 3, 'a' * 40, 'b' * 40,
                                          self.metadata['file'])
        job = {'name': self.expected.manager_job, 'status': 'completed',
               'conclusion': 'success', 'run_attempt': 2}
        reader = FakeReader(self.expected, [[self.artifact()]], {7: self.zip()},
                            current_job='absent', latest_job=job)
        with self.assertRaisesRegex(identity.IdentityError, 'Timed out'):
            self.wait(reader, timeout=15)

    def test_gh_reader_uses_read_only_repo_scoped_endpoints_and_hides_errors(self):
        response = subprocess.CompletedProcess([], 0, stdout=b'{"artifacts":[]}', stderr=b'')
        with patch.object(identity.subprocess, 'run', return_value=response) as run:
            self.assertEqual(identity.GitHubReader('cyzrqh/gki').get('actions/runs/123/artifacts'), {'artifacts': []})
            args = run.call_args.args[0]
            self.assertEqual(args[:6], ['gh', 'api', '--hostname', 'github.com', '--method', 'GET'])
            self.assertEqual(args[-1], 'repos/cyzrqh/gki/actions/runs/123/artifacts')
        bad = subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'gh token SECRET')
        with patch.object(identity.subprocess, 'run', return_value=bad):
            with self.assertRaises(identity.IdentityError) as error:
                identity.GitHubReader('cyzrqh/gki').get('actions/runs/123/artifacts')
            self.assertNotIn('SECRET', str(error.exception))
        for path in ('https://evil/actions/runs/123', 'actions/../secrets', 'secrets/actions'):
            with self.assertRaises(identity.IdentityError):
                identity.GitHubReader('cyzrqh/gki').get(path)

    def test_capture_adds_only_public_verified_signer_outputs_and_current_run_provenance(self):
        directory = ROOT / 'tests' / ('.manager-identity-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        output = directory / 'manager.json'
        env = {'META_FILE': self.metadata['file'], 'META_PACKAGE': self.metadata['package'],
               'META_KSU_VERSION_CODE': '40922', 'META_KSU_VERSION_NAME': '4.2.0',
               'META_KSU_REF': 'b' * 40, 'META_KSU_BRANCH': 'builtin',
               'META_MANAGER_CERT_SIZE': '730', 'META_MANAGER_CERT_SHA256': 'c' * 64,
               'META_APK_SHA256': 'd' * 64, 'GITHUB_REPOSITORY': 'cyzrqh/gki',
               'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40,
               'SUKISU_APK_PASSWORD': 'PRIVATE_SIGNING_PASSWORD',
               'SUKISU_APK_KEYSTORE': 'PRIVATE_KEYSTORE'}
        with patch.dict(os.environ, env), patch.object(sys, 'argv', ['capture', '--output', str(output)]):
            capture.main()
        text = output.read_text(encoding='utf-8')
        self.assertNotIn('PRIVATE', text)
        metadata = json.loads(text)
        self.assertEqual(metadata['producer_head_sha'], 'a' * 40)
        self.assertEqual(metadata['manager_cert_sha256'], 'c' * 64)
        values = identity.validate_metadata(metadata, self.expected)
        envfile = directory / 'github-env'
        identity.write_environment(envfile, values)
        self.assertIn('MANAGER_PACKAGE=com.cyzrqh.gki.sukisu\n', envfile.read_text())
        self.assertNotIn('SUKISU_APK_PASSWORD', envfile.read_text())

    def test_capture_reports_susfs_for_verified_checked_patch_sources_and_preserves_strict_policy(self):
        directory = ROOT / 'tests' / ('.manager-identity-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        output = directory / 'manager.json'
        env = {'META_FILE': self.metadata['file'], 'META_PACKAGE': self.metadata['package'],
               'META_KSU_VERSION_CODE': '40922', 'META_KSU_VERSION_NAME': '4.2.0',
               'META_KSU_REF': 'b' * 40, 'META_KSU_BRANCH': 'builtin',
               'META_MANAGER_CERT_SIZE': '730', 'META_MANAGER_CERT_SHA256': 'c' * 64,
               'META_APK_SHA256': 'd' * 64, 'GITHUB_REPOSITORY': 'cyzrqh/gki',
               'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40}
        cases = (
            ('checked-patch', 'true', 'f' * 40, 'v2.3.0'),
            ('reviewed', 'true', 'f' * 40, None),
            ('checked-patch', 'false', 'f' * 40, None),
            ('reviewed', 'true', 'B' * 40, 'v2.3.0'),
        )
        for policy, enabled, reviewed_commit, expected in cases:
            profile = {'source_policy': policy, 'version': 'v2.3.0', 'ksu_commits': [reviewed_commit]}
            with self.subTest(policy=policy, enabled=enabled, reviewed_commit=reviewed_commit), \
                    patch.object(capture, 'load_profile', return_value=profile) as load, \
                    patch.dict(os.environ, {**env, 'META_ENABLE_SUSFS': enabled}), \
                    patch.object(sys, 'argv', ['capture', '--output', str(output)]):
                capture.main()
                load.assert_called_once_with(ROOT)
                metadata = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual(metadata['susfs_version'], expected)
                identity.validate_metadata(metadata, self.expected)


class GitHubIdentityReaderTests(unittest.TestCase):
    def response(self, *, status=0, stdout=b'{"artifacts":[]}', stderr=b''):
        return subprocess.CompletedProcess([], status, stdout=stdout, stderr=stderr)

    def test_transient_http_reads_retry_same_endpoint_with_short_bounded_delays(self):
        errors = [self.response(status=1, stderr=b'gh: unavailable (HTTP 502) SECRET'),
                  self.response(status=1, stderr=b'gh: unavailable (HTTP 503) SECRET'),
                  self.response()]
        diagnostics = io.StringIO()
        with patch.object(identity.subprocess, 'run', side_effect=errors) as run, \
                patch.object(identity.time, 'sleep') as sleep, contextlib.redirect_stderr(diagnostics):
            result = identity.GitHubReader('cyzrqh/gki').get('actions/runs/123/artifacts')
        self.assertEqual({'artifacts': []}, result)
        self.assertEqual(3, run.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])
        self.assertTrue(all(call.args == run.call_args_list[0].args for call in run.call_args_list))
        self.assertIn('APK artifact listing', diagnostics.getvalue())
        self.assertIn('HTTP 502', diagnostics.getvalue())
        self.assertNotIn('SECRET', diagnostics.getvalue())

    def test_recognized_network_failures_recover_without_exposing_signed_urls(self):
        for message in (b'TLS handshake timeout', b'connection reset by peer', b'i/o timeout',
                        b'temporary failure in name resolution', b'no such host', b'unexpected EOF',
                        b'http2: client connection lost'):
            error = self.response(status=1, stderr=message + b' https://asset.example/a?token=SECRET')
            diagnostics = io.StringIO()
            with self.subTest(message=message), patch.object(identity.subprocess, 'run',
                    side_effect=[error, self.response(stdout=b'APK')]) as run, \
                    patch.object(identity.time, 'sleep') as sleep, contextlib.redirect_stderr(diagnostics):
                self.assertEqual(b'APK', identity.GitHubReader('cyzrqh/gki').get(
                    'actions/artifacts/7/zip', binary=True))
            self.assertEqual(2, run.call_count)
            sleep.assert_called_once_with(1)
            self.assertIn('signed APK download', diagnostics.getvalue())
            self.assertNotIn('asset.example', diagnostics.getvalue())
            self.assertNotIn('SECRET', diagnostics.getvalue())

    def test_permanent_http_and_unknown_cli_failures_are_not_retried(self):
        for message, category in (
            (b'gh: denied (HTTP 401) SECRET', 'HTTP 401'),
            (b'gh: denied (HTTP 403) SECRET connection reset', 'HTTP 403'),
            (b'gh: secondary rate limit (HTTP 403) SECRET', r'HTTP 403 \(rate limited\)'),
            (b'gh: missing (HTTP 404) SECRET', 'HTTP 404'),
            (b'gh: invalid (HTTP 422) SECRET', 'HTTP 422'),
            (b'gh: unavailable (HTTP 501) SECRET', 'HTTP 501'),
            (b'gh: opaque private error SECRET', 'GitHub CLI request failed'),
        ):
            with self.subTest(message=message), patch.object(identity.subprocess, 'run',
                    return_value=self.response(status=1, stderr=message)) as run, \
                    patch.object(identity.time, 'sleep') as sleep:
                with self.assertRaisesRegex(identity.IdentityError, category) as raised:
                    identity.GitHubReader('cyzrqh/gki').get('actions/runs/123/jobs')
            self.assertEqual(1, run.call_count)
            sleep.assert_not_called()
            self.assertIn('manager producer lookup', str(raised.exception))
            self.assertNotIn('SECRET', str(raised.exception))

    def test_every_allowlisted_transient_http_status_is_bounded_at_three_attempts(self):
        for code in (408, 429, 500, 502, 503, 504):
            diagnostics = io.StringIO()
            error = self.response(status=1, stderr=f'gh: private response (HTTP {code}) SECRET'.encode())
            with self.subTest(code=code), patch.object(identity.subprocess, 'run', return_value=error) as run, \
                    patch.object(identity.time, 'sleep') as sleep, contextlib.redirect_stderr(diagnostics):
                with self.assertRaisesRegex(identity.IdentityError, rf'HTTP {code}.*attempt 3/3') as raised:
                    identity.GitHubReader('cyzrqh/gki').get('actions/runs/123/attempts/1')
            self.assertEqual(3, run.call_count)
            self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])
            self.assertIn('workflow provenance lookup', str(raised.exception))
            self.assertNotIn('SECRET', str(raised.exception) + diagnostics.getvalue())

    def test_timeouts_recover_or_stop_after_three_without_exception_payloads(self):
        timeout = subprocess.TimeoutExpired(['gh', 'api', 'SECRET'], 60,
                                            output=b'private response SECRET', stderr=b'private URL SECRET')
        for attempts in (1, 3):
            effects = [timeout, self.response()] if attempts == 1 else [timeout] * 3
            diagnostics = io.StringIO()
            with self.subTest(attempts=attempts), patch.object(identity.subprocess, 'run', side_effect=effects) as run, \
                    patch.object(identity.time, 'sleep') as sleep, contextlib.redirect_stderr(diagnostics):
                if attempts == 1:
                    self.assertEqual({'artifacts': []}, identity.GitHubReader('cyzrqh/gki').get('actions/runs/123/jobs'))
                else:
                    with self.assertRaisesRegex(identity.IdentityError, r'request timeout.*attempt 3/3') as raised:
                        identity.GitHubReader('cyzrqh/gki').get('actions/runs/123/jobs')
                    self.assertNotIn('SECRET', str(raised.exception))
            self.assertEqual(2 if attempts == 1 else 3, run.call_count)
            self.assertNotIn('SECRET', diagnostics.getvalue())
            self.assertTrue(all(call.kwargs['timeout'] == 60 for call in run.call_args_list))

    def test_cli_execution_failure_and_invalid_success_payloads_fail_without_retry(self):
        with patch.object(identity.subprocess, 'run', side_effect=FileNotFoundError('SECRET')) as run, \
                patch.object(identity.time, 'sleep') as sleep:
            with self.assertRaisesRegex(identity.IdentityError, 'CLI could not execute') as raised:
                identity.GitHubReader('cyzrqh/gki').get('actions/runs/123/jobs')
        self.assertEqual(1, run.call_count)
        sleep.assert_not_called()
        self.assertNotIn('SECRET', str(raised.exception))
        cases = ((b'SECRET', False, 'API response'),
                 (b'\xff', False, 'API response'),
                 (b'x' * 11, False, 'size limit'),
                 (b'x' * 11, True, 'size limit'))
        for payload, binary, message in cases:
            with self.subTest(binary=binary, message=message), \
                    patch.object(identity, 'MAX_API_BYTES', 10), patch.object(identity, 'MAX_ARCHIVE_BYTES', 10), \
                    patch.object(identity.subprocess, 'run', return_value=self.response(stdout=payload)) as run, \
                    patch.object(identity.time, 'sleep') as sleep:
                with self.assertRaisesRegex(identity.IdentityError, message) as raised:
                    identity.GitHubReader('cyzrqh/gki').get('actions/artifacts/7/zip', binary=binary)
            self.assertEqual(1, run.call_count)
            sleep.assert_not_called()
            self.assertNotIn('SECRET', str(raised.exception))

    def test_transport_retries_do_not_skip_signed_apk_or_source_verification(self):
        fixture = ManagerIdentityTests()
        fixture.setUp()
        apk = fixture.zip()
        bad_source = {**fixture.metadata, 'ksu_ref': 'e' * 40}
        malformed_apk = fixture.zip(bad_source)
        response = self.response(stdout=malformed_apk)
        with patch.object(identity.subprocess, 'run', side_effect=[
                self.response(status=1, stderr=b'gh: temporarily unavailable (HTTP 503)'), response]), \
                patch.object(identity.time, 'sleep'), contextlib.redirect_stderr(io.StringIO()):
            raw = identity.GitHubReader('cyzrqh/gki').get('actions/artifacts/7/zip', binary=True)
        with self.assertRaisesRegex(identity.IdentityError, 'source commits differ'):
            identity.read_identity_archive(raw, fixture.expected)
        self.assertNotEqual(apk, raw)


if __name__ == '__main__':
    unittest.main()
