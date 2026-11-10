"""Keep Next's native setprocattr validation in the reviewed SUSFS port."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
from textwrap import indent


PATH = 'kernel/feature/selinux_hide.c'
NATIVE_PREFIX = r'''    /* Keep every non-empty value on the backup validation path */
    if (size && str[0]) {
        if (str[size - 1] == '\n') {
            str[size - 1] = 0;
            size--;
'''
OLD_PREFIX = NATIVE_PREFIX.replace('Keep every non-empty value on the backup validation path',
                                  'Obtain a SID for the context, if one was specified.').replace(
                                      'if (size && str[0])', r"if (size && str[0] && str[0] != '\n')")
CONTEXT_COMMENT = '    /* Obtain a SID for the context, if one was specified. */\n'
OLD_PORT_CONTEXT = CONTEXT_COMMENT + r'''    if (size && str[0]) {
        bool newline_prefix = (str[0] == '\n');

        if (newline_prefix && size > 1) {
            str[0] = 'u';
        }
        if (str[0] != '\n') {
            if (str[size - 1] == '\n') {
                str[size - 1] = 0;
                size--;
            }
'''
OLD_PORT_PREFIX = OLD_PORT_CONTEXT + r'''            error2 = __security_context_to_sid_with_policy(backup_sepolicy, value, size, &tmp_sid, SECSID_NULL,
                                                           GFP_KERNEL, &sid, &error);
            if (newline_prefix) {
                str[0] = '\n';
                return -EINVAL;
            }
            if (error2 || error)
                return error2 ?: error;
'''
NATIVE_CALL = '''        error2 = security_context_to_sid_with_policy(backup_sepolicy, value, size, &tmp_sid, SECSID_NULL, GFP_KERNEL,
                                                     &sid, &error);
        if (error2 || error)
            return error2 ?: error;
'''
PORT_CALL = NATIVE_CALL.replace('security_context_to_sid_with_policy(', '__security_context_to_sid_with_policy(', 1)
CLOSE_INNER = '        }\n'
CLOSE_OUTER = '    }\n'
NATIVE_BLOCK = NATIVE_PREFIX + CLOSE_INNER + NATIVE_CALL + CLOSE_OUTER
PORT_BLOCK = NATIVE_PREFIX + CLOSE_INNER + PORT_CALL + CLOSE_OUTER
OLD_DECLARATIONS = '''
static int security_context_to_sid_with_policy(struct selinux_policy *policy, const char *scontext, u32 scontext_len,
                                               u32 *sid, u32 def_sid, gfp_t gfp_flags, u32 *orig_sid_p, int *orig_rc_p);
static int security_sid_to_context_with_policy(struct selinux_policy *policy, u32 sid, char **scontext,
                                               u32 *scontext_len);
static void security_compute_av_user_with_policy(struct selinux_policy *policy, u32 ssid, u32 tsid, u16 tclass,
                                                 struct av_decision *avd);
static void (*security_dump_masked_av_fn)(struct policydb *policydb, struct context *scontext, struct context *tcontext,
                                          u16 tclass, u32 permissions, const char *reason) = NULL;
static void (*context_struct_compute_av_fn)(struct policydb *policydb, struct context *scontext,
                                            struct context *tcontext, u16 tclass, struct av_decision *avd,
                                            struct extended_perms *xperms) = NULL;

'''
LIVE_DECLARATION = 'static int security_context_to_sid_live(const char *scontext, u32 scontext_len, u32 *sid);\n'
NATIVE_LIVE_DECLARATIONS = OLD_DECLARATIONS.replace('static void security_compute_av_user_with_policy(',
                                                   LIVE_DECLARATION + 'static void security_compute_av_user_with_policy(')
PORTED_LIVE_DECLARATION = '\n' + LIVE_DECLARATION + '\n'
NATIVE_LIVE_CALL = '''        error = security_context_to_sid_live(value, size, &sid);
        if (error == -EAGAIN) {
            // stale type mark, use the dual parse
            error2 = security_context_to_sid_with_policy(backup_sepolicy, value, size, &tmp_sid, SECSID_NULL,
                                                         GFP_KERNEL, &sid, &error);
            if (error2 || error)
                return error2 ?: error;
        } else if (error) {
            return error;
        }
'''
NATIVE_LIVE_BLOCK = OLD_PREFIX + CLOSE_INNER + NATIVE_LIVE_CALL + CLOSE_OUTER
# Preserve the reviewed newline rejection path. For ordinary contexts the
# official live parser owns validation, with the existing SUSFS internal ABI
# used only for its -EAGAIN fallback.
NEWLINE_VALIDATION = r'''            if (newline_prefix) {
                __security_context_to_sid_with_policy(backup_sepolicy, value, size, &tmp_sid, SECSID_NULL,
                                                      GFP_KERNEL, &sid, &error);
                str[0] = '\n';
                return -EINVAL;
            }
'''
PORTED_LIVE_PREFIX = OLD_PORT_CONTEXT + NEWLINE_VALIDATION + indent(
    NATIVE_LIVE_CALL.replace('security_context_to_sid_with_policy(', '__security_context_to_sid_with_policy(', 1), '    ')
CONFLICT = re.compile(r'^<<<<<<< [^\n]*\n(.*?)^\|\|\|\|\|\|\| [^\n]*\n(.*?)'
                      r'^=======\n(.*?)^>>>>>>> [^\n]*\n', re.M | re.S)
MARKERS = re.compile(r'^(?:<<<<<<<|\|\|\|\|\|\|\||=======|>>>>>>>)', re.M)


def resolve_live_sid(conflicted: str, official: str, *, reverse: bool) -> str | None:
    """Resolve the declaration move and live SID parser as one reviewed change."""
    if official.count(NATIVE_LIVE_DECLARATIONS) != 1 or official.count(NATIVE_LIVE_BLOCK) != 1:
        return None
    matches = list(CONFLICT.finditer(conflicted))
    if len(matches) != 2:
        return None
    declaration, parser = matches
    if not conflicted[:declaration.start()].endswith('static write_op_fn *selinux_write_op;\n'):
        return None
    if not conflicted[declaration.end():].startswith('static write_op_fn *context_write, *access_write;\n'):
        return None
    if reverse:
        expected = (PORTED_LIVE_PREFIX.removeprefix(CONTEXT_COMMENT),
                    OLD_PORT_PREFIX.removeprefix(CONTEXT_COMMENT), OLD_PREFIX.removeprefix(CONTEXT_COMMENT))
        if declaration.groups() != (PORTED_LIVE_DECLARATION, '', OLD_DECLARATIONS) or parser.groups() != expected:
            return None
        begin = parser.start() - len(CONTEXT_COMMENT)
        tail = CLOSE_INNER + NATIVE_CALL + CLOSE_OUTER
        end = parser.end() + len(tail)
        if conflicted[begin:parser.start()] != CONTEXT_COMMENT or conflicted[parser.end():end] != tail:
            return None
        replacement = NATIVE_LIVE_BLOCK
        declarations = NATIVE_LIVE_DECLARATIONS
    else:
        if declaration.groups() != (NATIVE_LIVE_DECLARATIONS, OLD_DECLARATIONS, ''):
            return None
        if parser.groups() != (NATIVE_LIVE_CALL, NATIVE_CALL, ''):
            return None
        context = OLD_PORT_PREFIX + CLOSE_INNER
        begin = parser.start() - len(context)
        end = parser.end() + len(CLOSE_OUTER)
        if conflicted[begin:parser.start()] != context or conflicted[parser.end():end] != CLOSE_OUTER:
            return None
        replacement = PORTED_LIVE_PREFIX + CLOSE_INNER + CLOSE_OUTER
        declarations = PORTED_LIVE_DECLARATION
    before_parser = conflicted[declaration.end():begin]
    if ('my_setprocattr(const char *name, void *value, size_t size)' not in before_parser
            or not before_parser.endswith('PROCESS__SETCURRENT, NULL);\n    if (error)\n        return error;\n\n')):
        return None
    updated = conflicted[:declaration.start()] + declarations + before_parser + replacement + conflicted[end:]
    if MARKERS.search(updated) or (reverse and updated != official):
        return None
    return updated


def resolved_text(conflicted: str, official: str, *, reverse: bool) -> str | None:
    """Resolve the two reviewed upstream input-validation changes."""
    if NATIVE_LIVE_BLOCK in official:
        return resolve_live_sid(conflicted, official, reverse=reverse)
    if official.count(NATIVE_BLOCK) != 1:
        return None
    matches = list(CONFLICT.finditer(conflicted))
    if reverse:
        if (len(matches) != 2 or matches[0].groups() != (NATIVE_PREFIX, OLD_PORT_PREFIX, OLD_PREFIX)
                or matches[1].groups() != (PORT_CALL, '', NATIVE_CALL)
                or conflicted[matches[0].end():matches[1].start()] != CLOSE_INNER):
            return None
        end = matches[1].end()
        replacement = NATIVE_BLOCK
    else:
        if len(matches) != 1 or matches[0].groups() != (NATIVE_PREFIX, OLD_PREFIX, OLD_PORT_PREFIX):
            return None
        end = matches[0].end() + len(CLOSE_INNER)
        if conflicted[matches[0].end():end] != CLOSE_INNER:
            return None
        replacement = PORT_BLOCK
    if conflicted[end:end + len(CLOSE_OUTER)] != CLOSE_OUTER:
        return None
    start = matches[0].start()
    # Restrict the substitution to my_setprocattr's real input validation.
    prefix = conflicted[:start]
    if ('my_setprocattr(const char *name, void *value, size_t size)' not in prefix
            or not prefix.endswith('PROCESS__SETCURRENT, NULL);\n    if (error)\n        return error;\n\n')):
        return None
    updated = prefix + replacement + conflicted[end + len(CLOSE_OUTER):]
    if MARKERS.search(updated) or (reverse and updated != official):
        return None
    return updated


def resolve_setprocattr(source: Path, *, reverse: bool = False) -> bool:
    git = ['git', '-C', str(source)]
    unmerged = subprocess.check_output([*git, 'diff', '--name-only', '--diff-filter=U'], timeout=30)
    if PATH.encode() not in unmerged.splitlines():
        return False
    path = source / PATH
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 262144
            or not path.resolve().is_relative_to(source.resolve())):
        return False
    official = subprocess.check_output([*git, 'show', 'HEAD:' + PATH], timeout=30).decode('utf-8')
    updated = resolved_text(path.read_text(encoding='utf-8'), official, reverse=reverse)
    if updated is None:
        return False
    path.write_text(updated, encoding='utf-8', newline='\n')
    subprocess.run([*git, '-c', 'core.autocrlf=false', 'add', '--', PATH], check=True, timeout=30)
    return True
