import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import resolve_build_sources as sources

KSU = 'c' * 40
NEW_KSU = 'd' * 40
REPO = 'SukiSU-Ultra/SukiSU-Ultra'
PROFILE = {'schema_version': 1, 'sukisu_susfs': {
    'ksu_commits': [KSU], 'tiers': ['Stable(标准)', 'Dev(开发)'],
    'version': 'v2.3.0', 'pins': {'android14-6.1': 'a' * 40}}}


class BuildSourceTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.sources-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.profile = self.root / 'build-compatibility.json'
        self.write_profile(PROFILE)
        self.output = self.root / 'outputs'
        environment = patch.dict(os.environ, {'GITHUB_OUTPUT': str(self.output), 'GH_TOKEN': '', 'GITHUB_TOKEN': ''})
        environment.start()
        self.addCleanup(environment.stop)

    def write_profile(self, value):
        self.profile.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def resolve(self, **overrides):
        values = dict(root=self.root, variant='SukiSU', tier='Dev(开发)', susfs=True)
        values.update(overrides)
        return sources.resolve_sources(**values)

    def test_sukisu_stable_and_dev_follow_official_source_with_or_without_susfs(self):
        for tier, branch, responses in (
                ('Stable(标准)', 'v4.2.1-r1', [{'tag_name': 'v4.2.1-r1'}, {'sha': NEW_KSU.upper()}]),
                ('Dev(开发)', 'dev', [{'default_branch': 'main'}, {'commit': {'sha': NEW_KSU.upper()}}])):
            for susfs in (True, False):
                with self.subTest(tier=tier, susfs=susfs), patch.object(sources, 'github_json') as api:
                    api.side_effect = responses
                    result = self.resolve(tier=tier, susfs=susfs)
                    self.assertEqual(result, {'ksu_ref': NEW_KSU, 'ksu_repo': REPO, 'ksu_short': NEW_KSU[:7],
                                              'ksu_source_branch': branch, 'effective_ksu_branch': tier})
                    self.assertEqual(api.call_count, 2)
        self.assertFalse(hasattr(sources, 'subprocess'))
        self.assertNotIn('resolve-ksu-ref.sh', Path(sources.__file__).read_text(encoding='utf-8'))

    def test_sukisu_resolution_does_not_read_compatibility_profile(self):
        self.profile.write_text('invalid compatibility profile', encoding='utf-8')
        for tier, responses in (
                ('Stable(标准)', [{'tag_name': 'v4.2.1-r1'}, {'sha': NEW_KSU}]),
                ('Dev(开发)', [{'default_branch': 'main'}, None, {'sha': NEW_KSU}])):
            with self.subTest(tier=tier), patch.object(sources, 'github_json') as api:
                api.side_effect = responses
                self.assertEqual(self.resolve(tier=tier)['ksu_ref'], NEW_KSU)
        self.profile.unlink()
        with patch.object(sources, 'github_json') as api:
            api.side_effect = [{'default_branch': 'main'}, None, {'sha': NEW_KSU}]
            self.assertEqual(self.resolve()['ksu_ref'], NEW_KSU)

    def test_sukisu_formal_alias_uses_latest_release_and_exports_canonical_tier(self):
        with patch.object(sources, 'github_json') as api:
            api.side_effect = [{'tag_name': 'v4.2.1-r1'}, {'sha': NEW_KSU}]
            result = self.resolve(tier='SukiSU 正式版')
        self.assertEqual(result['ksu_ref'], NEW_KSU)
        self.assertEqual(result['ksu_source_branch'], 'v4.2.1-r1')
        self.assertEqual(result['effective_ksu_branch'], 'Stable(标准)')
        self.assertEqual([call.args[1] for call in api.call_args_list],
                         ['/releases/latest', '/commits/v4.2.1-r1'])

    def test_sukisu_latest_alias_uses_default_branch_when_dev_is_absent(self):
        for branch in ('main', 'next'):
            with self.subTest(branch=branch), patch.object(sources, 'github_json') as api:
                api.side_effect = [{'default_branch': branch}, {'sha': NEW_KSU}, None]
                result = self.resolve(tier='SukiSU 最新版')
            self.assertEqual(result['ksu_ref'], NEW_KSU)
            self.assertEqual(result['ksu_source_branch'], branch)
            self.assertEqual(result['effective_ksu_branch'], 'Latest(最新)')
            self.assertEqual(api.call_args_list[1].args[1], '/commits/' + branch)
            self.assertEqual(api.call_args_list[2].args[1], '/branches/dev')
            self.assertEqual(api.call_args_list[2].kwargs, {'missing': True})
            self.assertEqual(api.call_count, 3)

    def comparison(self, *, ahead=1, behind=0, status='ahead'):
        return {'url': f'https://api.github.com/repos/{REPO}/compare/{KSU}...{NEW_KSU}',
                'base_commit': {'sha': KSU}, 'ahead_by': ahead, 'behind_by': behind,
                'total_commits': ahead, 'status': status}

    def test_sukisu_latest_selects_higher_native_history_count_from_main_or_dev(self):
        for ahead, behind, status, expected, branch in (
                (1, 0, 'ahead', NEW_KSU, 'dev'),
                (0, 1, 'behind', KSU, 'main'),
                (4, 2, 'diverged', NEW_KSU, 'dev'),
                (2, 4, 'diverged', KSU, 'main'),
                (2, 2, 'diverged', KSU, 'main')):
            for tier in ('Latest(最新)', 'SukiSU 最新版'):
                with self.subTest(ahead=ahead, behind=behind, tier=tier), patch.object(sources, 'github_json') as api:
                    api.side_effect = [{'default_branch': 'main'}, {'sha': KSU},
                                       {'name': 'dev', 'commit': {'sha': NEW_KSU.upper()}},
                                       self.comparison(ahead=ahead, behind=behind, status=status)]
                    result = self.resolve(tier=tier)
                self.assertEqual(expected, result['ksu_ref'])
                self.assertEqual(branch, result['ksu_source_branch'])
                self.assertEqual('Latest(最新)', result['effective_ksu_branch'])
                self.assertEqual(f'/compare/{KSU}...{NEW_KSU}?per_page=1&page=2',
                                 api.call_args_list[3].args[1])
                self.assertFalse(any('/actions/' in str(call) or '/releases/' in str(call)
                                     for call in api.call_args_list))

    def test_sukisu_latest_same_head_or_default_dev_needs_no_comparison(self):
        for default, responses, count in (
                ('main', [{'default_branch': 'main'}, {'sha': KSU},
                          {'name': 'dev', 'commit': {'sha': KSU}}], 3),
                ('dev', [{'default_branch': 'dev'}, {'sha': KSU}], 2)):
            with self.subTest(default=default), patch.object(sources, 'github_json', side_effect=responses) as api:
                result = self.resolve(tier='Latest(最新)')
            self.assertEqual(KSU, result['ksu_ref'])
            self.assertEqual(default, result['ksu_source_branch'])
            self.assertEqual(count, api.call_count)

    def test_sukisu_latest_rejects_wrong_branch_invalid_commit_or_comparison(self):
        for development in ({'name': 'feature', 'commit': {'sha': NEW_KSU}},
                            {'name': 'dev', 'commit': {'sha': 'main'}},
                            {'name': 'dev'}, {'commit': {'sha': NEW_KSU}}):
            with self.subTest(development=development), patch.object(sources, 'github_json') as api:
                api.side_effect = [{'default_branch': 'main'}, {'sha': KSU}, development]
                with self.assertRaisesRegex(sources.SourceResolutionError, 'dev branch'):
                    self.resolve(tier='SukiSU 最新版')
        for changes in ({'url': f'https://api.github.com/repos/{REPO}/compare/{KSU}...' + 'e' * 40},
                        {'base_commit': {'sha': NEW_KSU}}, {'ahead_by': True}, {'behind_by': -1},
                        {'ahead_by': '1'}, {'total_commits': 2}, {'status': 'behind'}, {'status': 'identical'}):
            comparison = {**self.comparison(), **changes}
            with self.subTest(changes=changes), patch.object(sources, 'github_json') as api:
                api.side_effect = [{'default_branch': 'main'}, {'sha': KSU},
                                   {'name': 'dev', 'commit': {'sha': NEW_KSU}}, comparison]
                with self.assertRaisesRegex(sources.SourceResolutionError, 'comparison'):
                    self.resolve(tier='Latest(最新)')

    def test_sukisu_latest_does_not_silently_fall_back_on_lookup_failure(self):
        for responses in (
                [{'default_branch': 'main'}, {'sha': KSU}, sources.SourceResolutionError('dev lookup failed')],
                [{'default_branch': 'main'}, {'sha': KSU}, {'name': 'dev', 'commit': {'sha': NEW_KSU}},
                 sources.SourceResolutionError('comparison failed')]):
            with self.subTest(responses=responses), patch.object(sources, 'github_json', side_effect=responses):
                with self.assertRaises(sources.SourceResolutionError):
                    self.resolve(tier='Latest(最新)')

    def test_sukisu_aliases_cannot_silently_select_a_different_variant(self):
        for variant in ('Official', 'BakaSU', 'None'):
            for tier in sources.TIER_ALIASES:
                with self.subTest(variant=variant, tier=tier), patch.object(sources, 'github_json') as api:
                    with self.assertRaisesRegex(sources.SourceResolutionError, 'require the SukiSU variant'):
                        self.resolve(variant=variant, tier=tier)
                api.assert_not_called()

    def test_cli_accepts_latest_alias_but_shares_only_canonical_tier(self):
        with patch.object(sources, 'github_json') as api, redirect_stdout(io.StringIO()):
            api.side_effect = [{'default_branch': 'main'}, {'sha': NEW_KSU}, None]
            status = sources.main(['--root', str(self.root), '--variant', 'SukiSU',
                                   '--tier', 'SukiSU 最新版', '--susfs', 'true'])
        self.assertEqual(status, 0)
        output = self.output.read_text(encoding='utf-8')
        self.assertIn('effective_ksu_branch=Latest(最新)\n', output)
        self.assertNotIn('SukiSU 最新版', output)

    def test_none_needs_no_profile_or_network(self):
        self.profile.unlink()
        with patch.object(sources, 'github_json') as network:
            result = self.resolve(variant='None')
        self.assertEqual(result['ksu_ref'], '')
        self.assertEqual(result['ksu_repo'], '')
        network.assert_not_called()

    def test_latest_reads_official_default_branch_head_without_profile(self):
        self.profile.unlink()
        for variant in sources.REPOSITORIES:
            with self.subTest(variant=variant), patch.object(sources, 'github_json') as api:
                responses = [{'default_branch': 'next'}, {'sha': NEW_KSU.upper()}]
                api.side_effect = responses + ([None] if variant == 'SukiSU' else [])
                result = self.resolve(variant=variant, tier='Latest(最新)')
                self.assertEqual(result['ksu_ref'], NEW_KSU)
                self.assertEqual(result['ksu_source_branch'], 'next')
                self.assertEqual(api.call_args_list[0].args, (sources.REPOSITORIES[variant],))
                self.assertEqual(api.call_args_list[1].args[1], '/commits/next')

    def test_all_variants_stable_resolve_official_release_tag(self):
        self.profile.unlink()
        for variant in sources.REPOSITORIES:
            with self.subTest(variant=variant), patch.object(sources, 'github_json') as api:
                api.side_effect = [{'tag_name': 'v4.2.1'}, {'sha': NEW_KSU}]
                result = self.resolve(variant=variant, tier='Stable(标准)')
                self.assertEqual(result['ksu_source_branch'], 'v4.2.1')
                self.assertEqual(api.call_args_list[0].args[1], '/releases/latest')
                self.assertEqual(api.call_args_list[1].args[1], '/commits/v4.2.1')

    def test_all_variants_dev_uses_real_dev_or_explicit_default_fallback(self):
        for variant in sources.REPOSITORIES:
            for development, expected_branch in [({'commit': {'sha': NEW_KSU}}, 'dev'), (None, 'main')]:
                with self.subTest(variant=variant, expected_branch=expected_branch), patch.object(sources, 'github_json') as api:
                    api.side_effect = [{'default_branch': 'main'}, development, {'sha': NEW_KSU}]
                    result = self.resolve(variant=variant)
                    self.assertEqual(result['ksu_ref'], NEW_KSU)
                    self.assertEqual(result['ksu_source_branch'], expected_branch)
                    self.assertEqual(api.call_args_list[1].args[1], '/branches/dev')
                    self.assertEqual(api.call_count, 2 if development else 3)

    def test_missing_stable_release_fails_without_silent_tier_change(self):
        for variant in sources.REPOSITORIES:
            with self.subTest(variant=variant), patch.object(sources, 'github_json', return_value=None):
                with self.assertRaisesRegex(sources.SourceResolutionError, 'no published release'):
                    self.resolve(variant=variant, tier='Stable(标准)')

    def test_custom_exact_ref_uses_only_requested_official_commit_lookup(self):
        for ref in ('feature/manager', 'v4.2.1', NEW_KSU.upper()):
            with self.subTest(ref=ref), patch.object(sources, 'github_commit', return_value=NEW_KSU) as lookup:
                result = self.resolve(tier='Custom(自定义)', custom_ref=ref)
            lookup.assert_called_once_with(REPO, ref)
            self.assertEqual(result['ksu_ref'], NEW_KSU)

    def test_custom_branch_number_keeps_nth_successful_manager_workflow_semantics(self):
        runs = [{'head_sha': KSU, 'head_branch': 'feature/manager', 'conclusion': 'success'},
                {'head_sha': NEW_KSU, 'head_branch': 'feature/manager', 'conclusion': 'success'}]
        for number, page in ((2, 1), (102, 2)):
            with self.subTest(number=number), patch.object(sources, 'github_json', return_value={'workflow_runs': runs}) as api:
                result = self.resolve(tier='Custom(自定义)', custom_ref='feature/manager:' + str(number))
            self.assertEqual(result['ksu_ref'], NEW_KSU)
            self.assertEqual(result['ksu_source_branch'], 'feature/manager')
            resource = api.call_args.args[1]
            self.assertIn('/actions/workflows/build-manager.yml/runs?', resource)
            self.assertIn('branch=feature%2Fmanager', resource)
            self.assertIn('status=success', resource)
            self.assertIn('page=' + str(page), resource)

    def test_custom_workflow_missing_wrong_branch_or_fork_fails(self):
        for document in [None, {'workflow_runs': []}, {'workflow_runs': [{'head_sha': NEW_KSU}]},
                         {'workflow_runs': [{'head_sha': NEW_KSU, 'head_branch': 'main', 'conclusion': 'success',
                                             'head_repository': {'full_name': 'untrusted/repo'}}]},
                         {'workflow_runs': [{'head_sha': NEW_KSU, 'head_branch': 'main', 'conclusion': 'success',
                                             'head_repository': {'full_name': None}}]}]:
            with self.subTest(document=document), patch.object(sources, 'github_json', return_value=document):
                with self.assertRaisesRegex(sources.SourceResolutionError, 'Custom'):
                    self.resolve(tier='Custom(自定义)', custom_ref='main:1')

    def test_invalid_inputs_and_empty_custom_do_not_use_network(self):
        with patch.object(sources, 'github_json') as network:
            for changes in ({'variant': 'other/repo'}, {'tier': 'main\n'},
                            {'tier': 'Custom(自定义)', 'custom_ref': ''},
                            *({'custom_ref': ref} for ref in ('main\nINJECT=true', 'main;echo', 'a..b', '-main',
                                                             'main\r', 'main\t', 'a\\b', 'main:0', 'a:1:2'))):
                with self.subTest(changes=changes), self.assertRaises(sources.SourceResolutionError):
                    self.resolve(**changes)
        network.assert_not_called()

    def test_lookup_failure_is_sanitized_and_emits_no_job_outputs(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sources, 'github_json', side_effect=sources.SourceResolutionError('official lookup failed')):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = sources.main(['--root', str(self.root), '--variant', 'Official',
                                       '--tier', 'Dev(开发)', '--susfs', 'false'])
        self.assertEqual(status, 1)
        self.assertFalse(self.output.exists())

    def test_cli_writes_only_public_shared_source_outputs(self):
        with patch.object(sources, 'github_json') as api, redirect_stdout(io.StringIO()):
            api.side_effect = [{'default_branch': 'main'}, None, {'sha': NEW_KSU}]
            status = sources.main(['--root', str(self.root), '--variant', 'SukiSU',
                                   '--tier', 'Dev(开发)', '--susfs', 'true'])
        self.assertEqual(status, 0)
        self.assertEqual(self.output.read_text(encoding='utf-8'),
                         f'ksu_ref={NEW_KSU}\nksu_repo={REPO}\nksu_short={NEW_KSU[:7]}\n'
                         'ksu_source_branch=main\neffective_ksu_branch=Dev(开发)\n')


