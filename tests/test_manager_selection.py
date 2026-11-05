"""Resolve frozen combined sources without rebuilding an identical formal SHA."""
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


def source(variant, tier='正式版', reference=None, branch=None):
    channel = selection.effective_tier(tier)
    reference = reference or hashlib.sha1((variant + ('' if channel == 'Stable(标准)' else '@' + channel)).encode()).hexdigest()
    return sources.source_result(reference, sources.REPOSITORIES[variant],
                                 branch or ('v1.0' if channel == 'Stable(标准)' else 'main'), channel)


def resolve(group=COMBINED_SELECTION, missing=(), tier='正式版'):
    def choose(root, variant, channel, custom_ref, susfs):
        if variant in missing:
            raise sources.NoStableRelease('No published official stable release')
        return source(variant, channel)
    with patch.object(selection, 'resolve_sources', side_effect=choose):
        return selection.resolve_selection(group, tier, susfs=True)


def resolve_latest(group=COMBINED_SELECTION, same=(), unreleased=(), *, calls=None):
    """Latest and formal are distinct by default; same chooses a real equal SHA."""
    def choose(root, variant, tier, custom_ref, susfs):
        channel = selection.effective_tier(tier)
        if calls is not None:
            calls.append((variant, channel))
        if channel == 'Stable(标准)' and variant in unreleased:
            raise sources.NoStableRelease('Official repository has no formal release')
        reference = source(variant, '正式版')['ksu_ref'] if variant in same else None
        return source(variant, tier, reference=reference)
    with patch.object(selection, 'resolve_sources', side_effect=choose):
        return selection.resolve_selection(group, '最新版', susfs=True)


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

    def test_missing_single_formal_or_latest_source_is_not_silently_skipped(self):
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


