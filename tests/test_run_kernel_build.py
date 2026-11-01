import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import run_kernel_build as runner


class BuildProgressTests(unittest.TestCase):
    def test_actual_child_exit_code_is_preserved_and_summary_is_measured(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            summary = Path(directory) / 'summary.md'
            with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}), contextlib.redirect_stdout(io.StringIO()):
                code = runner.run([sys.executable, '-c', 'raise SystemExit(23)'], '测试编译', interval=0.05)
            self.assertEqual(23, code)
            self.assertIn('退出码 23', summary.read_text('utf-8'))

    def test_no_shell_evaluation_of_child_arguments(self):
        with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}), contextlib.redirect_stdout(io.StringIO()):
            result = runner.run([sys.executable, '-c', 'import sys; assert sys.argv[1] == "literal $(secret) one two"',
                                 'literal $(secret) one two'], '测试编译', interval=0.05)
        self.assertEqual(0, result)

    def test_unavailable_process_info_is_not_a_build_failure(self):
        self.assertEqual('官方构建命令仍在运行', runner.activity(123, ROOT / 'not-an-existing-proc'))


if __name__ == '__main__':
    unittest.main()
