#!/usr/bin/env python3
"""Derive the default Android build suffix from the fixed kernel source.

The full compiler/configuration cache guards remain authoritative. A random
cosmetic CONFIG_LOCALVERSION would otherwise invalidate those guards on every
build of identical sources. Explicit user-supplied versions are unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import re


def build_id(reference: str) -> str:
    if not isinstance(reference, str) or not re.fullmatch(r'[0-9a-f]{40}', reference):
        raise ValueError('The default build identity requires the fixed full kernel Git commit')
    number = int.from_bytes(hashlib.sha256(reference.encode('ascii')).digest()[:8], 'big')
    return 'ab' + str(10_000_000 + number % 90_000_000)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-ref', required=True)
    args = parser.parse_args()
    try:
        value = build_id(args.source_ref)
    except ValueError as error:
        parser.error(str(error))
    print(value)


if __name__ == '__main__':
    main()
