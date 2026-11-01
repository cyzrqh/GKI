#!/usr/bin/env python3
"""Record official SukiSU/BakaSU/SUSFS candidates without changing build pins."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from pin_build_dependencies import CompatibilityError, load_profile
from resolve_build_sources import (NoRedirect as GitHubNoRedirect, SourceResolutionError,
                                   github_commit, github_json, validate_ref)

ROOT = Path(__file__).resolve().parents[1]
SUKISU = 'SukiSU-Ultra/SukiSU-Ultra'
BAKASU = 'Baka-SU/BakaSU'
from manager_variants import ADDITIONAL_MANAGERS, manager_profile
ADDITIONAL_TRACKS = {variant.lower(): variant for variant in ADDITIONAL_MANAGERS}
SUSFS = 'simonpunk/susfs4ksu'
TARGETS = ('android12-5.10', 'android13-5.15', 'android14-6.1', 'android15-6.6', 'android16-6.12')
CANDIDATE_PATH = 'data/upstream-candidates.json'
TITLE = 'chore: review official SukiSU, BakaSU and SUSFS updates'
BRANCH = 'automation/official-updates'
SHA = re.compile(r'[0-9a-fA-F]{40}\Z')
LOOKUP_ATTEMPTS = 3
RELEASE_PAGE_SIZE = 20
MAX_RELEASE_PAGES = 5
MAX_RELEASE_BYTES = 1024 * 1024


class UpdateError(RuntimeError):
    """Stop candidate publication when the official snapshot cannot be verified."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise UpdateError('Official SUSFS API redirects are not permitted')


def github_lookup(label: str, function, *arguments, **keywords):
    """Retry bounded public lookups, without logging remote responses or tokens."""
    transient = {'Official GitHub source lookup failed', 'GitHub source lookup failed',
                 'GitHub source lookup returned an unsuccessful status'}
    for attempt in range(LOOKUP_ATTEMPTS):
        try:
            return function(*arguments, **keywords)
        except SourceResolutionError as error:
            # Only these constant messages come from the shared HTTP client.
            # Ref, redirect, size and shape failures cannot be fixed by retries.
            if str(error) not in transient:
                category = ('response exceeds size limit' if str(error) == 'GitHub source lookup response is too large'
                            else 'invalid or unexpected GitHub metadata')
                raise UpdateError(f'{label}: {category}') from None
            if attempt + 1 == LOOKUP_ATTEMPTS:
                raise UpdateError(f'{label}: GitHub network/API lookup failed after {LOOKUP_ATTEMPTS} attempts') from None
            time.sleep(attempt + 1)


def gitlab_head(target: str) -> str:
    if target not in TARGETS:
        raise UpdateError('Unknown official SUSFS target')
    branch = 'gki-' + target
    url = ('https://gitlab.com/api/v4/projects/' + quote(SUSFS, safe='')
           + '/repository/branches/' + quote(branch, safe=''))
    # GitLab is public. Never forward GH_TOKEN/GITHUB_TOKEN to this host.
    request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'gki-official-updates'})
    label = 'SUSFS ' + branch
    for attempt in range(LOOKUP_ATTEMPTS):
        try:
            with build_opener(NoRedirect()).open(request, timeout=60) as response:
                if response.geturl() != url or urlsplit(response.geturl()).netloc != 'gitlab.com':
                    raise UpdateError(label + ': official API returned an unexpected URL')
                if response.status != 200:
                    raise UpdateError(label + ': official API returned an unsuccessful status')
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise UpdateError(label + ': official API response is too large')
                document = json.loads(raw)
            break
        except HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504):
                raise UpdateError(f'{label}: official API failed (HTTP {error.code})') from None
            if attempt + 1 == LOOKUP_ATTEMPTS:
                raise UpdateError(f'{label}: official API failed (HTTP {error.code}) after {LOOKUP_ATTEMPTS} attempts') from None
        except (URLError, OSError):
            if attempt + 1 == LOOKUP_ATTEMPTS:
                raise UpdateError(f'{label}: network lookup failed after {LOOKUP_ATTEMPTS} attempts') from None
        except (ValueError, UnicodeError):
            raise UpdateError(label + ': official API returned invalid JSON') from None
        time.sleep(attempt + 1)
    commit = document.get('commit') if isinstance(document, dict) else None
    if (not isinstance(commit, dict) or not isinstance(commit.get('id'), str)
            or not SHA.fullmatch(commit['id']) or document.get('name', branch) != branch):
        raise UpdateError(label + ': official branch did not return its full commit SHA')
    return commit['id'].lower()


