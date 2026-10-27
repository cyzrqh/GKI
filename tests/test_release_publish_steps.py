"""Run the shared publication sequence against an inert local gh function."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from workflow_jobs import release_publish_steps


def shell_path():
    if found := shutil.which('bash') or shutil.which('sh'):
        return found
    if found := shutil.which('git'):
        shell = Path(found).parent.parent / 'usr/bin/sh.exe'
        if shell.is_file():
            return str(shell)
    return None


class ReleasePublishStepsTests(unittest.TestCase):
    def test_shared_conditions_outputs_and_safe_relative_paths(self):
        steps = release_publish_steps('success()', 'verified/assets', 'report/summary.md', 'report/title.txt')
        self.assertEqual(['release-tag', 'release', None], [step.get('id') for step in steps])
        self.assertEqual(['success()', 'success()', "steps.release.outcome == 'success'"],
                         [step['if'] for step in steps])
        self.assertIn('allocate --notes report/summary.md', steps[0]['run'])
        self.assertIn('assets=(verified/assets/*)', steps[1]['run'])
        for path in ('/tmp/file', '../file', 'x/../file', 'x//file', '-option', 'x;touch-file', 'x space'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                release_publish_steps('success()', assets_directory=path)

    @unittest.skipUnless(shell_path(), 'A POSIX shell is required for inert publication sequencing')
    def test_native_assets_publish_draft_first_and_existing_draft_is_reused(self):
        directory = ROOT.parent / '.gki-upstream-cache' / ('release-publish-steps-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        for name, value in (('report/title.txt', 'SukiSU 41099 最新测试版'), ('report/summary.md', 'verified notes'),
                            ('verified/assets/SukiSU-release.apk', 'inert APK'),
                            ('verified/assets/kernel-AnyKernel3.zip', 'inert AK3'),
                            ('verified/assets/SHA256SUMS.txt', 'inert hashes')):
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding='utf-8')
        script = ('gh() { printf \'%s\\n\' "$*" >> gh-calls; }\n'
                  'GITHUB_OUTPUT=output; GITHUB_STEP_SUMMARY=summary;\n'
                  + release_publish_steps('success()', 'verified/assets', 'report/summary.md', 'report/title.txt')[1]['run'])
        shell = shell_path()
        for existing in ('false', 'true'):
            trace = directory / 'gh-calls'
            trace.unlink(missing_ok=True)
            environment = {**os.environ, 'RELEASE_EXISTS': existing, 'RELEASE_TAG': 'r1.3',
                           'GITHUB_REPOSITORY': 'cyzrqh/GKI', 'GITHUB_SHA': 'a' * 40,
                           'PATH': str(Path(shell).parent) + os.pathsep + os.environ.get('PATH', '')}
            result = subprocess.run([shell, '-c', script], cwd=directory, env=environment,
                                    capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = trace.read_text(encoding='utf-8').splitlines()
            self.assertEqual(3 if existing == 'false' else 2, len(calls))
            if existing == 'false':
                self.assertTrue(calls[0].startswith('release create r1.3 '))
                self.assertIn('--draft --title SukiSU 41099 最新测试版', calls[0])
            self.assertTrue(calls[-2].startswith('release upload r1.3 '))
            self.assertTrue(calls[-1].startswith('release edit r1.3 --draft=false '))
            self.assertNotIn('title.txt', calls[-2])
            self.assertNotIn('summary.md', calls[-2])
            self.assertIn('SHA256SUMS.txt', calls[-2])


if __name__ == '__main__':
    unittest.main()
