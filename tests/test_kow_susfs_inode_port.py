"""Check the new KowSU port's scope, preflight and idempotence."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import kow_susfs_compat as kow
from snapshot_ksu_source import temporary_directory


@unittest.skipUnless(shutil.which('git'), 'Git is required for the reviewed patch proof')
class KowSusfsInodePortTests(unittest.TestCase):
    def setUp(self):
        self.fixture = self.enterContext(temporary_directory(Path(__file__).parent, '.kow-proof-test-'))
        self.source = self.fixture / 'KernelSU'
        self.source.mkdir()
        self.git('init', '--quiet')
        self.kernel = self.source / 'kernel/native.c'
        self.uapi = self.source / 'uapi/native.h'
        self.kernel.parent.mkdir()
        self.uapi.parent.mkdir()
        self.kernel.write_bytes(b'int native(void) { return 1; }\n')
        self.uapi.write_bytes(b'int native(void);\n')
        self.commit('Reviewed native source')
        native_kernel = self.git('rev-parse', 'HEAD:kernel').strip().decode()
        native_uapi = self.git('rev-parse', 'HEAD:uapi').strip().decode()
        self.original = self.kernel.read_bytes()
        self.result = self.original + b'int susfs(void) { return native(); }\n'
        self.kernel.write_bytes(self.result)
        raw = self.git('diff', '--full-index', '--', 'kernel')
        self.git('add', 'kernel/native.c')
        patched = self.git('write-tree').strip().decode()
        patched_kernel = self.git('rev-parse', patched + ':kernel').strip().decode()
        self.git('restore', '--source=HEAD', '--staged', '--worktree', '--', 'kernel')
        self.patch = self.fixture / 'reviewed.patch'
        self.patch.write_bytes(raw)
        self.enterContext(patch.multiple(kow, PATCH=self.patch, KERNEL_TREE=native_kernel,
                                         UAPI_TREE=native_uapi, PATCHED_KERNEL_TREE=patched_kernel,
                                         PATCH_SHA256=hashlib.sha256(raw).hexdigest()))

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), '-c', 'core.autocrlf=false',
                                        '-c', 'core.hooksPath=', *args], timeout=30)

    def commit(self, message):
        self.git('add', '--all')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', message)

    def test_manager_only_update_reuses_reviewed_trees_without_touching_index(self):
        (self.source / 'manager.txt').write_bytes(b'New UI\n')
        self.commit('Manager only update')
        original_index_tree = self.git('write-tree')
        self.assertTrue(kow.apply_reviewed_kow_port(self.source))
        self.assertEqual(self.kernel.read_bytes(), self.result)
        self.assertTrue(kow.apply_reviewed_kow_port(self.source))
        self.assertEqual(self.kernel.read_bytes(), self.result)
        self.assertEqual(self.git('write-tree'), original_index_tree)
        self.git('apply', '--reverse', str(self.patch))
        self.assertEqual(self.kernel.read_bytes(), self.original)
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_unknown_uapi_commit_does_not_use_reviewed_patch(self):
        self.uapi.write_bytes(b'long native(void);\n')
        self.commit('Unreviewed native ABI')
        self.assertFalse(kow.apply_reviewed_kow_port(self.source))
        self.assertEqual(self.kernel.read_bytes(), self.original)

    def test_dirty_native_source_and_wrong_postimage_fail_before_writing(self):
        with patch.object(kow, 'PATCHED_KERNEL_TREE', '0' * 40):
            with self.assertRaisesRegex(ValueError, 'reviewed native result'):
                kow.apply_reviewed_kow_port(self.source)
        self.assertEqual(self.kernel.read_bytes(), self.original)
        dirty = self.original + b'int external_edit;\n'
        self.kernel.write_bytes(dirty)
        with self.assertRaisesRegex(ValueError, 'clean or already reviewed'):
            kow.apply_reviewed_kow_port(self.source)
        self.assertEqual(self.kernel.read_bytes(), dirty)


if __name__ == '__main__':
    unittest.main()
