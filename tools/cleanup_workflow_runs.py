#!/usr/bin/env python3
"""Keep published Release runs plus the newest ordinary successful runs.

The default is a read-only preview retaining two successful non-cleanup runs
in addition to every run identified by an existing published gki Release.
Release metadata is read only; Release/asset deletion is never allowed.
Only --execute enables deletion of validated old workflow runs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from release_tags import release_run_identity

DEFAULT_WORKFLOW = 'all'
DEFAULT_KEEP = 2
DEFAULT_EXCLUDE_WORKFLOW = 'cleanup-build-history.yml'
CONCLUSIONS = frozenset(('success', 'failure', 'neutral', 'cancelled', 'skipped',
                         'timed_out', 'action_required', 'stale', 'startup_failure'))
MAX_RESPONSE = 16 * 1024 * 1024
MAX_RUNS = 100000
MAX_RELEASES = 100000


class CleanupError(RuntimeError):
    """A safe, locally generated error message, without API bodies or secrets."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CleanupError('Authenticated GitHub API redirects are refused')


def validate_repository(repository):
    if not isinstance(repository, str) or not re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', repository):
        raise CleanupError('Expected a valid OWNER/REPO repository')
    return repository


def validate_workflow(workflow):
    if not isinstance(workflow, str) or not re.fullmatch(
            r'(?:all|[1-9][0-9]*|[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.ya?ml)', workflow):
        raise CleanupError('Expected all, a workflow filename, or a numeric workflow ID')
    return workflow


def positive_id(value):
    return type(value) is int and value > 0


def parse_json(raw):
    def object_pairs(pairs):
        document = {}
        for key, value in pairs:
            if key in document:
                raise ValueError('duplicate JSON key')
            document[key] = value
        return document

    def reject_constant(_):
        raise ValueError('non-finite JSON number')

    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=object_pairs,
                          parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        raise CleanupError('GitHub API response was not valid strict UTF-8 JSON') from None


class GitHub:
    """Allow workflow/run endpoints and read-only paginated Release metadata."""

    def __init__(self, repository, token, opener=None):
        self.repository = validate_repository(repository)
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) == 127 for c in token):
            raise CleanupError('A valid GH_TOKEN or GITHUB_TOKEN is required')
        self.token = token
        self.opener = opener or build_opener(NoRedirect())

    def request(self, resource, *, method='GET', missing_ok=False):
        workflow = r'(?:[1-9][0-9]*|[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.ya?ml)'
        run_path = re.fullmatch(r'/actions/runs/[1-9][0-9]*', resource)
        listing = re.fullmatch(
            rf'/actions/workflows/{workflow}(?:/runs\?per_page=100&page=[1-9][0-9]*)?', resource)
        repository_listing = re.fullmatch(r'/actions/runs\?per_page=100&page=[1-9][0-9]*', resource)
        releases = re.fullmatch(r'/releases\?per_page=100&page=[1-9][0-9]*', resource)
        if method not in ('GET', 'DELETE') or not (
                run_path or (method == 'GET' and (listing or repository_listing or releases))):
            raise CleanupError('Only workflow/run paths and read-only Release listing are allowed')
        request = Request('https://api.github.com/repos/' + self.repository + resource, method=method,
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Accept': 'application/vnd.github+json',
                                   'X-GitHub-Api-Version': '2022-11-28',
                                   'User-Agent': 'gki-workflow-run-maintenance'})
        try:
            with self.opener.open(request, timeout=45) as response:
                status = response.status
                if method == 'DELETE':
                    if status != 204:
                        raise CleanupError('GitHub run deletion returned an unexpected status')
                    # GitHub's successful DELETE is an empty 204, never JSON.
                    return 'deleted'
                if status != 200:
                    raise CleanupError('GitHub run lookup returned an unexpected status')
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise CleanupError('GitHub API response exceeded its size limit')
            return parse_json(raw)
        except HTTPError as error:
            code = error.code
            error.close()
            if code == 404 and (method == 'DELETE' or missing_ok):
                return 'gone' if method == 'DELETE' else None
            raise CleanupError(f'GitHub API {method} failed (HTTP {code})') from None
        except (OSError, URLError, ValueError):
            raise CleanupError(f'GitHub API {method} request failed') from None


