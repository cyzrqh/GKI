import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import remove_previous_update_checks as removal

WORKFLOW_ID = 903
PREFIX = '/actions/workflows/' + removal.WORKFLOW


def run(run_id, **changes):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=run_id)
    value = {'id': run_id, 'workflow_id': WORKFLOW_ID, 'event': 'schedule', 'status': 'completed',
             'conclusion': 'success', 'display_title': '仅检查并提醒', 'run_attempt': 1,
             'head_sha': 'a' * 40, 'created_at': created.strftime('%Y-%m-%dT%H:%M:%SZ'),
             'path': removal.WORKFLOW_PATH, 'head_branch': 'main',
             'repository': {'full_name': 'owner/gki'}, 'head_repository': {'full_name': 'owner/gki'}}
    value.update(changes)
    if value['status'] != 'completed' and 'conclusion' not in changes:
        value['conclusion'] = None
    return value


class FakeGitHub:
    repository = 'owner/gki'

    def __init__(self, runs, *, current=None):
        self.runs = copy.deepcopy(runs)
        self.current = copy.deepcopy(current if current is not None else run(99, status='in_progress'))
        self.metadata = {'id': WORKFLOW_ID, 'path': removal.WORKFLOW_PATH}
        self.before_delete = {}
        self.delete_results = {}
        self.calls = []

    def request(self, resource, *, method='GET', missing_ok=False):
        self.calls.append((method, resource, missing_ok))
        if resource == PREFIX and method == 'GET':
            return copy.deepcopy(self.metadata)
        match = re.fullmatch(re.escape(PREFIX) + r'/runs\?per_page=100&page=([1-9][0-9]*)', resource)
        if match and method == 'GET':
            start = (int(match[1]) - 1) * 100
            return {'total_count': len(self.runs), 'workflow_runs': copy.deepcopy(self.runs[start:start + 100])}
        match = re.fullmatch(r'/actions/runs/([1-9][0-9]*)', resource)
        if match:
            run_id = int(match[1])
            if method == 'GET':
                if run_id == self.current['id']:
                    return copy.deepcopy(self.current)
                return copy.deepcopy(self.before_delete.get(run_id, next(
                    (value for value in self.runs if value['id'] == run_id), None)))
            if method == 'DELETE':
                value = self.delete_results.get(run_id, 'deleted')
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError('Unexpected request: ' + method + ' ' + resource)

    @property
    def deleted(self):
        return [path for method, path, _ in self.calls if method == 'DELETE']


