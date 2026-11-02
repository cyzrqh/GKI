"""Validate and stage NoMount from the existing, pinned eight-KMI jobs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from manager_full_native import inspect_extra
from manager_lkm_jobs import KMIS
from prepare_manager_lkm import locked_ddk, public_identity, read_json, selected_source


def record(source: Path, directory: Path, identity_file: Path, reference: str, kmi: str, image: str) -> dict:
    locked_ddk(kmi, image)
    identity = public_identity(identity_file)
    provenance = selected_source(source, reference)
    name = kmi + '_nomount.ko'
    component = inspect_extra(directory / name, 'aarch64', 'nomount', kmi, reference, identity)
    receipt = {'schema_version': 1, 'kmi': kmi, 'ddk_image': image,
               **identity, **provenance, 'component': {'file': name, **component}}
    (directory / 'nomount.json').write_text(json.dumps(receipt, sort_keys=True) + '\n', encoding='utf-8')
    return receipt


def checked(source: Path, identity_file: Path, reference: str, receipt: dict, module: Path, kmi: str) -> None:
    identity = public_identity(identity_file)
    provenance = selected_source(source, reference)
    if (not isinstance(receipt, dict) or receipt.get('schema_version') != 1 or receipt.get('kmi') != kmi
            or any(receipt.get(key) != value for key, value in {**identity, **provenance}.items())):
        raise ValueError('NoMount cache does not match the prepared source and certificate')
    locked_ddk(kmi, receipt.get('ddk_image'))
    component = inspect_extra(module, 'aarch64', 'nomount', kmi, reference, identity)
    if receipt.get('component') != {'file': kmi + '_nomount.ko', **component}:
        raise ValueError('NoMount module changed after its pinned build')


def stage(source: Path, artifacts: Path, identity_file: Path, reference: str) -> None:
    pending, receipts = [], []
    for kmi in KMIS:
        directory = artifacts / kmi / 'nomount'
        module = directory / (kmi + '_nomount.ko')
        receipt = read_json(directory / 'nomount.json')
        checked(source, identity_file, reference, receipt, module, kmi)
        target = source / 'userspace/ksud/bin/aarch64' / module.name
        if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (target, *target.parents)):
            raise ValueError('NoMount destination must not traverse links')
        pending.append((target, module.read_bytes()))
        receipts.append(receipt)
    for target, content in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (source / '.gki-fina-nomount.json').write_text(json.dumps({'components': receipts}, sort_keys=True) + '\n', encoding='utf-8')


def verify_staged(source: Path, identity_file: Path, reference: str) -> bool:
    manifest = source / '.gki-fina-nomount.json'
    if not manifest.exists():
        return False
    receipts = read_json(manifest).get('components')
    if not isinstance(receipts, list) or len(receipts) != len(KMIS):
        raise ValueError('Incomplete staged NoMount receipt')
    for kmi, receipt in zip(KMIS, receipts):
        checked(source, identity_file, reference, receipt,
                source / 'userspace/ksud/bin/aarch64' / (kmi + '_nomount.ko'), kmi)
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('record', 'stage'))
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--identity', required=True, type=Path)
    parser.add_argument('--expected-ref', required=True)
    parser.add_argument('--kmi', choices=KMIS)
    parser.add_argument('--ddk-image')
    args = parser.parse_args()
    if args.operation == 'record':
        record(args.source, args.directory, args.identity, args.expected_ref, args.kmi, args.ddk_image)
    else:
        stage(args.source, args.directory, args.identity, args.expected_ref)
    print('Verified native NoMount ' + args.operation)
