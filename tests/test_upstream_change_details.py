import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import official_sync_report as reports
import upstream_change_details as details
from check_official_updates import head_state
from test_official_sync_report import snapshot, record, entry, version_observations

OLD, NEW = 'a' * 40, 'b' * 40


def change(source='SukiSU', channel='正式版', before=OLD, after=NEW):
    return {'source': source, 'channel': channel, 'kind': 'changed',
            'before': {'ref': 'old', 'commit': before}, 'after': {'ref': 'new', 'commit': after}}


def comparison(**changes):
    return {'base_commit': {'sha': OLD}, 'status': 'ahead', 'total_commits': 1,
            'commits': [{'sha': NEW, 'commit': {'message': 'fix: upstream change\n\nbody'}}], **changes}


class CommitDetailsTests(unittest.TestCase):
    def test_official_urls_cover_all_six_managers_and_susfs(self):
        expected = {'SukiSU': 'SukiSU-Ultra/SukiSU-Ultra', 'BakaSU': 'Baka-SU/BakaSU',
                    'KernelSU-Next': 'KernelSU-Next/KernelSU-Next', 'KernelSU': 'tiann/KernelSU',
                    'KowSU': 'KOWX712/KernelSU', 'FinaSU': 'LyraVoid/FinaSU'}
        for name, repo in expected.items():
            self.assertEqual('https://github.com/' + repo, details.source_url(name))
        self.assertEqual('https://gitlab.com/simonpunk/susfs4ksu', details.source_url('SUSFS'))
        for name in ('other', 'https://evil.invalid', 'SukiSU\n'):
            with self.assertRaises(ValueError):
                details.source_url(name)

    def test_compare_is_immutable_and_token_only_goes_to_github(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'private-test-token'}):
            github = details.comparison_request('SukiSU', OLD, NEW)
            gitlab = details.comparison_request('SUSFS', OLD, NEW)
        self.assertIn('/compare/' + OLD + '...' + NEW, github.full_url)
        self.assertEqual('Bearer private-test-token', github.get_header('Authorization'))
        self.assertNotIn('Authorization', gitlab.headers)
        self.assertIn('from=' + OLD + '&to=' + NEW + '&straight=true', gitlab.full_url)
        for ref in ('main', 'b' * 39, NEW + '?token=secret', NEW + '\n'):
            with self.assertRaises(ValueError):
                details.comparison_request('SukiSU', OLD, ref)

    def test_same_range_across_channels_is_only_fetched_once(self):
        requests = []
        def fetch(request):
            requests.append(request.full_url)
            return comparison()
        result = details.observe_changes([change(), change(channel='默认分支')], fetch=fetch)
        self.assertEqual(1, len(requests))
        self.assertEqual(['正式版', '默认分支'], result[0]['channels'])
        self.assertEqual('fix: upstream change', result[0]['commits'][0]['title'])

    def test_first_observation_same_commit_or_unavailable_never_fetch(self):
        rows = [change(before=NEW), {**change(), 'kind': 'first-observation'},
                {**change(), 'before': None}, {**change(), 'after': {'available': False}}]
        def unexpected(request):
            self.fail('No comparison is possible for these observations')
        self.assertEqual([], details.observe_changes(rows, fetch=unexpected))

    def test_outage_keeps_frozen_range_without_server_error_or_secret(self):
        def unavailable(request):
            raise URLError('https://user:fake-secret@invalid')
        result = details.observe_changes([change()], fetch=unavailable)
        self.assertEqual('unavailable', result[0]['status'])
        self.assertEqual(OLD, result[0]['before'])
        self.assertNotIn('fake-secret', json.dumps(result))

    def test_response_cannot_replace_fixed_base(self):
        for document in (None, {}, comparison(base_commit={'sha': NEW}),
                         comparison(status='unknown'), comparison(total_commits=True),
                         comparison(commits=[{'sha': 'main', 'commit': {'message': 'bad'}}])):
            with self.subTest(document=document), self.assertRaises(ValueError):
                details.parse_comparison('SukiSU', OLD, NEW, document)

    def test_gitlab_requires_head_and_marks_partial_result(self):
        data = {'commit': {'id': NEW}, 'compare_timeout': True,
                'commits': [{'id': NEW, 'title': 'fix SUSFS'}]}
        result = details.parse_comparison('SUSFS', OLD, NEW, data)
        self.assertIsNone(result['total_commits'])
        self.assertTrue(result['partial'])
        data['commit']['id'] = OLD
        with self.assertRaises(ValueError):
            details.parse_comparison('SUSFS', OLD, NEW, data)

    def test_commit_and_request_count_are_bounded(self):
        data = comparison(total_commits=50, commits=[{'sha': NEW, 'commit': {'message': 'x' * 200}}] * 50)
        result = details.parse_comparison('SukiSU', OLD, NEW, data)
        self.assertEqual(details.MAX_COMMITS, len(result['commits']))
        self.assertEqual(180, len(result['commits'][0]['title']))
        self.assertTrue(result['commits'][0]['title_truncated'])
        rows = [change(after=f'{index:040x}') for index in range(1, 17)]
        seen = []
        def failed(request):
            seen.append(request.full_url)
            raise OSError('offline')
        result = details.observe_changes(rows, fetch=failed)
        self.assertEqual(details.MAX_REQUESTS, len(seen))
        self.assertEqual(4, sum(row['status'] == 'budget-exceeded' for row in result))

    def test_redirects_are_rejected(self):
        with self.assertRaises(ValueError):
            details.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid')


