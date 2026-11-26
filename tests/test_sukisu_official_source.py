"""Exercise the official host migration across resolver, prebuild and notices."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import resolve_build_sources as sources
import sukisu_official_source as official
from manager_prebuild import discover_channels

MAIN = 'a' * 40
DEV = 'b' * 40
STABLE = 'c' * 40
REPO = sources.REPOSITORIES['SukiSU']


class OfficialSukiSourceTests(unittest.TestCase):
    def response(self, resource, body):
        result = MagicMock()
        result.__enter__.return_value = result
        result.status = 200
        result.geturl.return_value = official.API + resource
        result.read.return_value = json.dumps(body).encode()
        return result

    def test_new_official_api_receives_no_github_credentials(self):
        response = self.response('/git/commits/main', {'sha': MAIN})
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {'GH_TOKEN': 'private', 'GITHUB_TOKEN': 'also-private'}), \
                patch.object(official, 'build_opener', return_value=opener):
            self.assertEqual(MAIN, sources.github_commit(REPO, 'main'))
        request = opener.open.call_args.args[0]
        self.assertEqual(official.API + '/git/commits/main', request.full_url)
        self.assertIsNone(request.get_header('Authorization'))

    def test_main_dev_and_formal_channels_use_the_new_official_sources(self):
        def api(resource, **kwargs):
            values = {'': {'id': 3, 'full_name': 'shirkneko/SukiSU', 'default_branch': 'main'},
                      '/git/commits/main': {'sha': MAIN},
                      '/branches/dev': {'name': 'dev', 'commit': {'id': DEV}},
                      '/compare/' + MAIN + '...' + DEV + '?limit=1&page=2': {'total_commits': 1, 'commits': []},
                      '/compare/' + DEV + '...' + MAIN + '?limit=1&page=2': {'total_commits': 44, 'commits': []},
                      '/releases/latest': {'tag_name': 'v3.0.0-r1', 'draft': False, 'prerelease': False},
                      '/git/commits/v3.0.0-r1': {'sha': STABLE}}
            return values[resource]
        with patch.object(official, '_read', side_effect=api):
            self.assertEqual((MAIN, 'main'), sources.official_selection(REPO, 'Latest(最新)', ''))
            self.assertEqual((STABLE, 'v3.0.0-r1'), sources.official_selection(REPO, 'Stable(标准)', ''))
            with patch('manager_prebuild.MANAGERS', ('SukiSU',)):
                channels = discover_channels()
            self.assertEqual({'Stable(标准)': (STABLE, 'v3.0.0-r1'),
                              'Latest(最新)': (MAIN, 'main')}, channels['SukiSU'])

    def test_newer_dev_remains_selectable_after_divergence(self):
        with patch.object(official, '_read', side_effect=[{'total_commits': 45, 'commits': []},
                                                       {'total_commits': 2, 'commits': []}]):
            self.assertEqual((DEV, 'dev'), sources.compare_sukisu_channels(REPO, 'main', MAIN, DEV))

    def test_source_outage_fails_without_using_the_stale_github_archive(self):
        opener = MagicMock()
        opener.open.side_effect = HTTPError(official.API, 503, 'Unavailable', {}, None)
        with patch.object(official, 'build_opener', return_value=opener), \
                patch.object(sources, 'build_opener') as github:
            with self.assertRaises(sources.SourceResolutionError):
                sources.official_selection(REPO, 'Latest(最新)', '')
        github.assert_not_called()

    def test_redirects_bad_identity_and_invalid_comparison_are_rejected(self):
        response = self.response('', {'id': 3, 'full_name': 'other/SukiSU'})
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(official, 'build_opener', return_value=opener):
            with self.assertRaises(sources.SourceResolutionError):
                official.official_json()
            response.geturl.return_value = 'https://unexpected.example/api'
            with self.assertRaises(sources.SourceResolutionError):
                official.official_json()
        with patch.object(official, '_read', return_value={'total_commits': True, 'commits': []}):
            with self.assertRaises(sources.SourceResolutionError):
                official.official_json('/compare/' + MAIN + '...' + DEV)


if __name__ == '__main__':
    unittest.main()
