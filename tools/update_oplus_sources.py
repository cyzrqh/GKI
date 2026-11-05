#!/usr/bin/env python3
"""Check immutable OPlus source/recipe updates without replacing local adapters."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

import yaml

import oplus_catalog as catalog
import oplus_cctv as schemes

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = 'config/oplus-cctv-profiles.json'
CANDIDATE_PATH = 'data/oplus-upstream-candidates.json'
FILES = (PROFILE_PATH, CANDIDATE_PATH)
MAX_BYTES = 4 * 1024 * 1024
RECIPE_PATH = re.compile(r'\.github/workflows/fastbuild_(6\.(?:1|6|12)\.(?:0|[1-9][0-9]*))(_mtk|_gki)?\.yml\Z')


class UpdateError(ValueError):
    pass


def recipe_path(profile):
    return 'third_party/cctv-oplus/recipes/' + Path(profile['recipe_workflow']).name


def allowed_paths(profiles_document):
    rows = profile_rows(profiles_document)
    return frozenset((*FILES, *(recipe_path(row) for row in rows)))


def profile_rows(document):
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'profiles'}
            or type(document['schema_version']) is not int or document['schema_version'] != 1
            or not isinstance(document['profiles'], list) or not 1 <= len(document['profiles']) <= 128):
        raise UpdateError('Invalid OPlus profile document')
    identities, families = set(), set()
    for row in document['profiles']:
        if (not isinstance(row, dict) or set(row) != schemes.FIELDS
                or any(not isinstance(value, str) for value in row.values())
                or row['cctv_scheme'] != schemes.SCHEME
                or row['kernel_repo'] not in schemes.SOURCE_REPOSITORIES
                or row['recipe_repository'] not in catalog.RECIPE_REPOSITORIES
                or not catalog.KERNEL.fullmatch(row['kernel'])
                or row['kernel_version'] != '.'.join(row['kernel'].split('.')[:2])
                or row['sub_level'] != row['kernel'].split('.')[-1]
                or row['android_version'] != catalog.ANDROID.get(row['kernel_version'])
                or row['recipe_repository'] != catalog.RECIPE_FOR_SERIES[row['kernel_version']]
                or row['id'] != row['platform'] + '-' + row['kernel']
                or not catalog.SHA.fullmatch(row['kernel_ref'])
                or not catalog.SHA.fullmatch(row['recipe_ref'])
                or not catalog.SHA256.fullmatch(row['recipe_sha256'])):
            raise UpdateError('Unsupported OPlus profile identity')
        family = schemes.family(row['platform'])
        match = RECIPE_PATH.fullmatch(row['recipe_workflow'])
        if (not match or match[1] != row['kernel']
                or family == '通用 GKI' and match[2] != '_gki'
                or family != '通用 GKI' and not row['kernel_repo'].endswith('_' + row['platform'])):
            raise UpdateError('OPlus recipe platform/version differs')
        branch(row['kernel_branch'])
        date(row['source_committed_at'])
        key = family, row['kernel']
        if row['id'] in identities or key in families:
            raise UpdateError('Duplicate OPlus platform-family/kernel target')
        identities.add(row['id']); families.add(key)
    return document['profiles']


def normalize_manifest(document):
    profile_rows(document)
    return document


def branch(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,255}', value)
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise UpdateError('Invalid OPlus source branch')
    return value


def date(value):
    try:
        if not isinstance(value, str) or len(value) > 64 or datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise UpdateError('Invalid OPlus commit timestamp') from None
    return value


def normalize_receipt(document):
    if document is None:
        return None
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'recipe_heads', 'profiles', 'new_recipes'}
            or type(document['schema_version']) is not int or document['schema_version'] != 1
            or not isinstance(document['recipe_heads'], dict)
            or set(document['recipe_heads']) != set(catalog.RECIPE_REPOSITORIES)
            or not isinstance(document['profiles'], list) or not 1 <= len(document['profiles']) <= 128
            or not isinstance(document['new_recipes'], list) or len(document['new_recipes']) > 128):
        raise UpdateError('Invalid OPlus source receipt')
    for repository, head in document['recipe_heads'].items():
        if (not isinstance(head, dict) or set(head) != {'branch', 'ref'}
                or not isinstance(head['ref'], str) or not catalog.SHA.fullmatch(head['ref'])):
            raise UpdateError('Invalid OPlus recipe head')
        branch(head['branch'])
    ids = set()
    for row in document['profiles']:
        if (not isinstance(row, dict) or set(row) != {'id', 'source_ref', 'source_date', 'source_version', 'recipe_ref', 'recipe_sha256', 'recipe_status'}
                or any(not isinstance(v, str) for v in row.values())
                or not re.fullmatch(r'(?:sm[0-9]{4}|mt[0-9]{4}|gki)-6\.(?:1|6|12)\.[0-9]+', row['id'])
                or row['id'] in ids or not catalog.SHA.fullmatch(row['source_ref'])
                or not catalog.SHA.fullmatch(row['recipe_ref'])
                or not catalog.SHA256.fullmatch(row['recipe_sha256'])
                or not catalog.KERNEL.fullmatch(row['source_version'])
                or row['recipe_status'] not in ('unchanged', 'execution-equivalent', 'pending-adaptation')):
            raise UpdateError('Invalid OPlus profile observation')
        date(row['source_date']); ids.add(row['id'])
    keys = set()
    for row in document['new_recipes']:
        if (not isinstance(row, dict) or set(row) != {'repository', 'ref', 'path', 'kernel', 'family'}
                or any(not isinstance(v, str) for v in row.values())
                or row['repository'] not in catalog.RECIPE_REPOSITORIES
                or row['ref'] != document['recipe_heads'][row['repository']]['ref']):
            raise UpdateError('Invalid new OPlus recipe')
        match = RECIPE_PATH.fullmatch(row['path'])
        expected = '天玑' if match and match[2] == '_mtk' else '通用 GKI' if match and match[2] == '_gki' else '骁龙'
        if (not match or row['kernel'] != match[1] or row['family'] != expected
                or row['repository'] != catalog.RECIPE_FOR_SERIES['.'.join(row['kernel'].split('.')[:2])]
                or (row['family'], row['kernel']) in keys):
            raise UpdateError('Duplicate or inconsistent new OPlus target')
        keys.add((row['family'], row['kernel']))
    return document


def validate_snapshot(document, profiles_document=None):
    value = normalize_receipt(document)
    if value is None:
        raise UpdateError('Missing OPlus source snapshot')
    if profiles_document is None:
        profiles_document = catalog.strict_json((ROOT / PROFILE_PATH).read_bytes())
    profiles = profile_rows(profiles_document)
    known = {row['id']: row for row in profiles}
    if {row['id'] for row in value['profiles']} != set(known):
        raise UpdateError('OPlus snapshot must cover every current target exactly once')
    for row in value['profiles']:
        original = known[row['id']]
        if row['recipe_ref'] != value['recipe_heads'][original['recipe_repository']]['ref']:
            raise UpdateError('OPlus recipe observations must share one frozen repository head')
    existing = {(row['recipe_repository'], row['recipe_workflow']) for row in profiles}
    if any((row['repository'], row['path']) in existing for row in value['new_recipes']):
        raise UpdateError('Existing OPlus target advertised as newly discovered')
    return value


class Reader:
    """Bounded public reads; never follow a credential-bearing redirect."""
    def __init__(self, token=None, opener=None):
        self.token = token if token is not None else os.environ.get('GH_TOKEN', os.environ.get('GITHUB_TOKEN', ''))
        if any(ord(c) < 33 or ord(c) == 127 for c in self.token):
            raise UpdateError('Invalid OPlus read token')
        self.opener = opener or build_opener(catalog.NoRedirect())
        self.cache = {}

    def get(self, repository, suffix=''):
        if repository not in schemes.SOURCE_REPOSITORIES | catalog.RECIPE_REPOSITORIES:
            raise UpdateError('Unreviewed OPlus source destination')
        if not (suffix == '' or re.fullmatch(r'branches/[A-Za-z0-9_.%/-]+|git/commits/[0-9a-f]{40}', suffix)
                or re.fullmatch(r'contents/(?:Makefile|\.github/workflows(?:/fastbuild_6\.(?:1|6|12)\.[0-9]+(?:_mtk|_gki)?\.yml)?)\?ref=[0-9a-f]{40}', suffix)):
            raise UpdateError('Unreviewed OPlus read operation')
        key = repository, suffix
        if key in self.cache: return self.cache[key]
        url = 'https://api.github.com/repos/' + repository + ('/' + suffix if suffix else '')
        request = Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'gki-oplus-source-sync'})
        if self.token: request.add_header('Authorization', 'Bearer ' + self.token)
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=30) as response:
                    if response.status != 200 or response.geturl() != url:
                        raise UpdateError('OPlus source response changed destination')
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES: raise UpdateError('Oversized OPlus response')
                document = catalog.strict_json(raw)
                self.cache[key] = document
                return document
            except HTTPError as error:
                status = error.code; error.close()
                if status not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                    raise UpdateError(f'OPlus public source lookup failed (HTTP {status})') from None
            except (OSError, URLError, TimeoutError):
                if attempt == 2: raise UpdateError('OPlus public source transport failed') from None
            time.sleep(attempt + 1)


def content(document, path):
    if (not isinstance(document, dict) or document.get('type') != 'file' or document.get('path') != path
            or document.get('encoding') != 'base64' or not isinstance(document.get('content'), str)
            or type(document.get('size')) is not int or not 0 < document['size'] <= 2 * 1024 * 1024
            or not isinstance(document.get('sha'), str) or not catalog.SHA.fullmatch(document['sha'])):
        raise UpdateError('Invalid immutable OPlus source file')
    try: data = base64.b64decode(''.join(document['content'].split()), validate=True)
    except ValueError: raise UpdateError('Invalid OPlus file encoding') from None
    digest = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if len(data) != document['size'] or digest != document['sha']:
        raise UpdateError('OPlus source bytes differ from Git blob identity')
    return data


def commit_info(document, reference=None):
    if (not isinstance(document, dict) or not isinstance(document.get('sha'), str)
            or not catalog.SHA.fullmatch(document['sha']) or reference is not None and document['sha'] != reference):
        raise UpdateError('Invalid OPlus immutable commit')
    metadata = document.get('commit', document)
    committer = metadata.get('committer') if isinstance(metadata, dict) else None
    if not isinstance(committer, dict): raise UpdateError('Missing OPlus committer')
    return document['sha'], date(committer.get('date'))


def execution_identity(raw):
    try:
        document = yaml.safe_load(raw.decode('utf-8'))
        return document['env'], document['jobs']['build']['steps']
    except (ValueError, UnicodeError, KeyError, TypeError, yaml.YAMLError):
        raise UpdateError('Invalid OPlus recipe structure') from None


def recipe_status(before, after):
    if before == after: return 'unchanged'
    # The local adapter executes only these two reviewed sections. Changes to
    # menus/comments can be recorded, but newly changed commands stay pending.
    return 'execution-equivalent' if execution_identity(before) == execution_identity(after) else 'pending-adaptation'


def fetch_snapshot(root=ROOT, reader=None):
    root = Path(root)
    rows = schemes.load_profiles(root)
    reader = reader or Reader()
    heads, discovered = {}, []
    existing = {(row['recipe_repository'], row['recipe_workflow']) for row in rows}
    existing_keys = {(schemes.family(row['platform']), row['kernel']) for row in rows}
    for repository in sorted(catalog.RECIPE_REPOSITORIES):
        identity = reader.get(repository)
        if not isinstance(identity, dict) or identity.get('full_name') != repository:
            raise UpdateError('OPlus recipe repository identity changed')
        default = branch(identity.get('default_branch'))
        response = reader.get(repository, 'branches/' + quote(default, safe=''))
        if not isinstance(response, dict) or response.get('name') != default:
            raise UpdateError('OPlus recipe default branch changed')
        head, _ = commit_info(response.get('commit'))
        heads[repository] = {'branch': default, 'ref': head}
        listing = reader.get(repository, 'contents/.github/workflows?ref=' + head)
        if not isinstance(listing, list) or len(listing) > 512:
            raise UpdateError('Invalid OPlus workflow listing')
        for entry in listing:
            path = entry.get('path') if isinstance(entry, dict) else None
            match = RECIPE_PATH.fullmatch(path) if isinstance(path, str) else None
            if not match or entry.get('type') != 'file' or (repository, path) in existing: continue
            family = '天玑' if match[2] == '_mtk' else '通用 GKI' if match[2] == '_gki' else '骁龙'
            key = family, match[1]
            if key in existing_keys: continue
            if catalog.RECIPE_FOR_SERIES['.'.join(match[1].split('.')[:2])] != repository: continue
            discovered.append({'repository': repository, 'ref': head, 'path': path, 'kernel': match[1], 'family': family})
            existing_keys.add(key)

    def observe(row):
        response = reader.get(row['kernel_repo'], 'branches/' + quote(row['kernel_branch'], safe=''))
        if not isinstance(response, dict) or response.get('name') != row['kernel_branch']:
            raise UpdateError('OPlus source branch identity changed')
        source_ref, source_date = commit_info(response.get('commit'))
        version = catalog.kernel_version(reader.get(row['kernel_repo'], 'contents/Makefile?ref=' + source_ref))
        recipe_ref = heads[row['recipe_repository']]['ref']
        raw = content(reader.get(row['recipe_repository'], 'contents/' + row['recipe_workflow'] + '?ref=' + recipe_ref), row['recipe_workflow'])
        previous = (root / recipe_path(row)).read_bytes()
        return {'id': row['id'], 'source_ref': source_ref, 'source_date': source_date, 'source_version': version,
                'recipe_ref': recipe_ref, 'recipe_sha256': hashlib.sha256(raw).hexdigest(), 'recipe_status': recipe_status(previous, raw)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        observed = list(pool.map(observe, rows))
    return validate_snapshot({'schema_version': 1, 'recipe_heads': heads, 'profiles': observed,
                              'new_recipes': sorted(discovered, key=lambda x: (tuple(map(int, x['kernel'].split('.'))), x['family']))},
                             {'schema_version': 1, 'profiles': rows})


def planned_profiles(before, snapshot):
    validate_snapshot(snapshot, before)
    observed = {row['id']: row for row in snapshot['profiles']}
    output = json.loads(json.dumps(before))
    for row in output['profiles']:
        found = observed[row['id']]
        if found['source_version'] == row['kernel']:
            row.update(kernel_ref=found['source_ref'], source_committed_at=found['source_date'])
        if found['recipe_status'] != 'pending-adaptation':
            row.update(recipe_ref=found['recipe_ref'], recipe_sha256=found['recipe_sha256'])
    return output


def receipt_delta(before, after):
    before, after = normalize_receipt(before), normalize_receipt(after)
    if after is None: return {'updated': [], 'new_recipes': []}
    old = {row['id']: row for row in before['profiles']} if before else {}
    old_new = {(r['repository'], r['path']): r for r in before['new_recipes']} if before else {}
    identity = lambda row: {key: value for key, value in row.items() if key != 'recipe_status'} if row else None
    return {'updated': [row for row in after['profiles'] if identity(row) != identity(old.get(row['id']))],
            'new_recipes': [r for r in after['new_recipes'] if r != old_new.get((r['repository'], r['path']))]}


def head_state(snapshot):
    value = normalize_receipt(snapshot)
    if value is None: return None
    return {**value, 'profiles': [{k: v for k, v in row.items() if k != 'recipe_status'} for row in value['profiles']]}


def validate_update(before_profiles, after_profiles, before_receipt, after_receipt, recipe_before=None, recipe_after=None):
    profile_rows(before_profiles); profile_rows(after_profiles)
    snapshot = validate_snapshot(after_receipt, before_profiles)
    normalize_receipt(before_receipt)
    if after_profiles != planned_profiles(before_profiles, snapshot):
        raise UpdateError('OPlus update changed targets, local adaptations, or unrelated profile fields')
    rows = {row['id']: row for row in after_profiles['profiles']}
    for before in before_profiles['profiles']:
        row = rows[before['id']]; path = recipe_path(before)
        if recipe_before is None or recipe_after is None or path not in recipe_before or path not in recipe_after:
            raise UpdateError('OPlus recipe validation needs every frozen base/head recipe')
        old, new = recipe_before[path], recipe_after[path]
        if (hashlib.sha256(old).hexdigest() != before['recipe_sha256']
                or hashlib.sha256(new).hexdigest() != row['recipe_sha256']
                or recipe_status(old, new) == 'pending-adaptation'):
            raise UpdateError('OPlus update changed executable build recipe commands')
    return receipt_delta(before_receipt, after_receipt)


def verify_remote_snapshot(snapshot, profiles_document=None, reader=None):
    if profiles_document is None: profiles_document = catalog.strict_json((ROOT / PROFILE_PATH).read_bytes())
    validate_snapshot(snapshot, profiles_document)
    rows = {row['id']: row for row in profile_rows(profiles_document)}
    reader = reader or Reader()
    for repository, head in snapshot['recipe_heads'].items():
        commit_info(reader.get(repository, 'git/commits/' + head['ref']), head['ref'])
    listings = {}
    for candidate in snapshot['new_recipes']:
        key = candidate['repository'], candidate['ref']
        if key not in listings:
            listing = reader.get(key[0], 'contents/.github/workflows?ref=' + key[1])
            if not isinstance(listing, list) or len(listing) > 512:
                raise UpdateError('Invalid frozen OPlus recipe listing')
            listings[key] = listing
        matches = [entry for entry in listings[key] if isinstance(entry, dict)
                   and entry.get('path') == candidate['path']]
        if (len(matches) != 1 or matches[0].get('type') != 'file'
                or not isinstance(matches[0].get('sha'), str)
                or not catalog.SHA.fullmatch(matches[0]['sha'])):
            raise UpdateError('New OPlus recipe does not exist at its immutable upstream reference')
    def verify(item):
        row = rows[item['id']]
        _, timestamp = commit_info(reader.get(row['kernel_repo'], 'git/commits/' + item['source_ref']), item['source_ref'])
        version = catalog.kernel_version(reader.get(row['kernel_repo'], 'contents/Makefile?ref=' + item['source_ref']))
        raw = content(reader.get(row['recipe_repository'], 'contents/' + row['recipe_workflow'] + '?ref=' + item['recipe_ref']), row['recipe_workflow'])
        if timestamp != item['source_date'] or version != item['source_version'] or hashlib.sha256(raw).hexdigest() != item['recipe_sha256']:
            raise UpdateError('OPlus candidate differs from its immutable upstream bytes')
    with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(verify, snapshot['profiles']))
    return snapshot


def atomic(path, data):
    if path.is_symlink(): raise UpdateError('Refusing a linked OPlus update destination')
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.oplus-sync-', delete=False) as stream:
        temporary = Path(stream.name); stream.write(data)
    try: os.replace(temporary, path)
    finally: temporary.unlink(missing_ok=True)


def apply_snapshot(root, snapshot, reader=None):
    root = Path(root)
    before = catalog.strict_json((root / PROFILE_PATH).read_bytes())
    reader = reader or Reader()
    verify_remote_snapshot(snapshot, before, reader=reader)
    after = planned_profiles(before, snapshot)
    old_bytes, new_bytes = {}, {}
    observations = {row['id']: row for row in snapshot['profiles']}
    for row in before['profiles']:
        path = recipe_path(row); old_bytes[path] = (root / path).read_bytes()
        found = observations[row['id']]
        if found['recipe_status'] == 'pending-adaptation':
            new_bytes[path] = old_bytes[path]
        else:
            new_bytes[path] = content(reader.get(row['recipe_repository'], 'contents/' + row['recipe_workflow'] + '?ref=' + found['recipe_ref']), row['recipe_workflow'])
            if recipe_status(old_bytes[path], new_bytes[path]) != found['recipe_status']:
                raise UpdateError('OPlus executable recipe classification changed')
    previous = catalog.strict_json((root / CANDIDATE_PATH).read_bytes()) if (root / CANDIDATE_PATH).exists() else None
    validate_update(before, after, previous, snapshot, old_bytes, new_bytes)
    # All exact upstream files and local transitions pass before writing any.
    for path, raw in new_bytes.items():
        if raw != old_bytes[path]: atomic(root / path, raw)
    atomic(root / PROFILE_PATH, (json.dumps(after, indent=2, ensure_ascii=False) + '\n').encode())
    atomic(root / CANDIDATE_PATH, (json.dumps(snapshot, indent=2, ensure_ascii=False) + '\n').encode())
    return after


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply-candidates-json')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        profiles = catalog.strict_json((root / PROFILE_PATH).read_bytes())
        snapshot = fetch_snapshot(root) if args.check else validate_snapshot(catalog.strict_json(args.apply_candidates_json.encode()), profiles)
        previous = catalog.strict_json((root / CANDIDATE_PATH).read_bytes()) if (root / CANDIDATE_PATH).exists() else None
        changed = head_state(snapshot) != head_state(previous) or planned_profiles(profiles, snapshot) != profiles
        if args.apply_candidates_json is not None: apply_snapshot(root, snapshot)
        if args.github_output:
            if args.github_output.is_symlink(): raise UpdateError('GitHub output must be a regular file')
            with args.github_output.open('a', encoding='utf-8', newline='\n') as stream:
                stream.write('has_updates=' + str(changed).lower() + '\n')
                stream.write('candidates_json=' + json.dumps(snapshot, sort_keys=True, separators=(',', ':')) + '\n')
        print('OPlus source observations updated; changed build commands remain pending adaptation.' if changed else 'No new OPlus source updates.')
        return 0
    except (OSError, ValueError, UnicodeError, KeyError) as error:
        print('::error::OPlus source sync stopped: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
