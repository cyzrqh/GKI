"""Kow's direct SUSFS hooks must use the real original-policy SELinux view."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from verify_kow_susfs import function_source, verify_selinux_view

COMPILER = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')


class KowSelinuxViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='kow-view-', dir=ROOT / 'tests')
        cls.addClassCleanup(cls.temporary.cleanup)
        workspace = Path(cls.temporary.name)
        subprocess.run(['git', '-C', str(workspace), 'init', '--quiet'], check=True, capture_output=True, timeout=30)
        patch = ROOT / 'tools/patches/kow-susfs23.patch'
        metadata = patch.with_name('kow-susfs23-base')
        inventory = json.loads((metadata / 'objects.json').read_text(encoding='utf-8'))
        raw_patch = patch.read_bytes()
        if hashlib.sha256(raw_patch).hexdigest() != inventory['patch_sha256']:
            raise AssertionError('The actual reviewed Kow patch digest differs')
        patch_text = raw_patch.decode('utf-8')
        for relative in ('kernel/feature/selinux_hide.c', 'kernel/selinux/rules.c'):
            section = re.search(r'(?ms)^diff --git a/' + re.escape(relative) + r' b/'
                                + re.escape(relative) + r'\n.*?(?=^diff --git |\Z)', patch_text)
            identity = re.search(r'^index ([0-9a-f]{40})\.\.', section[0], re.M)[1]
            digest = inventory['objects'][identity]
            data = (metadata / (digest + '.source')).read_bytes()
            if (hashlib.sha256(data).hexdigest() != digest
                    or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != identity):
                raise AssertionError('Kow reviewed native preimage differs')
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            result = subprocess.run(['git', '-C', str(workspace), '-c', 'core.autocrlf=false',
                                     'apply', '--include=' + relative, str(patch)], capture_output=True, timeout=30)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
        cls.hide = (workspace / 'kernel/feature/selinux_hide.c').read_text(encoding='utf-8')
        cls.rules = (workspace / 'kernel/selinux/rules.c').read_text(encoding='utf-8')

    def test_reviewed_port_preserves_a_real_original_policy_view_and_lifecycle(self):
        verify_selinux_view(self.hide, self.rules)
        self.assertIn('fake_state.policy = backup_sepolicy;', self.hide)
        self.assertNotIn('fake_state.policy = selinux_state.policy;', self.hide)

    def test_empty_or_real_policy_alias_and_missing_initialization_are_rejected(self):
        for before, after in (
            ('fake_state.policy = backup_sepolicy;', 'fake_state.policy = NULL;'),
            ('fake_state.policy = backup_sepolicy;', 'fake_state.policy = selinux_state.policy;'),
            ('fake_state.initialized = true;', ''),
            ('struct selinux_state fake_state;', 'extern struct selinux_state fake_state;'),
            ('    if (!backup_sepolicy) {', '    if (false) {'),
        ):
            with self.subTest(before=before, after=after), self.assertRaises(ValueError):
                verify_selinux_view(self.hide.replace(before, after, 1), self.rules)

    def test_modern_helper_lookup_and_success_only_publication_cannot_be_removed(self):
        for before, after in (
            ('security_dump_masked_av_fn = (void *)find_kernel_symbol_exact("security_dump_masked_av");', ''),
            ('context_struct_compute_av_fn = (void *)find_kernel_symbol_exact("context_struct_compute_av");', ''),
            ('if (!ret) {', 'if (true) {'),
            ('fake_state.policy = NULL;', ''),
        ):
            with self.subTest(before=before), self.assertRaises(ValueError):
                verify_selinux_view(self.hide.replace(before, after, 1), self.rules)

    def test_backup_must_capture_real_policy_and_sid_table_before_live_rules_change(self):
        for before, after in (
            ('backup_sepolicy = ksu_dup_sepolicy(old_pol);', 'backup_sepolicy = pol;'),
            ('policydb_load_isids(&backup_sepolicy->policydb, backup_sepolicy->sidtab)', 'skipped_backup_sidtab()'),
        ):
            with self.subTest(before=before), self.assertRaises(ValueError):
                verify_selinux_view(self.hide, self.rules.replace(before, after, 1))

    @unittest.skipUnless(COMPILER and os.name != 'nt', 'A native POSIX C compiler is required')
    def test_actual_lifecycle_links_and_uses_original_view_on_all_kernel_api_branches(self):
        state = re.search(r'(?ms)^#if LINUX_VERSION_CODE < KERNEL_VERSION\(6, 6, 0\)\n'
                          r'struct selinux_state fake_state;\n#endif', self.hide)[0]
        enable = 'static int ' + function_source(self.hide, 'ksu_selinux_hide_enable')
        setter = 'static int ' + function_source(self.hide, 'selinux_hide_feature_set')
        drop = 'void ' + function_source(self.hide, 'ksu_selinux_hide_drop_backup_if_unused')
        model = r'''#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#define KERNEL_VERSION(a,b,c) (((a)<<16) | ((b)<<8) | (c))
#define EAGAIN 11
#define pr_info(...) ((void)0)
#define pr_warn(...) ((void)0)
#define pr_err(...) ((void)0)
typedef unsigned long long u64;
struct selinux_policy { void *sidtab; int marker; };
#if LINUX_VERSION_CODE < KERNEL_VERSION(6,6,0)
struct selinux_state { bool initialized; struct selinux_policy *policy; };
/* This is the real old SELinux consumer's external ABI; the provider must
 * supply and initialize the object or the final link must fail. */
extern struct selinux_state fake_state;
#else
/* Modern kernels deliberately have no legacy state.policy/initialized. */
struct selinux_state { int modern; };
#endif
struct selinux_policy *backup_sepolicy;
bool ksu_selinux_hide_enabled, ksu_selinux_hide_running;
static int selinux_hide_mutex, lock_count, destroyed, lookups;
static bool lookup_missing;
static void mutex_lock(int *mutex) { (void)mutex; lock_count++; }
static void mutex_unlock(int *mutex) { (void)mutex; lock_count--; }
static void sidtab_destroy(void *table) { assert(table != NULL); destroyed++; }
static void kfree(void *table) { assert(table != NULL); }
static void ksu_destroy_sepolicy(struct selinux_policy *policy) { assert(policy != NULL); }
typedef void (*callback)(void);
static callback security_dump_masked_av_fn, context_struct_compute_av_fn;
static void native_callback(void) { }
static unsigned long find_kernel_symbol_exact(const char *name) {
    assert(!strcmp(name,"security_dump_masked_av") || !strcmp(name,"context_struct_compute_av"));
    lookups++;
    return lookup_missing ? 0 : (unsigned long)(uintptr_t)&native_callback;
}
'''
        main = r'''
int main(void) {
    struct selinux_policy original = { (void *)1, 41 }, live = { (void *)2, 99 };
    assert(selinux_hide_feature_set(1) == -EAGAIN);
    assert(!ksu_selinux_hide_running && lock_count == 0);
    backup_sepolicy = &original;
    assert(selinux_hide_feature_set(1) == 0);
    assert(ksu_selinux_hide_running && ksu_selinux_hide_enabled && lock_count == 0);
#if LINUX_VERSION_CODE < KERNEL_VERSION(6,6,0)
    assert(fake_state.initialized && fake_state.policy == &original);
    assert(fake_state.policy != &live && fake_state.policy->marker == 41);
#else
    assert(lookups == 2 && security_dump_masked_av_fn && context_struct_compute_av_fn);
    assert(selinux_hide_feature_set(0) == 0);
    lookup_missing = true;
    assert(selinux_hide_feature_set(1) == 0);
    assert(lookups == 4 && !security_dump_masked_av_fn && !context_struct_compute_av_fn);
#endif
    (void)live;
    ksu_selinux_hide_drop_backup_if_unused();
    assert(backup_sepolicy == &original && destroyed == 0);
    assert(selinux_hide_feature_set(0) == 0);
    assert(!ksu_selinux_hide_running && !ksu_selinux_hide_enabled && lock_count == 0);
    ksu_selinux_hide_drop_backup_if_unused();
    assert(backup_sepolicy == NULL && destroyed == 1 && lock_count == 0);
#if LINUX_VERSION_CODE < KERNEL_VERSION(6,6,0)
    assert(!fake_state.initialized && fake_state.policy == NULL);
#endif
    assert(selinux_hide_feature_set(1) == -EAGAIN);
    assert(!ksu_selinux_hide_running && lock_count == 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='kow-view-c-', dir=ROOT / 'tests') as temporary:
            source = Path(temporary) / 'view.c'
            source.write_text(model + '\n' + state + '\n' + enable + '\n' + setter + '\n' + drop + main,
                              encoding='utf-8', newline='\n')
            missing = Path(temporary) / 'missing-state.c'
            missing.write_text(re.sub(r'(?m)^struct selinux_state fake_state;\n', '', source.read_text(), count=1),
                               encoding='utf-8', newline='\n')
            failed = subprocess.run([COMPILER, '-std=gnu11', '-DLINUX_VERSION_CODE=' + str((5 << 16) | (15 << 8)),
                                     str(missing), '-o', str(Path(temporary) / 'missing-state')],
                                    capture_output=True, timeout=30)
            self.assertNotEqual(0, failed.returncode)
            self.assertIn(b'fake_state', failed.stderr)
            for major, minor in ((5, 10), (5, 15), (6, 1), (6, 6), (6, 12)):
                with self.subTest(kernel=(major, minor)):
                    output = Path(temporary) / ('view-' + str(major) + '-' + str(minor))
                    command = [COMPILER, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
                               '-Wno-unused-variable', '-DLINUX_VERSION_CODE=' + str((major << 16) | (minor << 8)),
                               str(source), '-o', str(output)]
                    result = subprocess.run(command, capture_output=True, timeout=30)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    subprocess.run([str(output)], check=True, capture_output=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
