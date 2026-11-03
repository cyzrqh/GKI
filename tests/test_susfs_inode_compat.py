import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import ensure_susfs_inode_compat as compat

COMPILER = next((shutil.which(name) for name in ('cc', 'gcc', 'clang') if shutil.which(name)), None)

# Member types and UID-accessor body from Android common
# cdf3c668057f532964388bfd1ca053df5cc01343/include/linux/fs.h.
# Unrelated kernel members/types are omitted from this bounded compile fixture.
OLD_HEADER = '''#ifndef _TEST_FS_H
#define _TEST_FS_H
typedef unsigned int uid_t;
typedef struct { unsigned int val; } kuid_t;
struct user_namespace { unsigned int shift; };
struct super_block;
struct inode {
    kuid_t i_uid;
    struct super_block *i_sb;
};
struct super_block {
    struct user_namespace *s_user_ns;
};
static inline uid_t from_kuid(struct user_namespace *ns, kuid_t value)
{
    return value.val - ns->shift;
}
static inline uid_t i_uid_read(const struct inode *inode)
{
    return from_kuid(inode->i_sb->s_user_ns, inode->i_uid);
}
#endif
'''
SUSFS = '''#include <linux/fs.h>

static struct user_namespace *namespace_for_inode(const struct inode *inode)
{
    return i_user_ns(inode);
}
'''


class SusfsInodeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.inode-compat-' + uuid.uuid4().hex)
        self.common = self.root / 'common'
        self.susfs = self.common / 'fs/susfs.c'
        self.header = self.common / 'include/linux/fs.h'
        self.susfs.parent.mkdir(parents=True)
        self.header.parent.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.susfs.write_text(SUSFS, encoding='utf-8', newline='\n')
        self.header.write_text(OLD_HEADER, encoding='utf-8', newline='\n')

    def state(self):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.susfs, self.header)}

    def test_old_inode_api_backports_only_upstream_namespace_accessor(self):
        original = self.susfs.read_bytes()
        self.assertEqual((self.header,), compat.ensure_inode_compat(self.root))
        self.assertEqual(original, self.susfs.read_bytes())
        updated = self.header.read_text()
        self.assertEqual(OLD_HEADER, updated.replace(compat.IMPLEMENTATION + '\n', '', 1))
        self.assertLess(updated.index('return inode->i_sb->s_user_ns;'), updated.index('static inline uid_t i_uid_read'))
        self.assertNotIn('init_user_ns', updated)

    def test_second_call_and_common_root_preserve_every_byte_and_timestamp(self):
        compat.ensure_inode_compat(self.common)
        before = self.state()
        self.assertEqual((), compat.ensure_inode_compat(self.root))
        self.assertEqual(before, self.state())

    def test_native_function_or_declaration_remains_unchanged(self):
        for native in (compat.IMPLEMENTATION,
                       'struct user_namespace *i_user_ns(const struct inode *inode);\n'):
            with self.subTest(native=native):
                self.header.write_text(OLD_HEADER.replace('static inline uid_t i_uid_read',
                                       native + '\nstatic inline uid_t i_uid_read'), encoding='utf-8')
                before = self.state()
                self.assertEqual((), compat.ensure_inode_compat(self.root))
                self.assertEqual(before, self.state())

    def test_missing_or_unneeded_susfs_does_not_change_kernel(self):
        for unused in ('/* i_user_ns(inode) */\n',
                       'const char *unused = "i_user_ns(inode)";\n',
                       '#define unused(inode) i_user_ns(inode)\n',
                       '#include <linux/fs.h>\nint ordinary(void) { return 1; }\n'):
            with self.subTest(unused=unused):
                self.susfs.write_text(unused, encoding='utf-8')
                before = self.state()
                self.assertEqual((), compat.ensure_inode_compat(self.root))
                self.assertEqual(before, self.state())
        self.susfs.unlink()
        original = self.header.read_bytes()
        self.assertEqual((), compat.ensure_inode_compat(self.root))
        self.assertEqual(original, self.header.read_bytes())

    def test_commented_or_quoted_native_definition_does_not_hide_missing_api(self):
        for unused in ('/* ' + compat.IMPLEMENTATION + ' */\n',
                       'static const char *message = "i_user_ns(inode)";\n'):
            with self.subTest(unused=unused):
                self.header.write_text(unused + OLD_HEADER, encoding='utf-8')
                self.assertEqual((self.header,), compat.ensure_inode_compat(self.root))
                self.assertIn(compat.IMPLEMENTATION + '\nstatic inline uid_t', self.header.read_text())

    def test_changed_native_signature_or_macro_is_rejected_without_writes(self):
        for changed in ('int i_user_ns(const struct inode *inode);\n',
                        'struct user_namespace *i_user_ns(struct inode *inode);\n',
                        '#define i_user_ns(inode) vendor_namespace(inode)\n'):
            with self.subTest(changed=changed):
                self.header.write_text(changed + OLD_HEADER, encoding='utf-8')
                before = self.state()
                with self.assertRaisesRegex(ValueError, 'changed native i_user_ns'):
                    compat.ensure_inode_compat(self.root)
                self.assertEqual(before, self.state())

    def test_missing_or_changed_native_members_and_anchor_refuse_writes(self):
        changes = (
            ('struct super_block *i_sb;', 'void *i_sb;'),
            ('struct user_namespace *s_user_ns;', 'unsigned long s_user_ns;'),
            ('struct user_namespace *s_user_ns;', '/* struct user_namespace *s_user_ns; */'),
            ('struct super_block *i_sb;', 'struct nested { struct super_block *i_sb; } nested;'),
            ('struct super_block {', 'struct vendor_super_block {'),
            ('static inline uid_t i_uid_read', 'static inline uid_t renamed_uid_read'),
        )
        for old, new in changes:
            with self.subTest(old=old, new=new):
                self.header.write_text(OLD_HEADER.replace(old, new), encoding='utf-8')
                before = self.state()
                with self.assertRaises(ValueError):
                    compat.ensure_inode_compat(self.root)
                self.assertEqual(before, self.state())

    @unittest.skipUnless(COMPILER and os.name != 'nt', 'A native POSIX C compiler is required')
    def test_real_accessor_compiles_and_preserves_each_inode_namespace_and_uid_mapping(self):
        # Run the actual generated C body, using distinct namespaces so a stub
        # returning init_user_ns or any fixed pointer cannot satisfy the test.
        source = SUSFS + '''
#include <assert.h>
int main(void)
{
    struct user_namespace first = { 1000 }, second = { 2000 };
    struct super_block one = { &first }, two = { &second };
    const struct inode inode_one = { { 1234 }, &one }, inode_two = { { 2234 }, &two };
    assert(namespace_for_inode(&inode_one) == &first);
    assert(namespace_for_inode(&inode_two) == &second);
    assert(i_uid_read(&inode_one) == 234);
    assert(i_uid_read(&inode_two) == 234);
    two.s_user_ns = &first;
    assert(namespace_for_inode(&inode_two) == &first);
    assert(i_uid_read(&inode_two) == 1234);
    return 0;
}
'''
        self.susfs.write_text(source, encoding='utf-8', newline='\n')
        output = self.root / 'namespace-test'
        command = [COMPILER, '-std=c11', '-Wall', '-Wextra', '-Werror', '-I',
                   str(self.common / 'include'), str(self.susfs), '-o', str(output)]
        failed = subprocess.run(command, capture_output=True, timeout=30)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn(b'i_user_ns', failed.stderr)
        compat.ensure_inode_compat(self.root)
        compiled = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(0, compiled.returncode, compiled.stdout + compiled.stderr)
        self.assertEqual(0, subprocess.run([str(output)], timeout=10).returncode)


if __name__ == '__main__':
    unittest.main()
