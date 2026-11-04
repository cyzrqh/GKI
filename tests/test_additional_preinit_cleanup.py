"""Native preinit descriptor ownership through the actual reviewed SUSFS ports."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import additional_manager_integration as integration
from apply_susfs23 import verify_preinit_fd_cleanup
from verify_kow_susfs import function_source

FIXTURE = ROOT / 'tests/fixtures/additional-preinit'
PATCHES = ROOT / 'tools/patches'


def native_util():
    raw = (FIXTURE / 'util.h.source').read_bytes()
    provenance = json.loads((FIXTURE / 'provenance.json').read_text(encoding='utf-8'))
    if (hashlib.sha256(raw).hexdigest() != provenance['sha256']
            or hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != provenance['git_blob']):
        raise AssertionError('The native upstream syscall wrapper fixture changed')
    return raw


def git(source, *args):
    result = subprocess.run(['git', '-C', str(source), '-c', 'core.autocrlf=false', *args],
                            capture_output=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr.decode(errors='replace'))
    return result.stdout


def materialize(source, name='official-susfs23'):
    source.mkdir(parents=True)
    base = PATCHES / (name + '-base')
    inventory = json.loads((base / 'objects.json').read_text(encoding='utf-8'))
    patch = PATCHES / (name + '.patch')
    raw = patch.read_bytes()
    if hashlib.sha256(raw).hexdigest() != inventory['patch_sha256']:
        raise AssertionError('The reviewed complete SUSFS port digest changed')
    for relative, blob in re.findall(rb'(?m)^diff --git a/(\S+) b/\S+\nindex ([0-9a-f]{40})\.\.', raw):
        if blob == b'0' * 40:
            continue
        digest = inventory['objects'][blob.decode()]
        data = (base / (digest + '.source')).read_bytes()
        if (hashlib.sha256(data).hexdigest() != digest
                or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest().encode() != blob):
            raise AssertionError('The reviewed upstream preimage identity changed')
        path = source / relative.decode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    util = source / 'kernel/include/util.h'
    util.parent.mkdir(parents=True, exist_ok=True)
    util.write_bytes(native_util())
    git(source, 'init', '--quiet')
    git(source, 'add', '.')
    git(source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--quiet', '-m', 'Exact native reviewed SUSFS preimages')
    return patch


def snapshot(source):
    return {path.relative_to(source).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (source / 'kernel').rglob('*') if path.is_file()}


def compiler():
    if os.environ.get('GKI_TEST_ZIG') or shutil.which('zig'):
        return [os.environ.get('GKI_TEST_ZIG') or shutil.which('zig'), 'cc']
    if os.environ.get('GKI_TEST_CC'):
        return shlex.split(os.environ['GKI_TEST_CC'])
    for name in ('cc', 'clang', 'gcc'):
        if shutil.which(name):
            return [shutil.which(name)]
    return None


@unittest.skipUnless(shutil.which('git'), 'Git is required to apply real reviewed upstream patches')
class AdditionalPreinitCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='additional-preinit-',
                                                     dir=ROOT.parent / '.gki-upstream-cache')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def prepared(self, name='official-susfs23'):
        source = self.directory / name / 'KernelSU'
        patch = materialize(source, name)
        git(source, 'apply', '--check', str(patch))
        git(source, 'apply', '--3way', str(patch))
        return source, patch

    def test_complete_official_and_kow_ports_preserve_cleanup_and_reverse_to_exact_upstream(self):
        for name in ('official-susfs23', 'kow-susfs23'):
            with self.subTest(port=name):
                source = self.directory / name
                patch = materialize(source, name)
                before = {key: value[0] for key, value in snapshot(source).items()}
                git(source, 'apply', '--check', str(patch))
                git(source, 'apply', '--3way', str(patch))
                preinit = (source / 'kernel/preinit/preinit.c').read_text(encoding='utf-8')
                verify_preinit_fd_cleanup(preinit, native_util().decode())
                integration.verify_native_preinit_cleanup(source)
                self.assertIn('ksu_close_fd(fd);', function_source(preinit, 'ksu_setup_preinit_file'))
                self.assertIn('ksu_close_fd(*preinit_fd);',
                              function_source(preinit, 'ksu_preinit_handle_post_execveat'))
                self.assertEqual(native_util(), (source / 'kernel/include/util.h').read_bytes())
                state = snapshot(source)
                integration.verify_native_preinit_cleanup(source)
                self.assertEqual(state, snapshot(source))
                git(source, 'apply', '--reverse', '--3way', str(patch))
                git(source, 'diff', '--exit-code', 'HEAD', '--', 'kernel')
                self.assertEqual(before, {key: value[0] for key, value in snapshot(source).items()})

    def test_real_postimage_drift_is_rejected_without_any_source_write(self):
        source, _ = self.prepared()
        preinit = source / 'kernel/preinit/preinit.c'
        util = source / 'kernel/include/util.h'
        original = preinit.read_text(encoding='utf-8')
        helper = util.read_text(encoding='utf-8')
        for changed, native in (
                (original.replace('ksu_close_fd(fd);', 'close_fd(fd);', 1), helper),
                (original.replace('ksu_close_fd(fd);', '', 1), helper),
                (original.replace('ksu_close_fd(*preinit_fd);', '', 1), helper),
                (original.replace('ksu_close_fd(fd);', 'ksu_close_fd(fd + 1);', 1), helper),
                (original.replace('#include "util.h"', '', 1), helper),
                (original, helper.replace('#define ksu_close_fd(fd) ({ ksyscall(close, fd); })',
                                          '#define ksu_close_fd(fd) ((void)0)', 1))):
            with self.subTest(source_changed=changed != original, helper_changed=native != helper):
                preinit.write_text(changed, encoding='utf-8', newline='\n')
                util.write_text(native, encoding='utf-8', newline='\n')
                before = snapshot(source)
                with self.assertRaises(ValueError):
                    integration.verify_native_preinit_cleanup(source)
                self.assertEqual(before, snapshot(source))

    def test_official_direct_and_already_applied_paths_recheck_the_native_helper(self):
        source = self.directory / 'KernelSU'
        patch = materialize(source)
        dependency = self.directory / 'susfs/kernel_patches/KernelSU'
        dependency.mkdir(parents=True)
        shutil.copyfile(patch, dependency / '10_enable_susfs_for_ksu.patch')
        # KPM composition has its own full-source tests. The real SUSFS patch,
        # subprocess application, and descriptor verifier remain active here.
        with mock.patch('additional_manager_kpm.with_susfs', side_effect=lambda root, variant, apply: apply()):
            integration.integrate_susfs(self.directory, self.directory / 'susfs', 'KernelSU')
            before = snapshot(source)
            integration.integrate_susfs(self.directory, self.directory / 'susfs', 'KernelSU')
            self.assertEqual(before, snapshot(source))
            helper = source / 'kernel/include/util.h'
            helper.write_bytes(native_util().replace(b'ksyscall(close, fd);', b'(void)0;'))
            changed = snapshot(source)
            with self.assertRaisesRegex(ValueError, 'native close syscall'):
                integration.integrate_susfs(self.directory, self.directory / 'susfs', 'KernelSU')
            self.assertEqual(changed, snapshot(source))

    def test_stable_source_without_modern_preinit_is_left_untouched(self):
        source = self.directory / 'stable'
        source.mkdir()
        integration.verify_native_preinit_cleanup(source)
        self.assertEqual([], list(source.iterdir()))

    @unittest.skipUnless(compiler(), 'A C compiler or GKI_TEST_ZIG is required')
    def test_real_preinit_bodies_and_native_syscall_macros_release_exact_fd_on_old_and_modern_kernels(self):
        source, _ = self.prepared()
        preinit = (source / 'kernel/preinit/preinit.c').read_text(encoding='utf-8')
        setup = 'int ' + function_source(preinit, 'ksu_setup_preinit_file')
        post = 'int ' + function_source(preinit, 'ksu_preinit_handle_post_execveat')
        util = native_util().decode()
        # Preserve the production architecture selection and all six-register
        # argument packing/dispatch macros, including native ksu_close_fd.
        macros = util[util.index('#if defined(__aarch64__)'):util.index('static inline void __user *userspace_stack_buffer')]
        macros += re.search(r'(?m)^#define ksu_close_fd[^\n]+', util)[0] + '\n'
        model = C_MODEL + macros + C_SYSCALL
        target = self.directory / ('fd-contract.exe' if os.name == 'nt' else 'fd-contract')
        contract = self.directory / 'fd-contract.c'
        command = [*compiler(), '-std=gnu11', '-Wall', '-Werror', '-Wno-unused-function',
                   '-Wno-unused-but-set-variable', str(contract), '-o', str(target)]
        contract.write_text(model + setup.replace('ksu_close_fd(fd);', 'close_fd(fd);', 1) + post + C_MAIN,
                            encoding='utf-8', newline='\n')
        failed = subprocess.run([*command, '-DMODERN_CLOSE=0'], capture_output=True, timeout=120)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn('close_fd', failed.stderr.decode(errors='replace'))
        contract.write_text(model + setup + post + C_MAIN, encoding='utf-8', newline='\n')
        for modern in (0, 1):
            with self.subTest(modern=modern):
                result = subprocess.run([*command, '-DMODERN_CLOSE=' + str(modern)], capture_output=True, timeout=120)
                self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
                result = subprocess.run([str(target)], capture_output=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))


C_MODEL = r'''#include <assert.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#define EINVAL 22
#define O_CLOEXEC 1
#define LOOKUP_FOLLOW 1
#define INIT_PATH "/system/bin/init"
#define likely(value) (value)
#define READ_ONCE(value) (value)
#define xchg(pointer, value) ({ __typeof__(*(pointer)) old = *(pointer); *(pointer) = (value); old; })
#define pr_err(...) ((void)0)
#define pr_info(...) ((void)0)
struct pt_regs { unsigned long args[6]; };
#define PT_REGS_SYSCALL_PARM1(regs) ((regs)->args[0])
#define PT_REGS_PARM2(regs) ((regs)->args[1])
#define PT_REGS_PARM3(regs) ((regs)->args[2])
#define PT_REGS_SYSCALL_PARM4(regs) ((regs)->args[3])
#define PT_REGS_PARM5(regs) ((regs)->args[4])
#define PT_REGS_PARM6(regs) ((regs)->args[5])
struct file { int marker; };
struct path { int marker; };
struct filename { char *name; };
struct user_arg_ptr { int valid; };
struct files_struct { int open[32]; };
struct task_struct { struct files_struct *files; };
static struct files_struct table;
static struct task_struct task = { &table };
static struct task_struct *current = &task;
static struct file runner, *ksu_preinit_file;
static int next_fd, path_error, installed, dropped, path_released;
static int syscall_count, legacy_count, modern_count, closed_fd;
static int is_selinux_setup(struct user_arg_ptr *args) { return args->valid; }
static int get_unused_fd_flags(int flags) { assert(flags == O_CLOEXEC); return next_fd; }
static void fput(struct file *file) { assert(file == &runner); dropped++; }
static void fd_install(int fd, struct file *file) {
    assert(file == &runner && fd > 0 && fd < 32); table.open[fd] = 1; installed++;
}
static int kern_path(const char *name, int flags, struct path *path) {
    assert(strncmp(name, "/proc/self/fd/", 14) == 0 && flags == LOOKUP_FOLLOW); (void)path; return path_error;
}
static void path_put(struct path *path) { (void)path; path_released++; }
#if MODERN_CLOSE
static int close_fd(unsigned int fd) {
    assert(fd < 32 && current->files->open[fd]); current->files->open[fd] = 0;
    modern_count++; closed_fd = (int)fd; return 0;
}
#else
static int __close_fd(struct files_struct *files, unsigned int fd) {
    assert(files == current->files && fd < 32 && files->open[fd]); files->open[fd] = 0;
    legacy_count++; closed_fd = (int)fd; return 0;
}
#endif
'''

C_SYSCALL = r'''
long KSU_SYS_PREFIX(close)(const struct pt_regs *regs) {
    for (int index = 1; index < 6; index++) assert(regs->args[index] == 0);
    syscall_count++;
#if MODERN_CLOSE
    return close_fd((unsigned int)regs->args[0]);
#else
    return __close_fd(current->files, (unsigned int)regs->args[0]);
#endif
}
static void reset(void) {
    memset(&table, 0, sizeof(table)); table.open[9] = 1;
    ksu_preinit_file = &runner; next_fd = 7; path_error = 0;
    installed = dropped = path_released = syscall_count = legacy_count = modern_count = 0; closed_fd = -1;
}
'''

C_MAIN = r'''
int main(void) {
    char name[64] = INIT_PATH;
    struct filename filename = {name}, *pointer = &filename;
    struct user_arg_ptr args = {1};
    int fd = 0, flags = 0, retval = 0, preinit_fd = 7;
    reset(); path_error = 1;
    assert(ksu_setup_preinit_file(&pointer, &args) == -EINVAL);
    assert(installed == 1 && syscall_count == 1 && closed_fd == 7 && !table.open[7]);
    assert(table.open[9] && !ksu_preinit_file && !dropped && !path_released);
#if MODERN_CLOSE
    assert(modern_count == 1 && !legacy_count);
#else
    assert(legacy_count == 1 && !modern_count);
#endif
    reset(); strcpy(name, INIT_PATH);
    assert(ksu_setup_preinit_file(&pointer, &args) == 7);
    assert(strcmp(name, "/proc/self/fd/7") == 0 && installed == 1 && path_released == 1);
    assert(table.open[7] && table.open[9] && !syscall_count && !dropped);
    assert(ksu_preinit_handle_post_execveat(&fd, &pointer, NULL, NULL, &flags, &retval, &preinit_fd) == 0);
    assert(table.open[7] && !syscall_count);
    retval = -1;
    assert(ksu_preinit_handle_post_execveat(&fd, &pointer, NULL, NULL, &flags, &retval, &preinit_fd) == -EINVAL);
    assert(strcmp(name, INIT_PATH) == 0 && syscall_count == 1 && closed_fd == 7);
    assert(!table.open[7] && table.open[9] && !dropped);
    reset(); strcpy(name, INIT_PATH); next_fd = -1;
    assert(ksu_setup_preinit_file(&pointer, &args) == -EINVAL);
    assert(dropped == 1 && !installed && !syscall_count && table.open[9]);
    reset(); ksu_preinit_file = NULL;
    assert(ksu_setup_preinit_file(&pointer, &args) == -EINVAL);
    assert(!installed && !dropped && !syscall_count && table.open[9]);
    reset(); args.valid = 0;
    assert(ksu_setup_preinit_file(&pointer, &args) == -EINVAL);
    assert(ksu_preinit_file == &runner && !installed && !dropped && !syscall_count);
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
