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
from urllib.error import HTTPError, URLError
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import cleanup_workflow_runs as cleanup


WORKFLOW_ID = 701
WORKFLOW = 'kernel-full-feature-matrix.yml'
PREFIX = '/actions/workflows/' + WORKFLOW
ALL_PREFIX = '/actions'
EXCLUDED_WORKFLOW = 'cleanup-build-history.yml'
EXCLUDED_ID = 703
EXCLUDED_PATH = '/actions/workflows/' + EXCLUDED_WORKFLOW


def run(run_id, **changes):
    created = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=run_id)
    result = {'id': run_id, 'workflow_id': WORKFLOW_ID, 'event': 'workflow_dispatch',
              'status': 'completed', 'conclusion': 'success', 'display_title': '编译内核',
              'created_at': created.strftime('%Y-%m-%dT%H:%M:%SZ')}
    result.update(changes)
    if 'conclusion' not in changes and result['status'] != 'completed':
        result['conclusion'] = None
    return result


class FakeGitHub:
    repository = 'owner/gki'

    def __init__(self, runs):
        self.runs = copy.deepcopy(runs)
        self.calls = []
        self.before_delete = {}
        self.delete_results = {}

    def request(self, resource, *, method='GET', missing_ok=False):
        self.calls.append((method, resource, missing_ok))
        if resource == PREFIX and method == 'GET':
            return {'id': WORKFLOW_ID}
        if resource in (EXCLUDED_PATH, '/actions/workflows/' + str(EXCLUDED_ID)) and method == 'GET':
            return {'id': EXCLUDED_ID}
        match = re.fullmatch(r'(?:' + re.escape(PREFIX) + r'|' + re.escape(ALL_PREFIX)
                             + r')/runs\?per_page=100&page=([1-9][0-9]*)', resource)
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
        raise AssertionError('Unexpected API request: ' + method + ' ' + resource)

    @property
    def deleted(self):
        return [resource for method, resource, _ in self.calls if method == 'DELETE']