class PreviousCheckRemovalTests(unittest.TestCase):
    def remove(self, client, **kwargs):
        return removal.remove_runs(client, current_run_id=99, **kwargs)

    def test_default_preview_reads_only_fixed_workflow_and_preserves_current(self):
        client = FakeGitHub([run(1), run(2), run(99, status='in_progress')])
        report = self.remove(client)
        self.assertEqual([2, 1], report.candidate_ids)
        self.assertEqual([99], report.preserved_ids)
        self.assertEqual([], client.deleted)
        self.assertEqual('dry-run', report.as_dict()['mode'])
        self.assertTrue(report.finished)
        self.assertTrue(all(path.startswith(PREFIX) or path == '/actions/runs/99'
                            for _, path, _ in client.calls))

    def test_check_only_completed_rows_with_any_conclusion_can_be_deleted(self):
        values = [run(1), run(2, conclusion='failure'), run(3, conclusion='cancelled'),
                  run(4, conclusion='skipped', event='workflow_dispatch', display_title='检查上游并提醒')]
        client = FakeGitHub(values)
        report = self.remove(client, execute=True)
        self.assertEqual([4, 3, 2, 1], report.deleted_ids)
        self.assertEqual(['/actions/runs/4', '/actions/runs/3', '/actions/runs/2', '/actions/runs/1'], client.deleted)
        self.assertFalse(any('releases' in path or '/actions/runs?' in path or 'kernel-full-feature' in path
                             for _, path, _ in client.calls))

    def test_sync_legacy_unknown_and_active_records_are_preserved(self):
        values = [run(1), run(2, event='workflow_dispatch', display_title='同步官方更新'),
                  run(3, display_title='检查并同步官方更新'), run(4, display_title='自动同步官方更新'),
                  run(5, event='workflow_dispatch', display_title='检查上游更新'),
                  run(6, status='queued'), run(7, status='in_progress'),
                  run(8, event='push'), run(9, event='pull_request'), run(10, display_title='unknown')]
        client = FakeGitHub(values)
        report = self.remove(client, execute=True)
        self.assertEqual([1], report.deleted_ids)
        self.assertEqual(list(range(10, 1, -1)), report.preserved_ids)

    def test_rerunning_old_check_preserves_newer_ids_dates_and_same_second(self):
        current = run(50, run_attempt=3, status='in_progress')
        values = [run(1), run(2, created_at=current['created_at']),
                  run(3, created_at=run(70)['created_at']), run(60),
                  run(61, created_at=run(1)['created_at']), current]
        client = FakeGitHub(values, current=current)
        report = removal.remove_runs(client, execute=True, current_run_id='50')
        self.assertEqual([1], report.deleted_ids)
        self.assertEqual({2, 3, 50, 60, 61}, set(report.preserved_ids))
        self.assertEqual(current['created_at'], report.cutoff)
        self.assertNotIn('/actions/runs/50', client.deleted)

    def test_missing_current_or_wrong_current_workflow_never_deletes(self):
        for current_id in (None, '', '0', '-1', True, 'bad'):
            client = FakeGitHub([run(1)])
            with self.subTest(current_id=current_id), self.assertRaises(removal.CleanupError):
                removal.remove_runs(client, execute=True, current_run_id=current_id)
            self.assertEqual([], client.calls)
        for changes in ({'workflow_id': 12}, {'path': '.github/workflows/build.yml'},
                        {'event': 'workflow_dispatch', 'display_title': '同步官方更新'},
                        {'repository': {'full_name': 'other/repo'}}, {'head_sha': None}):
            client = FakeGitHub([run(1)], current=run(99, **changes))
            with self.subTest(changes=changes), self.assertRaises(removal.CleanupError): self.remove(client, execute=True)
            self.assertEqual([], client.deleted)

    def test_foreign_metadata_inventory_or_missing_required_identity_blocks_all_deletion(self):
        for metadata in ({'id': True}, {'id': WORKFLOW_ID, 'path': '.github/workflows/build.yml'},
                         {'id': WORKFLOW_ID}, None):
            client = FakeGitHub([run(1)]); client.metadata = metadata
            with self.subTest(metadata=metadata), self.assertRaises(removal.CleanupError): self.remove(client, execute=True)
            self.assertEqual([], client.deleted)
        for changes in ({'workflow_id': 12}, {'path': '.github/workflows/build.yml'},
                        {'repository': {'full_name': 'foreign/repo'}}, {'created_at': 'not a date'}):
            client = FakeGitHub([run(1), run(2, **changes)])
            with self.subTest(changes=changes), self.assertRaises(removal.CleanupError): self.remove(client, execute=True)
            self.assertEqual([], client.deleted)

    def test_legacy_missing_attempt_or_commit_is_ambiguous_and_preserved(self):
        for field in ('run_attempt', 'head_sha'):
            value = run(1); del value[field]
            client = FakeGitHub([value])
            with self.subTest(field=field):
                self.assertEqual([1], self.remove(client, execute=True).preserved_ids)
            self.assertEqual([], client.deleted)

    def test_complete_pagination_before_any_deletion_and_date_fence(self):
        current = run(200, status='in_progress')
        client = FakeGitHub([run(value) for value in range(1, 102)], current=current)
        report = removal.remove_runs(client, execute=True, current_run_id=200)
        self.assertEqual(list(range(101, 0, -1)), report.deleted_ids)
        second_page = ('GET', PREFIX + '/runs?per_page=100&page=2', False)
        self.assertIn(second_page, client.calls)
        first_delete = next(index for index, call in enumerate(client.calls) if call[0] == 'DELETE')
        self.assertLess(client.calls.index(second_page), first_delete)

    def test_bad_later_page_prevents_every_delete(self):
        for failure in ('missing', 'changed', 'duplicate', 'foreign'):
            client = FakeGitHub([run(value) for value in range(1, 102)], current=run(200, status='in_progress'))
            original = client.request
            def changed(resource, **kwargs):
                result = original(resource, **kwargs)
                if resource.endswith('page=2'):
                    if failure == 'missing': result['workflow_runs'] = []
                    elif failure == 'changed': result['total_count'] += 1
                    elif failure == 'duplicate': result['workflow_runs'][0] = run(1)
                    else: result['workflow_runs'][0]['workflow_id'] = 12
                return result
            with self.subTest(failure=failure), mock.patch.object(client, 'request', side_effect=changed), \
                    self.assertRaises(removal.CleanupError):
                removal.remove_runs(client, execute=True, current_run_id=200)
            self.assertEqual([], client.deleted)

    def test_identity_revalidation_blocks_rerun_active_or_sync_races(self):
        for changes in ({'id': 7}, {'run_attempt': 2}, {'head_sha': 'b' * 40},
                        {'event': 'workflow_dispatch'}, {'display_title': '同步官方更新'},
                        {'status': 'in_progress', 'conclusion': None}, {'workflow_id': 77},
                        {'head_branch': 'other'}, {'created_at': run(100)['created_at']},
                        {'conclusion': 'failure'}, {'repository': {'full_name': 'foreign/repo'}}):
            client = FakeGitHub([run(1), run(2)])
            client.before_delete[2] = run(2, **changes)
            with self.subTest(changes=changes), self.assertRaises(removal.CleanupError): self.remove(client, execute=True)
            self.assertEqual([], client.deleted)

    def test_missing_lookups_and_delete_404_are_idempotent(self):
        client = FakeGitHub([run(1), run(2), run(3)])
        client.before_delete[3] = None; client.delete_results[2] = 'gone'
        report = self.remove(client, execute=True)
        self.assertEqual([3, 2], report.gone_ids)
        self.assertEqual([1], report.deleted_ids)
        self.assertNotIn('/actions/runs/3', client.deleted)

    def test_failure_preserves_partial_report_and_stops_remaining_deletes(self):
        client = FakeGitHub([run(1), run(2), run(3)])
        client.delete_results[2] = removal.CleanupError('DELETE failed')
        report = removal.RemovalReport(client.repository, True)
        with self.assertRaises(removal.CleanupError): self.remove(client, execute=True, report=report)
        self.assertEqual([3], report.deleted_ids)
        self.assertFalse(report.finished)
        self.assertNotIn('/actions/runs/1', client.deleted)

    def test_cli_requires_current_context_and_defaults_to_preview(self):
        environment = {'GITHUB_REPOSITORY': 'owner/gki', 'GITHUB_RUN_ID': '99',
                       'GH_TOKEN': 'private-token', 'GITHUB_TOKEN': ''}
        for args, expected in (([], []), (['--execute'], [2, 1])):
            client = FakeGitHub([run(1), run(2)])
            out = io.StringIO()
            with mock.patch.dict(os.environ, environment), mock.patch.object(removal, 'GitHub', return_value=client), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(0, removal.main(args))
            document = json.loads(out.getvalue())
            self.assertEqual(expected, document['deleted_ids'])
            self.assertEqual(removal.WORKFLOW, document['workflow'])
            self.assertEqual('completed', document['status'])
            self.assertNotIn('private-token', out.getvalue())
        out, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**environment, 'GITHUB_RUN_ID': ''}), \
                mock.patch.object(removal, 'GitHub') as api, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(errors):
            self.assertEqual(1, removal.main(['--execute']))
        api.assert_not_called()
        self.assertEqual('failed', json.loads(out.getvalue())['status'])
        self.assertNotIn('private-token', out.getvalue() + errors.getvalue())


if __name__ == '__main__':
    unittest.main()
