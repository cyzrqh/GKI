#!/usr/bin/env python3
"""Connect an already checked out, immutable SukiSU commit to a kernel tree.

The workflow provides KernelSU from its prepared Git bundle. This helper only
checks that checkout and adds the driver entries; it never downloads, checks
out, stashes or updates SukiSU sources.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


MAKE_ENTRY = 'obj-$(CONFIG_KSU) += kernelsu/'
KCONFIG_ENTRY = 'source "drivers/kernelsu/Kconfig"'
COMMIT = re.compile(r'[0-9a-fA-F]{40}\Z')
MAKE_RULE = re.compile(r'obj-\$\(CONFIG_KSU\)[ \t]*\+=[ \t]*kernelsu/[ \t]*\Z')
KCONFIG_RULE = re.compile(r'source[ \t]+"drivers/kernelsu/Kconfig"[ \t]*\Z')
DATE = re.compile(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}\Z')


@dataclass(frozen=True)
class Integration:
    repository: Path
    drivers: Path
    commit: str
    commit_date: str


def is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def require_directory(path: Path) -> None:
    if is_link(path) or not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError(f'Expected a real directory, not a link: {path}')


def read_regular(path: Path) -> bytes:
    if is_link(path) or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f'Expected a regular file, not a link: {path}')
    return path.read_bytes()


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ['git', '-C', str(repository), *arguments],
        check=False, capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    if result.returncode:
        raise ValueError(f'Cannot verify the local SukiSU checkout ({arguments[0]})')
    return result.stdout.strip()


def active(line: str) -> str:
    return line.split('#', 1)[0].strip()


def add_make_entry(original: bytes) -> bytes:
    text = original.decode('utf-8')
    references = [active(line) for line in text.splitlines()
                  if active(line) and ('kernelsu' in active(line).lower()
                                      or re.match(r'obj-\$\(CONFIG_KSU\)', active(line)))]
    if references:
        if len(references) != 1 or not MAKE_RULE.fullmatch(references[0]):
            raise ValueError('Unexpected or duplicate KernelSU driver rules in drivers/Makefile')
        return original
    newline = '\r\n' if '\r\n' in text else '\n'
    return (text + ('' if not text or text.endswith('\n') else newline)
            + MAKE_ENTRY + newline).encode('utf-8')


def add_kconfig_entry(original: bytes) -> bytes:
    text = original.decode('utf-8')
    references = [active(line) for line in text.splitlines()
                  if active(line) and 'kernelsu' in active(line).lower()]
    if references:
        if len(references) != 1 or not KCONFIG_RULE.fullmatch(references[0]):
            raise ValueError('Unexpected or duplicate KernelSU includes in drivers/Kconfig')
        return original
    newline = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines(keepends=True)
    endmenus = [index for index, line in enumerate(lines) if active(line) == 'endmenu']
    if endmenus:
        lines.insert(endmenus[-1], KCONFIG_ENTRY + newline)
        return ''.join(lines).encode('utf-8')
    # A drivers Kconfig without its own menu can still contain source entries.
    return (text + ('' if not text or text.endswith('\n') else newline)
            + KCONFIG_ENTRY + newline).encode('utf-8')


def stage(path: Path, content: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def integrate(kernel_root: Path, expected_ref: str, github_env: Path | None = None) -> Integration:
    if not COMMIT.fullmatch(expected_ref):
        raise ValueError('SukiSU expected ref must be an immutable full 40-character commit SHA')
    expected_ref = expected_ref.lower()
    if '..' in kernel_root.parts:
        raise ValueError('Kernel root must not contain parent traversal')
    require_directory(kernel_root)
    root = kernel_root.resolve(strict=True)
    repository = root / 'KernelSU'
    for directory in (repository, repository / '.git', repository / 'kernel'):
        require_directory(directory)
    read_regular(repository / 'kernel/Kbuild')
    if Path(git(repository, 'rev-parse', '--show-toplevel')).resolve() != repository:
        raise ValueError('KernelSU must be its own Git checkout')
    found = git(repository, 'rev-parse', '--verify', 'HEAD').lower()
    if found != expected_ref:
        raise ValueError(f'SukiSU checkout mismatch: requested {expected_ref}, found {found}')
    commit_date = git(repository, 'log', '-1', '--date=format:%Y-%m-%d %H:%M:%S %z',
                      '--format=%cd', expected_ref)
    if not DATE.fullmatch(commit_date):
        raise ValueError('Cannot verify the SukiSU commit date')

    common = root / 'common'
    if common.exists() or is_link(common):
        require_directory(common)
    common_drivers = common / 'drivers'
    if common_drivers.exists() or is_link(common_drivers):
        drivers = common_drivers
    else:
        drivers = root / 'drivers'
    require_directory(drivers)
    makefile = drivers / 'Makefile'
    kconfig = drivers / 'Kconfig'
    original_makefile, original_kconfig = read_regular(makefile), read_regular(kconfig)
    updated_makefile = add_make_entry(original_makefile)
    updated_kconfig = add_kconfig_entry(original_kconfig)

    link = drivers / 'kernelsu'
    target = repository / 'kernel'
    create_link = not os.path.lexists(link)
    if not create_link:
        if not link.is_symlink() or os.path.isabs(os.readlink(link)) or link.resolve(strict=True) != target:
            raise ValueError(f'Refusing to replace an unrelated or non-relative kernelsu entry: {link}')

    updates = [(path, updated) for path, original, updated in (
        (makefile, original_makefile, updated_makefile),
        (kconfig, original_kconfig, updated_kconfig),
    ) if original != updated]
    if github_env is not None:
        original_env = read_regular(github_env) if os.path.lexists(github_env) else b''
        line = f'KSU_LATEST_COMMIT_DATE={commit_date}'.encode('ascii')
        if line not in original_env.splitlines():
            updates.append((github_env, original_env + (b'' if not original_env or original_env.endswith(b'\n')
                                                       else b'\n') + line + b'\n'))

    # Validate every required input, then stage all file replacements before
    # touching either driver file. Creating the link first also avoids partial
    # edits when the host cannot create symlinks (e.g. unprivileged Windows).
    staged: list[tuple[Path, Path]] = []
    try:
        for path, content in updates:
            staged.append((path, stage(path, content)))
        if create_link:
            link.symlink_to(os.path.relpath(target, drivers), target_is_directory=True)
        for path, temporary in staged:
            os.replace(temporary, path)
    finally:
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)
    return Integration(repository, drivers, found, commit_date)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path, required=True)
    parser.add_argument('--expected-ref', required=True)
    arguments = parser.parse_args()
    try:
        result = integrate(arguments.kernel_root, arguments.expected_ref,
                           Path(os.environ['GITHUB_ENV']) if os.environ.get('GITHUB_ENV') else None)
    except (ValueError, OSError, UnicodeError) as error:
        raise SystemExit(f'SukiSU local integration failed: {error}') from None
    print(f'SukiSU {result.commit} integrated into {result.drivers}')
    print(f'SukiSU commit date: {result.commit_date}')


if __name__ == '__main__':
    main()
