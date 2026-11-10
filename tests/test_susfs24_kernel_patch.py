from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from snapshot_ksu_source import temporary_directory
from susfs24_kernel_patch import EXPORTS, adapt_header, apply_susfs24, guard_preinit, patch_sections


def section(path, hunk):
    return f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n' + hunk


class Susfs24KernelPatchTests(unittest.TestCase):
    def test_missing_trace_include_keeps_every_real_added_line(self):
        source = ('#include <linux/uaccess.h>\n#include <linux/pkeys.h>\n\n'
                  '#include <asm/elf.h>\n#include <asm/tlb.h>\n')
        hook = ('+#if defined(CONFIG_KSU_SUSFS_SUS_MAP)\n'
                '+#include <linux/susfs_def.h>\n+#endif\n')
        patch = section('fs/proc/task_mmu.c', '@@ -22,6 +22,9 @@\n'
                        ' #include <linux/uaccess.h>\n #include <linux/pkeys.h>\n'
                        ' #include <trace/hooks/mm.h>\n' + hook +
                        ' \n #include <asm/elf.h>\n #include <asm/tlb.h>\n')
        adapted = adapt_header(patch, source, ('trace/hooks/mm.h',))
        self.assertIn('@@ -22,5 +22,8 @@\n', adapted)
        self.assertIn(hook, adapted)
        self.assertNotIn('trace/hooks/mm.h', adapted)
        for unexpected in (source + source, source.replace('linux/pkeys.h', 'linux/other.h')):
            with self.assertRaisesRegex(ValueError, 'Unknown native source layout'):
                adapt_header(patch, unexpected, ('trace/hooks/mm.h',))

    def test_preinit_uses_one_global_kconfig_guard_for_all_four_sites(self):
        native = '+#ifndef KSU_NO_PREINIT\n+\tksu_native_preinit();\n+#endif // #ifndef KSU_NO_PREINIT\n'
        result = guard_preinit(native * 4)
        self.assertEqual(4, result.count('+#ifdef CONFIG_KSU_SUSFS_NATIVE_PREINIT\n'))
        self.assertEqual(4, result.count('ksu_native_preinit();'))
        self.assertNotIn('KSU_NO_PREINIT', result)
        with self.assertRaisesRegex(ValueError, 'Unknown SUSFS v2.4 native preinit'):
            guard_preinit(native * 3)

    def test_66_vendor_trace_hook_keeps_its_native_preprocessor_order(self):
        source = ('#include <linux/vmalloc.h>\n#include <linux/sched/sysctl.h>\n\n'
                  '#include <trace/events/kmem.h>\n\n#undef CREATE_TRACE_POINTS\n'
                  '#include <trace/hooks/mm.h>\n')
        hook = ('+#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
                '+#include <linux/susfs_def.h>\n+#endif\n')
        patch = section('mm/memory.c', '@@ -82,6 +82,9 @@\n'
                        ' #include <linux/vmalloc.h>\n #include <linux/sched/sysctl.h>\n'
                        ' #include <linux/zswap.h>\n' + hook +
                        ' \n #include <trace/events/kmem.h>\n #include <trace/hooks/mm.h>\n')
        adapted = adapt_header(patch, source, ('linux/zswap.h',))
        self.assertIn('@@ -82,4 +82,7 @@\n', adapted)
        self.assertIn(hook, adapted)
        self.assertNotIn('trace/hooks/mm.h', adapted)
        with self.assertRaisesRegex(ValueError, 'Unknown native source layout'):
            adapt_header(patch, source.replace('#undef CREATE_TRACE_POINTS', '#define OTHER'), ('linux/zswap.h',))

    def test_rejected_staged_patch_keeps_native_files_and_exports_untouched(self):
        with temporary_directory(Path(__file__).parent, '.susfs24-test-') as directory:
            kernel = directory / 'common'
            upstream = directory / 'susfs4ksu'
            exported = upstream / 'kernel_patches'
            for name in EXPORTS:
                path = exported / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'#define SUSFS_VERSION "v2.4.0"\n' if name.endswith('/susfs.h') else b'export\n')
            paths = ('fs/exec.c', 'fs/notify/fdinfo.c', 'block/ioprio.c', 'security/selinux/status.c')
            for name in paths:
                path = kernel / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'native\n')
            patch = exported / '50_add_susfs_in_gki-android14-6.1.patch'
            native = '+#ifndef KSU_NO_PREINIT\n+\tksu_native_preinit();\n+#endif // #ifndef KSU_NO_PREINIT\n'
            patch.write_bytes((section(paths[0], '@@ -1 +1,13 @@\n native\n' + native * 4) +
                               ''.join(section(name, '@@ -1 +1 @@\n-native\n+hooked\n')
                                       for name in paths[1:])).encode())

            def rejected(stage, prepared):
                self.assertNotEqual(kernel, stage)
                self.assertEqual(set(paths), set(patch_sections(prepared.read_text(encoding='utf-8'))))
                (stage / paths[0]).write_bytes(b'partially patched\n')
                raise ValueError('unexpected native context')

            with self.assertRaisesRegex(ValueError, 'unexpected native context'):
                apply_susfs24(kernel, upstream, 'android14-6.1', apply_patch=rejected)
            for name in paths:
                self.assertEqual(b'native\n', (kernel / name).read_bytes())
            self.assertFalse(any((kernel / name).exists() for name in EXPORTS))

    def test_patch_cannot_rename_or_leave_native_source_tree(self):
        for invalid in (section('fs/../boot.c', '@@ -1 +1 @@\n-a\n+b\n'),
                        section('fs/open.c', '@@ -1 +1 @@\n-a\n+b\n').replace('+++ b/fs/open.c', '+++ b/fs/other.c')):
            with self.assertRaises(ValueError):
                patch_sections(invalid)


if __name__ == '__main__':
    unittest.main()
