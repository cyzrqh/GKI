"""Source integration is a required gate, separate from update discovery."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import verify_manager_update as verifier
from test_official_sync_report import snapshot


class ManagerUpdateTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = snapshot()
        self.history = mock.patch.object(verifier, 'source_repository', return_value=Path('source-cache'))
        self.history.start()
        self.addCleanup(self.history.stop)
        counts = {self.snapshot['sukisu']['head']: 10, self.snapshot['sukisu']['development']['head']: 20,
                  self.snapshot['bakasu']['head']: 30}
        counting = mock.patch.object(verifier, 'fetch_reference', side_effect=lambda repository, ref: counts[ref])
        counting.start()
        self.addCleanup(counting.stop)
        checking = mock.patch.object(verifier, 'check_source', return_value={
            'version_code': 41117, 'checks': ['complete source integration']})
        self.checking = checking.start()
        self.addCleanup(checking.stop)

    def test_all_available_channels_are_checked_with_latest_main_dev_selection(self):
        report = verifier.validate_manager_snapshot(self.snapshot, cache=Path('cache'))
        self.assertTrue(report['passed'])
        self.assertEqual(3, self.checking.call_count)
        self.assertEqual({'正式版', '测试版'}, {row['channel'] for row in report['rows']})
        latest = next(row for row in report['rows'] if row['source'] == 'SukiSU' and row['channel'] == '测试版')
        self.assertEqual(self.snapshot['sukisu']['development']['head'], latest['commit'])
        self.assertEqual('passed', latest['status'])

    def test_incompatible_source_is_recorded_with_version_channel_and_never_passes(self):
        self.checking.side_effect = verifier.SourceIntegrationError('complete SUSFS patch does not apply', 41117)
        with tempfile.TemporaryDirectory(dir=ROOT.parent / '.gki-upstream-cache') as directory:
            path = Path(directory) / 'compatibility.json'
            with self.assertRaisesRegex(verifier.ManagerUpdateError, '未合并同步'):
                verifier.validate_manager_snapshot(self.snapshot, cache=Path('cache'), report_path=path)
            report = json.loads(path.read_text('utf-8'))
            self.assertFalse(report['passed'])
            self.assertIn('失败，未同步', path.with_suffix('.md').read_text('utf-8'))
            self.assertTrue(all(row['commit'] for row in report['rows'] if row['status'] == 'failure'))
            self.assertTrue(all(row['version_code'] == 41117 for row in report['rows'] if row['status'] == 'failure'))

    def test_both_channels_are_checked_without_duplicate_version_tables(self):
        before = copy.deepcopy(self.snapshot)
        before['sukisu']['development']['head'] = '8' * 40
        result = verifier.validate_manager_snapshot(self.snapshot, cache=Path('cache'), before_snapshot=before)
        text = verifier.render_report(result)
        self.assertNotIn('| SukiSU | 正式版 |', text)
        self.assertNotIn('| BakaSU |', text)
        self.assertNotIn('| SukiSU | 测试版 |', text)
        self.assertIn('配套验证通过', text)
        self.assertEqual(3, self.checking.call_count)


if __name__ == '__main__':
    unittest.main()
