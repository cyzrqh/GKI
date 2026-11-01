"""Exercise the actual reviewed Kow dispatch code with an inert host harness."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

from verify_kow_susfs import function_source, verify_kow_port

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'tools/patches/kow-susfs23.patch'
BASE = ROOT / 'tools/patches/kow-susfs23-base'


def call(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr.decode(errors='replace'))
    return result.stdout


def prepared(root):
    manifest = json.loads((BASE / 'objects.json').read_text())
    patch = PATCH.read_bytes()
    if hashlib.sha256(patch).hexdigest() != manifest['patch_sha256']:
        raise AssertionError('Reviewed source patch digest changed')
    for name, blob in re.findall(rb'(?m)^diff --git a/(\S+) b/\S+\nindex ([0-9a-f]{40})\.\.', patch):
        digest = manifest['objects'][blob.decode()]
        data = (BASE / (digest + '.source')).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise AssertionError('Reviewed source preimage digest changed')
        path = root / name.decode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    call(root, 'init', '--quiet')
    call(root, 'config', 'core.autocrlf', 'false')
    call(root, 'add', '--', 'kernel')
    call(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
         'commit', '--quiet', '-m', 'Native reviewed preimages')
    call(root, 'apply', '--check', str(PATCH))
    call(root, 'apply', str(PATCH))
    verify_kow_port(root)


@unittest.skipUnless(shutil.which('git'), 'Git is required to apply the real reviewed source patch')
class KowSusfsTests(unittest.TestCase):
    def test_native_permissions_lifecycle_and_forwarding_survive_real_patch_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared(root)
            # Changing a real permission check can never be accepted as a
            # layout adaptation by the compatibility gate.
            path = root / 'kernel/supercall/supercall.c'
            text = path.read_text().replace('current_uid().val != 0', 'false', 1)
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, 'authorization'):
                verify_kow_port(root)

    @unittest.skipUnless(os.environ.get('GKI_TEST_CC') or shutil.which('cc'), 'A native C compiler is required')
    def test_real_c_fd_toolkit_arguments_and_susfs_root_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared(root)
            native = (root / 'kernel/supercall/supercall.c').read_text()
            dispatch = (root / 'kernel/supercall/dispatch.c').read_text()
            reboot = function_source(dispatch, 'ksu_handle_sys_reboot')
            command_names = sorted(set(re.findall(r'\bCMD_SUSFS_[A-Z0-9_]+\b', reboot)))
            functions = sorted(set(re.findall(r'\b(susfs_[a-z_]+)\s*\(', reboot)))
            # Only kernel transport/UID and SUSFS leaves are simulated. The
            # functions under test below are the actual production C bodies.
            fixture = '''#include <assert.h>
#include <stddef.h>
#define __user
#define KSU_INSTALL_MAGIC1 0xDEADBEEF
#define KSU_INSTALL_MAGIC2 0xCAFEBABE
#define SUSFS_MAGIC 0x5555
#define EINVAL 22
struct uid { unsigned int val; };
static unsigned int uid;
static struct uid current_uid(void) { return (struct uid){uid}; }
static int saved_magic1, saved_magic2;
static unsigned int saved_cmd;
static void *saved_arg;
static int calls, susfs_calls;
static int ksu_handle_legacy_reboot(int magic1, int magic2, unsigned int cmd, void **arg) {
    saved_magic1=magic1; saved_magic2=magic2; saved_cmd=cmd; saved_arg=*arg; calls++; return 123;
}
'''
            fixture += '\n'.join(f'#define {name} {index + 1}' for index, name in enumerate(command_names)) + '\n'
            fixture += '\n'.join(f'void {name}(void *arg) {{ (void)arg; susfs_calls++; }}' for name in functions) + '\n'
            fixture += 'int ' + function_source(native, 'ksu_supercall_reboot_handler') + '\n'
            fixture += 'int ' + function_source(native, 'ksu_handle_toolkit_reboot') + '\n'
            fixture += 'int ' + reboot + '\n'
            fixture += '''int main(void) {
    int marker=1; void *argument=&marker;
    assert(ksu_supercall_reboot_handler(&argument)==123);
    assert((unsigned int)saved_magic1==KSU_INSTALL_MAGIC1);
    assert((unsigned int)saved_magic2==KSU_INSTALL_MAGIC2 && saved_cmd==0 && saved_arg==argument);
    assert(ksu_handle_toolkit_reboot(10011, 41100, argument)==123);
    assert(saved_magic2==10011 && saved_cmd==41100 && saved_arg==argument);
    calls=0; uid=0;
    assert(ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1, 10013, 6, &argument)==123);
    assert(calls==1 && saved_magic2==10013 && saved_cmd==6 && saved_arg==argument);
    uid=10000; susfs_calls=0;
    ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1, SUSFS_MAGIC, CMD_SUSFS_SHOW_VERSION, &argument);
    assert(susfs_calls==0);
    uid=0;
    assert(ksu_handle_sys_reboot(KSU_INSTALL_MAGIC1, SUSFS_MAGIC, CMD_SUSFS_SHOW_VERSION, &argument)==0);
    assert(susfs_calls==1);
    calls=0; susfs_calls=0;
    assert(ksu_handle_sys_reboot(1, SUSFS_MAGIC, CMD_SUSFS_SHOW_VERSION, &argument)==-EINVAL);
    assert(calls==0 && susfs_calls==0);
    return 0;
}
'''
            source = root / 'contract.c'
            source.write_text(fixture)
            output = root / ('contract.exe' if os.name == 'nt' else 'contract')
            compiler = shlex.split(os.environ.get('GKI_TEST_CC', 'cc'))
            # Match the kernel's signed/unsigned warning policy for its u32
            # reboot magic constants and signed syscall arguments.
            built = subprocess.run([*compiler, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
                                    str(source), '-o', str(output)], capture_output=True, timeout=120)
            self.assertEqual(0, built.returncode, built.stderr.decode(errors='replace'))
            ran = subprocess.run([str(output)], capture_output=True, timeout=10)
            self.assertEqual(0, ran.returncode, ran.stderr.decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