def baseline(root: Path) -> dict:
    profile = load_profile(root)
    source = profile.get('source_ref', profile['ksu_commits'][0])
    if not isinstance(source, str) or not SHA.fullmatch(source):
        raise UpdateError('Compatibility source_ref must be a full commit SHA')
    if any(target not in profile['pins'] for target in TARGETS):
        raise UpdateError('The compatibility profile must contain all five official SUSFS targets')
    return {'sukisu': source.lower(), 'susfs_version': profile['version'],
            'susfs': {target: profile['pins'][target].lower() for target in TARGETS}}


def bakasu_release_page(page: int) -> list:
    """Read only the fixed official release list, with bounded public metadata."""
    if type(page) is not int or not 1 <= page <= MAX_RELEASE_PAGES:
        raise UpdateError('BakaSU release pagination exceeds its bound')
    url = f'https://api.github.com/repos/{BAKASU}/releases?per_page={RELEASE_PAGE_SIZE}&page={page}'
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'gki-official-updates'}
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        if any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise UpdateError('Invalid GitHub authentication token')
        headers['Authorization'] = 'Bearer ' + token
    try:
        with build_opener(GitHubNoRedirect()).open(Request(url, headers=headers), timeout=60) as response:
            if response.geturl() != url:
                raise UpdateError('BakaSU releases: official API returned an unexpected URL')
            if response.status != 200:
                raise SourceResolutionError('GitHub source lookup returned an unsuccessful status')
            raw = response.read(MAX_RELEASE_BYTES + 1)
        if len(raw) > MAX_RELEASE_BYTES:
            raise UpdateError('BakaSU releases: official API response is too large')
        document = json.loads(raw)
    except HTTPError as error:
        if error.code not in (408, 429, 500, 502, 503, 504):
            raise UpdateError(f'BakaSU releases: official API failed (HTTP {error.code})') from None
        raise SourceResolutionError('Official GitHub source lookup failed') from None
    except (URLError, OSError):
        raise SourceResolutionError('GitHub source lookup failed') from None
    except (ValueError, UnicodeError):
        raise UpdateError('BakaSU releases: official API returned invalid JSON') from None
    if not isinstance(document, list) or len(document) > RELEASE_PAGE_SIZE:
        raise UpdateError('BakaSU releases: invalid or oversized release page')
    return document


def fetch_bakasu_snapshot() -> dict:
    repository = github_lookup('BakaSU repository', github_json, BAKASU)
    branch = repository.get('default_branch') if isinstance(repository, dict) else None
    if (not isinstance(branch, str) or 'full_name' in repository
            and (not isinstance(repository['full_name'], str) or repository['full_name'].casefold() != BAKASU.casefold())):
        raise UpdateError('Official BakaSU repository did not return its default branch identity')
    validate_ref(branch)
    head = github_lookup('BakaSU default branch', github_commit, BAKASU, branch)
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise UpdateError('Invalid official BakaSU head SHA')
    formal_document = github_lookup('BakaSU latest formal release', github_json, BAKASU, '/releases/latest', missing=True)
    formal = None
    if formal_document is not None:
        if (not isinstance(formal_document, dict) or formal_document.get('draft') is not False or formal_document.get('prerelease') is not False
                or not isinstance(formal_document.get('tag_name'), str)):
            raise UpdateError('BakaSU formal release must be published and not a prerelease')
        tag = validate_ref(formal_document['tag_name'])
        commit = github_lookup('BakaSU formal release commit', github_commit, BAKASU, tag)
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise UpdateError('Invalid official BakaSU formal release commit')
        formal = {'tag': tag, 'commit': commit.lower()}
    candidates, ids, tags = [], set(), set()
    for page in range(1, MAX_RELEASE_PAGES + 1):
        batch = github_lookup('BakaSU release list', bakasu_release_page, page)
        for release in batch:
            if (not isinstance(release, dict) or type(release.get('id')) is not int or release['id'] < 1
                    or release['id'] in ids or type(release.get('draft')) is not bool
                    or type(release.get('prerelease')) is not bool or not isinstance(release.get('tag_name'), str)):
                raise UpdateError('BakaSU releases: invalid flags or duplicate release identity')
            tag = validate_ref(release['tag_name'])
            if tag in tags:
                raise UpdateError('BakaSU releases: duplicate release tag during pagination')
            ids.add(release['id'])
            tags.add(tag)
            if release['draft']:
                continue
            published = release.get('published_at')
            try:
                instant = (datetime.fromisoformat(published.replace('Z', '+00:00'))
                           if isinstance(published, str) and len(published) <= 64 else None)
            except ValueError:
                instant = None
            if instant is None or instant.tzinfo is None or instant.utcoffset() != timezone.utc.utcoffset(instant):
                raise UpdateError('BakaSU releases: published release lacks a valid UTC timestamp')
            if release['prerelease']:
                candidates.append((instant, release['id'], tag))
        if len(batch) < RELEASE_PAGE_SIZE:
            break
    else:
        raise UpdateError('BakaSU releases: pagination did not finish within its bound')
    prerelease = None
    if candidates:
        tag = max(candidates)[2]
        commit = github_lookup('BakaSU prerelease commit', github_commit, BAKASU, tag)
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise UpdateError('Invalid official BakaSU prerelease commit')
        prerelease = {'tag': tag, 'commit': commit.lower()}
    return {'branch': branch, 'head': head.lower(), 'release': formal, 'prerelease': prerelease}


