"""Official forge migration keeps historical source identity and frozen bundles."""
from pathlib import Path
import shutil
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import official_manager_versions as versions
import snapshot_ksu_source as snapshot
import verify_manager_update as updates
from checkout_ksu_snapshot import checkout_snapshot
from manager_variants import MANAGERS, manager_profile
from resolve_build_sources import source_repository_url


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class OfficialSourceTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = snapshot.temporary_directory(ROOT.parent, '.source-transport-test-')
        self.root = self.temporary.__enter__()
        self.addCleanup(self.temporary.__exit__, None, None, None)
        self.source = self.root / 'source'
        self.source.mkdir()
        for arguments in (('init', '--quiet'), ('config', 'user.name', 'Source transport test'),
                          ('config', 'user.email', 'test@example.invalid')):
            snapshot.git(self.source, *arguments)
        (self.source / 'README').write_text('committed source\n', encoding='utf-8')
        snapshot.git(self.source, 'add', 'README')
        snapshot.git(self.source, 'commit', '--quiet', '-m', 'Fixed source')
        self.reference = snapshot.git(self.source, 'rev-parse', 'HEAD')
        snapshot.git(self.source, 'remote', 'add', 'origin', source_repository_url('SukiSU-Ultra/SukiSU-Ultra'))

    def test_official_forge_and_historical_github_origins_are_accepted(self):
        current = source_repository_url('SukiSU-Ultra/SukiSU-Ultra')
        self.assertEqual('https://git.zako.shirkneko.top:20004/shirkneko/SukiSU.git', current)
        for origin in (current, current.removesuffix('.git'),
                       'https://github.com/SukiSU-Ultra/SukiSU-Ultra.git',
                       'https://github.com/SukiSU-Nova/SukiSU-Nova.git',
                       'git@github.com:SukiSU-Nova/SukiSU-Nova.git'):
            with self.subTest(origin=origin):
                snapshot.git(self.source, 'remote', 'set-url', 'origin', origin)
                snapshot.check_official_source(self.source, 'SukiSU')

    def test_untrusted_or_credential_bearing_forge_origin_is_rejected(self):
        current = source_repository_url('SukiSU-Ultra/SukiSU-Ultra')
        for origin in (current.replace('https://', 'https://user:password@'),
                       current + '/other', 'https://example.invalid/shirkneko/SukiSU.git'):
            with self.subTest(origin=origin):
                snapshot.git(self.source, 'remote', 'set-url', 'origin', origin)
                with self.assertRaises(snapshot.SnapshotError):
                    snapshot.check_official_source(self.source, 'SukiSU')

    def test_forge_source_round_trips_with_the_existing_receipt_identity(self):
        output = self.root / 'snapshot'
        manifest = snapshot.snapshot_source(self.source, output, self.reference, root=self.root)
        self.assertEqual('SukiSU-Ultra/SukiSU-Ultra', manifest['repository'])
        self.assertEqual(manifest, checkout_snapshot(output, self.root / 'restored', self.reference))
        self.assertEqual('committed source\n', (self.root / 'restored/README').read_text(encoding='utf-8'))

    def test_version_history_cache_rebinds_the_transport_without_changing_identity(self):
        cache = self.root / 'versions'
        for variant in MANAGERS:
            with self.subTest(variant=variant):
                repository = versions._repository(cache, variant, time.monotonic() + 30)
                snapshot.git(repository, 'remote', 'set-url', 'origin', 'https://example.invalid/stale.git')
                reused = versions._repository(cache, variant, time.monotonic() + 30)
                self.assertEqual(repository, reused)
                self.assertEqual(source_repository_url(manager_profile(variant)['repository']),
                                 snapshot.git(reused, 'remote', 'get-url', 'origin'))

    def test_compatibility_history_cache_rebinds_the_transport_without_changing_identity(self):
        cache = self.root / 'compatibility'
        for variant in MANAGERS:
            with self.subTest(variant=variant):
                repository = updates.source_repository(cache, variant)
                snapshot.git(repository, 'remote', 'set-url', 'origin', 'https://example.invalid/stale.git')
                reused = updates.source_repository(cache, variant)
                self.assertEqual(repository, reused)
                self.assertEqual(source_repository_url(manager_profile(variant)['repository']),
                                 snapshot.git(reused, 'remote', 'get-url', 'origin'))


if __name__ == '__main__':
    unittest.main()
