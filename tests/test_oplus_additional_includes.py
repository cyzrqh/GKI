"""Replay all three failed native Kbuild layouts with make and a real C compiler."""
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
from oplus_ksu_compat import BUILTIN_ROOT, prepare_builtin_includes
from manager_variants import manager_profile
import test_oplus_ksu_compat as suki_tests

FIXTURE = ROOT / 'tests/fixtures/oplus-additional-includes'
MANAGERS = ('KernelSU', 'KowSU', 'FinaSU')


@unittest.skipUnless(shutil.which('git'), 'Git is required for frozen official header verification')
class OPlusAdditionalIncludeTests(unittest.TestCase):
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
        shutil.copyfile(FIXTURE / 'app_profile.h', self.repository / 'uapi/app_profile.h')
        shutil.copyfile(FIXTURE / 'klog.h', self.kernel / 'include/klog.h')
        self.kbuild = self.kernel / 'Kbuild'
        self.git('init', '-q')
        self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'core.symlinks', 'true')

    def native(self, variant):
        self.kbuild.write_bytes((FIXTURE / (variant + '-Kbuild')).read_bytes())
        self.commit()
        # Production sets these from this run's verified APK without changing includes.
        self.signature = '\nccflags-y += -DEXPECTED_SIZE=730 -DEXPECTED_HASH=paired_certificate -Werror\n'
        with self.kbuild.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(self.signature)

    def prepare(self, variant):
        return prepare_builtin_includes(self.root, self.reference, variant=variant)

    def probe(self, output, *options):
        output.mkdir(exist_ok=True)
        source = '..' if output.name == 'out' else '.'
        (output / 'probe.mk').write_text(
            'srctree := ' + source + '\nobjtree := .\nobj := drivers/kernelsu\n'
            'src := $(if $(VPATH),$(VPATH)/)$(obj)\n'
            'include $(srctree)/drivers/kernelsu/Kbuild\n'
            '$(info HEADER_FLAGS=$(filter -I$(KSU_KERNEL_DIR)%,$(ccflags-y)))\n'
            '$(info APP_HEADER=$(if $(wildcard $(KSU_KERNEL_DIR)/include/uapi/app_profile.h),1,0))\n'
            '$(info KLOG_HEADER=$(if $(wildcard $(KSU_KERNEL_DIR)/include/klog.h),1,0))\n'
            '.PHONY: probe\nprobe:;\n', encoding='utf-8', newline='\n')
        # Prevent the native version detector from performing a network fetch;
        # it does not participate in include-root resolution.
        result = subprocess.run(['make', '--no-print-directory', '-f', 'probe.mk',
                                 'GIT=', 'KSU_GIT_VERSION_VALID=1', 'KSU_GIT_VERSION=100',
                                 *options, 'probe'], cwd=output, text=True, encoding='utf-8',
                                capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout

    def test_fixtures_are_the_exact_failed_full_kbuild_and_official_headers(self):
        info = json.loads((FIXTURE / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(set(MANAGERS), set(info['sources']))
        for variant, source in info['sources'].items():
            self.assertEqual(manager_profile(variant)['repository'], source['repository'])
            self.assertRegex(source['commit'], r'^[0-9a-f]{40}$')
        for name, expected in info['sha256'].items():
            self.assertEqual(expected, hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest())

    def test_each_manager_preserves_all_native_objects_preinit_flags_and_paired_signature(self):
        for variant in MANAGERS:
            with self.subTest(variant=variant):
                self.native(variant)
                original = self.kbuild.read_text(encoding='utf-8')
                self.assertTrue(self.prepare(variant))
                changed = self.kbuild.read_text(encoding='utf-8')
                self.assertEqual(original, changed.replace(BUILTIN_ROOT, '', 1))
                self.assertIn(self.signature, changed)
                self.assertFalse(self.prepare(variant))
                self.assertEqual(changed, self.kbuild.read_text(encoding='utf-8'))

    @unittest.skipUnless(shutil.which('make'), 'GNU make is required for native Kbuild path replay')
    def test_all_managers_resolve_old_prefixed_absolute_and_external_roots(self):
        output = self.root / 'common/out'
        for variant in MANAGERS:
            with self.subTest(variant=variant):
                self.native(variant)
                self.assertIn('APP_HEADER=1', self.probe(output, 'VPATH='))
                self.assertIn('APP_HEADER=0', self.probe(output, 'VPATH=..'))
                external = ('VPATH=..', 'KBUILD_EXTMOD=' + self.kernel.as_posix(),
                            'srcroot=' + self.kernel.as_posix())
                before = self.probe(output, *external)
                self.assertIn('APP_HEADER=1', before)
                self.prepare(variant)
                for options in (('VPATH=',), ('VPATH=..',),
                                ('VPATH=..', 'src=' + self.kernel.as_posix()), external):
                    result = self.probe(output, *options)
                    self.assertIn('APP_HEADER=1', result)
                    self.assertIn('KLOG_HEADER=1', result)
                    if options == external:
                        self.assertEqual(before, result)
                self.assertIn('APP_HEADER=1', self.probe(self.root / 'common', 'VPATH='))

    @unittest.skipUnless(shutil.which('make'), 'GNU make is required for real C include compilation')
    def test_real_c_compile_fails_before_and_succeeds_with_all_three_native_flags(self):
        compiler = json.loads(os.environ.get('GKI_TEST_CC', 'null'))
        if compiler is None:
            native = next((shutil.which(name) for name in ('clang', 'gcc', 'cc') if shutil.which(name)), None)
            if not native:
                self.skipTest('A C compiler is required for native header compilation')
            compiler = [native]
        output = self.root / 'common/out'
        linux = self.root / 'common/include/linux'
        linux.mkdir(parents=True)
        # Only the kernel's primitive types/logging are stubbed. The manager
        # UAPI and klog headers, and all include flags, are the real files.
        (linux / 'types.h').write_text('typedef int __s32; typedef unsigned int __u32;\n'
                                       'typedef unsigned long long __u64; typedef _Bool bool;\n')
        (linux / 'printk.h').write_text('/* No logging is emitted by this header probe. */\n')
        def compile():
            data = self.probe(output, 'VPATH=..')
            flags = shlex.split(re.search(r'^HEADER_FLAGS=(.*)$', data, re.M)[1])
            if os.name == 'nt':
                flags = [re.sub(r'^-I/(?:cygdrive/)?([a-zA-Z])/',
                                lambda match: '-I' + match[1] + ':/', flag) for flag in flags]
            return subprocess.run([*compiler, '-std=c11', '-Werror', '-I../include', *flags,
                                   '-c', '-x', 'c', '-', '-o', 'probe.o'], cwd=output,
                                  input='#include "uapi/app_profile.h"\n#include "klog.h"\n'
                                        '_Static_assert(KSU_APP_PROFILE_VER == 4, "native UAPI");\n'
                                        'int check(struct app_profile *p) { return p->allow_su; }\n',
                                  text=True, encoding='utf-8', capture_output=True, timeout=60)
        for variant in MANAGERS:
            with self.subTest(variant=variant):
                self.native(variant)
                broken = compile()
                self.assertNotEqual(0, broken.returncode)
                self.assertIn('uapi/app_profile.h', broken.stderr)
                self.prepare(variant)
                fixed = compile()
                self.assertEqual(0, fixed.returncode, fixed.stdout + fixed.stderr)
                self.assertTrue((output / 'probe.o').is_file())

    def test_unknown_assignment_or_include_and_modified_official_header_stop_before_write(self):
        for variant in MANAGERS:
            with self.subTest(variant=variant):
                self.native(variant)
                original = self.kbuild.read_bytes()
                for changed in (original + b'KSU_KERNEL_DIR := another/root\n',
                                b'include another.mk\n' + original):
                    self.kbuild.write_bytes(changed)
                    with self.assertRaisesRegex(ValueError, 'assignments or included makefiles'):
                        self.prepare(variant)
                    self.assertEqual(changed, self.kbuild.read_bytes())
                self.kbuild.write_bytes(original)
                header = self.repository / 'uapi/app_profile.h'
                content = header.read_bytes()
                header.write_bytes(content + b'\n/* unreviewed */\n')
                with self.assertRaisesRegex(ValueError, 'frozen official Git blob'):
                    self.prepare(variant)
                self.assertEqual(original, self.kbuild.read_bytes())
                header.write_bytes(content)

    def test_all_three_older_flat_header_layouts_are_left_untouched(self):
        for variant in MANAGERS:
            with self.subTest(variant=variant):
                self.kbuild.write_text('ccflags-y += -I$(srctree)/$(src)\n', encoding='utf-8', newline='\n')
                # The same fixture may already be committed on this iteration.
                self.git('add', '--all')
                self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                         'commit', '--allow-empty', '-qm', 'Legacy flat header layout')
                self.reference = self.git('rev-parse', 'HEAD')
                before = self.kbuild.read_bytes()
                self.assertFalse(self.prepare(variant))
                self.assertEqual(before, self.kbuild.read_bytes())


if __name__ == '__main__':
    unittest.main()