class PlannerTests(unittest.TestCase):
    def test_keeps_two_newest_successes_with_unsorted_input(self):
        plan = cleanup.plan_runs([run(value) for value in [3, 8, 1, 7, 4, 2, 6, 5]])
        self.assertEqual(plan.kept_ids, [8, 7])
        self.assertEqual(plan.candidate_ids, [6, 5, 4, 3, 2, 1])
        self.assertIsNone(plan.workflow_id)

    def test_sort_uses_creation_time_before_id_and_id_breaks_ties(self):
        runs = [run(99, created_at='2025-01-01T00:00:00Z'),
                run(1, created_at='2025-01-02T00:00:00Z'),
                run(2, created_at='2025-01-02T00:00:00Z')]
        plan = cleanup.plan_runs(runs, keep=1)
        self.assertEqual(plan.kept_ids, [2])
        self.assertEqual(plan.candidate_ids, [1, 99])

    def test_mixed_workflows_count_successes_only_and_current_does_not_consume_a_slot(self):
        runs = [run(1), run(2, workflow_id=702, event='schedule', display_title='自动同步官方更新'),
                run(3, workflow_id=703, display_title='清理历史编译（保留5次）'),
                run(4, workflow_id=704, event='push', display_title='编译 APK'),
                run(5, workflow_id=702, event='schedule', display_title='自动同步官方更新'),
                run(6, status='queued'), run(7, status='in_progress'),
                run(8, workflow_id=703, status='in_progress', display_title='清理历史记录（保留5条）')]
        plan = cleanup.plan_runs(runs, current_run_id='8')
        self.assertEqual(plan.kept_ids, [5, 4])
        self.assertEqual(plan.candidate_ids, [3, 2, 1])
        self.assertEqual(plan.preserved, {8: 'current run', 7: 'not completed', 6: 'not completed'})
        self.assertEqual(plan.excluded_workflow_ids, [])

    def test_only_old_current_or_unfinished_runs_are_temporarily_extra(self):
        runs = [run(value) for value in range(1, 11)]
        runs[0]['status'] = 'in_progress'
        runs[1]['status'] = 'queued'
        runs[2]['status'] = 'waiting'
        plan = cleanup.plan_runs(runs, keep=5, current_run_id='4')
        self.assertEqual(plan.kept_ids, [10, 9, 8, 7, 6])
        self.assertEqual(plan.candidate_ids, [5])
        self.assertEqual(plan.preserved, {4: 'current run', 3: 'not completed',
                                          2: 'not completed', 1: 'not completed'})

    def test_single_workflow_scope_still_requires_matching_workflow_ids(self):
        plan = cleanup.plan_runs([run(1)], workflow_id=WORKFLOW_ID)
        self.assertEqual(plan.workflow_id, WORKFLOW_ID)
        with self.assertRaises(cleanup.CleanupError):
            cleanup.plan_runs([run(1), run(2, workflow_id=99)], workflow_id=WORKFLOW_ID)

    def test_empty_and_fewer_than_two_successes_have_no_candidates(self):
        for runs in ([], [run(1)], [run(1), run(2)]):
            with self.subTest(count=len(runs)):
                self.assertEqual(cleanup.plan_runs(runs).candidate_ids, [])

    def test_newer_failed_cancelled_and_skipped_runs_do_not_replace_successes(self):
        runs = [run(1), run(3), run(5),
                run(6, conclusion='failure'), run(7, conclusion='cancelled'),
                run(8, conclusion='skipped'), run(9, conclusion='timed_out'),
                run(10, status='queued'), run(11, status='in_progress')]
        plan = cleanup.plan_runs(runs)
        self.assertEqual([5, 3], plan.kept_ids)
        self.assertEqual([9, 8, 7, 6, 1], plan.candidate_ids)
        self.assertEqual({11: 'not completed', 10: 'not completed'}, plan.preserved)
        self.assertEqual([], plan.excluded_workflow_ids)

    def test_no_successes_still_removes_every_completed_failure(self):
        plan = cleanup.plan_runs([run(1, conclusion='failure'), run(2, conclusion='neutral'),
                                  run(3, status='waiting')])
        self.assertEqual([], plan.kept_ids)
        self.assertEqual([2, 1], plan.candidate_ids)
        self.assertEqual({3: 'not completed'}, plan.preserved)

    def test_current_never_reduces_the_two_successful_slots(self):
        runs = [run(1), run(2), run(3, status='in_progress')]
        for current in (None, '999'):
            with self.subTest(current=current):
                plan = cleanup.plan_runs(runs, current_run_id=current)
                self.assertEqual([2, 1], plan.kept_ids)
                self.assertEqual([], plan.excluded_workflow_ids)
        plan = cleanup.plan_runs(runs, current_run_id='3')
        self.assertEqual([2, 1], plan.kept_ids)
        self.assertEqual({3: 'current run'}, plan.preserved)
        self.assertEqual([], plan.candidate_ids)
        # A completed current success is already included in the ordinary two.
        plan = cleanup.plan_runs([run(1), run(2), run(3)], current_run_id='3')
        self.assertEqual([3, 2], plan.kept_ids)
        self.assertEqual([], plan.excluded_workflow_ids)

    def test_keep_one_still_keeps_one_success_without_deleting_unfinished_runs(self):
        plan = cleanup.plan_runs([run(1), run(2), run(3, status='in_progress'),
                                  run(4, status='queued')], keep=1, current_run_id='3')
        self.assertEqual([2], plan.kept_ids)
        self.assertEqual([1], plan.candidate_ids)
        self.assertEqual({4: 'not completed', 3: 'current run'}, plan.preserved)

    def test_excluded_workflow_successes_are_deleted_without_using_a_success_slot(self):
        plan = cleanup.plan_runs([run(1), run(2), run(3),
                                  run(4, workflow_id=EXCLUDED_ID),
                                  run(5, workflow_id=EXCLUDED_ID, status='in_progress'),
                                  run(6, workflow_id=EXCLUDED_ID, status='queued')],
                                 excluded_workflow_ids={EXCLUDED_ID}, current_run_id='5')
        self.assertEqual([3, 2], plan.kept_ids)
        self.assertEqual([4, 1], plan.candidate_ids)
        self.assertEqual({6: 'not completed', 5: 'current run'}, plan.preserved)
        self.assertEqual([EXCLUDED_ID], plan.excluded_workflow_ids)

    def test_invalid_excluded_workflow_ids_are_refused(self):
        for excluded in (None, '703', [None], [0], [True], ['703']):
            with self.subTest(excluded=excluded), self.assertRaises(cleanup.CleanupError):
                cleanup.plan_runs([run(1)], excluded_workflow_ids=excluded)

    def test_invalid_or_missing_fields_are_refused(self):
        bad_values = {'id': [None, 0, True, '1'], 'workflow_id': [None, 0, True],
                      'event': [None, ''], 'status': [None, ''], 'display_title': [None],
                      'conclusion': [None, '', 'unknown', [], True],
                      'created_at': [None, 'yesterday', '2025-02-30T00:00:00Z']}
        for key, values in bad_values.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(cleanup.CleanupError):
                    cleanup.plan_runs([run(1, **{key: value})])
        for key in run(1):
            item = run(1)
            del item[key]
            with self.subTest(missing=key), self.assertRaises(cleanup.CleanupError):
                cleanup.plan_runs([item])

    def test_duplicate_ids_and_invalid_keep_are_refused(self):
        with self.assertRaises(cleanup.CleanupError):
            cleanup.plan_runs([run(1), run(1)])
        for keep in (0, -1, True, '5'):
            with self.subTest(keep=keep), self.assertRaises(cleanup.CleanupError):
                cleanup.plan_runs([run(1)], keep=keep)


