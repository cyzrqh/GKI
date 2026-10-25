import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import check_official_updates as updates

BASE = 'a' * 40
NEW = 'b' * 40
OLD_RELEASE = 'c' * 40
PINS = {target: 'd' * 39 + str(index) for index, target in enumerate(updates.TARGETS)}


class OfficialCandidateTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.official-updates-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        profile = {'schema_version': 1, 'sukisu_susfs': {
            'source_ref': BASE, 'ksu_commits': [BASE], 'tiers': ['Stable(标准)', 'Dev(开发)'],
            'version': 'v2.3.0', 'pins': PINS}}
        self.profile = self.root / 'build-compatibility.json'
        self.profile.write_text(json.dumps(profile), encoding='utf-8')
        self.original_profile = self.profile.read_bytes()
        self.output = self.root / 'github-output.txt'
        self.report = self.root / 'reports'
        environment = patch.dict(os.environ, {'GITHUB_OUTPUT': str(self.output), 'GH_TOKEN': '', 'GITHUB_TOKEN': ''})
        environment.start()
        self.addCleanup(environment.stop)
        self.snapshot = {'schema_version': 1,
                         'sukisu': {'branch': 'main', 'head': BASE, 'release': {
                             'tag': 'v4.2.1', 'commit': BASE, 'newer_than_baseline': False}},
                         'susfs': copy.deepcopy(PINS)}

    def invoke(self, mode, snapshot=None, fixed=False):
        snapshot = snapshot or self.snapshot
        arguments = [mode, '--repo-root', str(self.root), '--report-dir', str(self.report)]
        if fixed:
            arguments += ['--candidates-json', json.dumps(snapshot)]
        with patch.object(updates, 'fetch_snapshot', return_value=copy.deepcopy(snapshot)) as fetch:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = updates.main(arguments)
        if fixed:
            fetch.assert_not_called()
        values = dict(line.split('=', 1) for line in self.output.read_text(encoding='utf-8').splitlines())
        self.assertEqual(self.profile.read_bytes(), self.original_profile)
        return result, values

    def test_check_only_is_read_only_and_uses_known_compatibility_baseline(self):
        result, values = self.invoke('--check-only')
        self.assertEqual(result, 0)
        self.assertEqual(values['has_updates'], 'false')
        self.assertEqual(json.loads(values['candidates_json']), self.snapshot)
        self.assertEqual(values['heads_marker'], updates.heads_marker(self.snapshot))
        self.assertFalse((self.root / updates.CANDIDATE_PATH).exists())
        self.assertFalse(self.report.exists())

    def test_prepare_records_candidates_and_review_body_without_changing_build_pins(self):
        self.snapshot['sukisu']['head'] = NEW
        self.snapshot['susfs']['android14-6.1'] = NEW
        result, values = self.invoke('--prepare', fixed=True)
        self.assertEqual(result, 0)
        self.assertEqual(values['has_updates'], 'true')
        candidate = json.loads((self.root / updates.CANDIDATE_PATH).read_text(encoding='utf-8'))
        self.assertEqual(candidate['status'], 'candidates-require-validation')
        self.assertEqual(candidate['baseline']['sukisu'], BASE)
        self.assertEqual(candidate['sources'], self.snapshot)
        self.assertEqual(len(candidate['changes']), 2)
        self.assertEqual(candidate['acknowledged_heads'], updates.head_state(self.snapshot))
        body = Path(values['body_path']).read_text(encoding='utf-8')
        self.assertIn('尚未将候选版本设为构建版本', body)
        self.assertIn('需要核对内核 API', body)
        self.assertIn(BASE, body)
        self.assertIn('SUSFS `v2.3.0`', body)
        self.assertIn(updates.heads_marker(self.snapshot), body)
        self.assertEqual(json.loads(values['changed_files_json']), [updates.CANDIDATE_PATH])

    def test_same_acknowledged_heads_do_not_produce_daily_duplicate_pr(self):
        self.snapshot['sukisu']['head'] = NEW
        self.invoke('--prepare', fixed=True)
        before = (self.root / updates.CANDIDATE_PATH).read_bytes()
        result, values = self.invoke('--check-only')
        self.assertEqual(result, 0)
        self.assertEqual(values['has_updates'], 'false')
        self.invoke('--prepare', fixed=True)
        self.assertEqual((self.root / updates.CANDIDATE_PATH).read_bytes(), before)
        self.assertEqual(len(list(self.report.glob('*.md'))), 1)
        self.snapshot['susfs']['android15-6.6'] = 'e' * 40
        result, values = self.invoke('--check-only')
        self.assertEqual(values['has_updates'], 'true')

    def test_development_updates_are_recorded_and_acknowledged_without_rewriting_legacy_snapshot(self):
        # Deployed schema-1 records remain valid without a development field.
        self.assertEqual(self.snapshot, updates.validate_snapshot(copy.deepcopy(self.snapshot)))
        self.assertNotIn('sukisu_development', updates.head_state(self.snapshot))
        self.snapshot['sukisu']['head'] = NEW
        self.invoke('--prepare', fixed=True)
        legacy = json.loads((self.root / updates.CANDIDATE_PATH).read_text(encoding='utf-8'))
        self.assertEqual(legacy['acknowledged_heads'], updates.acknowledged(self.root))
        old_marker = updates.heads_marker(self.snapshot)
        self.snapshot['sukisu']['development'] = {'branch': 'dev', 'head': 'e' * 40}
        result, values = self.invoke('--prepare', fixed=True)
        self.assertEqual((0, 'true'), (result, values['has_updates']))
        self.assertNotEqual(old_marker, values['heads_marker'])
        candidate = json.loads((self.root / updates.CANDIDATE_PATH).read_text(encoding='utf-8'))
        self.assertEqual({'branch': 'dev', 'head': 'e' * 40}, candidate['acknowledged_heads']['sukisu_development'])
        self.assertEqual('SukiSU development', candidate['changes'][-1]['source'])
        self.assertEqual('dev', candidate['changes'][-1]['ref'])
        self.assertEqual('false', self.invoke('--check-only')[1]['has_updates'])
        self.snapshot['sukisu']['development']['head'] = 'f' * 40
        self.assertEqual('true', self.invoke('--check-only')[1]['has_updates'])

    def test_development_only_change_is_detected_and_identical_commits_are_not_duplicated(self):
        self.snapshot['sukisu']['development'] = {'branch': 'dev', 'head': NEW}
        selected = updates.changes(updates.baseline(self.root), self.snapshot)
        self.assertEqual(1, len(selected))
        self.assertEqual(('SukiSU development', 'dev', NEW),
                         tuple(selected[0][key] for key in ('source', 'ref', 'candidate')))
        self.snapshot['sukisu']['head'] = NEW
        self.assertEqual(['SukiSU'], [item['source'] for item in updates.changes(updates.baseline(self.root), self.snapshot)])

    def test_development_snapshot_accepts_only_fixed_official_dev_and_preserves_legacy_ack(self):
        for value in ({'branch': 'main', 'head': NEW}, {'branch': 'dev', 'head': 'moving-dev'},
                      {'branch': 'dev', 'head': NEW, 'command': 'unused'}, 'dev', True):
            invalid = copy.deepcopy(self.snapshot)
            invalid['sukisu']['development'] = value
            with self.subTest(value=value), self.assertRaises(updates.UpdateError):
                updates.validate_snapshot(invalid)
        self.snapshot['sukisu']['development'] = {'branch': 'dev', 'head': NEW.upper()}
        normalized = updates.validate_snapshot(self.snapshot)
        self.assertEqual(NEW, normalized['sukisu']['development']['head'])
        self.snapshot['sukisu']['development'] = None
        self.assertIsNone(updates.validate_snapshot(self.snapshot)['sukisu']['development'])
        self.assertIsNone(updates.head_state(self.snapshot)['sukisu_development'])

    def test_older_official_release_is_not_announced_as_an_upgrade(self):
        self.snapshot['sukisu']['release'] = {'tag': 'v4.0.0', 'commit': OLD_RELEASE, 'newer_than_baseline': False}
        _, values = self.invoke('--check-only')
        self.assertEqual(values['has_updates'], 'false')
        self.snapshot['sukisu']['release']['newer_than_baseline'] = True
        _, values = self.invoke('--check-only')
        self.assertEqual(values['has_updates'], 'true')

    def test_marker_is_order_independent_and_tracks_release_tag_and_every_susfs_head(self):
        marker = updates.heads_marker(self.snapshot)
        reordered = copy.deepcopy(self.snapshot)
        reordered['susfs'] = dict(reversed(list(reordered['susfs'].items())))
        reordered['sukisu']['head'] = BASE.upper()
        self.assertEqual(updates.heads_marker(reordered), marker)
        reordered['sukisu']['release']['newer_than_baseline'] = True
        self.assertEqual(updates.heads_marker(reordered), marker)
        reordered['sukisu']['release']['tag'] = 'v4.2.2'
        self.assertNotEqual(updates.heads_marker(reordered), marker)
        for target in updates.TARGETS:
            changed = copy.deepcopy(self.snapshot)
            changed['susfs'][target] = NEW
            self.assertNotEqual(updates.heads_marker(changed), marker)

    def test_invalid_probe_or_acknowledgement_does_not_overwrite_candidate_file(self):
        self.snapshot['sukisu']['head'] = NEW
        self.invoke('--prepare', fixed=True)
        path = self.root / updates.CANDIDATE_PATH
        before = path.read_bytes()
        invalid = copy.deepcopy(self.snapshot)
        invalid['sukisu']['head'] = 'main\ninjected=true'
        result, values = self.invoke('--prepare', invalid, fixed=True)
        self.assertEqual(result, 1)
        self.assertEqual(values['has_updates'], 'false')
        self.assertEqual(path.read_bytes(), before)
        path.write_text('{"schema_version":999}', encoding='utf-8')
        result, values = self.invoke('--prepare', fixed=True)
        self.assertEqual(result, 1)
        self.assertEqual(values['has_updates'], 'false')
        self.assertEqual(path.read_text(), '{"schema_version":999}')

    def test_atomic_candidate_write_failure_keeps_previous_acknowledgement(self):
        self.snapshot['sukisu']['head'] = NEW
        self.invoke('--prepare', fixed=True)
        path = self.root / updates.CANDIDATE_PATH
        before = path.read_bytes()
        self.snapshot['susfs']['android12-5.10'] = NEW
        with patch.object(updates.os, 'replace', side_effect=OSError('simulated failure')):
            result, values = self.invoke('--prepare', fixed=True)
        self.assertEqual(result, 1)
        self.assertEqual(values['has_updates'], 'false')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(path.parent.glob('*.tmp')))

    def test_fetch_reads_default_head_release_and_all_five_official_susfs_branches(self):
        current = updates.baseline(self.root)
        with patch.object(updates, 'github_json', side_effect=[{'default_branch': 'main'}, {'tag_name': 'v4.0.0'},
                                                             {'status': 'behind', 'base_commit': {'sha': BASE}},
                                                             {'name': 'dev', 'commit': {'sha': 'e' * 40}}]) as github:
            with patch.object(updates, 'github_commit', side_effect=[NEW, OLD_RELEASE]) as commit:
                with patch.object(updates, 'gitlab_head', side_effect=lambda target: PINS[target]) as gitlab:
                    snapshot = updates.fetch_snapshot(current)
        self.assertEqual(snapshot['sukisu']['head'], NEW)
        self.assertFalse(snapshot['sukisu']['release']['newer_than_baseline'])
        self.assertEqual(github.call_args_list[1].args[1], '/releases/latest')
        self.assertEqual(github.call_args_list[2].args[1],
                         '/compare/' + BASE + '...' + OLD_RELEASE + '?per_page=1&page=2')
        self.assertEqual(commit.call_args_list[0].args, (updates.SUKISU, 'main'))
        self.assertEqual(commit.call_args_list[1].args, (updates.SUKISU, 'v4.0.0'))
        self.assertEqual(snapshot['sukisu']['development'], {'branch': 'dev', 'head': 'e' * 40})
        self.assertEqual(github.call_args_list[3].args, (updates.SUKISU, '/branches/dev'))
        self.assertEqual(github.call_args_list[3].kwargs, {'missing': True})
        self.assertEqual([call.args[0] for call in gitlab.call_args_list], list(updates.TARGETS))

    def test_no_official_release_is_supported_without_inventing_a_release(self):
        with patch.object(updates, 'github_json', side_effect=[{'default_branch': 'main'}, None, None]):
            with patch.object(updates, 'github_commit', return_value=BASE):
                with patch.object(updates, 'gitlab_head', side_effect=lambda target: PINS[target]):
                    self.assertIsNone(updates.fetch_snapshot(updates.baseline(self.root))['sukisu']['release'])

    def test_missing_development_branch_is_explicit_and_existing_default_dev_is_reused(self):
        for branch in ('main', 'dev'):
            reader = MagicMock(side_effect=[{'default_branch': branch}, None, None])
            with self.subTest(branch=branch), patch.object(updates, 'github_json', reader), \
                    patch.object(updates, 'github_commit', return_value=BASE) as commit, \
                    patch.object(updates, 'gitlab_head', side_effect=lambda target: PINS[target]):
                found = updates.fetch_snapshot(updates.baseline(self.root))
            expected = {'branch': 'dev', 'head': BASE} if branch == 'dev' else None
            self.assertEqual(expected, found['sukisu']['development'])
            self.assertEqual(2 if branch == 'dev' else 3, reader.call_count)
            commit.assert_called_once_with(updates.SUKISU, branch)

    def test_invalid_or_unavailable_development_probe_never_announces_no_updates(self):
        for document in ({'name': 'main', 'commit': {'sha': NEW}}, {'name': 'dev', 'commit': {'sha': 'dev'}},
                         {'name': 'dev'}, []):
            with self.subTest(document=document), patch.object(updates, 'github_json',
                    side_effect=[{'default_branch': 'main'}, None, document]), \
                    patch.object(updates, 'github_commit', return_value=BASE):
                with self.assertRaisesRegex(updates.UpdateError, 'development branch'):
                    updates.fetch_snapshot(updates.baseline(self.root))
        def unavailable(repository, resource='', **keywords):
            if not resource:
                return {'default_branch': 'main'}
            if resource == '/releases/latest':
                return None
            self.assertEqual('/branches/dev', resource)
            raise updates.SourceResolutionError('Official GitHub source lookup failed')
        with patch.object(updates, 'github_json', side_effect=unavailable), \
                patch.object(updates, 'github_commit', return_value=BASE), patch.object(updates.time, 'sleep'):
            with self.assertRaisesRegex(updates.UpdateError, 'development branch.*3 attempts'):
                updates.fetch_snapshot(updates.baseline(self.root))

    def test_release_ancestry_avoids_large_first_page_diff_and_requires_exact_baseline(self):
        def github(repository, resource='', **kwargs):
            if not resource:
                return {'default_branch': 'main'}
            if resource == '/releases/latest':
                return {'tag_name': 'v4.2.1-r2'}
            if resource == '/branches/dev':
                return None
            if resource == '/compare/' + BASE + '...' + NEW + '?per_page=1&page=2':
                return {'status': 'ahead', 'base_commit': {'sha': BASE}, 'total_commits': 30}
            # The unpaginated r2 response is 1.4 MB and is correctly rejected
            # by the shared client's 1 MiB limit. Do not increase that limit.
            raise updates.SourceResolutionError('GitHub source lookup response is too large')
        with patch.object(updates, 'github_json', side_effect=github):
            with patch.object(updates, 'github_commit', return_value=NEW):
                with patch.object(updates, 'gitlab_head', side_effect=lambda target: PINS[target]):
                    found = updates.fetch_snapshot(updates.baseline(self.root))
        self.assertTrue(found['sukisu']['release']['newer_than_baseline'])
        for comparison in ({'status': 'ahead'}, {'status': 'ahead', 'base_commit': {'sha': NEW}},
                           {'status': 'unknown', 'base_commit': {'sha': BASE}}):
            with self.subTest(comparison=comparison):
                with patch.object(updates, 'github_json', side_effect=[{'default_branch': 'main'},
                                                                       {'tag_name': 'v4.2.1-r2'}, comparison]):
                    with patch.object(updates, 'github_commit', return_value=NEW):
                        with self.assertRaisesRegex(updates.UpdateError, 'ancestry.*baseline'):
                            updates.fetch_snapshot(updates.baseline(self.root))

    def test_github_lookups_retry_transient_failures_only_and_report_source_stage(self):
        retry = updates.SourceResolutionError('Official GitHub source lookup failed')
        reader = MagicMock(side_effect=[retry, retry, {'default_branch': 'main'}])
        with patch.object(updates.time, 'sleep') as sleep:
            self.assertEqual(updates.github_lookup('SukiSU repository', reader), {'default_branch': 'main'})
        self.assertEqual(reader.call_count, 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(1,), (2,)])
        for message, category in (('GitHub source lookup response is too large', 'size limit'),
                                  ('GitHub source lookup redirects are not permitted', 'metadata'),
                                  ('untrusted private-token response', 'metadata')):
            reader = MagicMock(side_effect=updates.SourceResolutionError(message))
            with self.subTest(message=message), patch.object(updates.time, 'sleep') as sleep:
                with self.assertRaisesRegex(updates.UpdateError, 'SukiSU release ancestry.*' + category) as raised:
                    updates.github_lookup('SukiSU release ancestry', reader)
                self.assertEqual(reader.call_count, 1)
                sleep.assert_not_called()
                self.assertNotIn('private-token', str(raised.exception))
        reader = MagicMock(side_effect=retry)
        with patch.object(updates.time, 'sleep'):
            with self.assertRaisesRegex(updates.UpdateError, 'SukiSU default branch.*3 attempts'):
                updates.github_lookup('SukiSU default branch', reader)
        self.assertEqual(reader.call_count, 3)

    def test_main_reports_validation_stage_without_echoing_unknown_exception_details(self):
        arguments = ['--check-only', '--repo-root', str(self.root)]
        with patch.object(updates, 'fetch_snapshot', side_effect=updates.UpdateError(
                'SukiSU release ancestry: response exceeds size limit')):
            output = io.StringIO()
            with redirect_stderr(output):
                self.assertEqual(updates.main(arguments), 1)
        self.assertIn('official source lookup', output.getvalue())
        self.assertIn('SukiSU release ancestry: response exceeds size limit', output.getvalue())
        with patch.object(updates, 'fetch_snapshot', side_effect=ValueError('secret-response-token')):
            output = io.StringIO()
            with redirect_stderr(output):
                self.assertEqual(updates.main(arguments), 1)
        self.assertIn('JSON or snapshot metadata is invalid', output.getvalue())
        self.assertNotIn('secret-response-token', output.getvalue())


