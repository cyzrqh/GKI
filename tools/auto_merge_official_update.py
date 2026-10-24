#!/usr/bin/env python3
"""Validate and squash only this repository's additive official-data update PR."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

from check_official_updates import (baseline, changes, head_state, validate_snapshot,
                                    UpdateError)
from matrix import validate_catalog, _month
from pin_build_dependencies import CompatibilityError
from resolve_build_sources import SourceResolutionError
from snapshot_ksu_source import temporary_directory
from upstream_pr_guard import (BRANCH, HEADS_MARKER_PATTERN, NoRedirect, GuardError,
                               _owned_pr, validate_base, validate_repository)

FILES = frozenset(('data/kernels.json', 'data/upstream-candidates.json'))
PROFILE = 'build-compatibility.json'
SHA = re.compile(r'[0-9a-f]{40}\Z')
MAX_FILE = 512 * 1024
MAX_RESPONSE = 4 * 1024 * 1024


class MergeError(ValueError):
    pass


def full_sha(value) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise MergeError('GitHub did not return a fixed full commit/blob SHA')
    return value


def json_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise MergeError('Duplicate JSON field in official update data')
        result[key] = value
    return result


def parse_json(raw: bytes):
    def reject(_):
        raise MergeError('Non-finite JSON number in official update data')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=json_object, parse_constant=reject)
    except (ValueError, UnicodeError):
        raise MergeError('Official update data is not strict UTF-8 JSON') from None


class GitHub:
    """Fixed repository API paths only; redirects and remote error text are refused."""
    def __init__(self, repository: str, token: str, opener=None):
        validate_repository(repository)
        if not token or any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise MergeError('GH_TOKEN is required for automatic official-data synchronization')
        self.prefix = '/repos/' + repository
        self.token = token
        self.opener = opener or build_opener(NoRedirect())

    def request(self, suffix: str, *, document=None, missing=False):
        # These are constructed local paths, never URLs taken from PR metadata.
        allowed = (suffix == '' or re.fullmatch(r'/pulls/[1-9][0-9]*(?:/files\?per_page=100&page=[12])?', suffix)
                   or re.fullmatch(r'/git/ref/heads/[A-Za-z0-9_./-]+', suffix)
                   or re.fullmatch(r'/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}\?per_page=100&page=1', suffix)
                   or re.fullmatch(r'/contents/(?:data/kernels\.json|data/upstream-candidates\.json|build-compatibility\.json)\?ref=[0-9a-f]{40}', suffix))
        merge = document is not None
        if merge:
            allowed = re.fullmatch(r'/pulls/[1-9][0-9]*/merge', suffix)
            if (not isinstance(document, dict) or set(document) != {'sha', 'merge_method'}
                    or document['merge_method'] != 'squash'):
                raise MergeError('Only a SHA-bound squash merge is permitted')
            full_sha(document['sha'])
        if not allowed or '..' in suffix.split('?', 1)[0].replace('...', ''):
            raise MergeError('GitHub API endpoint is outside the official-update scope')
        request = Request('https://api.github.com' + self.prefix + suffix,
                          data=json.dumps(document).encode() if merge else None,
                          method='PUT' if merge else 'GET', headers={
                              'Authorization': 'Bearer ' + self.token,
                              'Accept': 'application/vnd.github+json',
                              'Content-Type': 'application/json',
                              'X-GitHub-Api-Version': '2022-11-28',
                              'User-Agent': 'gki-official-data-sync'})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE or 'rel="next"' in response.headers.get('Link', ''):
                    raise MergeError('GitHub response is oversized or paginated; refusing an incomplete validation')
            return parse_json(raw)
        except HTTPError as error:
            if missing and error.code == 404:
                return None
            raise MergeError(f'GitHub official-data synchronization failed (HTTP {error.code}); repository rules remain enforced') from None
        except (URLError, OSError):
            raise MergeError('GitHub official-data synchronization request failed') from None


def repository_state(api, repository: str, base: str):
    value = api.request('')
    if (not isinstance(value, dict) or str(value.get('full_name', '')).lower() != repository.lower()
            or value.get('default_branch') != base):
        raise MergeError('Automatic synchronization must target this repository default branch')


def branch_sha(api, branch: str) -> str:
    value = api.request('/git/ref/heads/' + quote(branch, safe='/'))
    if (not isinstance(value, dict) or value.get('ref') != 'refs/heads/' + branch
            or not isinstance(value.get('object'), dict) or value['object'].get('type') != 'commit'):
        raise MergeError('GitHub returned an unexpected official-update branch ref')
    return full_sha(value['object'].get('sha'))


def validate_pr(pr, repository: str, base: str, number: int) -> tuple[str, bool]:
    if (not isinstance(pr, dict) or type(pr.get('number')) is not int or pr['number'] != number
            or not isinstance(pr.get('body'), str) or len(pr['body']) > 128 * 1024
            or any(not isinstance(pr.get(side), dict) or not isinstance(pr[side].get('repo'), dict)
                   for side in ('head', 'base')) or not _owned_pr(pr, repository, base)):
        raise MergeError('Pull request is not the marked same-repository official-update automation')
    owner = repository.split('/')[0].lower()
    author = pr.get('user') or {}
    if not isinstance(author, dict) or not (
            author.get('type') == 'User' and str(author.get('login', '')).lower() == owner
            or author.get('type') == 'Bot' and author.get('login') == 'github-actions[bot]'):
        raise MergeError('Official-update pull request has an untrusted author')
    if (pr.get('draft') is not False or type(pr.get('merged')) is not bool
            or not any(HEADS_MARKER_PATTERN.fullmatch(line) for line in pr['body'].splitlines())):
        raise MergeError('Official-update pull request must be a non-draft marked snapshot')
    head = full_sha(pr['head'].get('sha'))
    if pr['merged']:
        if pr.get('state') != 'closed':
            raise MergeError('GitHub returned an inconsistent merged pull request')
        full_sha(pr.get('merge_commit_sha'))
        return head, True
    if pr.get('state') != 'open' or pr.get('mergeable') is False:
        raise MergeError('Official-update pull request is closed or cannot merge normally')
    return head, False


def file_map(files) -> dict:
    if not isinstance(files, list) or not 1 <= len(files) <= len(FILES):
        raise MergeError('The complete update must change only its two permitted data files')
    result = {}
    for item in files:
        if (not isinstance(item, dict) or item.get('filename') not in FILES
                or item['filename'] in result or item.get('status') not in ('added', 'modified')
                or 'previous_filename' in item):
            raise MergeError('Official update contains a foreign, removed, renamed or duplicate file')
        result[item['filename']] = (item['status'], full_sha(item.get('sha')))
    return result


def read_file(api, path: str, ref: str, *, expected_blob=None, missing=False):
    value = api.request('/contents/' + path + '?ref=' + ref, missing=missing)
    if value is None and missing:
        return None
    if (not isinstance(value, dict) or value.get('type') != 'file' or value.get('path') != path
            or value.get('encoding') != 'base64' or type(value.get('size')) is not int
            or not 0 < value['size'] <= MAX_FILE or not isinstance(value.get('content'), str)
            or len(value['content']) > MAX_FILE * 2):
        raise MergeError('GitHub Contents did not return a bounded regular JSON file')
    try:
        raw = base64.b64decode(''.join(value['content'].split()), validate=True)
    except (ValueError, binascii.Error):
        raise MergeError('GitHub Contents returned invalid Base64') from None
    actual = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if (len(raw) != value['size'] or actual != full_sha(value.get('sha'))
            or expected_blob is not None and actual != expected_blob):
        raise MergeError('GitHub Contents blob does not match the reviewed commit/file list')
    return parse_json(raw)


def validate_additions(old, new):
    validate_catalog(old)
    validate_catalog(new)
    if {key: value for key, value in old.items() if key != 'entries'} != {
            key: value for key, value in new.items() if key != 'entries'}:
        raise MergeError('Kernel catalog schema/metadata cannot be rewritten automatically')
    def rows(document):
        result = {}
        for entry in document['entries']:
            key = tuple(entry[name] for name in ('android_version', 'kernel_version', 'kernel', 'date'))
            if key in result:
                raise MergeError('Kernel catalog contains a duplicate target')
            result[key] = entry
        return result
    before, after = rows(old), rows(new)
    if any(after.get(key) != entry for key, entry in before.items()):
        raise MergeError('Automatic updates must preserve every existing kernel entry unchanged')
    added = [entry for key, entry in after.items() if key not in before]
    if not added:
        raise MergeError('Kernel catalog update contains no new verified target')
    allowed = {'android_version', 'kernel_version', 'kernel', 'date', 'ref', 'revision'}
    fixed = {}
    for entry in new['entries']:
        ref = entry.get('ref', '')
        if isinstance(ref, str) and SHA.fullmatch(ref):
            if ref in fixed and fixed[ref] != entry['kernel']:
                raise MergeError('One fixed kernel commit cannot identify conflicting kernel versions')
            fixed[ref] = entry['kernel']
    for entry in added:
        if set(entry) - allowed:
            raise MergeError('New kernel entry contains unsupported attributes')
        full_sha(entry.get('ref'))
        _month(entry['date'], 'New official kernel target')
        if 'revision' in entry and (not isinstance(entry['revision'], str)
                                    or not re.fullmatch(r'r[0-9]+', entry['revision'])):
            raise MergeError('New kernel boot revision must use the official r<number> format')


def validate_candidates(document, expected_baseline):
    keys = {'schema_version', 'status', 'baseline', 'sources', 'acknowledged_heads', 'changes'}
    if (not isinstance(document, dict) or set(document) != keys
            or type(document.get('schema_version')) is not int or document['schema_version'] != 1
            or document.get('status') != 'candidates-require-validation'
            or document.get('baseline') != expected_baseline):
        raise MergeError('Official candidate schema/baseline differs from the fixed compatibility profile')
    snapshot = validate_snapshot(document['sources'])
    if (type(document['sources'].get('schema_version')) is not int or snapshot != document['sources']
            or document['acknowledged_heads'] != head_state(snapshot)
            or document['changes'] != changes(expected_baseline, snapshot)):
        raise MergeError('Official candidate changes/acknowledgement do not match their validated source snapshot')


def sync(repository: str, number: int, base: str, api) -> dict:
    validate_repository(repository)
    validate_base(base)
    if type(number) is not int or number < 1:
        raise MergeError('Pull request number must be a positive integer')
    repository_state(api, repository, base)
    pr_path = '/pulls/' + str(number)
    pr = api.request(pr_path)
    head, merged = validate_pr(pr, repository, base, number)
    public_url = f'https://github.com/{repository}/pull/{number}'
    def result(commit):
        return {'merged': True, 'pull_request_url': public_url, 'merge_commit': full_sha(commit)}
    if merged:
        return result(pr['merge_commit_sha'])
    current_base = branch_sha(api, base)
    if branch_sha(api, BRANCH) != head:
        raise MergeError('The official-update branch changed; retry the update check')
    count = pr.get('changed_files')
    if type(count) is not int or not 1 <= count <= len(FILES):
        raise MergeError('Pull request file inventory exceeds the permitted data scope')
    pr_files = file_map(api.request(pr_path + '/files?per_page=100&page=1'))
    if len(pr_files) != count or api.request(pr_path + '/files?per_page=100&page=2') != []:
        raise MergeError('Pull request file inventory is incomplete or unexpectedly paginated')
    comparison = api.request('/compare/' + current_base + '...' + head + '?per_page=100&page=1')
    if (not isinstance(comparison, dict) or comparison.get('status') not in ('ahead', 'diverged')
            or not isinstance(comparison.get('base_commit'), dict)
            or comparison['base_commit'].get('sha') != current_base
            or type(comparison.get('total_commits')) is not int or not 1 <= comparison['total_commits'] <= 100
            or not isinstance(comparison.get('commits'), list)
            or len(comparison['commits']) != comparison['total_commits']
            or comparison['commits'][-1].get('sha') != head
            or file_map(comparison.get('files')) != pr_files):
        raise MergeError('Current-base/head comparison does not prove the complete allowed update')
    for path, (_, blob) in pr_files.items():
        new = read_file(api, path, head, expected_blob=blob)
        if path == 'data/kernels.json':
            old = read_file(api, path, current_base)
            validate_additions(old, new)
        else:
            profile = read_file(api, PROFILE, current_base)
            # Reuse the existing profile validator with JSON data only. No PR
            # source is checked out or imported/executed.
            with temporary_directory(Path.cwd(), '.gki-update-profile-') as temporary:
                (temporary / PROFILE).write_text(json.dumps(profile), encoding='utf-8')
                expected_baseline = baseline(temporary)
            validate_candidates(new, expected_baseline)
    # Refuse any source/base drift observed during content validation. A PR's
    # base.sha can be its old creation point; the actual branch ref is our base.
    repository_state(api, repository, base)
    latest = api.request(pr_path)
    latest_head, already = validate_pr(latest, repository, base, number)
    if already:
        return result(latest['merge_commit_sha'])
    if (latest_head != head or latest.get('changed_files') != count
            or branch_sha(api, base) != current_base or branch_sha(api, BRANCH) != head):
        raise MergeError('Official-update head/base changed during validation; retry without merging')
    response = api.request(pr_path + '/merge', document={'sha': head, 'merge_method': 'squash'})
    if not isinstance(response, dict) or response.get('merged') is not True:
        raise MergeError('GitHub did not merge the verified update; check repository rules')
    return result(response.get('sha'))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pull-request', type=int, required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--github-output', type=Path,
                        default=Path(os.environ['GITHUB_OUTPUT']) if os.environ.get('GITHUB_OUTPUT') else None)
    args = parser.parse_args(argv)
    try:
        repository = os.environ.get('GITHUB_REPOSITORY', '')
        api = GitHub(repository, os.environ.get('GH_TOKEN', ''))
        result = sync(repository, args.pull_request, args.base, api)
        if args.github_output:
            with args.github_output.open('a', encoding='utf-8', newline='\n') as stream:
                for key, value in result.items():
                    stream.write(key + '=' + (str(value).lower() if type(value) is bool else value) + '\n')
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (MergeError, GuardError, UpdateError, CompatibilityError, SourceResolutionError, OSError, ValueError, UnicodeError):
        # Remote bodies and data never enter errors, and tokens cannot reach logs.
        print('Official update was not automatically merged: ownership, data, current commits or repository rules could not be verified.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
