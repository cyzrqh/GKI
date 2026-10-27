"""Exact native OPlus adaptations, including real Git dry-run/application."""
import difflib
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_compat as compat


class OplusCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.common = ROOT.parent / '.gki-upstream-cache' / ('oplus-compat-test-' + uuid.uuid4().hex)
        self.common.mkdir()
        self.addCleanup(self.cleanup)
        subprocess.run(['git', '-C', str(self.common), 'init', '-q'], check=True, capture_output=True)
        self.patch = self.common / 'reference.patch'

    def cleanup(self):
        expected = (ROOT.parent / '.gki-upstream-cache').resolve()
        if self.common.resolve().parent != expected or not self.common.name.startswith('oplus-compat-test-'):
            raise ValueError('Invalid test cleanup directory')
        def writable(function, path, error):
            if not isinstance(error, PermissionError):
                raise error
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)
        shutil.rmtree(self.common, onexc=writable)

    def write(self, relative, text):
        path = self.common / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def task_patch(self):
        # Small synthetic source windows; no upstream source program is copied.
        smap = ('static int show_smap(struct seq_file *m, void *v)\n{\n'
                '\tstruct vm_area_struct *vma = v;\n\tstruct mem_size_stats mss = {};\n\n'
                '\tif (!vma_pages(vma))\n\t\tgoto show_pad;\n\n'
                '\treturn gather(vma, &mss);\n}\n')
        rollup = ('static int show_smaps_rollup(struct seq_file *m, void *v)\n{\n'
                  '\tstruct vm_area_struct *vma = v;\n\tstruct mem_size_stats mss = {};\n'
                  '\tsmap_gather_stats(vma, &mss, 0);\n'
                  '\tif (lock_contended()) {\n\t\tsmap_gather_stats(vma, &mss, 0);\n'
                  '\t\tsmap_gather_stats(vma, &mss, last_vma_end);\n\t}\n'
                  '\treturn preserve_vendor_lifecycle();\n}\n')
        self.write('fs/proc/task_mmu.c', smap + '\n' + rollup)
        patch = ('diff --git a/fs/proc/task_mmu.c b/fs/proc/task_mmu.c\n'
                 '--- a/fs/proc/task_mmu.c\n+++ b/fs/proc/task_mmu.c\n'
                 '@@ -3,6 +3,13 @@ static int show_smap(struct seq_file *m, void *v)\n'
                 ' \tstruct vm_area_struct *vma = v;\n \tstruct mem_size_stats mss = {};\n \n'
                 '+#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n+\tif (vma->vm_file) {\n'
                 '+\t\tif (SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n+\t\t\treturn 0;\n'
                 '+\t}\n+#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n+\n'
                 ' \tif (!vma_data_pages(vma))\n \t\tgoto show_pad;\n \n'
                 '@@ -30,6 +37,11 @@ static int show_smaps_rollup(struct seq_file *m, void *v)\n'
                 ' \t\t\tgoto out_unlock;\n \t\t}\n \n'
                 '+#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
                 '+\t\tif (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
                 '+\t\t\tgoto bypass_orig_flow;\n+#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n+\n'
                 ' \t\tif (vma->vm_start < last_vma_end) {\n \t\t\t/* native old loop */\n'
                 '@@ -50,6 +62,9 @@ static int show_smaps_rollup(struct seq_file *m, void *v)\n'
                 ' \t\t\tsmap_gather_stats(priv, vma, &mss);\n \t\t}\n'
                 '+#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n+bypass_orig_flow:\n'
                 '+#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
                 ' \t\tlast_vma_end = vma->vm_end;\n \n')
        return patch.encode(), smap, rollup

    def test_smap_patch_cannot_relocate_to_another_function_and_preserves_three_gather_paths(self):
        raw, _smap, _rollup = self.task_patch()
        adapted = compat.adapt_susfs_patch(self.common, raw)
        self.patch.write_bytes(adapted)
        for arguments in (['--check'], []):
            result = subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(self.common), 'apply',
                                     *arguments, str(self.patch)], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
        result = (self.common / 'fs/proc/task_mmu.c').read_text()
        smap = compat.function_text(result, 'show_smap')
        rollup = compat.function_text(result, 'show_smaps_rollup')
        self.assertIn('if (vma->vm_file)', smap)
        self.assertEqual(3, rollup.count('if (!vma->vm_file || !SUSFS_IS_INODE_SUS_MAP'))
        self.assertEqual(3, rollup.count('smap_gather_stats(vma, &mss,'))
        self.assertIn('preserve_vendor_lifecycle()', rollup)
        self.assertNotIn('bypass_orig_flow', rollup)

    def test_unknown_smap_context_rollup_lifecycle_or_new_feature_addition_is_rejected(self):
        raw, _smap, _rollup = self.task_patch()
        original = (self.common / 'fs/proc/task_mmu.c').read_text()
        for text, patch in ((original.replace('vma_pages(vma)', 'new_vma_pages(vma)'), raw),
                (original.replace('smap_gather_stats(vma, &mss, last_vma_end)', 'different_gather(vma, &mss)'), raw),
                (original, raw.replace(b'+bypass_orig_flow:', b'+new_feature_hook();\n+bypass_orig_flow:'))):
            self.write('fs/proc/task_mmu.c', text)
            with self.subTest(text=text[-80:]), self.assertRaises(ValueError):
                compat.adapt_susfs_patch(self.common, patch)

    def test_rust_bitcode_filters_only_preprocessor_maps_with_real_exact_patch_and_is_idempotent(self):
        source = ('# Every native command and all source flags remain unchanged\n'
                  'source_c_flags := -fdebug-prefix-map=/src=. -fmacro-prefix-map=/src=. -ffile-prefix-map=/src=.\n'
                  'cmd_rustc_library = $(if $(link_helper),llvm-link helpers.bc;) \\\n'
                  + compat.BITCODE_COMMAND + '\n')
        path = self.write('rust/Makefile', source)
        self.assertTrue(compat.prepare_rust_bitcode(self.common))
        self.assertFalse(compat.prepare_rust_bitcode(self.common))
        result = path.read_text()
        self.assertEqual(source.replace(compat.BITCODE_COMMAND, compat.BITCODE_FIXED), result)
        self.assertIn('source_c_flags := -fdebug-prefix-map=/src=. -fmacro-prefix-map=/src=. -ffile-prefix-map=/src=.', result)
        self.assertNotIn('-Wno-error', result)
        path.write_text(source.replace('$(KBUILD_CFLAGS)', '$(UNKNOWN_FLAGS)'), encoding='utf-8', newline='\n')
        with self.assertRaises(ValueError):
            compat.prepare_rust_bitcode(self.common)

    @unittest.skipUnless(shutil.which('clang'), 'Clang is required for the native C/LLVM-bitcode reproduction')
    def test_real_clang_bitcode_retains_debug_mapping_without_unused_preprocessor_flags(self):
        compiler = shutil.which('clang')
        source = self.write('helper.c', 'int official_helper(int value) { return value + 1; }\n')
        bitcode, output = self.common / 'helper.bc', self.common / 'helper.o'
        mappings = ['-fdebug-prefix-map=' + str(self.common) + '=.',
                    '-fmacro-prefix-map=' + str(self.common) + '=.',
                    '-ffile-prefix-map=' + str(self.common) + '=.']
        compiled = subprocess.run([compiler, '-g', *mappings, '-emit-llvm', '-c', str(source), '-o', str(bitcode)],
                                  capture_output=True, text=True)
        self.assertEqual(0, compiled.returncode, compiled.stderr)
        original = subprocess.run([compiler, '-Werror', *mappings, '-c', str(bitcode), '-o', str(output)],
                                  capture_output=True, text=True)
        self.assertNotEqual(0, original.returncode)
        self.assertIn('argument unused during compilation', original.stderr)
        fixed = subprocess.run([compiler, '-Werror', mappings[0], '-c', str(bitcode), '-o', str(output)],
                               capture_output=True, text=True)
        self.assertEqual(0, fixed.returncode, fixed.stderr)
        self.assertGreater(output.stat().st_size, 0)

    def rtmutex_fixture(self):
        paths = ('kernel/locking/rtmutex.c', 'kernel/locking/rtmutex_api.c')
        before = ('header\n\tstruct task_struct *owner = rt_mutex_owner(lock);\n'
                  '\tstruct rt_mutex_base *next_lock;\n\n'
                  '\tlockdep_assert_held(&lock->wait_lock);\n\n'
                  '\traw_spin_lock(&current->pi_lock);\n\trt_mutex_dequeue(lock, waiter);\n'
                  '\tcurrent->pi_blocked_on = NULL;\n\traw_spin_unlock(&current->pi_lock);\n\nfooter\n')
        guard = '\tif (!waiter_task) /* never enqueued */\n\t\treturn;\n\n'
        after = before.replace('\tstruct rt_mutex_base *next_lock;',
            '\tstruct task_struct *waiter_task = waiter->task;\n\tstruct rt_mutex_base *next_lock;')
        after = after.replace('\traw_spin_lock(&current->pi_lock);\n\trt_mutex_dequeue(lock, waiter);\n'
            '\tcurrent->pi_blocked_on = NULL;\n\traw_spin_unlock(&current->pi_lock);',
            guard + '\tscoped_guard(raw_spinlock, &waiter_task->pi_lock) {\n'
            '\t\trt_mutex_dequeue(lock, waiter);\n\t\twaiter_task->pi_blocked_on = NULL;\n\t}')
        api_before = 'header\n\tif (unlikely(ret))\n\t\tremove_waiter(lock, waiter);\nfooter\n'
        api_after = api_before.replace('unlikely(ret)', 'unlikely(ret < 0)')
        raw = ''.join('diff --git a/' + path + ' b/' + path + '\n' + ''.join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile='a/' + path, tofile='b/' + path))
            for path, old, new in ((paths[0], before, after), (paths[1], api_before, api_after))).encode()
        self.patch.write_bytes(raw)
        self.write(paths[0], after.replace(guard, '', 1))
        self.write(paths[1], api_before)
        patcher = mock.patch.object(compat, 'RTMUTEX_PATCH_SHA256', hashlib.sha256(raw).hexdigest())
        patcher.start()
        self.addCleanup(patcher.stop)
        return paths, before, after, api_before, api_after

    def test_partial_official_rtmutex_fix_is_completed_transactionally_and_idempotently(self):
        paths, _before, after, _api_before, api_after = self.rtmutex_fixture()
        self.assertTrue(compat.apply_rtmutex_patch(self.common, self.patch))
        self.assertEqual(after, (self.common / paths[0]).read_text())
        self.assertEqual(api_after, (self.common / paths[1]).read_text())
        self.assertFalse(compat.apply_rtmutex_patch(self.common, self.patch))

    def test_full_old_rtmutex_source_and_already_fixed_api_are_supported_without_losing_other_changes(self):
        paths, before, after, _api_before, api_after = self.rtmutex_fixture()
        self.write(paths[0], before)
        self.write(paths[1], api_after)
        self.assertTrue(compat.apply_rtmutex_patch(self.common, self.patch))
        self.assertEqual(after, (self.common / paths[0]).read_text())
        self.assertEqual(api_after, (self.common / paths[1]).read_text())

    def test_unknown_rtmutex_state_or_dry_run_rejection_never_partially_writes_other_file(self):
        paths, _before, _after, _api_before, _api_after = self.rtmutex_fixture()
        self.write(paths[0], 'changed unknown upstream implementation\n')
        original = {path: (self.common / path).read_bytes() for path in paths}
        with self.assertRaises(ValueError):
            compat.apply_rtmutex_patch(self.common, self.patch)
        self.assertEqual(original, {path: (self.common / path).read_bytes() for path in paths})
        updates = {paths[0]: ('changed unknown upstream implementation\n', 'after\n')}
        with mock.patch.object(compat.subprocess, 'run', return_value=mock.Mock(returncode=1)), self.assertRaises(ValueError):
            compat.apply_checked_updates(self.common, updates)
        self.assertEqual(original, {path: (self.common / path).read_bytes() for path in paths})

    def naming_fixture(self, *, series='6.12', generation='6', suffix='-4k', localversion=None):
        android = {'6.1': 'android14', '6.6': 'android15', '6.12': 'android16'}[series]
        self.write('build.config.constants', 'BRANCH=' + android + '-' + series + '\n')
        self.write('build.config.common', 'KMI_GENERATION=' + generation + '\n')
        self.write('build.config.gki', 'POST_DEFCONFIG_CMDS="check_defconfig"\n')
        self.write('scripts/setlocalversion', '#!/bin/sh\necho "${KERNELVERSION}${LOCALVERSION}"\n')
        self.write('arch/arm64/configs/gki_defconfig', 'CONFIG_LOCALVERSION="' + suffix + '"\nCONFIG_LOCALVERSION_AUTO=y\n')
        if localversion is not None:
            self.write('localversion', localversion)
        for arguments in (['config', 'core.autocrlf', 'false'], ['add', '.'],
                          ['-c', 'user.name=tests', '-c', 'user.email=tests@example.invalid',
                           '-c', 'commit.gpgsign=false', 'commit', '-qm', 'official naming fixture']):
            subprocess.run(['git', '-C', str(self.common), *arguments], check=True, capture_output=True)
        return {'kernel': series + '.58', 'kernel_version': series, 'android_version': android}

    def test_native_release_comes_from_official_generation_and_preserves_script_and_4k(self):
        profile = self.naming_fixture()
        script = (self.common / 'scripts/setlocalversion').read_bytes()
        config = (self.common / 'arch/arm64/configs/gki_defconfig').read_bytes()
        self.assertEqual('6.12.58-android16-6', compat.prepare_native_release(self.common, profile))
        self.assertEqual('-android16-6', (self.common / 'out/localversion.gki').read_text())
        self.assertEqual('6.12.58-android16-6', compat.prepare_native_release(self.common, profile))
        self.assertEqual(script, (self.common / 'scripts/setlocalversion').read_bytes())
        self.assertEqual(config, (self.common / 'arch/arm64/configs/gki_defconfig').read_bytes())

    def test_existing_native_android_prefix_needs_no_generated_override(self):
        profile = self.naming_fixture(localversion='-android16-6')
        self.assertEqual('6.12.58-android16-6', compat.prepare_native_release(self.common, profile))
        self.assertFalse((self.common / 'out/localversion.gki').exists())

    def test_unknown_or_mutated_official_naming_is_rejected_before_any_release_override(self):
        profile = self.naming_fixture()
        for relative, text in (
                ('build.config.common', 'KMI_GENERATION=5\n'),
                ('scripts/setlocalversion', '#!/bin/sh\necho "-android16-5-fakehash"\n'),
                ('arch/arm64/configs/gki_defconfig', 'CONFIG_LOCALVERSION="-fakehash"\n')):
            path = self.common / relative
            original = path.read_bytes()
            path.write_text(text, encoding='utf-8', newline='\n')
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                compat.prepare_native_release(self.common, profile)
            self.assertFalse((self.common / 'out/localversion.gki').exists())
            path.write_bytes(original)
        with self.assertRaises(ValueError):
            compat.prepare_native_release(self.common, {**profile, 'android_version': 'android15'})

    def test_official_generation_must_be_unique_static_and_never_guessed_from_series(self):
        profile = self.naming_fixture()
        for common in ('# no generation provided\n', 'KMI_GENERATION=${SOME_BRANCH_VALUE}\n',
                       'KMI_GENERATION=5\nKMI_GENERATION=6\n'):
            self.write('build.config.common', common)
            subprocess.run(['git', '-C', str(self.common), 'add', 'build.config.common'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(self.common), '-c', 'user.name=tests',
                '-c', 'user.email=tests@example.invalid', '-c', 'commit.gpgsign=false',
                'commit', '-qm', 'changed official naming test'], check=True, capture_output=True)
            with self.subTest(common=common), self.assertRaises(ValueError):
                compat.prepare_native_release(self.common, profile)
            self.assertFalse((self.common / 'out/localversion.gki').exists())

    def test_conflicting_native_prefix_is_never_replaced_or_stacked(self):
        profile = self.naming_fixture(localversion='-android16-5')
        original = (self.common / 'localversion').read_bytes()
        with self.assertRaises(ValueError):
            compat.prepare_native_release(self.common, profile)
        self.assertEqual(original, (self.common / 'localversion').read_bytes())
        self.assertFalse((self.common / 'out/localversion.gki').exists())


if __name__ == '__main__':
    unittest.main()
