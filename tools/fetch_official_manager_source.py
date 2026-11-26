#!/usr/bin/env python3
"""Fetch a fixed public manager commit with its complete native version history."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from manager_variants import MANAGERS, manager_profile
from resolve_build_sources import source_repository_url
from snapshot_ksu_source import (GitCommandError, SnapshotError, check_official_source,
                                 expected_commit, git, git_failure_category,
                                 source_identity, temporary_directory)


def fetch_history(source: Path, reference: str) -> None:
    """A public fetch never inherits GitHub credentials or global URL rewrites."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('GIT_') and key not in {
                       'GH_TOKEN', 'GITHUB_TOKEN', 'SSH_ASKPASS'}}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1')
    command = ['git', '-C', str(source), '-c', 'core.hooksPath=',
               '-c', 'credential.helper=', '-c', 'http.extraHeader=',
               '-c', 'http.followRedirects=false', '-c', 'http.version=HTTP/1.1',
               '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
               'fetch', '--quiet', '--tags', '--no-recurse-submodules', 'origin', reference]
    for attempt in range(3):
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=300, env=environment)
        except subprocess.TimeoutExpired:
            error = GitCommandError('fetch', timeout=300)
        except OSError:
            error = GitCommandError('fetch', category='process execution failure')
        else:
            if not result.returncode:
                return
            category, status = git_failure_category(result.stderr)
            error = GitCommandError('fetch', category=category, returncode=result.returncode,
                                    http_status=status)
        if not error.transient or attempt == 2:
            raise error from None
        time.sleep(attempt + 1)


def fetch_source(output: Path, reference: str, *, variant: str = 'SukiSU') -> dict:
    reference = expected_commit(reference)
    repository = manager_profile(variant)['repository']
    url = source_repository_url(repository)
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or getattr(output, 'is_junction', lambda: False)():
        raise SnapshotError('Official manager source output already exists')
    if not output.parent.is_dir():
        raise SnapshotError('Official manager source parent directory does not exist')
    for parent in (output.parent, *output.parent.parents):
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise SnapshotError('Linked official manager source directory is not permitted')
    with temporary_directory(output.parent, '.official-manager-source-') as temporary:
        source = temporary / 'source'
        source.mkdir()
        git(source, 'init', '--quiet')
        git(source, 'remote', 'add', 'origin', url)
        fetch_history(source, reference)
        git(source, 'checkout', '--quiet', '--detach', reference)
        check_official_source(source, variant)
        identity = source_identity(source, reference)
        git(source, 'fsck', '--connectivity-only', '--no-dangling', timeout=300)
        if output.exists() or output.is_symlink():
            raise SnapshotError('Official manager source output appeared while fetching')
        source.rename(output)
    return identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=MANAGERS, default='SukiSU')
    parser.add_argument('--ref', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        identity = fetch_source(args.output, args.ref, variant=args.variant)
    except (SnapshotError, OSError, ValueError) as error:
        print('Official manager source fetch stopped: ' + str(error), file=sys.stderr)
        return 1
    print('Verified full official manager history: ' + str(identity['git_count']) + ' commits')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
