"""Check Kow's native permissions and toolkit while using SUSFS hooks."""
from pathlib import Path
import re
import subprocess

from validate_susfs_hooks import strip_comments_and_literals


def function_source(text: str, name: str) -> str:
    clean = strip_comments_and_literals(text)
    matches = list(re.finditer(r'\b' + re.escape(name) + r'\s*\([^;{}]*\)\s*\{', clean))
    if len(matches) != 1:
        raise ValueError('Expected one real C definition: ' + name)
    start = matches[0].start()
    body = clean.index('{', start)
    depth, end = 1, body + 1
    while depth and end < len(clean):
        if clean[end] == '{':
            depth += 1
        elif clean[end] == '}':
            depth -= 1
        end += 1
    if depth:
        raise ValueError('Unbalanced native C function: ' + name)
    return text[start:end]


def verify_kow_port(source: Path) -> None:
    def read(name):
        path = source / 'kernel' / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError('Missing bounded Kow native source input: ' + name)
        return path.read_text(encoding='utf-8')

    native = subprocess.run(['git', '-C', str(source), 'show', 'HEAD:kernel/supercall/supercall.c'],
                            capture_output=True, timeout=30)
    if native.returncode:
        raise ValueError('Cannot inspect the fixed native Kow toolkit')
    original = function_source(native.stdout.decode(), 'ksu_handle_sys_reboot')
    supercall = read('supercall/supercall.c')
    preserved = function_source(supercall, 'ksu_handle_legacy_reboot')
    if preserved != original.replace('ksu_handle_sys_reboot', 'ksu_handle_legacy_reboot', 1):
        raise ValueError('Kow toolkit operations or original authorization checks changed')
    toolkit = function_source(supercall, 'ksu_handle_toolkit_reboot')
    installer = function_source(supercall, 'ksu_supercall_reboot_handler')
    if ('ksu_handle_legacy_reboot(KSU_INSTALL_MAGIC1, magic2, cmd, &arg)' not in toolkit
            or 'ksu_handle_legacy_reboot(KSU_INSTALL_MAGIC1, KSU_INSTALL_MAGIC2, 0, arg)' not in installer):
        raise ValueError('Kow native command/fd parameters are not forwarded intact')
    if supercall.count('#ifndef CONFIG_KSU_SUSFS') != 4:
        raise ValueError('Kow reboot kprobe must be disabled when the SUSFS hook is active')
    dispatch = read('supercall/dispatch.c')
    reboot = function_source(dispatch, 'ksu_handle_sys_reboot')
    if ('magic1 != KSU_INSTALL_MAGIC1' not in reboot
            or 'magic2 == SUSFS_MAGIC && current_uid().val == 0' not in reboot
            or 'return ksu_handle_toolkit_reboot(magic2, cmd, *arg);' not in reboot):
        raise ValueError('SUSFS authorization or Kow toolkit dispatch is incomplete')
    if 'magic2' in function_source(dispatch, 'do_lookup_symbols'):
        raise ValueError('Reboot parameters leaked into the symbol lookup handler')
    core = read('core/init.c')
    for call in ('susfs_init();', 'ksu_feature_init();', 'ksu_avc_spoof_init();', 'ksu_avc_spoof_exit();'):
        if core.count(call) != 1:
            raise ValueError('Kow native lifecycle callback is missing or duplicated: ' + call)
    if 'ksu_selinux_inode_init' in core:
        raise ValueError('Kow must not call the newer upstream-only SELinux inode initializer')
    if 'kernelsu-objs += extras.o' not in read('Kbuild'):
        raise ValueError('Kow AVC implementation is not linked')
    header = read('supercall/supercall.h')
    for identifier in ('CHANGE_MANAGER_UID', 'GET_SULOG_DUMP_V2', 'CHANGE_KSUVER',
                       'CHANGE_SPOOF_UNAME', 'CHANGE_KSUFLAGS'):
        if len(re.findall(r'^#define\s+' + identifier + r'\b', header, re.M)) != 1:
            raise ValueError('Missing native Kow toolkit command: ' + identifier)
