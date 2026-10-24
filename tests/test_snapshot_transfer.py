"""Real Git-bundle recovery plus isolated GitHub draft/HTTP contract tests."""
import copy
import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import checkout_ksu_snapshot as checkout
import snapshot_ksu_source as snapshot
import transfer_ksu_snapshot as transfer


class FakeGitHub:
    """In-memory release transport; Git/bundle validation stays real."""
    def __init__(self):
        self.releases, self.data, self.calls = {}, {}, []
        self.upload_failure = False
        self.bad_download = False

    def json(self, path, *, method='GET', document=None, missing=False):
        self.calls.append((method, path, copy.deepcopy(document)))
        if path == '/releases' and method == 'POST':
            release = {**document, 'id': 901, 'published_at': None, 'assets': []}
            self.releases[901] = release
            return copy.deepcopy(release)
        if path.startswith('/releases/tags/'):
            return copy.deepcopy(self.releases.get('published'))
        release_id = int(path.rsplit('/', 1)[1])
        if method == 'DELETE':
            del self.releases[release_id]
            return None
        return copy.deepcopy(self.releases.get(release_id))

    def upload(self, release_id, name, raw):
        if self.upload_failure and name == snapshot.MANIFEST_NAME:
            raise transfer.TransferError('upload temporarily unavailable')
        asset_id = 1001 + len(self.data)
        self.data[asset_id] = bytes(raw)
        self.releases[release_id]['assets'].append({'id': asset_id, 'name': name, 'size': len(raw),
            'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'state': 'uploaded'})

    def download(self, asset_id, limit):
        self.calls.append(('DOWNLOAD', asset_id, limit))
        data = self.data[asset_id]
        return data[:-1] + b'!' if self.bad_download else data

    def published(self, expected):
        self.releases['published'] = {'id': 999, 'tag_name': f'gki-{expected.run_id}-{expected.run_attempt}',
            'target_commitish': expected.head_sha, 'draft': False, 'prerelease': False,
            'published_at': '2026-10-25T00:00:00Z', 'assets': [
                {'name': 'SHA256SUMS.txt', 'size': 100, 'digest': 'sha256:' + 'c' * 64},
                {'name': 'SukiSU_v4.2.1_41050-release.apk', 'size': 500, 'digest': 'sha256:' + 'd' * 64}]}


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class SnapshotTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = snapshot.temporary_directory(ROOT / 'tests', '.snapshot-transfer-fixture-')
        cls.fixture = cls.temporary.__enter__()
        cls.source = cls.fixture / 'official'
        cls.source.mkdir()
        cls.git(cls.source, 'init', '--quiet')
        cls.git(cls.source, 'config', 'user.name', 'Source backup regression')
        cls.git(cls.source, 'config', 'user.email', 'test@example.invalid')
        cls.git(cls.source, 'config', 'core.autocrlf', 'false')
        cls.git(cls.source, 'remote', 'add', 'origin', 'https://github.com/' + snapshot.REPOSITORY + '.git')
        (cls.source / 'kernel').mkdir()
        (cls.source / 'kernel/Kbuild').write_text('fixture source, never executed\n')
        cls.git(cls.source, 'add', '.')
        cls.git(cls.source, 'commit', '--quiet', '-m', 'initial fixture source')
        cls.git(cls.source, 'tag', '-a', 'v4.2.1', '-m', 'version fixture')
        (cls.source / 'manager.txt').write_text('second fixture revision\n')
        cls.git(cls.source, 'add', '.')
        cls.git(cls.source, 'commit', '--quiet', '-m', 'manager fixture revision')
        cls.reference = cls.git(cls.source, 'rev-parse', 'HEAD')
        cls.bundle = cls.fixture / 'original-bundle'
        cls.manifest = snapshot.snapshot_source(cls.source, cls.bundle, cls.reference, root=cls.fixture)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.__exit__(None, None, None)

    @staticmethod
    def git(directory, *args):
        return subprocess.run(['git', '-C', str(directory), *args], check=True,
                              capture_output=True, text=True, encoding='utf-8').stdout.strip()

    def setUp(self):
        self.temporary_test = snapshot.temporary_directory(self.fixture, '.case-')
        self.root = self.temporary_test.__enter__()
        self.addCleanup(self.temporary_test.__exit__, None, None, None)
        self.output = self.root / 'ksu-source-bundle'
        self.api = FakeGitHub()
        self.expected = transfer.Expected('cyzrqh/gki', 123, 1, 'a' * 40, self.reference)

    def create(self):
        return transfer.create(self.bundle, self.expected, self.api)

    def test_draft_backup_restores_real_full_git_history_without_cache_or_artifact(self):
        result = self.create()
        self.assertEqual(901, result['backup_release_id'])
        release = self.api.releases[901]
        self.assertTrue(release['draft'])
        self.assertFalse(release['prerelease'])
        self.assertEqual('false', release['make_latest'])
        self.assertIn('内部临时源码', release['name'])
        self.assertEqual(transfer.FILES, {asset['name'] for asset in release['assets']})
        transfer.restore(901, self.output, self.expected, self.api)
        restored = self.root / 'actual-checkout'
        actual = checkout.checkout_snapshot(self.output, restored, self.reference)
        self.assertEqual(self.manifest, actual)
        self.assertEqual(self.reference, self.git(restored, 'rev-parse', 'HEAD'))
        self.assertEqual('2', self.git(restored, 'rev-list', '--count', 'HEAD'))
        self.assertEqual(self.manifest['git_describe'], self.git(restored, 'describe', '--tags', '--always'))
        self.assertFalse(any('artifacts' in str(call) for call in self.api.calls))

    def test_partial_known_cache_is_replaced_only_after_complete_backup_verification(self):
        self.create()
        self.output.mkdir()
        partial = self.output / snapshot.BUNDLE_NAME
        partial.write_bytes(b'incomplete cache download')
        self.api.bad_download = True
        with self.assertRaisesRegex(transfer.TransferError, 'SHA256'):
            transfer.restore(901, self.output, self.expected, self.api)
        self.assertEqual(b'incomplete cache download', partial.read_bytes())
        self.api.bad_download = False
        transfer.restore(901, self.output, self.expected, self.api)
        self.assertEqual((self.bundle / snapshot.BUNDLE_NAME).read_bytes(), partial.read_bytes())
        self.assertEqual(transfer.FILES, {path.name for path in self.output.iterdir()})

    def test_unknown_cache_files_or_non_dedicated_output_are_preserved(self):
        self.create()
        self.output.mkdir()
        unknown = self.output / 'user-owned.txt'
        unknown.write_text('preserve this file')
        with self.assertRaisesRegex(transfer.TransferError, 'unknown files'):
            transfer.restore(901, self.output, self.expected, self.api)
        self.assertEqual('preserve this file', unknown.read_text())
        with self.assertRaisesRegex(transfer.TransferError, 'dedicated'):
            transfer.restore(901, self.root / 'other-directory', self.expected, self.api)
        self.assertFalse(any(call[0] == 'DOWNLOAD' for call in self.api.calls))

    def test_unknown_file_appearing_during_download_is_never_removed(self):
        self.create()
        self.output.mkdir()
        original_download = self.api.download
        def download(asset_id, limit):
            (self.output / 'appeared-during-download.txt').write_text('preserve concurrent file')
            return original_download(asset_id, limit)
        self.api.download = download
        with self.assertRaisesRegex(transfer.TransferError, 'unknown files'):
            transfer.restore(901, self.output, self.expected, self.api)
        self.assertEqual('preserve concurrent file', (self.output / 'appeared-during-download.txt').read_text())

    def test_install_and_rollback_failure_preserves_original_cache_outside_automatic_cleanup(self):
        self.create()
        self.output.mkdir()
        original = b'original partial cache evidence'
        (self.output / snapshot.BUNDLE_NAME).write_bytes(original)
        real_rename = Path.rename
        def fail_install_and_rollback(path, target):
            if path.name in {'verified', 'partial-cache'}:
                raise OSError('fixture: install/rollback denied')
            return real_rename(path, target)
        with mock.patch.object(Path, 'rename', fail_install_and_rollback):
            with self.assertRaisesRegex(transfer.TransferError, 'original cache preserved at') as error:
                transfer.restore(901, self.output, self.expected, self.api)
        retained = list(self.root.glob('.gki-source-recovery-*'))
        self.assertEqual(1, len(retained))
        previous = retained[0] / 'partial-cache'
        self.assertEqual(original, (previous / snapshot.BUNDLE_NAME).read_bytes())
        self.assertEqual((self.bundle / snapshot.BUNDLE_NAME).read_bytes(),
                         (retained[0] / 'verified' / snapshot.BUNDLE_NAME).read_bytes())
        self.assertIn(str(previous), str(error.exception))
        self.assertFalse(self.output.exists())

    def test_failed_rerun_reuses_earlier_prepare_but_refuses_future_attempt_or_other_run(self):
        self.create()
        transfer.restore(901, self.output, replace(self.expected, run_attempt=2), self.api)
        self.assertTrue(self.output.is_dir())
        for changes in ({'run_id': 124}, {'repository': 'other/gki'}, {'head_sha': 'b' * 40},
                        {'ksu_ref': 'c' * 40}):
            with self.subTest(changes=changes), self.assertRaises(transfer.TransferError):
                transfer.restore(901, self.output, replace(self.expected, **changes), self.api)
        self.api.releases[901]['body'] = self.api.releases[901]['body'].replace('"run_attempt": 1', '"run_attempt": 3')
        with self.assertRaisesRegex(transfer.TransferError, 'different repository, run, attempt'):
            transfer.restore(901, self.output, replace(self.expected, run_attempt=2), self.api)

    def test_published_foreign_renamed_or_incomplete_drafts_cannot_be_recovered(self):
        self.create()
        original = copy.deepcopy(self.api.releases[901])
        changes = ({'draft': False}, {'prerelease': True}, {'published_at': 'not-a-draft'},
                   {'name': 'an unrelated draft'}, {'tag_name': 'someone-elses-tag'},
                   {'target_commitish': 'd' * 40}, {'body': '{}'}, {'assets': []},
                   {'assets': [*original['assets'], original['assets'][0]]})
        for changed in changes:
            with self.subTest(changed=changed):
                self.api.releases[901] = {**original, **changed}
                with self.assertRaises(transfer.TransferError):
                    transfer.restore(901, self.output, self.expected, self.api)
                self.assertFalse(self.output.exists())

    def test_upload_failure_retains_owned_draft_and_never_deletes_any_release(self):
        self.api.upload_failure = True
        with self.assertRaisesRegex(transfer.TransferError, 'upload temporarily'):
            self.create()
        self.assertTrue(self.api.releases[901]['draft'])
        self.assertEqual(1, len(self.api.releases[901]['assets']))
        self.assertFalse(any(call[0] == 'DELETE' for call in self.api.calls))

    def test_cleanup_requires_matching_published_build_and_only_deletes_owned_draft(self):
        self.create()
        with self.assertRaisesRegex(transfer.TransferError, 'before the matching final Release'):
            transfer.delete(901, 'gki-123-1', self.expected, self.api)
        self.assertIn(901, self.api.releases)
        self.api.published(self.expected)
        self.api.releases[901]['draft'] = False
        with self.assertRaises(transfer.TransferError):
            transfer.delete(901, 'gki-123-1', self.expected, self.api)
        self.assertFalse(any(call[0] == 'DELETE' for call in self.api.calls))
        self.api.releases[901]['draft'] = True
        result = transfer.delete(901, 'gki-123-1', self.expected, self.api)
        self.assertTrue(result['deleted'])
        self.assertEqual([('DELETE', '/releases/901', None)], [call for call in self.api.calls if call[0] == 'DELETE'])
        self.assertTrue(transfer.delete(901, 'gki-123-1', self.expected, self.api)['already_absent'])

    def test_cleanup_failed_attempt_can_delete_backup_after_successful_later_attempt(self):
        self.create()
        expected = replace(self.expected, run_attempt=2)
        self.api.published(expected)
        with self.assertRaisesRegex(transfer.TransferError, 'exact published build tag'):
            transfer.delete(901, 'gki-123-1', expected, self.api)
        self.assertTrue(transfer.delete(901, 'gki-123-2', expected, self.api)['deleted'])

    def test_mismatched_local_bundle_is_not_uploaded_and_cli_outputs_only_public_ids(self):
        wrong = replace(self.expected, ksu_ref='e' * 40)
        with self.assertRaises(ValueError):
            transfer.create(self.bundle, wrong, self.api)
        self.assertEqual([], self.api.calls)
        output = self.root / 'github-output'
        environment = {'GITHUB_REPOSITORY': 'cyzrqh/gki', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
                       'GITHUB_SHA': 'a' * 40, 'GITHUB_OUTPUT': str(output), 'GH_TOKEN': 'private-fixture-token'}
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, environment), mock.patch.object(transfer, 'GitHub', return_value=self.api):
            with contextlib.redirect_stdout(stdout):
                result = transfer.main(['create', '--source', str(self.bundle), '--expected-ref', self.reference])
        self.assertEqual(0, result)
        self.assertEqual({'backup_release_id', 'backup_release_tag'}, set(json.loads(stdout.getvalue())))
        self.assertIn('backup_release_id=901\n', output.read_text())
        self.assertNotIn('private-fixture-token', output.read_text() + stdout.getvalue()
                         + self.api.releases[901]['body'])


class SnapshotHttpTests(unittest.TestCase):
    def test_http_errors_identify_public_api_path_without_query_or_download_url(self):
        def forbidden(request, timeout):
            raise HTTPError(request.full_url, 403, 'forbidden', {}, io.BytesIO())
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'private-request-token'}):
            api = transfer.GitHub('cyzrqh/gki', opener=mock.Mock(open=forbidden))
            for url, path in (
                ('https://api.github.com/repos/cyzrqh/gki/releases/901?secret=private-query',
                 '/repos/cyzrqh/gki/releases/901'),
                ('https://api.github.com/repos/cyzrqh/gki/releases/assets/1001',
                 '/repos/cyzrqh/gki/releases/assets/1001'),
            ):
                with self.subTest(path=path), self.assertRaises(transfer.TransferError) as raised:
                    api.request(url)
                message = str(raised.exception)
                self.assertIn('GET ' + path + ' failed (HTTP 403)', message)
                self.assertNotIn('private-request-token', message)
                self.assertNotIn('private-query', message)
                self.assertNotIn('?', message)
            with self.assertRaises(transfer.TransferError) as raised:
                api.request('https://release-assets.githubusercontent.com/private-path?signature=private-query',
                            authenticated=False, binary=True)
            self.assertIn('GET public release asset failed (HTTP 403)', str(raised.exception))
            self.assertNotIn('private-path', str(raised.exception))
            self.assertNotIn('private-query', str(raised.exception))

    def test_release_asset_upload_uses_json_accept_and_binary_content_type(self):
        calls = []
        def open_request(request, timeout):
            calls.append(request)
            return io.BytesIO(b'{"id":1001,"state":"uploaded"}')
        payload = b'\0real binary bundle transport\xff'
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'private-upload-token'}):
            api = transfer.GitHub('cyzrqh/gki', opener=mock.Mock(open=open_request))
            api.upload(901, snapshot.BUNDLE_NAME, payload)
        self.assertEqual(1, len(calls))
        request = calls[0]
        self.assertEqual('POST', request.get_method())
        self.assertEqual('https://uploads.github.com/repos/cyzrqh/gki/releases/901/assets?name=SukiSU.bundle',
                         request.full_url)
        self.assertEqual('application/vnd.github+json', request.get_header('Accept'))
        self.assertEqual('application/octet-stream', request.get_header('Content-type'))
        self.assertEqual(payload, request.data)
        self.assertEqual('Bearer private-upload-token', request.get_header('Authorization'))

    def test_download_redirect_creates_new_request_without_repository_token(self):
        calls = []
        def open_request(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise HTTPError(request.full_url, 302, 'redirect',
                                {'Location': 'https://release-assets.githubusercontent.com/asset?signature=public'}, io.BytesIO())
            return io.BytesIO(b'source bundle bytes')
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'private-token'}):
            api = transfer.GitHub('cyzrqh/gki', opener=mock.Mock(open=open_request))
            self.assertEqual(b'source bundle bytes', api.download(123, 1024))
        self.assertEqual('Bearer private-token', calls[0].get_header('Authorization'))
        self.assertIsNone(calls[1].get_header('Authorization'))
        self.assertEqual(2, len(calls))

    def test_external_or_nested_redirects_never_receive_authorization(self):
        for target in ('https://attacker.example/asset', 'http://release-assets.githubusercontent.com/asset',
                       'https://user:password@release-assets.githubusercontent.com/asset'):
            calls = []
            def open_request(request, timeout):
                calls.append(request)
                raise HTTPError(request.full_url, 302, 'redirect', {'Location': target}, io.BytesIO())
            with self.subTest(target=target), mock.patch.dict(os.environ, {'GH_TOKEN': 'private-token'}):
                api = transfer.GitHub('cyzrqh/gki', opener=mock.Mock(open=open_request))
                with self.assertRaises(transfer.TransferError):
                    api.download(123, 1024)
                self.assertEqual(1, len(calls))
        calls = []
        def nested(request, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 302, 'redirect',
                            {'Location': 'https://release-assets.githubusercontent.com/next'}, io.BytesIO())
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'private-token'}):
            api = transfer.GitHub('cyzrqh/gki', opener=mock.Mock(open=nested))
            with self.assertRaises(transfer.TransferError):
                api.download(123, 1024)
        self.assertEqual(2, len(calls))
        self.assertIsNone(calls[1].get_header('Authorization'))

    def test_api_metadata_redirect_and_oversized_response_fail_closed(self):
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'private-token'}):
            opener = mock.Mock()
            opener.open.side_effect = HTTPError('https://api.github.com/', 302, 'redirect',
                {'Location': 'https://release-assets.githubusercontent.com/asset'}, io.BytesIO())
            api = transfer.GitHub('cyzrqh/gki', opener=opener)
            with self.assertRaises(transfer.TransferError):
                api.json('/releases/901')
            self.assertEqual(1, opener.open.call_count)
            opener.open.side_effect = None
            opener.open.return_value = io.BytesIO(b'oversized')
            with self.assertRaises(transfer.TransferError):
                api.download(123, 3)


if __name__ == '__main__':
    unittest.main()
