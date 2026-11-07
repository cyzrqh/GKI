"""Keep Next's native setprocattr validation in the reviewed SUSFS port."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess


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
OLD_PORT_PREFIX = r'''    /* Obtain a SID for the context, if one was specified. */
    if (size && str[0]) {
        bool newline_prefix = (str[0] == '\n');

        if (newline_prefix && size > 1) {
            str[0] = 'u';
        }
        if (str[0] != '\n') {
            if (str[size - 1] == '\n') {
                str[size - 1] = 0;
                size--;
            }
            error2 = __security_context_to_sid_with_policy(backup_sepolicy, value, size, &tmp_sid, SECSID_NULL,
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
CONFLICT = re.compile(r'^<<<<<<< [^\n]*\n(.*?)^\|\|\|\|\|\|\| [^\n]*\n(.*?)'
                      r'^=======\n(.*?)^>>>>>>> [^\n]*\n', re.M | re.S)
MARKERS = re.compile(r'^(?:<<<<<<<|\|\|\|\|\|\|\||=======|>>>>>>>)', re.M)


def resolved_text(conflicted: str, official: str, *, reverse: bool) -> str | None:
    """Recognize only the existing upstream newline fix and the old port hunk."""
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
