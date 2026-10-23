from pathlib import Path
import os
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import integrate_sukisu as setup


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class IntegrateSukiSUTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.sukisu-local-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup_directory)
        self.repository = self.root / 'KernelSU'
        self.repository.mkdir()
        (self.repository / 'kernel').mkdir()
        (self.repository / 'kernel/Kbuild').write_bytes(b'obj-y += kernel.o\n')
        self.git('init', '--quiet')
        self.git('add', 'kernel/Kbuild')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '--quiet', '-m', 'source')
        self.commit = self.git('rev-parse', 'HEAD')
        self.drivers = self.root / 'common/drivers'
        self.drivers.mkdir(parents=True)
        self.makefile = self.drivers / 'Makefile'
        self.kconfig = self.drivers / 'Kconfig'
        self.makefile.write_bytes(b'obj-y += base/\n')
        self.kconfig.write_bytes(b'menu "Drivers"\nsource "drivers/base/Kconfig"\nendmenu\n')

    def cleanup_directory(self):
        target = self.root.resolve()
        if target.parent != (ROOT / 'tests').resolve() or not target.name.startswith('.sukisu-local-'):
            raise ValueError('Unexpected test cleanup target')

        def remove_readonly(function, name, error):
            Path(name).chmod(stat.S_IWRITE)
            function(name)

        shutil.rmtree(target, onexc=remove_readonly)

    def git(self, *arguments):
        return subprocess.run(['git', '-C', str(self.repository), *arguments],
                              check=True, capture_output=True, text=True).stdout.strip()

    def symlink(self, path, target, *, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows user cannot create symlinks')
            raise

    def run_integration(self, **arguments):
        try:
            return setup.integrate(self.root, self.commit, **arguments)
        except OSError as error:
            if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows user cannot create symlinks')
            raise

    def assert_failure_preserves_drivers(self, expected=ValueError, **arguments):
        before = {path: path.read_bytes() for path in (self.makefile, self.kconfig) if path.is_file()}
        with self.assertRaises(expected):
            setup.integrate(arguments.pop('kernel_root', self.root),
                            arguments.pop('expected_ref', self.commit), **arguments)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_integrates_relative_driver_link_and_keeps_git_checkout_unchanged(self):
        git_state = self.git('status', '--porcelain')
        index = (self.repository / '.git/index').read_bytes()
        env = self.root / 'environment'
        result = self.run_integration(github_env=env)
        link = self.drivers / 'kernelsu'
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), os.path.relpath(self.repository / 'kernel', self.drivers))
        self.assertFalse(os.path.isabs(os.readlink(link)))
        self.assertEqual(link.resolve(), self.repository / 'kernel')
        self.assertEqual(self.makefile.read_text(), 'obj-y += base/\n' + setup.MAKE_ENTRY + '\n')
        self.assertEqual(self.kconfig.read_text(), 'menu "Drivers"\nsource "drivers/base/Kconfig"\n'
                         + setup.KCONFIG_ENTRY + '\nendmenu\n')
        self.assertEqual(result.commit, self.commit)
        self.assertRegex(result.commit_date, setup.DATE)
        self.assertEqual(env.read_text(), f'KSU_LATEST_COMMIT_DATE={result.commit_date}\n')
        self.assertEqual(self.git('status', '--porcelain'), git_state)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.commit)
        self.assertEqual((self.repository / '.git/index').read_bytes(), index)
        self.assertFalse(self.git('remote'))

    def test_existing_compatible_integration_is_idempotent_and_preserves_crlf(self):
        self.makefile.write_bytes(b'# kernelsu comment\r\nobj-$(CONFIG_KSU)\t+= kernelsu/ # keep\r\n')
        self.kconfig.write_bytes(b'menu "Drivers"\r\n  source "drivers/kernelsu/Kconfig" # keep\r\nendmenu\r\n')
        self.symlink(self.drivers / 'kernelsu', os.path.relpath(self.repository / 'kernel', self.drivers),
                     directory=True)
        env = self.root / 'environment'
        env.write_bytes(b'OTHER=value')
        self.run_integration(github_env=env)
        before = [path.read_bytes() for path in (self.makefile, self.kconfig, env)]
        with patch.object(setup, 'stage', side_effect=AssertionError('unnecessary file write')):
            self.run_integration(github_env=env)
        self.assertEqual([path.read_bytes() for path in (self.makefile, self.kconfig, env)], before)
        self.assertTrue(before[2].startswith(b'OTHER=value\nKSU_LATEST_COMMIT_DATE='))

    def test_root_drivers_fallback_and_last_endmenu_position(self):
        fallback = self.root / 'drivers'
        self.drivers.rename(fallback)
        (fallback / 'Kconfig').write_bytes(b'menu "Outer"\nmenu "Inner"\nendmenu\nendmenu')
        result = self.run_integration()
        self.assertEqual(result.drivers, fallback)
        self.assertEqual((fallback / 'Kconfig').read_text(),
                         'menu "Outer"\nmenu "Inner"\nendmenu\n' + setup.KCONFIG_ENTRY + '\nendmenu')

    def test_common_drivers_has_priority(self):
        other = self.root / 'drivers'
        other.mkdir()
        (other / 'Makefile').write_bytes(b'other\n')
        (other / 'Kconfig').write_bytes(b'other\n')
        self.assertEqual(self.run_integration().drivers, self.drivers)
        self.assertEqual((other / 'Makefile').read_bytes(), b'other\n')
        self.assertEqual((other / 'Kconfig').read_bytes(), b'other\n')

    def test_rejects_wrong_or_nonimmutable_commit_before_writing(self):
        for expected_ref in ('0' * 40, 'main', '--help', self.commit + '\n', '../HEAD'):
            with self.subTest(ref=expected_ref):
                self.assert_failure_preserves_drivers(expected_ref=expected_ref)
        self.assertFalse(os.path.lexists(self.drivers / 'kernelsu'))

    def test_rejects_parent_traversal(self):
        self.assert_failure_preserves_drivers(kernel_root=self.root / 'common/..')

    def test_rejects_missing_drivers_or_kbuild(self):
        self.drivers.rename(self.root / 'backup')
        self.assert_failure_preserves_drivers(expected=FileNotFoundError)
        (self.root / 'backup').rename(self.drivers)
        (self.repository / 'kernel/Kbuild').unlink()
        self.assert_failure_preserves_drivers(expected=FileNotFoundError)

    def test_rejects_nonrepository_or_git_file(self):
        (self.repository / '.git').rename(self.repository / '.git-backup')
        (self.repository / '.git').write_text('gitdir: elsewhere\n')
        self.assert_failure_preserves_drivers()

    def test_driver_text_changes_preserve_newlines_and_only_insert_once(self):
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=newline):
                original = ('obj-y += base/' + newline).encode()
                updated = setup.add_make_entry(original)
                self.assertEqual(updated, original + (setup.MAKE_ENTRY + newline).encode())
                self.assertEqual(setup.add_make_entry(updated), updated)
                original = ('menu "Outer"' + newline + 'menu "Inner"' + newline
                            + 'endmenu' + newline + 'endmenu').encode()
                updated = setup.add_kconfig_entry(original)
                self.assertEqual(updated, ('menu "Outer"' + newline + 'menu "Inner"' + newline
                                          + 'endmenu' + newline + setup.KCONFIG_ENTRY + newline
                                          + 'endmenu').encode())
                self.assertEqual(setup.add_kconfig_entry(updated), updated)

    def test_kconfig_without_own_menu_includes_driver_at_end(self):
        original = b'source "drivers/base/Kconfig"'
        self.assertEqual(setup.add_kconfig_entry(original), original + b'\n'
                         + setup.KCONFIG_ENTRY.encode() + b'\n')

    def test_refuses_unrelated_kernelsu_directory(self):
        (self.drivers / 'kernelsu').mkdir()
        marker = self.drivers / 'kernelsu/keep'
        marker.write_text('mine')
        self.assert_failure_preserves_drivers()
        self.assertEqual(marker.read_text(), 'mine')

    def test_refuses_unrelated_or_absolute_existing_link(self):
        link = self.drivers / 'kernelsu'
        other = self.root / 'unrelated'
        other.mkdir()
        for target in (os.path.relpath(other, self.drivers), str(self.repository / 'kernel')):
            with self.subTest(target=target):
                self.symlink(link, target, directory=True)
                try:
                    self.assert_failure_preserves_drivers()
                    self.assertEqual(os.readlink(link), target)
                finally:
                    link.unlink()

    def test_rejects_driver_file_or_source_directory_symlinks(self):
        for path, directory in ((self.makefile, False), (self.kconfig, False),
                                (self.repository / 'kernel', True)):
            with self.subTest(path=path):
                preserved = path.with_name(path.name + '-original')
                path.rename(preserved)
                try:
                    self.symlink(path, preserved.name, directory=directory)
                    self.assert_failure_preserves_drivers()
                finally:
                    if path.is_symlink():
                        path.unlink()
                    preserved.rename(path)

    def test_duplicate_or_foreign_entries_do_not_partially_modify_other_file(self):
        for filename, content in (
            ('Makefile', setup.MAKE_ENTRY + '\n' + setup.MAKE_ENTRY + '\n'),
            ('Makefile', 'obj-y += kernelsu/\n'),
            ('Makefile', 'obj-$(CONFIG_KSU) += other/\n'),
            ('Kconfig', setup.KCONFIG_ENTRY + '\n' + setup.KCONFIG_ENTRY + '\n'),
            ('Kconfig', 'source "../KernelSU/kernel/Kconfig"\n'),
        ):
            with self.subTest(filename=filename, content=content):
                path = self.drivers / filename
                original = path.read_bytes()
                path.write_text(content)
                try:
                    self.assert_failure_preserves_drivers()
                    self.assertFalse(os.path.lexists(self.drivers / 'kernelsu'))
                finally:
                    path.write_bytes(original)

    def test_symlink_creation_failure_leaves_files_and_env_unmodified(self):
        env = self.root / 'environment'
        env.write_bytes(b'OTHER=value\n')
        with patch.object(Path, 'symlink_to', side_effect=PermissionError('symlinks denied')):
            self.assert_failure_preserves_drivers(expected=PermissionError, github_env=env)
        self.assertEqual(env.read_bytes(), b'OTHER=value\n')
        self.assertFalse(os.path.lexists(self.drivers / 'kernelsu'))
        self.assertFalse(list(self.drivers.glob('.*.tmp')))
        self.assertFalse(list(self.root.glob('.*.tmp')))

    def test_no_network_or_git_mutations_are_invoked(self):
        original = setup.git
        observed = []

        def verify_only(repository, *arguments):
            observed.append(arguments)
            self.assertIn(arguments[0], ('rev-parse', 'log'))
            return original(repository, *arguments)

        with patch.object(setup, 'git', side_effect=verify_only):
            self.run_integration()
        self.assertEqual([arguments[0] for arguments in observed], ['rev-parse', 'rev-parse', 'log'])


if __name__ == '__main__':
    unittest.main()
