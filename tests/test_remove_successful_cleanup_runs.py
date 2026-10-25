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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import remove_successful_cleanup_runs as removal


WORKFLOW_ID = 703
PREFIX = '/actions/workflows/cleanup-build-history.yml'


def run(run_id, **changes):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=run_id)
    result = {'id': run_id, 'workflow_id': WORKFLOW_ID, 'status': 'completed',
              'conclusion': 'success', 'event': 'workflow_dispatch',
              'display_title': '清理历史记录', 'run_attempt': 1, 'head_sha': 'a' * 40,
              'created_at': created.strftime('%Y-%m-%dT%H:%M:%SZ')}
    result.update(changes)
    if result['status'] != 'completed' and 'conclusion' not in changes:
        result['conclusion'] = None
    return result


class FakeGitHub:
    repository = 'owner/gki'

    def __init__(self, runs):
        self.runs = copy.deepcopy(runs)
        self.calls = []
        self.before_delete = {}
        self.delete_results = {}
        self.metadata = {'id': WORKFLOW_ID}

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
                return copy.deepcopy(self.before_delete.get(run_id, next(
                    (item for item in self.runs if item['id'] == run_id), None)))
            if method == 'DELETE':
                result = self.delete_results.get(run_id, 'deleted')
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError('Unexpected API path: ' + method + ' ' + resource)

    @property
    def deleted(self):
        return [resource for method, resource, _ in self.calls if method == 'DELETE']


