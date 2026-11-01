import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import notify_official_sync as notices
from upstream_pr_guard import BRANCH, OWNERSHIP_MARKER

REPO = 'owner/gki'
DIGEST = 'a' * 64
COMMIT = 'b' * 40


def report(**changes):
    return {'schema_version': 1, 'digest': DIGEST, 'has_changes': True,
            'checks': {'official': 'success', 'catalog': 'success'},
            'summary_markdown': '## 本次具体变化\n\nSukiSU v4.2.1-r1 → v4.2.1-r2；新增 GKI 6.6.89。', **changes}


def pull(*, merged=False):
    return {'number': 12, 'body': OWNERSHIP_MARKER, 'draft': False, 'merged': merged,
            'state': 'closed' if merged else 'open', 'merge_commit_sha': COMMIT if merged else None,
            'head': {'ref': BRANCH, 'repo': {'full_name': REPO}},
            'base': {'ref': 'main', 'repo': {'full_name': REPO}}}


def comment(body='unrelated comment', *, login=notices.BOT, kind='Bot'):
    return {'id': 9, 'body': body, 'user': {'type': kind, 'login': login}}


class FakeAPI:
    def __init__(self, *, merged=False, pages=None):
        self.repo = {'full_name': REPO, 'default_branch': 'main'}
        self.pr = pull(merged=merged)
        self.pages = pages or {1: []}
        self.calls = []

    def request(self, suffix, *, document=None):
        self.calls.append((suffix, copy.deepcopy(document)))
        if document is not None:
            return {'id': 57}
        if suffix == '':
            return copy.deepcopy(self.repo)
        if suffix == '/pulls/12':
            return copy.deepcopy(self.pr)
        return copy.deepcopy(self.pages[int(suffix.rsplit('=', 1)[1])])

    @property
    def posts(self):
        return [(suffix, document) for suffix, document in self.calls if document is not None]


