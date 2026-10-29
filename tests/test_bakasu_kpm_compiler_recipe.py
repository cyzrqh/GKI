"""Run the audited native Make flag rules and a real C preprocessor compile."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import prepare_manager_lkm as lkm

FIXTURE = ROOT / 'tests/fixtures/bakasu-kpm-compiler'
MAKE = os.environ.get('BAKASU_MAKE') or shutil.which('make')
COMPILER = os.environ.get('BAKASU_NATIVE_CC') or shutil.which('cc')


@unittest.skipUnless(MAKE and COMPILER, 'Requires GNU Make and a C compiler')
class BakaNativeCompilerRecipeTests(unittest.TestCase):
    def test_native_numeric_and_full_version_rules_compile_without_aliasing_identifiers(self):
        root = ROOT / 'tests' / ('.baka-compiler-' + uuid.uuid4().hex)
        root.mkdir()
        self.addCleanup(shutil.rmtree, root)
        raw = (FIXTURE / 'native-flags.mk').read_bytes()
        provenance = json.loads((FIXTURE / 'provenance.json').read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance['fixture_sha256'])
        self.assertEqual('d5e9e025b8b49155f8014684d64c91742b465f65', provenance['ref'])
        makefile = root / 'Makefile'
        flags_file = root / 'flags.txt'
        makefile.write_text('include ' + (FIXTURE / 'native-flags.mk').as_posix() + '\n'
                            + '$(file >' + flags_file.as_posix() + ',$(ccflags-y))\n'
                            + '.PHONY: inspect\ninspect: ;\n', encoding='utf-8', newline='\n')
        source = root / 'kpm/kpm.c'
        source.parent.mkdir()
        source.write_text('_Static_assert(CONFIG_KPM == 1, "KPM must be enabled");\n'
                          '_Static_assert(KSU_VERSION == 35252, "native numeric version must match");\n'
                          'const char native_name[] = KSU_VERSION_FULL;\n', encoding='utf-8', newline='\n')
        target = source.with_suffix('.o')
        compiler = [COMPILER, 'cc'] if Path(COMPILER).stem.lower() == 'zig' else [COMPILER]
        for mode in ('y', 'n'):
            result = subprocess.run([MAKE, '--no-print-directory', '-f', str(makefile), 'inspect',
                                     'CONFIG_KPM=' + mode, 'KBUILD_EXTMOD=module', 'KSU_VERSION=35252',
                                     'KSU_VERSION_FULL=v4.2.0-rc3-d5e9e025-dirty@BakaSU'],
                                    capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            flags = flags_file.read_text('utf-8').strip()
            command = 'clang ' + flags + ' -c -o ' + target.as_posix() + ' ' + source.as_posix()
            if mode == 'n':
                with self.assertRaisesRegex(lkm.LkmError, 'missing the required macro: CONFIG_KPM'):
                    lkm.validate_kpm_command(command, 35252)
                continue
            lkm.validate_kpm_command(command, 35252)
            self.assertIn('-DKSU_VERSION=35252', shlex.split(flags))
            self.assertIn('-DKSU_VERSION_FULL="v4.2.0-rc3-d5e9e025-dirty@BakaSU"', shlex.split(flags))
            result = subprocess.run([*compiler, '-std=c11', '-Wall', '-Werror', *shlex.split(flags),
                                     '-c', '-o', str(target), str(source)],
                                    capture_output=True, text=True, encoding='utf-8', timeout=120)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertGreater(target.stat().st_size, 0)


if __name__ == '__main__':
    unittest.main()
