"""Apply the reviewed KowSU inode/SID port without changing the older port."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

from snapshot_ksu_source import temporary_directory


# The full official commit documents the source audited for this rebase. A
# manager-only commit may also use it if its two native Git trees are identical.
AUDITED_REF = '21787ce135c988bae4244a44cfcb5a542aa3347f'
KERNEL_TREE = '0f10ede759f8a51481ec238f3c2893b510aa6ca9'
UAPI_TREE = '6331ac1ff69acd30de971cad79ef9c8ed03e9920'
PATCHED_KERNEL_TREE = '41e0a90b247f04060248acc01f35c979761e85a7'
PATCH_SHA256 = 'b4a78038f28cc0973fff5568a0bec875ea3ac288a261902602bdc330245e1827'
PATCH = Path(__file__).resolve().parent / 'patches/kow-susfs23-inode.patch'


def apply_reviewed_kow_port(source: Path) -> bool:
    """Return False for an unrecognized native tree; validate before writing."""
    source = source.resolve(strict=True)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('GIT_CONFIG_') and key not in {
                       'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                       'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_SHALLOW_FILE'}}
    environment.update(GIT_NO_REPLACE_OBJECTS='1', GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    command = ['git', '-C', str(source), '-c', 'core.autocrlf=false', '-c', 'core.longpaths=true',
               '-c', 'core.hooksPath=', '-c', 'core.fsmonitor=false']

    def git(*args: str, env=None, data=None, status=(0,)):
        try:
            result = subprocess.run([*command, *args], input=data, env=env or environment,
                                    capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Could not validate the reviewed KowSU SUSFS port') from None
        if result.returncode not in status:
            raise ValueError('Reviewed KowSU SUSFS port failed Git ' + args[0])
        return result

    native_trees = git('rev-parse', 'HEAD:kernel', 'HEAD:uapi').stdout.decode('ascii').splitlines()
    if native_trees != [KERNEL_TREE, UAPI_TREE]:
        return False
    original = git('rev-parse', 'HEAD^{tree}').stdout.strip()
    raw = PATCH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PATCH_SHA256:
        raise ValueError('Reviewed KowSU SUSFS patch digest differs')
    objects = git('rev-parse', '--path-format=absolute', '--git-path', 'objects').stdout.decode('utf-8').strip()
    object_dir = Path(objects).resolve(strict=True)
    if any(char in objects for char in ('\r', '\n')) or not object_dir.is_dir():
        raise ValueError('Invalid KowSU Git object directory')

    # All Git objects and index writes for this proof go to a separate temporary
    # directory. The source worktree, index and object store stay untouched.
    with temporary_directory(source.parent, '.kow-susfs-proof-') as temporary:
        scratch = temporary / 'objects'
        (scratch / 'info').mkdir(parents=True)
        (scratch / 'info/alternates').write_bytes(object_dir.as_posix().encode('utf-8') + b'\n')
        isolated = {**environment, 'GIT_INDEX_FILE': str(temporary / 'index'),
                    'GIT_OBJECT_DIRECTORY': str(scratch)}
        git('read-tree', 'HEAD', env=isolated)
        diff = ('diff', '--quiet', '--no-ext-diff', '--no-textconv', '--', 'kernel', 'uapi')
        native = git(*diff, env=isolated, status=(0, 1)).returncode == 0
        git('apply', '--cached', '--whitespace=error', '-', data=raw, env=isolated)
        patched = git('write-tree', env=isolated).stdout.decode('ascii').strip()
        observed = git('rev-parse', f'{patched}:kernel', f'{patched}:uapi', env=isolated)
        if observed.stdout.decode('ascii').splitlines() != [PATCHED_KERNEL_TREE, UAPI_TREE]:
            raise ValueError('KowSU SUSFS patch differs from the reviewed native result')
        applied = git(*diff, env=isolated, status=(0, 1)).returncode == 0
        git('apply', '--cached', '--reverse', '--whitespace=nowarn', '-', data=raw, env=isolated)
        if git('write-tree', env=isolated).stdout.strip() != original:
            raise ValueError('KowSU SUSFS patch could not reverse to the exact official source')

    if applied:
        return True
    if not native:
        raise ValueError('KowSU native source differs from the clean or already reviewed SUSFS tree')
    git('apply', '--check', '--whitespace=error', '-', data=raw)
    git('apply', '--whitespace=error', '-', data=raw)
    return True
