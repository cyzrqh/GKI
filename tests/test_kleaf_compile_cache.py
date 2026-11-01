"""Official Kleaf layouts and preservation of its actual Make/compiler argv."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import kleaf_compile_cache as cache
import thinlto_cache
from test_kernel_compile_cache import bash, posix


class KleafCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'kernel'
        self.target = self.source / 'build/kernel/kleaf/impl/kernel_build.bzl'
        self.target.parent.mkdir(parents=True)
        self.program = self.root / 'cache/ccache'
        self.output = self.root / 'cache/gki-make'
        self.directory = self.root / 'objects'

    def configure(self):
        return cache.configure(self.source, self.program, self.output, self.directory)

    def test_actual_official_old_and_new_kleaf_keep_all_goals_and_module_install(self):
        rows = json.loads((ROOT / 'tests/fixtures/kleaf-cache/build-blocks.json').read_text())
        for row in rows:
            with self.subTest(reference=row['reference']):
                text = 'command = """\n' + row['block'] + ' make modules_install\n"""\n'
                self.target.write_text(text, encoding='utf-8')
                self.assertTrue(self.configure())
                actual = self.target.read_text('utf-8')
                self.assertEqual(text.count(cache.MAKE), actual.count(self.output.as_posix()))
                expected = actual.replace('         ' + cache.MARKER + '\n', '').replace(
                    self.output.as_posix() + ' -C ', 'make -C ')
                self.assertEqual(text, expected)
                self.assertTrue(self.configure())
                self.assertEqual(actual, self.target.read_text('utf-8'))

    def test_unknown_layout_is_not_changed_or_advertised_as_enabled(self):
        for text in ('not a supported rule', cache.START + 'make different_arguments\n' + cache.END,
                     cache.START + cache.MAKE * 5 + '\n' + cache.END):
            self.target.write_text(text, encoding='utf-8')
            self.assertFalse(self.configure())
            self.assertEqual(text, self.target.read_text('utf-8'))

    def test_launcher_preserves_official_compiler_flags_and_failure_status(self):
        shell = bash()
        if not shell:
            self.skipTest('Bash required')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        make = bin_dir / 'make'
        make.write_text('#!/usr/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_LOG"\n'
                        'printf "%s\\n" "$CCACHE_DIR" "$CCACHE_BASEDIR" > "$ENV_LOG"\n'
                        'exit "${MAKE_STATUS:-0}"\n', encoding='utf-8', newline='\n')
        make.chmod(0o700)
        script = self.root / 'gki-make'
        script.write_text(cache.launcher(self.program, self.directory), encoding='utf-8', newline='\n')
        log, environment_log = self.root / 'argv.log', self.root / 'env.log'
        environment = {**os.environ, 'PATH': posix(bin_dir) + os.pathsep + os.environ['PATH'],
                       'ARGV_LOG': posix(log), 'ENV_LOG': posix(environment_log), 'CC': 'wrong-compiler',
                       'ROOT_DIR': posix(self.source), 'MAKE_STATUS': '23'}
        arguments = ['-C', 'common', 'CC=clang-actual', 'LLVM=1', 'O=out', 'Image', 'modules', 'VALUE=one two']
        result = subprocess.run([shell, posix(script), *arguments], env=environment,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(23, result.returncode, result.stdout + result.stderr)
        actual = log.read_text('utf-8').splitlines()
        self.assertEqual(arguments, actual[:-1])
        self.assertEqual('CC=' + self.program.as_posix() + ' clang-actual', actual[-1])
        self.assertEqual([self.directory.as_posix(), posix(self.source)], environment_log.read_text('utf-8').splitlines())

    def test_all_five_families_retain_upstream_thinlto_cache_policy(self):
        rows = json.loads((ROOT / 'tests/fixtures/kleaf-cache/native-lto-blocks.json').read_text())
        self.assertEqual({'5.10', '5.15', '6.1', '6.6', '6.12'}, {row['kernel'].rsplit('.', 1)[0] for row in rows})
        for row in rows:
            source = self.root / ('common-' + row['kernel'])
            source.mkdir()
            (source / 'Makefile').write_text(row['block'], encoding='utf-8')
            native = any('--thinlto-cache-dir=' in line for line in row['cache_lines'])
            self.assertEqual(native, thinlto_cache.configure(source, self.root / ('lto-' + row['kernel'])))
            if not native:
                self.assertEqual(row['block'], (source / 'Makefile').read_text('utf-8'))

    def test_real_make_and_c_compiler_keep_identical_output_through_cache_launcher(self):
        make, compiler = shutil.which('make'), shutil.which('cc')
        if os.name == 'nt' or not make or not compiler:
            self.skipTest('Native Linux Make/C compiler required; checked in Ubuntu preflight')
        self.program.parent.mkdir(parents=True)
        self.program.write_text('#!/bin/bash\nexec "$@"\n', encoding='utf-8', newline='\n')
        self.program.chmod(0o700)
        self.output.write_text(cache.launcher(self.program, self.directory), encoding='utf-8', newline='\n')
        self.output.chmod(0o700)
        (self.root / 'unit.c').write_text('int main(void) { return MAGIC != 42; }\n', encoding='utf-8')
        (self.root / 'Makefile').write_text('all:\n\t$(CC) $(CFLAGS) unit.c -o result\n', encoding='utf-8')
        arguments = ['-s', '-C', str(self.root), 'CC=' + compiler, 'CFLAGS=-O2 -DMAGIC=42']
        direct = subprocess.run([make, *arguments], capture_output=True)
        self.assertEqual(0, direct.returncode, direct.stderr.decode(errors='replace'))
        original = (self.root / 'result').read_bytes()
        wrapped = subprocess.run([str(self.output), *arguments], capture_output=True)
        self.assertEqual(0, wrapped.returncode, wrapped.stderr.decode(errors='replace'))
        self.assertEqual(original, (self.root / 'result').read_bytes())
        self.assertEqual(0, subprocess.run([str(self.root / 'result')]).returncode)


if __name__ == '__main__':
    unittest.main()