class NotificationTests(unittest.TestCase):
    def test_pending_mentions_owner_and_preserves_full_detailed_report_and_manual_steps(self):
        api = FakeAPI()
        result = notices.notify(report(), REPO, 'https://github.com/owner/gki/pull/12', 'pending', api=api)
        self.assertTrue(result['notified'])
        self.assertEqual(57, result['comment_id'])
        self.assertEqual(1, len(api.posts))
        endpoint, document = api.posts[0]
        self.assertEqual('/issues/12/comments', endpoint)
        body = document['body']
        self.assertIn('@owner', body)
        self.assertIn('Actions → 检查并同步官方更新 → Run workflow', body)
        self.assertIn('**operation** 选择为 **同步官方更新**', body)
        self.assertIn('手动合并', body)
        self.assertIn(report()['summary_markdown'], body)
        self.assertIn('没有自动编译 APK 或内核', body)
        self.assertEqual(notices.marker('pending', DIGEST), body.splitlines()[0])

    def test_synced_requires_real_merged_sha_and_has_its_own_marker(self):
        pending = comment(notices.marker('pending', DIGEST))
        api = FakeAPI(merged=True, pages={1: [pending]})
        result = notices.notify(report(), REPO, 12, 'synced', commit=COMMIT, api=api)
        self.assertTrue(result['notified'])
        body = api.posts[0][1]['body']
        self.assertIn(notices.marker('synced', DIGEST), body)
        self.assertIn('/commit/' + COMMIT, body)
        self.assertIn('已同步到默认分支', body)
        self.assertNotIn('等待你手动同步', body)

    def test_no_changes_does_not_even_create_api_or_require_a_token(self):
        with mock.patch.object(notices, 'GitHub') as api:
            result = notices.notify(report(has_changes=False), REPO, '12', 'pending')
        api.assert_not_called()
        self.assertEqual('no_changes', result['reason'])

    def test_partial_checks_keep_the_detailed_failed_check_without_requiring_fake_success(self):
        summary = 'GKI 新增 6.6.89；官方源码检查未完成，不能确认有无更新。'
        document = report(checks={'official': 'failure', 'catalog': 'success'}, summary_markdown=summary)
        api = FakeAPI()
        self.assertTrue(notices.notify(document, REPO, 12, 'pending', commit='', api=api)['notified'])
        self.assertIn(summary, api.posts[0][1]['body'])

    def test_only_exact_bot_state_and_digest_prevent_another_mention(self):
        marker = notices.marker('pending', DIGEST)
        for previous in (comment(marker, login='owner', kind='User'),
                         comment(marker, login='other[bot]'), comment(marker, kind='User'),
                         comment(notices.marker('synced', DIGEST)),
                         comment(notices.marker('pending', 'f' * 64))):
            with self.subTest(previous=previous):
                api = FakeAPI(pages={1: [previous]})
                self.assertTrue(notices.notify(report(), REPO, 12, 'pending', api=api)['notified'])
        api = FakeAPI(pages={1: [comment(marker)]})
        self.assertEqual('already_notified', notices.notify(report(), REPO, 12, 'pending', api=api)['reason'])
        self.assertEqual([], api.posts)

    def test_all_pages_are_read_and_an_old_bot_comment_is_found(self):
        api = FakeAPI(pages={1: [comment()] * 100, 2: [comment(notices.marker('pending', DIGEST))]})
        self.assertFalse(notices.notify(report(), REPO, 12, 'pending', api=api)['notified'])
        self.assertEqual(4, len(api.calls))
        self.assertEqual('/issues/12/comments?per_page=100&page=2', api.calls[-1][0])
        self.assertEqual([], api.posts)

    def test_incomplete_page_budget_never_posts_even_if_duplicate_seen(self):
        api = FakeAPI(pages={page: [comment(notices.marker('pending', DIGEST))] * 100
                             for page in range(1, notices.MAX_PAGES + 1)})
        with self.assertRaisesRegex(ValueError, 'Too many PR comments'):
            notices.notify(report(), REPO, 12, 'pending', api=api)
        self.assertEqual([], api.posts)

    def test_repository_default_branch_and_owned_pr_are_mandatory(self):
        variants = []
        for side, key, value in (('head', 'ref', 'foreign'), ('base', 'ref', 'release')):
            item = pull(); item[side][key] = value; variants.append(item)
        for side in ('head', 'base'):
            item = pull(); item[side]['repo']['full_name'] = 'attacker/gki'; variants.append(item)
            item = pull(); item[side]['repo'] = None; variants.append(item)
        variants.extend([{**pull(), 'body': 'unmarked'}, {**pull(), 'number': True},
                         {**pull(), 'number': 13}, {**pull(), 'draft': True},
                         {**pull(), 'state': 'closed'}, {**pull(), 'merged': 0}])
        for previous in variants:
            with self.subTest(previous=previous):
                api = FakeAPI(); api.pr = previous
                with self.assertRaises(ValueError): notices.notify(report(), REPO, 12, 'pending', api=api)
                self.assertEqual([], api.posts)
        for changed in ({'full_name': 'other/gki'}, {'default_branch': 'release'}):
            api = FakeAPI(); api.repo.update(changed)
            with self.assertRaises(ValueError): notices.notify(report(), REPO, 12, 'pending', api=api)
            self.assertEqual(1, len(api.calls))

    def test_unmerged_mismatched_commit_or_pending_merged_are_not_claimed_synced(self):
        for state, merged, commit in (('synced', False, None), ('synced', True, 'c' * 40),
                                      ('pending', True, None), ('pending', False, COMMIT)):
            api = FakeAPI(merged=merged)
            with self.subTest(state=state, merged=merged), self.assertRaises(ValueError):
                notices.notify(report(), REPO, 12, state, commit=commit, api=api)
            self.assertEqual([], api.posts)

    def test_invalid_comment_inventory_never_posts_and_large_detail_has_bounded_preview(self):
        for batch in ({}, [None], [{'body': 'x'}], [comment()] * 101):
            api = FakeAPI(pages={1: batch})
            with self.subTest(batch_type=type(batch)), self.assertRaises(ValueError):
                notices.notify(report(), REPO, 12, 'pending', api=api)
            self.assertEqual([], api.posts)
        api = FakeAPI()
        notices.notify(report(summary_markdown='中' * 20000), REPO, 12, 'pending', api=api)
        self.assertEqual(1, len(api.posts))
        body = api.posts[0][1]['body']
        self.assertLess(len(body.encode('utf-8')), notices.MAX_COMMENT)
        self.assertIn('完整逐项变化', body)

    def test_foreign_pr_urls_and_invalid_inputs_never_touch_api(self):
        for reference in ('https://github.com/attacker/gki/pull/12', 'https://evil.example/owner/gki/pull/12',
                          'https://github.com/owner/gki/pull/12?token=x', '12\n', True, 0, -1, '0'):
            api = FakeAPI()
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                notices.notify(report(), REPO, reference, 'pending', api=api)
            self.assertEqual([], api.calls)
        for document in (report(schema_version=True), report(digest='A' * 64), report(has_changes=1),
                         report(summary_markdown=''), report(summary_markdown='secret\0'),
                         report(checks={'official': 'ok', 'catalog': 'success'})):
            api = FakeAPI()
            with self.subTest(document=document), self.assertRaises(ValueError):
                notices.notify(document, REPO, 12, 'pending', api=api)
            self.assertEqual([], api.calls)

    def test_file_and_cli_are_bounded_strict_and_only_publish_public_result(self):
        with contextlib.ExitStack() as cleanup:
            directory = ROOT.parent / '.gki-upstream-cache' / ('notice-test-' + uuid.uuid4().hex)
            directory.mkdir()
            cleanup.callback(shutil.rmtree, directory)
            path = directory / 'report.json'
            path.write_text(json.dumps(report(), ensure_ascii=False), encoding='utf-8')
            self.assertEqual(report(), notices.read_report(path))
            for raw in (b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}', b'\xff',
                        b'x' * (notices.MAX_REPORT + 1)):
                path.write_bytes(raw)
                with self.assertRaises(ValueError): notices.read_report(path)
            path.write_text(json.dumps(report()), encoding='utf-8')
            environment = {'GITHUB_REPOSITORY': REPO, 'GH_TOKEN': 'private-token'}
            out = io.StringIO()
            with mock.patch.dict(os.environ, environment), mock.patch.object(notices, 'GitHub', return_value=FakeAPI()), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(0, notices.main(['--report', str(path), '--pull-request', '12', '--state', 'pending',
                                                 '--commit', '']))
            self.assertTrue(json.loads(out.getvalue())['notified'])
            self.assertNotIn('private-token', out.getvalue())
            errors = io.StringIO()
            with mock.patch.dict(os.environ, environment), \
                    mock.patch.object(notices, 'GitHub', side_effect=RuntimeError('private-token')), \
                    contextlib.redirect_stderr(errors):
                self.assertEqual(1, notices.main(['--report', str(path), '--pull-request', '12', '--state', 'pending']))
            self.assertNotIn('private-token', errors.getvalue())


