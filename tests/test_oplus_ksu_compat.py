"""Use real GNU make to reproduce the vendor 6.12 double source prefix."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from oplus_ksu_compat import BUILTIN_ROOT, prepare_builtin_includes

FIXTURE = ROOT / 'tests/fixtures/oplus-ksu612'


def remove_temporary(root):
    root = root.resolve()
    if root.parent != ROOT / 'tests' or not root.name.startswith('.oplus-ksu-include-'):
        raise ValueError('Refusing cleanup outside this test temporary directory')
    def writable(function, name, error):
        target = Path(name)
        if not target.resolve().is_relative_to(root):
            raise ValueError('Test cleanup escaped its own temporary directory')
        target.chmod(stat.S_IWRITE)
        function(name)
    shutil.rmtree(root, onexc=writable)


@unittest.skipUnless(shutil.which('git'), 'Git is required for frozen header verification')
class OPlusKsuIncludeTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.oplus-ksu-include-' + uuid.uuid4().hex)
        self.kernel = self.root / 'KernelSU/kernel'
        self.repository = self.kernel.parent
        (self.kernel / 'include').mkdir(parents=True)
        (self.repository / 'uapi').mkdir()
        self.drivers = self.root / 'common/drivers'
        self.drivers.mkdir(parents=True)
        self.addCleanup(remove_temporary, self.root)
        self.alias(self.kernel / 'include/uapi', '../../uapi')
        self.alias(self.drivers / 'kernelsu', '../../KernelSU/kernel')
        self.kbuild = self.kernel / 'Kbuild'
        self.kbuild.write_bytes((FIXTURE / 'Kbuild-include.inc').read_bytes())
        shutil.copyfile(FIXTURE / 'app_profile.h', self.repository / 'uapi/app_profile.h')
        shutil.copyfile(FIXTURE / 'klog.h', self.kernel / 'include/klog.h')
        self.git('init', '-q')
        self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'core.symlinks', 'true')
        self.commit()
        self.signature = '\nccflags-y += -DEXPECTED_HASH=paired_certificate -DKSU_VERSION=41095 -Werror\n'
        with self.kbuild.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(self.signature)

    def alias(self, path, target):
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                raise
            # NTFS junctions let this Windows host run the real make path test
            # without requiring the system-wide symbolic-link privilege.
            resolved = (path.parent / target).resolve(strict=True)
            self.assertTrue(resolved.is_relative_to(self.root))
            result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(path), str(resolved)],
                                    capture_output=True, timeout=20)
            if result.returncode:
                self.skipTest('This host cannot create a checked local source alias')
            self.assertEqual(resolved, path.resolve(strict=True))

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repository), *args], text=True,
                                capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def commit(self):
        self.git('add', '--all')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Native source inputs')
        self.reference = self.git('rev-parse', 'HEAD')

    def test_real_fixture_headers_and_source_commit_are_checksummed(self):
        info = json.loads((FIXTURE / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual('96ceb4a42c50e27199a094264f62cfbafee77fa0', info['commit'])
        for name, expected in info['sha256'].items():
            self.assertEqual(expected, hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest())

    def test_builtin_root_preserves_the_official_uapi_link_signature_and_all_flags(self):
        original = self.kbuild.read_text(encoding='utf-8')
        self.assertTrue(prepare_builtin_includes(self.root, self.reference))
        changed = self.kbuild.read_text(encoding='utf-8')
        self.assertEqual(original, changed.replace(BUILTIN_ROOT, '', 1))
        self.assertIn(self.signature, changed)
        self.assertEqual(self.repository / 'uapi', (self.kernel / 'include/uapi').resolve(strict=True))
        self.assertFalse(prepare_builtin_includes(self.root, self.reference))
        self.assertEqual(changed, self.kbuild.read_text(encoding='utf-8'))

    @unittest.skipUnless(shutil.which('make'), 'GNU make is required for the native Kbuild path probe')
    def test_real_gnu_make_finds_both_headers_after_fix_and_keeps_external_module_behavior(self):
        output = self.root / 'common/out'
        output.mkdir()
        probe = output / 'probe.mk'
        probe.write_text('srctree := ..\nobjtree := .\nobj := drivers/kernelsu\n'
                         'src := $(if $(VPATH),$(VPATH)/)$(obj)\n'
                         'include ../drivers/kernelsu/Kbuild\n'
                         '$(info APP_HEADER=$(if $(wildcard $(KSU_KERNEL_DIR)/include/uapi/app_profile.h),1,0))\n'
                         '$(info KLOG_HEADER=$(if $(wildcard $(KSU_KERNEL_DIR)/include/klog.h),1,0))\n'
                         'all:;\n', encoding='utf-8', newline='\n')
        def run(*options):
            result = subprocess.run(['make', '--no-print-directory', '-f', 'probe.mk', *options], cwd=output,
                                    text=True, capture_output=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return result.stdout
        # Old Kbuild passes an unprefixed src: its unchanged fallback works.
        old = run('VPATH=')
        self.assertIn('APP_HEADER=1', old)
        # Vendor 6.12 sets VPATH=.., so srctree/src points outside common.
        broken = run('VPATH=..')
        self.assertIn('APP_HEADER=0', broken)
        self.assertIn('KLOG_HEADER=0', broken)
        external_args = ('VPATH=..', 'KBUILD_EXTMOD=1', 'srcroot=' + self.kernel.as_posix())
        external_before = run(*external_args)
        self.assertIn('APP_HEADER=1', external_before)
        prepare_builtin_includes(self.root, self.reference)
        fixed = run('VPATH=..')
        self.assertIn('APP_HEADER=1', fixed)
        self.assertIn('KLOG_HEADER=1', fixed)
        self.assertEqual(external_before, run(*external_args))

    def test_changed_or_untracked_headers_are_rejected_without_kbuild_writes(self):
        original = self.kbuild.read_bytes()
        path = self.repository / 'uapi/app_profile.h'
        path.write_bytes(path.read_bytes() + b'\n/* unreviewed */\n')
        with self.assertRaisesRegex(ValueError, 'frozen official Git blob'):
            prepare_builtin_includes(self.root, self.reference)
        self.assertEqual(original, self.kbuild.read_bytes())

    def test_uapi_link_must_remain_inside_the_frozen_repository(self):
        original = self.kbuild.read_bytes()
        outside = self.root / 'outside-uapi'
        outside.mkdir()
        shutil.copyfile(FIXTURE / 'app_profile.h', outside / 'app_profile.h')
        link = self.kernel / 'include/uapi'
        if hasattr(link, 'is_junction') and link.is_junction():
            link.rmdir()
        else:
            link.unlink()
        self.alias(link, '../../../outside-uapi')
        with self.assertRaisesRegex(ValueError, 'own bounded official source'):
            prepare_builtin_includes(self.root, self.reference)
        self.assertEqual(original, self.kbuild.read_bytes())

    def test_unknown_current_or_native_kbuild_layout_is_not_silently_replaced(self):
        self.kbuild.write_text(self.kbuild.read_text().replace('ifndef KSU_KERNEL_DIR', 'ifdef KSU_KERNEL_DIR'))
        original = self.kbuild.read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed SukiSU include-root layout'):
            prepare_builtin_includes(self.root, self.reference)
        self.assertEqual(original, self.kbuild.read_bytes())

    def test_older_stable_flat_header_layout_is_left_untouched(self):
        self.kbuild.write_text('ccflags-y += -I$(srctree)/$(src)\n', encoding='utf-8', newline='\n')
        self.commit()
        original = self.kbuild.read_bytes()
        self.assertFalse(prepare_builtin_includes(self.root, self.reference))
        self.assertEqual(original, self.kbuild.read_bytes())


if __name__ == '__main__':
    unittest.main()
