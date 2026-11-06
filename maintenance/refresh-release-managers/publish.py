#!/usr/bin/env python3
"""Publish an explicitly frozen UI-only refresh to existing cyzrqh/GKI assets.

plan is offline; verify/dry-run perform GETs only; execute is the only writer.
No command creates a release/tag, changes latest, or invokes an Actions run.
Every upload is staged and downloaded for verification before ANY old asset is
deleted. APK and SHA256SUMS replacements are the sole authorized asset writes.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

REPOSITORY = 'cyzrqh/GKI'
UI_FIX_COMMIT = '0f61220ea4da784e1633069271103e1687524916'
AUTHORIZED = {'r2.1': 434932408, 'r2.0': 434815478, 'r1.20': 434447315,
              'r1.19': 433860069, 'r1.18': 433721765, 'r1.17': 433408992,
              'r1.16': 433406194, 'r1.15': 433389123, 'r1.14': 430418482,
              'r1.13': 430370990}
PROTECTED = {'r1.12': 430364781, 'r1.11': 430321556, 'r1.10': 430319830,
             'r1.9': 430119554, 'r1.8': 429013953, 'r1.7': 428714162,
             'r1.6': 428265591, 'r1.5': 427931288, 'r1.4': 427467646,
             'r1.3': 425840591, 'r1.2': 424959481, 'r1.1': 424829988,
             'r1.0': 423466531}
SHA = re.compile(r'[0-9a-f]{64}\Z')
COMMIT = re.compile(r'[0-9a-f]{40}\Z')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+@()-]{0,249}\Z')
MAX_APK = 256 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024
FROZEN = ('id', 'tag_name', 'target_commitish', 'name', 'body', 'draft',
          'prerelease', 'published_at', 'immutable')
ASSET = ('id', 'name', 'size', 'digest', 'label', 'content_type', 'state')
CHECKS = ('version', 'package', 'signer', 'source', 'native_payload', 'abi_set',
          'lkm_payload', 'ui_only')
RUN_MARKER = re.compile(r'<!--\s*gki-release-run:v1\b.*?-->', re.S)
PUBLIC_HOSTS = {'release-assets.githubusercontent.com', 'objects.githubusercontent.com',
                'github-releases.githubusercontent.com'}


class PublishError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PublishError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def unique_object(items):
    result = {}
    for key, value in items:
        require(key not in result, 'Duplicate JSON field')
        result[key] = value
    return result


def decode_json(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(PublishError('Invalid JSON constant')))


def read_json(path):
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= MAX_JSON,
            'Expected a bounded JSON file: ' + str(path))
    return decode_json(path.read_bytes())


def atomic_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink() and not path.parent.is_symlink(), 'Linked JSON outputs are refused')
    temporary = path.with_name(path.name + '.tmp')
    require(not temporary.exists() and not temporary.is_symlink(), 'A previous JSON staging file requires inspection')
    temporary.write_bytes(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                    indent=2, allow_nan=False).encode('utf-8') + b'\n')
    os.replace(temporary, path)


def positive(value):
    return type(value) is int and value > 0


def asset_record(value):
    require(isinstance(value, dict) and all(key in value for key in ASSET), 'Incomplete asset inventory')
    record = {key: value[key] for key in ASSET}
    require(positive(record['id']) and isinstance(record['name'], str) and NAME.fullmatch(record['name'])
            and type(record['size']) is int and 0 < record['size'] <= MAX_APK
            and isinstance(record['digest'], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', record['digest'])
            and record['state'] == 'uploaded'
            and (record['label'] is None or isinstance(record['label'], str) and len(record['label']) <= 1024)
            and isinstance(record['content_type'], str)
            and not any(c in record['content_type'] for c in '\r\n'),
            'Invalid frozen asset identity')
    return record


def inventory(records):
    require(isinstance(records, list) and 1 <= len(records) <= 10000, 'Invalid asset count')
    result = sorted((asset_record(row) for row in records), key=lambda row: row['id'])
    require(len({row['id'] for row in result}) == len(result)
            and len({row['name'] for row in result}) == len(result), 'Duplicate asset ID/name')
    return result


def freeze_plan(releases, replacements, *, latest_release_id, ui_commit=UI_FIX_COMMIT,
                producer_head_sha=None):
    """Helper for the caller: freeze raw release snapshots plus authorized rows.

    replacements rows require release_id, asset_id, name, old_sha256,
    new_sha256, built_file, receipt_file. Paths are relative to CLI directories.
    """
    grouped = {}
    for row in replacements:
        grouped.setdefault(row['release_id'], []).append({key: value for key, value in row.items()
                                                         if key != 'release_id'})
    rows, protected = [], []
    for release in releases:
        if release['id'] not in grouped:
            frozen = {key: release.get(key) for key in FROZEN}
            frozen['assets'] = inventory(release['assets'])
            protected.append(frozen)
            continue
        row = {key: release.get(key) for key in FROZEN}
        row['assets'] = inventory(release['assets'])
        row['replacements'] = grouped.pop(release['id'])
        sums = [asset for asset in row['assets'] if asset['name'] == 'SHA256SUMS.txt']
        require(len(sums) == 1, 'A release must have exactly one SHA256SUMS.txt')
        row['checksum_asset_id'] = sums[0]['id']
        rows.append(row)
    require(not grouped, 'Replacement refers to a missing release')
    plan = {'schema_version': 1, 'repository': REPOSITORY, 'ui_commit': ui_commit,
            'producer_head_sha': producer_head_sha, 'latest_release_id': latest_release_id,
            'require_all_apks': True, 'releases': rows, 'protected_releases': protected}
    validate_plan(plan, allow_unbuilt=True)
    return plan


def validate_plan(plan, *, allow_unbuilt=False):
    require(isinstance(plan, dict) and type(plan.get('schema_version')) is int and plan['schema_version'] == 1
            and plan.get('repository') == REPOSITORY and plan.get('ui_commit') == UI_FIX_COMMIT
            and (allow_unbuilt and plan.get('producer_head_sha') is None
                 or COMMIT.fullmatch(str(plan.get('producer_head_sha', ''))))
            and positive(plan.get('latest_release_id')) and plan.get('require_all_apks') is True,
            'Expected a frozen cyzrqh/GKI UI refresh plan')
    if 'authorized_release_ids' in plan:
        require(isinstance(plan['authorized_release_ids'], list)
                and len(plan['authorized_release_ids']) == len(AUTHORIZED)
                and set(plan['authorized_release_ids']) == set(AUTHORIZED.values()),
                'Explicit authorization IDs differ from the user-approved scope')
    rows = plan.get('releases')
    require(isinstance(rows, list) and len(rows) == len(AUTHORIZED), 'Only the authorized latest ten releases may be selected')
    require({row.get('tag_name'): row.get('id') for row in rows} == AUTHORIZED,
            'Release scope differs from the exact authorized ten release IDs/tags')
    protected = plan.get('protected_releases')
    require(isinstance(protected, list) and len(protected) == len(PROTECTED)
            and {row.get('tag_name'): row.get('id') for row in protected} == PROTECTED,
            'The thirteen older releases require their complete read-only frozen snapshot')
    seen, asset_ids, hashes = set(), set(), {}
    for row in rows:
        require(isinstance(row, dict) and all(key in row for key in FROZEN), 'Incomplete frozen release')
        require(positive(row['id']) and row['id'] not in seen and row['draft'] is False
                and row['prerelease'] is False and row['immutable'] is False
                and isinstance(row['published_at'], str) and row['published_at']
                and isinstance(row['tag_name'], str) and row['tag_name']
                and COMMIT.fullmatch(str(row['target_commitish']))
                and isinstance(row['body'], str) and len(row['body'].encode('utf-8')) <= 4 * 1024 * 1024,
                'Release must be a mutable, already published, fixed-commit release')
        seen.add(row['id'])
        assets = inventory(row.get('assets'))
        require(not asset_ids & {asset['id'] for asset in assets}, 'Asset IDs must be globally unique')
        asset_ids.update(asset['id'] for asset in assets)
        selected = row.get('replacements')
        require(isinstance(selected, list) and selected, 'Every selected release requires APK replacements')
        by_id = {asset['id']: asset for asset in assets}
        wanted = set()
        for item in selected:
            require(isinstance(item, dict) and all(key in item for key in
                    ('asset_id', 'name', 'old_sha256', 'new_sha256', 'built_file', 'receipt_file')),
                    'Incomplete APK replacement')
            old = by_id.get(item['asset_id'])
            require(old is not None and item['asset_id'] not in wanted and old['name'] == item['name']
                    and old['name'].endswith('.apk') and SHA.fullmatch(str(item['old_sha256']))
                    and (allow_unbuilt and item['new_sha256'] is None or SHA.fullmatch(str(item['new_sha256'])))
                    and old['digest'] == 'sha256:' + item['old_sha256']
                    and item['new_sha256'] != item['old_sha256'], 'Invalid exact authorized APK replacement')
            wanted.add(item['asset_id'])
            if item['new_sha256'] is not None:
                previous = hashes.setdefault(item['old_sha256'], item['new_sha256'])
                require(previous == item['new_sha256'], 'One original APK cannot map to two new APKs')
        require(wanted == {a['id'] for a in assets if a['name'].endswith('.apk')},
                'All APKs in each frozen release must be accounted for')
        checksum = by_id.get(row.get('checksum_asset_id'))
        require(checksum is not None and checksum['name'] == 'SHA256SUMS.txt'
                and len([a for a in assets if a['name'] == 'SHA256SUMS.txt']) == 1,
                'Checksum asset is not the frozen exact SHA256SUMS.txt')
    for row in protected:
        require(all(key in row for key in FROZEN) and row['draft'] is False and row['immutable'] is False
                and isinstance(row['body'], str), 'Incomplete protected release snapshot')
        untouched = inventory(row.get('assets'))
        require(not asset_ids & {asset['id'] for asset in untouched}, 'Protected assets overlap mutation scope')
        asset_ids.update(asset['id'] for asset in untouched)
    return plan


def normalize_plan(plan):
    plan = copy.deepcopy(plan)
    # The caller may freeze the full public API objects. Exclude volatile
    # counters/timestamps/uploader details from stable inventory comparisons.
    for row in plan.get('releases', []) + plan.get('protected_releases', []):
        row['assets'] = inventory(row.get('assets'))
    for row in plan.get('releases', []):
        for item in row.get('replacements', []):
            item.setdefault('new_sha256', None)
    plan.setdefault('producer_head_sha', None)
    return plan


def bind_receipts(plan, receipts, producer_head_sha=None):
    """Fill only runtime producer SHA/new APK SHA; frozen old release data stays unchanged."""
    plan = normalize_plan(plan)
    if producer_head_sha is not None:
        require(COMMIT.fullmatch(producer_head_sha), 'Invalid runtime UI producer SHA')
        require(plan.get('producer_head_sha') in (None, producer_head_sha), 'Runtime producer differs from frozen plan')
        plan['producer_head_sha'] = producer_head_sha
    validate_plan(plan, allow_unbuilt=True)
    for row in plan['releases']:
        for item in row['replacements']:
            receipt = read_json(regular_file(receipts, item['receipt_file'], MAX_JSON))
            require(receipt.get('old_apk_sha256') == item['old_sha256']
                    and receipt.get('asset_name') == item['name'], 'Receipt differs from frozen original APK')
            new_hash = receipt.get('new_apk_sha256')
            require(SHA.fullmatch(str(new_hash)) and item.get('new_sha256') in (None, new_hash),
                    'Receipt differs from frozen new APK hash')
            item['new_sha256'] = new_hash
    return validate_plan(plan)


def regular_file(directory, relative, limit=MAX_APK):
    require(isinstance(relative, str) and relative and '\\' not in relative,
            'Expected a forward-slash relative file path')
    parts = relative.split('/')
    require(all(part and part not in ('.', '..') and ':' not in part for part in parts),
            'File path escapes the selected directory')
    directory = Path(directory).resolve(strict=True)
    path = directory.joinpath(*parts)
    for component in (path, *path.parents):
        if component == directory:
            break
        require(not component.is_symlink() and not (hasattr(component, 'is_junction') and component.is_junction()),
                'Linked input paths are refused')
    require(path.is_file() and 0 < path.stat().st_size <= limit and path.resolve().is_relative_to(directory),
            'Missing or oversized input: ' + relative)
    return path


def verify_receipt(receipt, item, plan):
    require(isinstance(receipt, dict) and type(receipt.get('schema_version')) is int and receipt['schema_version'] == 1
            and receipt.get('status') == 'passed' and receipt.get('verified') is True
            and receipt.get('asset_name') == item['name']
            and receipt.get('old_apk_sha256') == item['old_sha256']
            and receipt.get('new_apk_sha256') == item['new_sha256'], 'Receipt does not bind this exact APK refresh')
    checks = receipt.get('checks')
    require(isinstance(checks, dict) and all(checks.get(key) is True for key in CHECKS),
            'The local verifier has not passed every required preservation check')
    producer = receipt.get('producer')
    require(isinstance(producer, dict) and producer.get('repository') == REPOSITORY
            and producer.get('head_sha') == plan['producer_head_sha']
            and re.fullmatch(r'[1-9][0-9]*', str(producer.get('run_id', '')))
            and re.fullmatch(r'[1-9][0-9]*', str(producer.get('run_attempt', ''))),
            'Receipt lacks exact UI build provenance')
    require(isinstance(receipt.get('identity'), dict), 'Receipt lacks preserved APK identity')


def verify_apks(original, replacement, receipt, filename):
    """Independently verify manifests/signatures/identities/native bytes, not only a passed flag."""
    from apk_manifest import manifest_identity
    from embedded_build_metadata import (APK_MEMBER, metadata_json, read_manager_apk,
                                         validate_members, verify_apk_v2)
    old_raw, new_raw = original.read_bytes(), replacement.read_bytes()
    old_cert, new_cert = verify_apk_v2(old_raw), verify_apk_v2(new_raw)
    require(old_cert == new_cert, 'APK signing certificate changed')
    new_identity = read_manager_apk(new_raw, filename=filename)
    documents, manifests, protected = [], [], []
    for raw in (old_raw, new_raw):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = validate_members(archive)
            require(sum(member.file_size for member in members.values()) <= MAX_APK and not archive.testzip(),
                    'APK ZIP integrity or expansion bound failed')
            documents.append(metadata_json(archive.read(APK_MEMBER)))
            manifests.append(manifest_identity(archive.read('AndroidManifest.xml')))
            # All original assets are preserved, including daemon inputs and
            # every possible embedded native/LKM container. Only signed build
            # provenance is intentionally regenerated by the signer.
            protected.append({name: sha(archive.read(member)) for name, member in members.items()
                              if (name.startswith(('lib/', 'assets/')) and name != APK_MEMBER)
                              or name.endswith(('.ko', '.kpm'))})
    require(manifests[0] == manifests[1], 'Actual Android package/version changed')
    require(protected[0] == protected[1], 'APK native/ABI/assets/LKM payload changed')
    fields = ('file', 'package', 'ksu_variant', 'ksu_ref', 'ksu_branch', 'ksu_version_code',
              'ksu_version_name', 'manager_cert_size', 'manager_cert_sha256')
    for key in fields:
        require(key in documents[0] and str(documents[0][key]) == str(documents[1].get(key)),
                'APK historical identity changed: ' + key)
    identity = receipt['identity']
    expected = {'variant': new_identity['ksu_variant'], 'package': manifests[0]['package'],
                'version_code': manifests[0]['version_code'], 'version_name': manifests[0]['version_name'],
                'ksu_ref': new_identity['ksu_ref'], 'certificate_sha256': sha(new_cert)}
    require(all(str(identity.get(key)) == str(value) for key, value in expected.items()),
            'Receipt identity differs from the actual signed APK')
    producer = receipt['producer']
    for key, actual in (('repository', 'producer_repository'), ('head_sha', 'producer_head_sha'),
                        ('run_id', 'producer_run_id'), ('run_attempt', 'producer_run_attempt')):
        require(str(producer[key]) == str(new_identity[actual]), 'Receipt producer differs from the signed APK')


def updated_checksums(raw, row):
    by_name = {asset['name']: asset for asset in row['assets'] if asset['name'] != 'SHA256SUMS.txt'}
    replacements = {item['name']: item for item in row['replacements']}
    seen, result = set(), []
    for line in raw.decode('utf-8').splitlines(keepends=True):
        match = re.fullmatch(r'([0-9a-f]{64}) ([ *])([^\r\n]+)(\r?\n)?', line)
        require(match is not None and match[3] in by_name and match[3] not in seen,
                'Checksum list has a missing, duplicate, or unexpected filename')
        name = match[3]
        require(by_name[name]['digest'] == 'sha256:' + match[1], 'Original checksum differs from frozen asset digest')
        seen.add(name)
        if name in replacements:
            item = replacements[name]
            require(match[1] == item['old_sha256'], 'Original APK checksum does not match the replacement')
            line = item['new_sha256'] + line[64:]
        result.append(line)
    require(seen == set(by_name), 'Original checksum list does not cover the whole frozen release')
    return ''.join(result).encode('utf-8')


def updated_body(row, plan, receipt_hashes, producers):
    replacements = {item['old_sha256']: item['new_sha256'] for item in row['replacements']}
    # Existing hidden provenance is historical evidence, not a current summary.
    pieces = re.split(r'(<!--[\s\S]*?-->)', row['body'])
    digest_pattern = (r'(?<![0-9a-fA-F])(?:' + '|'.join(re.escape(value) for value in replacements)
                      + r')(?![0-9a-fA-F])')
    for index in range(0, len(pieces), 2):
        pieces[index] = re.sub(digest_pattern, lambda match: replacements[match[0].lower()],
                               pieces[index], flags=re.I)
    body = ''.join(pieces)
    marker = ('<!-- gki-manager-ui-refresh:v1 ui_commit=' + plan['ui_commit']
              + ' plan_sha256=' + sha(canonical(plan)) + ' receipts_sha256='
              + sha(canonical(sorted(receipt_hashes))) + ' producer_head_sha=' + plan['producer_head_sha']
              + ' producer_runs=' + ','.join(sorted(producers))
              + ' apk_count=' + str(len(row['replacements'])) + ' -->')
    body += ('' if not body or body.endswith('\n') else '\n') + '\n' + marker + '\n'
    require(RUN_MARKER.findall(body) == RUN_MARKER.findall(row['body']), 'Historical Actions run marker changed')
    return body


def local_preflight(plan, built, receipts, backup):
    validate_plan(plan)
    prepared, verified = [], set()
    for row in plan['releases']:
        by_id = {asset['id']: asset for asset in row['assets']}
        changes, receipt_hashes, producers = [], [], set()
        for item in row['replacements']:
            original = regular_file(backup, str(row['id']) + '/' + item['name'])
            replacement = regular_file(built, item['built_file'])
            receipt_file = regular_file(receipts, item['receipt_file'], MAX_JSON)
            require(sha(original.read_bytes()) == item['old_sha256']
                    and original.stat().st_size == by_id[item['asset_id']]['size']
                    and sha(replacement.read_bytes()) == item['new_sha256'], 'APK input differs from the frozen SHA256')
            receipt = read_json(receipt_file)
            verify_receipt(receipt, item, plan)
            receipt_hash = sha(receipt_file.read_bytes())
            key = (item['old_sha256'], item['new_sha256'], receipt_hash)
            if key not in verified:
                verify_apks(original, replacement, receipt, item['name'])
                verified.add(key)
            receipt_hashes.append(receipt_hash)
            producers.add(str(receipt['producer']['run_id']) + '.' + str(receipt['producer']['run_attempt']))
            changes.append({'original': by_id[item['asset_id']], 'file': replacement,
                            'backup': original, 'sha256': item['new_sha256']})
        old_sum = by_id[row['checksum_asset_id']]
        old_file = regular_file(backup, str(row['id']) + '/SHA256SUMS.txt', 2 * 1024 * 1024)
        old_raw = old_file.read_bytes()
        require(sha(old_raw) == old_sum['digest'][7:] and len(old_raw) == old_sum['size'], 'Original checksum backup changed')
        new_raw = updated_checksums(old_raw, row)
        changes.append({'original': old_sum, 'data': new_raw, 'backup': old_file, 'sha256': sha(new_raw)})
        prepared.append({'release': row, 'changes': changes,
                         'body': updated_body(row, plan, receipt_hashes, producers)})
    return prepared


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class GitHub:
    def __init__(self, token):
        require(isinstance(token, str) and token and not any(character.isspace() for character in token),
                'GITHUB_TOKEN or GH_TOKEN is required for online verification/publication')
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, resource, *, method='GET', document=None, data=None, accept='application/vnd.github+json',
                content_type='application/json', limit=MAX_JSON):
        host = 'uploads.github.com' if resource.startswith('/upload/') else 'api.github.com'
        path = resource.removeprefix('/upload')
        require(path.startswith('/repos/' + REPOSITORY + '/releases'), 'GitHub operation outside the fixed release repository')
        payload = canonical(document) if document is not None else data
        request = Request('https://' + host + path, data=payload, method=method,
                          headers={'Authorization': 'Bearer ' + self.token, 'Accept': accept,
                                   'Content-Type': content_type, 'X-GitHub-Api-Version': '2022-11-28',
                                   'User-Agent': 'gki-historical-manager-ui-refresh'})
        for attempt in range(3 if method == 'GET' else 1):
            try:
                with self.opener.open(request, timeout=180) as response:
                    raw = response.read(limit + 1)
                    require(len(raw) <= limit, 'GitHub response exceeds the allowed size')
                    return raw
            except HTTPError as error:
                status = error.code
                location = error.headers.get('Location', '')
                error.close()
                if method == 'GET' and accept == 'application/octet-stream' and status in (301, 302, 303, 307, 308):
                    parsed = urlsplit(location)
                    require(parsed.scheme == 'https' and parsed.hostname in PUBLIC_HOSTS and not parsed.username and not parsed.password,
                            'Asset download redirect is outside public GitHub asset hosts')
                    # Do not forward the repository token to the public asset host.
                    with build_opener(NoRedirect()).open(Request(location, headers={'User-Agent': 'gki-manager-refresh'}), timeout=180) as response:
                        raw = response.read(limit + 1)
                        require(len(raw) <= limit, 'Asset download exceeds the allowed size')
                        return raw
                if method == 'GET' and status in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise PublishError('GitHub ' + method + ' failed (HTTP ' + str(status) + ')') from None
            except (URLError, OSError):
                if method == 'GET' and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise PublishError('GitHub ' + method + ' transport failed; inspect the journal before retrying') from None
        raise PublishError('GitHub operation failed')

    def json(self, resource, **kwargs):
        raw = self.request('/repos/' + REPOSITORY + resource, **kwargs)
        return decode_json(raw) if raw else None

    def get_release(self, release_id):
        return self.json('/releases/' + str(release_id))

    def get_latest(self):
        return self.json('/releases/latest')['id']

    def list_assets(self, release_id):
        rows = []
        for page in range(1, 102):
            result = self.json('/releases/' + str(release_id) + '/assets?' + urlencode({'per_page': 100, 'page': page}))
            require(isinstance(result, list) and len(result) <= 100, 'Invalid paginated asset inventory')
            rows.extend(result)
            if len(result) < 100:
                return inventory(rows)
        raise PublishError('Asset inventory exceeds the lookup budget')

    def download(self, asset_id):
        return self.request('/repos/' + REPOSITORY + '/releases/assets/' + str(asset_id),
                            accept='application/octet-stream', limit=MAX_APK)

    def upload(self, release_id, name, data, content_type):
        raw = self.request('/upload/repos/' + REPOSITORY + '/releases/' + str(release_id)
                           + '/assets?' + urlencode({'name': name}), method='POST', data=data,
                           content_type=content_type)
        return asset_record(decode_json(raw))

    def rename(self, asset_id, name, label):
        return asset_record(self.json('/releases/assets/' + str(asset_id), method='PATCH',
                                     document={'name': name, 'label': label}))

    def delete(self, asset_id):
        self.json('/releases/assets/' + str(asset_id), method='DELETE')

    def body(self, release_id, body):
        return self.json('/releases/' + str(release_id), method='PATCH', document={'body': body})


def check_metadata(api, row, *, body=None):
    current = api.get_release(row['id'])
    for key in FROZEN:
        require(current.get(key) == (body if key == 'body' and body is not None else row[key]),
                'Frozen release metadata changed: ' + row['tag_name'] + '/' + key)


def online_preflight(api, plan, prepared):
    require(api.get_latest() == plan['latest_release_id'], 'The original latest release changed')
    for record in prepared:
        row = record['release']
        check_metadata(api, row)
        require(api.list_assets(row['id']) == inventory(row['assets']), 'Full release inventory changed: ' + row['tag_name'])
    verify_protected(api, plan)


def verify_protected(api, plan):
    for row in plan['protected_releases']:
        check_metadata(api, row)
        require(api.list_assets(row['id']) == inventory(row['assets']),
                'An older protected release changed: ' + row['tag_name'])


def change_bytes(change):
    raw = change['file'].read_bytes() if 'file' in change else change['data']
    require(sha(raw) == change['sha256'], 'Prepared replacement changed after verification')
    return raw


def verify_uploaded(api, asset, expected, raw):
    require(asset['digest'] == 'sha256:' + expected and asset['size'] == len(raw), 'Uploaded asset metadata does not match')
    downloaded = api.download(asset['id'])
    require(len(downloaded) == len(raw) and sha(downloaded) == expected, 'Uploaded bytes failed download verification')


def expected_final(record):
    changed = {item['original']['id']: item for item in record['changes']}
    result = []
    for old in record['release']['assets']:
        change = changed.get(old['id'])
        if change is None:
            result.append(old)
        else:
            asset = copy.deepcopy(change['staged'])
            asset.update(name=old['name'], label=old['label'])
            result.append(asset)
    return inventory(result)


def rollback_record(api, record):
    """Restore payloads/body only; original GitHub IDs cannot be recreated."""
    row = record['release']
    current = api.get_release(row['id'])
    require(all(current.get(key) == row[key] for key in FROZEN if key != 'body'),
            'Refusing rollback over independently changed release metadata')
    require(current.get('body') in (row['body'], record['body']), 'Refusing rollback over independently edited release notes')
    for change in reversed(record['changes']):
        old = change['original']
        current_assets = api.list_assets(row['id'])
        original = next((a for a in current_assets if a['id'] == old['id']), None)
        if original is not None:
            require(original == old, 'Original authorized asset was changed externally')
            continue
        named = next((a for a in current_assets if a['name'] == old['name']), None)
        if named is not None:
            require('staged' in change and named['id'] == change['staged']['id']
                    and named['digest'] == 'sha256:' + change['sha256'],
                    'Refusing rollback over an unknown asset with the target name')
        raw = change['backup'].read_bytes()
        require(sha(raw) == old['digest'][7:] and len(raw) == old['size'], 'Rollback backup no longer matches')
        restore_name = 'gki-ui-rollback-' + str(old['id']) + '.upload'
        restored = api.upload(row['id'], restore_name, raw, old['content_type'])
        verify_uploaded(api, restored, old['digest'][7:], raw)
        # A failed rollback upload must leave the already verified new APK in
        # place rather than deleting it before a replacement is available.
        if named is not None:
            api.delete(named['id'])
        restored = api.rename(restored['id'], old['name'], old['label'])
        require(restored['name'] == old['name'] and restored['digest'] == old['digest'], 'Rollback rename failed')
    if current['body'] != row['body']:
        api.body(row['id'], row['body'])


def execute(api, plan, prepared, journal_path):
    online_preflight(api, plan, prepared)
    plan_hash = sha(canonical(plan))
    journal = {'schema_version': 1, 'plan_sha256': plan_hash, 'repository': REPOSITORY,
               'state': 'staging', 'events': []}
    require(not journal_path.exists(), 'An execution journal already exists; inspect it before making a new frozen plan')
    atomic_json(journal_path, journal)

    def event(kind, **values):
        journal['events'].append({'kind': kind, **values})
        atomic_json(journal_path, journal)

    modified = []
    try:
        # Stage the ENTIRE batch first. A failed upload cannot strand an old
        # APK/checksum that has already been deleted in an earlier release.
        for record in prepared:
            row = record['release']
            for change in record['changes']:
                old = change['original']
                raw = change_bytes(change)
                name = 'gki-ui-' + plan_hash[:12] + '-' + str(old['id']) + '.upload'
                change['temporary_name'] = name
                event('upload-start', release_id=row['id'], original_asset_id=old['id'], temporary_name=name,
                      expected_sha256=change['sha256'])
                asset = api.upload(row['id'], name, raw, old['content_type'])
                change['staged'] = asset
                event('uploaded', release_id=row['id'], original_asset_id=old['id'], staged=asset)
                verify_uploaded(api, asset, change['sha256'], raw)
        require(api.get_latest() == plan['latest_release_id'], 'Latest release changed during staging')
        verify_protected(api, plan)
        for record in prepared:
            row = record['release']
            check_metadata(api, row)
            staged = [change['staged'] for change in record['changes']]
            require(api.list_assets(row['id']) == inventory(row['assets'] + staged),
                    'Full inventory changed during staging')
        journal['state'] = 'replacing'
        atomic_json(journal_path, journal)
        for record in prepared:
            row = record['release']
            check_metadata(api, row)
            require(api.list_assets(row['id']) == inventory(row['assets'] + [c['staged'] for c in record['changes']]),
                    'Full inventory changed before committing this release')
            modified.append(record)
            for change in record['changes']:
                old = change['original']
                event('delete-start', release_id=row['id'], original_asset_id=old['id'])
                api.delete(old['id'])
                event('deleted', release_id=row['id'], original_asset_id=old['id'])
                renamed = api.rename(change['staged']['id'], old['name'], old['label'])
                require(renamed['id'] == change['staged']['id'] and renamed['name'] == old['name']
                        and renamed['digest'] == 'sha256:' + change['sha256'], 'Staged asset rename failed')
                change['staged'] = renamed
                event('renamed', release_id=row['id'], original_asset_id=old['id'], final=renamed)
            api.body(row['id'], record['body'])
            check_metadata(api, row, body=record['body'])
            require(api.list_assets(row['id']) == expected_final(record), 'Final inventory changed unexpectedly')
            event('release-verified', release_id=row['id'], final_assets=expected_final(record))
        for record in prepared:
            check_metadata(api, record['release'], body=record['body'])
            require(api.list_assets(record['release']['id']) == expected_final(record),
                    'A previously committed release changed during the batch')
        require(api.get_latest() == plan['latest_release_id'], 'Latest release was changed unexpectedly')
        verify_protected(api, plan)
        journal['state'] = 'complete'
        atomic_json(journal_path, journal)
        return journal
    except BaseException as failure:
        errors = []
        for record in reversed(modified):
            try:
                rollback_record(api, record)
                event('rolled-back', release_id=record['release']['id'])
            except Exception as error:
                errors.append(str(error))
        # Remove only temporary assets that match this exact attempt's name,
        # size and expected bytes. Never touch an unrecognized attachment.
        for record in prepared:
            try:
                assets = api.list_assets(record['release']['id'])
                for change in record['changes']:
                    candidate = next((a for a in assets if a['name'] == change.get('temporary_name')), None)
                    if candidate is not None:
                        require(candidate['digest'] == 'sha256:' + change['sha256'], 'Unknown staging asset blocks cleanup')
                        api.delete(candidate['id'])
            except Exception as error:
                errors.append(str(error))
        for record in modified:
            try:
                row = record['release']
                check_metadata(api, row)
                current = {asset['name']: asset for asset in api.list_assets(row['id'])}
                require(set(current) == {asset['name'] for asset in row['assets']},
                        'Rollback did not restore the exact original asset names')
                allowed = {change['original']['id'] for change in record['changes']}
                for old in row['assets']:
                    actual = current[old['name']]
                    require(all(actual[key] == old[key] for key in ASSET if key != 'id')
                            and (old['id'] in allowed or actual['id'] == old['id']),
                            'Rollback changed an original asset payload or an unrelated asset ID')
                require(api.get_latest() == plan['latest_release_id'], 'Rollback latest release differs')
            except Exception as error:
                errors.append(str(error))
        journal['state'] = 'rollback-incomplete' if errors else 'rolled-back'
        journal['failure'] = str(failure)
        journal['rollback_errors'] = errors
        atomic_json(journal_path, journal)
        raise PublishError('Publication stopped; ' + journal['state']
                           + '. Original backups retained. Journal: ' + str(journal_path)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--built', type=Path, required=True)
    parser.add_argument('--receipts', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--mode', choices=('plan', 'verify', 'dry-run', 'execute'), default='verify')
    parser.add_argument('--tools', type=Path, default=Path.cwd() / 'tools')
    parser.add_argument('--journal', type=Path)
    parser.add_argument('--ui-head', default=os.environ.get('GITHUB_SHA'),
                        help='Actual maintenance workflow SHA; distinct from fixed 0f61220 UI fix')
    parser.add_argument('--write-bound-plan', type=Path,
                        help='Write exact receipt-bound final plan after successful local checks')
    args = parser.parse_args(argv)
    try:
        sys.path.insert(0, str(args.tools.resolve(strict=True)))
        source_plan = read_json(args.plan)
        if args.mode == 'plan':
            plan = validate_plan(normalize_plan(source_plan), allow_unbuilt=True)
            require(args.write_bound_plan is None, 'Receipt-bound output requires verify/dry-run, not the offline scope preview')
            print(json.dumps({'mode': 'plan', 'state': 'planned-not-published', 'repository': REPOSITORY,
                              'releases': [{'id': row['id'], 'tag': row['tag_name'],
                                            'apks': len(row['replacements'])} for row in plan['releases']],
                              'apk_replacements': sum(len(row['replacements']) for row in plan['releases']),
                              'protected_releases': len(plan['protected_releases']),
                              'network_reads': False, 'network_writes': False}, ensure_ascii=False, sort_keys=True))
            return 0
        plan = bind_receipts(source_plan, args.receipts, args.ui_head)
        prepared = local_preflight(plan, args.built, args.receipts, args.backup)
        if args.write_bound_plan is not None:
            require(args.write_bound_plan.resolve() != args.plan.resolve(), 'Bound plan must not overwrite frozen input')
            require(not args.write_bound_plan.exists(), 'Bound plan output already exists; preserve existing inputs')
            atomic_json(args.write_bound_plan, plan)
        summary = {'mode': args.mode, 'plan_sha256': sha(canonical(plan)), 'repository': REPOSITORY,
                   'releases': len(prepared), 'apk_replacements': sum(len(row['replacements']) for row in plan['releases']),
                   'checksum_replacements': len(prepared), 'network_writes': False}
        if args.mode != 'plan':
            api = GitHub(os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN', ''))
            if args.mode == 'execute':
                journal_path = args.journal or args.backup / ('.publisher-' + summary['plan_sha256'][:16] + '.json')
                result = execute(api, plan, prepared, journal_path)
                summary.update(network_writes=True, state=result['state'], journal=str(journal_path))
            else:
                online_preflight(api, plan, prepared)
                summary['state'] = 'verified-no-mutation'
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        print('Manager UI publication stopped: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
