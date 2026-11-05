"""Exercise GitHub API contracts with immutable JSON blobs; never merge online."""
import base64
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import auto_merge_official_update as merge
from check_official_updates import TARGETS
from upstream_pr_guard import OWNERSHIP_MARKER

BASE, HEAD, COMMIT = 'b' * 40, 'a' * 40, 'd' * 40
REPO = 'owner/gki'
KERNELS = 'data/kernels.json'
CANDIDATES = 'data/upstream-candidates.json'


def contents(path, document):
    raw = document if isinstance(document, bytes) else json.dumps(document, sort_keys=True).encode()
    digest = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    return {'type': 'file', 'path': path, 'size': len(raw), 'sha': digest,
            'encoding': 'base64', 'content': base64.b64encode(raw).decode()}


class FakeAPI:
    """Representative GET PR/refs/files/compare/Contents and guarded merge PUT."""
    def __init__(self):
        self.calls, self.mutations = [], []
        self.pr_reads, self.base_reads, self.head_reads = 0, 0, 0
        self.drift = ''
        self.repo = {'full_name': REPO, 'default_branch': 'main'}
        self.pr = {'number': 1, 'state': 'open', 'draft': False, 'merged': False,
                   'body': OWNERSHIP_MARKER + '\n<!-- gki-official-heads:sha256=' + '1' * 64 + ' -->',
                   'user': {'login': 'owner', 'type': 'User'}, 'changed_files': 2, 'mergeable': True,
                   'head': {'sha': HEAD, 'ref': 'automation/upstream-sync', 'repo': {'full_name': REPO}},
                   # GitHub can keep the PR creation base while main advances.
                   'base': {'sha': 'c' * 40, 'ref': 'main', 'repo': {'full_name': REPO}}}
        old = {'android_version': 'android13', 'kernel_version': '5.15', 'kernel': '5.15.41',
               'date': '2022-06', 'ref': 'refs/heads/android13-5.15-2022-06', 'revision': 'r1'}
        added = {**old, 'kernel': '5.15.50', 'date': '2022-07', 'ref': '2' * 40}
        profile = {'schema_version': 1, 'sukisu_susfs': {
            'ksu_commits': ['c' * 40], 'source_ref': 'c' * 40, 'version': 'v2.3.0',
            'tiers': ['Stable(标准)'], 'pins': {target: 'd' * 40 for target in TARGETS}}}
        baseline = {'sukisu': 'c' * 40, 'susfs_version': 'v2.3.0',
                    'susfs': {target: 'd' * 40 for target in TARGETS}}
        snapshot = {'schema_version': 1, 'sukisu': {'branch': 'main', 'head': 'e' * 40, 'release': None},
                    'susfs': {target: 'f' * 40 for target in TARGETS}}
        candidate = {'schema_version': 1, 'status': 'candidates-require-validation', 'baseline': baseline,
                     'sources': snapshot, 'acknowledged_heads': merge.head_state(snapshot),
                     'changes': merge.changes(baseline, snapshot)}
        self.documents = {(KERNELS, BASE): {'schema_version': 1, 'entries': [old]},
                          (KERNELS, HEAD): {'schema_version': 1, 'entries': [old, added]},
                          (CANDIDATES, HEAD): candidate, (merge.PROFILE, BASE): profile}
        self.files = []
        self.comparison = {'status': 'diverged', 'base_commit': {'sha': BASE}, 'total_commits': 1,
                           'commits': [{'sha': HEAD}], 'files': []}
        self.response = {'merged': True, 'sha': COMMIT, 'message': 'Pull Request successfully merged'}
        self.refresh()

    def refresh(self):
        self.files = [{'filename': path, 'status': 'modified' if path == KERNELS else 'added',
                       'sha': contents(path, self.documents[path, HEAD])['sha']}
                      for path in (KERNELS, CANDIDATES)]
        self.comparison['files'] = copy.deepcopy(self.files)

    def request(self, path, *, document=None, missing=False):
        self.calls.append(path)
        if document is not None:
            self.mutations.append((path, document))
            return copy.deepcopy(self.response)
        if path == '':
            return copy.deepcopy(self.repo)
        if path == '/pulls/1':
            self.pr_reads += 1
            value = copy.deepcopy(self.pr)
            if self.drift == 'pr' and self.pr_reads > 1:
                value['head']['sha'] = '9' * 40
            return value
        if path.startswith('/git/ref/heads/'):
            branch = path.removeprefix('/git/ref/heads/')
            if branch == 'main':
                self.base_reads += 1
                digest = '8' * 40 if self.drift == 'base' and self.base_reads > 1 else BASE
            else:
                self.head_reads += 1
                digest = '7' * 40 if self.drift == 'head' and self.head_reads > 1 else HEAD
            return {'ref': 'refs/heads/' + branch, 'object': {'type': 'commit', 'sha': digest}}
        if path == '/pulls/1/files?per_page=100&page=1':
            return copy.deepcopy(self.files)
        if path == '/pulls/1/files?per_page=100&page=2':
            return []
        if path.startswith('/compare/'):
            return copy.deepcopy(self.comparison)
        if path.startswith('/contents/'):
            filename, ref = path.removeprefix('/contents/').split('?ref=')
            document = self.documents.get((filename, ref))
            if document is None and missing:
                return None
            return contents(filename, document)
        raise AssertionError('Unexpected API path: ' + path)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        validation = patch.object(merge, 'validate_manager_snapshot', return_value={'passed': True})
        self.compatibility = validation.start()
        self.addCleanup(validation.stop)

    def sync(self):
        return merge.sync(REPO, 1, 'main', self.api)

    def refused(self):
        with self.assertRaises((merge.MergeError, merge.UpdateError, merge.SourceResolutionError, ValueError)):
            self.sync()
        self.assertEqual(self.api.mutations, [])

    def test_current_base_comparison_validates_data_then_sha_bound_squash(self):
        result = self.sync()
        self.assertEqual({'merged': True, 'pull_request_url': 'https://github.com/owner/gki/pull/1',
                          'merge_commit': COMMIT}, result)
        self.assertIn('/compare/' + BASE + '...' + HEAD + '?per_page=100&page=1', self.api.calls)
        self.assertIn('/contents/' + KERNELS + '?ref=' + BASE, self.api.calls)
        self.assertEqual([('/pulls/1/merge', {'sha': HEAD, 'merge_method': 'squash'})], self.api.mutations)
        self.assertEqual(self.api.documents[CANDIDATES, HEAD]['sources'], self.compatibility.call_args.args[0])

    def test_source_integration_failure_prevents_merge_and_keeps_pending_pr(self):
        self.compatibility.side_effect = merge.ManagerUpdateError('SUSFS/KPM integration failed')
        self.refused()
        self.compatibility.assert_called_once()

    def test_source_drift_during_integration_validation_prevents_merge(self):
        self.compatibility.side_effect = lambda *args, **kwargs: setattr(self.api, 'drift', 'head')
        self.refused()

    def test_trusted_owner_pat_or_real_actions_bot_are_supported(self):
        self.api.pr['user'] = {'login': 'github-actions[bot]', 'type': 'Bot'}
        self.assertTrue(self.sync()['merged'])

    def test_foreign_branch_author_marker_draft_or_non_default_base_is_refused(self):
        for change in ('fork', 'branch', 'author', 'marker', 'snapshot-marker', 'draft', 'base'):
            self.api = FakeAPI()
            if change == 'fork': self.api.pr['head']['repo']['full_name'] = 'other/gki'
            if change == 'branch': self.api.pr['head']['ref'] = 'human-branch'
            if change == 'author': self.api.pr['user']['login'] = 'untrusted'
            if change == 'marker': self.api.pr['body'] = 'human PR'
            if change == 'snapshot-marker': self.api.pr['body'] = OWNERSHIP_MARKER
            if change == 'draft': self.api.pr['draft'] = True
            if change == 'base': self.api.repo['default_branch'] = 'release'
            with self.subTest(change=change): self.refused()

    def test_changes_to_pins_extra_files_deletions_renames_are_never_merged(self):
        for change in ({'filename': 'build-compatibility.json'}, {'filename': '.github/workflows/x.yml'},
                       {'status': 'removed'}, {'status': 'renamed', 'previous_filename': 'old.json'}):
            self.api = FakeAPI()
            self.api.files[0].update(change)
            with self.subTest(change=change): self.refused()

    def test_compare_and_pr_file_inventory_must_match_without_overflow(self):
        for change in ('extra', 'different-blob', 'missing', 'overflow', 'wrong-base', 'wrong-head'):
            self.api = FakeAPI()
            compare = self.api.comparison
            if change == 'extra': compare['files'].append({'filename': 'README.md', 'status': 'modified', 'sha': HEAD})
            if change == 'different-blob': compare['files'][0]['sha'] = '6' * 40
            if change == 'missing': compare['files'].pop()
            if change == 'overflow': compare['total_commits'] = 101
            if change == 'wrong-base': compare['base_commit']['sha'] = '5' * 40
            if change == 'wrong-head': compare['commits'][0]['sha'] = '4' * 40
            with self.subTest(change=change): self.refused()

    def test_old_entry_rewrite_deletion_duplicate_or_new_mutable_ref_is_refused(self):
        for change in ('rewrite', 'delete', 'duplicate', 'mutable', 'sha64', 'month', 'pair', 'extra', 'revision'):
            self.api = FakeAPI()
            entries = self.api.documents[KERNELS, HEAD]['entries']
            if change == 'rewrite': entries[0] = {**entries[0], 'revision': 'r2'}
            if change == 'delete': entries.pop(0)
            if change == 'duplicate': entries.append(copy.deepcopy(entries[0]))
            if change == 'mutable': entries[1]['ref'] = 'refs/heads/new'
            if change == 'sha64': entries[1]['ref'] = '4' * 64
            if change == 'month': entries[1]['date'] = '2026-13'
            if change == 'pair': entries[1]['android_version'] = 'android14'
            if change == 'extra': entries[1]['command'] = 'never execute'
            if change == 'revision': entries[1]['revision'] = 'r1; unsafe'
            self.api.refresh()
            with self.subTest(change=change): self.refused()

    def test_existing_rows_may_reorder_without_changing_their_contents(self):
        self.api.documents[KERNELS, HEAD]['entries'].reverse()
        self.api.refresh()
        self.assertTrue(self.sync()['merged'])

    def test_candidate_schema_baseline_sources_acknowledgement_and_changes_are_exact(self):
        for change in ('schema', 'extra', 'status', 'baseline', 'source', 'ack', 'changes'):
            self.api = FakeAPI()
            candidate = self.api.documents[CANDIDATES, HEAD]
            if change == 'schema': candidate['schema_version'] = True
            if change == 'extra': candidate['run_command'] = 'never execute'
            if change == 'status': candidate['status'] = 'approved'
            if change == 'baseline': candidate['baseline']['susfs_version'] = 'v2.2.0'
            if change == 'source': candidate['sources']['sukisu']['head'] = 'moving-main'
            if change == 'ack': candidate['acknowledged_heads'] = {}
            if change == 'changes': candidate['changes'] = []
            self.api.refresh()
            with self.subTest(change=change): self.refused()

    def test_development_candidates_can_merge_with_exact_extended_acknowledgement(self):
        for development in (None, {'branch': 'dev', 'head': '9' * 40}):
            self.api = FakeAPI()
            candidate = self.api.documents[CANDIDATES, HEAD]
            candidate['sources']['sukisu']['development'] = development
            candidate['acknowledged_heads'] = merge.head_state(candidate['sources'])
            candidate['changes'] = merge.changes(candidate['baseline'], candidate['sources'])
            self.api.refresh()
            with self.subTest(development=development):
                self.assertTrue(self.sync()['merged'])
            self.assertEqual([('/pulls/1/merge', {'sha': HEAD, 'merge_method': 'squash'})], self.api.mutations)

    def test_baka_optional_candidates_merge_without_changing_the_legacy_baseline(self):
        candidate = self.api.documents[CANDIDATES, HEAD]
        original_baseline = copy.deepcopy(candidate['baseline'])
        candidate['sources']['bakasu'] = {'branch': 'main', 'head': '8' * 40, 'release': None,
                                          'prerelease': {'tag': 'v4.2.0-rc3', 'commit': '9' * 40}}
        candidate['acknowledged_heads'] = merge.head_state(candidate['sources'])
        candidate['changes'] = merge.changes(candidate['baseline'], candidate['sources'])
        self.api.refresh()
        self.assertTrue(self.sync()['merged'])
        self.assertEqual(original_baseline, candidate['baseline'])
        self.assertEqual([('/pulls/1/merge', {'sha': HEAD, 'merge_method': 'squash'})], self.api.mutations)

    def test_baka_extra_fields_unpinned_sources_or_wrong_ack_never_merge(self):
        for change in ('head', 'extra', 'release', 'ack', 'changes'):
            self.api = FakeAPI()
            candidate = self.api.documents[CANDIDATES, HEAD]
            candidate['sources']['bakasu'] = {'branch': 'main', 'head': '8' * 40,
                                              'release': None, 'prerelease': None}
            candidate['acknowledged_heads'] = copy.deepcopy(merge.head_state(candidate['sources']))
            candidate['changes'] = merge.changes(candidate['baseline'], candidate['sources'])
            baka = candidate['sources']['bakasu']
            if change == 'head': baka['head'] = 'main'
            if change == 'extra': baka['repository'] = 'untrusted/other'
            if change == 'release': baka['release'] = {'tag': 'v4', 'commit': 'moving-tag'}
            if change == 'ack': candidate['acknowledged_heads'].pop('bakasu')
            if change == 'changes': candidate['changes'] = [item for item in candidate['changes'] if item['source'] != 'BakaSU']
            self.api.refresh()
            with self.subTest(change=change): self.refused()

    def test_development_field_cannot_hide_wrong_branch_unpinned_source_or_missing_acknowledgement(self):
        for change in ('branch', 'head', 'extra', 'ack', 'changes'):
            self.api = FakeAPI()
            candidate = self.api.documents[CANDIDATES, HEAD]
            candidate['sources']['sukisu']['development'] = {'branch': 'dev', 'head': '9' * 40}
            candidate['acknowledged_heads'] = copy.deepcopy(merge.head_state(candidate['sources']))
            candidate['changes'] = merge.changes(candidate['baseline'], candidate['sources'])
            development = candidate['sources']['sukisu']['development']
            if change == 'branch': development['branch'] = 'arbitrary-feature'
            if change == 'head': development['head'] = 'dev'
            if change == 'extra': development['command'] = 'never execute'
            if change == 'ack': candidate['acknowledged_heads'].pop('sukisu_development')
            if change == 'changes': candidate['changes'] = [item for item in candidate['changes']
                                                            if item['source'] != 'SukiSU development']
            self.api.refresh()
            with self.subTest(change=change): self.refused()

    def test_changed_head_or_actual_base_during_validation_blocks_merge(self):
        for drift in ('pr', 'base', 'head'):
            self.api = FakeAPI()
            self.api.drift = drift
            with self.subTest(drift=drift): self.refused()

    def test_already_merged_is_idempotent_but_foreign_merged_pr_is_not_accepted(self):
        self.api.pr.update(merged=True, state='closed', merge_commit_sha=COMMIT)
        self.assertTrue(self.sync()['merged'])
        self.assertEqual([], self.api.mutations)
        self.assertEqual(['', '/pulls/1'], self.api.calls)
        self.api.pr['head']['repo']['full_name'] = 'foreign/gki'
        self.refused()

    def test_repository_rules_or_unsuccessful_merge_are_not_bypassed(self):
        self.api.response = {'merged': False, 'message': 'Required checks have not passed'}
        with self.assertRaisesRegex(merge.MergeError, 'repository rules'):
            self.sync()
        self.assertEqual(self.api.mutations[0][1], {'sha': HEAD, 'merge_method': 'squash'})


