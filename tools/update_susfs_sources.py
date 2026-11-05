#!/usr/bin/env python3
"""Record SUSFS candidates and advance pins only across identical build trees.

Source/ABI changes are a pending adaptation, never an implicit compatibility
approval. No downloaded shell script or PR Python source is executed here.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote
import uuid

from check_official_updates import TARGETS, gitlab_head
from official_susfs_archive import API, MAX_JSON, request_bytes

ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = 'data/susfs-upstream-candidates.json'
PROFILE_PATH = 'build-compatibility.json'
PINS_PATH = 'config/manager-susfs-pins.json'
SOURCE = 'https://gitlab.com/simonpunk/susfs4ksu'
POLICY = 'same-build-tree-v1'
MANAGERS = ('SukiSU', 'KernelSU-Next', 'KernelSU', 'KowSU')
SHA = re.compile(r'[0-9a-f]{40}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
VERSION = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+\Z')
# All other root entries (including directories, scripts and hidden build
# metadata) are part of the identity. These are documentation blobs only.
DOCUMENTATION = {'README', 'README.md', 'README.rst', 'LICENSE', 'COPYING', 'AUTHORS'}


class SusfsUpdateError(ValueError):
    pass


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise SusfsUpdateError('SUSFS synchronization requires full lowercase commit hashes')
    return value


def _json(raw):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SusfsUpdateError('Duplicate SUSFS synchronization field')
            result[key] = value
        return result
    if not isinstance(raw, (str, bytes)) or len(raw) > 1024 * 1024:
        raise SusfsUpdateError('SUSFS synchronization metadata exceeds its bound')
    return json.loads(raw, object_pairs_hook=unique)


def normalize_receipt(document):
    if isinstance(document, (str, bytes)):
        document = _json(document)
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'source', 'policy', 'candidates', 'pins'}
            or type(document['schema_version']) is not int or document['schema_version'] != 1
            or document['source'] != SOURCE or document['policy'] != POLICY
            or not isinstance(document['candidates'], dict) or set(document['candidates']) != set(TARGETS)
            or not isinstance(document['pins'], dict) or set(document['pins']) != set(MANAGERS)):
        raise SusfsUpdateError('Invalid SUSFS synchronization receipt')
    for target, item in document['candidates'].items():
        if (not isinstance(item, dict) or set(item) != {'ref', 'version', 'build_tree_sha256'}
                or not isinstance(item['version'], str) or not VERSION.fullmatch(item['version'])
                or not isinstance(item['build_tree_sha256'], str) or not DIGEST.fullmatch(item['build_tree_sha256'])):
            raise SusfsUpdateError('Invalid official SUSFS candidate identity')
        _sha(item['ref'])
    for variant, targets in document['pins'].items():
        if not isinstance(targets, dict) or set(targets) != set(TARGETS):
            raise SusfsUpdateError('Missing SUSFS manager/target pin state')
        for target, item in targets.items():
            if (not isinstance(item, dict) or set(item) != {'selected_ref', 'candidate_ref', 'selected_build_tree_sha256', 'status', 'reason'}
                    or item['candidate_ref'] != document['candidates'][target]['ref']
                    or not isinstance(item['selected_build_tree_sha256'], str)
                    or not DIGEST.fullmatch(item['selected_build_tree_sha256'])):
                raise SusfsUpdateError('Invalid SUSFS effective pin identity')
            _sha(item['selected_ref'])
            if item['status'] == 'current':
                if (item['selected_ref'] != item['candidate_ref'] or item['reason'] != 'selected'
                        or item['selected_build_tree_sha256'] != document['candidates'][target]['build_tree_sha256']):
                    raise SusfsUpdateError('Current SUSFS source must equal the effective build pin')
            elif item['status'] == 'pending_adaptation':
                if item['selected_ref'] == item['candidate_ref'] or item['reason'] not in ('version-changed', 'build-tree-changed'):
                    raise SusfsUpdateError('Pending SUSFS adaptation must retain a different effective pin')
            else:
                raise SusfsUpdateError('Unknown SUSFS synchronization state')
    return copy.deepcopy(document)


def configured_pins(profile_document, manager_pins_document):
    try:
        suki = profile_document['sukisu_susfs']
        if not isinstance(suki['version'], str) or not VERSION.fullmatch(suki['version']):
            raise KeyError('version')
        if manager_pins_document.get('version') != suki['version']:
            raise SusfsUpdateError('SUSFS compatibility profiles disagree on their version')
        result = {'SukiSU': {target: _sha(suki['pins'][target]) for target in TARGETS}}
        for variant in MANAGERS[1:]:
            result[variant] = {target: _sha(manager_pins_document['managers'][variant]['pins'][target]) for target in TARGETS}
        return result, suki['version']
    except (KeyError, TypeError, AttributeError):
        raise SusfsUpdateError('The selected SUSFS compatibility pin inventory is incomplete') from None


def verify_receipt_pins(receipt, profile_document, manager_pins_document):
    receipt = normalize_receipt(receipt)
    actual, _ = configured_pins(profile_document, manager_pins_document)
    if any(receipt['pins'][variant][target]['selected_ref'] != actual[variant][target]
           for variant in MANAGERS for target in TARGETS):
        raise SusfsUpdateError('SUSFS receipt differs from the effective compatibility pins')
    return receipt


def observe_metadata(reference, *, fetch=None):
    """Read fixed official metadata; every non-documentation root tree is bound."""
    reference = _sha(reference)
    fetch = fetch or request_bytes
    entries, names = [], set()
    for page in range(1, 11):
        url = API + '/repository/tree?ref=' + reference + '&per_page=100&page=' + str(page)
        batch = _json(fetch(url, MAX_JSON))
        if not isinstance(batch, list) or len(batch) > 100:
            raise SusfsUpdateError('Invalid official SUSFS tree inventory')
        for item in batch:
            if (not isinstance(item, dict) or not isinstance(item.get('name'), str)
                    or item['name'] != item.get('path') or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', item['name'])
                    or item['name'] in ('.', '..') or item['name'] in names
                    or (item.get('type'), item.get('mode')) not in (('tree', '040000'), ('blob', '100644'), ('blob', '100755'))):
                raise SusfsUpdateError('Unexpected or duplicate official SUSFS root entry')
            names.add(item['name'])
            _sha(item.get('id'))
            if item['name'] not in DOCUMENTATION or item['type'] != 'blob' or item['mode'] != '100644':
                entries.append({key: item[key] for key in ('name', 'type', 'mode', 'id')})
        if len(batch) < 100:
            break
    else:
        raise SusfsUpdateError('Official SUSFS tree inventory exceeded its bound')
    if not any(item['name'] == 'kernel_patches' and item['type'] == 'tree' for item in entries):
        raise SusfsUpdateError('Official SUSFS source lacks the complete kernel patch tree')
    path = quote('kernel_patches/include/linux/susfs.h', safe='')
    header = fetch(API + '/repository/files/' + path + '/raw?ref=' + reference, MAX_JSON).decode('utf-8')
    versions = re.findall(r'^\s*#define\s+SUSFS_VERSION\s+"(v[0-9]+\.[0-9]+\.[0-9]+)"', header, re.M)
    if len(versions) != 1:
        raise SusfsUpdateError('Official SUSFS header must declare one version')
    digest = hashlib.sha256(json.dumps(sorted(entries, key=lambda row: row['name']),
                           sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'ref': reference, 'version': versions[0], 'build_tree_sha256': digest}


def plan(sources, profile_document, manager_pins_document, *, fetch=None):
    if not isinstance(sources, dict) or set(sources) != set(TARGETS):
        raise SusfsUpdateError('SUSFS update must contain all five official target commits')
    sources = {target: _sha(sources[target]) for target in TARGETS}
    pins, version = configured_pins(profile_document, manager_pins_document)
    references = sorted(set(sources.values()) | {value for rows in pins.values() for value in rows.values()})
    with ThreadPoolExecutor(max_workers=4) as pool:
        metadata = dict(zip(references, pool.map(lambda value: observe_metadata(value, fetch=fetch), references)))
    receipt = {'schema_version': 1, 'source': SOURCE, 'policy': POLICY,
               'candidates': {target: metadata[sources[target]] for target in TARGETS}, 'pins': {}}
    after_profile, after_managers = copy.deepcopy(profile_document), copy.deepcopy(manager_pins_document)
    for variant, targets in pins.items():
        receipt['pins'][variant] = {}
        for target, old_ref in targets.items():
            old, candidate = metadata[old_ref], metadata[sources[target]]
            if old['version'] != version:
                raise SusfsUpdateError('Effective SUSFS source version differs from its compatibility profile')
            same = old['build_tree_sha256'] == candidate['build_tree_sha256'] and candidate['version'] == version
            selected = candidate if same else old
            row = {'selected_ref': selected['ref'], 'candidate_ref': candidate['ref'],
                   'selected_build_tree_sha256': selected['build_tree_sha256'],
                   'status': 'current' if same else 'pending_adaptation',
                   'reason': 'selected' if same else ('version-changed' if candidate['version'] != version else 'build-tree-changed')}
            receipt['pins'][variant][target] = row
            target_pins = (after_profile['sukisu_susfs']['pins'] if variant == 'SukiSU'
                           else after_managers['managers'][variant]['pins'])
            target_pins[target] = selected['ref']
    return normalize_receipt(receipt), after_profile, after_managers


def validate_update(old_receipt, new_receipt, base_profile, base_manager_pins, *,
                    new_profile=None, new_manager_pins=None, expected_sources=None, fetch=None):
    if old_receipt is not None:
        # A separately reviewed pin adaptation may have landed since the last
        # observation. Recompute from the actual base pins, not the old receipt.
        normalize_receipt(old_receipt)
    new_receipt = normalize_receipt(new_receipt)
    sources = {target: item['ref'] for target, item in new_receipt['candidates'].items()}
    if expected_sources is not None:
        expected_sources = expected_sources.get('susfs', expected_sources)
        if sources != expected_sources:
            raise SusfsUpdateError('SUSFS receipt differs from the frozen official source snapshot')
    expected, profile, managers = plan(sources, base_profile, base_manager_pins, fetch=fetch)
    if new_receipt != expected or (new_profile is not None and new_profile != profile) or (new_manager_pins is not None and new_manager_pins != managers):
        raise SusfsUpdateError('SUSFS update changed unverified pins or compatibility settings')
    return {'receipt': expected, 'profile': profile, 'manager_pins': managers}


def _read(root, relative, *, missing=False):
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise SusfsUpdateError('SUSFS synchronization path must remain in the repository')
    if missing and not path.exists():
        return None
    return _json(path.read_bytes())


def write_documents(root, documents):
    # Stage all JSON first. If an atomic replacement fails, restore every
    # previous file before returning a failure; never commit a partial pin set.
    prepared, originals, replaced = {}, {}, []
    try:
        for relative, document in documents.items():
            path = root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise SusfsUpdateError('SUSFS write must remain in the repository')
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[path] = path.read_bytes() if path.exists() else None
            staged = path.with_name('.susfs-update-' + uuid.uuid4().hex + '.tmp')
            staged.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
            prepared[path] = staged
        for path, staged in prepared.items():
            os.replace(staged, path)
            replaced.append(path)
    except OSError:
        for path in reversed(replaced):
            if originals[path] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(originals[path])
        raise
    finally:
        for staged in prepared.values():
            staged.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply-candidates-json')
    parser.add_argument('--candidates-json', help='Frozen official check snapshot, or just its SUSFS target map')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        profile, managers = _read(root, PROFILE_PATH), _read(root, PINS_PATH)
        old = _read(root, RECEIPT_PATH, missing=True)
        if args.check:
            source = _json(args.candidates_json) if args.candidates_json is not None else {target: gitlab_head(target) for target in TARGETS}
            source = source.get('susfs', source)
            receipt, after_profile, after_managers = plan(source, profile, managers)
            if old is not None:
                normalize_receipt(old)
        else:
            if args.candidates_json is not None:
                raise SusfsUpdateError('Apply requires only the frozen SUSFS receipt')
            result = validate_update(old, _json(args.apply_candidates_json), profile, managers)
            receipt, after_profile, after_managers = result['receipt'], result['profile'], result['manager_pins']
        changed = receipt != old or profile != after_profile or managers != after_managers
        if not args.check and changed:
            documents = {RECEIPT_PATH: receipt}
            if profile != after_profile:
                documents[PROFILE_PATH] = after_profile
            if managers != after_managers:
                documents[PINS_PATH] = after_managers
            write_documents(root, documents)
        values = {'has_updates': str(changed).lower(), 'candidates_json': json.dumps(receipt, sort_keys=True, separators=(',', ':'))}
        if args.github_output:
            with args.github_output.open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(''.join(key + '=' + value + '\n' for key, value in values.items()))
        pending = sum(row['status'] == 'pending_adaptation' for targets in receipt['pins'].values() for row in targets.values())
        print('SUSFS 官方分支检查完成；' + str(pending) + ' 个管理器/内核组合的源码变化待适配，保留原构建 pin。')
        return 0
    except (OSError, ValueError, RuntimeError, UnicodeError) as error:
        print('SUSFS synchronization failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
