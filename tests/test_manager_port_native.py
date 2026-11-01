"""Replay the real reviewed port, then compile its native state contract."""
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
from additional_manager_integration import verify_legacy_next_lifecycle
from validate_susfs_hooks import strip_comments_and_literals


def function_source(source, name):
    """Extract a real complete function without changing its code."""
    code = strip_comments_and_literals(source)
    match = re.search(r'^(?:static )?(?:int|void)\s+' + re.escape(name) + r'\([^;]+?\)\s*\{', code, re.M)
    if not match:
        raise AssertionError('Missing native function: ' + name)
    depth = 1
    end = match.end()
    while depth:
        if code[end] == '{':
            depth += 1
        elif code[end] == '}':
            depth -= 1
        end += 1
    return source[match.start():end]


class NativeNextPortTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gki-next-native-contract-')
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name)
        result = subprocess.run(['git', '-C', str(self.source), 'init', '--quiet'], capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
        directory = ROOT / 'tools/patches/next-susfs23-stable-base'
        inventory = json.loads((directory / 'objects.json').read_text(encoding='utf-8'))
        patch = ROOT / 'tools/patches/next-susfs23-stable.patch'
        self.assertEqual(inventory['patch_sha256'], hashlib.sha256(patch.read_bytes()).hexdigest())
        for path, entry in inventory.get('native_sources', {}).items():
            raw = (directory / (entry['sha256'] + '.source')).read_bytes()
            self.assertEqual(entry['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(entry['blob_sha1'], hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest())
            target = self.source / path
            self.assertTrue(target.resolve().is_relative_to(self.source.resolve()))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        for block in patch.read_text(encoding='utf-8').split('diff --git ')[1:]:
            path = re.match(r'a/([^\n ]+) b/', block)[1]
            identity = re.search(r'^index ([0-9a-f]{40})\.\.', block, re.M)[1]
            if identity == '0' * 40:
                continue
            digest = inventory['objects'][identity]
            raw = (directory / (digest + '.source')).read_bytes()
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            self.assertEqual(identity, hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest())
            target = self.source / path
            self.assertTrue(target.resolve().is_relative_to(self.source.resolve()))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        for arguments in (['--check'], []):
            result = subprocess.run(['git', '-C', str(self.source), '-c', 'core.autocrlf=false',
                                     'apply', *arguments, str(patch)], capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
        # A surrounding Git repository can otherwise silently skip every path.
        # Verify each complete postimage, rather than trusting exit status alone.
        for block in patch.read_text(encoding='utf-8').split('diff --git ')[1:]:
            path = re.match(r'a/[^\n ]+ b/([^\n ]+)', block)[1]
            identity = re.search(r'^index [0-9a-f]{40}\.\.([0-9a-f]{40})', block, re.M)[1]
            target = self.source / path
            if identity == '0' * 40:
                self.assertFalse(target.exists())
            else:
                raw = target.read_bytes()
                self.assertEqual(identity, hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest())

    def test_real_complete_port_preserves_the_native_lifecycle_contract(self):
        verify_legacy_next_lifecycle(self.source)

    def test_missing_state_and_removed_profile_initializer_fail_before_fanout(self):
        core = self.source / 'kernel/core/init.c'
        raw = core.read_text(encoding='utf-8')
        for broken in (raw.replace('bool ksu_late_loaded;', ''),
                       raw.replace('int __init kernelsu_init(void)\n{',
                                   'int __init kernelsu_init(void)\n{\n    ksu_app_profile_init();')):
            self.assertNotEqual(raw, broken)
            core.write_text(broken, encoding='utf-8', newline='\n')
            with self.assertRaises(ValueError):
                verify_legacy_next_lifecycle(self.source)
        core.write_text(raw, encoding='utf-8', newline='\n')

    def test_lost_native_toolkit_parameters_fail_before_fanout(self):
        path = self.source / 'kernel/supercall/supercall.c'
        text = path.read_text(encoding='utf-8')
        path.write_text(text.replace('int magic2, unsigned int cmd, void __user *arg',
                                     'void __user *arg'), encoding='utf-8', newline='\n')
        with self.assertRaisesRegex(ValueError, 'toolkit reboot parameters'):
            verify_legacy_next_lifecycle(self.source)

    def test_missing_native_boot_object_fails_before_fanout(self):
        kbuild = self.source / 'kernel/Kbuild'
        text = kbuild.read_text(encoding='utf-8')
        kbuild.write_text(text.replace('kernelsu-objs += extras.o', ''), encoding='utf-8', newline='\n')
        with self.assertRaisesRegex(ValueError, 'boot callback'):
            verify_legacy_next_lifecycle(self.source)

    def test_real_native_boot_callback_links_and_runs_with_its_kbuild_object(self):
        compiler = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
        if not compiler:
            self.skipTest('Native C compiler required; Ubuntu preflight links the real boot callback')
        extras = (self.source / 'kernel/extras.c').read_text(encoding='utf-8')
        boot = (self.source / 'kernel/runtime/boot_event.c').read_text(encoding='utf-8')
        unit = self.source / 'extras.c'
        unit.write_text('#include <stdbool.h>\nbool boot_completed, ksu_avc_spoof_enabled=true;\n'
                        'unsigned native_enables;\nvoid ksu_avc_spoof_enable(void) { native_enables++; }\n'
                        + function_source(extras, 'ksu_avc_spoof_late_init'), encoding='utf-8', newline='\n')
        program = self.source / 'boot_event.c'
        program.write_text('#include <stdbool.h>\n#define pr_info(...) ((void)0)\n'
                           'bool ksu_boot_completed;\nextern bool boot_completed, ksu_avc_spoof_enabled;\n'
                           'extern unsigned native_enables;\nunsigned throne, backup;\n'
                           'void track_throne(bool final) { if(final) throne++; }\n'
                           'void ksu_selinux_hide_drop_backup_if_unused(void) { backup++; }\n'
                           'void ksu_avc_spoof_late_init(void);\n'
                           + function_source(boot, 'on_boot_completed') + '\n'
                           'int main(void) { on_boot_completed();\n'
                           'if(!ksu_boot_completed || !boot_completed || native_enables!=1 || throne!=1 || backup!=1) return 1;\n'
                           'ksu_avc_spoof_enabled=false; on_boot_completed();\n'
                           'return native_enables!=1 || throne!=2 || backup!=2; }\n', encoding='utf-8', newline='\n')
        binary = self.source / ('native_boot.exe' if os.name == 'nt' else 'native_boot')
        result = subprocess.run([compiler, '-std=c11', '-Wall', '-Werror', str(program), str(unit),
                                 '-o', str(binary)], capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
        self.assertEqual(0, subprocess.run([str(binary)], capture_output=True).returncode)

    def test_native_reboot_commands_preserve_parameters_root_checks_and_fd_isolation(self):
        compiler = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
        if not compiler:
            self.skipTest('Native C compiler required; Ubuntu preflight executes the real command handlers')
        supercall = (self.source / 'kernel/supercall/supercall.c').read_text(encoding='utf-8')
        dispatch = (self.source / 'kernel/supercall/dispatch.c').read_text(encoding='utf-8')
        handler = function_source(dispatch, 'ksu_handle_sys_reboot')
        cases = list(dict.fromkeys(re.findall(r'case (CMD_SUSFS_\w+):', handler)))
        calls = list(dict.fromkeys(re.findall(r'\b(susfs_\w+)\(arg\);', handler)))
        flags = sorted(set(re.findall(r'#ifdef (CONFIG_KSU_SUSFS_\w+)', handler)))
        constants = ['KSU_INSTALL_MAGIC1', 'KSU_INSTALL_MAGIC2', 'SUSFS_MAGIC', 'CHANGE_MANAGER_UID',
                     'GET_SULOG_DUMP_V2', 'CHANGE_KSUVER', 'CHANGE_SPOOF_UNAME'] + cases
        code = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <stddef.h>
#define __user
#define GFP_KERNEL 0
#define TWA_RESUME 0
#define pr_info(...) ((void)0)
#define pr_warn(...) ((void)0)
struct kuid { unsigned val; };
static unsigned uid, manager_uid, allocations, susfs_calls, sulog_calls;
static uint32_t ksuver_override;
static struct kuid current_uid(void) { return (struct kuid){uid}; }
static void ksu_set_manager_appid(unsigned value) { manager_uid=value; }
static unsigned ksu_get_manager_appid(void) { return manager_uid; }
static int copy_to_user(void *to, const void *from, size_t n) { memcpy(to,from,n); return 0; }
static int copy_from_user(void *to, const void *from, size_t n) { memcpy(to,from,n); return 0; }
static long strncpy_from_user(char *to, const char *from, size_t n) { strncpy(to,from,n); return (long)strlen(from); }
static long strscpy(char *to, const char *from, size_t n) { strncpy(to,from,n); to[n-1]=0; return (long)strlen(to); }
static int ksu_sulog_handle_compat_dump(void *arg) { (void)arg; sulog_calls++; return 0; }
struct new_utsname { char release[65], version[65]; };
static struct new_utsname system_name={"stock-release","stock-version"};
static struct new_utsname *utsname(void) { return &system_name; }
static int uts_sem;
static void down_write(int *lock) { (void)lock; }
static void up_write(int *lock) { (void)lock; }
struct callback_head { void (*func)(struct callback_head *); };
struct ksu_install_fd_tw { struct callback_head cb; int *outp; };
static struct callback_head *pending;
static int current;
static void *kzalloc(size_t n, int flags) { (void)flags; allocations++; return calloc(1,n); }
static void kfree(void *p) { free(p); }
static int task_work_add(int task, struct callback_head *cb, int flags) {
    (void)task; (void)flags; pending=cb; return 0;
}
static void ksu_install_fd_tw_func(struct callback_head *cb) {
    struct ksu_install_fd_tw *tw=(struct ksu_install_fd_tw *)cb;
    *tw->outp=123; free(tw);
}
static void **expected_susfs_arg;
static void record_susfs(void **arg) { if(arg!=expected_susfs_arg) abort(); susfs_calls++; }
'''
        code += '\n'.join('#define ' + flag for flag in flags) + '\n'
        code += '\n'.join('#define ' + name + ' ' + str(i + 10) for i, name in enumerate(constants)) + '\n'
        code += '\n'.join('static void ' + name + '(void **arg) { record_susfs(arg); }' for name in calls) + '\n'
        code += function_source(supercall, 'ksu_supercall_reboot_handler') + '\n'
        code += function_source(supercall, 'ksu_handle_toolkit_reboot') + '\n' + handler
        code += r'''
int main(void) {
    unsigned long reply=0;
    void *arg=&reply;
    uid=0;
    if(ksu_handle_sys_reboot(-1,CHANGE_MANAGER_UID,77,&arg)!=-EINVAL || manager_uid) return 1;
    if(ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1,-1,77,&arg)!=-EINVAL || allocations) return 2;
    uid=2000;
    ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1,CHANGE_MANAGER_UID,77,&arg);
    if(manager_uid || reply || allocations) return 3;
    uid=0;
    ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1,CHANGE_MANAGER_UID,77,&arg);
    if(manager_uid!=77 || reply!=(unsigned long)&reply || allocations) return 4;
    reply=0;
    ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1,CHANGE_KSUVER,33355,&arg);
    if(ksuver_override!=33355 || reply!=(unsigned long)&reply || allocations) return 5;
    ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1,GET_SULOG_DUMP_V2,0,&arg);
    if(sulog_calls!=1 || allocations) return 6;
    char names[]="test-release\0test-version";
    char *p=names; void *pp=&p; arg=&pp;
    ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1,CHANGE_SPOOF_UNAME,0,&arg);
    if(strcmp(system_name.release,"test-release") || strcmp(system_name.version,"test-version") || allocations) return 7;
    char defaults[]="default\0default"; p=defaults; pp=&p; arg=&pp;
    ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1,CHANGE_SPOOF_UNAME,0,&arg);
    if(strcmp(system_name.release,"stock-release") || strcmp(system_name.version,"stock-version")) return 8;
    arg=&reply; expected_susfs_arg=&arg;
    uid=2000;
    if(ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1,SUSFS_MAGIC,CMD_SUSFS_SHOW_VERSION,&arg)!=-EINVAL || susfs_calls) return 9;
    uid=0;
    if(ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1,SUSFS_MAGIC,CMD_SUSFS_SHOW_VERSION,&arg)!=0 || susfs_calls!=1 || allocations) return 10;
    int fd=-1; arg=&fd;
    if(ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1,KSU_INSTALL_MAGIC2,0,&arg)!=0 || allocations!=1 || !pending) return 11;
    pending->func(pending);
    return fd!=123;
}
'''
        program = self.source / 'native_reboot.c'
        program.write_text(code, encoding='utf-8', newline='\n')
        binary = self.source / ('native_reboot.exe' if os.name == 'nt' else 'native_reboot')
        result = subprocess.run([compiler, '-std=c11', '-Wall', '-Werror', str(program), '-o', str(binary)], capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
        result = subprocess.run([str(binary)], capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))

    def test_real_c_state_matches_builtin_and_native_late_loaded_modes(self):
        compiler = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
        if not compiler:
            self.skipTest('Native C compiler required; Ubuntu preflight executes both modes')
        core = (self.source / 'kernel/core/init.c').read_text(encoding='utf-8')
        header = (self.source / 'kernel/include/ksu.h').read_text(encoding='utf-8')
        dispatch = (self.source / 'kernel/supercall/dispatch.c').read_text(encoding='utf-8')
        definition = re.search(r'^bool ksu_late_loaded\s*;', core, re.M)[0]
        declaration = re.search(r'^extern bool ksu_late_loaded\s*;', header, re.M)[0]
        initialization = re.search(r'#ifdef MODULE\n\s*ksu_late_loaded = .*?#endif', core, re.S)[0]
        reporting = re.search(r'if \(ksu_late_loaded\) \{\n\s*cmd.flags \|= KSU_GET_INFO_FLAG_LATE_LOAD;\n\s*}', dispatch)[0]
        code = ('#include <stdbool.h>\n' + definition + '\n' + declaration + '\n'
                'struct task { int pid; };\nstatic struct task process;\n'
                'static struct task *current = &process;\n'
                '#define KSU_GET_INFO_FLAG_LATE_LOAD 1U\n'
                'static void initialize(void) {\n' + initialization + '\n}\n'
                'static unsigned flags(void) { struct { unsigned flags; } cmd = {0};\n'
                + reporting + '\nreturn cmd.flags; }\n'
                'int main(void) { process.pid=1; if(current->pid!=process.pid) return 2; initialize(); if(flags()!=0) return 1;\n'
                'process.pid=2; initialize();\n#ifdef MODULE\nreturn flags()!=1;\n'
                '#else\nreturn flags()!=0;\n#endif\n}\n')
        program = self.source / 'native_state.c'
        program.write_text(code, encoding='utf-8', newline='\n')
        for mode in ('builtin', 'module'):
            binary = self.source / ('native_state_' + mode + ('.exe' if os.name == 'nt' else ''))
            flags = ['-DMODULE'] if mode == 'module' else []
            result = subprocess.run([compiler, '-std=c11', '-Wall', '-Werror', *flags,
                                     str(program), '-o', str(binary)], capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
            self.assertEqual(0, subprocess.run([str(binary)], capture_output=True).returncode)


if __name__ == '__main__':
    unittest.main()
