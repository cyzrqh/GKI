#!/usr/bin/env python3
"""Record official SukiSU/SUSFS candidates for review without changing build pins."""
from __future__ import annotations

import argparse
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
from resolve_build_sources import SourceResolutionError, github_commit, github_json, validate_ref

ROOT = Path(__file__).resolve().parents[1]
SUKISU = 'SukiSU-Ultra/SukiSU-Ultra'
SUSFS = 'simonpunk/susfs4ksu'
TARGETS = ('android12-5.10', 'android13-5.15', 'android14-6.1', 'android15-6.6', 'android16-6.12')
CANDIDATE_PATH = 'data/upstream-candidates.json'
TITLE = 'chore: review official SukiSU and SUSFS updates'
BRANCH = 'automation/official-updates'
SHA = re.compile(r'[0-9a-fA-F]{40}\Z')
LOOKUP_ATTEMPTS = 3


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
                              'susfs': {target: gitlab_head(target) for target in TARGETS}})


def validate_snapshot(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'sukisu', 'susfs'}
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
    return result


def head_state(snapshot: dict) -> dict:
    release = snapshot['sukisu']['release']
    result = {'sukisu_branch': snapshot['sukisu']['branch'], 'sukisu_head': snapshot['sukisu']['head'],
              'sukisu_release': {'tag': release['tag'], 'commit': release['commit']} if release else None,
              'susfs': snapshot['susfs']}
    if 'development' in snapshot['sukisu']:
        result['sukisu_development'] = snapshot['sukisu']['development']
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
    for target in TARGETS:
        if snapshot['susfs'][target] != current['susfs'][target]:
            selected.append({'source': 'SUSFS', 'ref': 'gki-' + target, 'current': current['susfs'][target],
                             'candidate': snapshot['susfs'][target], 'url': f'https://gitlab.com/{SUSFS}/-/commit/' + snapshot['susfs'][target]})
    return selected


def report_text(current: dict, snapshot: dict, updates: list[dict]) -> str:
    lines = ['<!-- gki-official-candidates:v1 -->', heads_marker(snapshot),
             '检测到官方源码的新候选版本。本 PR 记录更新提示，尚未将候选版本设为构建版本。', '',
             f"当前验证基线：SukiSU `{current['sukisu']}`，SUSFS `{current['susfs_version']}`。", '',
             '| 官方源码 | 分支/版本 | 当前验证提交 | 候选提交 |', '| --- | --- | --- | --- |']
    for item in updates:
        lines.append(f"| {item['source']} | `{item['ref']}` | `{item['current'][:12]}` | [`{item['candidate'][:12]}`]({item['url']}) |")
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
