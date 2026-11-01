#!/usr/bin/env python3
"""Optional, bounded official commit titles for an already frozen sync delta."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from manager_variants import MANAGERS, manager_profile

SHA = re.compile(r'[0-9a-f]{40}\Z')
SUSFS = 'simonpunk/susfs4ksu'
MAX_REQUESTS = 12
MAX_COMMITS = 8
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 8


class DetailError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise DetailError('Official change detail redirects are not permitted')


def source_url(source: str) -> str:
    if source == 'SUSFS':
        return 'https://gitlab.com/' + SUSFS
    if source not in MANAGERS:
        raise DetailError('Unknown official source')
    return 'https://github.com/' + manager_profile(source)['repository']


def compare_url(source: str, before: str, after: str) -> str:
    if not SHA.fullmatch(before) or not SHA.fullmatch(after):
        raise DetailError('Official change details require frozen commits')
    return source_url(source) + ('/-/compare/' if source == 'SUSFS' else '/compare/') + before + '...' + after


def commit_url(source: str, commit: str) -> str:
    if not SHA.fullmatch(commit):
        raise DetailError('Invalid official commit')
    return source_url(source) + ('/-/commit/' if source == 'SUSFS' else '/commit/') + commit


def comparison_request(source: str, before: str, after: str) -> Request:
    compare_url(source, before, after)  # Validate source and both immutable refs.
    headers = {'Accept': 'application/json', 'User-Agent': 'gki-official-change-report'}
    if source == 'SUSFS':
        url = ('https://gitlab.com/api/v4/projects/' + quote(SUSFS, safe='')
               + '/repository/compare?' + urlencode({'from': before, 'to': after, 'straight': 'true'}))
    else:
        url = ('https://api.github.com/repos/' + manager_profile(source)['repository']
               + '/compare/' + before + '...' + after + '?per_page=8&page=1')
        token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if token:
            headers['Authorization'] = 'Bearer ' + token
    # GitHub credentials are never forwarded to the public GitLab API.
    return Request(url, headers=headers)


def request_json(request: Request) -> dict:
    with build_opener(NoRedirect()).open(request, timeout=TIMEOUT) as response:
        if response.status != 200 or response.geturl() != request.full_url:
            raise DetailError('Unexpected official change response')
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise DetailError('Official change response exceeds size bound')
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise DetailError('Invalid official change response')
    return document


def parse_comparison(source: str, before: str, after: str, document: dict) -> dict:
    if not isinstance(document, dict):
        raise DetailError('Invalid official comparison')
    commits = document.get('commits')
    if not isinstance(commits, list):
        raise DetailError('Missing official commits')
    if source == 'SUSFS':
        head = document.get('commit')
        if not isinstance(head, dict) or head.get('id') != after:
            raise DetailError('Official comparison differs from frozen head')
        partial = document.get('compare_timeout', False)
        if type(partial) is not bool or document.get('compare_same_ref', False) is True:
            raise DetailError('Invalid official comparison state')
        total, relation = (None if partial else len(commits)), 'direct'
    else:
        base = document.get('base_commit')
        total, relation = document.get('total_commits'), document.get('status')
        if (not isinstance(base, dict) or base.get('sha') != before
                or type(total) is not int or total < len(commits)
                or relation not in ('ahead', 'behind', 'diverged', 'identical')):
            raise DetailError('Invalid frozen official comparison')
        partial = False
    rows = []
    for item in commits[:MAX_COMMITS]:
        if not isinstance(item, dict):
            raise DetailError('Invalid official commit row')
        sha = item.get('id' if source == 'SUSFS' else 'sha')
        metadata = item if source == 'SUSFS' else item.get('commit')
        if not isinstance(sha, str) or not SHA.fullmatch(sha) or not isinstance(metadata, dict):
            raise DetailError('Invalid official commit identity')
        message = metadata.get('title' if source == 'SUSFS' else 'message')
        if not isinstance(message, str) or not message.strip():
            raise DetailError('Missing official commit title')
        title = ' '.join(message.strip().splitlines()[0].split())
        rows.append({'commit': sha, 'title': title[:180], 'title_truncated': len(title) > 180})
    return {'status': 'observed', 'relation': relation, 'total_commits': total,
            'partial': partial, 'commits': rows}


def observe_changes(changes: list[dict], *, fetch=None) -> list[dict]:
    """Deduplicate channel ranges; outages cannot block source synchronization."""
    fetch = fetch or request_json
    grouped = {}
    for change in changes:
        before, after = change['before'], change['after']
        if (change['kind'] != 'changed' or not before or not after
                or not before.get('commit') or not after.get('commit')
                or before['commit'] == after['commit']):
            continue
        key = change['source'], before['commit'], after['commit']
        if key not in grouped:
            grouped[key] = {'source': key[0], 'before': key[1], 'after': key[2], 'channels': []}
        grouped[key]['channels'].append(change['channel'])
    rows = list(grouped.values())

    def observe(row):
        try:
            document = fetch(comparison_request(row['source'], row['before'], row['after']))
            return {**row, **parse_comparison(row['source'], row['before'], row['after'], document)}
        except (OSError, ValueError, RuntimeError):
            # Do not include server bodies, URLs from responses, or credentials.
            return {**row, 'status': 'unavailable', 'commits': []}

    with ThreadPoolExecutor(max_workers=3) as executor:
        result = list(executor.map(observe, rows[:MAX_REQUESTS]))
    result.extend({**row, 'status': 'budget-exceeded', 'commits': []} for row in rows[MAX_REQUESTS:])
    return result
