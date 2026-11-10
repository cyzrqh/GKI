"""Verify both directions of a Linux patch before using the resulting sources."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

from snapshot_ksu_source import temporary_directory


def command(arguments, *, cwd=None, runner=None):
    if runner is None:
        runner = subprocess.run
    result = runner([str(item) for item in arguments], cwd=cwd, capture_output=True,
                    text=True, encoding='utf-8', errors='replace', timeout=240)
    if result.returncode:
        if Path(str(arguments[0])).name.lower() in ('patch', 'patch.exe'):
            phase = 'reverse' if '--reverse' in arguments else 'forward'
            phase += ' dry-run' if '--dry-run' in arguments else ' apply'
            patch_name = Path(str(arguments[arguments.index('-i') + 1])).name if '-i' in arguments else 'unknown.patch'
            diagnostics = result.stdout + '\n' + result.stderr
            for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'SUKISU_APK_PASSWORD', 'SUKISU_APK_KEYSTORE'):
                if secret := os.environ.get(name):
                    diagnostics = diagnostics.replace(secret, '[REDACTED]')
            print(f'Kernel patch {patch_name} failed during {phase}:\n' + diagnostics[-12 * 1024:], file=sys.stderr)
        raise ValueError('Kernel integration command failed: ' + str(arguments[0])
                         + ' (exit ' + str(result.returncode) + ')')
    return result.stdout.strip()


def patch_checked(common: Path, patch: Path, *, execute=command):
    if patch.is_symlink() or not patch.is_file() or not 0 < patch.stat().st_size <= 16 * 1024 * 1024:
        raise ValueError('The selected compatibility patch is missing or invalid')
    data = patch.read_bytes()
    strict_files = ()
    if patch.name == '50_add_susfs_in_gki-android14-6.1.patch':
        from oplus_susfs_headers import adapt_namespace_header
        data = adapt_namespace_header(common, data)
        strict_files = ('fs/namespace.c',)
    elif patch.name == '50_add_susfs_in_gki-android15-6.6.patch':
        from oplus_susfs66 import adapt_susfs66
        data = adapt_susfs66(common, data)
        strict_files = ('fs/namespace.c', 'fs/proc/task_mmu.c')
    elif patch.name == '50_add_susfs_in_gki-android16-6.12.patch':
        from oplus_compat import adapt_susfs_patch
        data = adapt_susfs_patch(common, data)
        strict_files = ('fs/proc/task_mmu.c',)
    with temporary_directory(common.parent, '.kernel-patch-') as temporary:
        pieces = []
        for strict_file in strict_files:
            # Bind reviewed namespace and map hooks without fuzzy placement.
            pattern = rb'(?ms)^diff --git a/' + re.escape(strict_file.encode()) + rb' b/' + re.escape(strict_file.encode()) + rb'\n.*?(?=^diff --git |\Z)'
            matches = list(re.finditer(pattern, data))
            if len(matches) != 1:
                raise ValueError('The adapted official SUSFS file must have exactly one complete patch section')
            selected = matches[0]
            pieces.append((selected[0], '--fuzz=0'))
            data = data[:selected.start()] + data[selected.end():]
        if not strict_files or data.strip():
            pieces.append((data, '--fuzz=3'))
        commands = []
        for index, (content, fuzz) in enumerate(pieces):
            prepared = temporary / (str(index) + '-' + patch.name)
            prepared.write_bytes(content)
            commands.append(['patch', '--batch', fuzz, '-p1', '-i', prepared])
        for flags in (('--forward', '--dry-run'), ('--forward',), ('--reverse', '--dry-run')):
            for options in commands:
                execute([*options, *flags], cwd=common)
