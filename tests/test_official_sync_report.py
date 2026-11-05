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
from types import SimpleNamespace
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


def susfs_data(profile, *, selected=BASE, candidate=NEW):
    from update_susfs_sources import MANAGERS, POLICY, SOURCE
    profile = copy.deepcopy(profile)
    profile['sukisu_susfs']['pins'] = {target: selected for target in reports.TARGETS}
    pins = {'version': 'v2.3.0', 'managers': {name: {'pins': dict(profile['sukisu_susfs']['pins'])}
                                           for name in MANAGERS[1:]}}
    current = selected == candidate
    receipt = {'schema_version': 1, 'source': SOURCE, 'policy': POLICY,
               'candidates': {target: {'ref': candidate, 'version': 'v2.3.0', 'build_tree_sha256': 'e' * 64}
                              for target in reports.TARGETS},
               'pins': {name: {target: {'selected_ref': selected, 'candidate_ref': candidate,
                                        'selected_build_tree_sha256': ('e' if current else 'd') * 64,
                                        'status': 'current' if current else 'pending_adaptation',
                                        'reason': 'selected' if current else 'build-tree-changed'}
                               for target in reports.TARGETS} for name in MANAGERS}}
    return profile, pins, receipt


def oplus_data():
    from oplus_catalog import RECIPE_REPOSITORIES
    manifest = json.loads((ROOT / 'config/oplus-cctv-profiles.json').read_text(encoding='utf-8'))
    manifest['profiles'] = manifest['profiles'][:1]
    row = manifest['profiles'][0]
    receipt = {'schema_version': 1,
               'recipe_heads': {repository: {'branch': 'main', 'ref': row['recipe_ref']}
                                for repository in RECIPE_REPOSITORIES},
               'profiles': [{'id': row['id'], 'source_ref': row['kernel_ref'],
                             'source_date': row['source_committed_at'], 'source_version': row['kernel'],
                             'recipe_ref': row['recipe_ref'], 'recipe_sha256': row['recipe_sha256'],
                             'recipe_status': 'unchanged'}], 'new_recipes': []}
    return manifest, receipt


def entry(kernel='6.1.75', date='2024-05', ref=BASE):
    return {'android_version': 'android14', 'kernel_version': '6.1', 'kernel': kernel,
            'date': date, 'ref': ref, 'revision': 'r1'}


def version_observations(before, after):
    result = {'status': 'observed', 'before': [], 'after': []}
    for side, document in (('before', before), ('after', after)):
        if document is None:
            continue
        source = document['sources']
        for key, name, code in (('sukisu', 'SukiSU', 41074), ('bakasu', 'BakaSU', 35252)):
            if key not in source:
                continue
            item = source[key]
            release = item['release']
            result[side].append({'source': name, 'channel': '正式版',
                                 'ref': release['tag'] if release else None,
                                 'commit': release['commit'] if release else None,
                                 'version_code': code if release else None,
                                 'status': 'observed' if release else 'unavailable'})
            selected = item.get('development') or {'branch': item['branch'], 'head': item['head']}
            result[side].append({'source': name, 'channel': '测试版', 'ref': selected['branch'],
                                 'commit': selected['head'], 'version_code': code + 1, 'status': 'observed'})
    return result


