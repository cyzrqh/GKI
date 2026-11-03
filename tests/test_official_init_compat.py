import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import ensure_official_init_compat as compat


COMPILER = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
# Verbatim from tiann/KernelSU 28db74f6f4692e97006ce9ebd8b19b9cc614b5b7,
# kernel/infra/symbol_resolver.c: Exercise its actual version-dependent
# callback setup instead of replacing initialization with a test stub.
NATIVE_INITIALIZER = r'''void __init ksu_init_symbol_resolver()
{
#if !ALWAYS_HAVE_ON_EACH_SYMBOL
    kallsyms_on_each_symbol_fn = find_kernel_symbol_exact("kallsyms_on_each_symbol");
    if (!kallsyms_on_each_symbol_fn) {
        pr_warn("kallsyms_on_each_symbol not found!\n");
    }
#endif
#if HAVE_ON_EACH_MATCH_SYMBOL
    kallsyms_on_each_match_symbol_fn = find_kernel_symbol_exact("kallsyms_on_each_match_symbol");
    if (!kallsyms_on_each_match_symbol_fn) {
        pr_warn("kallsyms_on_each_match_symbol not found!\n");
    }
#endif
}'''
NATIVE_HEADER = '''#ifndef __KSU_SYMBOL_RESOLVER_H
#define __KSU_SYMBOL_RESOLVER_H
#include <linux/types.h>
void *ksu_resolve_symbol_for_functable_hook(const char *symbol_name);
unsigned long find_kernel_symbol_exact(const char *symbol_name);
int ksu_lookup_kernel_symbols(const char *const *names, unsigned long *addrs, u32 count);
void ksu_init_symbol_resolver();
#endif
'''
CORE = '''#include <linux/types.h>
#include "selinux/selinux.h"

int kernelsu_init(void)
{
    int ret;
    ksu_init_symbol_resolver();
    ret = ksu_selinux_inode_init();
    if (ret) {
        put_cred(ksu_cred);
        ksu_cred = NULL;
        return ret;
    }
    return 0;
}
'''


class OfficialInitCompatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='official-init-', dir=ROOT / 'tests')
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name)
        self.source = self.repository / 'kernel'
        self.write('core/init.c', CORE)
        self.write('infra/symbol_resolver.h', NATIVE_HEADER)
        self.write('infra/symbol_resolver.c', NATIVE_INITIALIZER + '\n')
        self.write('Kbuild', 'kernelsu-objs := core/init.o\nkernelsu-objs += infra/symbol_resolver.o\n')

    def write(self, relative, text):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def snapshot(self):
        return {p.relative_to(self.source).as_posix(): p.read_bytes()
                for p in self.source.rglob('*') if p.is_file()}

    def test_restore_only_actual_header_without_changing_initialization_or_providers(self):
        before = self.snapshot()
        self.assertTrue(compat.ensure_official_init_compat(self.repository))
        after = self.snapshot()
        self.assertEqual(set(before), set(after))
        for relative in before:
            if relative != 'core/init.c':
                self.assertEqual(before[relative], after[relative])
        fixed = after['core/init.c'].decode()
        self.assertEqual(CORE, fixed.replace('\n' + compat.HEADER + '\n', '', 1))
        self.assertEqual(1, fixed.count(compat.HEADER))
        self.assertNotIn('extern ', fixed)

    def test_idempotent_with_direct_kernel_directory_and_preserves_mtime(self):
        compat.ensure_official_init_compat(self.repository)
        core = self.source / 'core/init.c'
        state = core.stat().st_mtime_ns, hashlib.sha256(core.read_bytes()).hexdigest()
        self.assertFalse(compat.ensure_official_init_compat(self.source))
        self.assertEqual(state, (core.stat().st_mtime_ns, hashlib.sha256(core.read_bytes()).hexdigest()))

    def test_native_include_is_left_untouched(self):
        self.write('core/init.c', compat.HEADER + '\n' + CORE)
        before = self.snapshot()
        self.assertFalse(compat.ensure_official_init_compat(self.repository))
        self.assertEqual(before, self.snapshot())

    def test_real_official_and_kow_core_preimages_only_regain_the_removed_header(self):
        for name, digest in (
            ('official-susfs23', '6217b3f690d38810a0a75ef7e2ef89a4c8b8898c7f63af2b53b87db2018c85ac'),
            ('kow-susfs23', 'e557dee991f9baad2e88c86cc093e6378c35be71d1cdb8236eac6f2629c7c4cd'),
        ):
            with self.subTest(port=name):
                raw = (ROOT / 'tools/patches' / (name + '-base') / (digest + '.source')).read_bytes()
                self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
                native = raw.decode('utf-8')
                self.assertEqual(1, native.count(compat.HEADER))
                removed = native.replace(compat.HEADER + '\n', '', 1)
                self.write('core/init.c', removed)
                before = self.snapshot()
                self.assertTrue(compat.ensure_official_init_compat(self.repository))
                after = self.snapshot()
                self.assertEqual(removed, after['core/init.c'].decode().replace('\n' + compat.HEADER + '\n', '', 1))
                for relative in before.keys() - {'core/init.c'}:
                    self.assertEqual(before[relative], after[relative])

    def test_commented_include_and_debug_only_include_do_not_satisfy_real_dependency(self):
        for prefix in ('/*\n' + compat.HEADER + '\n*/\n',
                       '#ifdef CONFIG_KSU_DEBUG\n' + compat.HEADER + '\n#endif\n'):
            with self.subTest(prefix=prefix):
                self.write('core/init.c', prefix + CORE)
                self.assertTrue(compat.ensure_official_init_compat(self.repository))
                self.assertTrue(compat._unconditional_include((self.source / 'core/init.c').read_text()))

    def test_stable_source_without_real_call_does_not_require_or_modify_resolver(self):
        for original in (CORE.replace('    ksu_init_symbol_resolver();\n', ''),
                         '#include <linux/types.h>\nvoid ksu_init_symbol_resolver();\n',
                         '#include <linux/types.h>\nconst char *x = "ksu_init_symbol_resolver()";\n'
                         '/* ksu_init_symbol_resolver(); */\n'):
            with self.subTest(original=original):
                self.write('core/init.c', original)
                (self.source / 'infra/symbol_resolver.h').unlink(missing_ok=True)
                before = self.snapshot()
                self.assertFalse(compat.ensure_official_init_compat(self.repository))
                self.assertEqual(before, self.snapshot())

    def test_missing_wrong_or_commented_header_declaration_fails_without_writing(self):
        for declaration in ('/* void ksu_init_symbol_resolver(); */\n',
                            'int ksu_init_symbol_resolver(void);\n',
                            'void ksu_init_symbol_resolver(int argument);\n'):
            with self.subTest(declaration=declaration):
                self.write('infra/symbol_resolver.h', declaration)
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, 'header must declare'):
                    compat.ensure_official_init_compat(self.repository)
                self.assertEqual(before, self.snapshot())

    def test_missing_non_global_or_empty_provider_is_rejected_without_writing(self):
        for provider in ('void ksu_init_symbol_resolver(void);\n',
                         'static ' + NATIVE_INITIALIZER,
                         'void ksu_init_symbol_resolver(void) {}\n',
                         'void ksu_init_symbol_resolver(void) { /* real setup removed */ }\n',
                         'void ksu_init_symbol_resolver(void) { return; }\n'):
            with self.subTest(provider=provider):
                self.write('infra/symbol_resolver.c', provider)
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, 'implementation|real callback setup'):
                    compat.ensure_official_init_compat(self.repository)
                self.assertEqual(before, self.snapshot())

    def test_commented_missing_duplicate_or_disabled_object_is_rejected(self):
        for build in ('# kernelsu-objs += infra/symbol_resolver.o\n',
                      'kernelsu-objs := core/init.o\n',
                      'kernelsu-objs += infra/symbol_resolver.o\n' * 2,
                      'ifeq ($(CONFIG_KSU_DEBUG),y)\nkernelsu-objs += infra/symbol_resolver.o\nendif\n'):
            with self.subTest(build=build):
                self.write('Kbuild', build)
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, 'remain linked once'):
                    compat.ensure_official_init_compat(self.repository)
                self.assertEqual(before, self.snapshot())

    def test_missing_input_or_no_include_anchor_does_not_leave_a_partial_fix(self):
        header = self.source / 'infra/symbol_resolver.h'
        header.unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Missing or unsupported'):
            compat.ensure_official_init_compat(self.repository)
        self.assertEqual(before, self.snapshot())
        self.write('infra/symbol_resolver.h', NATIVE_HEADER)
        self.write('core/init.c', CORE[CORE.index('int kernelsu_init'):])
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'unconditional header includes'):
            compat.ensure_official_init_compat(self.repository)
        self.assertEqual(before, self.snapshot())

    @unittest.skipUnless(COMPILER and os.name != 'nt', 'A native POSIX C compiler is required')
    def test_real_initializer_compiles_after_header_repair_and_preserves_versioned_callbacks(self):
        self.write('linux/types.h', '#include <stddef.h>\ntypedef unsigned int u32;\n')
        self.write('selinux/selinux.h', '''extern int *ksu_cred;
int ksu_selinux_inode_init(void);
void put_cred(int *credential);
''')
        model = '''#include <assert.h>
#include <string.h>
#include "infra/symbol_resolver.h"
#define __init
#define pr_warn(...) ((void)0)
#if TEST_KERNEL < 519
#define ALWAYS_HAVE_ON_EACH_SYMBOL 0
#else
#define ALWAYS_HAVE_ON_EACH_SYMBOL 1
#endif
#if TEST_KERNEL >= 601
#define HAVE_ON_EACH_MATCH_SYMBOL 1
#else
#define HAVE_ON_EACH_MATCH_SYMBOL 0
#endif
static void *kallsyms_on_each_symbol_fn;
static void *kallsyms_on_each_match_symbol_fn;
unsigned long find_kernel_symbol_exact(const char *name) {
    if (strcmp(name, "kallsyms_on_each_symbol") == 0) return 11;
    if (strcmp(name, "kallsyms_on_each_match_symbol") == 0) return 22;
    assert(!"unexpected native symbol lookup");
    return 0;
}
'''
        self.write('infra/symbol_resolver.c', model + NATIVE_INITIALIZER + '''
int *ksu_cred;
static int inode_error, dropped;
int ksu_selinux_inode_init(void) { return inode_error; }
void put_cred(int *credential) { assert(credential == ksu_cred); dropped++; }
int kernelsu_init(void);
int main(void) {
    int credential;
    ksu_cred = &credential;
    assert(kernelsu_init() == 0);
#if TEST_KERNEL < 519
    assert((unsigned long)kallsyms_on_each_symbol_fn == 11);
#else
    assert(!kallsyms_on_each_symbol_fn);
#endif
#if TEST_KERNEL >= 601
    assert((unsigned long)kallsyms_on_each_match_symbol_fn == 22);
#else
    assert(!kallsyms_on_each_match_symbol_fn);
#endif
    inode_error = -12;
    assert(kernelsu_init() == -12 && dropped == 1 && ksu_cred == NULL);
    return 0;
}
''')
        core = self.source / 'core/init.c'
        executable = self.repository / 'init-contract'
        command = [COMPILER, '-std=gnu11', '-Wall', '-Werror', '-Wno-unused-function',
                   '-Wno-int-conversion', '-I', str(self.source), str(core),
                   str(self.source / 'infra/symbol_resolver.c'), '-o', str(executable)]
        failed = subprocess.run(command + ['-DTEST_KERNEL=510'], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn(compat.SYMBOL, failed.stderr)
        self.assertTrue(compat.ensure_official_init_compat(self.repository))
        for kernel in (510, 515, 601, 606, 612):
            with self.subTest(kernel=kernel):
                result = subprocess.run(command + ['-DTEST_KERNEL=' + str(kernel)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
