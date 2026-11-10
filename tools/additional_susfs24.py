"""Compose the official SUSFS 2.4 ABI with the native additional managers."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

from ensure_susfs_priority import CONFIG as PRIORITY_CONFIG, OPTION as PRIORITY_OPTION, priority_api
from validate_susfs_hooks import strip_comments_and_literals
from verify_kow_susfs import function_source


VERSION = 'v2.4.0'
PREINIT_OPTION = 'KSU_SUSFS_NATIVE_PREINIT'
PRIORITY_GUARD = 'CONFIG_' + PRIORITY_OPTION
AVD = 'susfs_add_sus_avd_allowed_by_type'
TYPE = 'susfs_add_sus_type'
AVD_CALL = f'{AVD}(src->value, tgt->value, cls->value, node->datum.u.data);'
TYPE_CALL = f'{TYPE}(type_name, type->value);'
AVD_DECL = f'extern int {AVD}(u32 s_type, u32 t_type, u32 c_type, u32 orig_avd_allowed);'
TYPE_DECL = f'extern int {TYPE}(const char *secctx, u32 type_value);'
PRIORITY_CASE = 'CMD_SUSFS_SET_PRIORITY_UID_FILTER'
PRIORITY_SETTER = 'susfs_set_priority_uid_filter(arg);'
PRIORITY_REFRESH = 'susfs_parse_priority_app_strings();'


def read(path: Path, root: Path) -> str:
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError('Missing reviewed SUSFS 2.4 source: ' + path.name)
    return path.read_text(encoding='utf-8')


def dependency_version(dependency: Path) -> str | None:
    header = dependency / 'kernel_patches/include/linux/susfs.h'
    if not header.exists() and not header.is_symlink():
        return None  # Legacy callers may only provide a 2.3 port patch.
    content = read(header, dependency.resolve(strict=True))
    versions = re.findall(r'(?m)^\s*#define\s+SUSFS_VERSION\s+"(v[0-9.]+)"\s*$', content)
    if len(versions) != 1:
        raise ValueError('Expected one real SUSFS version in the selected official header')
    return versions[0]


def once(text: str, before: str, after: str, purpose: str) -> str:
    if text.count(before) != 1:
        raise ValueError('Review the native SUSFS 2.4 ' + purpose + ' context')
    return text.replace(before, after, 1)


def inside(text: str, name: str, edit) -> str:
    original = function_source(text, name)
    return once(text, original, edit(original), name)


def guard(lines: str, feature: str = 'CONFIG_KSU_SUSFS') -> str:
    return f'#ifdef {feature}\n{lines}\n#endif\n'


def policy_hooks(text: str, *, baka_layout: bool = False) -> str:
    """Record each new type and the original AV bits, before modifying them."""
    if AVD in text or TYPE in text:
        extern = re.compile(r'(?m)^extern int ' + AVD + r'\(u32 s_type, u32 t_type, '
                            r'u(?:16|32) (?:type|c_type), u32 orig_avd_allowed\);$')
        matches = list(extern.finditer(text))
        if len(matches) != 1 or text.count(TYPE_DECL) != 1:
            raise ValueError('Review the existing native SUSFS policy table declarations')
        text = once(text, matches[0].group(), AVD_DECL, 'AVD ABI')
    else:
        include = '#include "ss/symtab.h"\n'
        text = once(text, include, include + '\n' + guard(AVD_DECL + '\n' + TYPE_DECL), 'SELinux includes')

    def avd(body: str) -> str:
        anchor = '        if (invert) {\n'
        if (body.count(anchor) != 1 or 'if (!node)\n                return false;\n        }' not in body
                or 'node->datum.u.data &= ' not in body or 'node->datum.u.data |= ' not in body):
            raise ValueError('Review the native SELinux AV rule mutation path')
        if AVD in body:
            if body.count(AVD_CALL) != 1 or body.index(AVD_CALL) > body.index(anchor):
                raise ValueError('Native SUSFS must preserve AV permissions before mutation')
            return body
        return once(body, anchor, guard('        ' + AVD_CALL) + '\n' + anchor, 'AV rule')

    def types(body: str) -> str:
        if baka_layout:
            # BakaSU retains native array, vendor-array and flex-array paths.
            # Each updates the role map only after installing the new type.
            tail = ('        ebitmap_set_bit(&db->role_val_to_struct[i]->types, value - 1, 1);\n'
                    '    }\n')
            returned = '    return true;\n'
            registration = guard('    ' + TYPE_CALL)
            tails = ((tail + '\n', 2), (tail, 1))
            if TYPE in body:
                if body.count(TYPE_CALL) != 3 or any(
                        body.count(context + registration + returned) != count for context, count in tails):
                    raise ValueError('Review the BakaSU SUSFS policy type registration branches')
                return body
            if any(body.count(context + returned) != count for context, count in tails):
                raise ValueError('Review the native BakaSU SUSFS policy type allocation branches')
            for context, _ in tails:
                body = body.replace(context + returned, context + registration + returned)
            return body
        if TYPE in body:
            if body.count(TYPE_CALL) != 1 or not body.endswith('    return true;\n}'):
                raise ValueError('Review the existing SUSFS policy type registration')
            return body
        tail = ('        ebitmap_set_bit(&db->role_val_to_struct[i]->types, value - 1, 1);\n'
                '    }\n\n    return true;\n}')
        return once(body, tail, tail.replace('    return true;\n}',
                                             guard('    ' + TYPE_CALL) + '    return true;\n}'), 'policy type')

    updated = inside(inside(text, 'add_rule_raw', avd), 'add_type', types)
    for call, count in ((AVD_CALL, 1), (TYPE_CALL, 3 if baka_layout else 1)):
        if updated.count(call) != count:
            raise ValueError('Incomplete native SUSFS policy registration: ' + call)
    return updated


def priority_dispatch(text: str) -> str:
    def edit(body: str) -> str:
        compact = re.sub(r'\s+', '', strip_comments_and_literals(body))
        positive = 'if(magic2==SUSFS_MAGIC&&current_uid().val==0){switch(cmd){'
        negative = 'if(magic2!=SUSFS_MAGIC||current_uid().val!=0)return-EINVAL;switch(cmd){'
        gates = [match.end() for gate in (positive, negative)
                 for match in re.finditer(re.escape(gate), compact)]
        marker = 'caseCMD_SUSFS_ENABLE_AVC_LOG_SPOOFING:'
        if len(gates) != 1 or compact.count(marker) != 1:
            raise ValueError('SUSFS priority command must remain inside the native root-only dispatch')
        begin, end, depth = gates[0], 0, 1
        for index in range(begin, len(compact)):
            depth += (compact[index] == '{') - (compact[index] == '}')
            if depth == 0:
                end = index
                break
        if not begin <= compact.index(marker) < end:
            raise ValueError('SUSFS priority command must remain inside the native root-only dispatch')
        existing = re.compile(r'(?m)^([ \t]*)case ' + PRIORITY_CASE + r':\n'
                              r'\1[ \t]+susfs_set_priority_uid_filter\(arg\);\n'
                              r'\1[ \t]+return 0;\n')
        if PRIORITY_CASE in body:
            found = list(existing.finditer(body))
            case = 'case' + PRIORITY_CASE + ':'
            if (len(found) != 1 or compact.count(case) != 1
                    or not begin <= compact.index(case) < end):
                raise ValueError('Review the native SUSFS priority UID command')
            match = found[0]
            before = body[:match.start()].rstrip().splitlines()[-1]
            if before == '#ifdef ' + PRIORITY_GUARD:
                return body
            return body[:match.start()] + guard(match[0].rstrip('\n'), PRIORITY_GUARD) + body[match.end():]
        anchor = re.search(r'(?m)^([ \t]*)case CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING:', body)
        indent = anchor[1] if anchor else ''
        if not anchor or not indent:
            raise ValueError('Review the SUSFS command indentation')
        case = f'{indent}case {PRIORITY_CASE}:\n{indent}    {PRIORITY_SETTER}\n{indent}    return 0;'
        return body[:anchor.start()] + guard(case, PRIORITY_GUARD) + body[anchor.start():]

    return inside(text, 'ksu_handle_sys_reboot', edit)


def priority_observer(text: str, *, native_fina_guard: bool = False) -> str:
    if '#include <linux/susfs.h>' not in text:
        anchor = '#include <linux/version.h>\n'
        text = once(text, anchor, anchor + guard('#include <linux/susfs.h>'), 'priority observer header')
    scan = '    track_throne(false);\n'
    exiting = 'if (current->flags & PF_EXITING)\n        return;'
    if PRIORITY_REFRESH in text:
        if native_fina_guard:
            callback = function_source(text, 'ksu_track_throne_tw_func')
            # FinaSU already performs this refresh after the safe deferred
            # scan. Keep its additional command-availability condition.
            existing = re.search(
                r'(?m)^#if defined\(CONFIG_KSU_SUSFS\) && defined\(CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER\)'
                r' &&[ \t]+(?:\\\n[ \t]+)?defined\(CMD_SUSFS_SET_PRIORITY_UID_FILTER\)\n'
                r'    susfs_parse_priority_app_strings\(\);\n#endif\n', callback)
            if existing:
                if (text.count(PRIORITY_REFRESH) != 1 or callback.count(scan) != 1
                        or callback.count(exiting) != 1 or callback.index(exiting) >= callback.index(scan)
                        or existing.start() != callback.index(scan) + len(scan)):
                    raise ValueError('The native FinaSU priority UID refresh must follow the safe deferred scan')
                return text
        old = re.compile(r'(?m)^#ifdef CONFIG_KSU_SUSFS(?:_PRIORITY_UID_FILTER)?\n'
                         r'(?:[ \t]*//[^\n]*\n)?[ \t]*susfs_parse_priority_app_strings\(\);\n'
                         r'#endif[^\n]*\n')
        text = old.sub('', text)
        # Kow's older port launched the refresh in the fsnotify callback.
        # SUSFS 2.4 starts a kthread; run it after the safe deferred scan.
        old_kow = '    // Renew the getpriority uid bitmap filter\n    ' + PRIORITY_REFRESH + '\n\n'
        if old_kow in text:
            text = inside(text, 'ksu_defer_track_throne',
                          lambda body: once(body, old_kow, '', 'Kow fsnotify refresh'))
        if PRIORITY_REFRESH in text:
            raise ValueError('Review the existing native SUSFS priority refresh context')

    def refresh(body: str) -> str:
        if exiting not in body:
            raise ValueError('The priority UID refresh must run in the native safe task callback')
        return once(body, scan, scan + guard('    ' + PRIORITY_REFRESH, PRIORITY_GUARD),
                    'priority observer callback')

    return inside(text, 'ksu_track_throne_tw_func', refresh)


def priority_thread(text: str) -> str:
    """Refresh Next's default threaded tracker after a real package scan."""
    if PRIORITY_REFRESH in text:
        raise ValueError('Review the existing threaded SUSFS priority refresh')
    anchor = '#include <linux/version.h>\n'
    text = once(text, anchor, anchor + guard('#include <linux/susfs.h>'), 'priority tracker header')

    def refresh(body: str) -> str:
        scan = '\ttrack_throne_function(prune_only);\n'
        update = guard('\tif (!prune_only)\n\t\t' + PRIORITY_REFRESH, PRIORITY_GUARD)
        return once(body, scan, scan + update, 'threaded package scan')

    return inside(text, 'throne_tracker_thread', refresh)