def fetch_additional_snapshot(variant: str) -> dict:
    repository = manager_profile(variant)['repository']
    document = github_lookup(variant + ' repository', github_json, repository)
    branch = document.get('default_branch')
    validate_ref(branch)
    head = github_lookup(variant + ' default branch', github_commit, repository, branch)
    release = github_lookup(variant + ' formal release', github_json, repository, '/releases/latest', missing=True)
    formal = None
    if release is not None:
        if release.get('draft') is not False or release.get('prerelease') is not False:
            raise UpdateError('Official manager formal release must be published, not a prerelease')
        tag = release.get('tag_name')
        validate_ref(tag)
        formal = {'tag': tag, 'commit': github_lookup(variant + ' release commit', github_commit, repository, tag)}
    return {'branch': branch, 'head': head, 'release': formal}


def fetch_snapshot(current: dict) -> dict:
    repository = github_lookup('SukiSU repository', github_json, SUKISU)
    branch = repository.get('default_branch')
    if not isinstance(branch, str):
        raise UpdateError('Official SukiSU repository did not return a default branch')
    validate_ref(branch)
    head = github_lookup('SukiSU default branch', github_commit, SUKISU, branch)
    release_document = github_lookup('SukiSU latest release', github_json, SUKISU, '/releases/latest', missing=True)
    release = None
    if release_document is not None:
        tag = release_document.get('tag_name')
        if not isinstance(tag, str):
            raise UpdateError('Official SukiSU release did not return a tag')
        validate_ref(tag)
        commit = github_lookup('SukiSU release commit', github_commit, SUKISU, tag)
        newer = False
        if commit != current['sukisu']:
            # Only ancestry is needed. GitHub includes every changed file and
            # patch on page 1 even with per_page=1; page 2 omits that potentially
            # multi-megabyte diff while retaining the comparison's status.
            comparison = github_lookup('SukiSU release ancestry', github_json, SUKISU,
                                       '/compare/' + current['sukisu'] + '...' + commit + '?per_page=1&page=2')
            base = comparison.get('base_commit')
            if (not isinstance(base, dict) or base.get('sha') != current['sukisu']
                    or comparison.get('status') not in ('ahead', 'behind', 'identical', 'diverged')):
                raise UpdateError('SukiSU release ancestry: invalid comparison baseline or ancestry status')
            newer = comparison['status'] in ('ahead', 'diverged')
        release = {'tag': tag, 'commit': commit, 'newer_than_baseline': newer}
    development = {'branch': 'dev', 'head': head} if branch == 'dev' else None
    if branch != 'dev':
        document = github_lookup('SukiSU development branch', github_json, SUKISU, '/branches/dev', missing=True)
        if document is not None:
            commit = document.get('commit') if isinstance(document, dict) else None
            if (not isinstance(commit, dict) or document.get('name') != 'dev'
                    or not isinstance(commit.get('sha'), str) or not SHA.fullmatch(commit['sha'])):
                raise UpdateError('Official SukiSU development branch did not return its exact branch and full commit SHA')
            development = {'branch': 'dev', 'head': commit['sha'].lower()}
    return validate_snapshot({'schema_version': 1,
                              'sukisu': {'branch': branch, 'head': head, 'release': release, 'development': development},
                              'bakasu': fetch_bakasu_snapshot(),
                              **{key: fetch_additional_snapshot(variant) for key, variant in ADDITIONAL_TRACKS.items()},
                              'susfs': {target: gitlab_head(target) for target in TARGETS}})


