#!/usr/bin/env python3
"""Delete current-run staging artifacts only after raw files match the public Release."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


class CleanupError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CleanupError('Artifact maintenance does not follow authenticated redirects')


def api(repository: str, resource: str, *, method: str = 'GET'):
    token = os.environ.get('GH_TOKEN', '')
    if not token or any(ord(char) < 32 or ord(char) == 127 for char in token):
        raise CleanupError('A valid Actions token is required')
    request = Request(f'https://api.github.com/repos/{repository}{resource}', method=method,
                      headers={'Authorization': 'Bearer ' + token,
                               'Accept': 'application/vnd.github+json', 'User-Agent': 'gki-artifact-maintenance'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=45) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise CleanupError('Artifact API response exceeded its limit')
        return json.loads(raw) if raw else None
    except HTTPError as error:
        if method == 'DELETE' and error.code == 404:
            return None
        raise CleanupError(f'Artifact API {method} failed (HTTP {error.code}); remaining artifacts preserved') from None
    except (OSError, URLError, ValueError):
        raise CleanupError('Artifact API request failed; remaining artifacts preserved') from None


def validate_release(release: dict, source_sha: str) -> dict:
    if (not isinstance(release, dict) or release.get('draft') is not False
            or release.get('prerelease') is not False or release.get('target_commitish') != source_sha):
        raise CleanupError('Expected a public Release for the verified build commit')
    assets = release.get('assets')
    if not isinstance(assets, list) or not 2 <= len(assets) <= 1000:
        raise CleanupError('Expected checksums and at least one public build file')
    expected = {}
    for asset in assets:
        name = asset.get('name', '')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+@()-]{0,249}', name)
                or '..' in name or name in expected or
                not (name == 'SHA256SUMS.txt' or name.endswith(('.apk', '.img', '-AnyKernel3.zip')))
                or not isinstance(asset.get('size'), int) or asset['size'] <= 0
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest', ''))):
            raise CleanupError('Public Release asset identity is invalid or duplicated')
        expected[name] = asset
    if 'SHA256SUMS.txt' not in expected:
        raise CleanupError('Public Release is missing its checksums')
    return expected


def cleanup(run_id: str, release_tag: str | None = None, source_sha: str | None = None) -> list[str]:
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    current = os.environ.get('GITHUB_RUN_ID', '')
    head_sha = os.environ.get('GITHUB_SHA', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise CleanupError('Expected a valid Actions repository')
    if not re.fullmatch(r'[1-9][0-9]*', run_id) or run_id != current:
        raise CleanupError('Cleanup may only delete artifacts belonging to the current run')
    source_sha = source_sha or head_sha
    if not re.fullmatch(r'[0-9a-f]{40}', head_sha) or not re.fullmatch(r'[0-9a-f]{40}', source_sha):
        raise CleanupError('Expected a complete verified build commit')
    release_tag = release_tag or f'gki-{run_id}-{attempt}'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', release_tag):
        raise CleanupError('Invalid Release tag')
    run = api(repository, f'/actions/runs/{run_id}')
    if (run.get('head_sha') != head_sha or run.get('head_repository', {}).get('full_name') != repository):
        raise CleanupError('Current run source identity does not match Actions')
    expected = validate_release(api(repository, '/releases/tags/' + quote(release_tag, safe='')), source_sha)
    artifacts = []
    for page in range(1, 12):
        document = api(repository, f'/actions/runs/{run_id}/artifacts?per_page=100&page={page}')
        batch = document.get('artifacts') if isinstance(document, dict) else None
        if not isinstance(batch, list) or len(batch) > 100 or document.get('total_count', 1001) > 1000:
            raise CleanupError('Artifact listing is invalid or exceeds its limit')
        artifacts.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise CleanupError('Artifact listing did not finish')
    for name, asset in expected.items():
        matches = [item for item in artifacts if item.get('name') == name]
        if (len(matches) != 1 or matches[0].get('expired') is not False
                or matches[0].get('digest') != asset['digest']
                or matches[0].get('size_in_bytes') != asset['size']):
            raise CleanupError(f'Original artifact does not match the public Release: {name}; staging preserved')
    staging = [item for item in artifacts if isinstance(item.get('name'), str) and
               item['name'].startswith(('Release-', 'Manager-Identity-', 'AnyKernel3-', 'SukiSU_kernel-'))]
    ids = [item.get('id') for item in staging]
    if any(type(value) is not int or value <= 0 for value in ids) or len(ids) != len(set(ids)):
        raise CleanupError('Staging artifact IDs are invalid; no files deleted')
    # All verification finishes before the first mutation. Unknown diagnostic
    # artifacts and public Release assets are never removed by this tool.
    deleted = []
    for item in staging:
        api(repository, f'/actions/artifacts/{item["id"]}', method='DELETE')
        deleted.append(item['name'])
    print(json.dumps({'verified_original_files': list(expected), 'deleted_staging': deleted}, ensure_ascii=False))
    return deleted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--release-tag')
    parser.add_argument('--source-sha')
    args = parser.parse_args(argv)
    try:
        cleanup(args.run_id, args.release_tag, args.source_sha)
    except (CleanupError, KeyError, TypeError) as error:
        print('Artifact cleanup failed: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