class DetailedReportTests(unittest.TestCase):
    def setUp(self):
        self.before = record(snapshot())
        self.after = copy.deepcopy(self.before)
        self.after['sources']['sukisu']['development']['head'] = NEW
        self.after['acknowledged_heads'] = head_state(self.after['sources'])
        self.old_catalog = {'schema_version': 1, 'entries': [entry()]}
        self.new_catalog = {'schema_version': 1, 'entries': [entry(), entry(date='2024-06'), entry('6.1.118', '2024-08')]}
        self.profile = {'sukisu_susfs': {'source_ref': '1' * 40, 'version': 'v2.3.0',
                                       'pins': {target: '2' * 40 for target in reports.TARGETS}}}

    def report(self):
        return reports.build_report(self.before, self.after, self.old_catalog, self.new_catalog,
                                    self.profile, self.profile, base_ref=OLD,
                                    manager_version_observations=version_observations(self.before, self.after))

    def test_only_changed_manager_channel_has_codes_and_official_links(self):
        body = self.report()['summary_markdown']
        section = body.split('### 本次变化的管理器版本')[1].split('### 官方源码和版本标签')[0]
        self.assertIn('测试版（最新版）', section)
        self.assertNotIn('| SukiSU | 正式版 |', section)
        self.assertNotIn('| BakaSU |', section)
        self.assertIn('`41075`', section)
        self.assertIn('[SukiSU](https://github.com/SukiSU-Ultra/SukiSU-Ultra)', body)
        self.assertIn('/compare/' + 'c' * 40 + '...' + NEW, body)

    def test_kernel_classification_names_exact_origin_and_latest_selection(self):
        report = self.report()
        self.assertEqual(['6.1.118'], [row['kernel'] for row in report['catalog_details']['added_versions']])
        self.assertEqual(['6.1.75'], [row['kernel'] for row in report['catalog_details']['added_months']])
        body = report['summary_markdown']
        self.assertIn('新增完整内核版本', body)
        self.assertIn('既有内核新增补丁月份', body)
        self.assertIn('android14-6.1.75-2024-05', body)
        self.assertIn('android14-6.1.75-2024-06', body)
        self.assertIn('https://android.googlesource.com/kernel/common/+/' + OLD, body)
        self.assertIn('欧加真的平台配方目录不在本次检查范围内', body)

    def test_commit_titles_are_escaped_and_do_not_change_notification_digest(self):
        report = self.report()
        digest = report['digest']
        row = {'source': 'SukiSU', 'before': 'c' * 40, 'after': NEW, 'channels': ['开发分支']}
        data = comparison(base_commit={'sha': 'c' * 40}, commits=[{
            'sha': NEW, 'commit': {'message': 'fix @everyone | <img> `title`'}}])
        report['source_commit_details'] = [{**row, **details.parse_comparison('SukiSU', 'c' * 40, NEW, data)}]
        body = reports.render_report(report)
        self.assertEqual(digest, report['digest'])
        self.assertIn('&#64;everyone &#124; &lt;img&gt; &#96;title&#96;', body)
        self.assertNotIn('@everyone', body)
        self.assertIn('/commit/' + NEW, body)
        report['source_commit_details'] = [{**row, 'status': 'unavailable', 'commits': []}]
        self.assertIn('提交标题明细未能获取', reports.render_report(report))
        self.assertEqual(digest, report['digest'])

    def test_unchanged_compatibility_rows_are_omitted(self):
        body = self.report()['summary_markdown']
        self.assertNotIn('| SUSFS 版本 |', body)
        self.assertNotIn('| SUSFS android14-6.1 |', body)
        self.assertIn('手动同步另行验证', body)

    def test_large_preview_is_utf8_bounded_and_points_to_complete_summary(self):
        body = '## 明细\n\n' + ('| 新增内核 | 内容 |\n' * 10000)
        preview = reports.report_preview(body)
        self.assertLessEqual(len(preview.encode('utf-8')), 45000)
        self.assertIn('完整逐项变化', preview)
        self.assertEqual('short', reports.report_preview('short'))


if __name__ == '__main__':
    unittest.main()