class CleanupTests(unittest.TestCase):
    def test_default_preview_makes_no_delete_or_candidate_detail_requests(self):
        client = FakeGitHub([run(value) for value in range(1, 8)])
        report = cleanup.cleanup(client)
        self.assertEqual(report.plan.kept_ids, [7, 6])
        self.assertEqual(report.plan.candidate_ids, [5, 4, 3, 2, 1])
        self.assertEqual(client.calls, [('GET', ALL_PREFIX + '/runs?per_page=100&page=1', False)])
        self.assertEqual(report.as_dict()['mode'], 'dry-run')
        self.assertEqual(report.as_dict()['workflow'], 'all')

    def test_paginates_all_runs_before_deleting_only_candidate_paths(self):
        client = FakeGitHub([run(value) for value in range(1, 106)])
        report = cleanup.cleanup(client, keep=5, execute=True)
        self.assertEqual(report.plan.kept_ids, [105, 104, 103, 102, 101])
        self.assertEqual(report.deleted_ids, list(range(100, 0, -1)))
        self.assertEqual(client.calls[:2], [('GET', ALL_PREFIX + '/runs?per_page=100&page=1', False),
                                          ('GET', ALL_PREFIX + '/runs?per_page=100&page=2', False)])
        self.assertEqual(client.deleted, [f'/actions/runs/{value}' for value in range(100, 0, -1)])
        for index in range(2, len(client.calls), 2):
            method, resource, missing_ok = client.calls[index]
            self.assertEqual((method, missing_ok), ('GET', True))
            self.assertEqual(client.calls[index + 1], ('DELETE', resource, False))
        self.assertFalse(any(word in resource for _, resource, _ in client.calls
                             for word in ('releases', 'caches', 'branches', '/artifacts')))

    def test_full_last_page_is_followed_by_an_empty_page(self):
        client = FakeGitHub([run(value) for value in range(1, 101)])
        _, runs = cleanup.fetch_workflow_runs(client)
        self.assertEqual(len(runs), 100)
        self.assertEqual(client.calls[-1][1], ALL_PREFIX + '/runs?per_page=100&page=2')

    def test_single_workflow_option_validates_metadata_and_lists_only_that_workflow(self):
        client = FakeGitHub([run(value) for value in range(1, 8)])
        report = cleanup.cleanup(client, WORKFLOW)
        self.assertEqual(report.plan.workflow_id, WORKFLOW_ID)
        self.assertEqual(client.calls, [('GET', PREFIX, False),
                                       ('GET', PREFIX + '/runs?per_page=100&page=1', False)])
        client = FakeGitHub([])
        with patch.object(client, 'request', return_value={}), self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup(client, WORKFLOW, execute=True)

    def test_repository_scope_deletes_old_mixed_workflows_and_maintenance_runs(self):
        runs = [run(value, workflow_id=WORKFLOW_ID + value % 3,
                    event='schedule' if value % 2 else 'push',
                    display_title='自动同步官方更新' if value % 2 else '清理历史记录（保留5条）')
                for value in range(1, 9)]
        runs[-1]['status'] = 'in_progress'
        client = FakeGitHub(runs)
        report = cleanup.cleanup(client, execute=True, current_run_id='8')
        self.assertIsNone(report.plan.workflow_id)
        self.assertEqual(report.plan.kept_ids, [7, 6])
        self.assertEqual(report.deleted_ids, [5, 4, 3, 2, 1])
        self.assertEqual(report.plan.preserved, {8: 'current run'})

    def test_incomplete_second_page_or_request_failure_prevents_every_deletion(self):
        for failure in ('missing', 'count_changed', 'duplicate', 'invalid_workflow', 'request'):
            client = FakeGitHub([run(value) for value in range(1, 102)])
            original = client.request

            def request(resource, **kwargs):
                if resource.endswith('page=2'):
                    if failure == 'request':
                        raise cleanup.CleanupError('GitHub API GET failed (HTTP 500)')
                    document = original(resource, **kwargs)
                    if failure == 'missing':
                        document['workflow_runs'] = []
                    elif failure == 'count_changed':
                        document['total_count'] += 1
                    elif failure == 'duplicate':
                        document['workflow_runs'][0] = run(1)
                    elif failure == 'invalid_workflow':
                        document['workflow_runs'][0]['workflow_id'] = None
                    return document
                return original(resource, **kwargs)

            with self.subTest(failure=failure), patch.object(client, 'request', side_effect=request):
                with self.assertRaises(cleanup.CleanupError):
                    cleanup.cleanup(client, execute=True)
                self.assertEqual(client.deleted, [])

    def test_wrong_workflow_on_later_single_scope_page_prevents_every_deletion(self):
        client = FakeGitHub([run(value) for value in range(1, 102)])
        client.runs[-1]['workflow_id'] = 999
        with self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup(client, WORKFLOW, execute=True)
        self.assertEqual(client.deleted, [])

    def test_listing_metadata_and_fields_fail_closed(self):
        documents = [None, {}, {'total_count': True, 'workflow_runs': []},
                     {'total_count': 0, 'workflow_runs': [run(1)]},
                     {'total_count': 1, 'workflow_runs': [{'id': 1}]},
                     {'total_count': cleanup.MAX_RUNS + 1, 'workflow_runs': []}]
        for document in documents:
            client = FakeGitHub([])
            original = client.request

            def request(resource, **kwargs):
                return document if '/runs?' in resource else original(resource, **kwargs)

            with self.subTest(document=document), patch.object(client, 'request', side_effect=request):
                with self.assertRaises(cleanup.CleanupError):
                    cleanup.cleanup(client, execute=True)
                self.assertEqual(client.deleted, [])
        client = FakeGitHub([])
        with patch.object(client, 'request', return_value={}), self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup(client, execute=True)

    def test_candidate_revalidation_protects_changed_running_or_other_workflow_run(self):
        for change in ({'status': 'in_progress'}, {'workflow_id': 999}, {'id': 888},
                       {'event': 'schedule'}, {'display_title': '自动同步官方更新'},
                       {'display_title': '清理历史编译（保留5次）'},
                       {'created_at': '2025-02-01T00:00:00Z'}, {'status': None},
                       {'conclusion': 'failure'}, {'conclusion': None}):
            client = FakeGitHub([run(value) for value in range(1, 8)])
            client.before_delete[2] = run(2, **change)
            with self.subTest(change=change), self.assertRaises(cleanup.CleanupError):
                cleanup.cleanup(client, keep=5, execute=True)
            self.assertEqual(client.deleted, [])

    def test_repository_scope_revalidation_uses_each_candidates_original_workflow(self):
        client = FakeGitHub([run(value, workflow_id=WORKFLOW_ID + value % 2)
                             for value in range(1, 9)])
        client.before_delete[3] = run(3, workflow_id=WORKFLOW_ID)
        with self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup(client, keep=5, execute=True)
        self.assertEqual(client.deleted, [])

    def test_old_unfinished_run_is_skipped_without_reserving_five_other_builds(self):
        client = FakeGitHub([run(value, status='in_progress' if value == 2 else 'completed')
                             for value in range(1, 9)])
        report = cleanup.cleanup(client, keep=5, execute=True)
        self.assertEqual(report.plan.kept_ids, [8, 7, 6, 5, 4])
        self.assertEqual(report.plan.preserved, {2: 'not completed'})
        self.assertEqual(report.deleted_ids, [3, 1])
        self.assertNotIn('/actions/runs/2', [resource for _, resource, _ in client.calls])

    def test_delete_failure_stops_and_preserves_partial_results(self):
        client = FakeGitHub([run(value) for value in range(1, 9)])
        client.delete_results[2] = cleanup.CleanupError('GitHub API DELETE failed (HTTP 403)')
        report = cleanup.CleanupReport(client.repository, WORKFLOW, 5, True)
        with self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup(client, keep=5, execute=True, report=report)
        self.assertEqual(report.deleted_ids, [3])
        self.assertEqual(report.as_dict()['status'], 'failed')
        self.assertEqual(report.as_dict()['remaining_candidate_ids'], [2, 1])
        self.assertEqual(client.deleted, ['/actions/runs/3', '/actions/runs/2'])

    def test_missing_candidates_and_delete_404_are_gone_not_deleted(self):
        client = FakeGitHub([run(value) for value in range(1, 9)])
        client.before_delete[3] = None
        client.delete_results[2] = 'gone'
        report = cleanup.cleanup(client, keep=5, execute=True)
        self.assertEqual(report.deleted_ids, [1])
        self.assertEqual(report.gone_ids, [3, 2])
        self.assertEqual(report.as_dict()['remaining_candidate_ids'], [])
        self.assertNotIn('/actions/runs/3', client.deleted)

    def test_newer_failures_are_deleted_and_two_older_successes_remain(self):
        client = FakeGitHub([run(1), run(2), run(3), run(4, conclusion='failure'),
                             run(5, conclusion='cancelled'), run(6, status='queued')])
        report = cleanup.cleanup(client, execute=True)
        self.assertEqual([3, 2], report.plan.kept_ids)
        self.assertEqual([5, 4, 1], report.deleted_ids)
        self.assertEqual({6: 'not completed'}, report.plan.preserved)

    def test_exact_excluded_workflow_is_resolved_before_listing_and_never_counted(self):
        client = FakeGitHub([run(1), run(2),
                             run(3, workflow_id=EXCLUDED_ID, display_title='old cleanup name'),
                             run(4, display_title='清理历史记录（保留2条成功）'),
                             run(5, workflow_id=EXCLUDED_ID, status='in_progress')])
        report = cleanup.cleanup(client, execute=True, current_run_id='5',
                                 exclude_workflow=EXCLUDED_WORKFLOW)
        self.assertEqual(('GET', EXCLUDED_PATH, False), client.calls[0])
        self.assertEqual(('GET', ALL_PREFIX + '/runs?per_page=100&page=1', False), client.calls[1])
        self.assertEqual([4, 2], report.plan.kept_ids)
        self.assertEqual([3, 1], report.deleted_ids)
        self.assertEqual([EXCLUDED_ID], report.as_dict()['excluded_workflow_ids'])
        self.assertEqual({5: 'current run'}, report.plan.preserved)

    def test_exclusion_same_as_selected_scope_can_delete_all_completed_rows(self):
        client = FakeGitHub([run(1), run(2), run(3, status='in_progress')])
        report = cleanup.cleanup(client, WORKFLOW, exclude_workflow=WORKFLOW, execute=True,
                                 current_run_id='3')
        self.assertEqual([], report.plan.kept_ids)
        self.assertEqual([2, 1], report.deleted_ids)
        self.assertEqual({3: 'current run'}, report.plan.preserved)

    def test_excluded_workflow_metadata_failure_prevents_listing_and_every_deletion(self):
        for document in (None, {}, {'id': None}, {'id': 0}, {'id': True}):
            client = FakeGitHub([run(1)])
            original = client.request
            def request(resource, **kwargs):
                return document if resource == EXCLUDED_PATH else original(resource, **kwargs)
            with self.subTest(document=document), patch.object(client, 'request', side_effect=request):
                with self.assertRaises(cleanup.CleanupError):
                    cleanup.cleanup(client, exclude_workflow=EXCLUDED_WORKFLOW, execute=True)
                self.assertEqual([], client.calls)
                self.assertEqual([], client.deleted)

    def test_completed_failure_rerun_changed_to_success_or_new_attempt_is_preserved(self):
        for change in ({'conclusion': 'success'}, {'status': 'in_progress', 'conclusion': None},
                       {'run_attempt': 2}, {'head_sha': 'b' * 40}):
            client = FakeGitHub([run(1), run(2), run(3),
                                 run(4, conclusion='failure', run_attempt=1, head_sha='a' * 40)])
            original = next(item for item in client.runs if item['id'] == 4)
            client.before_delete[4] = {**original, **change}
            with self.subTest(change=change), self.assertRaises(cleanup.CleanupError):
                cleanup.cleanup(client, execute=True)
            self.assertEqual([], client.deleted)


