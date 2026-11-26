"""Read SukiSU's official Forgejo source, linked by sukisu.org.

GitHub is an outdated archive. Never substitute it after a failed lookup, and
never send GitHub credentials to the independently hosted public source API.
"""
from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from resolve_build_sources import (NoRedirect, SourceResolutionError, SHA,
                                  SUKISU_OFFICIAL_ORIGIN, SUKISU_OFFICIAL_REPOSITORY)


API = SUKISU_OFFICIAL_ORIGIN + '/api/v1/repos/' + SUKISU_OFFICIAL_REPOSITORY
MAX_RESPONSE = 8 * 1024 * 1024


def _read(resource: str, *, missing: bool = False) -> dict | None:
    if resource and (not resource.startswith('/') or any(c in resource for c in '\r\n#')):
        raise SourceResolutionError('Invalid official SukiSU source resource')
    url = API + resource
    request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'gki-source-resolver'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=60) as response:
            if response.status != 200 or response.geturl() != url:
                raise SourceResolutionError('Official SukiSU source returned an unexpected URL or status')
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise SourceResolutionError('Official SukiSU source response is too large')
            document = json.loads(raw)
    except HTTPError as error:
        if missing and error.code == 404:
            return None
        raise SourceResolutionError('Official SukiSU source lookup failed (HTTP ' + str(error.code) + ')') from None
    except (URLError, OSError, ValueError) as error:
        raise SourceResolutionError('Official SukiSU source lookup failed') from error
    if not isinstance(document, dict):
        raise SourceResolutionError('Official SukiSU source did not return an object')
    return document


def _compare(base: str, head: str) -> dict:
    # Forgejo exposes only the forward count. Query both fixed-SHA directions
    # so branch divergence follows exactly the existing full-history policy.
    counts = []
    for first, second in ((base, head), (head, base)):
        result = _read('/compare/' + first + '...' + second + '?limit=1&page=2')
        count = result.get('total_commits')
        if type(count) is not int or not 0 <= count <= 2147483647 or not isinstance(result.get('commits'), list):
            raise SourceResolutionError('Official SukiSU comparison history count is invalid')
        counts.append(count)
    ahead, behind = counts
    status = ('diverged' if ahead and behind else 'ahead' if ahead else 'behind' if behind else 'identical')
    return {'url': API + '/compare/' + base + '...' + head,
            'base_commit': {'sha': base}, 'ahead_by': ahead, 'behind_by': behind,
            'total_commits': ahead, 'status': status}


def official_json(resource: str = '', *, missing: bool = False) -> dict | None:
    """Adapt the bounded public Forgejo responses used by the shared resolver."""
    if not resource:
        result = _read('')
        if type(result.get('id')) is not int or result['id'] != 3 or result.get('full_name') != SUKISU_OFFICIAL_REPOSITORY:
            raise SourceResolutionError('Official SukiSU repository identity changed')
        return result
    if match := re.fullmatch(r'/commits/([A-Za-z0-9%._/-]+)', resource):
        result = _read('/git/commits/' + match[1], missing=missing)
        if result is not None and (not isinstance(result.get('sha'), str) or not SHA.fullmatch(result['sha'])):
            raise SourceResolutionError('Official SukiSU commit identity is invalid')
        return result
    if match := re.fullmatch(r'/branches/([A-Za-z0-9%._/-]+)', resource):
        result = _read(resource, missing=missing)
        if result is None:
            return None
        commit = result.get('commit')
        reference = commit.get('id') if isinstance(commit, dict) else None
        if not isinstance(reference, str) or not SHA.fullmatch(reference):
            raise SourceResolutionError('Official SukiSU branch identity is invalid')
        return {**result, 'commit': {**commit, 'sha': reference.lower()}}
    if match := re.fullmatch(r'/compare/([0-9a-f]{40})\.\.\.([0-9a-f]{40})(?:\?per_page=1&page=2)?', resource):
        return _compare(match[1], match[2])
    if resource == '/releases/latest':
        return _read(resource, missing=missing)
    raise SourceResolutionError('Unsupported SukiSU official source query; use an explicit branch, tag or commit')