class LatestManagerSelectionTests(unittest.TestCase):
    def test_both_groups_skip_each_identical_sha_position_and_freeze_both_proofs(self):
        for group, requested in COMBINED_GROUPS.items():
            for duplicate in requested:
                calls = []
                with self.subTest(group=group, duplicate=duplicate):
                    receipt = resolve_latest(group, same=(duplicate,), calls=calls)
                    active = [name for name in requested if name != duplicate]
                    self.assertEqual(tuple(active), selection.active_managers(group, receipt))
                    self.assertEqual(set(active), set(receipt['sources']))
                    self.assertEqual([{'variant': duplicate, 'reason': selection.LATEST_STABLE_REASON,
                        'latest_source': source(duplicate, '最新版', reference=source(duplicate)['ksu_ref']),
                        'stable_source': source(duplicate)}], receipt['skipped_managers'])
                    self.assertEqual([(name, tier) for name in requested
                                      for tier in ('Latest(最新)', 'Stable(标准)')], calls)
                    self.assertTrue(all(record['effective_ksu_branch'] == 'Latest(最新)'
                                        for record in receipt['sources'].values()))

    def test_every_identical_latest_is_a_successful_empty_plan_and_explains_why(self):
        for group, requested in COMBINED_GROUPS.items():
            with self.subTest(group=group):
                receipt = resolve_latest(group, same=requested)
                self.assertEqual((), selection.active_managers(group, receipt))
                self.assertEqual({}, receipt['sources'])
                self.assertEqual(list(requested), [row['variant'] for row in receipt['skipped_managers']])
                summary = selection.selection_summary(receipt)
                self.assertIn(selection.LATEST_STABLE_REASON, summary)
                self.assertIn('本次不编译、不发行', summary)
                self.assertNotIn('均未发布正式版', summary)

    def test_missing_formal_release_keeps_latest_for_each_position_in_both_groups(self):
        for group, requested in COMBINED_GROUPS.items():
            for missing in requested:
                with self.subTest(group=group, missing=missing):
                    receipt = resolve_latest(group, unreleased=(missing,))
                    self.assertEqual(list(requested), receipt['active_managers'])
                    self.assertEqual([], receipt['skipped_managers'])
                    self.assertEqual(source(missing, '最新版'), receipt['sources'][missing])

    def test_single_latest_never_performs_a_formal_lookup_even_when_sha_would_match(self):
        for group, requested in COMBINED_GROUPS.items():
            for variant in requested:
                calls = []
                with self.subTest(variant=variant):
                    receipt = resolve_latest(variant, same=(variant,), calls=calls)
                    self.assertEqual([(variant, 'Latest(最新)')], calls)
                    self.assertEqual([variant], receipt['active_managers'])
                    self.assertEqual([], receipt['skipped_managers'])

    def test_equal_version_labels_with_different_shas_do_not_skip(self):
        for group, requested in COMBINED_GROUPS.items():
            def choose(root, variant, tier, custom_ref, susfs):
                return source(variant, tier, branch='v4.3.0')
            with self.subTest(group=group), patch.object(selection, 'resolve_sources', side_effect=choose):
                receipt = selection.resolve_selection(group, '最新版', susfs=True)
            self.assertEqual(list(requested), receipt['active_managers'])
            self.assertEqual([], receipt['skipped_managers'])

    def test_network_or_invalid_formal_lookup_errors_are_not_treated_as_missing(self):
        for group in COMBINED_GROUPS:
            for reason in ('official formal network failure', 'invalid formal response'):
                def choose(root, variant, tier, custom_ref, susfs):
                    if selection.effective_tier(tier) == 'Stable(标准)':
                        raise sources.SourceResolutionError(reason)
                    return source(variant, tier)
                with self.subTest(group=group, reason=reason), patch.object(selection, 'resolve_sources', side_effect=choose):
                    with self.assertRaisesRegex(sources.SourceResolutionError, reason):
                        selection.resolve_selection(group, '最新版', susfs=True)

    def test_real_resolver_rejects_draft_prerelease_and_malformed_formal_responses(self):
        invalid = ({}, {'tag_name': None}, {'tag_name': 'v1.0', 'draft': True, 'prerelease': False},
                   {'tag_name': 'v1.0', 'draft': False, 'prerelease': True})
        for group in COMBINED_GROUPS:
            for formal in invalid:
                def api(repository, resource='', **kwargs):
                    if resource == '':
                        return {'full_name': repository, 'default_branch': 'main'}
                    if resource == '/commits/main':
                        return {'sha': hashlib.sha1((repository + '@latest').encode()).hexdigest()}
                    if resource == '/branches/dev':
                        return None
                    if resource == '/releases/latest':
                        return formal
                    raise AssertionError('Unexpected API lookup: ' + resource)
                with self.subTest(group=group, formal=formal), patch.object(sources, 'github_json', side_effect=api):
                    with self.assertRaises(sources.SourceResolutionError) as caught:
                        selection.resolve_selection(group, '最新版', susfs=True)
                    self.assertNotIsInstance(caught.exception, sources.NoStableRelease)

    def test_real_missing_formal_response_keeps_latest_only_for_identified_repository(self):
        for group, requested in COMBINED_GROUPS.items():
            def api(repository, resource='', **kwargs):
                if resource == '': return {'full_name': repository, 'default_branch': 'main'}
                if resource == '/commits/main': return {'sha': hashlib.sha1(repository.encode()).hexdigest()}
                if resource in ('/branches/dev', '/releases/latest'): return None
                raise AssertionError('Unexpected API lookup: ' + resource)
            with self.subTest(group=group), patch.object(sources, 'github_json', side_effect=api):
                receipt = selection.resolve_selection(group, '最新版', susfs=True)
            self.assertEqual(list(requested), receipt['active_managers'])
            self.assertEqual([], receipt['skipped_managers'])

    def test_proof_schema_tampering_cannot_claim_a_latest_formal_duplicate(self):
        for group, requested in COMBINED_GROUPS.items():
            original = resolve_latest(group, same=(requested[1],))
            for mutation in ('missing_latest', 'missing_stable', 'extra_field', 'reason', 'sha', 'uppercase',
                             'latest_channel', 'stable_channel', 'repo', 'short', 'invalid_ref', 'tier', 'single'):
                row = copy.deepcopy(original)
                skipped = row['skipped_managers'][0]
                if mutation == 'missing_latest': skipped.pop('latest_source')
                elif mutation == 'missing_stable': skipped.pop('stable_source')
                elif mutation == 'extra_field': skipped['version'] = 'latest'
                elif mutation == 'reason': skipped['reason'] = selection.SKIP_REASON
                elif mutation == 'sha': skipped['stable_source'].update(ksu_ref='b' * 40, ksu_short='b' * 7)
                elif mutation == 'uppercase':
                    for name in ('latest_source', 'stable_source'):
                        skipped[name]['ksu_ref'] = skipped[name]['ksu_ref'].upper()
                        skipped[name]['ksu_short'] = skipped[name]['ksu_ref'][:7]
                elif mutation == 'latest_channel': skipped['latest_source']['effective_ksu_branch'] = 'Stable(标准)'
                elif mutation == 'stable_channel': skipped['stable_source']['effective_ksu_branch'] = 'Latest(最新)'
                elif mutation == 'repo': skipped['stable_source']['ksu_repo'] = sources.REPOSITORIES[requested[0]]
                elif mutation == 'short': skipped['stable_source']['ksu_short'] = 'bad'
                elif mutation == 'invalid_ref': skipped['stable_source']['ksu_source_branch'] = '../untrusted'
                elif mutation == 'tier': row['tier'] = '正式版'
                else:
                    row.update(selection=requested[1], active_managers=[], sources={})
                with self.subTest(group=group, mutation=mutation), self.assertRaises((ValueError, sources.SourceResolutionError)):
                    selection.validate_receipt(row)

    def test_source_worker_reuses_latest_receipt_without_second_source_or_formal_lookup(self):
        for group, requested in COMBINED_GROUPS.items():
            receipt = resolve_latest(group, same=(requested[0],))
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'outputs'
                with patch.dict(os.environ, {'MANAGER_RESOLUTION': json.dumps(receipt), 'GITHUB_OUTPUT': str(output)}), \
                        patch.object(selection, 'resolve_sources', side_effect=AssertionError('Second upstream lookup')):
                    for variant in requested[1:]:
                        output.write_text('', encoding='utf-8')
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(0, selection.main(['--source-for', variant]))
                        values = dict(line.split('=', 1) for line in output.read_text('utf-8').splitlines())
                        self.assertEqual(receipt['sources'][variant], values)
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(1, selection.main(['--source-for', requested[0]]))

    def test_all_identical_cli_emits_zero_active_count_and_keeps_skip_evidence(self):
        for group, requested in COMBINED_GROUPS.items():
            with tempfile.TemporaryDirectory() as directory:
                output, summary = Path(directory) / 'outputs', Path(directory) / 'summary'
                def choose(root, variant, tier, custom_ref, susfs):
                    return source(variant, tier, reference=source(variant)['ksu_ref'])
                with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)}), \
                        patch.object(selection, 'resolve_sources', side_effect=choose), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, selection.main(['--selection', group, '--tier', '最新版', '--susfs', 'true']))
                values = dict(line.split('=', 1) for line in output.read_text('utf-8').splitlines())
                self.assertEqual('0', values['active_count'])
                self.assertEqual('[]', values['active_managers'])
                receipt = selection.validate_receipt(values['resolution'])
                self.assertEqual(3, len(receipt['skipped_managers']))
                self.assertIn('本次不编译、不发行', summary.read_text('utf-8'))


if __name__ == '__main__':
    unittest.main()
