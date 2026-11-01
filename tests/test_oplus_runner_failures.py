"""Execute the shell boundaries that stopped the real 6.12 matrix jobs."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_cctv
import oplus_recipe


class OPlusRunnerTests(unittest.TestCase):
    def setUp(self):
        git = shutil.which('git')
        candidates = ([Path(git).parent.parent / 'usr/bin' / name for name in ('bash.exe', 'sh.exe')]
                      if git else [])
        self.shell = shutil.which('bash') or next((str(path) for path in candidates if path.is_file()), None)
        if not self.shell:
            self.skipTest('Bash is required for native runner behavior tests')
        self.directory = ROOT / 'tests' / ('.oplus-runner-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)

    def run_shell(self, script):
        return subprocess.run([self.shell, '-euo', 'pipefail', '-c', 'set +o posix\n' + script], cwd=self.directory,
                              env=os.environ.copy(), capture_output=True, text=True, timeout=15)

    def native_script(self, row):
        path = ROOT / 'third_party/cctv-oplus/recipes' / Path(row['recipe_workflow']).name
        recipe = oplus_recipe.parse_recipe(path.read_bytes(), row)
        inputs = dict(kernelsu_variant='SukiSU', kernelsu_branch='最新版', enable_susfs=True,
                      use_kpm=True, use_lz4kd=True, use_bbg=False, use_bbr=False,
                      use_proxy_optimization=False, use_unicode_bypass=False)
        return oplus_recipe.plan_phase('build', recipe, row, inputs)[0].script

    def test_all_seven_native_setups_keep_exports_and_restore_strict_mode(self):
        setup = '[ -n "$_SETUP_ENV_SH_INCLUDED" ] && return || export _SETUP_ENV_SH_INCLUDED=1\n'
        setup += 'export OPLUS_NATIVE_EXPORT=preserved\n'
        (self.directory / '_setup_env.sh').write_text(setup, encoding='utf-8', newline='\n')
        broken = self.run_shell('source "./_setup_env.sh" 2>/dev/null || true\nprintf reached')
        self.assertNotEqual(0, broken.returncode)
        self.assertNotIn('reached', broken.stdout)
        rows = [row for row in oplus_cctv.load_profiles() if row['kernel_version'] == '6.12']
        self.assertEqual(7, len(rows))
        for row in rows:
            with self.subTest(target=row['id']):
                script = self.native_script(row)
                start = script.index('set +u\nsource "./_setup_env.sh"')
                end = script.index('set -u', start) + len('set -u')
                result = self.run_shell(script[start:end] + '\n'
                    '[[ "$OPLUS_NATIVE_EXPORT" == preserved ]]\n[[ "$-" == *u* ]]\nprintf reached')
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual('reached', result.stdout)

    def test_defconfig_failure_cannot_be_hidden_by_post_build_reporting(self):
        row = next(row for row in oplus_cctv.load_profiles() if row['id'] == 'sm8845-6.12.38')
        script = self.native_script(row)
        start = script.index('make -j')
        image = script.index(' O=out Image', start) + len(' O=out Image')
        stub = 'nproc() { printf 2; }; make() { printf "make-called\\n"; return 23; };\n'
        result = self.run_shell(stub + script[start:image] + '\nprintf after-build')
        self.assertEqual(23, result.returncode, result.stderr)
        self.assertEqual('make-called\n', result.stdout)

    def installer(self, mode):
        stubs = '''
ready=0
attempts=0
dpkg-query() { if ((ready)); then printf installed; else return 1; fi; }
timeout() {
  printf 'bounded: %s\\n' "$*"
  case " $* " in
    *' update '*) return 124 ;;
  esac
  attempts=$((attempts + 1))
  if [[ "$MODE" == fail ]] || ((attempts == 1)); then return 100; fi
  ready=1
}
'''
        if mode == 'installed':
            stubs += 'ready=1\n'
        script = (ROOT / 'tools/install_oplus_dependencies.sh').read_text('utf-8')
        return self.run_shell('MODE=' + mode + '\n' + stubs + script)

    def test_installed_runner_does_not_refresh_or_install_packages(self):
        result = self.installer('installed')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn('bounded:', result.stdout)

    def test_bounded_refresh_failure_can_recover_using_existing_indexes(self):
        result = self.installer('recover')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(3, result.stdout.count('bounded:'))
        self.assertIn('180s', result.stdout)
        self.assertIn('90s', result.stdout)
        self.assertIn('all required packages verified', result.stdout)

    def test_failed_retry_keeps_nonzero_result_and_does_not_claim_success(self):
        result = self.installer('fail')
        self.assertEqual(100, result.returncode, result.stderr)
        self.assertNotIn('all required packages verified', result.stdout)


if __name__ == '__main__':
    unittest.main()
