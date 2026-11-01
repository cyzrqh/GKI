#!/usr/bin/env python3
"""Mention the repository owner once per pending or merged official-data report."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from upstream_pr_guard import NoRedirect, _owned_pr, validate_base, validate_repository

MAX_REPORT = 512 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_COMMENT = 60000
MAX_PAGES = 10
CHECK_STATES = frozenset(('success', 'failure', 'skipped', 'cancelled', 'unknown'))
BOT = 'github-actions[bot]'
SHA = re.compile(r'[0-9a-f]{40}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


class NotificationError(ValueError):
    pass


def parse_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise NotificationError('Duplicate notification JSON field')
            result[key] = value
        return result
    def reject(_):
        raise NotificationError('Non-finite notification JSON number')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=reject)
    except (UnicodeError, ValueError):
        raise NotificationError('Invalid strict UTF-8 notification JSON') from None


def read_report(path: Path) -> dict:
    path = path.absolute()
    if any(item.is_symlink() or getattr(item, 'is_junction', lambda: False)()
           for item in (path, *path.parents)):
        raise NotificationError('Notification report cannot traverse filesystem links')
    if not stat.S_ISREG(path.stat().st_mode) or not 0 < path.stat().st_size <= MAX_REPORT:
        raise NotificationError('Notification report must be a bounded regular file')
    with path.open('rb') as stream:
        raw = stream.read(MAX_REPORT + 1)
    if len(raw) > MAX_REPORT:
        raise NotificationError('Notification report exceeds its size bound')
    report = parse_json(raw)
    validate_report(report)
    return report


def validate_report(report: dict) -> None:
    if (not isinstance(report, dict) or type(report.get('schema_version')) is not int
            or report['schema_version'] != 1 or not isinstance(report.get('digest'), str)
            or not DIGEST.fullmatch(report['digest']) or type(report.get('has_changes')) is not bool
            or not isinstance(report.get('summary_markdown'), str) or not report['summary_markdown'].strip()
            or any(ord(char) < 32 and char not in '\n\r\t' for char in report['summary_markdown'])
            or not isinstance(report.get('checks'), dict) or set(report['checks']) != {'official', 'catalog'}
            or any(not isinstance(value, str) or value not in CHECK_STATES for value in report['checks'].values())):
        raise NotificationError('Notification requires a valid official-sync report')


def pull_number(value: str | int, repository: str) -> int:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,9}', value):
        number = int(value)
    elif isinstance(value, str):
        match = re.fullmatch(r'https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]{0,9})', value)
        if match is None or match[1].lower() != repository.lower():
            raise NotificationError('Notification PR must belong to the selected GitHub repository')
        number = int(match[2])
    else:
        raise NotificationError('Invalid notification pull request number')
    if not 1 <= number <= 9999999999:
        raise NotificationError('Invalid notification pull request number')
    return number


class GitHub:
    """Only this PR's reads and its exact issue-comment POST are permitted."""
    def __init__(self, repository: str, number: int, token: str, opener=None):
        validate_repository(repository)
        self.number = pull_number(number, repository)
        if not token or any(ord(char) < 32 or ord(char) == 127 for char in token):
            raise NotificationError('GH_TOKEN is required for official-update notifications')
        self.prefix = 'https://api.github.com/repos/' + repository
        self.token = token
        self.opener = opener or build_opener(NoRedirect())

    def request(self, suffix: str, *, document=None):
        comments = f'/issues/{self.number}/comments'
        if document is None:
            allowed = suffix in ('', f'/pulls/{self.number}') or re.fullmatch(
                re.escape(comments) + r'\?per_page=100&page=(?:[1-9]|10)', suffix)
        else:
            allowed = (suffix == comments and isinstance(document, dict) and set(document) == {'body'}
                       and isinstance(document['body'], str) and 0 < len(document['body'].encode('utf-8')) <= MAX_COMMENT)
        if not allowed:
            raise NotificationError('GitHub endpoint is outside the notification scope')
        url = self.prefix + suffix
        request = Request(url, data=None if document is None else json.dumps(document).encode('utf-8'),
                          method='GET' if document is None else 'POST', headers={
                              'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                              'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28',
                              'User-Agent': 'gki-official-sync-notification'})
        try:
            with self.opener.open(request, timeout=30) as response:
                if getattr(response, 'geturl', lambda: url)() != url:
                    raise NotificationError('GitHub notification redirects are not accepted')
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise NotificationError('GitHub notification response exceeds its size bound')
                value = parse_json(raw)
                # Count-based paging uses locally constructed URLs only. A
                # short page claiming another page is not a complete inventory.
                if ('rel="next"' in response.headers.get('Link', '')
                        and (not isinstance(value, list) or len(value) < 100)):
                    raise NotificationError('GitHub returned incomplete notification pagination')
                return value
        except HTTPError as error:
            raise NotificationError(f'GitHub notification request failed (HTTP {error.code})') from None
        except (URLError, OSError):
            raise NotificationError('GitHub notification request failed') from None


