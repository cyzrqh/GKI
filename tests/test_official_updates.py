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
from urllib.error import HTTPError
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
                                                             {'status': 'behind'}]) as github:
            with patch.object(updates, 'github_commit', side_effect=[NEW, OLD_RELEASE]) as commit:
                with patch.object(updates, 'gitlab_head', side_effect=lambda target: PINS[target]) as gitlab:
                    snapshot = updates.fetch_snapshot(current)
        self.assertEqual(snapshot['sukisu']['head'], NEW)
        self.assertFalse(snapshot['sukisu']['release']['newer_than_baseline'])
        self.assertEqual(github.call_args_list[1].args[1], '/releases/latest')
        self.assertEqual(commit.call_args_list[0].args, (updates.SUKISU, 'main'))
        self.assertEqual(commit.call_args_list[1].args, (updates.SUKISU, 'v4.0.0'))
        self.assertEqual([call.args[0] for call in gitlab.call_args_list], list(updates.TARGETS))

    def test_no_official_release_is_supported_without_inventing_a_release(self):
        with patch.object(updates, 'github_json', side_effect=[{'default_branch': 'main'}, None]):
            with patch.object(updates, 'github_commit', return_value=BASE):
                with patch.object(updates, 'gitlab_head', side_effect=lambda target: PINS[target]):
                    self.assertIsNone(updates.fetch_snapshot(updates.baseline(self.root))['sukisu']['release'])


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


if __name__ == '__main__':
    unittest.main()
