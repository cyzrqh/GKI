"""Execute the shell boundaries that stopped the real 6.12 matrix jobs."""
import os
from pathlib import Path
import shutil
import stat
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
        self.addCleanup(self.remove_temporary)

    def remove_temporary(self):
        directory = self.directory.resolve(strict=True)
        if directory.parent != ROOT / 'tests' or not directory.name.startswith('.oplus-runner-'):
            raise ValueError('Refusing cleanup outside this runner test directory')
        def writable(function, name, error):
            path = Path(name)
            if not path.resolve(strict=True).is_relative_to(directory):
                raise ValueError('Runner test cleanup escaped its temporary directory')
            path.chmod(stat.S_IWRITE)
            function(name)
        shutil.rmtree(directory, onexc=writable)

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
                start = script.index('set +u\nKERNEL_DIR=.\nsource "./_setup_env.sh"')
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

    def test_all_native_setups_resolve_the_current_source_once_and_keep_its_real_epoch(self):
        # Replay the helper's real Git lookup with a real source repository.
        # KSU setup exports an absolute KERNEL_DIR; Kleaf joins it with ROOT_DIR.
        setup = ('export ROOT_DIR=$(readlink -f "$PWD")\n'
                 'export SOURCE_DATE_EPOCH=$(git -C "${ROOT_DIR}/${KERNEL_DIR}" log -1 --pretty=%ct)\n'
                 '[[ -n "$SOURCE_DATE_EPOCH" ]] || return 31\n'
                 'export OPLUS_NATIVE_EXPORT=preserved\n')
        (self.directory / '_setup_env.sh').write_text(setup, encoding='utf-8', newline='\n')
        initialized = self.run_shell('git init -q\ngit add _setup_env.sh\n'
            'git -c user.name=Fixture -c user.email=fixture@example.invalid commit -qm fixture\n')
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        broken = self.run_shell('export KERNEL_DIR="$PWD"\nset +u\n'
                                'source "./_setup_env.sh"\n')
        self.assertNotEqual(0, broken.returncode)
        self.assertIn('cannot change to', broken.stderr)
        for row in oplus_cctv.load_profiles():
            if row['kernel_version'] != '6.12':
                continue
            with self.subTest(target=row['id']):
                script = self.native_script(row)
                start = script.index('set +u\nKERNEL_DIR=.\nsource "./_setup_env.sh"')
                end = script.index('set -u', start) + len('set -u')
                result = self.run_shell('export KERNEL_DIR="$PWD"\n' + script[start:end] + '\n'
                    '[[ "$OPLUS_NATIVE_EXPORT" == preserved ]]\n'
                    '[[ "$SOURCE_DATE_EPOCH" == "$(git log -1 --pretty=%ct)" ]]\n'
                    '[[ "$KERNEL_DIR" == . ]]\n[[ "$-" == *u* ]]\nprintf reached')
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual('reached', result.stdout)
                self.assertNotIn('cannot change to', result.stderr)

if __name__ == '__main__':
    unittest.main()
