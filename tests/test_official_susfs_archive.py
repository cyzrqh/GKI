"""Real-Git and hostile-archive checks for the official API fallback."""
import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import official_susfs_archive as archive
from snapshot_ksu_source import SnapshotError, temporary_directory


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class OfficialArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory(ROOT.parent, '.oa-')
        self.root = self.temporary.__enter__()
        self.addCleanup(self.temporary.__exit__, None, None, None)
        self.source = self.root / 'manager'
        self.source.mkdir()
        archive.git(self.source, 'init', '--quiet')
        (self.source / 'native').write_bytes(b'unchanged native manager\n')
        archive.git(self.source, 'add', '.')
        archive.git(self.source, '-c', 'user.name=Native', '-c', 'user.email=native@example.invalid',
                    'commit', '--quiet', '-m', 'native source')
        self.manager_ref = archive.git(self.source, 'rev-parse', 'HEAD')
        self.target = 'android12-5.10'
        self.document = {'id': 'a' * 40, 'parent_ids': ['b' * 40],
                         'author_name': 'Official Author', 'author_email': 'author@example.invalid',
                         'committer_name': 'Official Committer', 'committer_email': 'committer@example.invalid',
                         'authored_date': '2026-10-19T23:59:56.000+08:00',
                         'committed_date': '2026-10-20T01:02:03.000+08:00',
                         'message': 'Official fixture\n\nFull message preserved.\n'}
        self.entries = {
            'kernel_patches/include/linux/susfs.h': (b'#define SUSFS_VERSION "v2.3.0"\n', 0o644),
            'kernel_patches/50_add_susfs_in_gki-' + self.target + '.patch': (b'patch\n', 0o644),
            '.gitattributes': (b'* text eol=crlf\n', 0o644),
            'run.sh': (b'#!/bin/sh\necho exact\n', 0o755),
            'crlf.bin': (b'exact\r\nbytes\x00\xff', 0o644),
        }
        self.payload = self.tar(self.entries)
        original = self.root / 'original'
        files = archive.extract_archive(self.payload, original)
        archive.git(original, 'init', '--quiet')
        (original / '.git/info/attributes').write_text('* -text -filter -ident -working-tree-encoding\n')
        archive.git(original, 'add', '--force', '--all', '--', '.')
        for path, executable in files.items():
            archive.git(original, 'update-index', '--chmod=+x' if executable else '--chmod=-x', '--', path)
        self.tree = archive.git(original, 'write-tree')
        raw = archive.original_commit(self.document, self.tree)
        self.document['id'] = archive.git(original, 'hash-object', '-t', 'commit', '--stdin', data=raw)
        self.reference = self.document['id']

    @staticmethod
    def tar(entries, extra=()):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as tar:
            top = tarfile.TarInfo('susfs-source/')
            top.type = tarfile.DIRTYPE
            tar.addfile(top)
            for name, (content, mode) in entries.items():
                info = tarfile.TarInfo('susfs-source/' + name)
                info.size, info.mode = len(content), mode
                tar.addfile(info, io.BytesIO(content))
            for info, content in extra:
                tar.addfile(info, io.BytesIO(content) if content is not None else None)
        return output.getvalue()

    def imported(self):
        with (mock.patch.object(archive, 'api_commit', return_value=self.document),
              mock.patch.object(archive, 'request_bytes', return_value=self.payload)):
            return archive.import_archive(self.source, self.target, self.reference)

    def test_exact_upstream_sha_tree_modes_and_original_message_survive_offline_transport(self):
        metadata = self.imported()
        self.assertEqual(self.reference, metadata['ref'])
        self.assertEqual(self.tree, metadata['tree'])
        self.assertNotEqual(metadata['ref'], metadata['transport_ref'])
        self.assertEqual('2026-10-20 01:02:03 +0800', metadata['date'])
        self.assertEqual('v2.3.0', metadata['version'])
        self.assertEqual(self.manager_ref, archive.git(self.source, 'rev-parse', 'HEAD'))
        self.assertEqual('false', archive.git(self.source, 'rev-parse', '--is-shallow-repository'))
        self.assertEqual('', archive.git(self.source, 'status', '--porcelain'))
        bundle = self.root / 'source.bundle'
        archive.git(self.source, 'bundle', 'create', str(bundle), 'HEAD', 'refs/gki-susfs/' + self.target)
        worker = self.root / 'worker'
        worker.mkdir()
        archive.git(worker, 'init', '--quiet')
        archive.git(worker, 'bundle', 'verify', str(bundle))
        archive.git(worker, 'fetch', '--quiet', str(bundle), 'refs/gki-susfs/' + self.target)
        with mock.patch.object(archive, 'request_bytes', side_effect=AssertionError('offline consumer')):
            archive.hydrate_carrier(worker, metadata['transport_ref'], metadata)
        archive.git(worker, 'checkout', '--quiet', '--detach', self.reference)
        archive.git(worker, 'fsck', '--connectivity-only', '--no-dangling')
        self.assertEqual(self.reference, archive.git(worker, 'rev-parse', 'HEAD'))
        self.assertEqual(self.tree, archive.git(worker, 'rev-parse', 'HEAD^{tree}'))
        self.assertEqual('true', archive.git(worker, 'rev-parse', '--is-shallow-repository'))
        for name, (content, mode) in self.entries.items():
            self.assertEqual(content, (worker / name).read_bytes())
            self.assertEqual('100755' if mode == 0o755 else '100644',
                             archive.git(worker, 'ls-tree', 'HEAD', name).split()[0])
        raw = archive.git_bytes(worker, 'cat-file', 'commit', self.reference)
        self.assertTrue(raw.endswith(self.document['message'].encode()))

    def test_carrier_is_deterministic_and_not_an_upstream_commit(self):
        first = self.imported()
        self.assertEqual(first, self.imported())
        raw = archive.git_bytes(self.source, 'cat-file', 'commit', first['transport_ref'])
        self.assertNotIn(b'\nparent ', raw)
        self.assertIn(archive.CARRIER_MESSAGE, raw)

    def test_wrong_original_hash_or_tree_fails_without_modifying_manager(self):
        self.document['id'] = 'a' * 40
        with self.assertRaisesRegex(SnapshotError, 'original commit SHA'):
            self.imported()
        self.assertEqual(self.manager_ref, archive.git(self.source, 'rev-parse', 'HEAD'))
        with self.assertRaises(SnapshotError):
            archive.git(self.source, 'rev-parse', '--verify', 'refs/gki-susfs/' + self.target)

    def test_hydration_refuses_manager_head_and_changed_prepare_identity(self):
        metadata = self.imported()
        with self.assertRaisesRegex(SnapshotError, 'existing source HEAD'):
            archive.hydrate_carrier(self.source, metadata['transport_ref'], metadata)
        self.assertFalse((self.source / '.git/shallow').exists())
        changed = dict(metadata, version='v9.9.9')
        with self.assertRaisesRegex(SnapshotError, 'successful prepare job'):
            archive.hydrate_carrier(self.source, metadata['transport_ref'], changed)

    def test_safe_extraction_rejects_traversal_duplicate_symlink_hardlink_and_specials(self):
        bad = []
        for name in ('susfs-source/../escape', '/absolute', 'susfs-source/.git/config',
                     'susfs-source/a\\b', 'other-root/file', 'susfs-source/run.sh'):
            entry = tarfile.TarInfo(name)
            entry.size = 1
            bad.append((entry, b'x'))
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            entry = tarfile.TarInfo('susfs-source/unsafe-' + kind.decode())
            entry.type, entry.linkname = kind, '/escape'
            bad.append((entry, None))
        for number, item in enumerate(bad):
            with self.subTest(path=item[0].name, kind=item[0].type), self.assertRaises(SnapshotError):
                archive.extract_archive(self.tar(self.entries, [item]), self.root / f'bad-{number}')
        self.assertFalse((self.root / 'escape').exists())

    def test_extraction_limits_and_empty_archives_fail(self):
        with mock.patch.object(archive, 'MAX_EXPANDED', 1), self.assertRaises(SnapshotError):
            archive.extract_archive(self.payload, self.root / 'large')
        with mock.patch.object(archive, 'MAX_FILES', 1), self.assertRaises(SnapshotError):
            archive.extract_archive(self.payload, self.root / 'many')
        with self.assertRaises(SnapshotError):
            archive.extract_archive(self.tar({}), self.root / 'empty')

    def test_commit_metadata_rejects_injection_fractional_dates_and_invalid_parents(self):
        for field, value in (('author_name', 'Author\ncommitter Evil'), ('author_email', 'a>b'),
                             ('committed_date', '2026-10-20T01:02:03.123+08:00'),
                             ('parent_ids', ['../bad']), ('parent_ids', [{}]), ('message', 'nul\x00')):
            changed = dict(self.document, **{field: value})
            with self.subTest(field=field), self.assertRaises(SnapshotError):
                archive.original_commit(changed, self.tree)

    def test_api_rejects_different_full_commit_and_custom_urls(self):
        with mock.patch.object(archive, 'request_bytes', return_value=json.dumps(self.document).encode()):
            with self.assertRaisesRegex(SnapshotError, 'different requested commit'):
                archive.api_commit('a' * 40, self.target)
            self.assertEqual(self.document, archive.api_commit('gki-' + self.target, self.target))
        for requested in ('https://third.invalid/source', '-cfoo', '../branch'):
            with self.subTest(requested=requested), self.assertRaises(SnapshotError):
                archive.api_commit(requested, self.target)

    def test_http_refuses_foreign_destinations_redirects_and_overlimit(self):
        for url in ('https://evil.invalid/source', 'http://gitlab.com/api/v4/projects/56471372/repository/x',
                    'https://gitlab.com.evil.invalid/api/v4/projects/56471372/repository/x'):
            with self.subTest(url=url), self.assertRaises(SnapshotError):
                archive.request_bytes(url, 10)
        class Response:
            status = 200
            headers = {'Content-Length': '100'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return archive.API + '/repository/x'
        opener = mock.Mock()
        opener.open.return_value = Response()
        with self.assertRaisesRegex(SnapshotError, 'exceeds'):
            archive.request_bytes(archive.API + '/repository/x', 10, opener=opener)
        sent = opener.open.call_args.args[0]
        self.assertNotIn('Authorization', sent.headers)

    def test_http_refuses_redirect_without_retry_or_credentials(self):
        url = archive.API + '/repository/x'
        opener = mock.Mock()
        opener.open.side_effect = HTTPError(url, 302, 'redirect', {'Location': 'https://evil.invalid/'}, None)
        with mock.patch.object(archive.time, 'sleep') as sleep, self.assertRaisesRegex(SnapshotError, 'HTTP 302'):
            archive.request_bytes(url, 100, opener=opener)
        self.assertEqual(1, opener.open.call_count)
        sleep.assert_not_called()
        self.assertNotIn('Authorization', opener.open.call_args.args[0].headers)

    def test_http_transient_failures_use_bounded_backoff_then_stop(self):
        url = archive.API + '/repository/x'
        for error in (HTTPError(url, 503, 'unavailable', {}, None), URLError('network unavailable')):
            opener = mock.Mock()
            opener.open.side_effect = error
            with self.subTest(kind=type(error).__name__), mock.patch.object(archive.time, 'sleep') as sleep:
                with self.assertRaises(SnapshotError):
                    archive.request_bytes(url, 100, opener=opener)
                self.assertEqual(3, opener.open.call_count)
                self.assertEqual([mock.call(5), mock.call(10)], sleep.call_args_list)

    def test_http_counts_actual_body_when_length_missing_and_checks_final_url(self):
        url = archive.API + '/repository/x'
        class Response:
            status = 200
            headers = {}
            def __init__(self, final):
                self.final = final
                self.body = io.BytesIO(b'x' * 11)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return self.final
            def read(self, size): return self.body.read(size)
        for final, pattern in ((url, 'exceeds'), ('https://evil.invalid/', 'redirected')):
            opener = mock.Mock()
            opener.open.return_value = Response(final)
            with self.subTest(final=final), self.assertRaisesRegex(SnapshotError, pattern):
                archive.request_bytes(url, 10, opener=opener)

    def test_truncated_gzip_and_expanded_tar_limit_fail_closed(self):
        with self.assertRaises(SnapshotError):
            archive.extract_archive(self.payload[:30], self.root / 'truncated')
        with (mock.patch.object(archive, 'MAX_EXPANDED', 1),
              mock.patch.object(archive, 'MAX_FILES', 0),
              self.assertRaises(SnapshotError)):
            archive.extract_archive(self.payload, self.root / 'bounded')


if __name__ == '__main__':
    unittest.main()