def verify_pr(pr, repository: str, base: str, number: int, state: str, commit: str | None):
    if (not isinstance(pr, dict) or type(pr.get('number')) is not int or pr['number'] != number
            or not isinstance(pr.get('body'), str)
            or any(not isinstance(pr.get(side), dict) or not isinstance(pr[side].get('repo'), dict)
                   for side in ('head', 'base')) or not _owned_pr(pr, repository, base)
            or pr.get('draft') is not False or type(pr.get('merged')) is not bool):
        raise NotificationError('Notification PR is not the owned same-repository official update')
    if state == 'pending':
        if commit is not None or pr.get('state') != 'open' or pr['merged']:
            raise NotificationError('Pending notification requires an open unmerged update PR')
        return None
    merged = pr.get('merge_commit_sha')
    if (pr.get('state') != 'closed' or not pr['merged'] or not isinstance(merged, str)
            or not SHA.fullmatch(merged) or commit is not None and commit != merged):
        raise NotificationError('Synced notification requires the actual merged update commit')
    return merged


def marker(state: str, digest: str) -> str:
    return f'<!-- gki-official-notice:v1 state={state} sha256={digest} -->'


def comment_body(report: dict, repository: str, number: int, state: str, commit: str | None) -> str:
    from official_sync_report import report_preview
    owner, _ = validate_repository(repository)
    lines = [marker(state, report['digest']), '', '@' + owner + ' ' + (
        '发现新的官方源码或内核目录更新，等待你手动同步。' if state == 'pending' else '本次官方记录更新已同步到默认分支。'), '',
        f'更新 PR：https://github.com/{repository}/pull/{number}', '']
    if state == 'pending':
        lines.extend(['手动同步：打开 **Actions → 检查并同步官方更新 → Run workflow**，将 **operation** 选择为 **同步官方更新**；也可以检查本 PR 的差异后手动合并。', ''])
    else:
        lines.extend([f'合并提交：https://github.com/{repository}/commit/{commit}', ''])
    lines.extend(['本次仅更新源码观察记录及目录，没有自动编译 APK 或内核。', '', report_preview(report['summary_markdown'].strip()), ''])
    body = '\n'.join(lines)
    if len(body.encode('utf-8')) > MAX_COMMENT:
        raise NotificationError('Official update detail exceeds the notification comment budget')
    return body


def notify(report: dict, repository: str, pull_request: str | int, state: str, *,
           base: str = 'main', commit: str | None = None, api=None) -> dict:
    validate_repository(repository)
    validate_base(base)
    validate_report(report)
    number = pull_number(pull_request, repository)
    commit = None if commit == '' else commit
    if state not in ('pending', 'synced') or commit is not None and not SHA.fullmatch(commit):
        raise NotificationError('Invalid notification state or fixed commit')
    result = {'notified': False, 'state': state, 'digest': report['digest'],
              'pull_request_url': f'https://github.com/{repository}/pull/{number}'}
    if not report['has_changes']:
        return {**result, 'reason': 'no_changes'}
    if api is None:
        api = GitHub(repository, number, os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN', ''))
    repo = api.request('')
    if (not isinstance(repo, dict) or str(repo.get('full_name', '')).lower() != repository.lower()
            or repo.get('default_branch') != base):
        raise NotificationError('Notifications must target the selected repository default branch')
    merged = verify_pr(api.request(f'/pulls/{number}'), repository, base, number, state, commit)
    body = comment_body(report, repository, number, state, merged)
    expected, duplicate = marker(state, report['digest']), False
    for page in range(1, MAX_PAGES + 1):
        batch = api.request(f'/issues/{number}/comments?per_page=100&page={page}')
        if (not isinstance(batch, list) or len(batch) > 100
                or any(not isinstance(item, dict) or not isinstance(item.get('body'), str)
                       or not isinstance(item.get('user'), dict) for item in batch)):
            raise NotificationError('GitHub returned an invalid notification comment inventory')
        duplicate |= any(item['user'].get('type') == 'Bot' and item['user'].get('login') == BOT
                         and expected in item['body'].splitlines() for item in batch)
        if len(batch) < 100:
            break
    else:
        raise NotificationError('Too many PR comments to deduplicate safely; no notification posted')
    if duplicate:
        return {**result, 'reason': 'already_notified'}
    posted = api.request(f'/issues/{number}/comments', document={'body': body})
    if not isinstance(posted, dict) or type(posted.get('id')) is not int or posted['id'] <= 0:
        raise NotificationError('GitHub did not confirm the notification comment')
    return {**result, 'notified': True, 'reason': 'posted', 'comment_id': posted['id']}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--pull-request', required=True)
    parser.add_argument('--state', choices=('pending', 'synced'), required=True)
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''))
    parser.add_argument('--base', default='main')
    parser.add_argument('--commit')
    args = parser.parse_args(argv)
    try:
        result = notify(read_report(args.report), args.repository, args.pull_request,
                        args.state, base=args.base, commit=args.commit)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError):
        # No remote response, request, arbitrary exception text or token enters logs.
        print('::error::Cannot post a verified official-update notification', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
