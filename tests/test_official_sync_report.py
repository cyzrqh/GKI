import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import official_sync_report as reports
from check_official_updates import head_state

BASE = 'a' * 40
NEW = 'b' * 40


def snapshot():
    return {'schema_version': 1,
            'sukisu': {'branch': 'main', 'head': BASE, 'release': {
                'tag': 'v4.2.1-r1', 'commit': BASE, 'newer_than_baseline': False},
                'development': {'branch': 'dev', 'head': 'c' * 40}},
            'bakasu': {'branch': 'main', 'head': 'd' * 40, 'release': None,
                       'prerelease': {'tag': 'v4.2.0-rc3', 'commit': 'e' * 40}},
            'susfs': {target: 'f' * 40 for target in reports.TARGETS}}


def record(source):
    return {'schema_version': 1, 'sources': source, 'acknowledged_heads': head_state(source)}


def entry(kernel='6.1.75', date='2024-05', ref=BASE):
    return {'android_version': 'android14', 'kernel_version': '6.1', 'kernel': kernel,
            'date': date, 'ref': ref, 'revision': 'r1'}


class OfficialSyncReportTests(unittest.TestCase):
    def setUp(self):
        self.before = record(snapshot())
        self.after = copy.deepcopy(self.before)
        self.catalog = {'schema_version': 1, 'entries': [entry()]}
        self.profile = {'schema_version': 1, 'sukisu_susfs': {
            'source_ref': '1' * 40, 'version': 'v2.3.0',
            'pins': {target: '2' * 40 for target in reports.TARGETS}}}

    def report(self, *, old=None, new=None, old_catalog=None, new_catalog=None, old_profile=None, new_profile=None, checks=None):
        return reports.build_report(self.before if old is None else old, self.after if new is None else new,
                                    self.catalog if old_catalog is None else old_catalog,
                                    self.catalog if new_catalog is None else new_catalog,
                                    self.profile if old_profile is None else old_profile,
                                    self.profile if new_profile is None else new_profile,
                                    base_ref=BASE, checks=checks)

    def refresh(self):
        self.after['acknowledged_heads'] = head_state(self.after['sources'])

    def test_identical_data_has_no_changes_or_spurious_old_baseline_updates(self):
        report = self.report()
        self.assertFalse(report['has_changes'])
        self.assertEqual([], report['source_changes'])
        self.assertEqual([], report['catalog']['selected_changes'])
        self.assertFalse(report['compatibility']['changed'])
        self.assertEqual(report['digest'], self.report()['digest'])
        self.assertIn('官方源码观察记录无变化', report['summary_markdown'])

    def test_unrelated_repository_commit_does_not_change_notification_digest(self):
        self.after['sources']['sukisu']['head'] = NEW
        self.refresh()
        first = self.report()
        second = reports.build_report(self.before, self.after, self.catalog, self.catalog,
                                      self.profile, self.profile, base_ref='3' * 40)
        self.assertNotEqual(first['base_ref'], second['base_ref'])
        self.assertEqual(first['digest'], second['digest'])
        self.after['sources']['sukisu']['head'] = '4' * 40
        self.refresh()
        self.assertNotEqual(first['digest'], self.report()['digest'])

    def test_compares_last_observation_not_ancient_compatibility_baseline(self):
        self.after['sources']['sukisu']['head'] = NEW
        self.refresh()
        report = self.report()
        self.assertEqual([{'source': 'SukiSU', 'channel': '默认分支',
                           'before': {'ref': 'main', 'commit': BASE},
                           'after': {'ref': 'main', 'commit': NEW}, 'kind': 'changed'}], report['source_changes'])
        self.assertEqual('1' * 40, report['compatibility']['after']['sukisu_source_ref'])
        self.assertNotEqual(self.report(new=self.before)['digest'], report['digest'])
        self.assertFalse(report['build_performed'])

    def test_all_manager_channels_and_five_susfs_targets_are_recorded_exactly(self):
        source = self.after['sources']
        source['sukisu']['development']['head'] = NEW
        source['sukisu']['release'] = {'tag': 'v4.2.1-r2', 'commit': NEW, 'newer_than_baseline': True}
        source['bakasu']['head'] = NEW
        source['bakasu']['release'] = {'tag': 'v4.2.0', 'commit': NEW}
        source['bakasu']['prerelease'] = {'tag': 'v4.2.1-rc1', 'commit': NEW}
        source['susfs'] = {target: NEW for target in reports.TARGETS}
        self.refresh()
        report = self.report()
        self.assertEqual(10, len(report['source_changes']))
        self.assertEqual({'SukiSU', 'BakaSU', 'SUSFS'}, {item['source'] for item in report['source_changes']})
        self.assertIn('v4.2.1-r1', report['summary_markdown'])
        self.assertIn('v4.2.1-r2', report['summary_markdown'])
        self.assertIn('v4.2.0-rc3', report['summary_markdown'])
        self.assertIn('v4.2.1-rc1', report['summary_markdown'])
        self.assertIn('已验证的 SUSFS 配对及适配基线保持不变', report['summary_markdown'])

    def test_initial_baka_observation_is_not_claimed_to_be_a_new_formal_release(self):
        del self.before['sources']['bakasu']
        self.before['acknowledged_heads'] = head_state(self.before['sources'])
        report = self.report()
        self.assertEqual({'默认分支', '预发行版'}, {item['channel'] for item in report['source_changes']})
        self.assertTrue(all(item['kind'] == 'first-observation' and item['before'] is None for item in report['source_changes']))
        self.assertIn('此前未记录', report['summary_markdown'])

    def test_first_snapshot_is_supported_and_missing_channel_is_not_claimed_deleted(self):
        report = reports.build_report(None, self.after, self.catalog, self.catalog,
                                     self.profile, self.profile, base_ref=BASE)
        self.assertTrue(report['has_changes'])
        self.assertTrue(all(change['kind'] == 'first-observation' for change in report['source_changes']))
        del self.after['sources']['sukisu']['development']
        self.refresh()
        item = self.report()['source_changes'][0]
        self.assertEqual('not-observed', item['kind'])
        self.assertIsNone(item['after'])

    def test_release_with_same_commit_and_new_tag_still_has_a_version_change(self):
        self.after['sources']['sukisu']['release']['tag'] = 'v4.2.1-r2'
        self.refresh()
        report = self.report()
        self.assertEqual(1, len(report['source_changes']))
        self.assertEqual('v4.2.1-r2', report['source_changes'][0]['after']['version'])
        self.assertEqual('not-observed', report['manager_version_codes']['status'])
        self.assertIn('以该次实际 APK 元数据为准', report['summary_markdown'])

    def test_catalog_lists_exact_additions_removals_and_changed_month_selection(self):
        old = {'schema_version': 1, 'entries': [entry(date='2024-04'), entry()]}
        new = {'schema_version': 1, 'entries': [entry(), entry(date='2024-06'), entry('6.1.118', '2024-08')]}
        report = self.report(old_catalog=old, new_catalog=new)
        catalog = report['catalog']
        self.assertEqual(['6.1.75', '6.1.118'], [item['kernel'] for item in catalog['added']])
        self.assertEqual(['2024-04'], [item['date'] for item in catalog['removed']])
        self.assertEqual((2, 3, 1, 2), tuple(catalog[key] for key in ('before_count','after_count','selected_before_count','selected_after_count')))
        self.assertEqual(('2024-05', '2024-06'), tuple(catalog['selected_changes'][0][side]['date'] for side in ('before', 'after')))
        self.assertIn('新增 2 条，移除 1 条', report['summary_markdown'])

    def test_existing_catalog_source_and_revision_changes_are_not_hidden_as_no_update(self):
        new = copy.deepcopy(self.catalog)
        new['entries'][0].update(ref=NEW, revision='r2')
        report = self.report(new_catalog=new)
        self.assertEqual([], report['catalog']['added'])
        self.assertEqual([], report['catalog']['removed'])
        self.assertEqual(1, len(report['catalog']['updated']))
        self.assertTrue(report['has_changes'])
        self.assertIn(NEW, report['summary_markdown'])
        self.assertIn('r2', report['summary_markdown'])

    def test_changed_compatibility_is_explicit_and_never_described_as_verified(self):
        new = copy.deepcopy(self.profile)
        new['sukisu_susfs']['pins']['android14-6.1'] = NEW
        report = self.report(new_profile=new)
        self.assertTrue(report['compatibility']['changed'])
        self.assertIn('不能视为已验证适配成功', report['summary_markdown'])

    def test_partial_failure_keeps_unknown_distinct_from_no_update(self):
        report = self.report(checks={'official':'failure','catalog':'success'})
        self.assertIn('未完成（failure），不能确认有无更新', report['summary_markdown'])
        self.assertIn('官方源码检查未完成；沿用原记录', report['summary_markdown'])
        self.assertNotIn('官方源码观察记录无变化', report['summary_markdown'])
        self.after['sources']['bakasu']['head'] = NEW
        self.refresh()
        with self.assertRaisesRegex(reports.ReportError, 'Unchecked data'):
            self.report(checks={'official':'failure','catalog':'success'})

    def test_strict_json_duplicates_nonfinite_metadata_and_duplicate_catalog_rejected(self):
        for value in (b'{"schema_version":1,"schema_version":1}', b'{"v":NaN}', b'\xff', b'x'*(reports.MAX_FILE+1)):
            with self.subTest(value=value[:40]), self.assertRaises(reports.ReportError):
                reports.parse_document(value)
        self.after['acknowledged_heads']['sukisu_head'] = NEW
        with self.assertRaisesRegex(reports.ReportError, 'acknowledgement'):
            self.report()
        with self.assertRaisesRegex(reports.ReportError, 'duplicate target'):
            self.report(new=self.before, new_catalog={'schema_version':1,'entries':[entry(),entry()]})

    def test_table_rendering_escapes_markup_mentions_and_nonstandard_refs(self):
        self.assertEqual('`&#64;owner&#124;&lt;x&gt;&#96;`', reports.code('@owner|<x>`'))
        for ref in ('main', '../HEAD', '-c', BASE+'\n'):
            with self.subTest(ref=ref), self.assertRaises(reports.ReportError):
                reports.repository_report(ROOT, ref)