class Response:
    def __init__(self, status=200, body=b'{}'):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        if self.status == 204:
            raise AssertionError('204 must not be parsed as JSON')
        return self.body[:size]


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TransportTests(unittest.TestCase):
    def test_repository_listing_is_allowed_only_as_a_fixed_read_only_paginated_path(self):
        opener = Opener(Response(body=b'{"total_count":0,"workflow_runs":[]}'))
        client = cleanup.GitHub('owner/gki', 'secret-token', opener)
        self.assertEqual(client.request('/actions/runs?per_page=100&page=1'),
                         {'total_count': 0, 'workflow_runs': []})
        self.assertEqual(opener.requests[0][0].full_url,
                         'https://api.github.com/repos/owner/gki/actions/runs?per_page=100&page=1')

    def test_transport_uses_fixed_https_host_headers_and_handles_empty_204(self):
        opener = Opener(Response(204))
        client = cleanup.GitHub('owner/gki', 'secret-token', opener)
        self.assertEqual(client.request('/actions/runs/1', method='DELETE'), 'deleted')
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, 'https://api.github.com/repos/owner/gki/actions/runs/1')
        self.assertEqual(request.get_method(), 'DELETE')
        self.assertEqual(request.get_header('Authorization'), 'Bearer secret-token')
        self.assertEqual(timeout, 45)

    def test_404_is_gone_and_all_other_errors_are_sanitized(self):
        for method in ('GET', 'DELETE'):
            opener = Opener(HTTPError('https://api.github.com', 404, 'secret-token', {}, None))
            client = cleanup.GitHub('owner/gki', 'secret-token', opener)
            self.assertEqual(client.request('/actions/runs/1', method=method, missing_ok=True),
                             None if method == 'GET' else 'gone')
        for error in (HTTPError('https://api.github.com', 403, 'secret-token', {}, None),
                      HTTPError('https://api.github.com', 302, 'secret-token', {}, None),
                      URLError('secret-token'), OSError('secret-token')):
            client = cleanup.GitHub('owner/gki', 'secret-token', Opener(error))
            with self.subTest(error=type(error).__name__), self.assertRaises(cleanup.CleanupError) as raised:
                client.request('/actions/runs/1')
            self.assertNotIn('secret-token', str(raised.exception))

    def test_redirects_are_refused_and_default_opener_installs_no_redirect_handler(self):
        with self.assertRaises(cleanup.CleanupError):
            cleanup.NoRedirect().redirect_request(None, None, None, None, None, None)
        with patch.object(cleanup, 'build_opener', return_value=Opener(Response())) as build:
            cleanup.GitHub('owner/gki', 'secret-token')
        self.assertIsInstance(build.call_args.args[0], cleanup.NoRedirect)

    def test_disallowed_paths_methods_repository_and_tokens_never_reach_network(self):
        opener = Opener(Response())
        client = cleanup.GitHub('owner/gki', 'secret-token', opener)
        for path, method in [('/releases/1', 'DELETE'), ('/actions/caches/1', 'DELETE'),
                             ('/branches/main', 'DELETE'), ('/actions/artifacts/1', 'DELETE'),
                             (PREFIX, 'DELETE'), ('/actions/runs/1', 'POST'),
                             ('/actions/runs?per_page=100&page=1', 'DELETE'),
                             ('/actions/runs?per_page=100&page=1&event=push', 'GET'),
                             ('/actions/runs?per_page=10&page=1', 'GET'),
                             ('/actions/runs?per_page=100&page=0', 'GET'),
                             ('https://evil.example/actions/runs/1', 'GET'),
                             ('/actions/runs/1?redirect=evil', 'GET')]:
            with self.subTest(path=path, method=method), self.assertRaises(cleanup.CleanupError):
                client.request(path, method=method)
        self.assertEqual(opener.requests, [])
        for repository in ('', '../owner/repo', 'owner/repo?x=1', 'owner/repo/extra', 'https://evil/repo'):
            with self.subTest(repository=repository), self.assertRaises(cleanup.CleanupError):
                cleanup.GitHub(repository, 'secret-token', opener)
        for token in ('', 'with space', 'with\nnewline', 'with\x7fdelete'):
            with self.subTest(token=token), self.assertRaises(cleanup.CleanupError):
                cleanup.GitHub('owner/gki', token, opener)

    def test_json_errors_size_limit_and_unexpected_success_status_fail_closed(self):
        for response in (Response(body=b''), Response(body=b'{"id":1,"id":2}'),
                         Response(body=b'{"id":NaN}'), Response(body=b'\xff'),
                         Response(201), Response(body=b'x' * (cleanup.MAX_RESPONSE + 1))):
            client = cleanup.GitHub('owner/gki', 'secret-token', Opener(response))
            with self.subTest(status=response.status, size=len(response.body)), self.assertRaises(cleanup.CleanupError):
                client.request('/actions/runs/1')
        client = cleanup.GitHub('owner/gki', 'secret-token', Opener(Response(200)))
        with self.assertRaises(cleanup.CleanupError):
            client.request('/actions/runs/1', method='DELETE')


