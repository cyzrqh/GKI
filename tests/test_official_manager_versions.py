"""Source-derived APK version observations use real full Git histories."""
from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import official_manager_versions as versions
import snapshot_ksu_source as snapshot

SUKI_KBUILD = '''VERSION_BASE := 40000
VERSION_OFFSET := 2815
KSU_VERSION := $(if $(LOCAL_COUNT),$(shell expr $(VERSION_BASE) + $(LOCAL_COUNT) - $(VERSION_OFFSET)),41000)
'''
SUKI_GRADLE = '''extra["managerVersionCode"] = getVersionCode()
fun getGitCommitCount(): Int {
    val process = Runtime.getRuntime().exec(arrayOf("git", "rev-list", "--count", "HEAD"))
    return process.inputStream.bufferedReader().use { it.readText().trim().toInt() }
}
fun getVersionCode(): Int {
    val commitCount = getGitCommitCount()
    val major = 4
    val end = 2815
    return major * 10000 + commitCount - end
}
'''
BAKA_KBUILD = '''KSU_LOCAL_VERSION := $(shell cd $(KSU_SRC); $(GIT_BIN) rev-list --count HEAD)
KSU_VERSION := $(shell expr 30000 + $(KSU_LOCAL_VERSION) + 700)
'''
BAKA_GRADLE = '''extra["managerVersionCode"] = 30000 + getGitCommitCount() + 700
fun getGitCommitCount(): Int = providers.exec {
    commandLine("git", "rev-list", "--count", "HEAD")
}.standardOutput.asText.get().trim().toInt()
'''


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class OfficialManagerVersionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = snapshot.temporary_directory(ROOT.parent, '.manager-version-test-')
        self.root = self.temporary.__enter__()
        self.addCleanup(self.temporary.__exit__, None, None, None)
        self.cache = self.root / 'cache'
        self.upstreams = {}
        self.refs = {}
        self.calls = []
        self.serial = 0
        for variant, kbuild, gradle in (('SukiSU', SUKI_KBUILD, SUKI_GRADLE),
                                        ('BakaSU', BAKA_KBUILD, BAKA_GRADLE)):
            path = self.root / variant
            path.mkdir()
            self.upstreams[variant] = path
            for args in (('init', '--quiet'), ('config', 'user.name', 'Native version test'),
                         ('config', 'user.email', 'test@example.invalid'),
                         ('config', 'uploadpack.allowFilter', 'true'),
                         ('config', 'core.autocrlf', 'false')):
                snapshot.git(path, *args)
            (path / 'kernel').mkdir()
            (path / 'manager').mkdir()
            (path / 'kernel/Kbuild').write_text(kbuild, encoding='utf-8')
            (path / 'manager/build.gradle.kts').write_text(gradle, encoding='utf-8')
            self.refs[variant] = self.commit(variant)
        self.state = {'sukisu': {'branch': 'main', 'head': self.refs['SukiSU'], 'development': None,
                                 'release': {'tag': 'v4.2.1', 'commit': self.refs['SukiSU']}},
                      'bakasu': {'branch': 'main', 'head': self.refs['BakaSU'], 'release': None,
                                 'prerelease': {'tag': 'v4.2.0-rc3', 'commit': self.refs['BakaSU']}}}

    def commit(self, variant):
        path = self.upstreams[variant]
        self.serial += 1
        (path / 'fixture-history-marker').write_text(str(self.serial), encoding='utf-8')
        snapshot.git(path, 'add', '.')
        snapshot.git(path, 'commit', '--quiet', '--allow-empty', '-m', 'native version fixture')
        return snapshot.git(path, 'rev-parse', 'HEAD')

    def transport(self, directory, *args, **kwargs):
        self.calls.append((Path(directory), args, kwargs))
        self.assertIn('http.version=HTTP/1.1', args)
        self.assertIn('credential.helper=', args)
        self.assertLessEqual(kwargs['timeout'], 45)
        rewrites = []
        for variant, path in self.upstreams.items():
            url = 'https://github.com/' + versions.manager_profile(variant)['repository'] + '.git'
            rewrites += ['-c', 'url.' + path.as_uri() + '.insteadOf=' + url]
        return snapshot.git(directory, *rewrites, *args, **kwargs)

    def observe(self, old=None, new=None, transport=None):
        with mock.patch.object(versions, 'git', side_effect=transport or self.transport), \
                mock.patch.object(versions.time, 'sleep'):
            return versions.observe_versions(old, self.state if new is None else new, self.cache)

    def row(self, report, source='SukiSU', channel='测试版', side='after'):
        return next(row for row in report[side] if row['source'] == source and row['channel'] == channel)

    def test_both_managers_use_native_gradle_and_kbuild_formulas_and_full_history(self):
        result = self.observe()
        self.assertEqual('observed', result['status'])
        self.assertEqual([], result['before'])
        self.assertEqual(37186, self.row(result)['version_code'])
        self.assertEqual(37186, self.row(result, channel='正式版')['version_code'])
        self.assertEqual(30701, self.row(result, 'BakaSU')['version_code'])
        formal = self.row(result, 'BakaSU', '正式版')
        self.assertEqual('unavailable', formal['status'])
        self.assertIsNone(formal['version_code'])
        self.assertIsNone(formal['commit'])
        self.assertIsNone(formal['ref'])
        self.assertEqual({variant + '.git' for variant in ('SukiSU', 'BakaSU')},
                         {path.name for path in self.cache.iterdir()})
        for variant in ('SukiSU', 'BakaSU'):
            repository = self.cache / (variant + '.git')
            self.assertEqual('true', snapshot.git(repository, 'rev-parse', '--is-bare-repository'))
            self.assertEqual('false', snapshot.git(repository, 'rev-parse', '--is-shallow-repository'))
            self.assertFalse((repository / 'kernel').exists())

    def test_repeated_fixed_snapshots_reuse_cached_objects_without_another_fetch(self):
        first = self.observe(old=self.state)
        self.calls.clear()
        self.assertEqual(first, self.observe(old=self.state))
        self.assertFalse(any('fetch' in args for _, args, _ in self.calls))

    def test_before_and_after_are_derived_from_their_exact_distinct_commits(self):
        before = copy.deepcopy(self.state)
        reference = self.commit('SukiSU')
        self.state['sukisu']['head'] = reference
        result = self.observe(old=before)
        self.assertEqual(37186, self.row(result, side='before')['version_code'])
        self.assertEqual(37187, self.row(result)['version_code'])
        self.assertEqual(before['sukisu']['head'], self.row(result, side='before')['commit'])
        self.assertEqual(reference, self.row(result)['commit'])
        # A source change must not relabel the unchanged official formal tag.
        self.assertEqual(37186, self.row(result, channel='正式版')['version_code'])

    def test_diverged_suki_main_dev_uses_complete_counts_and_ties_keep_main(self):
        path = self.upstreams['SukiSU']
        base = self.refs['SukiSU']
        main = self.commit('SukiSU')
        self.state['sukisu']['head'] = main
        snapshot.git(path, 'checkout', '--quiet', '--detach', base)
        dev = self.commit('SukiSU')
        self.state['sukisu']['development'] = {'branch': 'dev', 'head': dev}
        tied = self.observe()
        self.assertEqual(main, self.row(tied)['commit'])
        self.assertEqual('main', self.row(tied)['ref'])
        dev = self.commit('SukiSU')
        self.state['sukisu']['development']['head'] = dev
        newer = self.observe()
        self.assertEqual(dev, self.row(newer)['commit'])
        self.assertEqual('dev', self.row(newer)['ref'])
        self.assertEqual(37188, self.row(newer)['version_code'])

    def test_manager_formula_change_is_unknown_not_an_invented_apk_number(self):
        path = self.upstreams['BakaSU'] / 'manager/build.gradle.kts'
        path.write_text(BAKA_GRADLE.replace('+ 700', '+ 701'), encoding='utf-8')
        self.state['bakasu']['head'] = self.commit('BakaSU')
        result = self.observe()
        self.assertEqual('partial', result['status'])
        self.assertEqual('unknown', self.row(result, 'BakaSU')['status'])
        self.assertIsNone(self.row(result, 'BakaSU')['version_code'])
        self.assertEqual('observed', self.row(result)['status'])

    def test_failed_main_history_does_not_guess_latest_even_with_cached_dev(self):
        base = self.refs['SukiSU']
        dev = self.commit('SukiSU')
        self.state['sukisu']['development'] = {'branch': 'dev', 'head': dev}
        self.observe()
        main = self.commit('SukiSU')
        self.state['sukisu']['head'] = main
        def failed_main(directory, *args, **kwargs):
            if 'fetch' in args and any(item.startswith(main + ':') for item in args):
                raise snapshot.GitCommandError('fetch', category='connection failure', returncode=128)
            return self.transport(directory, *args, **kwargs)
        result = self.observe(transport=failed_main)
        latest = self.row(result)
        self.assertEqual('unknown', latest['status'])
        self.assertIsNone(latest['commit'])
        self.assertIsNone(latest['ref'])
        self.assertIsNone(latest['version_code'])
        self.assertEqual(base, self.row(result, channel='正式版')['commit'])
        self.assertEqual('observed', self.row(result, channel='正式版')['status'])

    def test_latest_formula_failure_retains_the_proven_selected_commit(self):
        path = self.upstreams['SukiSU'] / 'manager/build.gradle.kts'
        path.write_text(SUKI_GRADLE.replace('val end = 2815', 'val end = 2816'), encoding='utf-8')
        dev = self.commit('SukiSU')
        self.state['sukisu']['development'] = {'branch': 'dev', 'head': dev}
        result = self.observe()
        latest = self.row(result)
        self.assertEqual('unknown', latest['status'])
        self.assertIsNone(latest['version_code'])
        self.assertEqual(dev, latest['commit'])
        self.assertEqual('dev', latest['ref'])

    def test_unknown_kbuild_and_non_head_gradle_count_both_fail_closed(self):
        suki = self.upstreams['SukiSU'] / 'kernel/Kbuild'
        suki.write_text(SUKI_KBUILD.replace('$(LOCAL_COUNT)', '$(OTHER_COUNT)'), encoding='utf-8')
        self.state['sukisu']['head'] = self.commit('SukiSU')
        baka = self.upstreams['BakaSU'] / 'manager/build.gradle.kts'
        baka.write_text(BAKA_GRADLE.replace('"HEAD"', '"main"'), encoding='utf-8')
        self.state['bakasu']['head'] = self.commit('BakaSU')
        result = self.observe()
        self.assertEqual('unknown', self.row(result)['status'])
        self.assertEqual('unknown', self.row(result, 'BakaSU')['status'])
        self.assertEqual('observed', self.row(result, channel='正式版')['status'])

    def test_temporary_network_failure_retries_and_still_reports_exact_versions(self):
        attempts = []
        def flaky(directory, *args, **kwargs):
            if 'fetch' in args:
                attempts.append(args)
                if len(attempts) == 1:
                    raise snapshot.GitCommandError('fetch', category='connection failure', returncode=128)
            return self.transport(directory, *args, **kwargs)
        result = self.observe(transport=flaky)
        self.assertEqual('observed', result['status'])
        self.assertEqual(37186, self.row(result)['version_code'])
        self.assertEqual(3, len(attempts))

    def test_failed_history_lookup_does_not_fail_sync_or_guess_codes(self):
        def failed(directory, *args, **kwargs):
            if 'fetch' in args:
                raise snapshot.GitCommandError('fetch', category='connection failure', returncode=128)
            return self.transport(directory, *args, **kwargs)
        result = self.observe(transport=failed)
        self.assertEqual('not-observed', result['status'])
        self.assertTrue(all(row['version_code'] is None for row in result['after']))
        self.assertEqual('unavailable', self.row(result, 'BakaSU', '正式版')['status'])

    def test_cached_credentials_are_discarded_and_do_not_leave_the_history_cache(self):
        self.observe()
        repository = self.cache / 'SukiSU.git'
        config = repository / 'config'
        config.write_text(config.read_text() + '\n[http]\n\textraHeader = Authorization: Bearer fake-token\n',
                          encoding='utf-8')
        self.assertEqual('observed', self.observe()['status'])
        self.assertNotIn('fake-token', config.read_text())

    def test_foreign_or_shallow_cache_is_not_reused_or_deleted(self):
        self.observe()
        repository = self.cache / 'SukiSU.git'
        marker = repository / versions.MARKER
        marker.write_text(json.dumps({'schema_version': 1, 'repository': 'unrelated/foreign'}), encoding='utf-8')
        result = self.observe()
        self.assertEqual('unknown', self.row(result)['status'])
        self.assertEqual('unrelated/foreign', json.loads(marker.read_text())['repository'])
        marker.write_text(json.dumps({'schema_version': 1, 'repository': 'SukiSU-Ultra/SukiSU-Ultra'}), encoding='utf-8')
        (repository / 'shallow').write_text(self.refs['SukiSU'] + '\n', encoding='utf-8')
        self.assertEqual('unknown', self.row(self.observe())['status'])
        self.assertTrue((repository / 'shallow').exists())

    def test_missing_snapshots_do_not_touch_git_or_fabricate_channels(self):
        with mock.patch.object(versions, 'git') as transport:
            result = versions.observe_versions(None, None, self.cache)
        transport.assert_not_called()
        self.assertEqual({'status': 'observed', 'before': [], 'after': []}, result)

    def test_exhausted_budget_reports_unknown_instead_of_delaying_sync(self):
        with mock.patch.object(versions.time, 'monotonic',
                               side_effect=itertools.chain((0,), itertools.repeat(1000))), \
                mock.patch.object(versions, 'git') as transport:
            result = versions.observe_versions(None, self.state, self.cache)
        transport.assert_not_called()
        self.assertEqual('not-observed', result['status'])
        self.assertTrue(all(row['version_code'] is None for row in result['after']))


if __name__ == '__main__':
    unittest.main()
