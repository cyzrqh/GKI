"""Atomic release numbering and explicit run identity, using injected APIs."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import release_tags as tags

CONTEXT = {'repository': 'cyzrqh/GKI', 'run_id': 123, 'run_attempt': 1, 'head_sha': 'a' * 40}


def release(tag='r1.0', context=None, *, draft=False, id=1):
    identity = CONTEXT if context is None else context
    return {'id': id, 'tag_name': tag, 'draft': draft, 'target_commitish': identity['head_sha'],
            'body': tags.append_to_notes('Validated build notes\n', identity)}


def tag_ref(tag, sha='b' * 40, kind='commit'):
    return {'ref': 'refs/tags/' + tag, 'object': {'sha': sha, 'type': kind}}


class FakeAPI:
    def __init__(self, releases=(), refs=(), collision=False):
        self.releases, self.refs = list(releases), list(refs)
        self.collision, self.calls, self.posts = collision, [], []

    def request(self, resource, *, method='GET', data=None):
        self.calls.append((resource, method, data))
        if resource == '/releases?per_page=20&page=1':
            return list(self.releases)
        if resource == '/git/matching-refs/tags/r1.':
            return list(self.refs)
        if resource == '/git/refs' and method == 'POST':
            self.posts.append(dict(data))
            current = tag_ref(data['ref'].removeprefix('refs/tags/'), data['sha'])
            if self.collision:
                self.refs.append({**current, 'object': {'sha': 'f' * 40, 'type': 'commit'}})
                if self.collision != 'always':
                    self.collision = False
                raise tags.TagCollision('concurrent allocation')
            self.refs.append(current)
            return current
        raise AssertionError('Unexpected API request')


class ReleaseIdentityTests(unittest.TestCase):
    def test_exact_version_marker_and_legacy_tag_run_identity(self):
        self.assertEqual(CONTEXT, tags.release_run_identity(release()))
        old = {'tag_name': 'gki-123-1', 'target_commitish': 'a' * 40}
        self.assertEqual({**CONTEXT, 'repository': None}, tags.release_run_identity(old))
        for target in (None, 'main'):
            old['target_commitish'] = target
            self.assertEqual({'run_id': 123, 'run_attempt': 1, 'head_sha': None, 'repository': None},
                             tags.release_run_identity(old))
        self.assertEqual(CONTEXT, tags.release_run_identity(release('gki-123-1')))

    def test_body_marker_is_one_exact_standalone_line_and_matches_tag_and_target(self):
        valid = release()
        marker = valid['body'].splitlines()[-1]
        malformed = ['', marker + '\n' + marker, 'prefix ' + marker, marker + ' suffix',
                     marker.replace('v1', 'v2'), marker.replace('run_id=123', 'run_id=0'),
                     marker.replace('run_attempt=1', 'run_attempt=01'), marker.replace('a' * 40, 'a' * 39),
                     marker.replace('repository=cyzrqh/GKI', 'repository=../wrong')]
        for body in malformed:
            with self.subTest(body=body), self.assertRaises(ValueError):
                tags.release_run_identity({**valid, 'body': body})
        for changed in ({'target_commitish': 'b' * 40}, {'target_commitish': 'main'},
                        {'tag_name': 'gki-124-1'}, {'tag_name': 'gki-123-2'},
                        {'tag_name': 'unrelated'}, {'tag_name': 'r1.01'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                tags.release_run_identity({**valid, **changed})

    def test_append_is_idempotent_and_conflicting_notes_are_preserved(self):
        notes = tags.append_to_notes('# Build\n\n', CONTEXT)
        self.assertEqual(1, notes.count('gki-release-run:'))
        self.assertEqual(notes, tags.append_to_notes(notes, CONTEXT))
        for field, value in (('run_id', 124), ('run_attempt', 2), ('head_sha', 'f' * 40),
                             ('repository', 'other/repository')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                tags.append_to_notes(notes, {**CONTEXT, field: value})

    def test_invalid_context_is_always_a_value_error(self):
        for changes in ({'repository': None}, {'repository': 1}, {'run_id': True},
                        {'run_attempt': '1'}, {'head_sha': 'main'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                tags.append_to_notes('', {**CONTEXT, **changes})


class TagAllocationTests(unittest.TestCase):
    def test_first_tag_and_numeric_max_increment_ignore_unrelated_refs_and_backups(self):
        api = FakeAPI()
        self.assertEqual({'release_tag': 'r1.0', 'release_exists': False}, tags.allocate(api, CONTEXT))
        other = {**CONTEXT, 'run_id': 99}
        backup = {'id': 2, 'tag_name': 'gki-source-backup-123-1-SukiSU', 'draft': True,
                  'body': 'not a build marker', 'target_commitish': CONTEXT['head_sha']}
        api = FakeAPI([release('r1.8', other), backup], [tag_ref('r1.9'), tag_ref('r1.10'),
                      tag_ref('r1.11-backup'), tag_ref('r2.99'), tag_ref('r1.12', kind='tag')])
        self.assertEqual({'release_tag': 'r1.13', 'release_exists': False}, tags.allocate(api, CONTEXT))
        self.assertEqual([{'ref': 'refs/tags/r1.13', 'sha': 'a' * 40}], api.posts)
        self.assertEqual(2, len(api.releases))
        self.assertTrue(all(method in ('GET', 'POST') for _, method, _ in api.calls))

    def test_parallel_collision_rereads_complete_state_and_reserves_next_number(self):
        api = FakeAPI(refs=[tag_ref('r1.4')], collision=True)
        self.assertEqual({'release_tag': 'r1.6', 'release_exists': False}, tags.allocate(api, CONTEXT))
        self.assertEqual(['refs/tags/r1.5', 'refs/tags/r1.6'], [item['ref'] for item in api.posts])
        self.assertEqual(2, sum(resource.startswith('/releases?') for resource, _, _ in api.calls))
        self.assertEqual(2, sum(resource == '/git/matching-refs/tags/r1.' for resource, _, _ in api.calls))

    def test_collision_is_bounded_to_five_atomic_attempts(self):
        api = FakeAPI(collision='always')
        with self.assertRaisesRegex(ValueError, 'five concurrent'):
            tags.allocate(api, CONTEXT)
        self.assertEqual(5, len(api.posts))

    def test_same_exact_draft_reuses_its_tag_without_any_write(self):
        api = FakeAPI([release('r1.4', draft=True)], [tag_ref('r1.4', CONTEXT['head_sha'])])
        self.assertEqual({'release_tag': 'r1.4', 'release_exists': True}, tags.allocate(api, CONTEXT))
        self.assertEqual([], api.posts)

    def test_published_same_run_is_never_reused_and_conflicting_draft_fails_closed(self):
        fixtures = [FakeAPI([release('r1.4')], [tag_ref('r1.4', CONTEXT['head_sha'])]),
                    FakeAPI([release('r1.4', draft=True)], [tag_ref('r1.4', 'f' * 40)]),
                    FakeAPI([release('r1.4', {**CONTEXT, 'head_sha': 'f' * 40}, draft=True)],
                            [tag_ref('r1.4', 'f' * 40)]),
                    FakeAPI([release('r1.4', {**CONTEXT, 'repository': 'other/gki'}, draft=True)],
                            [tag_ref('r1.4', CONTEXT['head_sha'])]),
                    FakeAPI([release('r1.4', draft=True), release('r1.5', draft=True, id=2)],
                            [tag_ref('r1.4', CONTEXT['head_sha']), tag_ref('r1.5', CONTEXT['head_sha'])])]
        for api in fixtures:
            with self.subTest(releases=api.releases), self.assertRaises(ValueError):
                tags.allocate(api, CONTEXT)
            self.assertEqual([], api.posts)

    def test_same_commit_other_run_and_other_attempt_do_not_claim_existing_draft(self):
        for changes in ({'run_id': 122}, {'run_attempt': 2}):
            api = FakeAPI([release('r1.1', {**CONTEXT, **changes}, draft=True)],
                          [tag_ref('r1.1', CONTEXT['head_sha'])])
            self.assertEqual({'release_tag': 'r1.2', 'release_exists': False}, tags.allocate(api, CONTEXT))

    def test_malformed_existing_build_marker_or_api_shape_stops_before_atomic_write(self):
        malformed = release()
        malformed['body'] = 'missing run marker'
        for api in (FakeAPI([malformed]), FakeAPI(refs=[tag_ref('r1.0', 'bad')]),
                    FakeAPI([release(), release(id=1)]),
                    FakeAPI(refs=[tag_ref('r1.0'), tag_ref('r1.0')])):
            with self.subTest(api=api), self.assertRaises(ValueError):
                tags.allocate(api, CONTEXT)
            self.assertEqual([], api.posts)


class ReleaseTagAPITests(unittest.TestCase):
    def response(self, data, status=200):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.read.return_value = json.dumps(data).encode()
        return response

    def test_only_bounded_metadata_and_atomic_tag_api_operations_are_allowed(self):
        opener = mock.Mock()
        opener.open.return_value = self.response([])
        client = tags.GitHub('cyzrqh/GKI', 'secret', opener)
        self.assertEqual([], client.request('/git/matching-refs/tags/r1.'))
        request = opener.open.call_args.args[0]
        self.assertEqual('https://api.github.com/repos/cyzrqh/GKI/git/matching-refs/tags/r1.', request.full_url)
        self.assertNotIn('secret', request.full_url)
        self.assertEqual('Bearer secret', request.headers['Authorization'])
        for resource, method, data in (('/releases/123', 'DELETE', None),
                ('/git/refs/tags/r1.0', 'PATCH', {'sha': 'a' * 40}),
                ('/git/refs', 'POST', {'ref': 'refs/tags/source-backup', 'sha': 'a' * 40})):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                client.request(resource, method=method, data=data)

    def test_collision_status_is_distinct_without_exposing_response_body_or_token(self):
        opener = mock.Mock()
        opener.open.side_effect = HTTPError('https://api.github.com/invalid', 422, 'secret API body', {}, io.BytesIO(b'secret'))
        client = tags.GitHub('cyzrqh/GKI', 'secret', opener)
        with self.assertRaises(tags.TagCollision) as error:
            client.request('/git/refs', method='POST', data={'ref': 'refs/tags/r1.0', 'sha': 'a' * 40})
        self.assertNotIn('secret', str(error.exception))

    def test_invalid_utf8_duplicate_json_and_oversized_responses_are_rejected(self):
        for raw in (b'\xff', b'{"a":1,"a":2}', b'[NaN]', b'x' * (tags.MAX_RESPONSE + 1)):
            opener = mock.Mock()
            response = self.response([])
            response.read.return_value = raw
            opener.open.return_value = response
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                tags.GitHub('cyzrqh/GKI', 'secret', opener).request('/git/matching-refs/tags/r1.')

    def test_release_listing_uses_small_pages_and_validates_every_page(self):
        records = [release('r1.' + str(index), {**CONTEXT, 'run_id': 1000 + index}, id=index + 1)
                   for index in range(21)]
        api = mock.Mock()
        api.request.side_effect = [records[:20], records[20:]]
        self.assertEqual(records, tags.list_releases(api))
        self.assertEqual([mock.call('/releases?per_page=20&page=1'),
                          mock.call('/releases?per_page=20&page=2')], api.request.call_args_list)
        with self.assertRaises(ValueError):
            tags.GitHub('cyzrqh/GKI', 'secret', mock.Mock()).request('/releases?per_page=100&page=1')

    def test_transient_get_and_transport_retry_are_bounded_and_status_is_retained(self):
        for failure in (HTTPError('https://api.github.com/x', 502, 'secret-body', {}, io.BytesIO()),
                        OSError('secret transport detail')):
            opener = mock.Mock()
            opener.open.side_effect = [failure, self.response([])]
            with self.subTest(failure=type(failure)), mock.patch.object(tags.time, 'sleep') as sleep, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual([], tags.GitHub('cyzrqh/GKI', 'secret', opener).request('/git/matching-refs/tags/r1.'))
            self.assertEqual(2, opener.open.call_count)
            sleep.assert_called_once_with(2)
            self.assertNotIn('secret', output.getvalue())
        opener = mock.Mock()
        opener.open.side_effect = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPError('https://api.github.com/x', 503, 'secret', {}, io.BytesIO()))
        with mock.patch.object(tags.time, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(tags.ReleaseAPIError, 'HTTP 503.*three attempts') as error:
            tags.GitHub('cyzrqh/GKI', 'secret', opener).request('/git/matching-refs/tags/r1.')
        self.assertEqual(3, opener.open.call_count)
        self.assertEqual([mock.call(2), mock.call(5)], sleep.call_args_list)
        self.assertNotIn('secret', str(error.exception))

    def test_post_retry_collision_returns_to_allocator_without_overwriting_reserved_tag(self):
        opener = mock.Mock()
        opener.open.side_effect = [HTTPError('https://api.github.com/x', 502, 'body', {}, io.BytesIO()),
                                  HTTPError('https://api.github.com/x', 422, 'body', {}, io.BytesIO())]
        with mock.patch.object(tags.time, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(tags.TagCollision):
            tags.GitHub('cyzrqh/GKI', 'secret', opener).request('/git/refs', method='POST',
                data={'ref': 'refs/tags/r1.0', 'sha': CONTEXT['head_sha']})
        self.assertEqual(2, opener.open.call_count)
        sleep.assert_called_once_with(2)

    def test_cli_appends_marker_and_emits_workflow_outputs_without_publishing(self):
        root = ROOT / 'tests' / ('.release-tag-' + uuid.uuid4().hex)
        root.mkdir()
        self.addCleanup(shutil.rmtree, root)
        notes, output = root / 'release.md', root / 'output'
        notes.write_text('Verified release notes\n')
        environment = {'GITHUB_REPOSITORY': CONTEXT['repository'], 'GITHUB_RUN_ID': '123',
                       'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40, 'GH_TOKEN': 'secret',
                       'GITHUB_OUTPUT': str(output)}
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(tags, 'GitHub', return_value=FakeAPI()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, tags.main(['allocate', '--notes', str(notes)]))
        self.assertEqual(CONTEXT, tags.notes_marker(notes.read_text()))
        self.assertEqual('release_tag=r1.0\nrelease_exists=false\n', output.read_text())
        original = notes.read_bytes()
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(tags, 'GitHub', side_effect=ValueError('secret response body')), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(1, tags.main(['allocate', '--notes', str(notes)]))
        self.assertNotIn('secret', error.getvalue())
        self.assertEqual(original, notes.read_bytes())


if __name__ == '__main__':
    unittest.main()