class OfficialSyncReportTests(unittest.TestCase):
    def setUp(self):
        self.before = record(snapshot())
        self.after = copy.deepcopy(self.before)
        self.catalog = {'schema_version': 1, 'entries': [entry()]}
        self.profile = {'schema_version': 1, 'sukisu_susfs': {
            'source_ref': '1' * 40, 'version': 'v2.3.0',
            'pins': {target: '2' * 40 for target in reports.TARGETS}}}

    def report(self, *, old=None, new=None, old_catalog=None, new_catalog=None, old_profile=None, new_profile=None, checks=None,
               versions=None, **extra):
        return reports.build_report(self.before if old is None else old, self.after if new is None else new,
                                    self.catalog if old_catalog is None else old_catalog,
                                    self.catalog if new_catalog is None else new_catalog,
                                    self.profile if old_profile is None else old_profile,
                                    self.profile if new_profile is None else new_profile,
                                    base_ref=BASE, checks=checks, manager_version_observations=versions, **extra)

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
        self.assertIn('[SukiSU](https://github.com/SukiSU-Ultra/SukiSU-Ultra)', report['summary_markdown'])
        self.assertIn('[BakaSU](https://github.com/Baka-SU/BakaSU)', report['summary_markdown'])
        self.assertNotIn('### 官方源码和版本标签', report['summary_markdown'])
        self.assertIn('v4.2.1-r2', json.dumps(report['source_changes']))
        self.assertIn('手动同步另行验证所选管理器与 SUSFS/KPM 的配套', report['summary_markdown'])

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

    def test_only_changed_test_tier_shows_its_exact_numeric_version_delta(self):
        self.after['sources']['sukisu']['development']['head'] = NEW
        self.refresh()
        versions = version_observations(self.before, self.after)
        versions['before'][1]['version_code'] = 41074
        versions['after'][1]['version_code'] = 41075
        report = self.report(versions=versions)
        body = report['summary_markdown']
        section = body.split('### 本次变化的管理器版本', 1)[1].split('### 发生变化的官方上游', 1)[0]
        self.assertIn('| SukiSU | 测试版 | `41074` → `41075` |', section)
        self.assertNotIn('| SukiSU | 正式版', section)
        self.assertNotIn('| BakaSU |', section)
        self.assertIn('本次检查没有编译 APK', body)
        self.assertFalse(report['build_performed'])

    def test_only_changed_formal_tier_is_shown_even_when_its_numeric_code_is_equal(self):
        self.after['sources']['sukisu']['release']['tag'] = 'v4.2.1-r2'
        self.refresh()
        report = self.report(versions=version_observations(self.before, self.after))
        section = report['summary_markdown'].split('### 本次变化的管理器版本', 1)[1].split('### 发生变化的官方上游', 1)[0]
        self.assertIn('| SukiSU | 正式版 | `41074` → `41074` |', section)
        self.assertNotIn('| SukiSU | 测试版', section)
        self.assertNotIn('| BakaSU |', section)

    def test_default_branch_change_does_not_claim_a_test_update_when_dev_is_still_selected(self):
        self.after['sources']['sukisu']['head'] = NEW
        self.refresh()
        report = self.report(versions=version_observations(self.before, self.after))
        self.assertTrue(report['has_changes'])
        self.assertNotIn('### 本次变化的管理器版本', report['summary_markdown'])
        self.assertIn('本次无管理器正式版或测试版的版本变化', report['summary_markdown'])

    def test_changed_selected_commit_can_report_an_unknown_code_without_guessing(self):
        self.after['sources']['sukisu']['development']['head'] = NEW
        self.refresh()
        versions = version_observations(self.before, self.after)
        versions['status'] = 'partial'
        versions['after'][1].update(status='unknown', version_code=None)
        report = self.report(versions=versions)
        self.assertIn('| SukiSU | 测试版 | `41075` → 未能确认版本号 |', report['summary_markdown'])
        self.assertNotIn('`41076`', report['summary_markdown'])

    def test_unresolved_latest_selection_cannot_claim_changed_or_unchanged_test_version(self):
        versions = version_observations(self.before, self.after)
        versions['status'] = 'partial'
        versions['after'][1].update(status='unknown', ref=None, commit=None, version_code=None)
        body = self.report(versions=versions)['summary_markdown']
        self.assertNotIn('### 本次变化的管理器版本', body)
        self.assertNotIn('本次无管理器正式版或测试版的版本变化', body)
        self.assertIn('实际源码选择未能确认', body)

    def test_baka_new_formal_release_is_not_confused_with_existing_prerelease(self):
        self.after['sources']['bakasu']['release'] = {'tag': 'v4.2.0', 'commit': NEW}
        self.refresh()
        report = self.report(versions=version_observations(self.before, self.after))
        self.assertIn('| BakaSU | 正式版 | 官方尚未提供正式版 → `35252` |', report['summary_markdown'])
        self.assertNotIn('| BakaSU | 测试版 |', report['summary_markdown'])

    def test_informational_version_availability_never_changes_notification_identity(self):
        self.after['sources']['sukisu']['development']['head'] = NEW
        self.refresh()
        baseline = self.report()
        versions = version_observations(self.before, self.after)
        observed = self.report(versions=versions)
        versions['status'] = 'partial'
        versions['after'][1].update(status='unknown', version_code=None)
        partial = self.report(versions=versions)
        self.assertEqual(baseline['digest'], observed['digest'])
        self.assertEqual(observed['digest'], partial['digest'])
        self.assertEqual(baseline['has_changes'], partial['has_changes'])

    def test_unrelated_version_observation_does_not_create_a_source_update(self):
        versions = version_observations(self.before, self.after)
        observed = self.report(versions=versions)
        self.assertFalse(observed['has_changes'])
        self.assertEqual(self.report()['digest'], observed['digest'])
        self.assertNotIn('### 本次变化的管理器版本', observed['summary_markdown'])

    def test_version_observations_reject_wrong_source_channel_commit_or_number(self):
        changes = [{'source': 'Other'}, {'channel': '预发行版'}, {'commit': NEW},
                   {'ref': 'unrelated'}, {'version_code': True}, {'version_code': '41074'},
                   {'version_code': 0}, {'version_code': 2_100_000_001},
                   {'status': 'unavailable', 'ref': None, 'commit': None, 'version_code': None},
                   {'status': 'unknown', 'version_code': 41074}]
        for change in changes:
            with self.subTest(change=change):
                versions = version_observations(self.before, self.after)
                versions['after'][0].update(change)
                with self.assertRaises(reports.ReportError):
                    self.report(versions=versions)
        versions = version_observations(self.before, self.after)
        versions['after'][1] = copy.deepcopy(versions['after'][0])
        with self.assertRaisesRegex(reports.ReportError, 'duplicate'):
            self.report(versions=versions)
        versions = version_observations(self.before, self.after)
        versions['after'].pop()
        with self.assertRaisesRegex(reports.ReportError, 'cover'):
            self.report(versions=versions)

    def test_missing_previous_manager_observation_is_not_invented_as_a_formal_release(self):
        del self.before['sources']['bakasu']
        self.before['acknowledged_heads'] = head_state(self.before['sources'])
        versions = version_observations(self.before, self.after)
        body = self.report(versions=versions)['summary_markdown']
        self.assertNotIn('| BakaSU | 正式版 |', body)
        self.assertIn('| BakaSU | 测试版 | 此前未记录／本次未观测 → `35253` |', body)

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

    def test_new_check_states_remain_optional_but_unknown_checks_are_rejected(self):
        report = self.report(checks={'official': 'success', 'catalog': 'success', 'susfs': 'failure', 'oplus': 'skipped'})
        self.assertIn('| SUSFS 官方源与构建引用 | 未完成（failure）', report['summary_markdown'])
        self.assertIn('| 欧加真内核与平台配方 | 未完成（skipped）', report['summary_markdown'])
        self.assertNotIn('不在本次检查范围', report['summary_markdown'])
        self.assertFalse(report['has_changes'])
        for checks in ({'official': 'success', 'catalog': 'success', 'other': 'success'},
                       {'official': 'success', 'catalog': 'success', 'susfs': 'success'}):
            with self.subTest(checks=checks), self.assertRaises(reports.ReportError):
                self.report(checks=checks)

    def test_susfs_candidate_only_update_explicitly_preserves_the_actual_build_pins(self):
        profile, pins, receipt = susfs_data(self.profile)
        report = self.report(old_profile=profile, new_profile=profile,
                             checks={'official': 'success', 'catalog': 'success', 'susfs': 'success'},
                             before_manager_pins=pins, after_manager_pins=pins,
                             after_susfs_receipt=receipt)
        self.assertTrue(report['has_changes'])
        self.assertFalse(report['compatibility']['changed'])
        section = report['summary_markdown'].split('### SUSFS 官方源与实际构建引用', 1)[1].split('### 构建适配', 1)[0]
        self.assertIn('待适配；保留适配引用', section)
        self.assertNotIn('实际构建引用已更新', section)
        self.assertEqual({BASE}, {row['effective_after'] for row in report['susfs']['pins']})
        self.assertFalse(report['build_performed'])

    def test_susfs_effective_pin_change_is_separate_from_the_official_candidate(self):
        old_profile, old_pins, previous = susfs_data(self.profile, candidate=BASE)
        new_profile, new_pins, current = susfs_data(self.profile, selected=NEW)
        report = self.report(old_profile=old_profile, new_profile=new_profile,
                             checks={'official': 'success', 'catalog': 'success', 'susfs': 'success'},
                             before_manager_pins=old_pins, after_manager_pins=new_pins,
                             before_susfs_receipt=previous, after_susfs_receipt=current)
        self.assertEqual(5, len(report['susfs']['candidates']))
        self.assertEqual(20, len(report['susfs']['pins']))
        self.assertIn('实际构建引用已更新（构建内容未变）', report['summary_markdown'])
        current['pins']['KowSU']['android14-6.1']['selected_ref'] = BASE
        current['pins']['KowSU']['android14-6.1'].update(status='pending_adaptation', reason='build-tree-changed')
        with self.assertRaisesRegex(ValueError, 'effective compatibility pins'):
            self.report(old_profile=old_profile, new_profile=new_profile,
                        checks={'official': 'success', 'catalog': 'success', 'susfs': 'success'},
                        before_manager_pins=old_pins, after_manager_pins=new_pins,
                        before_susfs_receipt=previous, after_susfs_receipt=current)

    def test_manual_reviewed_pin_fix_can_refresh_a_stale_before_receipt_without_false_promotion(self):
        profile, pins, stale = susfs_data(self.profile)
        repaired, repaired_pins, current = susfs_data(self.profile, selected=NEW)
        report = self.report(old_profile=repaired, new_profile=repaired,
                             checks={'official': 'success', 'catalog': 'success', 'susfs': 'success'},
                             before_manager_pins=repaired_pins, after_manager_pins=repaired_pins,
                             before_susfs_receipt=stale, after_susfs_receipt=current)
        self.assertTrue(report['susfs']['previous_receipt_stale'])
        self.assertIn('实际构建配置为准', report['summary_markdown'])
        self.assertNotIn('实际构建引用已更新', report['summary_markdown'])

    def test_failed_susfs_probe_does_not_report_a_stale_receipt_as_no_update(self):
        profile, pins, receipt = susfs_data(self.profile)
        repaired, repaired_pins, _ = susfs_data(self.profile, selected=NEW)
        report = self.report(old_profile=repaired, new_profile=repaired,
                             checks={'official': 'success', 'catalog': 'success', 'susfs': 'failure'},
                             before_manager_pins=repaired_pins, after_manager_pins=repaired_pins,
                             before_susfs_receipt=receipt, after_susfs_receipt=receipt)
        self.assertFalse(report['has_changes'])
        self.assertIn('不能确认官方有无新更新', report['summary_markdown'])
        self.assertNotIn('官方候选和实际构建引用均无变化', report['summary_markdown'])

    def test_oplus_changed_source_and_pending_recipe_have_different_outcomes(self):
        old_config, old_receipt = oplus_data()
        new_config, new_receipt = copy.deepcopy(old_config), copy.deepcopy(old_receipt)
        new_config['profiles'][0]['kernel_ref'] = NEW
        row, active = new_receipt['profiles'][0], new_config['profiles'][0]
        row.update(source_ref=NEW, recipe_ref=NEW, recipe_status='pending-adaptation')
        new_receipt['recipe_heads'][active['recipe_repository']]['ref'] = NEW
        report = self.report(checks={'official': 'success', 'catalog': 'success', 'oplus': 'success'},
                             before_oplus_receipt=old_receipt, after_oplus_receipt=new_receipt,
                             before_oplus_profiles=old_config, after_oplus_profiles=new_config)
        self.assertTrue(report['has_changes'])
        body = report['summary_markdown']
        self.assertIn('### 欧加真内核与平台配方', body)
        self.assertIn('骁龙 · ' + active['kernel'], body)
        self.assertIn('保留原配方', body)
        self.assertNotIn('骁龙 · sm8650', body)
        self.assertNotIn('CCTV', body)
        self.assertNotIn('不在本次检查范围', body)
        self.assertNotEqual(self.report()['digest'], report['digest'])

    def test_new_oplus_full_version_is_a_candidate_until_added_to_the_actual_catalog(self):
        config, receipt = oplus_data()
        active = config['profiles'][0]
        receipt['profiles'][0].update(source_ref=NEW, source_version='6.1.999')
        receipt['new_recipes'] = [{'repository': active['recipe_repository'], 'ref': active['recipe_ref'],
                                  'path': '.github/workflows/fastbuild_6.1.999.yml',
                                  'kernel': '6.1.999', 'family': '骁龙'}]
        report = self.report(checks={'official': 'success', 'catalog': 'success', 'oplus': 'success'},
                             after_oplus_receipt=receipt, before_oplus_profiles=config, after_oplus_profiles=config)
        self.assertEqual((1, 1), (report['oplus']['before_count'], report['oplus']['after_count']))
        self.assertIn('尚未进入构建菜单，不能视为已支持', report['summary_markdown'])
        self.assertIn('待适配，未替换当前内核', report['summary_markdown'])
        self.assertNotIn('新增构建目标', report['summary_markdown'])

    def test_failed_or_unchanged_oplus_probe_is_not_claimed_as_a_successful_update(self):
        config, receipt = oplus_data()
        for status in ('success', 'failure'):
            report = self.report(checks={'official': 'success', 'catalog': 'success', 'oplus': status},
                                 before_oplus_receipt=receipt, after_oplus_receipt=receipt,
                                 before_oplus_profiles=config, after_oplus_profiles=config)
            self.assertFalse(report['has_changes'])
            expected = '均无变化' if status == 'success' else '不能确认欧加真来源有无新更新'
            self.assertIn(expected, report['summary_markdown'])
        receipt['profiles'][0]['source_ref'] = NEW
        with self.assertRaisesRegex(reports.ReportError, 'Unchecked data'):
            self.report(checks={'official': 'success', 'catalog': 'success', 'oplus': 'failure'},
                        after_oplus_receipt=receipt, before_oplus_profiles=config, after_oplus_profiles=config)


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

    def test_cli_binds_both_new_receipts_to_real_before_and_after_build_configuration(self):
        profile, pins, susfs = susfs_data(self.profile)
        config, oplus = oplus_data()
        self.write(reports.FILES[2], profile)
        self.write(reports.EXTRA_FILES['manager_pins'], pins)
        self.write(reports.EXTRA_FILES['oplus_profiles'], config)
        self.git('add', '.')
        self.git('commit', '-qm', 'Real fixed build pins before independent source checks')
        baseline = self.git('rev-parse', 'HEAD').strip()
        self.write(reports.EXTRA_FILES['susfs_receipt'], susfs)
        self.write(reports.EXTRA_FILES['oplus_receipt'], oplus)
        inputs = [self.root / path for path in (*reports.FILES, *reports.EXTRA_FILES.values())]
        before = {path: path.read_bytes() for path in inputs}
        output, markdown = self.root / 'report.json', self.root / 'report.md'
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = reports.main(['--root', str(self.root), '--base-ref', baseline,
                                   '--report-json', str(output), '--report-markdown', str(markdown),
                                   '--susfs-result', 'success', '--oplus-result', 'success'])
        self.assertEqual(0, result)
        report = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual({'official': 'success', 'catalog': 'success', 'susfs': 'success', 'oplus': 'success'},
                         report['checks'])
        self.assertTrue(report['susfs']['first_observation'])
        self.assertTrue(report['oplus']['first_observation'])
        self.assertFalse(report['compatibility']['changed'])
        self.assertEqual(before, {path: path.read_bytes() for path in inputs})
        self.assertIn('待适配；保留适配引用', report['summary_markdown'])
        self.assertNotIn('不在本次检查范围', report['summary_markdown'])
        self.assertFalse(report['build_performed'])

    def test_cli_opt_in_observes_only_the_fixed_before_and_after_snapshots(self):
        self.after['sources']['sukisu']['development']['head'] = NEW
        self.refresh()
        self.write(reports.FILES[0], self.after)
        versions = version_observations(self.before, self.after)
        observer = SimpleNamespace(observe_versions=lambda old, new, cache: self.observe(old, new, cache, versions))
        report, markdown, cache = self.root/'report.json', self.root/'report.md', self.root/'source-cache'
        self.observed = []
        with patch.dict(sys.modules, {'official_manager_versions': observer}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = reports.main(['--root', str(self.root), '--base-ref', self.base,
                                   '--report-json', str(report), '--report-markdown', str(markdown),
                                   '--observe-manager-versions', '--manager-cache', str(cache)])
        self.assertEqual(0, result)
        self.assertEqual([(self.before['sources'], self.after['sources'], cache)], self.observed)
        data = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual('observed', data['manager_version_codes']['status'])
        self.assertIn('| SukiSU | 测试版 | `41075` → `41075` |', markdown.read_text(encoding='utf-8'))
        self.assertFalse(data['build_performed'])

    def observe(self, old, new, cache, versions):
        self.observed.append((old, new, cache))
        return versions

    def test_cli_commit_detail_observation_is_opt_in_and_keeps_digest(self):
        self.after['sources']['sukisu']['development']['head'] = NEW
        self.refresh()
        self.write(reports.FILES[0], self.after)
        baseline = reports.repository_report(self.root, self.base)
        with patch('upstream_change_details.request_json', side_effect=OSError('fake-secret')) as fetch:
            offline = reports.repository_report(self.root, self.base)
            fetch.assert_not_called()
            report, markdown = self.root / 'details.json', self.root / 'details.md'
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = reports.main(['--root', str(self.root), '--base-ref', self.base,
                                       '--report-json', str(report), '--report-markdown', str(markdown),
                                       '--observe-source-changes'])
        self.assertEqual(0, result)
        document = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(baseline['digest'], document['digest'])
        self.assertEqual(offline['digest'], document['digest'])
        self.assertEqual(1, len(document['source_commit_details']))
        self.assertEqual('unavailable', document['source_commit_details'][0]['status'])
        self.assertNotIn('官方提交的具体内容', markdown.read_text(encoding='utf-8'))
        self.assertNotIn('fake-secret', json.dumps(document))

    def test_lookup_outage_does_not_block_sync_or_leak_upstream_errors(self):
        self.after['sources']['sukisu']['release']['tag'] = 'v4.2.1-r2'
        self.refresh()
        self.write(reports.FILES[0], self.after)
        baseline = reports.repository_report(self.root, self.base)
        def unavailable(*args):
            raise RuntimeError('https://user:fake-secret@example.invalid/private')
        observer = SimpleNamespace(observe_versions=unavailable)
        with patch.dict(sys.modules, {'official_manager_versions': observer}), redirect_stdout(io.StringIO()) as output:
            report = reports.repository_report(self.root, self.base, observe_manager_versions=True,
                                                manager_cache=self.root/'source-cache')
        self.assertTrue(report['has_changes'])
        self.assertEqual(baseline['digest'], report['digest'])
        self.assertEqual('not-observed', report['manager_version_codes']['status'])
        self.assertIn('| SukiSU | 正式版 | 未能确认版本号 → 未能确认版本号 |', report['summary_markdown'])
        self.assertNotIn('fake-secret', output.getvalue() + report['summary_markdown'])
        self.assertIn('::warning::', output.getvalue())

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