def validate_snapshot(value: object) -> dict:
    if (not isinstance(value, dict) or not {'schema_version', 'sukisu', 'susfs'} <= set(value)
            or set(value) - {'schema_version', 'sukisu', 'susfs', 'bakasu'} - set(ADDITIONAL_TRACKS)
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise UpdateError('Unsupported official candidate snapshot schema')
    suki, susfs = value['sukisu'], value['susfs']
    if not isinstance(suki, dict) or set(suki) not in ({'branch', 'head', 'release'},
                                                      {'branch', 'head', 'release', 'development'}):
        raise UpdateError('Invalid official SukiSU snapshot')
    validate_ref(suki['branch'])
    if not isinstance(suki['head'], str) or not SHA.fullmatch(suki['head']):
        raise UpdateError('Invalid official SukiSU head SHA')
    release = suki['release']
    if release is not None:
        if (not isinstance(release, dict) or set(release) != {'tag', 'commit', 'newer_than_baseline'}
                or not isinstance(release['commit'], str) or not SHA.fullmatch(release['commit'])
                or type(release['newer_than_baseline']) is not bool):
            raise UpdateError('Invalid official SukiSU release snapshot')
        validate_ref(release['tag'])
    development = suki.get('development')
    if development is not None:
        if (not isinstance(development, dict) or set(development) != {'branch', 'head'}
                or development.get('branch') != 'dev' or not isinstance(development.get('head'), str)
                or not SHA.fullmatch(development['head'])):
            raise UpdateError('Invalid official SukiSU development snapshot')
    if (not isinstance(susfs, dict) or set(susfs) != set(TARGETS)
            or any(not isinstance(commit, str) or not SHA.fullmatch(commit) for commit in susfs.values())):
        raise UpdateError('Invalid official SUSFS snapshot')
    result = {'schema_version': 1,
              'sukisu': {'branch': suki['branch'], 'head': suki['head'].lower(),
                         'release': {**release, 'commit': release['commit'].lower()} if release else None},
              'susfs': {target: susfs[target].lower() for target in TARGETS}}
    # Existing acknowledgements remain byte-for-byte compatible. Only new
    # probes carry the additional development observation and marker identity.
    if 'development' in suki:
        result['sukisu']['development'] = ({'branch': 'dev', 'head': development['head'].lower()}
                                           if development else None)
    if 'bakasu' in value:
        baka = value['bakasu']
        if (not isinstance(baka, dict) or set(baka) != {'branch', 'head', 'release', 'prerelease'}
                or not isinstance(baka['branch'], str) or not isinstance(baka['head'], str)
                or not SHA.fullmatch(baka['head'])):
            raise UpdateError('Invalid official BakaSU snapshot')
        validate_ref(baka['branch'])
        result['bakasu'] = {'branch': baka['branch'], 'head': baka['head'].lower()}
        for key in ('release', 'prerelease'):
            entry = baka[key]
            if entry is not None:
                if (not isinstance(entry, dict) or set(entry) != {'tag', 'commit'}
                        or not isinstance(entry.get('tag'), str) or not isinstance(entry.get('commit'), str)
                        or not SHA.fullmatch(entry['commit'])):
                    raise UpdateError('Invalid official BakaSU ' + key + ' snapshot')
                validate_ref(entry['tag'])
            result['bakasu'][key] = {'tag': entry['tag'], 'commit': entry['commit'].lower()} if entry is not None else None
    for key, variant in ADDITIONAL_TRACKS.items():
        if key not in value:
            continue
        item = value[key]
        if (not isinstance(item, dict) or set(item) != {'branch', 'head', 'release'}
                or not isinstance(item['head'], str) or not SHA.fullmatch(item['head'])):
            raise UpdateError('Invalid official ' + variant + ' snapshot')
        validate_ref(item['branch'])
        release = item['release']
        if release is not None:
            if (not isinstance(release, dict) or set(release) != {'tag', 'commit'}
                    or not isinstance(release['commit'], str) or not SHA.fullmatch(release['commit'])):
                raise UpdateError('Invalid official ' + variant + ' formal release')
            validate_ref(release['tag'])
        result[key] = {'branch': item['branch'], 'head': item['head'].lower(),
                       'release': {'tag': release['tag'], 'commit': release['commit'].lower()} if release else None}
    return result


def head_state(snapshot: dict) -> dict:
    release = snapshot['sukisu']['release']
    result = {'sukisu_branch': snapshot['sukisu']['branch'], 'sukisu_head': snapshot['sukisu']['head'],
              'sukisu_release': {'tag': release['tag'], 'commit': release['commit']} if release else None,
              'susfs': snapshot['susfs']}
    if 'development' in snapshot['sukisu']:
        result['sukisu_development'] = snapshot['sukisu']['development']
    if 'bakasu' in snapshot:
        result['bakasu'] = snapshot['bakasu']
    for key in ADDITIONAL_TRACKS:
        if key in snapshot:
            result[key] = snapshot[key]
    return result


def heads_marker(snapshot: dict) -> str:
    validated = validate_snapshot(snapshot)
    encoded = json.dumps(head_state(validated), sort_keys=True, separators=(',', ':')).encode()
    return '<!-- gki-official-heads:sha256=' + hashlib.sha256(encoded).hexdigest() + ' -->'


def candidate_path(root: Path) -> Path:
    path = root / CANDIDATE_PATH
    if not path.resolve().is_relative_to(root.resolve()):
        raise UpdateError('Official candidate path must stay inside the repository')
    return path


def acknowledged(root: Path) -> dict | None:
    path = candidate_path(root)
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise UpdateError('Cannot read the official candidate acknowledgement') from None
    if not isinstance(document, dict) or document.get('schema_version') != 1:
        raise UpdateError('Unsupported official candidate acknowledgement schema')
    snapshot = validate_snapshot(document.get('sources'))
    state = head_state(snapshot)
    if document.get('acknowledged_heads') != state:
        raise UpdateError('Official candidate acknowledgement does not match its source snapshot')
    return state


def changes(current: dict, snapshot: dict) -> list[dict[str, str]]:
    selected = []
    suki = snapshot['sukisu']
    if suki['head'] != current['sukisu']:
        selected.append({'source': 'SukiSU', 'ref': suki['branch'], 'current': current['sukisu'], 'candidate': suki['head'],
                         'url': f'https://github.com/{SUKISU}/commit/' + suki['head']})
    release = suki['release']
    if release and release['newer_than_baseline'] and release['commit'] != current['sukisu']:
        selected.append({'source': 'SukiSU release', 'ref': release['tag'], 'current': current['sukisu'],
                         'candidate': release['commit'], 'url': f'https://github.com/{SUKISU}/releases/tag/' + quote(release['tag'], safe='')})
    development = suki.get('development')
    if development and development['head'] not in (current['sukisu'], suki['head']):
        selected.append({'source': 'SukiSU development', 'ref': 'dev', 'current': current['sukisu'],
                         'candidate': development['head'],
                         'url': f'https://github.com/{SUKISU}/commit/' + development['head']})
    if 'bakasu' in snapshot:
        baka = snapshot['bakasu']
        selected.append({'source': 'BakaSU', 'ref': baka['branch'], 'current': '', 'candidate': baka['head'],
                         'url': f'https://github.com/{BAKASU}/commit/' + baka['head']})
        for key, label in (('release', 'BakaSU release'), ('prerelease', 'BakaSU prerelease')):
            entry = baka[key]
            if entry is not None:
                selected.append({'source': label, 'ref': entry['tag'], 'current': '', 'candidate': entry['commit'],
                                 'url': f'https://github.com/{BAKASU}/releases/tag/' + quote(entry['tag'], safe='')})
    for target in TARGETS:
        if snapshot['susfs'][target] != current['susfs'][target]:
            selected.append({'source': 'SUSFS', 'ref': 'gki-' + target, 'current': current['susfs'][target],
                             'candidate': snapshot['susfs'][target], 'url': f'https://gitlab.com/{SUSFS}/-/commit/' + snapshot['susfs'][target]})
    for key, variant in ADDITIONAL_TRACKS.items():
        if key not in snapshot:
            continue
        item = snapshot[key]
        repository = manager_profile(variant)['repository']
        selected.append({'source': variant, 'ref': item['branch'], 'current': '', 'candidate': item['head'],
                         'url': f'https://github.com/{repository}/commit/' + item['head']})
        release = item['release']
        if release:
            selected.append({'source': variant + ' release', 'ref': release['tag'], 'current': '',
                             'candidate': release['commit'], 'url': f'https://github.com/{repository}/releases/tag/' + quote(release['tag'], safe='')})
    return selected


def report_text(current: dict, snapshot: dict, updates: list[dict]) -> str:
    lines = ['<!-- gki-official-candidates:v1 -->', heads_marker(snapshot),
             '检测到官方源码的新候选版本。本 PR 记录更新提示，尚未将候选版本设为构建版本。', '',
             f"当前验证基线：SukiSU `{current['sukisu']}`，SUSFS `{current['susfs_version']}`。", '',
             '| 官方源码 | 分支/版本 | 当前验证提交 | 候选提交 |', '| --- | --- | --- | --- |']
    for item in updates:
        old = item['current'][:12] or '未设固定验证基线'
        lines.append(f"| {item['source']} | `{item['ref']}` | `{old}` | [`{item['candidate'][:12]}`]({item['url']}) |")
    lines.extend(['', '后续需要核对内核 API、SUSFS hook/ABI、管理器包名及签名，再执行对应内核构建验证。',
                  '`build-compatibility.json` 和已验证的构建提交保持不变；本次检测未编译候选内核。',
                  '合并本 PR 仅确认已收到这些官方更新提示，后续出现不同提交时会再次提示。', ''])
    return '\n'.join(lines)


def write_report(content: str, directory: Path | None) -> Path:
    parent = (directory or Path(os.environ.get('RUNNER_TEMP', tempfile.gettempdir()))).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / ('gki-official-update-' + uuid.uuid4().hex + '.md')
    path.write_text(content, encoding='utf-8', newline='\n')
    return path


def prepare(root: Path, current: dict, snapshot: dict, updates: list[dict], report_dir: Path | None) -> Path:
    document = {'schema_version': 1, 'status': 'candidates-require-validation', 'baseline': current,
                'sources': snapshot, 'acknowledged_heads': head_state(snapshot), 'changes': updates}
    body = write_report(report_text(current, snapshot, updates), report_dir)
    path = candidate_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name('.upstream-candidates-' + uuid.uuid4().hex + '.tmp')
    try:
        with staged.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + '\n')
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    return body