class GitHubSourceLookupTests(unittest.TestCase):
    def api_response(self, sha=NEW_KSU):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.geturl.return_value = f'https://api.github.com/repos/{REPO}/commits/feature%2Fmanager'
        response.read.return_value = json.dumps({'sha': sha}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        return response, opener

    def test_fixed_https_api_url_encodes_ref_and_uses_optional_token(self):
        _, opener = self.api_response(NEW_KSU.upper())
        with patch.dict(os.environ, {'GH_TOKEN': 'private-token', 'GITHUB_TOKEN': 'other-token'}):
            with patch.object(sources, 'build_opener', return_value=opener) as build:
                self.assertEqual(sources.github_commit(REPO, 'feature/manager'), NEW_KSU)
        request, = opener.open.call_args.args
        self.assertEqual(request.full_url, f'https://api.github.com/repos/{REPO}/commits/feature%2Fmanager')
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-token')
        self.assertIsInstance(build.call_args.args[0], sources.NoRedirect)

    def test_no_token_means_no_authorization_header(self):
        _, opener = self.api_response()
        with patch.dict(os.environ, {'GH_TOKEN': '', 'GITHUB_TOKEN': ''}):
            with patch.object(sources, 'build_opener', return_value=opener):
                sources.github_commit(REPO, 'feature/manager')
        self.assertIsNone(opener.open.call_args.args[0].get_header('Authorization'))

    def test_redirect_handler_rejects_every_destination(self):
        with self.assertRaisesRegex(sources.SourceResolutionError, 'redirects'):
            sources.NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://example.com/steal')

    def test_unexpected_response_urls_are_rejected_before_reading(self):
        for url in ('http://api.github.com/ref', 'https://api.github.com.evil.test/ref',
                    'https://user@api.github.com/ref', 'https://api.github.com/elsewhere'):
            response, opener = self.api_response()
            response.geturl.return_value = url
            with self.subTest(url=url), patch.object(sources, 'build_opener', return_value=opener):
                with self.assertRaisesRegex(sources.SourceResolutionError, 'unexpected URL'):
                    sources.github_commit(REPO, 'feature/manager')
            response.read.assert_not_called()

    def test_invalid_api_results_and_errors_do_not_expose_token(self):
        for document in ({'sha': 'main'}, {'sha': NEW_KSU + '\n'}, {'sha': None}, [], {}):
            response, opener = self.api_response()
            response.read.return_value = json.dumps(document).encode()
            with self.subTest(document=document), patch.object(sources, 'build_opener', return_value=opener):
                with self.assertRaises(sources.SourceResolutionError):
                    sources.github_commit(REPO, 'feature/manager')
        with patch.object(sources, 'build_opener') as build:
            build.return_value.open.side_effect = HTTPError('https://secret-url', 403, 'private-token', {}, None)
            with self.assertRaises(sources.SourceResolutionError) as raised:
                sources.github_commit(REPO, 'feature/manager')
        self.assertNotIn('private-token', str(raised.exception))
        self.assertNotIn('secret-url', str(raised.exception))

    def test_unallowlisted_repository_and_bad_auth_never_open_network(self):
        with patch.object(sources, 'build_opener') as build:
            with self.assertRaises(sources.SourceResolutionError):
                sources.github_commit('attacker/KernelSU', 'main')
            with patch.dict(os.environ, {'GH_TOKEN': 'token\nInjected: header'}):
                with self.assertRaises(sources.SourceResolutionError):
                    sources.github_commit(REPO, 'main')
        build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