class OfficialSyncReportGitTests(unittest.TestCase):
    def setUp(self):
        OfficialSyncReportTests.setUp(self)
        self.root = ROOT / 'tests' / ('.sync-report-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.remove_fixture)
        self.git('init','-q')
        self.git('config','user.email','local-report-tests@example.invalid')
        self.git('config','user.name','Local report test')
        self.git('config','core.autocrlf','false')
        self.write(reports.FILES[0], self.before)
        self.write(reports.FILES[1], self.catalog)
        self.write(reports.FILES[2], self.profile)
        self.git('add','--',*reports.FILES)
        self.git('commit','-qm','Local report fixture')
        self.base = self.git('rev-parse','HEAD').strip()

    refresh = OfficialSyncReportTests.refresh

    def remove_fixture(self):
        def remove_readonly(function, path, error):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)
        shutil.rmtree(self.root, onexc=remove_readonly)

    def git(self, *args):
        result = subprocess.run(['git','-C',str(self.root),*args], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(0,result.returncode,result.stderr)
        return result.stdout

    def write(self,path,document):
        target = self.root/path
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(document),encoding='utf-8')

    def test_git_baseline_is_fixed_and_current_worktree_delta_is_read_without_modification(self):
        self.after['sources']['sukisu']['head'] = NEW
        self.refresh()
        self.write(reports.FILES[0],self.after)
        status = self.git('status','--porcelain')
        report = reports.repository_report(self.root,self.base)
        self.assertEqual(self.base,report['base_ref'])
        self.assertEqual(BASE,report['source_changes'][0]['before']['commit'])
        self.assertEqual(NEW,report['source_changes'][0]['after']['commit'])
        self.assertEqual(status,self.git('status','--porcelain'))
        self.assertEqual(self.base,self.git('rev-parse','HEAD').strip())

    def test_cli_writes_structured_report_and_summary_with_digest_outputs(self):
        output = self.root/'github-output.txt'
        report = self.root/'out/report.json'
        markdown = self.root/'out/report.md'
        with patch.dict(os.environ,{'GITHUB_OUTPUT':str(output)}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = reports.main(['--root',str(self.root),'--base-ref',self.base,
                                   '--report-json',str(report),'--report-markdown',str(markdown)])
        self.assertEqual(0,result)
        data = json.loads(report.read_text('utf-8'))
        self.assertEqual(data['summary_markdown'],markdown.read_text('utf-8'))
        self.assertIn('report_digest='+data['digest'],output.read_text('utf-8'))
        self.assertEqual('false',dict(line.split('=',1) for line in output.read_text('utf-8').splitlines())['report_has_changes'])

    def test_missing_previous_candidate_is_legitimate_but_symlink_data_is_not(self):
        self.git('rm','--',reports.FILES[0])
        self.git('commit','-qm','No previous candidate fixture')
        self.write(reports.FILES[0],self.after)
        self.assertTrue(reports.repository_report(self.root)['has_changes'])
        target = self.root/reports.FILES[1]
        target.unlink()
        try:
            target.symlink_to(self.root/reports.FILES[2])
        except OSError:
            self.skipTest('This Windows account cannot create filesystem symlinks')
        with self.assertRaisesRegex(reports.ReportError,'filesystem links'):
            reports.repository_report(self.root)


if __name__ == '__main__':
    unittest.main()