def write_outputs(values: dict[str, str]) -> None:
    if not os.environ.get('GITHUB_OUTPUT'):
        return
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as stream:
        for key, value in values.items():
            if '\r' in value or '\n' in value:
                raise UpdateError('Official update output must be a single line')
            stream.write(f'{key}={value}\n')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check-only', action='store_true')
    mode.add_argument('--prepare', action='store_true')
    parser.add_argument('--repo-root', '--root', dest='root', type=Path, default=ROOT)
    parser.add_argument('--report-dir', type=Path)
    parser.add_argument('--candidates-json', help='Exact candidates_json snapshot returned by --check-only')
    args = parser.parse_args(argv)
    if args.candidates_json is not None and not args.prepare:
        parser.error('--candidates-json requires --prepare')
    write_outputs({'has_updates': 'false', 'body_path': ''})
    stage = 'compatibility metadata'
    try:
        root = args.root.resolve()
        current = baseline(root)
        stage = 'previous official snapshot'
        previous = acknowledged(root)
        stage = 'official source lookup'
        snapshot = validate_snapshot(json.loads(args.candidates_json)) if args.candidates_json is not None else fetch_snapshot(current)
        updates = changes(current, snapshot)
        needed = bool(updates) and previous != head_state(snapshot)
        values = {'has_updates': str(needed).lower(), 'title': TITLE, 'branch': BRANCH,
                  'candidates_json': json.dumps(snapshot, sort_keys=True, separators=(',', ':')),
                  'heads_marker': heads_marker(snapshot)}
        if args.prepare and needed:
            stage = 'candidate publication'
            body = prepare(root, current, snapshot, updates, args.report_dir)
            values.update(body_path=str(body), changed_files_json=json.dumps([CANDIDATE_PATH]))
        write_outputs(values)
        print('Official source candidates require compatibility review.' if needed else 'No unacknowledged official source updates.')
        return 0
    except UpdateError as error:
        # UpdateError messages contain only local labels/validation constants;
        # never forward the response body, headers, URLs or low-level exception.
        print(f'::error::Official update detection failed ({stage}): {error}', file=sys.stderr)
        return 1
    except (CompatibilityError, SourceResolutionError, OSError, ValueError, UnicodeError) as error:
        categories = {CompatibilityError: 'compatibility profile is invalid',
                      SourceResolutionError: 'official ref metadata is invalid',
                      OSError: 'local file access failed', ValueError: 'JSON or snapshot metadata is invalid',
                      UnicodeError: 'response or metadata encoding is invalid'}
        category = next(reason for kind, reason in categories.items() if isinstance(error, kind))
        print(f'::error::Official update detection failed ({stage}): {category}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