class CLITests(unittest.TestCase):
    def setUp(self):
        # A single directory under tests inherits workspace ACLs on Windows.
        self.directory = Path(__file__).parent / ('cleanup-runs-test-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(self.remove_directory)
        self.environment = patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/gki',
                                                  'GITHUB_TOKEN': 'secret-token'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def remove_directory(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def invoke(self, arguments, client):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cleanup, 'GitHub', return_value=client) as factory, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cleanup.main(arguments)
        self.assertNotIn('secret-token', stdout.getvalue() + stderr.getvalue())
        return status, json.loads(stdout.getvalue()), stderr.getvalue(), factory

    def test_cli_defaults_to_preview_and_reads_environment_summary(self):
        summary = self.directory / 'summary.md'
        os.environ['GITHUB_STEP_SUMMARY'] = str(summary)
        client = FakeGitHub([run(value) for value in range(1, 8)])
        status, data, _, factory = self.invoke([], client)
        self.assertEqual(status, 0)
        self.assertEqual(data['mode'], 'dry-run')
        self.assertEqual(data['candidate_ids'], [5, 4, 3, 2, 1])
        self.assertEqual(client.deleted, [])
        factory.assert_called_once_with('owner/gki', 'secret-token')
        self.assertIn('模式：仅预览', summary.read_text(encoding='utf-8'))
        self.assertEqual(data['workflow'], 'all')
        self.assertIn('范围：仓库全部工作流', summary.read_text(encoding='utf-8'))
        self.assertEqual(data['keep'], 2)
        self.assertIn('保留成功记录：2 条', summary.read_text(encoding='utf-8'))

    def test_explicit_execute_records_partial_failure_and_summary(self):
        summary = self.directory / 'summary.md'
        client = FakeGitHub([run(value) for value in range(1, 9)])
        client.delete_results[2] = cleanup.CleanupError('GitHub API DELETE failed (HTTP 403)')
        status, data, error, _ = self.invoke(['--repo', 'owner/gki', '--workflow', WORKFLOW,
                                            '--keep', '5', '--execute', '--summary', str(summary)], client)
        self.assertEqual(status, 1)
        self.assertEqual(data['status'], 'failed')
        self.assertEqual(data['deleted_ids'], [3])
        self.assertEqual(data['remaining_candidate_ids'], [2, 1])
        self.assertIn('HTTP 403', error)
        text = summary.read_text(encoding='utf-8')
        self.assertIn('状态：失败', text)
        self.assertIn('本次已删除：1 条', text)
        self.assertIn('剩余候选：2 条', text)
        self.assertNotIn('secret-token', text)

    def test_gh_token_precedence_current_run_and_keep_option(self):
        os.environ.update({'GH_TOKEN': 'preferred-token', 'GITHUB_RUN_ID': '8'})
        client = FakeGitHub([run(value) for value in range(1, 9)])
        status, data, _, factory = self.invoke(['--keep', '2'], client)
        self.assertEqual(status, 0)
        self.assertEqual(data['kept_ids'], [8, 7])
        self.assertEqual(data['preserved'], {})
        factory.assert_called_once_with('owner/gki', 'preferred-token')

    def test_summary_identifies_temporary_extra_protected_runs(self):
        summary = self.directory / 'summary.md'
        os.environ['GITHUB_RUN_ID'] = '2'
        client = FakeGitHub([run(value, status='queued' if value == 1 else 'completed')
                             for value in range(1, 9)])
        status, data, _, _ = self.invoke(['--summary', str(summary)], client)
        self.assertEqual(status, 0)
        self.assertEqual(data['kept_ids'], [8, 7])
        self.assertEqual(data['preserved'], {'2': 'current run', '1': 'not completed'})
        text = summary.read_text(encoding='utf-8')
        self.assertIn('保留成功记录：2 条', text)
        self.assertIn('暂时保护：2 条', text)
        self.assertIn('暂时保护运行 ID：2, 1', text)

    def test_current_cleanup_is_excluded_and_protected_without_using_success_slot(self):
        summary = self.directory / 'summary.md'
        os.environ['GITHUB_RUN_ID'] = '4'
        client = FakeGitHub([run(1), run(2), run(3, workflow_id=EXCLUDED_ID),
                             run(4, workflow_id=EXCLUDED_ID, status='in_progress')])
        status, data, _, _ = self.invoke(['--exclude-workflow', EXCLUDED_WORKFLOW,
                                         '--summary', str(summary)], client)
        self.assertEqual(0, status)
        self.assertEqual([2, 1], data['kept_ids'])
        self.assertEqual([EXCLUDED_ID], data['excluded_workflow_ids'])
        self.assertEqual({'4': 'current run'}, data['preserved'])
        self.assertEqual([3], data['candidate_ids'])
        self.assertIn('不计入保留名额的工作流 ID：703',
                      summary.read_text(encoding='utf-8'))

    def test_bad_arguments_fail_before_any_network_request(self):
        for arguments in (['--keep', '0'], ['--repo', 'https://evil/repo'], ['--workflow', '../other.yml'],
                          ['--exclude-workflow', 'all'], ['--exclude-workflow', '../other.yml']):
            client = FakeGitHub([])
            with self.subTest(arguments=arguments):
                status, data, _, factory = self.invoke(arguments, client)
                self.assertEqual(status, 1)
                self.assertEqual(data['status'], 'failed')
                self.assertEqual(client.calls, [])
                factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
