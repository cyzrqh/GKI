#!/usr/bin/env python3
"""Allocate atomic r1.x release tags and bind releases to exact Actions runs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


SHA = re.compile(r'[0-9a-f]{40}\Z')
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z')
VERSION_TAG = re.compile(r'r1\.(0|[1-9][0-9]{0,8})\Z')
LEGACY_TAG = re.compile(r'gki-([1-9][0-9]*)-([1-9][0-9]*)\Z')
MARKER = re.compile(r'<!-- gki-release-run:v1 repository=([A-Za-z0-9][A-Za-z0-9_.-]{0,99}/'
                    r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}) run_id=([1-9][0-9]*) '
                    r'run_attempt=([1-9][0-9]*) head_sha=([0-9a-f]{40}) -->\Z')
MARKER_PREFIX = 'gki-release-run:'
MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
MAX_RELEASES = 10000
RELEASE_PAGE_SIZE = 20
TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


def context_identity(context: dict) -> dict:
    if (not isinstance(context, dict) or not isinstance(context.get('repository'), str)
            or not REPOSITORY.fullmatch(context['repository'])
            or type(context.get('run_id')) is not int or context['run_id'] < 1
            or type(context.get('run_attempt')) is not int or context['run_attempt'] < 1
            or not isinstance(context.get('head_sha'), str) or not SHA.fullmatch(context['head_sha'])):
        raise ValueError('Expected the exact repository/run/attempt/commit release identity')
    return {key: context[key] for key in ('run_id', 'run_attempt', 'head_sha', 'repository')}


def notes_marker(notes: str) -> dict | None:
    if not isinstance(notes, str) or len(notes.encode('utf-8')) > MAX_BODY:
        raise ValueError('Release notes are missing or exceed their bounded size')
    if MARKER_PREFIX not in notes:
        return None
    lines = [line for line in notes.splitlines() if MARKER_PREFIX in line]
    if len(lines) != 1 or notes.count(MARKER_PREFIX) != 1 or not (match := MARKER.fullmatch(lines[0])):
        raise ValueError('Release run marker is malformed, duplicated or not an exact standalone line')
    return context_identity({'repository': match[1], 'run_id': int(match[2]),
                             'run_attempt': int(match[3]), 'head_sha': match[4]})


def release_run_identity(release: dict) -> dict:
    """Decode explicit provenance; never infer an Actions run from a commit."""
    if not isinstance(release, dict) or not isinstance(release.get('tag_name'), str):
        raise ValueError('Release metadata is missing its tag identity')
    tag = release['tag_name']
    version, legacy = VERSION_TAG.fullmatch(tag), LEGACY_TAG.fullmatch(tag)
    if not version and not legacy:
        raise ValueError('Release does not have a supported build tag')
    body = release.get('body')
    if body is None:
        body = ''
    marker = notes_marker(body)
    if marker is not None:
        if release.get('target_commitish') != marker['head_sha']:
            raise ValueError('Release target commit differs from its explicit run marker')
        if legacy and (int(legacy[1]), int(legacy[2])) != (marker['run_id'], marker['run_attempt']):
            raise ValueError('Legacy release tag disagrees with its explicit run marker')
        return marker
    if version:
        raise ValueError('Versioned release requires its explicit exact Actions run marker')
    target = release.get('target_commitish')
    return {'run_id': int(legacy[1]), 'run_attempt': int(legacy[2]),
            'head_sha': target if isinstance(target, str) and SHA.fullmatch(target) else None,
            'repository': None}


def append_to_notes(notes: str, context: dict) -> str:
    context = context_identity(context)
    current = notes_marker(notes)
    if current is not None:
        if current != context:
            raise ValueError('Release notes already belong to a different run, attempt or commit')
        return notes
    marker = ('<!-- gki-release-run:v1 repository=' + context['repository']
              + ' run_id=' + str(context['run_id']) + ' run_attempt=' + str(context['run_attempt'])
              + ' head_sha=' + context['head_sha'] + ' -->')
    updated = notes.rstrip('\r\n') + '\n\n' + marker + '\n'
    notes_marker(updated)
    return updated


class TagCollision(ValueError):
    pass


class ReleaseAPIError(ValueError):
    def __init__(self, message: str, *, transient=False):
        super().__init__(message)
        self.transient = transient


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError('Authenticated release API redirects are refused')


class GitHub:
    """Bounded public metadata reads plus atomic lightweight tag creation."""
    def __init__(self, repository: str, token: str, opener=None):
        if (not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
                or not isinstance(token, str) or not token
                or any(ord(character) < 33 or ord(character) == 127 for character in token)):
            raise ValueError('A valid repository and GH_TOKEN are required for tag allocation')
        self.repository, self.token = repository, token
        self.opener = opener or build_opener(NoRedirect())

    def request(self, resource: str, *, method='GET', data=None):
        if not ((method == 'GET' and data is None and (
                resource == '/git/matching-refs/tags/r1.'
                or re.fullmatch(r'/releases\?per_page=20&page=[1-9][0-9]*', resource)))
                or (method == 'POST' and resource == '/git/refs' and isinstance(data, dict)
                    and set(data) == {'ref', 'sha'} and isinstance(data['ref'], str)
                    and VERSION_TAG.fullmatch(data['ref'].removeprefix('refs/tags/'))
                    and data['ref'].startswith('refs/tags/') and isinstance(data['sha'], str)
                    and SHA.fullmatch(data['sha']))):
            raise ValueError('Unsupported release tag API operation')
        encoded = None if data is None else json.dumps(data, separators=(',', ':')).encode('utf-8')
        request = Request('https://api.github.com/repos/' + self.repository + resource,
            method=method, data=encoded, headers={'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                'User-Agent': 'gki-release-tag-allocation', 'Content-Type': 'application/json'})
        for attempt in range(3):
            try:
                return self._read(request, method)
            except ReleaseAPIError as error:
                if not error.transient or attempt == 2:
                    raise ReleaseAPIError(str(error) + (' after three attempts' if attempt == 2 else '')) from None
                print('Release tag API temporarily unavailable; retrying bounded request', flush=True)
                time.sleep((2, 5)[attempt])
        raise AssertionError('Unreachable release API retry state')

    def _read(self, request, method):
        try:
            with self.opener.open(request, timeout=45) as response:
                if response.status != (201 if method == 'POST' else 200):
                    raise ReleaseAPIError('Release tag API ' + method + ' failed (HTTP '
                                          + str(response.status) + ')', transient=response.status in TRANSIENT_HTTP)
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError('Release tag API response exceeded its size limit')
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError('duplicate field')
                    result[key] = value
                return result
            return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except HTTPError as error:
            status = error.code
            error.close()
            if status == 422 and method == 'POST':
                raise TagCollision('The requested release tag was concurrently reserved') from None
            raise ReleaseAPIError('Release tag API ' + method + ' failed (HTTP ' + str(status) + ')',
                                  transient=status in TRANSIENT_HTTP) from None
        except (OSError, URLError):
            raise ReleaseAPIError('Release tag API ' + method + ' transport request failed', transient=True) from None
        except ReleaseAPIError:
            raise
        except (UnicodeError, ValueError):
            raise ValueError('Release tag API request failed or returned invalid bounded JSON') from None


def list_releases(api) -> list[dict]:
    releases, ids, tags = [], set(), set()
    for page in range(1, MAX_RELEASES // RELEASE_PAGE_SIZE + 2):
        batch = api.request('/releases?per_page=20&page=' + str(page))
        if not isinstance(batch, list) or len(batch) > RELEASE_PAGE_SIZE:
            raise ValueError('Release listing is invalid or incomplete')
        for release in batch:
            if (not isinstance(release, dict) or type(release.get('id')) is not int or release['id'] < 1
                    or type(release.get('draft')) is not bool or not isinstance(release.get('tag_name'), str)
                    or not 0 < len(release['tag_name']) <= 1024
                    or any(ord(character) < 32 for character in release['tag_name'])
                    or release['id'] in ids or release['tag_name'] in tags):
                raise ValueError('Release listing contains invalid or duplicate identities')
            ids.add(release['id'])
            tags.add(release['tag_name'])
            releases.append(release)
        if len(releases) > MAX_RELEASES:
            raise ValueError('Release listing exceeds its bounded record limit')
        if len(batch) < RELEASE_PAGE_SIZE:
            return releases
    raise ValueError('Release listing exceeds its bounded pagination limit')


def list_tag_refs(api) -> dict[str, str]:
    document = api.request('/git/matching-refs/tags/r1.')
    if not isinstance(document, list) or len(document) > MAX_RELEASES:
        raise ValueError('Release tag ref listing is invalid or exceeds its limit')
    refs = {}
    for item in document:
        if not isinstance(item, dict) or not isinstance(item.get('ref'), str):
            raise ValueError('Release tag ref listing contains an invalid identity')
        ref = item['ref']
        tag = ref.removeprefix('refs/tags/')
        if not ref.startswith('refs/tags/') or not VERSION_TAG.fullmatch(tag):
            continue
        target = item.get('object')
        if (not isinstance(target, dict) or not isinstance(target.get('sha'), str)
                or not SHA.fullmatch(target['sha']) or target.get('type') not in ('commit', 'tag')
                or tag in refs):
            raise ValueError('Release tag ref listing contains an invalid or duplicate target')
        refs[tag] = target['sha'] if target['type'] == 'commit' else ''
    return refs


def allocate(api, context: dict) -> dict:
    context = context_identity(context)
    for _attempt in range(5):
        releases, refs = list_releases(api), list_tag_refs(api)
        existing = None
        minors = [int(VERSION_TAG.fullmatch(tag)[1]) for tag in refs]
        for release in releases:
            tag = release['tag_name']
            if not VERSION_TAG.fullmatch(tag) and not LEGACY_TAG.fullmatch(tag):
                continue
            identity = release_run_identity(release)
            if VERSION_TAG.fullmatch(tag):
                minors.append(int(VERSION_TAG.fullmatch(tag)[1]))
            if (identity['run_id'], identity['run_attempt']) != (context['run_id'], context['run_attempt']):
                continue
            if identity != context:
                raise ValueError('An existing release for this run has a different repository or commit')
            if not release['draft']:
                raise ValueError('This exact Actions run and attempt has already published its release')
            if existing is not None or not VERSION_TAG.fullmatch(tag) or refs.get(tag) != context['head_sha']:
                raise ValueError('The existing draft is ambiguous or lacks its exact reserved commit tag')
            existing = tag
        if existing:
            return {'release_tag': existing, 'release_exists': True}
        minor = max(minors, default=-1) + 1
        if minor > 999999999:
            raise ValueError('Release version sequence exceeds its supported limit')
        tag = 'r1.' + str(minor)
        try:
            result = api.request('/git/refs', method='POST', data={'ref': 'refs/tags/' + tag,
                                                                 'sha': context['head_sha']})
        except TagCollision:
            continue
        if (not isinstance(result, dict) or result.get('ref') != 'refs/tags/' + tag
                or not isinstance(result.get('object'), dict)
                or result['object'].get('type') != 'commit'
                or result['object'].get('sha') != context['head_sha']):
            raise ValueError('Reserved release tag did not confirm its exact selected commit')
        return {'release_tag': tag, 'release_exists': False}
    raise ValueError('Release tag could not be reserved after five concurrent allocation attempts')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('allocate',))
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--github-output', type=Path)
    arguments = parser.parse_args(argv)
    try:
        if (arguments.notes.is_symlink() or not arguments.notes.is_file()
                or arguments.notes.stat().st_size > MAX_BODY):
            raise ValueError('Release notes must be an existing bounded regular file')
        run_id, attempt = os.environ.get('GITHUB_RUN_ID', ''), os.environ.get('GITHUB_RUN_ATTEMPT', '')
        if not re.fullmatch(r'[1-9][0-9]{0,19}', run_id) or not re.fullmatch(r'[1-9][0-9]{0,9}', attempt):
            raise ValueError('Release Actions run and attempt IDs are missing or invalid')
        context = context_identity({'repository': os.environ.get('GITHUB_REPOSITORY', ''),
            'run_id': int(run_id), 'run_attempt': int(attempt),
            'head_sha': os.environ.get('GITHUB_SHA', '')})
        notes = append_to_notes(arguments.notes.read_text(encoding='utf-8'), context)
        output = arguments.github_output or (Path(os.environ['GITHUB_OUTPUT']) if os.environ.get('GITHUB_OUTPUT') else None)
        if output is not None and (output.is_symlink() or output.exists() and not output.is_file()):
            raise ValueError('GitHub output must be a regular file')
        result = allocate(GitHub(context['repository'], os.environ.get('GH_TOKEN', '')), context)
        arguments.notes.write_text(notes, encoding='utf-8', newline='\n')
        if output is not None:
            with output.open('a', encoding='utf-8', newline='\n') as stream:
                stream.write('release_tag=' + result['release_tag'] + '\nrelease_exists='
                             + str(result['release_exists']).lower() + '\n')
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, UnicodeError) as error:
        reason = (str(error) if isinstance(error, ReleaseAPIError) else
                  'invalid provenance, conflicting draft, already published run or invalid local notes/output')
        print('Release tag allocation stopped: ' + reason + '; no existing release was modified', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
