#!/usr/bin/env python3
"""Fetch official SUSFS once, then restore every matrix consumer offline."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time

from manager_variants import MANAGERS
from pin_build_dependencies import custom_configuration, selected_pin
from snapshot_ksu_source import (BUNDLE_NAME, ROOT, GitCommandError, SnapshotError, check_official_source,
                                expected_commit, file_digest, git, source_identity,
                                temporary_directory)


OFFICIAL = 'https://gitlab.com/simonpunk/susfs4ksu.git'
TARGET = re.compile(r'android[0-9]{1,2}-[0-9]{1,2}\.[0-9]{1,2}\Z')
FULL_SHA = re.compile(r'[0-9a-f]{40}\Z')
DATE = re.compile(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}\Z')
FIELDS = {'ref', 'tree', 'date', 'version', 'branch'}
NAMESPACE = 'refs/gki-susfs/'
MAX_TARGETS = 20


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise SnapshotError('Duplicate SUSFS source metadata field')
        result[key] = value
    return result


def read_json(value: str):
    if not isinstance(value, str) or len(value.encode('utf-8')) > 1024 * 1024:
        raise SnapshotError('SUSFS source metadata exceeds its size limit')
    try:
        return json.loads(value, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as error:
        raise SnapshotError('SUSFS source metadata is not valid JSON') from error


def validate_sources(document: dict) -> dict:
    if not isinstance(document, dict) or not 1 <= len(document) <= MAX_TARGETS:
        raise SnapshotError('Shared SUSFS sources must contain a bounded target map')
    result = {}
    for target, item in sorted(document.items()):
        if (not isinstance(target, str) or not TARGET.fullmatch(target)
                or not isinstance(item, dict) or set(item) not in (FIELDS, FIELDS | {'transport_ref'})
                or any(not isinstance(item[field], str) for field in item)
                or not FULL_SHA.fullmatch(item['ref']) or not FULL_SHA.fullmatch(item['tree'])
                or item['branch'] != 'gki-' + target
                or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', item['version'])
                or not DATE.fullmatch(item['date'])):
            raise SnapshotError('Invalid shared official SUSFS target identity')
        if 'transport_ref' in item and (not FULL_SHA.fullmatch(item['transport_ref'])
                                        or item['transport_ref'] == item['ref']):
            raise SnapshotError('Invalid shared SUSFS archive transport identity')
        try:
            datetime.strptime(item['date'], '%Y-%m-%d %H:%M:%S %z')
        except ValueError as error:
            raise SnapshotError('Invalid shared SUSFS commit date') from error
        result[target] = dict(item)
    return result


def matrix_targets(matrix: dict) -> list[str]:
    if not isinstance(matrix, dict) or set(matrix) != {'include'} or not isinstance(matrix['include'], list):
        raise SnapshotError('Expected the prepared kernel include matrix')
    if not 1 <= len(matrix['include']) <= 1000:
        raise SnapshotError('Prepared kernel matrix is empty or exceeds its limit')
    targets = set()
    for row in matrix['include']:
        if not isinstance(row, dict):
            raise SnapshotError('Invalid prepared kernel matrix row')
        android, series = row.get('android_version'), row.get('kernel_version')
        if not isinstance(android, str) or not isinstance(series, str):
            raise SnapshotError('Missing Android/kernel target in prepared matrix')
        target = android + '-' + series
        if not TARGET.fullmatch(target):
            raise SnapshotError('Invalid Android/kernel target in prepared matrix')
        targets.add(target)
    if len(targets) > MAX_TARGETS:
        raise SnapshotError('Too many shared SUSFS kernel targets')
    return sorted(targets)


def metadata(source: Path, target: str, ref: str) -> dict:
    if not TARGET.fullmatch(target):
        raise SnapshotError('Invalid shared SUSFS target')
    reference = git(source, 'rev-parse', '--verify', ref + '^{commit}').lower()
    tree = git(source, 'rev-parse', '--verify', reference + '^{tree}').lower()
    date = git(source, 'show', '-s', '--format=%cd', '--date=format:%Y-%m-%d %H:%M:%S %z', reference)
    header = git(source, 'show', reference + ':kernel_patches/include/linux/susfs.h')
    versions = re.findall(r'^\s*#define\s+SUSFS_VERSION\s+"(v[0-9]+\.[0-9]+\.[0-9]+)"', header, re.M)
    if len(versions) != 1:
        raise SnapshotError('Official SUSFS source must declare exactly one version')
    patch = 'kernel_patches/50_add_susfs_in_gki-' + target + '.patch'
    if git(source, 'cat-file', '-t', reference + ':' + patch) != 'blob':
        raise SnapshotError('Official SUSFS source is missing its target kernel patch')
    item = {'ref': reference, 'tree': tree, 'date': date, 'version': versions[0], 'branch': 'gki-' + target}
    return validate_sources({target: item})[target]


def verify_local_sources(source: Path, sources: dict) -> dict:
    sources = validate_sources(sources)
    for target, expected in sources.items():
        if 'transport_ref' in expected:
            from official_susfs_archive import carrier_metadata
            transport = git(source, 'rev-parse', '--verify', NAMESPACE + target + '^{commit}').lower()
            actual = carrier_metadata(source, target, transport, expected['ref'])
        else:
            actual = metadata(source, target, NAMESPACE + target)
        if actual != expected:
            raise SnapshotError('Local SUSFS objects differ from the prepared target identity')
    return sources


def fetch(source: Path, refspecs: list[str], *, attempts: int = 4) -> None:
    # One Git negotiation serves every selected kernel family. Never make a
    # matrix of ninety-seven independent requests against the GitLab service.
    if type(attempts) is not int or not 1 <= attempts <= 4:
        raise SnapshotError('Invalid official SUSFS Git retry limit')
    for attempt in range(attempts):
        try:
            git(source, '-c', 'http.extraHeader=', '-c', 'http.https://gitlab.com/.extraHeader=',
                '-c', 'credential.helper=', '-c', 'http.followRedirects=false',
                'fetch', '--quiet', '--no-tags', '--no-recurse-submodules',
                OFFICIAL, *refspecs, timeout=180)
            return
        except SnapshotError as error:
            # GitCommandError contains only fixed classifications and numeric
            # status. Never echo another exception's potentially raw message.
            reason = error.diagnostic if isinstance(error, GitCommandError) else 'Git request failed (unclassified)'
            if attempt + 1 == attempts:
                count = 'four spaced download attempts' if attempts == 4 else f'{attempts} download attempt(s)'
                print('Official SUSFS Git download failed: ' + reason, flush=True)
                raise SnapshotError('Official SUSFS preparation failed after ' + count + ': ' + reason) from None
            delay = (15, 30, 60)[attempt]
            print(f'Official SUSFS download unavailable ({reason}); retrying once in {delay}s', flush=True)
            time.sleep(delay)


def configured_custom_ref(root: Path, target: str) -> str | None:
    if not custom_configuration(root):
        return None
    text = (root / 'config/config').read_text(encoding='utf-8')
    matches = re.findall(r'^[ \t]*' + re.escape('gki-' + target) + r'[ \t]*=[ \t]*([^#\r\n]*)', text, re.M)
    if len(matches) > 1:
        raise SnapshotError('Duplicate custom SUSFS target ref')
    value = matches[0].strip() if matches else ''
    if not value:
        return None
    if not re.fullmatch(r'[0-9a-fA-F]{7,40}', value):
        raise SnapshotError('Custom SUSFS ref must be a hexadecimal commit prefix or full hash')
    return value.lower()


def prepare_sources(source: Path, matrix: dict, reference: str, *, root: Path = ROOT,
                    variant: str = 'SukiSU', tier: str = 'Latest(最新)') -> dict:
    source = source.resolve(strict=True)
    root = root.resolve(strict=True)
    reference = expected_commit(reference)
    check_official_source(source, variant)
    before = source_identity(source, reference)
    targets = matrix_targets(matrix)
    custom, requested_refs, refspecs = {}, {}, []
    for target in targets:
        pin = (selected_pin(root, variant, reference, tier, target, manager_source=source)
               if variant in ('KernelSU-Next', 'KernelSU') else
               selected_pin(root, variant, reference, tier, target))
        requested = configured_custom_ref(root, target)
        if requested is not None:
            custom[target] = requested
        # Full pins are fetched directly in the same Git negotiation. Short
        # custom hashes are resolved only in this official branch's history.
        value = (requested if requested and len(requested) == 40 else None) or (pin[0] if pin else None)
        requested_refs[target] = value or 'gki-' + target
        refspecs.append('+' + (value or 'refs/heads/gki-' + target) + ':' + NAMESPACE + target)
    try:
        fetch(source, refspecs, attempts=1)
    except SnapshotError:
        if any(len(value) != 40 for value in custom.values()):
            raise SnapshotError('Official SUSFS Git is unavailable; archive recovery requires full custom commit hashes') from None
        # The official REST/archive service remains usable during Git service
        # overload. Restore its exact original commit bytes, never a mirror or
        # a fabricated replacement commit. Workers still use one offline bundle.
        from official_susfs_archive import import_archive
        print('Official SUSFS Git unavailable; verifying fixed commits via the official archive API', flush=True)
        sources = {target: import_archive(source, target, requested_refs[target]) for target in targets}
    else:
        for target, value in custom.items():
            if len(value) == 40:
                continue
            candidates = [ref for ref in git(source, 'rev-list', NAMESPACE + target).splitlines() if ref.startswith(value)]
            if len(candidates) != 1:
                raise SnapshotError('Custom SUSFS prefix is missing or ambiguous in its official branch history')
            git(source, 'update-ref', NAMESPACE + target, candidates[0])
        sources = {target: metadata(source, target, NAMESPACE + target) for target in targets}
    if source_identity(source, reference) != before:
        raise SnapshotError('Preparing SUSFS changed the selected manager source identity')
    check_official_source(source, variant)
    return verify_local_sources(source, sources)


def recover_exact_sources(source: Path, sources: dict) -> dict:
    sources = validate_sources(sources)
    native = ['+' + item['ref'] + ':' + NAMESPACE + target for target, item in sources.items()
              if 'transport_ref' not in item]
    if native:
        fetch(source, native)
    for target, item in sources.items():
        if 'transport_ref' in item:
            from official_susfs_archive import import_archive
            if import_archive(source, target, item['ref']) != item:
                raise SnapshotError('Official archive recovery differs from the fixed prepare identity')
    return verify_local_sources(source, sources)


def verify_bundle_sources(bundle: Path, sources: dict) -> dict:
    sources = validate_sources(sources)
    heads = {}
    for line in git(bundle.parent, 'bundle', 'list-heads', str(bundle)).splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[1] in heads:
            raise SnapshotError('Invalid or duplicate source bundle ref')
        heads[fields[1]] = fields[0]
    for target, item in sources.items():
        if heads.get(NAMESPACE + target) != item.get('transport_ref', item['ref']):
            raise SnapshotError('Shared SUSFS bundle ref differs from its prepared source')
    return sources


def restore_source(directory: Path, output: Path, reference: str, target: str, expected: dict,
                   *, variant: str = 'SukiSU') -> dict:
    from checkout_ksu_snapshot import read_manifest
    expected = validate_sources(expected)
    if target not in expected:
        raise SnapshotError('SUSFS target was not selected by the prepare job')
    directory = directory.resolve(strict=True)
    manifest = read_manifest(directory, expected_commit(reference), variant=variant)
    if manifest.get('susfs_sources') != expected:
        raise SnapshotError('SUSFS source manifest differs from the successful prepare job')
    bundle = directory / BUNDLE_NAME
    if (bundle.is_symlink() or not bundle.is_file() or bundle.stat().st_size != manifest['bundle_size']
            or file_digest(bundle) != manifest['bundle_sha256']):
        raise SnapshotError('Shared SUSFS bundle size or digest does not match')
    verify_bundle_sources(bundle, expected)
    output = output.absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise SnapshotError('SUSFS output must be a new directory below an existing parent')
    with temporary_directory(output.parent, '.gki-susfs-') as temporary:
        prepared = temporary / 'source'
        prepared.mkdir()
        git(prepared, 'init', '--quiet')
        git(prepared, 'bundle', 'verify', str(bundle), timeout=300)
        # The bundle is self-contained: an empty object store must restore the
        # exact custom ref without asking GitLab or using the KSU checkout.
        git(prepared, 'fetch', '--quiet', '--no-tags', str(bundle), NAMESPACE + target)
        if 'transport_ref' in expected[target]:
            from official_susfs_archive import hydrate_carrier
            hydrate_carrier(prepared, expected[target]['transport_ref'], expected[target])
        git(prepared, 'checkout', '--quiet', '--detach', expected[target]['ref'])
        actual = metadata(prepared, target, 'HEAD')
        if 'transport_ref' in expected[target]:
            actual['transport_ref'] = expected[target]['transport_ref']
        if actual != expected[target]:
            raise SnapshotError('Restored SUSFS tree/date/version differ from the prepared identity')
        git(prepared, 'fsck', '--connectivity-only', '--no-dangling', timeout=300)
        git(prepared, 'remote', 'add', 'origin', OFFICIAL)
        if output.exists() or output.is_symlink():
            raise SnapshotError('SUSFS output appeared during restoration')
        prepared.rename(output)
    return expected[target]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--source', required=True, type=Path)
    prepare.add_argument('--matrix-json', required=True)
    prepare.add_argument('--expected-ref', required=True)
    prepare.add_argument('--variant', choices=MANAGERS, default=os.environ.get('KSU_VARIANT', 'SukiSU'))
    prepare.add_argument('--tier', required=True)
    prepare.add_argument('--root', type=Path, default=ROOT)
    prepare.add_argument('--output', required=True, type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('--input', required=True, type=Path)
    restore.add_argument('--output', required=True, type=Path)
    restore.add_argument('--expected-ref', required=True)
    restore.add_argument('--target', required=True)
    restore.add_argument('--expected-sources', required=True)
    restore.add_argument('--variant', choices=MANAGERS, default=os.environ.get('KSU_VARIANT', 'SukiSU'))
    args = parser.parse_args(argv)
    try:
        if args.operation == 'prepare':
            sources = prepare_sources(args.source, read_json(args.matrix_json), args.expected_ref,
                                      root=args.root, variant=args.variant, tier=args.tier)
            if args.output.exists() or args.output.is_symlink() or not args.output.parent.is_dir():
                raise SnapshotError('SUSFS metadata output must be a new regular file')
            args.output.write_text(json.dumps(sources, sort_keys=True) + '\n', encoding='utf-8', newline='\n')
            if os.environ.get('GITHUB_OUTPUT'):
                with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as output:
                    output.write('susfs_sources=' + json.dumps(sources, separators=(',', ':'), sort_keys=True) + '\n')
            print('Prepared official shared SUSFS targets: ' + ', '.join(sources))
        else:
            item = restore_source(args.input, args.output, args.expected_ref, args.target,
                                  read_json(args.expected_sources), variant=args.variant)
            print('Restored official SUSFS offline: ' + args.target + ' ' + item['ref'])
    except (OSError, ValueError, UnicodeError) as error:
        print('::error::Shared SUSFS source preparation/restoration failed: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