def verify_fina_preinit(kernel: Path, source: Path, preinit: str) -> None:
    # Fina tracks the real runner descriptor in direct_preinit_fd instead of
    # the output parameter used by the other managers. Preserve both cleanup
    # paths and the native inline backend without introducing a fake provider.
    setup = function_source(preinit, 'ksu_setup_preinit_file')
    post = function_source(preinit, 'ksu_preinit_handle_post_execveat')
    required = ('fd_install(fd, file);', 'ksu_close_fd(fd);', 'direct_preinit_fd = fd;', 'return fd;')
    cleanup = ('ksu_close_fd(direct_preinit_fd);', 'putname(direct_swapped_filename);',
               'direct_preinit_fd = -1;', 'return failed;')
    util = read(kernel / 'include/util.h', source)
    if (not all(setup.count(item) == 1 for item in required)
            or not all(post.count(item) == 1 for item in cleanup)
            or '#define ksu_close_fd(fd) ({ ksyscall(close, fd); })' not in util):
        raise ValueError('FinaSU must retain both real inline preinit descriptor cleanup paths')


def apply_official_tiann(source: Path, dependency: Path) -> None:
    from additional_manager_integration import verify_native_policy_generation, verify_native_preinit_cleanup

    patch = dependency / 'kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch'
    raw = read(patch, dependency).encode('utf-8')
    rules = source / 'kernel/selinux/rules.c'
    original = read(rules, source)
    # The official patch uses policy generation zero. Preserve the original
    # publication, cache/netlink/status notifications and type metadata instead.
    if raw.count(b'+    susfs_set_batch_sid();\n') != 1:
        raise ValueError('Review the official SUSFS 2.4 SELinux SID publication hunk')
    anchor = '    reset_avc_cache(pol->latest_granting);\nout_unlock:'
    updated = once(original, anchor, anchor.replace('\nout_unlock:', '\n\n    susfs_set_batch_sid();\nout_unlock:'),
                   'policy generation')
    command = ['git', '-C', str(source), '-c', 'core.autocrlf=false', 'apply', '--recount',
               '--whitespace=nowarn', '--exclude=kernel/selinux/rules.c']
    for arguments in (('--check',), (), ('--reverse', '--check')):
        result = subprocess.run([*command, *arguments, '-'], input=raw, capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError('Official SUSFS 2.4 patch does not fit this native KernelSU source')
    rules.write_text(updated, encoding='utf-8', newline='\n')
    verify_native_preinit_cleanup(source)
    verify_native_policy_generation(source)


def complete(source: Path, dependency: Path, variant: str) -> None:
    from additional_manager_integration import verify_native_preinit_cleanup

    source, dependency = source.resolve(strict=True), dependency.resolve(strict=True)
    header = read(dependency / 'kernel_patches/include/linux/susfs.h', dependency)
    implementation = read(dependency / 'kernel_patches/fs/susfs.c', dependency)
    if (priority_api(header, implementation) != 'conditional'
            or not re.search(r'(?m)^int ' + AVD + r'\(u32 s_type, u32 t_type, u32 c_type, u32 orig_avd_allowed\)\s*\{', implementation)
            or not re.search(r'(?m)^int ' + TYPE + r'\(const char \*secctx, u32 type_value\)\s*\{', implementation)):
        raise ValueError('Official SUSFS 2.4 implementation does not provide the reviewed priority/policy ABI')
    kernel = source / 'kernel'
    kconfig = kernel / 'Kconfig'
    text = read(kconfig, source)
    if not re.search(r'(?m)^config KSU_SUSFS\s*$', text):
        raise ValueError('Selected manager has no real native SUSFS Kconfig')
    if not re.search(r'(?m)^config ' + PRIORITY_OPTION + r'\s*$', text):
        text += PRIORITY_CONFIG
    if (kernel / 'preinit/preinit.c').is_file():
        preinit = read(kernel / 'preinit/preinit.c', source)
        for name in ('ksu_setup_preinit_file', 'ksu_preinit_handle_post_execveat'):
            function_source(preinit, name)
        if 'preinit/preinit.o' not in read(kernel / 'Kbuild', source):
            raise ValueError('Native modern preinit implementation is not linked')
        dependency = 'KSU_SUSFS'
        if variant == 'FinaSU':
            verify_fina_preinit(kernel, source, preinit)
            hook = '\tdefault KSU_HOOK_TRACEPOINT\n'
            text = once(text, hook, '\tdefault KSU_HOOK_SUSFS_INLINE if ARM64 && KSU=y && KSU_SUSFS\n' + hook,
                        'FinaSU inline hook backend')
            dependency += ' && KSU_HOOK_SUSFS_INLINE'
        else:
            verify_native_preinit_cleanup(source)
        if not re.search(r'(?m)^config ' + PREINIT_OPTION + r'\s*$', text):
            text += f'\nconfig {PREINIT_OPTION}\n\tdef_bool y\n\tdepends on {dependency}\n'
    else:
        legacy = function_source(read(kernel / 'feature/sucompat.c', source), 'ksu_handle_execveat')
        if 'ksu_handle_execveat_ksud(' not in legacy or 'ksu_handle_execveat_sucompat(' not in legacy:
            raise ValueError('Selected source lacks the real legacy execve/SUSFS integration')

    observer = kernel / 'manager/pkg_observer.c'
    dispatch = kernel / ('extension/susfs.c' if variant == 'FinaSU' else 'supercall/dispatch.c')
    policy = kernel / 'selinux/sepolicy.c'
    updates = {kconfig: text, observer: priority_observer(read(observer, source), native_fina_guard=variant == 'FinaSU'),
               dispatch: priority_dispatch(read(dispatch, source)), policy: policy_hooks(read(policy, source))}
    if variant == 'KernelSU-Next' and 'config KSU_THRONE_TRACKER_ALWAYS_THREADED\n' in text:
        tracker = kernel / 'manager/throne_tracker.c'
        updates[tracker] = priority_thread(read(tracker, source))
    for path, updated in updates.items():
        path.write_text(updated, encoding='utf-8', newline='\n')


def integrate(source: Path, dependency: Path, variant: str, apply_legacy) -> None:
    from additional_manager_kpm import with_susfs

    source, dependency = source.resolve(strict=True), dependency.resolve(strict=True)

    def apply() -> None:
        if variant == 'KernelSU' and (source / 'kernel/preinit/preinit.c').is_file():
            apply_official_tiann(source, dependency)
        elif variant != 'FinaSU':
            apply_legacy()
        complete(source, dependency, variant)

    # Probe the official patch only after undoing the already-verified KPM
    # changes; otherwise its native Kbuild/core/dispatch context conflicts.
    with_susfs(source, variant, apply)
