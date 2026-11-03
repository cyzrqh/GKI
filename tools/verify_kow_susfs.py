"""Check Kow's native permissions and toolkit while using SUSFS hooks."""
from pathlib import Path
import re
import subprocess

from validate_susfs_hooks import strip_comments_and_literals
from ensure_susfs_headers import without_comments


def verify_selinux_view(hide: str, rules: str) -> None:
    """The old SELinux state API must expose the real pre-KSU policy view."""
    from ensure_official_init_compat import _unconditional_include
    if not _unconditional_include(hide):
        raise ValueError('Kow SELinux hiding requires its native symbol resolver declaration')
    clean = without_comments(hide)
    state = ('#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 6, 0)\n'
             'struct selinux_state fake_state;\n#endif')
    if state not in clean or len(re.findall(r'(?m)^struct selinux_state fake_state;', clean)) != 1:
        raise ValueError('Kow legacy SELinux hiding requires the paired state view')
    enable = function_source(hide, 'ksu_selinux_hide_enable')
    enabled_code = strip_comments_and_literals(enable)
    available = re.search(r'if\s*\(\s*!backup_sepolicy\s*\)\s*\{[^{}]*return\s+-EAGAIN\s*;[^{}]*\}', enabled_code)
    initialization = ('#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 6, 0)\n'
                      '    fake_state.initialized = true;\n'
                      '    fake_state.policy = backup_sepolicy;\n#else')
    if (available is None or initialization not in without_comments(enable)
            or enabled_code.find('fake_state.initialized') < available.end()
            or enable.count('fake_state.policy = backup_sepolicy;') != 1):
        raise ValueError('Kow fake SELinux view must initialize only from an available policy backup')
    for variable, symbol in (('security_dump_masked_av_fn', 'security_dump_masked_av'),
                             ('context_struct_compute_av_fn', 'context_struct_compute_av')):
        statement = variable + ' = (void *)find_kernel_symbol_exact("' + symbol + '");'
        if without_comments(enable).count(statement) != 1 or enable.index(statement) < enable.index('#else'):
            raise ValueError('Kow modern SELinux helper must retain its real native symbol lookup')
    # Enabling the feature publishes the initialized view only after success.
    setter = strip_comments_and_literals(function_source(hide, 'selinux_hide_feature_set'))
    if (not re.search(r'ret\s*=\s*ksu_selinux_hide_enable\(\);\s*if\s*\(!ret\)\s*\{\s*'
                      r'ksu_selinux_hide_running\s*=\s*true;', setter)
            or 'mutex_lock(&selinux_hide_mutex);' not in setter):
        raise ValueError('Kow hiding cannot publish a running state before successful initialization')
    drop = function_source(hide, 'ksu_selinux_hide_drop_backup_if_unused')
    cleanup = ('#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 6, 0)\n'
               '        fake_state.initialized = false;\n'
               '        fake_state.policy = NULL;\n#endif')
    if (cleanup not in without_comments(drop)
            or drop.index(cleanup) > drop.index('sidtab_destroy(backup_sepolicy->sidtab);')
            or 'if (!ksu_selinux_hide_running && backup_sepolicy)' not in drop):
        raise ValueError('Kow policy backup destruction must invalidate the inactive legacy view first')
    apply = strip_comments_and_literals(function_source(rules, 'apply_kernelsu_rules'))
    capture = 'backup_sepolicy = ksu_dup_sepolicy(old_pol);'
    publication = 'rcu_assign_pointer(selinux_state.policy, pol);'
    if (capture not in apply or publication not in apply or apply.index(capture) > apply.index(publication)
            or 'policydb_load_isids(&backup_sepolicy->policydb, backup_sepolicy->sidtab)' not in apply):
        raise ValueError('Kow SELinux hiding must preserve the complete original policy and SID-table backup')


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
    verify_selinux_view(read('feature/selinux_hide.c'), read('selinux/rules.c'))
    header = read('supercall/supercall.h')
    for identifier in ('CHANGE_MANAGER_UID', 'GET_SULOG_DUMP_V2', 'CHANGE_KSUVER',
                       'CHANGE_SPOOF_UNAME', 'CHANGE_KSUFLAGS'):
        if len(re.findall(r'^#define\s+' + identifier + r'\b', header, re.M)) != 1:
            raise ValueError('Missing native Kow toolkit command: ' + identifier)