def response(raw, *, url=None, headers=None):
    value = io.BytesIO(raw)
    value.headers = headers or {}
    if url is not None:
        value.geturl = lambda: url
    return value


class NotificationHTTPTests(unittest.TestCase):
    def test_exact_comment_endpoint_and_only_fixed_github_host_receive_token(self):
        opener = mock.Mock(open=mock.Mock(return_value=response(b'{"id":57}')))
        api = notices.GitHub(REPO, 12, 'private-token', opener)
        self.assertEqual({'id': 57}, api.request('/issues/12/comments', document={'body': '@owner pending'}))
        request = opener.open.call_args.args[0]
        self.assertEqual('POST', request.get_method())
        self.assertEqual('https://api.github.com/repos/owner/gki/issues/12/comments', request.full_url)
        self.assertEqual('Bearer private-token', request.get_header('Authorization'))
        self.assertEqual({'body': '@owner pending'}, json.loads(request.data))
        for suffix in ('https://evil.example', '//evil.example', '/issues/13/comments', '/pulls/12/merge',
                       '/issues/12/comments?per_page=100&page=11', '/issues/12/comments#x', '/../../user'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError): api.request(suffix)
        with self.assertRaises(ValueError): api.request('/pulls/12', document={'body': 'wrong endpoint'})
        self.assertEqual(1, opener.open.call_count)

    def test_redirect_invalid_json_oversize_and_incomplete_pagination_are_rejected(self):
        url = 'https://api.github.com/repos/owner/gki/pulls/12'
        variants = (response(b'{}', url='https://evil.example'), response(b'{"x":1,"x":2}'),
                    response(b'x' * (notices.MAX_RESPONSE + 1)),
                    response(b'[]', headers={'Link': '<https://evil.example>; rel="next"'}))
        for reply in variants:
            opener = mock.Mock(open=mock.Mock(return_value=reply))
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                notices.GitHub(REPO, 12, 'secret', opener).request('/pulls/12')
        for error in (HTTPError(url, 403, 'private-token', {}, None), URLError('private-token')):
            opener = mock.Mock(open=mock.Mock(side_effect=error))
            with self.subTest(error=type(error)), self.assertRaises(ValueError) as caught:
                notices.GitHub(REPO, 12, 'secret', opener).request('/pulls/12')
            self.assertNotIn('private-token', str(caught.exception))
        with self.assertRaises(RuntimeError):
            notices.NoRedirect().redirect_request(None, None, 302, 'Moved', {}, 'https://evil.example')


if __name__ == '__main__':
    unittest.main()
