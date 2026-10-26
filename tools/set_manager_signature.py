#!/usr/bin/env python3
"""Bind the native manager check to the APK produced by this workflow."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import tempfile


DEFAULT_PACKAGE = 'com.cyzrqh.gki.sukisu'
PACKAGE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z')
PACKAGE_SETTING = (
    r'KSU_PACKAGE_NAME[ \t]*\?=[ \t]*'
    r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+'
    r'[ \t]*(?:#[^\r\n]*)?'
)
PACKAGE_BRANCHES = re.compile(
    r'^ifeq[ \t]*\([ \t]*\$\(KSU_IS_PR_BUILD\)[ \t]*,[ \t]*0[ \t]*\)[ \t]*\r?\n'
    + PACKAGE_SETTING + r'\r?\n'
    + r'else[ \t]*\r?\n'
    + PACKAGE_SETTING + r'\r?\n'
    + r'endif[ \t]*(?:\r?\n|$)',
    re.M,
)


def declarations(text: str, key: str) -> list[str]:
    return re.findall(r'^[ \t]*(?:(?:override|export|private|define|undefine)[ \t]+)*'
                      + key + r'\b[^\r\n]*', text, re.M)


def replace_setting(text: str, key: str, value: str) -> str:
    """Reject changed or duplicate declarations before rewriting a native setting."""
    pattern = re.compile(r'^' + key + r'[ \t]*(?:\?=|:=|=)[^\r\n]*(\r?)$', re.M)
    if len(declarations(text, key)) != 1 or len(list(pattern.finditer(text))) != 1:
        raise ValueError(f'Expected one native {key} setting; review the source layout')
    return pattern.sub(lambda match: key + ' := ' + value + match[1], text)


def set_certificate_setting(text: str, key: str, value: str) -> str:
    """BakaSU exposes optional signing inputs rather than default assignments."""
    if declarations(text, key):
        return replace_setting(text, key, value)
    normalized = text.replace('\r\n', '\n')
    if len(re.findall(r'^REPO_NAME[ \t]*:=[ \t]*(?:BakaSU|ReSukiSU)[ \t]*$', normalized, re.M)) != 1:
        raise ValueError(f'Expected one native {key} setting; review the source layout')
    if key == 'KSU_EXPECTED_SIZE':
        anchor = ('ifdef KSU_EXPECTED_SIZE\n'
                  'ccflags-y += -DEXPECTED_SIZE=$(KSU_EXPECTED_SIZE)\n'
                  '$(info -- Custom KernelSU Manager signature size: $(KSU_EXPECTED_SIZE))\nendif')
    elif key == 'KSU_EXPECTED_HASH':
        anchor = ('ifdef KSU_EXPECTED_HASH\n'
                  'ccflags-y += -DEXPECTED_HASH=\\"$(KSU_EXPECTED_HASH)\\"\n'
                  '$(info -- Custom KernelSU Manager signature hash: $(KSU_EXPECTED_HASH))\nendif')
    else:
        raise ValueError('Unsupported optional native manager signing input')
    if normalized.count(anchor) != 1:
        raise ValueError(f'Expected the reviewed optional BakaSU {key} block; review the source layout')
    newline = '\r\n' if '\r\n' in text else '\n'
    return text.replace(anchor.replace('\n', newline), key + ' := ' + value + newline
                        + anchor.replace('\n', newline), 1)


def replace_package(text: str, package: str) -> str:
    # Older fixed SukiSU uses its native optional KSU_MANAGER_PACKAGE macro.
    # Retain this path for builds where SUSFS is disabled or custom refs select
    # that version; do not invent the newer PR/default branch layout there.
    if not declarations(text, 'KSU_PACKAGE_NAME'):
        marker = ('ifdef KSU_MANAGER_PACKAGE\n'
                  'ccflags-y += -DKSU_MANAGER_PACKAGE=\\"$(KSU_MANAGER_PACKAGE)\\"\n'
                  '$(info -- KernelSU Manager package name: $(KSU_MANAGER_PACKAGE))\n'
                  'endif')
        normalized = text.replace('\r\n', '\n')
        if normalized.count(marker) != 1 or '-DKSU_PACKAGE_NAME' in text:
            raise ValueError('Expected a known native manager package setting; review the source layout')
        if declarations(text, 'KSU_MANAGER_PACKAGE'):
            return replace_setting(text, 'KSU_MANAGER_PACKAGE', package)
        newline = '\r\n' if '\r\n' in text else '\n'
        anchor = marker.replace('\n', newline)
        return text.replace(anchor, 'KSU_MANAGER_PACKAGE := ' + package + newline + anchor, 1)
    branches = list(PACKAGE_BRANCHES.finditer(text))
    if len(declarations(text, 'KSU_PACKAGE_NAME')) != 2 or len(branches) != 1:
        raise ValueError('Expected native KSU_PACKAGE_NAME PR/default branches; review the source layout')
    # Keep the native conditional and ?= operators. Both branches must accept
    # the actual APK ID, even when KSU_IS_PR_BUILD is supplied by a kernel build.
    block = re.sub(
        r'^(KSU_PACKAGE_NAME[ \t]*\?=[ \t]*)[^\s#]+',
        lambda match: match[1] + package,
        branches[0][0],
        flags=re.M,
    )
    return text[:branches[0].start()] + block + text[branches[0].end():]


def atomic_write(path: Path, content: bytes) -> None:
    """Replace the complete Kbuild only after all three settings validate."""
    permissions = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, permissions)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def configure(root: Path, size: str, digest: str, package: str = DEFAULT_PACKAGE,
              version_code: str | None = None) -> Path:
    if not re.fullmatch(r'[0-9]+', size) or not 64 <= int(size) <= 4096:
        raise ValueError('Invalid manager certificate size')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise ValueError('Invalid manager certificate SHA256')
    if not PACKAGE.fullmatch(package):
        raise ValueError('Invalid manager package; expected an ASCII Android application ID')
    if version_code is not None and (not re.fullmatch(r'[0-9]+', version_code)
                                     or not 1 <= int(version_code) <= 2_100_000_000):
        raise ValueError('Invalid manager version code')
    candidates = ('KernelSU/kernel/Kbuild', 'common/drivers/kernelsu/Kbuild', 'drivers/kernelsu/Kbuild')
    path = next((root / item for item in candidates if (root / item).is_file()), None)
    if path is None:
        raise ValueError('SukiSU Kbuild not found')
    # A driver directory may link to KernelSU. Replace its real Kbuild file,
    # rather than turning a source link into a standalone generated file.
    path = path.resolve(strict=True)
    original = path.read_bytes().decode('utf-8')
    updated = original
    for key, value in (('KSU_EXPECTED_SIZE', str(int(size))), ('KSU_EXPECTED_HASH', digest.lower())):
        updated = set_certificate_setting(updated, key, value)
    updated = replace_package(updated, package)
    if version_code is not None:
        updated = replace_setting(updated, 'KSU_VERSION', str(int(version_code)))
    if updated != original:
        atomic_write(path, updated.encode('utf-8'))
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path, required=True)
    parser.add_argument('--certificate-size', '--cert-size', required=True)
    parser.add_argument('--certificate-sha256', '--cert-sha256', required=True)
    parser.add_argument('--package', default=DEFAULT_PACKAGE,
                        help='Actual application ID extracted from the signed manager APK')
    parser.add_argument('--version-code', help='Use the same source version code as the signed manager APK')
    arguments = parser.parse_args()
    try:
        path = configure(arguments.kernel_root, arguments.certificate_size, arguments.certificate_sha256,
                         arguments.package, arguments.version_code)
    except (ValueError, OSError) as error:
        raise SystemExit(f'Manager signature configuration failed: {error}') from None
    print(f'KernelSU manager certificate configured: {path}')


if __name__ == '__main__':
    main()
