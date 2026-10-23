import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import historical_susfs23 as historical

FIXTURES = ROOT / 'tests/fixtures/historical-susfs23'


class HistoricalSusfs23Tests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.historical-susfs23-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.common = self.root / 'common'
        self.target = self.common / 'fs/notify/fdinfo.c'
        self.target.parent.mkdir(parents=True)
        subprocess.run(['git', 'init', '--quiet'], cwd=self.common, check=True,
                       capture_output=True)

    def fixture(self, version='5.15.41', month='2022-06'):
        original = (FIXTURES / f'fdinfo-{version}-{month}.c').read_bytes()
        self.target.write_bytes(original)
        return original

    def phase(self, phase, version='5.15.41'):
        kernel, sublevel = version.rsplit('.', 1)
        return historical.run_phase(self.root, 'v2.3.0', kernel, sublevel, phase)

    def patch(self, name, success=True):
        result = subprocess.run(['git', '-c', 'core.autocrlf=false', 'apply', '--reject', str(FIXTURES / name)],
                                cwd=self.common, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_real_old_android_sources_retain_both_23_hooks_and_native_masks(self):
        for version, family in (('5.10.110', 'android12-5.10'), ('5.15.41', 'android13-5.15')):
            with self.subTest(version=version):
                state_path = self.root / historical.STATE_NAME
                if state_path.exists():
                    state_path.unlink()
                original = self.fixture(version)
                self.phase('prepare', version)
                prepared = self.target.read_bytes()
                self.assertNotEqual(original, prepared)
                self.phase('prepare', version)
                self.assertEqual(prepared, self.target.read_bytes())
                self.patch(f'fdinfo-{family}-v2.3.0.patch')
                self.phase('finish', version)
                restored = self.target.read_text(encoding='utf-8')
                self.assertIn('if (susfs_is_inode_sus_kstat(inode, &is_fuse))', restored)
                self.assertIn('susfs_sus_kstat_spoof_inotify_fdinfo(&ino, &dev);', restored)
                self.assertIn('struct mount *mnt = real_mount(file->f_path.mnt);', restored)
                body = historical.inotify_body(restored)
                self.assertEqual(1, body.count('u32 mask = mark->mask & IN_ALL_EVENTS;'))
                self.assertEqual(3, body.count('mask, mark->ignored_mask'))
                self.assertEqual(3, body.count('ignored_mask:%x'))
                self.assertNotIn('inotify_mark_user_mask(mark)', body)
                state = json.loads(state_path.read_text(encoding='utf-8'))
                self.assertEqual(original.decode(), state['original_source'])
                self.phase('finish', version)
                self.phase('prepare', version)
                self.assertEqual(restored, self.target.read_text(encoding='utf-8'))
                self.assertFalse(self.target.with_suffix('.c.rej').exists())

    def test_real_20_fallback_can_apply_but_is_rejected_as_missing_23_kstat(self):
        self.fixture()
        # Prepare records the real source, but recreate the former workflow's
        # unadapted application order to reproduce its misleading success.
        self.phase('prepare')
        self.fixture()
        self.patch('fdinfo-android13-5.15-v2.3.0.patch', success=False)
        self.patch('fdinfo-v2.0.0-fallback.patch')
        partial = self.target.read_bytes()
        self.assertIn(b'susfs_is_current_proc_umounted()', partial)
        self.assertNotIn(b'if (susfs_is_inode_sus_kstat(inode, &is_fuse))', partial)
        with self.assertRaisesRegex(ValueError, 'Incomplete SUSFS 2.3'):
            self.phase('finish')
        self.assertEqual(partial, self.target.read_bytes())

    def test_modern_official_source_needs_no_context_or_event_mask_rewrite(self):
        original = self.fixture('5.15.74', '2022-12')
        self.phase('prepare', '5.15.74')
        self.assertEqual(original, self.target.read_bytes())
        self.patch('fdinfo-android13-5.15-v2.3.0.patch')
        patched = self.target.read_bytes()
        self.phase('finish', '5.15.74')
        self.assertEqual(patched, self.target.read_bytes())
        self.assertIn(b'susfs_sus_kstat_spoof_inotify_fdinfo(&ino, &dev);', patched)
        self.assertIn(b'real_mount(file->f_path.mnt)', patched)

    def test_unknown_original_context_fails_without_changing_or_backing_up_source(self):
        self.fixture()
        unknown = self.target.read_bytes().replace(b'mask, mark->ignored_mask', b'mask, unknown_mask')
        self.target.write_bytes(unknown)
        with self.assertRaisesRegex(ValueError, 'Unknown fdinfo source layout'):
            self.phase('prepare')
        self.assertEqual(unknown, self.target.read_bytes())
        self.assertFalse((self.root / historical.STATE_NAME).exists())

    def test_finish_before_official_patch_is_an_error_and_preserves_the_prepared_source(self):
        self.fixture()
        self.phase('prepare')
        prepared = self.target.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Incomplete SUSFS 2.3'):
            self.phase('finish')
        self.assertEqual(prepared, self.target.read_bytes())

    def test_finish_requires_the_original_source_backup(self):
        self.fixture()
        with self.assertRaisesRegex(ValueError, 'prepare must precede'):
            self.phase('finish')

    def test_changed_output_branches_stop_before_native_mask_rewrite(self):
        self.fixture()
        self.phase('prepare')
        self.patch('fdinfo-android13-5.15-v2.3.0.patch')
        modified = self.target.read_bytes().replace(b'ignored_mask:0', b'ignored_mask:unknown', 1)
        self.target.write_bytes(modified)
        with self.assertRaisesRegex(ValueError, 'Unknown SUSFS 2.3 fdinfo output branches'):
            self.phase('finish')
        self.assertEqual(modified, self.target.read_bytes())

    def test_mismatched_backup_cannot_be_reused_by_another_kernel_target(self):
        self.fixture()
        self.phase('prepare')
        with self.assertRaisesRegex(ValueError, 'mismatched historical fdinfo backup'):
            self.phase('prepare', '5.15.74')

    def test_other_susfs_versions_and_kernel_families_are_untouched(self):
        original = self.fixture()
        for version, kernel in (('2.2.0', '5.15'), ('2.3.0', '6.1'), ('2.3.0', '6.12')):
            for phase in ('prepare', 'finish'):
                with self.subTest(version=version, kernel=kernel, phase=phase):
                    historical.run_phase(self.root, version, kernel, '41', phase)
                    self.assertEqual(original, self.target.read_bytes())
        self.assertFalse((self.root / historical.STATE_NAME).exists())

    def test_modern_changed_event_format_is_rejected_without_rewriting_the_source(self):
        self.fixture('5.15.74', '2022-12')
        self.phase('prepare', '5.15.74')
        self.patch('fdinfo-android13-5.15-v2.3.0.patch')
        changed = self.target.read_bytes().replace(b'ignored_mask:0', b'ignored_mask:unknown', 1)
        self.target.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, 'Modern fdinfo event-mask semantics changed'):
            self.phase('finish', '5.15.74')
        self.assertEqual(changed, self.target.read_bytes())

    def test_cli_reports_missing_hooks_as_failure(self):
        self.fixture()
        self.phase('prepare')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            result = historical.main(['--kernel-root', str(self.root), '--version', '2.3.0',
                                      '--kernel', '5.15', '--sublevel', '41', '--phase', 'finish'])
        self.assertEqual(1, result)
        self.assertIn('Incomplete SUSFS 2.3', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
