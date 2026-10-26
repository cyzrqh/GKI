"""Real Git regression for one SUSFS fetch and completely offline consumers."""
import copy
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
import shared_susfs_source as shared
import snapshot_ksu_source as snapshot
import checkout_ksu_snapshot as checkout
import transfer_ksu_snapshot as transfer
import pin_build_dependencies as pinning


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class SharedSusfsSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = snapshot.temporary_directory(ROOT.parent, '.ss-')
        self.root = self.temporary.__enter__()
        self.addCleanup(self.temporary.__exit__, None, None, None)
        self.source = self.root / 'ksu'
        self.upstream = self.root / 'susfs'
        for path in (self.source, self.upstream):
            path.mkdir()
            self.git(path, 'init', '--quiet')
            self.git(path, 'config', 'user.name', 'Shared source regression')
            self.git(path, 'config', 'user.email', 'test@example.invalid')
            self.git(path, 'config', 'core.autocrlf', 'false')
        self.git(self.source, 'remote', 'add', 'origin', 'https://github.com/Baka-SU/BakaSU.git')
        (self.source / 'kernel').mkdir()
        (self.source / 'kernel/Kbuild').write_text('original manager kernel\n', encoding='utf-8')
        self.commit(self.source, 'native manager')
        self.git(self.source, 'tag', '-a', 'v4.2.0-rc3', '-m', 'native manager tag')
        self.reference = self.git(self.source, 'rev-parse', 'HEAD')
        self.targets = ('android12-5.10', 'android13-5.15')
        header = self.upstream / 'kernel_patches/include/linux/susfs.h'
        header.parent.mkdir(parents=True)
        header.write_text('#define SUSFS_VERSION "v2.3.0"\n', encoding='utf-8')
        for target in self.targets:
            (self.upstream / f'kernel_patches/50_add_susfs_in_gki-{target}.patch').write_text(
                f'real target fixture {target}\n', encoding='utf-8')
        self.commit(self.upstream, 'original official SUSFS')
        self.older = self.git(self.upstream, 'rev-parse', 'HEAD')
        self.git(self.upstream, 'tag', '-a', 'do-not-import-this-tag', '-m', 'SUSFS tag')
        for target in self.targets:
            self.git(self.upstream, 'branch', 'gki-' + target)
        (self.upstream / 'latest').write_text('new source\n', encoding='utf-8')
        self.commit(self.upstream, 'new official source')
        self.latest = self.git(self.upstream, 'rev-parse', 'HEAD')
        for target in self.targets:
            self.git(self.upstream, 'update-ref', 'refs/heads/gki-' + target, self.latest)
        self.matrix = {'include': [{'android_version': 'android12', 'kernel_version': '5.10', 'sub_level': 236},
                                   {'android_version': 'android12', 'kernel_version': '5.10', 'sub_level': 240},
                                   {'android_version': 'android13', 'kernel_version': '5.15', 'sub_level': 189}]}
        self.bundle = self.root / 'bundle'
        self.fetches = []

    @staticmethod
    def git(path, *args):
        return subprocess.run(['git', '-C', str(path), *args], capture_output=True,
                              text=True, encoding='utf-8', check=True).stdout.strip()

    def commit(self, path, message):
        self.git(path, 'add', '.')
        self.git(path, 'commit', '--quiet', '-m', message)

    def official_transport(self, path, *args, **kwargs):
        if shared.OFFICIAL in args:
            self.fetches.append(args)
            self.assertIn('--no-tags', args)
            self.assertFalse(any(value.startswith('--depth') for value in args))
            args = tuple(str(self.upstream) if value == shared.OFFICIAL else value for value in args)
        return snapshot.git(path, *args, **kwargs)

    def prepare(self, **kwargs):
        with mock.patch.object(shared, 'git', side_effect=self.official_transport):
            return shared.prepare_sources(self.source, self.matrix, self.reference,
                                          root=self.root, variant='BakaSU', **kwargs)

    def export(self, sources):
        return snapshot.snapshot_source(self.source, self.bundle, self.reference, root=self.root,
                                        variant='BakaSU', susfs=True, susfs_sources=sources)

    def test_one_official_fetch_serves_unique_targets_without_changing_native_identity(self):
        before = snapshot.source_identity(self.source, self.reference)
        tags = self.git(self.source, 'tag', '--list')
        source_bytes = (self.source / 'kernel/Kbuild').read_bytes()
        sources = self.prepare()
        self.assertEqual(1, len(self.fetches))
        self.assertEqual(set(self.targets), set(sources))
        self.assertEqual({self.latest}, {item['ref'] for item in sources.values()})
        self.assertEqual(before, snapshot.source_identity(self.source, self.reference))
        self.assertEqual(tags, self.git(self.source, 'tag', '--list'))
        self.assertEqual(source_bytes, (self.source / 'kernel/Kbuild').read_bytes())
        self.assertEqual('false', self.git(self.source, 'rev-parse', '--is-shallow-repository'))
        self.assertEqual('', self.git(self.source, 'status', '--porcelain'))

    def test_offline_consumers_restore_exact_trees_after_official_source_disappears(self):
        sources = self.prepare()
        manifest = self.export(sources)
        self.assertEqual({'source.json', 'SukiSU.bundle'}, {p.name for p in self.bundle.iterdir()})
        self.assertEqual(sources, manifest['susfs_sources'])
        # No remote is reachable, but both source identities still restore.
        self.upstream.rename(self.root / 'offline-upstream')
        with mock.patch.object(shared, 'fetch', side_effect=AssertionError('consumer network fetch')):
            for index, target in enumerate(self.targets):
                restored = self.root / f'restored-{index}'
                self.assertEqual(sources[target], shared.restore_source(
                    self.bundle, restored, self.reference, target, sources, variant='BakaSU'))
                self.assertEqual(self.latest, self.git(restored, 'rev-parse', 'HEAD'))
                self.assertEqual(shared.OFFICIAL, self.git(restored, 'remote', 'get-url', 'origin'))
            original = self.root / 'native-ksu'
            checkout.checkout_snapshot(self.bundle, original, self.reference, variant='BakaSU')
        self.assertEqual(self.reference, self.git(original, 'rev-parse', 'HEAD'))
        self.assertEqual(manifest['git_count'], int(self.git(original, 'rev-list', '--count', 'HEAD')))

    def test_pin_and_custom_prefix_are_selected_once_and_exported_without_moving_heads(self):
        config = self.root / 'config/config'
        config.parent.mkdir()
        config.write_text('custom=true\ngki-android12-5.10=' + self.older[:12] + '\n', encoding='utf-8')
        sources = self.prepare()
        self.assertEqual(self.older, sources['android12-5.10']['ref'])
        self.assertEqual(self.latest, sources['android13-5.15']['ref'])
        self.assertEqual(1, len(self.fetches))
        self.export(sources)
        restored = self.root / 'historical'
        shared.restore_source(self.bundle, restored, self.reference, 'android12-5.10', sources, variant='BakaSU')
        self.assertEqual(self.older, self.git(restored, 'rev-parse', 'HEAD'))

    def test_sukisu_compatibility_pins_share_one_exact_hash_fetch(self):
        self.git(self.source, 'remote', 'set-url', 'origin', 'https://github.com/SukiSU-Ultra/SukiSU-Ultra.git')
        with (mock.patch.object(shared, 'selected_pin', return_value=(self.older, 'v2.3.0')),
              mock.patch.object(shared, 'git', side_effect=self.official_transport)):
            sources = shared.prepare_sources(self.source, self.matrix, self.reference, root=self.root,
                                              variant='SukiSU', tier='Latest(最新)')
        self.assertEqual(1, len(self.fetches))
        self.assertEqual({self.older}, {item['ref'] for item in sources.values()})
        self.assertFalse(any('refs/heads/' in value for value in self.fetches[0]))

    def test_sukisu_pins_port_and_offline_consumer_keep_the_original_manager_identity(self):
        self.git(self.source, 'remote', 'set-url', 'origin', 'https://github.com/SukiSU-Ultra/SukiSU-Ultra.git')
        profile = {'schema_version': 1, 'sukisu_susfs': {
            'ksu_commits': [self.reference], 'tiers': ['Latest(最新)'], 'version': 'v2.3.0',
            'pins': {target: self.older for target in self.targets}}}
        (self.root / 'build-compatibility.json').write_text(json.dumps(profile), encoding='utf-8')
        before = snapshot.source_identity(self.source, self.reference)
        with mock.patch.object(shared, 'git', side_effect=self.official_transport):
            sources = shared.prepare_sources(self.source, self.matrix, self.reference, root=self.root,
                                            variant='SukiSU', tier='Latest(最新)')
        self.assertEqual({self.older}, {entry['ref'] for entry in sources.values()})
        # The native-port fixture is covered by the existing source/port suite;
        # this verifies that sharing dependency objects still invokes that gate.
        with mock.patch.object(snapshot, 'verify_susfs_compatibility') as port:
            manifest = snapshot.snapshot_source(self.source, self.bundle, self.reference, root=self.root,
                variant='SukiSU', susfs=True, tier='Latest(最新)', susfs_sources=sources)
        port.assert_called_once()
        self.assertEqual((self.source, self.root, 'Latest(最新)', True), port.call_args.args[:4])
        self.assertEqual(before, snapshot.source_identity(self.source, self.reference))
        self.upstream.rename(self.root / 'offline-upstream')
        restored = self.root / 'sukisu-pinned-susfs'
        shared.restore_source(self.bundle, restored, self.reference, self.targets[0], sources, variant='SukiSU')
        original_git = pinning.git
        def offline_pin(directory, *arguments):
            if arguments[0] == 'fetch':
                raise AssertionError('A restored compatibility pin must not contact GitLab')
            return original_git(directory, *arguments)
        with (mock.patch.object(pinning, 'git', side_effect=offline_pin),
              mock.patch.dict(os.environ, {'GITHUB_ENV': str(self.root / 'pinned-env')})):
            self.assertTrue(pinning.pin_dependencies(self.root, restored, 'SukiSU', self.reference,
                                                     'Latest(最新)', self.targets[0]))
        original = self.root / 'sukisu-restored'
        checkout.checkout_snapshot(self.bundle, original, self.reference, variant='SukiSU')
        self.assertEqual(before, snapshot.source_identity(original, self.reference))
        self.assertEqual(sources, manifest['susfs_sources'])

    def test_wrong_prepare_identity_and_bundle_corruption_are_refused(self):
        sources = self.prepare()
        self.export(sources)
        changed = copy.deepcopy(sources)
        changed['android12-5.10']['ref'] = 'a' * 40
        with self.assertRaisesRegex(snapshot.SnapshotError, 'successful prepare'):
            shared.restore_source(self.bundle, self.root / 'wrong', self.reference,
                                  'android12-5.10', changed, variant='BakaSU')
        with (self.bundle / snapshot.BUNDLE_NAME).open('ab') as output:
            output.write(b'corrupt')
        with self.assertRaisesRegex(snapshot.SnapshotError, 'size or digest'):
            shared.restore_source(self.bundle, self.root / 'corrupt', self.reference,
                                  'android12-5.10', sources, variant='BakaSU')
        self.assertFalse((self.root / 'wrong').exists())
        self.assertFalse((self.root / 'corrupt').exists())

    def test_wrong_tree_date_or_version_cannot_be_exported(self):
        sources = self.prepare()
        for field, value in (('tree', 'a' * 40), ('date', '2026-01-01 00:00:00 +0000'), ('version', 'v9.9.9')):
            changed = copy.deepcopy(sources)
            changed['android12-5.10'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(snapshot.SnapshotError, 'differ'):
                self.export(changed)
        self.assertFalse(self.bundle.exists())

    def test_foreign_target_and_custom_repo_options_fail_closed(self):
        for target in ('../android12-5.10', '-c-http.extraheader', 'android12-5.10/../../x'):
            with self.subTest(target=target), self.assertRaises(snapshot.SnapshotError):
                shared.validate_sources({target: {'ref': 'a'*40, 'tree': 'b'*40,
                    'date': '2026-01-01 00:00:00 +0000', 'version': 'v2.3.0', 'branch': 'gki-' + target}})
        config = self.root / 'config/config'
        config.parent.mkdir()
        config.write_text('custom=true\ngki-android12-5.10=https://untrusted.invalid/source\n', encoding='utf-8')
        with self.assertRaisesRegex(snapshot.SnapshotError, 'hexadecimal'):
            self.prepare()
        self.assertEqual([], self.fetches)

    def test_backup_identity_accepts_prepared_sources_and_exact_recovery_verifies_objects(self):
        sources = self.prepare()
        identity = {'ref': self.reference, **snapshot.source_identity(self.source, self.reference),
                    'susfs_sources': sources}
        self.assertEqual(identity, transfer.prepared_identity(identity, self.reference))
        # Remove local dependency refs; exact fallback must import hashes, not
        # updated or renamed moving branch heads.
        for target in self.targets:
            self.git(self.source, 'update-ref', '-d', shared.NAMESPACE + target)
        self.fetches.clear()
        with mock.patch.object(shared, 'git', side_effect=self.official_transport):
            shared.recover_exact_sources(self.source, sources)
        self.assertEqual(1, len(self.fetches))
        for target in self.targets:
            self.assertIn('+' + self.latest + ':' + shared.NAMESPACE + target, self.fetches[0])
        changed = copy.deepcopy(identity)
        changed['susfs_sources']['android12-5.10']['tree'] = 'a' * 40
        with mock.patch.object(shared, 'git', side_effect=self.official_transport), self.assertRaisesRegex(
                snapshot.SnapshotError, 'differ'):
            shared.recover_exact_sources(self.source, changed['susfs_sources'])

    def test_retry_backoff_is_spaced_and_never_expands_a_failed_prepare_matrix(self):
        with (mock.patch.object(shared, 'git', side_effect=snapshot.SnapshotError('unavailable')) as calls,
              mock.patch.object(shared.time, 'sleep') as sleep):
            with self.assertRaisesRegex(snapshot.SnapshotError, 'four spaced'):
                shared.fetch(self.source, ['+refs/heads/gki-android12-5.10:' + shared.NAMESPACE + 'android12-5.10'])
        self.assertEqual(4, calls.call_count)
        self.assertEqual([mock.call(15), mock.call(30), mock.call(60)], sleep.call_args_list)


if __name__ == '__main__':
    unittest.main()
