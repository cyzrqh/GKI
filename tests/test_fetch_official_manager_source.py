"""Fetch the new official host without losing immutable native version history."""
from pathlib import Path
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import fetch_official_manager_source as fetcher
from snapshot_ksu_source import GitCommandError, SnapshotError, git, temporary_directory


class OfficialSourceFetchTests(unittest.TestCase):
    def setUp(self):
        self.workspace = temporary_directory(ROOT / 'tests', '.fetch-source-')
        self.root = self.workspace.__enter__()
        self.addCleanup(self.workspace.__exit__, None, None, None)

    def history(self):
        remote = self.root / 'upstream'
        remote.mkdir()
        git(remote, 'init', '--quiet')
        git(remote, 'config', 'user.name', 'Test')
        git(remote, 'config', 'user.email', 'test@example.com')
        for index in range(3):
            (remote / 'version.txt').write_text(str(index), encoding='utf-8')
            git(remote, 'add', 'version.txt')
            git(remote, 'commit', '--quiet', '-m', 'version ' + str(index))
            if index == 0:
                git(remote, 'tag', 'v1.0')
            if index == 1:
                reference = git(remote, 'rev-parse', 'HEAD')
        return remote, reference

    def test_old_fixed_commit_keeps_full_history_tags_and_official_origin(self):
        remote, reference = self.history()

        def controlled_fetch(source, pinned):
            self.assertEqual(reference, pinned)
            git(source, 'fetch', '--quiet', '--tags', str(remote), pinned)

        output = self.root / 'selected'
        with patch.object(fetcher, 'fetch_history', side_effect=controlled_fetch):
            identity = fetcher.fetch_source(output, reference)
        self.assertEqual(reference, git(output, 'rev-parse', 'HEAD'))
        self.assertEqual('1', (output / 'version.txt').read_text(encoding='utf-8'))
        self.assertEqual(2, identity['git_count'])
        self.assertTrue(identity['git_describe'].startswith('v1.0-1-g'))
        self.assertEqual('false', git(output, 'rev-parse', '--is-shallow-repository'))
        self.assertEqual('https://git.zako.shirkneko.top:20004/shirkneko/SukiSU.git',
                         git(output, 'remote', 'get-url', 'origin'))

    def test_failed_fetch_never_publishes_a_partial_source(self):
        output = self.root / 'partial'
        with patch.object(fetcher, 'fetch_history', side_effect=GitCommandError('fetch')):
            with self.assertRaises(GitCommandError):
                fetcher.fetch_source(output, 'a' * 40)
        self.assertFalse(output.exists())
        self.assertEqual([], list(self.root.iterdir()))

    def test_rejects_moving_refs_and_existing_outputs_before_network(self):
        output = self.root / 'existing'
        output.mkdir()
        for reference in ('main', 'builtin', 'a' * 40):
            with self.subTest(reference=reference), patch.object(fetcher, 'fetch_history') as network:
                with self.assertRaises(SnapshotError):
                    fetcher.fetch_source(output, reference)
                network.assert_not_called()
        self.assertTrue(output.is_dir())
        with patch.object(fetcher, 'fetch_history') as network:
            with self.assertRaises(ValueError):
                fetcher.fetch_source(self.root / 'other', 'a' * 40, variant='untrusted')
            network.assert_not_called()

    def test_network_fetch_strips_credentials_and_url_rewrite_configuration(self):
        source = self.root / 'source'
        source.mkdir()
        with patch.dict(os.environ, {'GH_TOKEN': 'private-token', 'GITHUB_TOKEN': 'another-token',
                                     'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'url.bad.insteadOf',
                                     'GIT_CONFIG_VALUE_0': 'https://', 'GIT_ASKPASS': 'untrusted',
                                     'SSH_ASKPASS': 'untrusted'}):
            with patch.object(fetcher.subprocess, 'run', return_value=
                              subprocess.CompletedProcess([], 0, '', '')) as run:
                fetcher.fetch_history(source, 'a' * 40)
        args, options = run.call_args
        environment = options['env']
        for field in ('GH_TOKEN', 'GITHUB_TOKEN', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0',
                      'GIT_CONFIG_VALUE_0', 'GIT_ASKPASS', 'SSH_ASKPASS'):
            self.assertNotIn(field, environment)
        self.assertEqual('1', environment['GIT_CONFIG_NOSYSTEM'])
        self.assertEqual(os.devnull, environment['GIT_CONFIG_GLOBAL'])
        command = args[0]
        for option in ('credential.helper=', 'http.extraHeader=', 'http.followRedirects=false'):
            self.assertIn(option, command)
        self.assertIn('--tags', command)
        self.assertNotIn('--depth', command)
        self.assertEqual(['origin', 'a' * 40], command[-2:])

    def test_only_transient_network_failures_retry_and_keep_remote_errors_private(self):
        transient = subprocess.CompletedProcess([], 1, '', 'HTTP 503 secret-token')
        success = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(fetcher.subprocess, 'run', side_effect=[transient, success]) as run, \
                patch.object(fetcher.time, 'sleep'):
            fetcher.fetch_history(self.root, 'a' * 40)
        self.assertEqual(2, run.call_count)
        permanent = subprocess.CompletedProcess([], 1, '', 'authentication failed secret-token')
        with patch.object(fetcher.subprocess, 'run', return_value=permanent) as run:
            with self.assertRaises(GitCommandError) as error:
                fetcher.fetch_history(self.root, 'a' * 40)
        self.assertEqual(1, run.call_count)
        self.assertNotIn('secret-token', str(error.exception))


class OfficialSourceWorkflowTests(unittest.TestCase):
    def test_gki_oplus_and_prebuild_share_the_host_aware_pinned_checkout(self):
        from combined_manager_workflow import add_combined
        from generate_workflow import create_workflow
        from manager_prebuild_workflow import prebuild_workflow
        from oplus_workflow import create_workflow as create_oplus

        single = create_workflow(ROOT)
        documents = (
            (add_combined(single, oplus=False)[0], 'prepare-matrix'),
            (add_combined(create_oplus(ROOT, single), oplus=True)[0], 'prepare-matrix'),
            (prebuild_workflow(single), 'prebuild-manager-sources'),
        )
        for document, job in documents:
            with self.subTest(workflow=document['name']):
                steps = document['jobs'][job]['steps']
                selected = [step for step in steps if
                            'fetch_official_manager_source.py' in step.get('run', '')]
                self.assertEqual(1, len(selected))
                source = selected[0]
                self.assertIn("matrix.variant == 'SukiSU'", source['if'])
                self.assertIn('--ref "$KSU_REF"', source['run'])
                self.assertNotIn('GH_TOKEN', source['env'])
                github = next(step for step in steps if
                              step.get('with', {}).get('path') == 'ksu-snapshot-source')
                self.assertIn("matrix.variant != 'SukiSU'", github['if'])
                self.assertEqual('${{ steps.sources.outputs.ksu_repo }}', github['with']['repository'])
                self.assertEqual(0, github['with']['fetch-depth'])


if __name__ == '__main__':
    unittest.main()
