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
import uuid
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import checkout_ksu_snapshot as checkout
import snapshot_ksu_source as snapshot
import transfer_ksu_snapshot as transfer


@contextlib.contextmanager
def short_fixture_directory(parent):
    parent = parent.resolve()
    directory = parent / ('.st-' + uuid.uuid4().hex[:8])
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.is_symlink() or directory.resolve().parent != parent:
            raise AssertionError('Unsafe fixture cleanup target')
        def readonly(function, name, error):
            Path(name).chmod(0o600)
            function(name)
        shutil.rmtree(directory, onexc=readonly)


class FakeGitHub:
    """In-memory release transport; Git/bundle validation stays real."""
    def __init__(self):
        self.releases, self.data, self.calls = {}, {}, []
        self.upload_failure = False
        self.bad_download = False
        self.action_run, self.action_artifacts = None, []
        self.action_pages = None

    def json(self, path, *, method='GET', document=None, missing=False):
        self.calls.append((method, path, copy.deepcopy(document)))
        if path.startswith('/actions/runs/'):
            if '/artifacts?' not in path:
                return copy.deepcopy(self.action_run)
            page = int(parse_qs(urlsplit(path).query)['page'][0])
            if self.action_pages is not None:
                return copy.deepcopy(self.action_pages[page])
            return {'total_count': len(self.action_artifacts),
                    'artifacts': copy.deepcopy(self.action_artifacts[(page - 1) * 100:page * 100])}
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

    def actions(self, expected, directory):
        self.action_run = {'id': expected.run_id, 'run_attempt': expected.run_attempt,
                           'head_sha': expected.head_sha, 'head_repository': {'full_name': expected.repository},
                           'status': 'in_progress', 'conclusion': None}
        self.action_artifacts = [{'id': 2001 + index, 'name': path.name, 'size_in_bytes': path.stat().st_size,
                                 'digest': 'sha256:' + snapshot.file_digest(path), 'expired': False}
                                for index, path in enumerate(sorted(directory.iterdir()))]


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class SnapshotTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Real recovery nests Git verification directories; keep fixture paths
        # short enough for Windows' default filesystem path limit.
        cls.temporary = short_fixture_directory(ROOT.parent)
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
        self.root = self.fixture / ('c' + uuid.uuid4().hex[:6])
        self.root.mkdir()
        self.output = self.root / 'ksu-source-bundle'
        self.api = FakeGitHub()
        self.expected = transfer.Expected('cyzrqh/gki', 123, 1, 'a' * 40, self.reference)

    def identity(self):
        return {field: self.manifest[field] for field in ('ref', 'git_tree', 'git_count', 'git_describe')}

    def local_official_fetch(self, directory, *arguments, **kwargs):
        if arguments[0] == 'fetch':
            self.assertEqual(('fetch', '--quiet', '--no-recurse-submodules', '--tags', 'origin', self.reference), arguments)
            # A real bundle transport avoids Git for Windows spawning sh for
            # local upload-pack in restricted runners; Git/history stay real.
            return snapshot.git(directory, 'fetch', '--quiet', '--tags', str(self.bundle / snapshot.BUNDLE_NAME), self.reference)
        return snapshot.git(directory, *arguments, **kwargs)

    def cli_environment(self, variant='SukiSU'):
        return {'GITHUB_REPOSITORY': self.expected.repository, 'GITHUB_RUN_ID': '123',
                'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': self.expected.head_sha,
                'KSU_VARIANT': variant, 'GH_TOKEN': '', 'GITHUB_TOKEN': ''}

    def test_restore_official_cli_restores_real_same_source_without_release_api_or_token(self):
        for variant in ('SukiSU', 'BakaSU'):
            parent = self.root / variant[0]
            parent.mkdir()
            output = parent / 'ksu-source-bundle'
            with self.subTest(variant=variant), mock.patch.dict(os.environ, self.cli_environment(variant)), \
                    mock.patch.object(transfer, 'GitHub', side_effect=AssertionError('No Release API allowed')) as api, \
                    mock.patch.object(transfer, 'git', side_effect=self.local_official_fetch) as upstream, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                status = transfer.main(['restore-official', '--output', str(output),
                    '--prepared-identity', json.dumps(self.identity()), '--expected-ref', self.reference,
                    '--root', str(self.root), '--susfs', 'false', '--tier', 'Latest(最新)'])
            self.assertEqual(status, 0)
            self.assertTrue(json.loads(stdout.getvalue())['official_fallback'])
            api.assert_not_called()
            self.assertIn(('remote', 'add', 'origin', 'https://github.com/' +
                           ('SukiSU-Ultra/SukiSU-Ultra' if variant == 'SukiSU' else 'Baka-SU/BakaSU') + '.git'),
                          [call.args[1:] for call in upstream.call_args_list])
            restored = checkout.checkout_snapshot(output, self.root / ('verified-' + variant),
                                                  self.reference, variant=variant)
            self.assertEqual(self.identity(), {field: restored[field] for field in self.identity()})
        self.assertEqual([], self.api.calls)

    def test_restore_official_cli_mismatched_prepare_fails_without_cache_replacement_or_api(self):
        self.output.mkdir()
        previous = self.output / snapshot.BUNDLE_NAME
        previous.write_bytes(b'original cached evidence')
        with mock.patch.dict(os.environ, self.cli_environment()), \
                mock.patch.object(transfer, 'GitHub', side_effect=AssertionError('No Release API allowed')) as api, \
                mock.patch.object(transfer, 'git', side_effect=self.local_official_fetch), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            status = transfer.main(['restore-official', '--output', str(self.output),
                '--prepared-identity', json.dumps({**self.identity(), 'git_count': 999}),
                '--expected-ref', self.reference, '--root', str(self.root)])
        self.assertEqual(1, status)
        self.assertIn('changed since prepare', stderr.getvalue())
        self.assertEqual(b'original cached evidence', previous.read_bytes())
        api.assert_not_called()

    def test_verify_cache_cli_binds_real_bundle_and_manifest_to_prepare_without_api(self):
        for variant, source in (('SukiSU', self.bundle), ('BakaSU', self.baka_bundle())):
            with self.subTest(variant=variant), mock.patch.dict(os.environ, self.cli_environment(variant)), \
                    mock.patch.object(transfer, 'GitHub', side_effect=AssertionError('No Release API allowed')) as api, \
                    mock.patch.object(transfer, 'restore_official') as fallback, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                status = transfer.main(['verify-cache', '--source', str(source),
                    '--prepared-identity', json.dumps(self.identity()), '--expected-ref', self.reference])
            self.assertEqual(0, status)
            self.assertEqual({'verified': True, 'ksu_ref': self.reference, 'variant': variant},
                             json.loads(stdout.getvalue()))
            api.assert_not_called()
            fallback.assert_not_called()

    def test_verify_cache_rejects_tampered_hit_without_upstream_fallback(self):
        self.output.mkdir()
        for filename in transfer.FILES:
            shutil.copyfile(self.bundle / filename, self.output / filename)
        manifest_path = self.output / snapshot.MANIFEST_NAME
        original_manifest = manifest_path.read_bytes()
        bundle_path = self.output / snapshot.BUNDLE_NAME
        original_bundle = bundle_path.read_bytes()
        for field, value in [('git_tree', 'b' * 40), ('git_count', 999), ('git_describe', 'tampered-version')]:
            changed = json.loads(original_manifest)
            changed[field] = value
            manifest_path.write_text(json.dumps(changed))
            with self.subTest(field=field), mock.patch.dict(os.environ, self.cli_environment()), \
                    mock.patch.object(transfer, 'GitHub', side_effect=AssertionError('No Release API allowed')) as api, \
                    mock.patch.object(transfer, 'restore_official') as fallback, \
                    mock.patch.object(transfer, 'git') as upstream, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                status = transfer.main(['verify-cache', '--source', str(self.output),
                    '--prepared-identity', json.dumps(self.identity()), '--expected-ref', self.reference])
            self.assertEqual(1, status)
            self.assertIn('differs from this prepare', stderr.getvalue())
            fallback.assert_not_called()
            upstream.assert_not_called()
            api.assert_not_called()
        manifest_path.write_bytes(original_manifest)
        bundle_path.write_bytes(original_bundle + b'corrupt bytes')
        with mock.patch.dict(os.environ, self.cli_environment()), \
                mock.patch.object(transfer, 'GitHub', side_effect=AssertionError('No Release API allowed')) as api, \
                mock.patch.object(transfer, 'restore_official') as fallback, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            status = transfer.main(['verify-cache', '--source', str(self.output),
                '--prepared-identity', json.dumps(self.identity()), '--expected-ref', self.reference])
        self.assertEqual(1, status)
        self.assertIn('bundle does not match', stderr.getvalue())
        fallback.assert_not_called()
        api.assert_not_called()

    def test_verify_cache_rejects_cross_variant_wrong_ref_and_unknown_files(self):
        self.output.mkdir()
        for filename in transfer.FILES:
            shutil.copyfile(self.bundle / filename, self.output / filename)
        for reference, variant in ((self.reference, 'BakaSU'), ('f' * 40, 'SukiSU'), ('main', 'SukiSU')):
            with self.subTest(reference=reference, variant=variant), mock.patch.dict(os.environ, self.cli_environment(variant)), \
                    mock.patch.object(transfer, 'GitHub', side_effect=AssertionError('No Release API allowed')) as api, \
                    contextlib.redirect_stderr(io.StringIO()):
                status = transfer.main(['verify-cache', '--source', str(self.output),
                    '--prepared-identity', json.dumps(self.identity()), '--expected-ref', reference])
            self.assertEqual(1, status)
            api.assert_not_called()
        keep = self.output / 'unrelated.json'
        keep.write_text('preserve')
        with self.assertRaisesRegex(transfer.TransferError, 'only the two'):
            transfer.verify_cache(self.output, self.identity(), self.reference)
        self.assertEqual('preserve', keep.read_text())

    def test_verify_cache_strictly_compares_susfs_target_presence_and_identity(self):
        susfs = {'android14-6.1': {'ref': 'a' * 40, 'tree': 'b' * 40,
                                  'branch': 'gki-android14-6.1', 'version': 'v2.3.0',
                                  'date': '2026-10-27 00:00:00 +0000'}}
        self.output.mkdir()
        for filename in transfer.FILES:
            shutil.copyfile(self.bundle / filename, self.output / filename)
        with self.assertRaisesRegex(transfer.TransferError, 'differs from this prepare'):
            transfer.verify_cache(self.output, {**self.identity(), 'susfs_sources': susfs}, self.reference)
        manifest_path = self.output / snapshot.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        manifest['susfs_sources'] = susfs
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(transfer.TransferError, 'differs from this prepare'):
            transfer.verify_cache(self.output, self.identity(), self.reference)

    def test_missing_draft_recovers_exact_official_commit_and_native_version(self):
        with mock.patch.object(transfer, 'git', side_effect=self.local_official_fetch):
            result = transfer.restore(901, self.output, self.expected, self.api,
                                      identity=self.identity(), root=self.root)
        self.assertTrue(result['official_fallback'])
        restored = checkout.checkout_snapshot(self.output, self.root / 'restored', self.reference)
        self.assertEqual(self.identity(), {field: restored[field] for field in self.identity()})
        self.assertFalse(any(call[0] in ('DELETE', 'POST') for call in self.api.calls))

    def test_official_recovery_revalidates_susfs_before_installing_cache(self):
        with mock.patch.object(transfer, 'git', side_effect=self.local_official_fetch), \
                mock.patch.object(snapshot, 'verify_susfs_compatibility') as verify:
            transfer.restore(901, self.output, self.expected, self.api, identity=self.identity(),
                             root=self.root, susfs=True, tier='Latest(最新)')
        self.assertEqual(1, verify.call_count)
        self.assertEqual((self.root.resolve(), 'Latest(最新)', True), verify.call_args.args[1:4])

    def test_missing_backup_without_prepare_identity_never_fetches_upstream(self):
        with mock.patch.object(transfer, 'git') as upstream:
            with self.assertRaises(transfer.BackupUnavailable):
                transfer.restore(901, self.output, self.expected, self.api)
        upstream.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_bad_backup_hash_or_foreign_owner_is_not_hidden_by_official_recovery(self):
        self.create()
        with mock.patch.object(transfer, 'restore_official') as upstream:
            self.api.bad_download = True
            with self.assertRaisesRegex(transfer.TransferError, 'SHA256'):
                transfer.restore(901, self.output, self.expected, self.api, identity=self.identity())
            self.api.bad_download = False
            self.api.releases[901]['target_commitish'] = 'b' * 40
            with self.assertRaisesRegex(transfer.TransferError, 'temporary draft'):
                transfer.restore(901, self.output, self.expected, self.api, identity=self.identity())
        upstream.assert_not_called()

    def test_official_recovery_refuses_tree_history_or_version_drift(self):
        for changes in ({'git_tree': 'b' * 40}, {'git_count': 999}, {'git_describe': 'different-tag'}):
            with self.subTest(changes=changes), \
                    mock.patch.object(transfer, 'git', side_effect=self.local_official_fetch):
                with self.assertRaisesRegex(transfer.TransferError, 'changed since prepare'):
                    transfer.restore(901, self.output, self.expected, self.api,
                                     identity={**self.identity(), **changes}, root=self.root)
                self.assertFalse(self.output.exists())

    def test_invalid_prepare_identity_and_unknown_local_files_are_preserved_without_fetch(self):
        for changes in ({'ref': 'f' * 40}, {'git_count': True}, {'git_tree': '../unsafe'},
                        {'git_describe': 'unsafe\nversion'}, {'extra': 'unexpected'}):
            with self.subTest(changes=changes), mock.patch.object(transfer, 'git') as upstream:
                with self.assertRaisesRegex(transfer.TransferError, 'exact source and native version identity'):
                    transfer.restore(901, self.output, self.expected, self.api,
                                     identity={**self.identity(), **changes}, root=self.root)
                upstream.assert_not_called()
        self.output.mkdir()
        keep = self.output / 'user-owned.txt'
        keep.write_text('preserve')
        with mock.patch.object(transfer, 'git') as upstream:
            with self.assertRaisesRegex(transfer.TransferError, 'unknown files'):
                transfer.restore(901, self.output, self.expected, self.api, identity=self.identity(), root=self.root)
            upstream.assert_not_called()
        self.assertEqual('preserve', keep.read_text())

    def test_temporarily_unavailable_backup_can_recover_but_integrity_errors_cannot(self):
        with mock.patch.object(self.api, 'json', side_effect=transfer.BackupUnavailable('HTTP 503')), \
                mock.patch.object(transfer, 'git', side_effect=self.local_official_fetch):
            self.assertTrue(transfer.restore(901, self.output, self.expected, self.api,
                                            identity=self.identity(), root=self.root)['official_fallback'])

    def test_second_download_disappearing_cleans_partial_recovery_and_uses_exact_source(self):
        self.create()
        self.output.mkdir()
        (self.output / snapshot.BUNDLE_NAME).write_bytes(b'previous partial cache')
        original = self.api.download
        calls = []
        def disappearing(asset_id, limit):
            calls.append(asset_id)
            if len(calls) == 2:
                raise transfer.BackupUnavailable('asset HTTP 404')
            return original(asset_id, limit)
        with mock.patch.object(self.api, 'download', side_effect=disappearing), \
                mock.patch.object(transfer, 'git', side_effect=self.local_official_fetch):
            self.assertTrue(transfer.restore(901, self.output, self.expected, self.api,
                                            identity=self.identity(), root=self.root)['official_fallback'])
        self.assertEqual(2, len(calls))
        self.assertEqual(self.identity(), {field: checkout.read_manifest(self.output, self.reference)[field]
                                          for field in self.identity()})
        self.assertFalse(list(self.root.glob('.gki-source-recovery-*')))

    def test_tag_drift_retains_previous_cache_and_official_fetch_has_bounded_retries(self):
        self.output.mkdir()
        previous = self.output / snapshot.BUNDLE_NAME
        previous.write_bytes(b'original cache evidence')
        calls = []
        def changed_tags(directory, *arguments, **kwargs):
            if arguments[0] == 'fetch':
                calls.append(arguments)
                if len(calls) < 3:
                    raise snapshot.SnapshotError('temporary fetch failure')
                result = self.local_official_fetch(directory, *arguments, **kwargs)
                snapshot.git(directory, 'tag', 'new-official-tag', self.reference)
                return result
            return self.local_official_fetch(directory, *arguments, **kwargs)
        with mock.patch.object(transfer, 'git', side_effect=changed_tags), mock.patch.object(transfer.time, 'sleep') as wait:
            with self.assertRaisesRegex(transfer.TransferError, 'changed since prepare'):
                transfer.restore(901, self.output, self.expected, self.api, identity=self.identity(), root=self.root)
        self.assertEqual(3, len(calls))
        self.assertEqual([mock.call(1), mock.call(2)], wait.call_args_list)
        self.assertEqual(b'original cache evidence', previous.read_bytes())

    def create(self):
        return transfer.create(self.bundle, self.expected, self.api)

    def baka_bundle(self):
        # Both official variants can contain the same Git commit. Distinguish
        # their origin manifests and backup controls even in that case.
        directory = self.root / 'baka-bundle'
        directory.mkdir()
        shutil.copyfile(self.bundle / snapshot.BUNDLE_NAME, directory / snapshot.BUNDLE_NAME)
        (directory / snapshot.MANIFEST_NAME).write_text(json.dumps({
            **self.manifest, 'repository': 'Baka-SU/BakaSU'}, sort_keys=True), encoding='utf-8')
        return directory

    def test_bakasu_backup_restores_its_selected_repository_and_full_history(self):
        expected = replace(self.expected, variant='BakaSU')
        transfer.create(self.baka_bundle(), expected, self.api)
        control = json.loads(self.api.releases[901]['body'][len(transfer.MARKER):])
        self.assertEqual('BakaSU', control['manager_variant'])
        transfer.restore(901, self.output, expected, self.api)
        destination = self.root / 'baka-checkout'
        manifest = checkout.checkout_snapshot(self.output, destination, self.reference, variant='BakaSU')
        self.assertEqual('Baka-SU/BakaSU', manifest['repository'])
        self.assertEqual(self.reference, self.git(destination, 'rev-parse', 'HEAD'))
        self.assertEqual('2', self.git(destination, 'rev-list', '--count', 'HEAD'))
        self.assertEqual('https://github.com/Baka-SU/BakaSU.git', self.git(destination, 'remote', 'get-url', 'origin'))

    def test_backup_variant_discriminator_prevents_cross_variant_restore_and_fallback(self):
        baka = replace(self.expected, variant='BakaSU')
        for source, original, requested in ((self.bundle, self.expected, baka),
                                             (self.baka_bundle(), baka, self.expected)):
            self.api = FakeGitHub()
            transfer.create(source, original, self.api)
            with self.subTest(original=original.variant, requested=requested.variant), \
                    mock.patch.object(transfer, 'restore_official') as upstream, \
                    self.assertRaisesRegex(transfer.TransferError, 'different repository, run, attempt'):
                transfer.restore(901, self.output, requested, self.api, identity=self.identity())
            upstream.assert_not_called()
            self.assertFalse(self.output.exists())
            self.assertFalse(any(call[0] == 'DOWNLOAD' for call in self.api.calls))

    def test_bakasu_official_recovery_uses_only_bakasu_origin_and_no_sukisu_port(self):
        expected = replace(self.expected, variant='BakaSU')
        with mock.patch.object(transfer, 'git', side_effect=self.local_official_fetch) as upstream, \
                mock.patch.object(snapshot, 'verify_susfs_compatibility') as sukisu_port:
            result = transfer.restore(901, self.output, expected, self.api, identity=self.identity(),
                                      root=self.root, susfs=True, tier='Latest(最新)')
        self.assertTrue(result['official_fallback'])
        sukisu_port.assert_not_called()
        self.assertIn(('remote', 'add', 'origin', 'https://github.com/Baka-SU/BakaSU.git'),
                      [call.args[1:] for call in upstream.call_args_list])
        self.assertEqual('Baka-SU/BakaSU', checkout.read_manifest(self.output, self.reference,
                                                               variant='BakaSU')['repository'])

    def test_bakasu_cli_selects_variant_from_environment_and_never_deletes_public_release(self):
        expected = replace(self.expected, variant='BakaSU')
        environment = {'GITHUB_REPOSITORY': expected.repository, 'GITHUB_RUN_ID': '123',
                       'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': expected.head_sha, 'KSU_VARIANT': 'BakaSU'}
        with mock.patch.dict(os.environ, environment), mock.patch.object(transfer, 'GitHub', return_value=self.api), \
                contextlib.redirect_stdout(io.StringIO()):
            status = transfer.main(['create', '--source', str(self.baka_bundle()), '--expected-ref', self.reference])
        self.assertEqual(0, status)
        self.api.published(expected)
        self.api.releases[901]['draft'] = False
        with self.assertRaisesRegex(transfer.TransferError, 'temporary draft'):
            transfer.delete(901, 'gki-123-1', expected, self.api)
        self.assertFalse(any(call[0] == 'DELETE' for call in self.api.calls))
        self.api.releases[901]['draft'] = True
        self.assertTrue(transfer.delete(901, 'gki-123-1', expected, self.api)['deleted'])
        self.assertIn('published', self.api.releases)
        self.assertEqual([('DELETE', '/releases/901', None)], [call for call in self.api.calls if call[0] == 'DELETE'])

    def actions_assets(self, *, kernel=True):
        directory = self.root / 'assets'
        directory.mkdir()
        # These test bodies prove byte/metadata binding, never installability.
        outputs = {'SukiSU_v4.2.1-r2_41070-release.apk': b'fixture native APK bytes'}
        if kernel:
            outputs.update({'android12-5.10.236-2025-05-AnyKernel3.zip': b'fixture native AK3 bytes',
                            'android12-5.10.236-2025-05-boot.img': b'fixture native boot bytes'})
        for name, raw in outputs.items():
            (directory / name).write_bytes(raw)
        checksums = ''.join(hashlib.sha256(raw).hexdigest() + '  ' + name + '\n'
                            for name, raw in sorted(outputs.items()))
        (directory / 'SHA256SUMS.txt').write_text(checksums, encoding='utf-8', newline='\n')
        self.api.actions(self.expected, directory)
        return directory

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

    def versioned_publication(self, expected=None, *, tag='r1.0', repository=None):
        expected = expected or self.expected
        self.api.published(expected)
        published = self.api.releases['published']
        published['tag_name'] = tag
        published['body'] = (f'<!-- gki-release-run:v1 repository={repository or expected.repository} '
                             f'run_id={expected.run_id} run_attempt={expected.run_attempt} '
                             f'head_sha={expected.head_sha} -->\n\nKernel release\n')
        return published

    def test_versioned_release_cleans_only_exact_owned_source_draft_case_insensitively(self):
        self.create()
        original_tag = self.api.releases[901]['tag_name']
        self.versioned_publication(repository=self.expected.repository.upper())
        self.assertTrue(transfer.delete(901, 'r1.0', self.expected, self.api)['deleted'])
        self.assertTrue(original_tag.startswith('gki-internal-source-123-1-'))
        self.assertEqual('r1.0', self.api.releases['published']['tag_name'])
        self.assertEqual([('DELETE', '/releases/901', None)],
                         [call for call in self.api.calls if call[0] == 'DELETE'])

    def test_versioned_release_invalid_or_foreign_marker_cannot_unlock_draft_deletion(self):
        self.create()
        original = copy.deepcopy(self.versioned_publication())
        body = original['body']
        changes = ({'body': None}, {'body': 'no marker'}, {'body': body + body},
                   {'body': body.replace(self.expected.repository, 'foreign/repository')},
                   {'body': body.replace('run_id=123', 'run_id=124')},
                   {'body': body.replace('run_attempt=1', 'run_attempt=2')},
                   {'body': body.replace('head_sha=' + self.expected.head_sha, 'head_sha=' + 'e' * 40)},
                   {'target_commitish': 'e' * 40})
        for change in changes:
            self.api.releases['published'] = {**original, **change}
            with self.subTest(change=change), self.assertRaises(transfer.TransferError):
                transfer.delete(901, 'r1.0', self.expected, self.api)
            self.assertIn(901, self.api.releases)
            self.assertFalse(any(call[0] == 'DELETE' for call in self.api.calls))

    def test_versioned_later_attempt_can_cleanup_earlier_backup_but_old_marker_cannot(self):
        self.create()
        expected = replace(self.expected, run_attempt=2)
        self.versioned_publication()
        with self.assertRaisesRegex(transfer.TransferError, 'exact published build tag'):
            transfer.delete(901, 'r1.0', expected, self.api)
        self.versioned_publication(expected, tag='r1.1')
        self.assertTrue(transfer.delete(901, 'r1.1', expected, self.api)['deleted'])

    def test_actions_cleanup_matches_native_files_and_deletes_only_its_private_draft(self):
        self.create()
        directory = self.actions_assets()
        self.api.published(self.expected)
        result = transfer.delete_actions(901, directory, self.expected, self.api)
        self.assertTrue(result['deleted'])
        self.assertEqual(4, result['verified_actions_files'])
        self.assertIn('published', self.api.releases)
        self.assertEqual([('DELETE', '/releases/901', None)], [call for call in self.api.calls if call[0] == 'DELETE'])
        self.assertFalse(any('/releases/tags/' in call[1] for call in self.api.calls))
        self.assertTrue(transfer.delete_actions(901, directory, self.expected, self.api)['already_absent'])

    def test_apk_only_actions_cleanup_cli_uses_native_assets_without_any_public_release(self):
        self.create()
        directory = self.actions_assets(kernel=False)
        environment = {'GITHUB_REPOSITORY': self.expected.repository, 'GITHUB_RUN_ID': '123',
                       'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': self.expected.head_sha}
        with mock.patch.dict(os.environ, environment), mock.patch.object(transfer, 'GitHub', return_value=self.api):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = transfer.main(['delete-actions', '--release-id', '901', '--expected-ref', self.reference,
                                        '--assets-directory', str(directory)])
        self.assertEqual(0, status)
        self.assertEqual(2, json.loads(output.getvalue())['verified_actions_files'])
        self.assertFalse(self.api.releases)

    def test_actions_cleanup_rejects_different_run_repository_head_attempt_and_failed_run(self):
        self.create()
        directory = self.actions_assets()
        original = copy.deepcopy(self.api.action_run)
        for change in ({'id': 124}, {'id': True}, {'run_attempt': 2}, {'run_attempt': True},
                       {'head_sha': 'b' * 40}, {'head_repository': {'full_name': 'other/gki'}},
                       {'status': 'queued'}, {'status': 'completed', 'conclusion': 'failure'},
                       {'status': 'completed', 'conclusion': 'cancelled'}):
            with self.subTest(change=change):
                self.api.action_run = {**original, **change}
                with self.assertRaisesRegex(transfer.TransferError, 'run repository, ID, attempt, head or state'):
                    transfer.delete_actions(901, directory, self.expected, self.api)
                self.assertIn(901, self.api.releases)
        self.assertFalse(any(call[0] == 'DELETE' for call in self.api.calls))

    def test_actions_cleanup_rejects_missing_wrong_size_digest_expired_or_duplicate_original(self):
        self.create()
        directory = self.actions_assets()
        original = copy.deepcopy(self.api.action_artifacts)
        variants = [original[:-1]]
        for change in ({'size_in_bytes': 1}, {'size_in_bytes': True}, {'digest': 'sha256:' + 'b' * 64},
                       {'expired': True}, {'name': 'Release-Manager-wrapper'}):
            variant = copy.deepcopy(original)
            variant[0].update(change)
            variants.append(variant)
        variants.append([*original, {**original[0], 'id': 2999}])
        variants.append([*original, {**original[0], 'id': 2999, 'name': 'unexpected-native.apk'}])
        for index, variant in enumerate(variants):
            with self.subTest(index=index):
                self.api.action_artifacts = variant
                with self.assertRaises(transfer.TransferError):
                    transfer.delete_actions(901, directory, self.expected, self.api)
                self.assertIn(901, self.api.releases)
        self.assertFalse(any(call[0] == 'DELETE' for call in self.api.calls))

    def test_actions_cleanup_finishes_every_inventory_page_and_refuses_incomplete_inventory(self):
        self.create()
        directory = self.actions_assets()
        # Unrelated diagnostic records do not alter the required native file
        # list or become deletion targets, but their pages must be inspected.
        self.api.action_artifacts += [{'id': 3000 + n, 'name': 'diagnostic-' + str(n)} for n in range(101)]
        self.assertTrue(transfer.delete_actions(901, directory, self.expected, self.api)['deleted'])
        self.assertTrue(any(call[1].endswith('/artifacts?per_page=100&page=2') for call in self.api.calls))
        self.create()
        self.api.calls.clear()
        self.api.action_pages = {1: {'total_count': 105, 'artifacts': self.api.action_artifacts[:99]}}
        with self.assertRaisesRegex(transfer.TransferError, 'incomplete'):
            transfer.delete_actions(901, directory, self.expected, self.api)
        self.assertFalse(any(call[0] == 'DELETE' for call in self.api.calls))

    def test_actions_cleanup_validates_checksum_inventory_and_refuses_wrappers_or_subdirectories(self):
        self.create()
        directory = self.actions_assets()
        sums = directory / 'SHA256SUMS.txt'
        original = sums.read_bytes()
        for raw in (b'', original.splitlines(keepends=True)[0], original + original,
                    original.replace(original[:64], b'0' * 64, 1)):
            with self.subTest(raw=raw[:8]):
                sums.write_bytes(raw)
                with self.assertRaises(transfer.TransferError):
                    transfer.delete_actions(901, directory, self.expected, self.api)
        sums.write_bytes(original)
        for name in ('Release-Manager.zip', 'manager.json'):
            unknown = directory / name
            unknown.write_bytes(b'private intermediate wrapper')
            with self.assertRaisesRegex(transfer.TransferError, 'must be native'):
                transfer.delete_actions(901, directory, self.expected, self.api)
            unknown.unlink()
        nested = directory / 'nested'
        nested.mkdir()
        with self.assertRaisesRegex(transfer.TransferError, 'must be native'):
            transfer.delete_actions(901, directory, self.expected, self.api)
        self.assertFalse(any(call[0] == 'DELETE' for call in self.api.calls))

    def test_actions_cleanup_revalidates_attempt_and_source_and_never_deletes_published_or_foreign_release(self):
        self.create()
        directory = self.actions_assets()
        with self.assertRaisesRegex(transfer.TransferError, 'different repository, run, attempt, head or source'):
            transfer.delete_actions(901, directory, replace(self.expected, ksu_ref='c' * 40), self.api)
        original = copy.deepcopy(self.api.releases[901])
        for change in ({'draft': False}, {'published_at': '2026-10-25T00:00:00Z'},
                       {'target_commitish': 'c' * 40}, {'name': 'someone else'}, {'body': '{}'}):
            with self.subTest(change=change):
                self.api.releases[901] = {**original, **change}
                with self.assertRaises(transfer.TransferError):
                    transfer.delete_actions(901, directory, self.expected, self.api)
        self.api.releases[901] = original
        base_json = self.api.json
        reads = []
        def change_attempt(path, **kwargs):
            result = base_json(path, **kwargs)
            if path == '/actions/runs/123':
                reads.append(path)
                if len(reads) == 2:
                    result['run_attempt'] = 2
            return result
        with mock.patch.object(self.api, 'json', side_effect=change_attempt):
            with self.assertRaisesRegex(transfer.TransferError, 'run repository, ID, attempt, head or state'):
                transfer.delete_actions(901, directory, self.expected, self.api)
        self.assertEqual(2, len(reads))
        self.assertFalse(any(call[0] == 'DELETE' for call in self.api.calls))

    def test_actions_cleanup_can_remove_earlier_prepare_backup_only_in_verified_current_rerun(self):
        self.create()
        directory = self.actions_assets(kernel=False)
        expected = replace(self.expected, run_attempt=2)
        self.api.actions(expected, directory)
        self.api.action_run.update(status='completed', conclusion='success')
        self.assertTrue(transfer.delete_actions(901, directory, expected, self.api)['deleted'])

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
    def test_only_missing_or_transient_get_errors_are_recoverable(self):
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'private-token'}):
            for status in (401, 403, 404, 408, 429, 500, 502, 503, 504):
                opener = mock.Mock()
                opener.open.side_effect = HTTPError('https://api.github.com/', status, 'fixture', {}, io.BytesIO())
                api = transfer.GitHub('cyzrqh/gki', opener=opener)
                with self.subTest(status=status), self.assertRaises(transfer.TransferError) as error:
                    api.json('/releases/901')
                self.assertEqual(status not in (401, 403), isinstance(error.exception, transfer.BackupUnavailable))
            opener.open.side_effect = HTTPError('https://api.github.com/', 404, 'fixture', {}, io.BytesIO())
            self.assertIsNone(api.json('/releases/901', missing=True))

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
