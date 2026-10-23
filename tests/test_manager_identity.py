import copy
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
                                          'Manager-Identity-' + 'b' * 7)
        self.metadata = {'file': 'SukiSU_v4.2.0_40922-release.apk', 'package': 'com.cyzrqh.gki.sukisu',
                         'ksu_ref': 'b' * 40, 'ksu_variant': 'SukiSU', 'ksu_version_code': '40922',
                         'ksu_version_name': '4.2.0', 'manager_cert_size': '730',
                         'manager_cert_sha256': 'c' * 64, 'apk_sha256': 'd' * 64,
                         'producer_repository': 'cyzrqh/gki', 'producer_run_id': '123',
                         'producer_run_attempt': '1', 'producer_head_sha': 'a' * 40}

    def zip(self, metadata=None, *, member='manager.json', extra=False):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr(member, json.dumps(self.metadata if metadata is None else metadata))
            if extra:
                archive.writestr('untrusted.txt', 'extra')
        return content.getvalue()

    def artifact(self, artifact_id=7, **updates):
        value = {'id': artifact_id, 'name': self.expected.artifact_name, 'size_in_bytes': 500,
                 'expired': False, 'workflow_run': {'id': 123, 'head_sha': 'a' * 40}}
        value.update(updates)
        return value

    def wait(self, reader, **kwargs):
        self.clock = Clock()
        return identity.wait_for_identity(self.expected, reader, monotonic=self.clock.monotonic,
                                          sleep=self.clock.sleep, **kwargs)

    def test_signed_public_identity_is_available_while_apk_job_still_runs(self):
        reader = FakeReader(self.expected, [[self.artifact()]], {7: self.zip()})
        result = self.wait(reader)
        self.assertEqual(result['MANAGER_CERT_SIZE'], '730')
        self.assertEqual(result['MANAGER_CERT_SHA256'], 'c' * 64)
        self.assertEqual(result['MANAGER_VERSION_CODE'], '40922')
        self.assertEqual(result['MANAGER_APK_FILENAME'], self.metadata['file'])
        self.assertNotIn('producer_run_id', result)
        self.assertEqual(self.clock.waits, [])
        self.assertTrue(reader.assert_binary)

    def test_waits_for_pending_identity_then_downloads_current_artifact(self):
        reader = FakeReader(self.expected, [[], [], [self.artifact()]], {7: self.zip()})
        self.assertEqual(self.wait(reader)['MANAGER_PACKAGE'], self.metadata['package'])
        self.assertEqual(self.clock.waits, [15, 15])

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
        reader = FakeReader(self.expected, [[self.artifact(size_in_bytes=65537)]], {7: self.zip()})
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
        for key in self.metadata:
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
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('manager.json', '{"package":"com.good.app","package":"com.evil.app"}')
        with self.assertRaises(identity.IdentityError):
            identity.read_identity_archive(raw.getvalue(), self.expected)

    def test_previous_attempt_is_reused_only_with_retained_successful_manager_job(self):
        self.expected = identity.Expected('cyzrqh/gki', 123, 2, 'a' * 40, 'b' * 40,
                                          'Manager-Identity-' + 'b' * 7)
        prior_job = {'name': self.expected.manager_job, 'status': 'completed',
                     'conclusion': 'success', 'run_attempt': 1}
        reader = FakeReader(self.expected, [[self.artifact()]], {7: self.zip()},
                            current_job='absent', latest_job=prior_job)
        self.assertEqual(self.wait(reader)['MANAGER_CERT_SHA256'], 'c' * 64)
        self.assertEqual(self.clock.waits, [])

    def test_rerun_manager_in_progress_waits_for_replacement_instead_of_using_old_certificate(self):
        self.expected = identity.Expected('cyzrqh/gki', 123, 2, 'a' * 40, 'b' * 40,
                                          'Manager-Identity-' + 'b' * 7)
        current = {**self.metadata, 'producer_run_attempt': '2', 'manager_cert_sha256': 'e' * 64}
        reader = FakeReader(self.expected, [[self.artifact()], [self.artifact()], [self.artifact(8)]],
                            {7: self.zip(), 8: self.zip(current)})
        self.assertEqual(self.wait(reader)['MANAGER_CERT_SHA256'], 'e' * 64)
        self.assertEqual(self.clock.waits, [15, 15])
        self.assertEqual(reader.paths.count('actions/artifacts/7/zip'), 1)

    def test_old_identity_not_reused_for_different_successful_manager_attempt(self):
        self.expected = identity.Expected('cyzrqh/gki', 123, 3, 'a' * 40, 'b' * 40,
                                          'Manager-Identity-' + 'b' * 7)
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


if __name__ == '__main__':
    unittest.main()
