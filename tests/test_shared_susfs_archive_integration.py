"""Real Git integration of official HTTP recovery with shared source snapshots."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import checkout_ksu_snapshot as checkout
import official_susfs_archive as archive
import pin_build_dependencies as pinning
import shared_susfs_source as shared
import snapshot_ksu_source as snapshot


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class SharedSusfsArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = snapshot.temporary_directory(ROOT.parent, '.sai-')
        self.root = self.temporary.__enter__()
        self.addCleanup(self.temporary.__exit__, None, None, None)
        self.source, self.upstream = self.root / 'manager', self.root / 'susfs'
        for path in (self.source, self.upstream):
            path.mkdir()
            self.git(path, 'init', '--quiet')
            self.git(path, 'config', 'user.name', 'Independent integration fixture')
            self.git(path, 'config', 'user.email', 'fixture@example.invalid')
            self.git(path, 'config', 'core.autocrlf', 'false')
        self.git(self.source, 'remote', 'add', 'origin', 'https://github.com/SukiSU-Ultra/SukiSU-Ultra.git')
        (self.source / 'kernel').mkdir()
        (self.source / 'kernel/Kbuild').write_text('native manager one\n', encoding='utf-8')
        self.commit(self.source, 'native manager initial')
        self.git(self.source, 'tag', 'v4.2.1')
        (self.source / 'kernel/Kbuild').write_text('native manager two\n', encoding='utf-8')
        self.commit(self.source, 'native manager latest')
        self.reference = self.git(self.source, 'rev-parse', 'HEAD')
        self.targets = ('android12-5.10', 'android13-5.15')
        header = self.upstream / 'kernel_patches/include/linux/susfs.h'
        header.parent.mkdir(parents=True)
        header.write_text('#define SUSFS_VERSION "v2.3.0"\n', encoding='utf-8')
        for target in self.targets:
            (self.upstream / f'kernel_patches/50_add_susfs_in_gki-{target}.patch').write_text(
                'real archive fixture for ' + target + '\n', encoding='utf-8')
        (self.upstream / 'preserve-bytes.txt').write_bytes(b'CRLF\r\nfixture\r\n')
        (self.upstream / 'run.sh').write_bytes(b'#!/bin/sh\nprintf fixture\n')
        # POSIX Git refreshes index executable bits from the worktree on every
        # later add. Keep the physical mode consistent with the indexed mode;
        # update-index alone only happened to work with Windows filemode=false.
        (self.upstream / 'run.sh').chmod(0o755)
        if os.name != 'nt':
            self.git(self.upstream, 'config', 'core.filemode', 'true')
            self.assertEqual(0o755, (self.upstream / 'run.sh').stat().st_mode & 0o777)
        self.git(self.upstream, 'add', '.')
        self.git(self.upstream, 'update-index', '--chmod=+x', 'run.sh')
        self.git(self.upstream, 'commit', '--quiet', '-m', 'official initial fixture')
        (self.upstream / 'next').write_text('second official commit\n', encoding='utf-8')
        self.commit(self.upstream, 'official exact fixture\n\nFull original message.\n')
        self.original = self.git(self.upstream, 'rev-parse', 'HEAD')
        self.assertIn('100755', self.git(self.upstream, 'ls-tree', 'HEAD', 'run.sh'))
        self.original_tree = self.git(self.upstream, 'rev-parse', 'HEAD^{tree}')
        raw_json = self.git(self.upstream, 'show', '-s', '--format=%H%n%P%n%an%n%ae%n%aI%n%cn%n%ce%n%cI', 'HEAD').splitlines()
        original_bytes = self.git_bytes(self.upstream, 'cat-file', 'commit', self.original)
        self.document = dict(zip(('id', 'parents', 'author_name', 'author_email', 'authored_date',
                                  'committer_name', 'committer_email', 'committed_date'), raw_json))
        self.document['parent_ids'] = self.document.pop('parents').split()
        self.document['message'] = original_bytes.split(b'\n\n', 1)[1].decode('utf-8')
        self.archive = self.git_bytes(self.upstream, 'archive', '--format=tar.gz', '--prefix=official-source/', self.original)
        self.requests = []
        profile = {'schema_version': 1, 'sukisu_susfs': {
            'ksu_commits': [self.reference], 'tiers': ['Latest(最新)'], 'version': 'v2.3.0',
            'pins': {target: self.original for target in self.targets}}}
        (self.root / 'build-compatibility.json').write_text(json.dumps(profile), encoding='utf-8')
        self.matrix = {'include': [{'android_version': 'android12', 'kernel_version': '5.10', 'sub_level': 236},
                                   {'android_version': 'android12', 'kernel_version': '5.10', 'sub_level': 240},
                                   {'android_version': 'android13', 'kernel_version': '5.15', 'sub_level': 74}]}
        self.bundle = self.root / 'snapshot'

    @staticmethod
    def git_bytes(path, *arguments):
        return subprocess.run(['git', '-C', str(path), *arguments], capture_output=True,
                              check=True).stdout

    @classmethod
    def git(cls, path, *arguments):
        return cls.git_bytes(path, *arguments).decode('utf-8').strip()

    def commit(self, path, message):
        self.git(path, 'add', '.')
        self.git(path, 'commit', '--quiet', '-m', message)

    def official_http(self, url, limit, **kwargs):
        self.requests.append(url)
        if url.startswith(archive.API + '/repository/commits/'):
            return json.dumps(self.document).encode('utf-8')
        self.assertEqual(archive.API + '/repository/archive.tar.gz?sha=' + self.original, url)
        return self.archive

    def prepare(self, variant='SukiSU'):
        with (mock.patch.object(shared, 'fetch', side_effect=snapshot.SnapshotError('Git service unavailable')),
              mock.patch.object(archive, 'request_bytes', side_effect=self.official_http),
              contextlib.redirect_stdout(io.StringIO())):
            return shared.prepare_sources(self.source, self.matrix, self.reference,
                                          root=self.root, variant=variant)

    def synthetic_bakasu_port(self, source, reference):
        # Archive transport uses a two-commit native-manager fixture, not the
        # full BakaSU tree required by the separate real KPM port tests.
        self.assertEqual(self.reference, reference)
        self.assertEqual(snapshot.source_identity(self.source, self.reference),
                         snapshot.source_identity(source, reference))
        self.assertEqual('https://github.com/Baka-SU/BakaSU.git',
                         self.git(source, 'remote', 'get-url', 'origin'))
        self.assertEqual((self.source / 'kernel/Kbuild').read_bytes(),
                         (source / 'kernel/Kbuild').read_bytes())
        self.assertEqual('', self.git(source, 'status', '--porcelain', '--untracked-files=all'))
        return {}

    def export(self, sources, variant='SukiSU'):
        with (mock.patch.object(snapshot, 'verify_susfs_compatibility') as port,
              mock.patch('restore_bakasu_kpm.restore_bakasu_kpm',
                         side_effect=self.synthetic_bakasu_port) as baka_port):
            manifest = snapshot.snapshot_source(self.source, self.bundle, self.reference,
                root=self.root, variant=variant, susfs=True, tier='Latest(最新)', susfs_sources=sources)
        if variant == 'SukiSU':
            port.assert_called_once()
            baka_port.assert_not_called()
        else:
            port.assert_not_called()
            baka_port.assert_called_once()
        return manifest

    def test_sukisu_archive_bundle_preserves_full_manager_history_and_offline_pins(self):
        before = snapshot.source_identity(self.source, self.reference)
        native_bytes = (self.source / 'kernel/Kbuild').read_bytes()
        tags = self.git(self.source, 'tag', '--list')
        sources = self.prepare()
        self.assertEqual(set(self.targets), set(sources))
        self.assertEqual(4, len(self.requests))
        self.assertEqual(2, self.requests.count(archive.API + '/repository/commits/' + self.original))
        self.assertEqual(before, snapshot.source_identity(self.source, self.reference))
        self.assertEqual(native_bytes, (self.source / 'kernel/Kbuild').read_bytes())
        self.assertEqual(tags, self.git(self.source, 'tag', '--list'))
        self.assertEqual('', self.git(self.source, 'status', '--porcelain'))
        self.assertEqual('false', self.git(self.source, 'rev-parse', '--is-shallow-repository'))
        self.assertFalse((self.source / '.git/shallow').exists())
        manifest = self.export(sources)
        self.assertEqual({'SukiSU.bundle', 'source.json'}, {p.name for p in self.bundle.iterdir()})
        self.assertEqual(sources, manifest['susfs_sources'])
        # The carrier holds the original commit as a blob, never a synthetic
        # shallow commit inside the manager's complete native history.
        self.assertNotEqual(0, subprocess.run(['git', '-C', str(self.source), 'cat-file', '-e',
                                               self.original + '^{commit}'], capture_output=True).returncode)
        self.upstream.rename(self.root / 'removed-upstream')
        with (mock.patch.object(archive, 'request_bytes', side_effect=AssertionError('offline consumer used HTTP')),
              mock.patch.object(shared, 'fetch', side_effect=AssertionError('offline consumer used Git network'))):
            restored_manager = self.root / 'restored-manager'
            checkout.checkout_snapshot(self.bundle, restored_manager, self.reference)
            self.assertEqual(before, snapshot.source_identity(restored_manager, self.reference))
            self.assertFalse((restored_manager / '.git/shallow').exists())
            for index, target in enumerate(self.targets):
                restored = self.root / f'offline-susfs-{index}'
                self.assertEqual(sources[target], shared.restore_source(
                    self.bundle, restored, self.reference, target, sources))
                self.assertEqual(self.original, self.git(restored, 'rev-parse', 'HEAD'))
                self.assertEqual(self.original_tree, self.git(restored, 'rev-parse', 'HEAD^{tree}'))
                self.assertEqual('true', self.git(restored, 'rev-parse', '--is-shallow-repository'))
                self.assertEqual(self.original + '\n', (restored / '.git/shallow').read_text())
                self.assertEqual(b'CRLF\r\nfixture\r\n', (restored / 'preserve-bytes.txt').read_bytes())
                self.assertIn('100755', self.git(restored, 'ls-files', '--stage', 'run.sh'))
                self.git(restored, 'fsck', '--connectivity-only', '--no-dangling')
                original_git = pinning.git
                def no_network(directory, *arguments):
                    self.assertNotIn('fetch', arguments)
                    return original_git(directory, *arguments)
                with (mock.patch.object(pinning, 'git', side_effect=no_network),
                      mock.patch.dict(os.environ, {'GITHUB_ENV': str(self.root / 'pin-env')})):
                    self.assertTrue(pinning.pin_dependencies(self.root, restored, 'SukiSU', self.reference,
                                                             'Latest(最新)', target))

    def test_bakasu_latest_branch_archive_keeps_native_manager_identity(self):
        self.git(self.source, 'remote', 'set-url', 'origin', 'https://github.com/Baka-SU/BakaSU.git')
        before = snapshot.source_identity(self.source, self.reference)
        sources = self.prepare('BakaSU')
        for target in self.targets:
            self.assertIn(archive.API + '/repository/commits/gki-' + target, self.requests)
            self.assertEqual(self.original, sources[target]['ref'])
            self.assertNotEqual(self.original, sources[target]['transport_ref'])
        self.assertEqual(before, snapshot.source_identity(self.source, self.reference))
        self.export(sources, 'BakaSU')
        restored = self.root / 'bakasu-offline'
        shared.restore_source(self.bundle, restored, self.reference, self.targets[0], sources, variant='BakaSU')
        self.assertEqual(self.git_bytes(self.upstream, 'cat-file', 'commit', self.original),
                         self.git_bytes(restored, 'cat-file', 'commit', self.original))

    def test_exact_archive_recovery_reproduces_prepared_carrier_identity(self):
        sources = self.prepare()
        for target in self.targets:
            self.git(self.source, 'update-ref', '-d', shared.NAMESPACE + target)
        self.requests.clear()
        with (mock.patch.object(shared, 'fetch', side_effect=AssertionError('archive recovery used moving Git refs')),
              mock.patch.object(archive, 'request_bytes', side_effect=self.official_http)):
            self.assertEqual(sources, shared.recover_exact_sources(self.source, sources))
        self.assertEqual(2, self.requests.count(archive.API + '/repository/commits/' + self.original))
        self.assertFalse(any('/commits/gki-' in url for url in self.requests))
        self.assertFalse((self.source / '.git/shallow').exists())

    def test_changed_original_commit_blob_is_rejected_even_with_valid_carrier_hash(self):
        sources = self.prepare()
        target = self.targets[0]
        item = copy.deepcopy(sources[target])
        tree, raw = archive.carrier_contents(self.source, item['transport_ref'], item['ref'])
        altered = raw + b'changed original message\n'
        blob = archive.git(self.source, 'hash-object', '-w', '--stdin', data=altered)
        wrapper_tree = archive.git(self.source, 'mktree', data=(
            f'100644 blob {blob}\tcommit\n040000 tree {tree}\tsource\n').encode('ascii'))
        carrier = archive.git(self.source, 'hash-object', '-w', '-t', 'commit', '--stdin',
                              data=archive.carrier_bytes(wrapper_tree))
        self.git(self.source, 'update-ref', shared.NAMESPACE + target, carrier)
        changed = copy.deepcopy(sources)
        changed[target]['transport_ref'] = carrier
        with self.assertRaisesRegex(snapshot.SnapshotError, 'original commit SHA/tree'):
            shared.verify_local_sources(self.source, changed)

    def test_restore_rejects_changed_prepared_tree_or_original_ref(self):
        sources = self.prepare()
        self.export(sources)
        for field in ('tree', 'ref', 'transport_ref'):
            changed = copy.deepcopy(sources)
            changed[self.targets[0]][field] = 'a' * 40
            with self.subTest(field=field), self.assertRaisesRegex(snapshot.SnapshotError, 'successful prepare'):
                shared.restore_source(self.bundle, self.root / ('wrong-' + field), self.reference,
                                      self.targets[0], changed)
        self.assertFalse((self.root / 'wrong-tree').exists())


if __name__ == '__main__':
    unittest.main()
