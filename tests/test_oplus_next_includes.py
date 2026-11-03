"""Replay Next's frozen native include layout with GNU make and a C preprocessor."""
from pathlib import Path
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from oplus_ksu_compat import NEXT_BUILTIN_ROOT, NEXT_FALLBACK, prepare_builtin_includes
import test_oplus_ksu_compat as suki_tests

FIXTURE = ROOT / 'tests/fixtures/oplus-next-includes'


@unittest.skipUnless(shutil.which('git'), 'Git is required for frozen official header verification')
class OPlusNextIncludeTests(unittest.TestCase):
    alias = suki_tests.OPlusKsuIncludeTests.alias
    git = suki_tests.OPlusKsuIncludeTests.git
    commit = suki_tests.OPlusKsuIncludeTests.commit

    def setUp(self):
        self.root = ROOT / 'tests' / ('.oplus-ksu-include-' + uuid.uuid4().hex)
        self.repository = self.root / 'KernelSU'
        self.kernel = self.repository / 'kernel'
        (self.kernel / 'include').mkdir(parents=True)
        (self.repository / 'uapi').mkdir()
        drivers = self.root / 'common/drivers'
        drivers.mkdir(parents=True)
        self.addCleanup(suki_tests.remove_temporary, self.root)
        self.alias(self.kernel / 'include/uapi', '../../uapi')
        self.alias(drivers / 'kernelsu', '../../KernelSU/kernel')
        self.kbuild = self.kernel / 'Kbuild'
        self.kbuild.write_bytes((FIXTURE / 'Kbuild-include.inc').read_bytes())
        shutil.copyfile(FIXTURE / 'app_profile.h', self.repository / 'uapi/app_profile.h')
        shutil.copyfile(FIXTURE / 'klog.h', self.kernel / 'include/klog.h')
        self.git('init', '-q')
        self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'core.symlinks', 'true')
        self.commit()
        # The real SUSFS port adds this header after the official source snapshot.
        shutil.copyfile(FIXTURE / 'ksu_samsung_kdp.h', self.kernel / 'include/ksu_samsung_kdp.h')
        self.signature = ('\nccflags-y += -DEXPECTED_MANAGER_SIZE=730 '
                          '-DEXPECTED_MANAGER_HASH=paired_certificate -DKSU_VERSION=33455 -Werror\n')
        with self.kbuild.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(self.signature)

    def prepare(self):
        return prepare_builtin_includes(self.root, self.reference, variant='KernelSU-Next')

    def probe(self, output, *options):
        output.mkdir(exist_ok=True)
        source = '..' if output.name == 'out' else '.'
        probe = output / 'probe.mk'
        probe.write_text('srctree := ' + source + '\nobjtree := .\nobj := drivers/kernelsu\n'
                         'src := $(if $(VPATH),$(VPATH)/)$(obj)\n'
                         'include $(srctree)/drivers/kernelsu/Kbuild\n'
                         '$(info HEADER_FLAGS=$(filter -I%,$(ccflags-y)))\n'
                         '$(info APP_HEADER=$(if $(wildcard $(KSU_KERNEL_DIR)/include/uapi/app_profile.h),1,0))\n'
                         '$(info KDP_HEADER=$(if $(wildcard $(KSU_KERNEL_DIR)/include/ksu_samsung_kdp.h),1,0))\n'
                         'all:;\n', encoding='utf-8', newline='\n')
        result = subprocess.run(['make', '--no-print-directory', '-f', 'probe.mk', *options], cwd=output,
                                text=True, encoding='utf-8', capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout

    def test_fixtures_record_the_failed_latest_and_formal_native_layout_and_real_headers(self):
        info = json.loads((FIXTURE / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual('a6c81fa4a8b7341d0bc5c3e4507531459df871ae', info['commit'])
        self.assertEqual('4c706b5938665866ebe16944a481a7b91e3396af', info['stable_commit'])
        for name, expected in info['sha256'].items():
            self.assertEqual(expected, hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest())
        self.assertEqual(info['susfs_patch_sha256'],
                         hashlib.sha256((ROOT / 'tools/patches/next-susfs23.patch').read_bytes()).hexdigest())

    def test_only_builtin_root_changes_and_both_native_and_susfs_flags_are_preserved(self):
        for susfs in (False, True):
            with self.subTest(susfs=susfs):
                original = (FIXTURE / 'Kbuild-include.inc').read_text(encoding='utf-8') + self.signature
                if susfs:
                    original = original.replace('-I$(KSU_KERNEL_DIR)/include\n',
                                                '-I$(KSU_KERNEL_DIR)/include -I$(KSU_KERNEL_DIR)/..\n')
                self.kbuild.write_text(original, encoding='utf-8', newline='\n')
                self.assertTrue(self.prepare())
                changed = self.kbuild.read_text(encoding='utf-8')
                self.assertEqual(original, changed.replace(NEXT_BUILTIN_ROOT, '', 1))
                self.assertIn(NEXT_FALLBACK + NEXT_BUILTIN_ROOT, changed)
                self.assertIn(self.signature, changed)
                self.assertFalse(self.prepare())
                self.assertEqual(changed, self.kbuild.read_text(encoding='utf-8'))

    @unittest.skipUnless(shutil.which('make'), 'GNU make is required for native Kbuild path replay')
    def test_real_make_covers_old_prefixed_absolute_in_tree_and_external_source_paths(self):
        output = self.root / 'common/out'
        old = self.probe(output, 'VPATH=')
        broken = self.probe(output, 'VPATH=..')
        self.assertIn('APP_HEADER=1', old)
        self.assertIn('APP_HEADER=0', broken)
        self.assertIn('KDP_HEADER=0', broken)
        absolute = ('VPATH=..', 'src=' + self.kernel.as_posix())
        module = ('VPATH=..', 'KBUILD_EXTMOD=' + self.kernel.as_posix())
        srcroot = ('VPATH=..', 'KBUILD_EXTMOD=' + (self.root / 'module-output').as_posix(),
                   'srcroot=' + self.kernel.as_posix())
        before = {options: self.probe(output, *options) for options in (absolute, module, srcroot)}
        self.prepare()
        for options in (('VPATH=',), ('VPATH=..',), absolute, module, srcroot):
            fixed = self.probe(output, *options)
            self.assertIn('APP_HEADER=1', fixed)
            self.assertIn('KDP_HEADER=1', fixed)
            if options in (module, srcroot):
                self.assertEqual(before[options], fixed)
        self.assertIn('APP_HEADER=1', self.probe(self.root / 'common', 'VPATH='))

    @unittest.skipUnless(shutil.which('make'), 'GNU make is required for the compiler include probe')
    def test_real_c_preprocessor_resolves_official_uapi_and_susfs_kdp_via_kbuild_flags(self):
        compiler = json.loads(os.environ.get('GKI_TEST_CC', 'null'))
        if compiler is None:
            native = next((shutil.which(name) for name in ('clang', 'gcc', 'cc') if shutil.which(name)), None)
            if not native:
                self.skipTest('A C compiler is required for native header dependency preprocessing')
            compiler = [native]
        output = self.root / 'common/out'
        def dependencies():
            data = self.probe(output, 'VPATH=..')
            flags = shlex.split(re.search(r'^HEADER_FLAGS=(.*)$', data, re.M)[1])
            if os.name == 'nt':
                # MSYS make emits POSIX drive paths; the compiler is a native process.
                flags = [re.sub(r'^-I/(?:cygdrive/)?([a-zA-Z])/', lambda match: '-I' + match[1] + ':/', flag)
                         for flag in flags]
            result = subprocess.run([*compiler, '-M', '-MG', '-MT', 'probe.o', '-x', 'c', '-', *flags],
                                    input='#include "uapi/app_profile.h"\n#include "klog.h"\n'
                                          '#include "ksu_samsung_kdp.h"\n',
                                    cwd=output, text=True, encoding='utf-8', capture_output=True, timeout=60)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return result.stdout.replace('\\\n', ' ').replace('\\', '/').split()
        broken = dependencies()
        for name in ('uapi/app_profile.h', 'klog.h', 'ksu_samsung_kdp.h'):
            self.assertIn(name, broken)
        self.prepare()
        fixed = dependencies()
        for name in ('uapi/app_profile.h', 'klog.h', 'ksu_samsung_kdp.h'):
            self.assertNotIn(name, fixed)
            self.assertTrue(any(path.endswith('/include/' + name) for path in fixed), fixed)

    def test_changed_official_header_or_unknown_current_layout_rejects_before_kbuild_write(self):
        original = self.kbuild.read_bytes()
        header = self.repository / 'uapi/app_profile.h'
        saved = header.read_bytes()
        header.write_bytes(saved + b'\n/* unreviewed */\n')
        with self.assertRaisesRegex(ValueError, 'frozen official Git blob'):
            self.prepare()
        self.assertEqual(original, self.kbuild.read_bytes())
        header.write_bytes(saved)
        self.kbuild.write_bytes(original.replace(b'KSU_KERNEL_DIR := $(srctree)/$(src)',
                                                 b'KSU_KERNEL_DIR := $(srcroot)'))
        changed = self.kbuild.read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed KernelSU-Next include-root layout'):
            self.prepare()
        self.assertEqual(changed, self.kbuild.read_bytes())

    def test_additional_root_assignments_and_earlier_include_cannot_override_the_reviewed_root(self):
        original = self.kbuild.read_bytes()
        for changed in (original + b'KSU_KERNEL_DIR := another/root\n', b'include another.mk\n' + original):
            with self.subTest(changed=changed):
                self.kbuild.write_bytes(changed)
                with self.assertRaisesRegex(ValueError, 'assignments or included makefiles'):
                    self.prepare()
                self.assertEqual(changed, self.kbuild.read_bytes())


if __name__ == '__main__':
    unittest.main()
