#!/usr/bin/env python3
"""Download only the official Re-Kernel driver, retrying interrupted transfers."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import time

from snapshot_ksu_source import GitCommandError, SnapshotError, git, temporary_directory


OFFICIAL = 'https://github.com/Sakion-Team/Re-Kernel.git'
REQUIRED = ('Makefile', 'Kconfig', 'rekernel_main.c', 'rekernel.h')
NETWORK_OPTIONS = (
    '-c', 'http.version=HTTP/1.1',
    '-c', 'http.lowSpeedLimit=1024', '-c', 'http.lowSpeedTime=30',
    '-c', 'http.extraHeader=', '-c', 'http.https://github.com/.extraHeader=',
    '-c', 'credential.helper=', '-c', 'http.followRedirects=false',
    '-c', 'protocol.version=2',
)
ATTEMPTS = 3
TOTAL_TIMEOUT = 300


def _remaining(deadline: float, limit: int) -> int:
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise SnapshotError('Official Re-Kernel download exceeded its five-minute deadline')
    return min(limit, remaining)


def _retry(attempt: int, deadline: float, error: GitCommandError) -> None:
    if attempt + 1 == ATTEMPTS:
        raise SnapshotError('Official Re-Kernel download failed after three attempts: '
                            + error.diagnostic) from None
    delay = (5, 15)[attempt]
    if deadline - time.monotonic() <= delay:
        raise SnapshotError('Official Re-Kernel download exceeded its five-minute deadline') from None
    print(f'Re-Kernel transfer interrupted ({error.diagnostic}); retrying in {delay}s', flush=True)
    time.sleep(delay)


def resolve_head(parent: Path, deadline: float) -> str:
    # Resolve once: a retry must not change to a newer moving upstream HEAD.
    for attempt in range(ATTEMPTS):
        try:
            value = git(parent, *NETWORK_OPTIONS, 'ls-remote', '--exit-code', OFFICIAL, 'HEAD',
                        timeout=_remaining(deadline, 45))
        except GitCommandError as error:
            _retry(attempt, deadline, error)
            continue
        rows = value.splitlines()
        if len(rows) != 1 or not re.fullmatch(r'[0-9a-f]{40}\s+HEAD', rows[0]):
            raise SnapshotError('Official Re-Kernel HEAD did not return one full commit identity')
        return rows[0].split()[0]
    raise AssertionError('Unreachable Re-Kernel retry state')


def verify_source(source: Path, reference: str, deadline: float) -> None:
    if git(source, 'rev-parse', '--verify', 'HEAD', timeout=_remaining(deadline, 10)) != reference:
        raise SnapshotError('Official Re-Kernel checkout differs from its selected commit')
    if git(source, 'remote', 'get-url', 'origin', timeout=_remaining(deadline, 10)) != OFFICIAL:
        raise SnapshotError('Re-Kernel source origin differs from the official repository')
    driver = source / 'LKM-Source'
    if driver.is_symlink() or not driver.is_dir():
        raise SnapshotError('Official Re-Kernel source is missing its regular LKM-Source directory')
    entries = git(source, 'ls-tree', '-r', reference, '--', 'LKM-Source',
                  timeout=_remaining(deadline, 10)).splitlines()
    names = set()
    for entry in entries:
        match = re.fullmatch(r'(100644|100755) blob ([0-9a-f]{40})\t(LKM-Source/[^\r\n]+)', entry)
        if not match:
            raise SnapshotError('Re-Kernel LKM source contains a symlink or unsupported Git entry')
        names.add(match[3])
    if any('LKM-Source/' + name not in names for name in REQUIRED):
        raise SnapshotError('Official Re-Kernel source lacks its required driver files')
    for path in {*driver.glob('*.c'), *driver.glob('*.h'), *(driver / name for name in REQUIRED)}:
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise SnapshotError('Official Re-Kernel driver file is missing, empty or not a regular file')
    if git(source, 'status', '--porcelain', '--untracked-files=all', timeout=_remaining(deadline, 10)):
        raise SnapshotError('Official Re-Kernel checkout is not clean')


def download_source(output: Path) -> str:
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise SnapshotError('Re-Kernel output already exists; refusing to overwrite it')
    if not output.parent.is_dir():
        raise SnapshotError('Re-Kernel output parent directory does not exist')
    deadline = time.monotonic() + TOTAL_TIMEOUT
    reference = resolve_head(output.parent, deadline)
    print('Selected official Re-Kernel commit: ' + reference, flush=True)
    for attempt in range(ATTEMPTS):
        try:
            with temporary_directory(output.parent, '.gki-rekernel-') as temporary:
                prepared = temporary / 'source'
                prepared.mkdir()
                git(prepared, 'init', '--quiet', timeout=_remaining(deadline, 10))
                git(prepared, 'remote', 'add', 'origin', OFFICIAL, timeout=_remaining(deadline, 10))
                git(prepared, 'config', 'core.sparseCheckout', 'true', timeout=_remaining(deadline, 10))
                git(prepared, 'config', 'remote.origin.promisor', 'true', timeout=_remaining(deadline, 10))
                git(prepared, 'config', 'remote.origin.partialclonefilter', 'blob:none',
                    timeout=_remaining(deadline, 10))
                # Non-cone mode excludes even root-level binaries and archives.
                (prepared / '.git/info/sparse-checkout').write_text('/LKM-Source/\n', encoding='utf-8')
                git(prepared, *NETWORK_OPTIONS, 'fetch', '--quiet', '--depth=1', '--filter=blob:none',
                    '--no-tags', '--no-recurse-submodules', 'origin', reference,
                    timeout=_remaining(deadline, 120))
                # Checkout may lazily fetch the selected driver blobs. The same
                # HTTP/1.1, low-speed and hard time limits cover that request.
                git(prepared, *NETWORK_OPTIONS, 'checkout', '--quiet', '--detach', reference,
                    timeout=_remaining(deadline, 120))
                verify_source(prepared, reference, deadline)
                if output.exists() or output.is_symlink():
                    raise SnapshotError('Re-Kernel output appeared while downloading; refusing to overwrite it')
                prepared.rename(output)
            return reference
        except GitCommandError as error:
            # Every retry gets a fresh isolated directory, including after a
            # truncated pack or interrupted lazy checkout. Never reuse it.
            _retry(attempt, deadline, error)
    raise AssertionError('Unreachable Re-Kernel retry state')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        reference = download_source(args.output)
    except (OSError, ValueError) as error:
        # Git failures are already classified without raw stderr or credentials.
        reason = str(error) if isinstance(error, SnapshotError) else 'Local source preparation failed'
        print('::error::Re-Kernel source download failed: ' + reason, file=sys.stderr)
        return 1
    print('Verified official Re-Kernel driver: ' + reference, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