class ScopedMergeTests(unittest.TestCase):
    setUp = MergeTests.setUp
    sync = MergeTests.sync
    refused = MergeTests.refused

    def select_files(self, *paths):
        self.api.files = [{'filename': path,
                           'status': 'modified' if (path, BASE) in self.api.documents else 'added',
                           'sha': contents(path, self.api.documents[path, HEAD])['sha']} for path in paths]
        self.api.pr['changed_files'] = len(paths)
        self.api.comparison['files'] = copy.deepcopy(self.api.files)

    def candidate_pair(self):
        current = self.api.documents[CANDIDATES, HEAD]
        previous = copy.deepcopy(current)
        self.api.documents[CANDIDATES, BASE] = previous
        return previous, current

    def refresh_candidates(self):
        for ref in (BASE, HEAD):
            value = self.api.documents.get((CANDIDATES, ref))
            if value is not None:
                value['acknowledged_heads'] = merge.head_state(value['sources'])
                value['changes'] = merge.changes(value['baseline'], value['sources'])

    def susfs_scope(self, *, advance=False):
        import update_susfs_sources as update
        from test_update_susfs_sources import profiles, transport, DIFFERENT, NEW
        profile = copy.deepcopy(self.api.documents[merge.PROFILE, BASE])
        managers = profiles()[1]
        for row in managers['managers'].values():
            row['pins'] = dict(profile['sukisu_susfs']['pins'])
        candidate = NEW if advance else DIFFERENT
        receipt, after_profile, after_managers = update.plan(
            {target: candidate for target in TARGETS}, profile, managers, fetch=transport)
        self.api.documents.update({(merge.PROFILE, BASE): profile,
                                   (merge.PROFILE, HEAD): after_profile,
                                   (merge.MANAGER_PINS, BASE): managers,
                                   (merge.MANAGER_PINS, HEAD): after_managers,
                                   (merge.SUSFS_RECEIPT, HEAD): receipt})
        self.select_files(*((merge.PROFILE, merge.MANAGER_PINS, merge.SUSFS_RECEIPT)
                            if advance else (merge.SUSFS_RECEIPT,)))
        return update, transport

    def oplus_scope(self):
        import update_oplus_sources as update
        from test_update_oplus_sources import FakeReader
        manifest = json.loads((ROOT / merge.OPLUS_PROFILES).read_text(encoding='utf-8'))
        manifest['profiles'] = manifest['profiles'][:1]
        row = manifest['profiles'][0]
        path = update.recipe_path(row)
        raw = (ROOT / path).read_bytes()
        reader = FakeReader(row, raw)
        reader.timestamp = row['source_committed_at']
        reader.head = row['kernel_ref']
        reader.recipe_head = row['recipe_ref']
        receipt = {'schema_version': 1,
                   'recipe_heads': {repo: {'branch': 'main', 'ref': row['recipe_ref']}
                                    for repo in update.catalog.RECIPE_REPOSITORIES},
                   'profiles': [{'id': row['id'], 'source_ref': row['kernel_ref'],
                                 'source_date': row['source_committed_at'], 'source_version': row['kernel'],
                                 'recipe_ref': row['recipe_ref'], 'recipe_sha256': row['recipe_sha256'],
                                 'recipe_status': 'unchanged'}], 'new_recipes': []}
        self.api.documents.update({(merge.OPLUS_PROFILES, BASE): manifest,
                                   (merge.OPLUS_PROFILES, HEAD): manifest,
                                   (merge.OPLUS_RECEIPT, HEAD): receipt,
                                   (path, BASE): raw})
        self.select_files(merge.OPLUS_RECEIPT)
        return update, reader

    def test_gki_catalog_only_merges_without_reading_or_validating_unrelated_manager_sources(self):
        self.select_files(KERNELS)
        del self.api.documents[CANDIDATES, HEAD]
        self.compatibility.side_effect = RuntimeError('Unrelated manager network must never run')
        self.assertTrue(self.sync()['merged'])
        self.compatibility.assert_not_called()
        self.assertFalse(any('/contents/' + CANDIDATES in path for path in self.api.calls))
        self.assertIn('/contents/' + KERNELS + '?ref=' + BASE, self.api.calls)
        self.assertEqual(1, len(self.api.mutations))

    def test_receipt_only_susfs_update_keeps_its_real_validator_without_manager_network(self):
        update, transport = self.susfs_scope()
        self.compatibility.side_effect = RuntimeError('Unrelated manager network must never run')
        with patch.object(update, 'request_bytes', side_effect=transport) as remote, \
                patch.object(update, 'validate_update', wraps=update.validate_update) as validate:
            self.assertTrue(self.sync()['merged'])
        validate.assert_called_once()
        self.assertGreater(remote.call_count, 0)
        self.compatibility.assert_not_called()
        self.assertFalse(any('/contents/' + CANDIDATES in path for path in self.api.calls))

    def test_receipt_only_susfs_tampering_still_prevents_merge(self):
        update, transport = self.susfs_scope()
        self.api.documents[merge.SUSFS_RECEIPT, HEAD]['candidates'][TARGETS[0]]['build_tree_sha256'] = '0' * 64
        self.select_files(merge.SUSFS_RECEIPT)
        with patch.object(update, 'request_bytes', side_effect=transport):
            self.refused()
        self.compatibility.assert_not_called()

    def test_oplus_only_keeps_real_data_and_remote_byte_validation_without_manager_network(self):
        update, reader = self.oplus_scope()
        self.compatibility.side_effect = RuntimeError('Unrelated manager network must never run')
        with patch.object(update, 'Reader', return_value=reader), \
                patch.object(update, 'validate_update', wraps=update.validate_update) as validate, \
                patch.object(update, 'verify_remote_snapshot', wraps=update.verify_remote_snapshot) as verify:
            self.assertTrue(self.sync()['merged'])
        validate.assert_called_once()
        verify.assert_called_once()
        self.assertTrue(any(suffix.startswith('git/commits/') for _, suffix in reader.calls))
        self.assertTrue(any('contents/Makefile?ref=' in suffix for _, suffix in reader.calls))
        self.compatibility.assert_not_called()
        self.assertFalse(any('/contents/' + CANDIDATES in path for path in self.api.calls))

    def test_bad_oplus_remote_bytes_still_prevent_merge_when_manager_validation_is_unrelated(self):
        update, reader = self.oplus_scope()
        reader.version = '6.1.999'
        with patch.object(update, 'Reader', return_value=reader):
            self.refused()
        self.compatibility.assert_not_called()

    def test_actual_susfs_pin_change_still_requires_manager_integration_and_keeps_failures_fatal(self):
        for fails in (False, True):
            self.api = FakeAPI()
            self.compatibility.reset_mock()
            self.compatibility.side_effect = merge.ManagerUpdateError('Integration failed') if fails else None
            update, transport = self.susfs_scope(advance=True)
            with self.subTest(fails=fails), patch.object(update, 'request_bytes', side_effect=transport):
                if fails:
                    self.refused()
                else:
                    self.assertTrue(self.sync()['merged'])
            self.compatibility.assert_called_once()

    def test_susfs_only_change_in_official_snapshot_does_not_repeat_manager_validation(self):
        previous, _ = self.candidate_pair()
        previous['sources']['susfs'] = {target: 'd' * 40 for target in TARGETS}
        self.refresh_candidates()
        self.select_files(CANDIDATES)
        self.compatibility.side_effect = RuntimeError('Unrelated manager network must never run')
        self.assertTrue(self.sync()['merged'])
        self.compatibility.assert_not_called()
        self.api = FakeAPI()
        previous, current = self.candidate_pair()
        current['changes'] = []  # Scope selection never bypasses the candidate data validator.
        self.select_files(CANDIDATES)
        self.refused()
        self.compatibility.assert_not_called()

    def test_each_manager_head_or_formal_release_and_suki_dev_change_still_require_validation(self):
        cases = [(name, channel) for name in merge.MANAGERS for channel in ('head', 'release')]
        cases += [('SukiSU', 'development')]
        for name, channel in cases:
            self.api = FakeAPI()
            self.compatibility.reset_mock()
            previous, current = self.candidate_pair()
            snapshot = current['sources']
            snapshot['sukisu'].update(release={'tag': 'v1', 'commit': 'e' * 40, 'newer_than_baseline': False},
                                      development={'branch': 'dev', 'head': '5' * 40})
            snapshot['bakasu'] = {'branch': 'main', 'head': '6' * 40,
                                 'release': {'tag': 'v1', 'commit': '6' * 40}, 'prerelease': None}
            for variant in merge.MANAGERS[2:]:
                snapshot[variant.lower()] = {'branch': 'main', 'head': '7' * 40,
                                            'release': {'tag': 'v1', 'commit': '7' * 40}}
            previous['sources'] = copy.deepcopy(snapshot)
            if channel == 'head':
                snapshot[name.lower()]['head'] = '9' * 40
            elif channel == 'development':
                snapshot['sukisu']['development']['head'] = '9' * 40
            else:
                snapshot[name.lower()]['release'].update(tag='v2', commit='9' * 40)
            self.refresh_candidates()
            self.select_files(CANDIDATES)
            with self.subTest(manager=name, channel=channel):
                self.assertTrue(self.sync()['merged'])
                self.compatibility.assert_called_once()

    def test_added_or_removed_manager_record_is_not_misclassified_as_susfs_only(self):
        for kind in ('added', 'removed'):
            self.api = FakeAPI()
            self.compatibility.reset_mock()
            previous, current = self.candidate_pair()
            selected = current if kind == 'added' else previous
            selected['sources']['kowsu'] = {'branch': 'main', 'head': '7' * 40, 'release': None}
            self.refresh_candidates()
            self.select_files(CANDIDATES)
            with self.subTest(kind=kind):
                self.assertTrue(self.sync()['merged'])
                self.compatibility.assert_called_once()

    def test_skipped_report_records_not_run_and_never_claims_a_passing_integration(self):
        self.select_files(KERNELS)
        directory = ROOT / 'tests' / ('.auto-scope-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        path = directory / 'compatibility.json'
        self.assertTrue(merge.sync(REPO, 1, 'main', self.api, compatibility_report=path)['merged'])
        result = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual('skipped', result['status'])
        self.assertNotIn('passed', result)
        body = path.with_suffix('.md').read_text(encoding='utf-8')
        self.assertIn('不重复运行管理器配套验证', body)
        self.assertIn('跳过不代表重新验证通过', body)
        self.assertNotIn('配套验证通过。', body)


class ClientTests(unittest.TestCase):
    def setUp(self):
        validation = patch.object(merge, 'validate_manager_snapshot', return_value={'passed': True})
        validation.start()
        self.addCleanup(validation.stop)

    def test_fixed_api_only_bounded_json_and_exact_put_contract(self):
        response = io.BytesIO(b'{"merged":true,"sha":"' + COMMIT.encode() + b'"}')
        response.headers = {}
        opener = Mock(open=Mock(return_value=response))
        api = merge.GitHub(REPO, 'private-token', opener)
        api.request('/pulls/1/merge', document={'sha': HEAD, 'merge_method': 'squash'})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'PUT')
        self.assertEqual(request.full_url, 'https://api.github.com/repos/owner/gki/pulls/1/merge')
        self.assertEqual(json.loads(request.data), {'sha': HEAD, 'merge_method': 'squash'})
        for suffix in ('https://attacker.example', '//attacker.example', '/contents/.github/workflows/x.yml?ref=' + HEAD,
                       '/git/ref/heads/../main', '/pulls/1#x'):
            with self.subTest(suffix=suffix), self.assertRaises(merge.MergeError): api.request(suffix)
        self.assertEqual(opener.open.call_count, 1)

    def test_redirects_pagination_oversized_or_invalid_json_never_continue(self):
        opener = Mock()
        opener.open.side_effect = HTTPError('https://api.github.com', 302, 'private-token',
                                            {'Location': 'https://attacker.example'}, None)
        with self.assertRaises(merge.MergeError): merge.GitHub(REPO, 'private-token', opener).request('')
        for raw, headers in ((b'{}', {'Link': '<https://api.github.com/page2>; rel="next"'}),
                             (b'x' * (merge.MAX_RESPONSE + 1), {}), (b'{"x":1,"x":2}', {})):
            response = io.BytesIO(raw); response.headers = headers
            opener.open.side_effect = None; opener.open.return_value = response
            with self.subTest(headers=headers), self.assertRaises(merge.MergeError):
                merge.GitHub(REPO, 'private-token', opener).request('')

    def test_contents_checks_real_git_blob_hash_base64_size_and_json(self):
        good = contents(KERNELS, {'schema_version': 1})
        for change in ({'sha': '9' * 40}, {'size': True}, {'encoding': 'none'}, {'content': '%%%'}):
            api = Mock(request=Mock(return_value={**good, **change}))
            with self.subTest(change=change), self.assertRaises(merge.MergeError):
                merge.read_file(api, KERNELS, HEAD)

    def test_cli_outputs_public_result_and_failure_never_exposes_token(self):
        directory = ROOT / 'tests' / ('.auto-merge-' + uuid.uuid4().hex)
        directory.mkdir(); self.addCleanup(shutil.rmtree, directory)
        output = directory / 'outputs'
        environment = {'GH_TOKEN': 'private-token', 'GITHUB_REPOSITORY': REPO, 'GITHUB_OUTPUT': str(output)}
        with patch.dict(os.environ, environment), patch.object(merge, 'GitHub', return_value=FakeAPI()):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, merge.main(['--pull-request', '1', '--base', 'main']))
        text = output.read_text()
        self.assertIn('merged=true\n', text)
        self.assertIn('pull_request_url=https://github.com/owner/gki/pull/1\n', text)
        self.assertIn('merge_commit=' + COMMIT, text)
        self.assertNotIn('private-token', text)
        errors = io.StringIO()
        with patch.dict(os.environ, environment), patch.object(merge, 'GitHub', side_effect=merge.MergeError('private-token')):
            with contextlib.redirect_stderr(errors):
                self.assertEqual(1, merge.main(['--pull-request', '1', '--base', 'main']))
        self.assertNotIn('private-token', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
