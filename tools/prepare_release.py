#!/usr/bin/env python3
"""Prepare direct Release assets and notes from the selected workflow artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

from release_notes import asset_names, load_metadata, render_notes


def prepare(source: Path, output: Path) -> dict:
    # download-artifact puts a single matching artifact directly in `path`,
    # even with merge-multiple=false. Multiple artifacts get named folders.
    managers = sorted([*source.glob('manager.json'), *source.glob('*/manager.json')])
    kernels = sorted([*source.glob('kernel.json'), *source.glob('*/kernel.json')])
    if len(managers) > 1 or not (managers or kernels):
        raise ValueError('Expected at most one manager and at least one release artifact')
    manager = load_metadata(managers[0]) if managers else None
    kernel_documents = [load_metadata(path) for path in kernels]
    entries = []
    if manager:
        entries.extend((managers[0].parent, name) for name in asset_names(manager, manager=True))
    for path, document in zip(kernels, kernel_documents):
        entries.extend((path.parent, name) for name in asset_names(document))
    names = [name for _, name in entries]
    if not names or len(names) != len(set(names)):
        raise ValueError('Missing or duplicate public release filenames')
    # Validate the entire input before creating a partially populated output.
    for directory, name in entries:
        path = directory / name
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            raise ValueError(f'Missing nonempty release asset: {name}')
        if (output / 'assets' / name).exists():
            raise ValueError('Release output already contains an asset')
    body = render_notes(manager, kernel_documents)
    assets = output / 'assets'
    assets.mkdir(parents=True, exist_ok=True)
    checksums = []
    for directory, name in entries:
        destination = assets / name
        shutil.copyfile(directory / name, destination)
        checksums.append(hashlib.sha256(destination.read_bytes()).hexdigest() + '  ' + name)
    (assets / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', encoding='utf-8')
    (output / 'release.md').write_text(body, encoding='utf-8')
    title = 'GKI · SukiSU ' + str(manager.get('ksu_version_name') or manager.get('ksu_version_code')) if manager else 'GKI Kernel Release'
    (output / 'title.txt').write_text(title, encoding='utf-8')
    return {'assets': len(entries), 'kernels': len(kernels), 'manager': bool(manager)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.input, args.output)
    except (OSError, ValueError) as error:
        print(f'Release preparation failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