def validate_run(run, workflow_id=None):
    if not isinstance(run, dict) or not positive_id(run.get('id')) or not positive_id(run.get('workflow_id')):
        raise CleanupError('Workflow run IDs are missing or invalid')
    if workflow_id is not None and run['workflow_id'] != workflow_id:
        raise CleanupError('Workflow listing contains a different workflow ID')
    if (not isinstance(run.get('event'), str) or not run['event']
            or not isinstance(run.get('status'), str) or not run['status']
            or not isinstance(run.get('display_title'), str)):
        raise CleanupError('Workflow run event, status, or title is missing or invalid')
    conclusion = run.get('conclusion')
    valid_conclusion = isinstance(conclusion, str) and conclusion in CONCLUSIONS
    if ('conclusion' not in run or (run['status'] == 'completed' and not valid_conclusion)
            or (run['status'] != 'completed' and conclusion is not None and not valid_conclusion)):
        raise CleanupError('Workflow run conclusion is missing or invalid')
    if 'run_attempt' in run and not positive_id(run['run_attempt']):
        raise CleanupError('Workflow run attempt is invalid')
    if 'head_sha' in run and (not isinstance(run['head_sha'], str)
                             or not re.fullmatch(r'[0-9a-f]{40}', run['head_sha'])):
        raise CleanupError('Workflow run commit is invalid')
    created = run.get('created_at')
    if not isinstance(created, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', created):
        raise CleanupError('Workflow run creation time is missing or invalid')
    try:
        return datetime.strptime(created, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        raise CleanupError('Workflow run creation time is invalid') from None


def fetch_workflow_runs(client, workflow=DEFAULT_WORKFLOW):
    """Finish and validate every page before any deletion can be considered."""
    validate_workflow(workflow)
    workflow_id = None
    if workflow == 'all':
        resource = '/actions'
    else:
        resource = '/actions/workflows/' + quote(workflow, safe='')
        metadata = client.request(resource)
        if not isinstance(metadata, dict) or not positive_id(metadata.get('id')):
            raise CleanupError('Workflow metadata is missing a valid ID')
        workflow_id = metadata['id']
    runs, seen, total = [], set(), None
    for page in range(1, MAX_RUNS // 100 + 2):
        document = client.request(resource + f'/runs?per_page=100&page={page}')
        if not isinstance(document, dict):
            raise CleanupError('Workflow run listing is not an object')
        count, batch = document.get('total_count'), document.get('workflow_runs')
        if (type(count) is not int or not 0 <= count <= MAX_RUNS
                or not isinstance(batch, list) or len(batch) > 100):
            raise CleanupError('Workflow run listing is incomplete or exceeds its limit')
        if total is None:
            total = count
        if count != total:
            raise CleanupError('Workflow run count changed during pagination; rerun the cleanup')
        for run in batch:
            validate_run(run, workflow_id)
            if run['id'] in seen:
                raise CleanupError('Workflow run pagination contains a duplicate ID')
            seen.add(run['id'])
            runs.append(run)
        if len(runs) > total:
            raise CleanupError('Workflow run listing exceeds its advertised count')
        if len(batch) < 100:
            if len(runs) != total:
                raise CleanupError('Workflow run listing ended before its advertised count')
            return workflow_id, runs
    raise CleanupError('Workflow run pagination did not finish')


def fetch_release_run_ids(client, runs):
    """Protect explicit run/attempt identities from old and versioned Releases.

    A commit can belong to many manual runs, so target_commitish never selects
    a run. Unknown or conflicting published identities stop cleanup safely.
    Draft source backups have no published build and do not protect a run.
    """
    original = {run['id']: run for run in runs}
    protected, seen_ids, seen_tags = set(), set(), set()
    for page in range(1, MAX_RELEASES // 100 + 2):
        batch = client.request(f'/releases?per_page=100&page={page}')
        if not isinstance(batch, list) or len(batch) > 100:
            raise CleanupError('Release listing is incomplete or invalid; no remaining runs may be deleted')
        for release in batch:
            if (not isinstance(release, dict) or not positive_id(release.get('id'))
                    or type(release.get('draft')) is not bool
                    or not isinstance(release.get('tag_name'), str)
                    or not 0 < len(release['tag_name']) <= 1024
                    or any(ord(c) < 32 or ord(c) == 127 for c in release['tag_name'])):
                raise CleanupError('Release identity is invalid; source runs cannot be protected safely')
            tag = release['tag_name']
            if release['id'] in seen_ids or tag in seen_tags:
                raise CleanupError('Release pagination contains duplicate identities; rerun the cleanup')
            seen_ids.add(release['id'])
            seen_tags.add(tag)
            if len(seen_ids) > MAX_RELEASES:
                raise CleanupError('Release listing exceeds its safe pagination limit')
            if release['draft']:
                continue
            published = release.get('published_at')
            if (not isinstance(published, str)
                    or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', published)):
                raise CleanupError('Published Release time is missing or invalid')
            try:
                datetime.strptime(published, '%Y-%m-%dT%H:%M:%SZ')
            except ValueError:
                raise CleanupError('Published Release time is invalid') from None
            try:
                identity = release_run_identity(release)
            except ValueError:
                raise CleanupError('A published Release has no verified gki run/attempt tag or versioned run marker; '
                                   'cleanup stopped to preserve its unknown source run') from None
            if (identity['repository'] is not None
                    and identity['repository'].casefold() != client.repository.casefold()):
                raise CleanupError('Published Release run marker belongs to another repository')
            run_id, attempt = identity['run_id'], identity['run_attempt']
            run = original.get(run_id)
            if run is not None:
                if not positive_id(run.get('run_attempt')) or attempt > run['run_attempt']:
                    raise CleanupError('Published Release attempt does not match its Actions run')
                if identity['head_sha'] is not None and identity['head_sha'] != run.get('head_sha'):
                    raise CleanupError('Published Release commit does not match its Actions run')
            protected.add(run_id)
        if len(batch) < 100:
            return protected
    raise CleanupError('Release pagination did not finish; remaining runs preserved')


@dataclass
class CleanupPlan:
    workflow_id: int | None
    kept_ids: list[int] = field(default_factory=list)
    candidate_ids: list[int] = field(default_factory=list)
    preserved: dict[int, str] = field(default_factory=dict)
    excluded_workflow_ids: list[int] = field(default_factory=list)
    release_run_ids: list[int] = field(default_factory=list)


def preservation_reason(run, current_run_id=None):
    if current_run_id is not None and str(run['id']) == str(current_run_id):
        return 'current run'
    if run['status'] != 'completed':
        return 'not completed'
    return None


def plan_runs(runs, *, keep=DEFAULT_KEEP, current_run_id=None, workflow_id=None,
              excluded_workflow_ids=(), release_run_ids=()):
    """Release runs are extra protection and never consume ordinary success slots."""
    if type(keep) is not int or keep < 1:
        raise CleanupError('At least one successful workflow run must be retained')
    if not isinstance(runs, list):
        raise CleanupError('Expected a complete list of workflow runs')
    if workflow_id is not None and not positive_id(workflow_id):
        raise CleanupError('Expected a valid workflow ID')
    if current_run_id is not None and not re.fullmatch(r'[1-9][0-9]*', str(current_run_id)):
        raise CleanupError('GITHUB_RUN_ID is invalid')
    if (not isinstance(excluded_workflow_ids, (list, tuple, set, frozenset))
            or any(not positive_id(value) for value in excluded_workflow_ids)):
        raise CleanupError('Excluded workflow IDs must be valid positive integers')
    excluded = set(excluded_workflow_ids)
    if (not isinstance(release_run_ids, (list, tuple, set, frozenset))
            or any(not positive_id(value) for value in release_run_ids)):
        raise CleanupError('Release source run IDs must be valid positive integers')
    published = set(release_run_ids)
    seen, ordered = set(), []
    for run in runs:
        created = validate_run(run, workflow_id)
        if run['id'] in seen:
            raise CleanupError('Workflow run list contains a duplicate ID')
        seen.add(run['id'])
        ordered.append((created, run['id'], run))
    ordered.sort(key=lambda item: (item[0], item[1]), reverse=True)
    successful = [run_id for _, run_id, run in ordered
                  if run['status'] == 'completed' and run['conclusion'] == 'success'
                  and run['workflow_id'] not in excluded and run_id not in published]
    kept_ids = successful[:keep]
    kept = set(kept_ids)
    candidates, preserved = [], {}
    for _, run_id, run in ordered:
        if run_id in kept:
            continue
        reason = 'published release' if run_id in published else preservation_reason(run, current_run_id)
        if reason:
            preserved[run_id] = reason
        else:
            candidates.append(run_id)
    return CleanupPlan(workflow_id, kept_ids, candidates, preserved, sorted(excluded), sorted(published))


@dataclass
class CleanupReport:
    repository: str
    workflow: str
    keep: int
    execute: bool
    plan: CleanupPlan | None = None
    deleted_ids: list[int] = field(default_factory=list)
    gone_ids: list[int] = field(default_factory=list)
    error: str | None = None
    finished: bool = False

    def as_dict(self):
        candidates = self.plan.candidate_ids if self.plan else []
        finished_ids = set(self.deleted_ids + self.gone_ids)
        return {'repository': self.repository, 'workflow': self.workflow, 'keep': self.keep,
                'preserve_releases': True,
                'preserve_release_runs': True,
                'mode': 'execute' if self.execute else 'dry-run',
                'status': 'failed' if self.error or not self.finished else 'completed',
                'kept_ids': self.plan.kept_ids if self.plan else [],
                'preserved': self.plan.preserved if self.plan else {},
                'excluded_workflow_ids': self.plan.excluded_workflow_ids if self.plan else [],
                'release_run_ids': self.plan.release_run_ids if self.plan else [],
                'candidate_ids': candidates, 'deleted_ids': self.deleted_ids, 'gone_ids': self.gone_ids,
                'remaining_candidate_ids': [value for value in candidates if value not in finished_ids],
                'error': self.error}


def cleanup(client, workflow=DEFAULT_WORKFLOW, *, keep=DEFAULT_KEEP, current_run_id=None,
            execute=False, report=None, exclude_workflow=DEFAULT_EXCLUDE_WORKFLOW):
    report = report if report is not None else CleanupReport(client.repository, workflow, keep, execute)
    excluded = []
    if exclude_workflow is not None:
        validate_workflow(exclude_workflow)
        if exclude_workflow == 'all':
            raise CleanupError('Only one exact maintenance workflow can be excluded')
        metadata = client.request('/actions/workflows/' + quote(exclude_workflow, safe=''))
        if not isinstance(metadata, dict) or not positive_id(metadata.get('id')):
            raise CleanupError('Excluded workflow metadata is missing a valid ID')
        excluded.append(metadata['id'])
    workflow_id, runs = fetch_workflow_runs(client, workflow)
    release_ids = fetch_release_run_ids(client, runs)
    report.plan = plan_runs(runs, keep=keep, current_run_id=current_run_id, workflow_id=workflow_id,
                            excluded_workflow_ids=excluded, release_run_ids=release_ids)
    if execute:
        original = {run['id']: run for run in runs}
        for run_id in list(report.plan.candidate_ids):
            resource = f'/actions/runs/{run_id}'
            current = client.request(resource, missing_ok=True)
            if current is None:
                report.gone_ids.append(run_id)
                continue
            validate_run(current, original[run_id]['workflow_id'])
            if (current['id'] != run_id or preservation_reason(current, current_run_id)
                    or any(current[key] != original[run_id][key]
                           for key in ('created_at', 'event', 'display_title', 'status', 'conclusion'))
                    or any(current.get(key) != original[run_id].get(key)
                           for key in ('run_attempt', 'head_sha'))):
                raise CleanupError('An old run changed before deletion; remaining runs preserved')
            # A draft may have been published after the initial plan. Refresh
            # the complete Release inventory immediately before every DELETE;
            # a failed page aborts cleanup rather than using stale protection.
            release_ids.update(fetch_release_run_ids(client, runs))
            finished = set(report.deleted_ids + report.gone_ids)
            refreshed = plan_runs([run for run in runs if run['id'] not in finished], keep=keep,
                                  current_run_id=current_run_id, workflow_id=workflow_id,
                                  excluded_workflow_ids=excluded, release_run_ids=release_ids)
            report.plan = refreshed
            report.plan.candidate_ids = list(report.deleted_ids + report.gone_ids) + refreshed.candidate_ids
            if run_id not in refreshed.candidate_ids:
                continue
            result = client.request(resource, method='DELETE')
            if result == 'deleted':
                report.deleted_ids.append(run_id)
            elif result == 'gone':
                report.gone_ids.append(run_id)
            else:
                raise CleanupError('GitHub run deletion returned an invalid result')
    report.finished = True
    return report


def write_summary(report, path):
    data = report.as_dict()
    temporary = {key: value for key, value in data['preserved'].items() if value != 'published release'}
    lines = ['## 清理历史记录', '',
             '- 模式：' + ('执行清理' if report.execute else '仅预览'),
             '- 状态：' + ('失败' if data['status'] == 'failed' else '完成'),
             '- 范围：' + ('仓库全部工作流' if report.workflow == 'all' else report.workflow),
             f'- 保留普通成功记录：{len(data["kept_ids"])} 条（上限 {report.keep} 条，发行记录不占名额）',
             f'- 保护发行对应运行：{sum(value == "published release" for value in data["preserved"].values())} 条（含日志及尚未过期的 Actions 产物）',
             '- 不计入保留名额的工作流 ID：' + (', '.join(map(str, data['excluded_workflow_ids'])) or '无'),
             f'- 暂时保护：{len(temporary)} 条（当前或未完成运行，不取消任务）',
             f'- 待清理旧运行：{len(data["candidate_ids"])} 条',
             f'- 本次已删除：{len(data["deleted_ids"])} 条',
             f'- 已不存在：{len(data["gone_ids"])} 条',
             f'- 剩余候选：{len(data["remaining_candidate_ids"])} 条',
             '- 删除运行记录时，其关联的 Actions 产物也会删除。',
             '- 所有发行版（Release）及其全部附件均保留，不受两条记录限制。',
             '- 发行对应的 Actions 运行也保留；无法确认发行来源或读取发行失败时停止清理。',
             '- 缓存和分支均保留。']
    for label, key in (('保留成功运行 ID', 'kept_ids'), ('保护运行 ID', 'preserved'),
                       ('发行来源运行 ID', 'release_run_ids'),
                       ('候选运行 ID', 'candidate_ids'),
                       ('本次删除运行 ID', 'deleted_ids'), ('已不存在运行 ID', 'gone_ids')):
        lines.append(f'- {label}：' + (', '.join(map(str, data[key])) or '无'))
    if report.error:
        lines.append('- 错误：' + report.error)
    with Path(path).open('a', encoding='utf-8', newline='\n') as handle:
        handle.write('\n'.join(lines) + '\n\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', ''))
    parser.add_argument('--workflow', default=DEFAULT_WORKFLOW,
                        help='all repository workflows (default), or one workflow filename/ID')
    parser.add_argument('--keep', type=int, default=DEFAULT_KEEP,
                        help='ordinary successful non-excluded runs to retain in addition to all published Release runs (default: 2)')
    parser.add_argument('--exclude-workflow', default=DEFAULT_EXCLUDE_WORKFLOW,
                        help='maintenance workflow excluded from successful retention slots '
                             '(default: cleanup-build-history.yml)')
    parser.add_argument('--execute', action='store_true', help='delete candidates; default is read-only')
    parser.add_argument('--summary', default=os.environ.get('GITHUB_STEP_SUMMARY'))
    args = parser.parse_args(argv)
    report = CleanupReport(args.repo, args.workflow, args.keep, args.execute)
    try:
        validate_repository(args.repo)
        validate_workflow(args.workflow)
        if args.exclude_workflow is not None:
            validate_workflow(args.exclude_workflow)
            if args.exclude_workflow == 'all':
                raise CleanupError('Only one exact maintenance workflow can be excluded')
        if args.keep < 1:
            raise CleanupError('At least one successful workflow run must be retained')
        current_run_id = os.environ.get('GITHUB_RUN_ID') or None
        if current_run_id is not None and not re.fullmatch(r'[1-9][0-9]*', current_run_id):
            raise CleanupError('GITHUB_RUN_ID is invalid')
        token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN', '')
        client = GitHub(args.repo, token)
        cleanup(client, args.workflow, keep=args.keep, current_run_id=current_run_id,
                execute=args.execute, report=report, exclude_workflow=args.exclude_workflow)
    except CleanupError as error:
        report.error = str(error)
    if args.summary:
        try:
            write_summary(report, args.summary)
        except (OSError, ValueError):
            report.error = report.error or 'Could not write the cleanup summary'
    print(json.dumps(report.as_dict(), ensure_ascii=False))
    if report.error:
        print('Historical workflow run cleanup failed: ' + report.error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
