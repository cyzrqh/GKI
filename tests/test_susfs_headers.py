import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import ensure_susfs_headers as headers

COMPILER = next((shutil.which(name) for name in ('cc', 'gcc', 'clang') if shutil.which(name)), None)


class SusfsHeaderTests(unittest.TestCase):
    HEADER = '''#define SUSFS_IS_INODE_SUS_MAP(inode) test_bit(39, &(inode)->i_mapping->flags)
#define SUSFS_IS_INODE_OPEN_REDIRECT(inode) test_bit(36, &(inode)->i_mapping->flags)
#define SUSFS_IS_INODE_OPEN_REDIRECT_WITHOUT_UID_CHECK(inode) test_bit(36, &(inode)->i_mapping->flags)
'''
    # Actual SUSFS 2.3 susfs_def.h inline bodies; test only their real thread-
    # flag dependencies below, without changing these implementations.
    DEFINITION_HELPERS = '''static inline bool susfs_is_current_proc_umounted(void) {
\treturn (likely(test_thread_flag(TIF_PROC_UMOUNTED)));
}

static inline bool susfs_is_current_proc_no_su(void) {
\treturn (likely(test_thread_flag(TIF_PROC_NO_SU)));
}
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

    def test_exec_and_base_with_only_native_function_calls_receive_the_real_header(self):
        self.source('common/include/linux/susfs_def.h', self.HEADER + self.DEFINITION_HELPERS)
        paths = []
        for relative, symbol in (('fs/exec.c', 'susfs_is_current_proc_no_su'),
                                 ('fs/proc/base.c', 'susfs_is_current_proc_umounted')):
            original = ('#include <linux/fs.h>\n\nint probe(void) {\n#ifdef CONFIG_KSU_SUSFS\n'
                        '    return ' + symbol + '();\n#else\n    return 0;\n#endif\n}\n')
            path = self.source('common/' + relative, original)
            paths.append((path, original))
        self.assertEqual({path for path, _ in paths}, set(headers.ensure_headers(self.root)))
        block = '\n#ifdef CONFIG_KSU_SUSFS\n#include <linux/susfs_def.h>\n#endif\n'
        for path, original in paths:
            text = path.read_text(encoding='utf-8')
            self.assertEqual(original, text.replace(block, '', 1))
            self.assertNotIn('extern ', text)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path, _ in paths}
        self.assertEqual((), headers.ensure_headers(self.root))
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path, _ in paths})

    def test_existing_conditionally_included_function_provider_is_unchanged(self):
        self.source('common/include/linux/susfs_def.h', self.HEADER + self.DEFINITION_HELPERS)
        original = ('#include <linux/fs.h>\n#ifdef CONFIG_KSU_SUSFS\n#include <linux/susfs_def.h>\n'
                    'int probe(void) { return susfs_is_current_proc_no_su(); }\n#endif\n')
        path = self.source('common/fs/exec.c', original)
        self.assertEqual((), headers.ensure_headers(self.root))
        self.assertEqual(original, path.read_text())

    def test_function_names_in_comments_or_strings_do_not_require_a_header(self):
        original = ('#include <linux/fs.h>\n/* susfs_is_current_proc_no_su() */\n'
                    'const char *text = "susfs_is_current_proc_umounted()";\n')
        path = self.source('common/fs/exec.c', original)
        self.assertEqual((), headers.ensure_headers(self.root))
        self.assertEqual(original, path.read_text())

    def test_missing_function_provider_rejects_the_entire_plan_before_any_write(self):
        first = self.source('common/fs/a.c', self.TASK)
        second = self.source('common/fs/z.c', '#include <linux/fs.h>\nint probe(void) { return susfs_is_current_proc_no_su(); }\n')
        before = first.read_bytes(), second.read_bytes()
        # A comment, different signature or call is not a native declaration.
        for absent in ('/* bool susfs_is_current_proc_no_su(void); */\n',
                       'bool susfs_is_current_proc_no_su(int arg);\n',
                       'bool other(void) { return susfs_is_current_proc_no_su(); }\n'):
            with self.subTest(provider=absent):
                self.source('common/include/linux/susfs_def.h', self.HEADER + absent)
                with self.assertRaisesRegex(ValueError, 'does not provide functions.*susfs_is_current_proc_no_su'):
                    headers.ensure_headers(self.root)
                self.assertEqual(before, (first.read_bytes(), second.read_bytes()))

    @unittest.skipUnless(COMPILER and os.name != 'nt', 'A native POSIX C compiler is required')
    def test_actual_header_inline_helpers_compile_after_repair_and_preserve_both_flag_states(self):
        self.source('common/include/linux/susfs_def.h', self.HEADER + self.DEFINITION_HELPERS)
        self.source('common/include/linux/fs.h', '''#include <assert.h>
#include <stdbool.h>
#define likely(value) (value)
#define TIF_PROC_NO_SU 0
#define TIF_PROC_UMOUNTED 1
static unsigned int current_thread_flags;
static inline int test_thread_flag(int flag) { return !!(current_thread_flags & (1U << flag)); }
''')
        original = '''#include <linux/fs.h>

static int probe(void) {
#ifdef CONFIG_KSU_SUSFS
    return susfs_is_current_proc_no_su() + 2 * susfs_is_current_proc_umounted();
#else
    return 0;
#endif
}
int main(void) {
    for (unsigned int flags = 0; flags < 4; flags++) {
        current_thread_flags = flags;
#ifdef CONFIG_KSU_SUSFS
        assert(probe() == (int)flags);
#else
        assert(probe() == 0);
#endif
    }
    return 0;
}
'''
        path = self.source('common/fs/exec.c', original)
        output = self.root / 'header-test'
        command = [COMPILER, '-std=c11', '-Wall', '-Wextra', '-Werror',
                   '-I', str(self.root / 'common/include'), str(path), '-o', str(output)]
        missing = subprocess.run(command + ['-DCONFIG_KSU_SUSFS'], capture_output=True, timeout=30)
        self.assertNotEqual(0, missing.returncode)
        self.assertIn(b'susfs_is_current_proc_no_su', missing.stderr)
        self.assertEqual((path,), headers.ensure_headers(self.root))
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                result = subprocess.run(command + (['-DCONFIG_KSU_SUSFS'] if enabled else []),
                                        capture_output=True, timeout=30)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(0, subprocess.run([str(output)], timeout=10).returncode)


if __name__ == '__main__':
    unittest.main()
