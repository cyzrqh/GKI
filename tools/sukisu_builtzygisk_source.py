"""Validate the BuiltZygisk dependency declared by a frozen Nova checkout."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

from snapshot_ksu_source import SnapshotError, git


SUBMODULE = 'external/BuiltZygisk'
OFFICIAL_URLS = frozenset(
    base + suffix
    for base in ('https://github.com/SukiSU-Nova/BuiltZygisk',
                 'https://git.zako.shirkneko.top:20004/shirkneko/BuiltZygisk')
    for suffix in ('', '.git')
)


def verify_source(source: Path) -> tuple[str, str]:
    """Return only the committed official URL and exact gitlink revision."""
    source = Path(source)
    url = git(source, 'config', '--blob', 'HEAD:.gitmodules', '--get-all',
              'submodule.' + SUBMODULE + '.url')
    if url not in OFFICIAL_URLS:
        raise SnapshotError('Unexpected official Nova BuiltZygisk repository.')
    path = git(source, 'config', '--blob', 'HEAD:.gitmodules', '--get-all',
               'submodule.' + SUBMODULE + '.path')
    if path != SUBMODULE:
        raise SnapshotError('The Nova BuiltZygisk dependency path requires review.')
    if git(source, 'diff', '--name-only', 'HEAD', '--', '.gitmodules'):
        raise SnapshotError('The Nova BuiltZygisk dependency declarations differ from the fixed source.')
    entry = git(source, 'ls-tree', '--format=%(objectmode) %(objectname)', 'HEAD', '--', SUBMODULE)
    match = re.fullmatch(r'160000 ([0-9a-f]{40})', entry)
    if match is None:
        raise SnapshotError('The Nova source must pin its BuiltZygisk submodule.')
    return url, match[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args(argv)
    try:
        url, revision = verify_source(args.source)
    except (SnapshotError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(url + ' ' + revision)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
