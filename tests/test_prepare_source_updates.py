"""A failed independent preparation cannot discard other verified source work."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import prepare_source_updates as prepare


class SourcePreparationTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.source-preparation-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.reports = self.root / 'reports'
        self.profile = json.loads((ROOT / prepare.PROFILE_PATH).read_text(encoding='utf-8'))
        for name, raw in {
                prepare.PROFILE_PATH: json.dumps(self.profile).encode(),
                'data/upstream-candidates.json': b'old official', 'data/kernels.json': b'old catalog',
                'build-compatibility.json': b'old profile', 'config/manager-susfs-pins.json': b'old pins'}.items():
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        self.environment = {key.upper() + suffix: value for key in prepare.SOURCES
                            for suffix, value in (('_RESULT', 'success'), ('_CHANGED', 'true'), ('_CANDIDATES', '{"fixed":"snapshot"}'))}
        self.environment.update(GH_TOKEN='private', GITHUB_OUTPUT='must-not-inherit')

    def invoke(self, runner):
        return prepare.prepare(self.root, self.environment, self.reports, runner=runner)

    def source(self, command):
        return {'check_official_updates.py': 'official', 'update_kernel_catalog.py': 'catalog',
                'update_susfs_sources.py': 'susfs', 'update_oplus_sources.py': 'oplus'}[Path(command[1]).name]

    def write(self, name, raw):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)

    def test_susfs_network_failure_rolls_back_its_three_files_and_still_prepares_oplus(self):
        calls = []
        def run(command, **options):
            source = self.source(command); calls.append(source)
            self.assertEqual(self.root, options['cwd'])
            self.assertNotIn('GITHUB_OUTPUT', options['env'])
            self.assertEqual('private', options['env']['GH_TOKEN'])
            if source == 'official': self.write('data/upstream-candidates.json', b'new official')
            if source == 'catalog': self.write('data/kernels.json', b'new catalog')
            if source == 'susfs':
                self.write('build-compatibility.json', b'partial profile')
                self.write('config/manager-susfs-pins.json', b'partial pins')
                self.write('data/susfs-upstream-candidates.json', b'partial receipt')
                return SimpleNamespace(returncode=1)
            if source == 'oplus': self.write('data/oplus-upstream-candidates.json', b'verified oplus')
            return SimpleNamespace(returncode=0)
        result = self.invoke(run)
        self.assertEqual(list(prepare.SOURCES), calls)
        self.assertTrue(result['has_updates']); self.assertTrue(result['partial_failure'])
        self.assertEqual('failure', result['sources']['susfs']['result'])
        self.assertEqual('success', result['sources']['oplus']['result'])
        self.assertEqual(b'old profile', (self.root / 'build-compatibility.json').read_bytes())
        self.assertEqual(b'old pins', (self.root / 'config/manager-susfs-pins.json').read_bytes())
        self.assertFalse((self.root / 'data/susfs-upstream-candidates.json').exists())
        self.assertEqual(b'new official', (self.root / 'data/upstream-candidates.json').read_bytes())
        self.assertEqual(b'new catalog', (self.root / 'data/kernels.json').read_bytes())
        self.assertEqual(b'verified oplus', (self.root / 'data/oplus-upstream-candidates.json').read_bytes())
        report = prepare.render_report(result)
        self.assertIn('| SUSFS 构建来源 | 完成 | 失败 | 0 | 已恢复该来源原文件', report)
        self.assertIn('| 欧加真来源 | 完成 | 完成 | 1 |', report)
        self.assertIn('部分来源未完成', report)

    def test_every_source_can_fail_independently_without_blocking_later_sources(self):
        for failing in prepare.SOURCES:
            calls = []
            def run(command, **options):
                source = self.source(command); calls.append(source)
                if source == failing: raise OSError('network unavailable')
                return SimpleNamespace(returncode=0)
            with self.subTest(failing=failing):
                result = self.invoke(run)
                self.assertEqual(list(prepare.SOURCES), calls)
                self.assertEqual('failure', result['sources'][failing]['result'])
                self.assertTrue(result['partial_failure'])
                self.assertFalse(result['has_updates'])

    def test_unchanged_sources_never_invoke_a_network_helper(self):
        for source in prepare.SOURCES: self.environment[source.upper() + '_CHANGED'] = 'false'
        runner = mock.Mock()
        result = self.invoke(runner)
        runner.assert_not_called()
        self.assertFalse(result['has_updates']); self.assertFalse(result['partial_failure'])
        self.assertEqual({key + '_result': 'success' for key in prepare.SOURCES},
                         {k: v for k, v in prepare.outputs(result).items() if k.endswith('_result')})
        report = prepare.render_report(result)
        self.assertNotIn('部分来源未完成', report)
        self.assertEqual(4, report.count('无需变更'))
        self.assertIn('来源准备完成', report)

    def test_failed_probe_never_applies_its_candidates_and_successful_sources_continue(self):
        self.environment['OFFICIAL_RESULT'] = 'failure'
        self.environment['SUSFS_RESULT'] = 'skipped'
        calls = []
        def run(command, **options):
            calls.append(self.source(command)); return SimpleNamespace(returncode=0)
        result = self.invoke(run)
        self.assertEqual(['catalog', 'oplus'], calls)
        self.assertTrue(result['partial_failure'])
        self.assertEqual('failure', result['sources']['official']['result'])
        self.assertEqual('skipped', result['sources']['susfs']['result'])

    def test_timeout_restores_the_failed_source_and_does_not_report_changes(self):
        for source in prepare.SOURCES: self.environment[source.upper() + '_CHANGED'] = 'false'
        self.environment['CATALOG_CHANGED'] = 'true'
        def run(command, **options):
            self.write('data/kernels.json', b'partial')
            raise subprocess.TimeoutExpired('fixed-source-tool', 600)
        result = self.invoke(run)
        self.assertEqual(b'old catalog', (self.root / 'data/kernels.json').read_bytes())
        self.assertFalse(result['has_updates']); self.assertTrue(result['partial_failure'])
        self.assertEqual('来源准备超时', result['sources']['catalog']['error'])

    def test_restore_failure_is_hard_failure_and_stops_later_sources(self):
        calls = []
        def run(command, **options):
            source = self.source(command); calls.append(source)
            return SimpleNamespace(returncode=1 if source == 'susfs' else 0)
        with mock.patch.object(prepare, 'restore_files', side_effect=OSError('disk failed')), \
                self.assertRaisesRegex(prepare.PreparationError, '无法恢复原文件'):
            self.invoke(run)
        self.assertEqual(['official', 'catalog', 'susfs'], calls)

    def test_missing_changed_snapshot_is_failure_without_invoking_child(self):
        for source in prepare.SOURCES: self.environment[source.upper() + '_CHANGED'] = 'false'
        self.environment.update(OPLUS_CHANGED='true', OPLUS_CANDIDATES='')
        runner = mock.Mock()
        result = self.invoke(runner)
        runner.assert_not_called()
        self.assertEqual('failure', result['sources']['oplus']['result'])

    def test_cli_writes_explicit_partial_states_and_detailed_report(self):
        self.environment['SUSFS_RESULT'] = 'failure'
        for source in prepare.SOURCES: self.environment[source.upper() + '_CHANGED'] = 'false'
        output = self.root / 'outputs'
        with mock.patch.dict(prepare.os.environ, self.environment, clear=True):
            self.assertEqual(0, prepare.main(['--root', str(self.root), '--report-dir', str(self.reports), '--github-output', str(output)]))
        values = dict(line.split('=', 1) for line in output.read_text(encoding='utf-8').splitlines())
        self.assertEqual('failure', values['susfs_result'])
        self.assertEqual('success', values['oplus_result'])
        self.assertEqual('true', values['partial_failure'])
        self.assertEqual('false', values['has_updates'])
        report = json.loads((self.reports / 'source-preparation.json').read_text(encoding='utf-8'))
        self.assertEqual('failure', report['sources']['susfs']['result'])
        self.assertNotIn('private', json.dumps(report))
        markdown = (self.reports / 'source-preparation.md').read_text(encoding='utf-8')
        self.assertIn('| SUSFS 构建来源 | 失败 | 未运行 | 0 |', markdown)
        self.assertIn('失败来源没有同步', markdown)

    def test_input_states_and_arbitrary_paths_are_rejected(self):
        self.environment['SUSFS_RESULT'] = 'success; unwanted shell'
        runner = mock.Mock()
        with self.assertRaises(prepare.PreparationError): self.invoke(runner)
        runner.assert_not_called()
        for relative in ('../escape.json', '../../elsewhere.json'):
            with self.assertRaises(prepare.PreparationError): prepare.checked_path(self.root, relative)


if __name__ == '__main__':
    unittest.main()