class OfficialGitLabSecurityTests(unittest.TestCase):
    def response(self, document=None):
        url = 'https://gitlab.com/api/v4/projects/simonpunk%2Fsusfs4ksu/repository/branches/gki-android14-6.1'
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.geturl.return_value = url
        response.read.return_value = json.dumps(document or {'name': 'gki-android14-6.1', 'commit': {'id': NEW}}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        return response, opener

    def test_gitlab_get_never_receives_github_token_and_rejects_redirects(self):
        _, opener = self.response()
        with patch.dict(os.environ, {'GH_TOKEN': 'private-github-token', 'GITHUB_TOKEN': 'other-private-token'}):
            with patch.object(updates, 'build_opener', return_value=opener) as build:
                self.assertEqual(updates.gitlab_head('android14-6.1'), NEW)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertIsNone(request.get_header('Private-token'))
        self.assertNotIn('private-github-token', str(request.headers))
        self.assertIsInstance(build.call_args.args[0], updates.NoRedirect)
        with self.assertRaisesRegex(updates.UpdateError, 'redirects'):
            updates.NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://example.com/')

    def test_unexpected_final_url_is_rejected_before_response_body(self):
        response, opener = self.response()
        response.geturl.return_value = 'https://gitlab.com.evil.test/steal'
        with patch.object(updates, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(updates.UpdateError, 'unexpected URL'):
                updates.gitlab_head('android14-6.1')
        response.read.assert_not_called()

    def test_invalid_branch_sha_and_large_or_error_response_fail_safely(self):
        for document in [{'commit': {'id': 'main'}}, {'name': 'other', 'commit': {'id': NEW}}, []]:
            _, opener = self.response()
            opener.open.return_value.read.return_value = json.dumps(document).encode()
            with self.subTest(document=document), patch.object(updates, 'build_opener', return_value=opener):
                with self.assertRaises(updates.UpdateError):
                    updates.gitlab_head('android14-6.1')
        _, opener = self.response()
        opener.open.return_value.read.return_value = b'x' * (1024 * 1024 + 1)
        with patch.object(updates, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(updates.UpdateError, 'too large'):
                updates.gitlab_head('android14-6.1')
        with patch.object(updates, 'build_opener') as build:
            build.return_value.open.side_effect = HTTPError('https://secret-url', 403, 'private-token', {}, None)
            with self.assertRaises(updates.UpdateError) as raised:
                updates.gitlab_head('android14-6.1')
        self.assertNotIn('private-token', str(raised.exception))
        self.assertNotIn('secret-url', str(raised.exception))

    def test_gitlab_retries_transient_http_and_network_errors_without_relaxing_validation(self):
        response, opener = self.response()
        opener.open.side_effect = [HTTPError('https://private-url', 503, 'secret-body', {}, None),
                                   URLError('secret-network-detail'), response]
        with patch.object(updates, 'build_opener', return_value=opener):
            with patch.object(updates.time, 'sleep') as sleep:
                self.assertEqual(updates.gitlab_head('android14-6.1'), NEW)
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(1,), (2,)])
        opener.open.side_effect = HTTPError('https://private-url', 429, 'secret-body', {}, None)
        with patch.object(updates, 'build_opener', return_value=opener):
            with patch.object(updates.time, 'sleep'):
                with self.assertRaisesRegex(updates.UpdateError, 'SUSFS gki-android14-6.1.*HTTP 429.*3 attempts') as raised:
                    updates.gitlab_head('android14-6.1')
        self.assertNotIn('private-url', str(raised.exception))
        self.assertNotIn('secret-body', str(raised.exception))

    def test_invalid_gitlab_json_has_safe_source_label_and_is_not_retried(self):
        response, opener = self.response()
        response.read.return_value = b'{invalid-secret-body'
        with patch.object(updates, 'build_opener', return_value=opener):
            with patch.object(updates.time, 'sleep') as sleep:
                with self.assertRaisesRegex(updates.UpdateError, 'SUSFS gki-android14-6.1.*invalid JSON') as raised:
                    updates.gitlab_head('android14-6.1')
        self.assertNotIn('secret-body', str(raised.exception))
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
