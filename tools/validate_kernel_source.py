#!/usr/bin/env python3
"""Reject a source checkout whose full version differs from the matrix target."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from update_kernel_catalog import CatalogError, makefile_version


def validate_source(root: Path, family: str, sublevel: str) -> str:
    if family not in {'5.10', '5.15', '6.1', '6.6', '6.12'}:
        raise ValueError('Unsupported matrix kernel family')
    if not isinstance(sublevel, str) or not re.fullmatch(r'(?:0|[1-9][0-9]*|X)', sublevel):
        raise ValueError('Invalid matrix kernel sublevel')
    path = Path(root) / 'common/Makefile'
    with path.open('rb') as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError('Kernel Makefile exceeds its size limit')
    actual = makefile_version(raw.decode('utf-8'), family)
    if sublevel != 'X' and actual != f'{family}.{sublevel}':
        raise ValueError(f'Source checkout is {actual}, matrix requested {family}.{sublevel}')
    return actual


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', required=True, type=Path)
    parser.add_argument('--family', required=True)
    parser.add_argument('--sublevel', required=True)
    args = parser.parse_args(argv)
    try:
        actual = validate_source(args.kernel_root, args.family, args.sublevel)
    except (OSError, UnicodeError, ValueError, CatalogError) as error:
        parser.exit(1, f'Kernel source version check failed: {error}\n')
    print(json.dumps({'actual_kernel_version': actual, 'matrix_sublevel': args.sublevel}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
