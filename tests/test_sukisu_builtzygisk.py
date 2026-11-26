"""Exercise the selected-source dependency gate without fetching or building it."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_URLS = ('https://github.com/SukiSU-Nova/BuiltZygisk',
                 'https://github.com/SukiSU-Nova/BuiltZygisk.git',
                 'https://git.zako.shirkneko.top:20004/shirkneko/BuiltZygisk',
                 'https://git.zako.shirkneko.top:20004/shirkneko/BuiltZygisk.git')
PIN = 'dfe0b4d2cac3d91a72880b073c3f1d95bc8953d4'
SUBMODULE = 'external/BuiltZygisk'


class NovaBuiltZygiskSourceTests(unittest.TestCase):
    def setUp(self):
        git = shutil.which('git')
        if not git:
            self.skipTest('Git is required for the selected-source dependency fixture')
        self.environment = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        self.git_executable = git
        temporary = tempfile.TemporaryDirectory(prefix='.builtzygisk-gate-', dir=ROOT / 'tests')
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        self.modules = self.source / '.gitmodules'
        self.git('init', '-q')
        self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'user.name', 'Native dependency test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write_url(OFFICIAL_URLS[0])
        self.git('add', '.gitmodules')
        self.git('update-index', '--add', '--cacheinfo', '160000,' + PIN + ',' + SUBMODULE)
        self.commit()

    def git(self, *arguments, input=None):
        result = subprocess.run([self.git_executable, '-C', str(self.source), *arguments],
                                input=input, capture_output=True, text=True, encoding='utf-8',
                                env=self.environment, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout.strip()

    def write_url(self, url):
        self.modules.write_text('[submodule "external/BuiltZygisk"]\n'
                                '\tpath = external/BuiltZygisk\n\turl = ' + url + '\n',
                                encoding='utf-8', newline='\n')

    def commit(self):
        self.git('commit', '-q', '--allow-empty', '-m', 'selected official source fixture')

    def run_gate(self):
        return subprocess.run([sys.executable, str(ROOT / 'tools/sukisu_builtzygisk_source.py'), str(self.source)],
                              capture_output=True, text=True, encoding='utf-8', env=self.environment,
                              timeout=15)

    def test_both_official_transports_with_and_without_git_preserve_the_pin(self):
        for url in OFFICIAL_URLS:
            with self.subTest(url=url):
                self.write_url(url)
                self.git('add', '.gitmodules')
                self.commit()
                result = self.run_gate()
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(url + ' ' + PIN, result.stdout.strip())

    def test_third_party_or_duplicate_urls_are_not_accepted(self):
        for url, duplicate in (('https://example.invalid/third-party/BuiltZygisk.git', None),
                               ('file:///untrusted/BuiltZygisk.git', None),
                               (OFFICIAL_URLS[0], OFFICIAL_URLS[0]),
                               (OFFICIAL_URLS[0], OFFICIAL_URLS[1])):
            with self.subTest(url=url, duplicate=duplicate):
                self.write_url(url)
                if duplicate:
                    with self.modules.open('a', encoding='utf-8', newline='\n') as stream:
                        stream.write('\turl = ' + duplicate + '\n')
                self.git('add', '.gitmodules')
                self.commit()
                result = self.run_gate()
                self.assertNotEqual(0, result.returncode)
                self.assertIn('Unexpected official Nova BuiltZygisk repository', result.stderr)
                self.assertNotIn(PIN, result.stdout)

    def test_worktree_or_staged_url_and_path_changes_are_rejected(self):
        original = self.modules.read_text(encoding='utf-8')
        for before, after in ((OFFICIAL_URLS[0], OFFICIAL_URLS[1]),
                              ('path = external/BuiltZygisk', 'path = external/Other')):
            for staged in (False, True):
                with self.subTest(before=before, staged=staged):
                    self.git('restore', '--source=HEAD', '--staged', '--worktree', '.gitmodules')
                    self.modules.write_text(original.replace(before, after), encoding='utf-8', newline='\n')
                    if staged:
                        self.git('add', '.gitmodules')
                    result = self.run_gate()
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn('dependency declarations differ from the fixed source', result.stderr)
                    self.assertNotIn(PIN, result.stdout)

    def test_missing_or_regular_file_entry_cannot_replace_the_gitlink(self):
        for mode in ('missing', '100644'):
            with self.subTest(mode=mode):
                self.git('update-index', '--force-remove', SUBMODULE)
                if mode != 'missing':
                    blob = self.git('hash-object', '-w', '--stdin', input='not a Git submodule\n')
                    self.git('update-index', '--add', '--cacheinfo', mode + ',' + blob + ',' + SUBMODULE)
                self.commit()
                result = self.run_gate()
                self.assertNotEqual(0, result.returncode)
                self.assertIn('must pin its BuiltZygisk submodule', result.stderr)
                self.assertNotIn(PIN, result.stdout)

    def test_committed_path_changes_and_credential_bearing_urls_are_rejected(self):
        for url in ('https://user:password@github.com/SukiSU-Nova/BuiltZygisk.git',
                    'https://github.com/SukiSU-Nova/BuiltZygisk.git?ref=main'):
            with self.subTest(url=url):
                self.write_url(url)
                self.git('add', '.gitmodules')
                self.commit()
                self.assertNotEqual(0, self.run_gate().returncode)
        self.write_url(OFFICIAL_URLS[1])
        self.modules.write_text(self.modules.read_text(encoding='utf-8').replace(
            'path = external/BuiltZygisk', 'path = external/Other'), encoding='utf-8', newline='\n')
        self.git('add', '.gitmodules')
        self.commit()
        result = self.run_gate()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('dependency path requires review', result.stderr)

    def test_builder_runs_the_shared_guard_before_network_or_build(self):
        script = (ROOT / 'tools/build_sukisu_builtzygisk.sh').read_text(encoding='utf-8')
        guard = 'dependency_identity="$(python3 "$build_tools/sukisu_builtzygisk_source.py" "$source_dir")"'
        self.assertIn(guard, script)
        self.assertLess(script.index(guard), script.index('submodule update --init'))
        self.assertIn('-c "submodule.$submodule_path.url=$source_url"', script)


if __name__ == '__main__':
    unittest.main()
