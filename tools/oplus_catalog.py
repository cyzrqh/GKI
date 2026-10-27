#!/usr/bin/env python3
"""Resolve reviewed OPlus recipes against immutable official kernel sources."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r'[0-9a-f]{40}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
PLATFORM = re.compile(r'(?:sm|mt)[0-9]{4}\Z')
KERNEL = re.compile(r'6\.(1|6|12)\.([1-9][0-9]*|0)\Z')
ANDROID = {'6.1': 'android14', '6.6': 'android15', '6.12': 'android16'}
OFFICIAL_REPOSITORIES = frozenset({
    'OnePlusOSS/android_kernel_common_oneplus_sm8650',
    'OnePlusOSS/android_kernel_common_oneplus_sm8750',
    'OnePlusOSS/android_kernel_common_oneplus_sm8845',
    'OnePlusOSS/android_kernel_common_oneplus_sm8850',
    'OnePlusOSS/android_kernel_oneplus_mt6897',
    'OnePlusOSS/android_kernel_oneplus_mt6989',
    'OnePlusOSS/android_kernel_oneplus_mt6991',
    'OnePlusOSS/android_kernel_oneplus_mt6993',
    'oppo-source/android_kernel_oppo_mt6993',
})
RECIPE_REPOSITORIES = frozenset('cctv18/oppo_oplus_realme_' + chip
                              for chip in ('sm8650', 'sm8750', 'sm8850'))
RECIPE_FOR_SERIES = {'6.1': 'cctv18/oppo_oplus_realme_sm8650',
                     '6.6': 'cctv18/oppo_oplus_realme_sm8750',
                     '6.12': 'cctv18/oppo_oplus_realme_sm8850'}
FIELDS = frozenset(('id', 'platform', 'kernel', 'android_version', 'kernel_version',
                    'sub_level', 'kernel_repo', 'kernel_branch', 'recipe_repository',
                    'recipe_ref', 'recipe_workflow', 'recipe_sha256', 'system',
                    'compatibility', 'verified_kernel_ref'))
MAX_JSON = 4 * 1024 * 1024


class CatalogError(ValueError):
    """A source cannot be proven to match its reviewed build recipe."""


def unique_object(items):
    document = {}
    for key, value in items:
        if key in document:
            raise CatalogError('Duplicate OPlus source metadata field')
        document[key] = value
    return document


def strict_json(raw: bytes):
    def reject_constant(_):
        raise ValueError('Non-finite JSON number')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    except (ValueError, UnicodeError) as error:
        raise CatalogError('OPlus source metadata must be strict UTF-8 JSON') from error


def load_profiles(root: Path = ROOT) -> list[dict]:
    path = Path(root) / 'config/oplus-kernel-profiles.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
        raise CatalogError('Missing or oversized reviewed OPlus source catalog')
    document = strict_json(path.read_bytes())
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'profiles'}
            or type(document['schema_version']) is not int or document['schema_version'] != 1
            or not isinstance(document['profiles'], list) or not 1 <= len(document['profiles']) <= 128):
        raise CatalogError('Unsupported reviewed OPlus source catalog schema')
    profiles, ids = [], set()
    for item in document['profiles']:
        if (not isinstance(item, dict) or set(item) != FIELDS
                or any(not isinstance(item[key], str) for key in FIELDS)):
            raise CatalogError('Invalid reviewed OPlus source profile fields')
        match = KERNEL.fullmatch(item['kernel'])
        series = '.'.join(item['kernel'].split('.')[:2])
        if (not PLATFORM.fullmatch(item['platform']) or match is None
                or item['id'] != item['platform'] + '-' + item['kernel']
                or item['id'] in ids or item['kernel_version'] != series
                or item['sub_level'] != match[2] or item['android_version'] != ANDROID.get(series)
                or item['kernel_repo'] not in OFFICIAL_REPOSITORIES
                or not item['kernel_repo'].endswith('_' + item['platform'])
                or item['recipe_repository'] not in RECIPE_REPOSITORIES
                or item['recipe_repository'] != RECIPE_FOR_SERIES.get(series)
                or not SHA.fullmatch(item['recipe_ref'])
                or not SHA.fullmatch(item['verified_kernel_ref'])
                or not SHA256.fullmatch(item['recipe_sha256'])):
            raise CatalogError('OPlus profile identity is unsupported, duplicated or inconsistent')
        branch = item['kernel_branch']
        if (not 1 <= len(branch) <= 255 or not re.fullmatch(r'[A-Za-z0-9_./-]+', branch)
                or any(part in ('', '.', '..') for part in branch.split('/'))
                or not branch.startswith(('oneplus/' + item['platform'] + '_',
                                          'oppo/' + item['platform'] + '_'))):
            raise CatalogError('Invalid reviewed official OPlus source branch')
        workflow = item['recipe_workflow']
        if not re.fullmatch(r'\.github/workflows/fastbuild_' + re.escape(item['kernel'])
                            + r'(?:_mtk)?\.yml', workflow):
            raise CatalogError('Only reviewed matching OKI build recipes are supported')
        for field, maximum in (('system', 512), ('compatibility', 1024)):
            if (not 1 <= len(item[field]) <= maximum
                    or any(ord(char) < 32 or ord(char) == 127 for char in item[field])):
                raise CatalogError('Invalid OPlus system or compatibility description')
        ids.add(item['id'])
        profiles.append(dict(item))
    return profiles


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OfficialReader:
    """Read public official branches and exact-ref Makefiles without redirects."""
    def __init__(self, token: str | None = None, opener=None):
        self.token = token if token is not None else (os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN', ''))
        if not isinstance(self.token, str) or any(ord(char) < 33 or ord(char) == 127 for char in self.token):
            raise CatalogError('Invalid GitHub source-read token')
        self.opener = opener or build_opener(NoRedirect())

    def get(self, resource: str):
        if (not isinstance(resource, str) or any(char in resource for char in '\r\n#')
                or not any(resource.startswith('/repos/' + repo + '/') for repo in OFFICIAL_REPOSITORIES)):
            raise CatalogError('Official OPlus source reader refuses an unreviewed destination')
        suffix = next(resource[len('/repos/' + repo + '/'):]
                      for repo in OFFICIAL_REPOSITORIES if resource.startswith('/repos/' + repo + '/'))
        if not (re.fullmatch(r'branches/[A-Za-z0-9_.%/-]+', suffix)
                or re.fullmatch(r'contents/Makefile\?ref=[0-9a-f]{40}', suffix)):
            raise CatalogError('Only official branch identity and immutable Makefile reads are permitted')
        request = Request('https://api.github.com' + resource,
                          headers={'Accept': 'application/vnd.github+json',
                                   'User-Agent': 'gki-oplus-official-catalog',
                                   'X-GitHub-Api-Version': '2022-11-28'})
        if self.token:
            request.add_header('Authorization', 'Bearer ' + self.token)
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=30) as response:
                    if response.status != 200 or response.geturl() != request.full_url:
                        raise CatalogError('Official OPlus source response changed destination or status')
                    raw = response.read(MAX_JSON + 1)
                if len(raw) > MAX_JSON:
                    raise CatalogError('Official OPlus source response exceeds its size limit')
                return strict_json(raw)
            except HTTPError as error:
                status = error.code
                error.close()
                if status not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                    raise CatalogError(f'Official OPlus source read failed (HTTP {status})') from None
            except (OSError, URLError, TimeoutError):
                if attempt == 2:
                    raise CatalogError('Official OPlus source transport failed after three attempts') from None
            time.sleep(attempt + 1)
        raise CatalogError('Official OPlus source read did not complete')


def kernel_version(document: dict) -> str:
    if (not isinstance(document, dict) or document.get('type') != 'file'
            or document.get('path') != 'Makefile' or document.get('encoding') != 'base64'
            or not isinstance(document.get('content'), str)
            or type(document.get('size')) is not int or not 1 <= document['size'] <= 2 * 1024 * 1024
            or not isinstance(document.get('sha'), str) or not SHA.fullmatch(document['sha'])):
        raise CatalogError('Official source did not return its exact Makefile identity')
    try:
        data = base64.b64decode(''.join(document['content'].split()), validate=True)
        text = data.decode('utf-8')
    except (ValueError, UnicodeError) as error:
        raise CatalogError('Official Makefile data is not valid base64 UTF-8') from error
    digest = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
    if len(data) != document['size'] or digest != document['sha']:
        raise CatalogError('Official Makefile bytes differ from their Git blob identity')
    numbers = []
    for name in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL'):
        values = re.findall(r'^' + name + r'\s*=\s*(0|[1-9][0-9]*)\s*$', text, re.M)
        if len(values) != 1:
            raise CatalogError('Official Makefile has an ambiguous kernel version')
        numbers.append(values[0])
    return '.'.join(numbers)


def resolve_profile(profile: dict, reader) -> dict:
    repository, branch = profile['kernel_repo'], profile['kernel_branch']
    identity = reader.get('/repos/' + repository + '/branches/' + quote(branch, safe=''))
    commit = identity.get('commit') if isinstance(identity, dict) else None
    reference = commit.get('sha') if isinstance(commit, dict) else None
    metadata = commit.get('commit') if isinstance(commit, dict) else None
    committer = metadata.get('committer') if isinstance(metadata, dict) else None
    date = committer.get('date') if isinstance(committer, dict) else None
    if (not isinstance(identity, dict) or identity.get('name') != branch
            or not isinstance(reference, str) or not SHA.fullmatch(reference)
            or not isinstance(date, str) or len(date) > 64):
        raise CatalogError('Official branch returned invalid immutable commit metadata')
    try:
        parsed = datetime.fromisoformat(date.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
    except ValueError as error:
        raise CatalogError('Official source commit has an invalid timestamp') from error
    document = reader.get('/repos/' + repository + '/contents/Makefile?ref=' + reference)
    actual = kernel_version(document)
    if actual != profile['kernel']:
        raise CatalogError('Official source version changed for ' + profile['id'] + ': expected '
                           + profile['kernel'] + ', found ' + actual
                           + '; review the matching build recipe before compiling')
    row = {key: value for key, value in profile.items() if key != 'verified_kernel_ref'}
    row.update(kernel_ref=reference, source_committed_at=date)
    return row


def order(row: dict):
    return tuple(map(int, row['kernel'].split('.'))), row['platform'], row['id']


def discover_matrix(root: Path = ROOT, reader=None) -> dict:
    profiles = load_profiles(root)
    reader = reader or OfficialReader()
    selected = {}
    for profile in profiles:
        row = resolve_profile(profile, reader)
        key = row['platform'], row['kernel']
        previous = selected.get(key)
        if previous is None or (datetime.fromisoformat(row['source_committed_at'].replace('Z', '+00:00')),
                                row['kernel_ref']) > (datetime.fromisoformat(previous['source_committed_at'].replace('Z', '+00:00')),
                                                     previous['kernel_ref']):
            selected[key] = row
    return {'include': sorted(selected.values(), key=order)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--github-output', action='store_true')
    parser.add_argument('--summary', action='store_true')
    args = parser.parse_args(argv)
    try:
        matrix = discover_matrix(args.root)
        encoded = json.dumps(matrix, ensure_ascii=True, separators=(',', ':'))
        if args.github_output:
            with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as output:
                output.write('matrix=' + encoded + '\ncount=' + str(len(matrix['include'])) + '\n')
        else:
            print(encoded)
        if args.summary:
            with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8', newline='\n') as summary:
                summary.write('## 欧加真官方源码构建范围\n\n')
                summary.write('仅构建下列已审核平台与完整内核版本；同版本不代表其他机型可通刷。\n\n')
                summary.write('| 平台 | 内核 | 官方源码 | 适用系统依据 |\n|---|---|---|---|\n')
                for row in matrix['include']:
                    summary.write('| ' + row['platform'] + ' | ' + row['kernel'] + ' | '
                                  + row['kernel_repo'] + ' @ `' + row['kernel_ref'][:12] + '` | '
                                  + row['system'].replace('|', '\\|') + ' |\n')
    except (CatalogError, OSError, KeyError, UnicodeError) as error:
        print('Official OPlus catalog stopped: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