class SuccessfulCleanupRemovalTests(unittest.TestCase):
    def test_default_preview_reads_only_the_fixed_workflow(self):
        client = FakeGitHub([run(1), run(2)])
        report = removal.remove_runs(client)
        self.assertEqual([2, 1], report.candidate_ids)
        self.assertEqual('dry-run', report.as_dict()['mode'])
        self.assertEqual([('GET', PREFIX, False),
                          ('GET', PREFIX + '/runs?per_page=100&page=1', False)], client.calls)
        self.assertTrue(report.finished)

    def test_only_successful_cleanup_rows_are_deleted(self):
        client = FakeGitHub([run(1), run(2, conclusion='failure'), run(3, conclusion='cancelled'),
                             run(4, status='in_progress'), run(5, status='queued'),
                             run(6, conclusion='skipped'), run(7)])
        report = removal.remove_runs(client, execute=True)
        self.assertEqual([7, 1], report.deleted_ids)
        self.assertEqual([6, 5, 4, 3, 2], report.preserved_ids)
        self.assertEqual(['/actions/runs/7', '/actions/runs/1'], client.deleted)
        self.assertFalse(any('/actions/runs?' in resource or 'releases' in resource
                             or 'kernel-full-feature' in resource for _, resource, _ in client.calls))

    def test_current_run_is_never_removed_even_if_already_successful(self):
        client = FakeGitHub([run(1), run(2)])
        report = removal.remove_runs(client, execute=True, current_run_id='2')
        self.assertEqual([2], report.preserved_ids)
        self.assertEqual([1], report.deleted_ids)
        self.assertFalse(any(resource == '/actions/runs/2' for _, resource, _ in client.calls))

    def test_foreign_workflow_or_wrong_metadata_id_prevents_every_delete(self):
        for metadata in ({'id': 999}, {'id': True}, {}, None):
            client = FakeGitHub([run(1)])
            client.metadata = metadata
            with self.subTest(metadata=metadata), self.assertRaises(removal.CleanupError):
                removal.remove_runs(client, execute=True)
            self.assertEqual([], client.deleted)
        client = FakeGitHub([run(1, workflow_id=701)])
        with self.assertRaises(removal.CleanupError):
            removal.remove_runs(client, execute=True)
        self.assertEqual([], client.deleted)

    def test_every_page_is_validated_before_deletion(self):
        client = FakeGitHub([run(value) for value in range(1, 102)])
        report = removal.remove_runs(client, execute=True)
        self.assertEqual(list(range(101, 0, -1)), report.deleted_ids)
        self.assertEqual([('GET', PREFIX, False),
                          ('GET', PREFIX + '/runs?per_page=100&page=1', False),
                          ('GET', PREFIX + '/runs?per_page=100&page=2', False)], client.calls[:3])

    def test_incomplete_changed_duplicate_or_foreign_later_page_blocks_all_deletion(self):
        for failure in ('missing', 'changed', 'duplicate', 'foreign'):
            client = FakeGitHub([run(value) for value in range(1, 102)])
            request = client.request
            def changed_page(resource, **kwargs):
                document = request(resource, **kwargs)
                if resource.endswith('page=2'):
                    if failure == 'missing':
                        document['workflow_runs'] = []
                    elif failure == 'changed':
                        document['total_count'] += 1
                    elif failure == 'duplicate':
                        document['workflow_runs'][0] = run(1)
                    else:
                        document['workflow_runs'][0]['workflow_id'] = 701
                return document
            with self.subTest(failure=failure), patch.object(client, 'request', side_effect=changed_page):
                with self.assertRaises(removal.CleanupError):
                    removal.remove_runs(client, execute=True)
            self.assertEqual([], client.deleted)

    def test_revalidation_rejects_reruns_changes_and_foreign_builds(self):
        changes = ({'id': 88}, {'workflow_id': 701}, {'status': 'in_progress', 'conclusion': None},
                   {'conclusion': 'failure'}, {'created_at': '2026-02-01T00:00:00Z'},
                   {'event': 'push'}, {'display_title': '编译内核'}, {'run_attempt': 2},
                   {'head_sha': 'b' * 40})
        for change in changes:
            client = FakeGitHub([run(1), run(2)])
            client.before_delete[2] = run(2, **change)
            with self.subTest(change=change), self.assertRaises(removal.CleanupError):
                removal.remove_runs(client, execute=True)
            self.assertEqual([], client.deleted)

    def test_optional_identity_fields_must_remain_equal_when_present(self):
        for key in ('head_sha', 'run_attempt'):
            client = FakeGitHub([run(1)])
            del client.runs[0][key]
            client.before_delete[1] = run(1)
            with self.subTest(added=key), self.assertRaises(removal.CleanupError):
                removal.remove_runs(client, execute=True)
            self.assertEqual([], client.deleted)
        row = run(1)
        del row['head_sha'], row['run_attempt']
        client = FakeGitHub([row])
        self.assertEqual([1], removal.remove_runs(client, execute=True).deleted_ids)

    def test_already_gone_lookups_and_delete_404_are_idempotent(self):
        client = FakeGitHub([run(1), run(2), run(3)])
        client.before_delete[3] = None
        client.delete_results[2] = 'gone'
        report = removal.remove_runs(client, execute=True)
        self.assertEqual([3, 2], report.gone_ids)
        self.assertEqual([1], report.deleted_ids)
        self.assertNotIn('/actions/runs/3', client.deleted)

    def test_delete_failure_stops_and_preserves_partial_report(self):
        client = FakeGitHub([run(1), run(2), run(3)])
        client.delete_results[2] = removal.CleanupError('GitHub API DELETE failed (HTTP 403)')
        report = removal.RemovalReport(client.repository, True)
        with self.assertRaises(removal.CleanupError):
            removal.remove_runs(client, execute=True, report=report)
        self.assertEqual([3], report.deleted_ids)
        self.assertFalse(report.finished)
        self.assertEqual(['/actions/runs/3', '/actions/runs/2'], client.deleted)

    def test_invalid_current_id_is_rejected_before_api_requests(self):
        for current in ('0', '-1', 'bad', True):
            client = FakeGitHub([run(1)])
            with self.subTest(current=current), self.assertRaises(removal.CleanupError):
                removal.remove_runs(client, execute=True, current_run_id=current)
            self.assertEqual([], client.calls)

    def test_cli_uses_environment_and_is_preview_until_execute_is_supplied(self):
        environment = {'GITHUB_REPOSITORY': 'owner/gki', 'GITHUB_RUN_ID': '99',
                       'GH_TOKEN': 'fixture-private-token', 'GITHUB_TOKEN': ''}
        for arguments, expected in (([], []), (['--execute'], [2, 1])):
            client = FakeGitHub([run(1), run(2)])
            output = io.StringIO()
            with self.subTest(arguments=arguments), patch.dict(os.environ, environment), \
                    patch.object(removal, 'GitHub', return_value=client) as constructor, \
                    contextlib.redirect_stdout(output):
                self.assertEqual(0, removal.main(arguments))
            constructor.assert_called_once_with('owner/gki', 'fixture-private-token')
            document = json.loads(output.getvalue())
            self.assertEqual(expected, document['deleted_ids'])
            self.assertEqual('completed', document['status'])
            self.assertEqual(removal.WORKFLOW, document['workflow'])
            self.assertNotIn('fixture-private-token', output.getvalue())

    def test_cli_failure_is_reported_without_secret_or_any_delete(self):
        client = FakeGitHub([run(1, workflow_id=701)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/gki', 'GITHUB_RUN_ID': '',
                                     'GH_TOKEN': 'fixture-private-token'}), \
                patch.object(removal, 'GitHub', return_value=client), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(1, removal.main(['--execute']))
        self.assertEqual('failed', json.loads(stdout.getvalue())['status'])
        self.assertEqual([], client.deleted)
        self.assertNotIn('fixture-private-token', stdout.getvalue() + stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
