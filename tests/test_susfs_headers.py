import hashlib
from pathlib import Path
import shutil
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import ensure_susfs_headers as headers


class SusfsHeaderTests(unittest.TestCase):
    HEADER = '''#define SUSFS_IS_INODE_SUS_MAP(inode) test_bit(39, &(inode)->i_mapping->flags)
#define SUSFS_IS_INODE_OPEN_REDIRECT(inode) test_bit(36, &(inode)->i_mapping->flags)
#define SUSFS_IS_INODE_OPEN_REDIRECT_WITHOUT_UID_CHECK(inode) test_bit(36, &(inode)->i_mapping->flags)
'''
    TASK = '''// SPDX-License-Identifier: GPL-2.0
#include <linux/mm.h>
#include "internal.h"

void show_map(struct inode *inode) {
#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT
    if (SUSFS_IS_INODE_OPEN_REDIRECT(inode)) redirect(inode);
#endif
#ifdef CONFIG_KSU_SUSFS_SUS_MAP
    if (SUSFS_IS_INODE_SUS_MAP(inode)) hide(inode);
#endif
}
'''
    NAMESPACE = '''#include <linux/mnt_idmapping.h>
#include "internal.h"

void clone_mount(void) {
#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT
    if (susfs_is_current_ksu_domain()) flags |= CL_COPY_MNT_NS;
    if (static_branch_unlikely(&susfs_is_sdcard_android_data_not_decrypted)) assign_id();
#endif
}
'''

    def setUp(self):
        self.root = ROOT / 'tests' / f'.susfs-headers-{uuid.uuid4().hex}'
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.source('common/include/linux/susfs_def.h', self.HEADER)
        (self.root / 'common/fs').mkdir()

    def source(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='\n')
        return path

    def test_missing_task_mmu_header_is_restored_without_changing_functionality(self):
        path = self.source('common/fs/proc/task_mmu.c', self.TASK)
        self.assertEqual((path,), headers.ensure_headers(self.root))
        updated = path.read_text(encoding='utf-8')
        block = '\n#ifdef CONFIG_KSU_SUSFS\n#include <linux/susfs_def.h>\n#endif\n'
        self.assertEqual(self.TASK, updated.replace(block, '', 1))
        self.assertLess(updated.index('#include "internal.h"'), updated.index('#include <linux/susfs_def.h>'))

    def test_real_macro_uses_in_fs_and_mm_receive_the_same_dependency(self):
        paths = [self.source(name, self.TASK) for name in
                 ('common/fs/proc/base.c', 'common/fs/namei.c', 'common/mm/memory.c')]
        self.assertEqual(set(paths), set(headers.ensure_headers(self.root)))
        for path in paths:
            self.assertIn('#include <linux/susfs_def.h>', path.read_text(encoding='utf-8'))

    def test_second_run_preserves_bytes_and_modification_time(self):
        path = self.source('common/fs/proc/task_mmu.c', self.TASK)
        headers.ensure_headers(self.root)
        state = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
        self.assertEqual((), headers.ensure_headers(self.root))
        self.assertEqual(state, (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns))

    def test_comments_and_literals_do_not_create_false_macro_dependencies(self):
        path = self.source('common/fs/proc/task_mmu.c',
                           '#include <linux/mm.h>\n/* SUSFS_IS_INODE_SUS_MAP(inode) */\n'
                           'const char *text = "SUSFS_IS_INODE_OPEN_REDIRECT(inode)";\n')
        before = path.read_bytes()
        self.assertEqual((), headers.ensure_headers(self.root))
        self.assertEqual(before, path.read_bytes())

    def test_commented_out_header_is_not_treated_as_a_real_include(self):
        text = '/*\n#include <linux/susfs_def.h>\n*/\n' + self.TASK
        path = self.source('common/fs/proc/task_mmu.c', text)
        headers.ensure_headers(self.root)
        self.assertIn('#ifdef CONFIG_KSU_SUSFS\n#include <linux/susfs_def.h>', path.read_text(encoding='utf-8'))

    def test_namespace_rejected_header_hunk_restores_real_declarations_and_bit(self):
        path = self.source('common/fs/namespace.c', self.NAMESPACE)
        self.assertEqual((path,), headers.ensure_headers(self.root))
        text = path.read_text(encoding='utf-8')
        self.assertIn('#include <linux/susfs_def.h>', text)
        for _, _, declaration in headers.NAMESPACE_DECLARATIONS:
            self.assertEqual(1, text.count(declaration))
        self.assertTrue(text.endswith(self.NAMESPACE.split('\n\n', 1)[1]))

    def test_namespace_existing_61_fallback_is_left_byte_identical(self):
        block = ('#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n#include <linux/susfs_def.h>\n'
                 + '\n'.join(item[2] for item in headers.NAMESPACE_DECLARATIONS) + '\n#endif\n')
        text = self.NAMESPACE.replace('\n\n', '\n' + block + '\n', 1)
        path = self.source('common/fs/namespace.c', text)
        self.assertEqual((), headers.ensure_headers(self.root))
        self.assertEqual(text, path.read_text(encoding='utf-8'))

    def test_namespace_header_only_omission_is_also_repaired(self):
        text = self.NAMESPACE.replace('\n\n', '\n' + '\n'.join(item[2] for item in headers.NAMESPACE_DECLARATIONS) + '\n\n', 1)
        path = self.source('common/fs/namespace.c', text)
        headers.ensure_headers(self.root)
        text = path.read_text(encoding='utf-8')
        self.assertIn('#include <linux/susfs_def.h>', text)
        for _, _, declaration in headers.NAMESPACE_DECLARATIONS:
            self.assertEqual(1, text.count(declaration))

    def test_missing_actual_macro_provider_fails_before_any_source_changes(self):
        first = self.source('common/fs/a.c', self.TASK)
        second = self.source('common/fs/z.c', self.TASK.replace('SUSFS_IS_INODE_SUS_MAP', 'SUSFS_IS_INODE_NEW_MAP'))
        before = first.read_bytes(), second.read_bytes()
        with self.assertRaisesRegex(ValueError, 'does not provide macros'):
            headers.ensure_headers(self.root)
        self.assertEqual(before, (first.read_bytes(), second.read_bytes()))

    def test_no_include_anchor_fails_without_partial_repair(self):
        first = self.source('common/fs/a.c', self.TASK)
        self.source('common/fs/z.c', 'void f(void) { SUSFS_IS_INODE_SUS_MAP(inode); }\n')
        before = first.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Cannot locate unconditional header'):
            headers.ensure_headers(self.root)
        self.assertEqual(before, first.read_bytes())

    def test_direct_common_directory_can_be_selected(self):
        path = self.source('common/fs/proc/task_mmu.c', self.TASK)
        self.assertEqual((path,), headers.ensure_headers(self.root / 'common'))


if __name__ == '__main__':
    unittest.main()
