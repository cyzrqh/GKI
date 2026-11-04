"""Resolve real source choices while skipping only absent combined stable releases."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import resolve_build_sources as sources
import resolve_manager_selection as selection
from manager_variants import COMBINED_GROUPS, COMBINED_SELECTION, selected_managers


def source(variant, tier='正式版'):
    reference = hashlib.sha1(variant.encode()).hexdigest()
    return sources.source_result(reference, sources.REPOSITORIES[variant],
                                 'v1.0' if tier == '正式版' else 'main', selection.effective_tier(tier))


def resolve(group=COMBINED_SELECTION, missing=(), tier='正式版'):
    def choose(root, variant, channel, custom_ref, susfs):
        if variant in missing:
            raise sources.NoStableRelease('No published official stable release')
        return source(variant, tier)
    with patch.object(selection, 'resolve_sources', side_effect=choose):
        return selection.resolve_selection(group, tier, susfs=True)


class ManagerSelectionTests(unittest.TestCase):
    def test_each_combination_can_skip_first_middle_or_last_without_changing_channel(self):
        for group, requested in COMBINED_GROUPS.items():
            for missing in requested:
                with self.subTest(group=group, missing=missing):
                    receipt = resolve(group, (missing,))
                    expected = tuple(name for name in requested if name != missing)
                    self.assertEqual(expected, selection.active_managers(group, receipt))
                    self.assertEqual([{'variant': missing, 'reason': selection.SKIP_REASON}],
                                     receipt['skipped_managers'])
                    self.assertEqual(set(expected), set(receipt['sources']))
                    self.assertTrue(all(row['effective_ksu_branch'] == 'Stable(标准)'
                                        for row in receipt['sources'].values()))

    def test_all_missing_is_a_successful_empty_plan_with_an_explicit_summary(self):
        for group, requested in COMBINED_GROUPS.items():
            receipt = resolve(group, requested)
            self.assertEqual((), selection.active_managers(group, receipt))
            self.assertEqual({}, receipt['sources'])
            self.assertIn('本次不编译、不发行', selection.selection_summary(receipt))

    def test_single_manager_and_latest_do_not_silently_skip_or_change_tier(self):
        for group, tier, missing in (('BakaSU', '正式版', ('BakaSU',)),
                                     (COMBINED_SELECTION, '最新版', ('BakaSU',))):
            with self.subTest(group=group, tier=tier), self.assertRaises(sources.NoStableRelease):
                resolve(group, missing, tier)

    def test_lookup_errors_cannot_be_treated_as_missing_releases(self):
        with patch.object(selection, 'resolve_sources', side_effect=sources.SourceResolutionError('network failure')):
            with self.assertRaisesRegex(sources.SourceResolutionError, 'network failure'):
                selection.resolve_selection(COMBINED_SELECTION, '正式版')

    def test_actual_source_resolver_skips_only_missing_release_and_freezes_other_tags(self):
        missing = sources.REPOSITORIES['BakaSU']
        calls = []
        def api(repository, resource='', **kwargs):
            calls.append((repository, resource))
            if resource == '/releases/latest':
                return None if repository == missing else {'tag_name': 'v1.0', 'draft': False, 'prerelease': False}
            if resource == '/commits/v1.0':
                return {'sha': hashlib.sha1(repository.encode()).hexdigest()}
            if resource == '':
                return {'full_name': repository}
            raise AssertionError('Unexpected lookup or fallback: ' + resource)
        with patch.object(sources, 'github_json', side_effect=api):
            receipt = selection.resolve_selection(COMBINED_SELECTION, '正式版')
        self.assertEqual(['SukiSU', 'KernelSU-Next'], receipt['active_managers'])
        self.assertNotIn((missing, '/commits/v1.0'), calls)
        self.assertTrue(all(row['ksu_source_branch'] == 'v1.0' for row in receipt['sources'].values()))

    def test_missing_release_does_not_hide_an_inaccessible_or_wrong_repository(self):
        for identity in ({'full_name': 'unexpected/repository'}, {},
                         sources.SourceResolutionError('repository inaccessible')):
            with self.subTest(identity=identity), patch.object(sources, 'github_json', side_effect=[None, identity]):
                with self.assertRaises(sources.SourceResolutionError) as caught:
                    selection.resolve_selection(COMBINED_SELECTION, '正式版')
                self.assertNotIsInstance(caught.exception, sources.NoStableRelease)

    def test_invalid_or_prerelease_responses_fail_instead_of_skipping(self):
        for document in ({}, {'tag_name': None},
                         {'tag_name': 'v1.0', 'draft': True, 'prerelease': False},
                         {'tag_name': 'v1.0', 'draft': False, 'prerelease': True}):
            with self.subTest(document=document), patch.object(sources, 'github_json', return_value=document):
                with self.assertRaises(sources.SourceResolutionError) as caught:
                    selection.resolve_selection(COMBINED_SELECTION, '正式版')
                self.assertNotIsInstance(caught.exception, sources.NoStableRelease)

    def test_receipt_rejects_forged_subsets_sources_and_skip_reasons(self):
        original = resolve(missing=('BakaSU',))
        invalid = []
        for field, value in (('active_managers', ['KernelSU-Next', 'SukiSU']),
                             ('active_managers', ['SukiSU', 'SukiSU']),
                             ('active_managers', ['SukiSU', 'Unknown']),
                             ('skipped_managers', []), ('tier', '最新版')):
            row = copy.deepcopy(original)
            row[field] = value
            invalid.append(row)
        for mutation in ('reason', 'repository', 'commit', 'channel'):
            row = copy.deepcopy(original)
            if mutation == 'reason':
                row['skipped_managers'][0]['reason'] = 'build failed'
            else:
                field = {'repository': 'ksu_repo', 'commit': 'ksu_ref', 'channel': 'effective_ksu_branch'}[mutation]
                row['sources']['SukiSU'][field] = 'invalid'
            invalid.append(row)
        for row in invalid:
            with self.subTest(receipt=row), self.assertRaises(ValueError):
                selection.validate_receipt(row)
        with self.assertRaises(ValueError):
            selection.validate_receipt(original, selection='KowSU + KernelSU + FinaSU')
        with self.assertRaises(ValueError):
            selection.validate_receipt('{"schema_version":1,"schema_version":1}')

    def test_default_plan_keeps_all_requested_managers_and_latest_versions(self):
        for group in COMBINED_GROUPS:
            self.assertEqual(selected_managers(group), selection.active_managers(group))
            receipt = resolve(group, tier='最新版')
            self.assertEqual(list(selected_managers(group)), receipt['active_managers'])
            self.assertEqual([], receipt['skipped_managers'])

    def test_source_worker_uses_the_frozen_receipt_without_another_upstream_lookup(self):
        receipt = resolve(missing=('BakaSU',))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'outputs'
            values = {'MANAGER_RESOLUTION': json.dumps(receipt), 'GITHUB_OUTPUT': str(output)}
            with patch.dict(os.environ, values), patch.object(selection, 'resolve_sources', side_effect=AssertionError('Network re-resolution')):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, selection.main(['--source-for', 'SukiSU']))
                result = dict(line.split('=', 1) for line in output.read_text(encoding='utf-8').splitlines())
                self.assertEqual(receipt['sources']['SukiSU'], result)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(1, selection.main(['--source-for', 'BakaSU']))

    def test_all_missing_cli_writes_zero_active_count_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            output, summary = Path(directory) / 'output', Path(directory) / 'summary'
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)}):
                with patch.object(selection, 'resolve_sources', side_effect=sources.NoStableRelease('unavailable')):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(0, selection.main(['--selection', COMBINED_SELECTION, '--tier', '正式版']))
            values = dict(line.split('=', 1) for line in output.read_text(encoding='utf-8').splitlines())
            self.assertEqual('0', values['active_count'])
            self.assertEqual('[]', values['active_managers'])
            self.assertIn('本次不编译、不发行', summary.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
