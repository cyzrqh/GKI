#!/usr/bin/env python3
"""Preview or delete older, completed check-only rows in the fixed update workflow."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
import re
import sys

from cleanup_workflow_runs import (CleanupError, GitHub, fetch_workflow_runs,
                                   positive_id, validate_repository, validate_run)

WORKFLOW = 'check-upstream-updates.yml'
WORKFLOW_PATH = '.github/workflows/' + WORKFLOW
CHECK_TITLES = frozenset(('仅检查并提醒', '检查上游并提醒'))
IDENTITY_FIELDS = ('id', 'workflow_id', 'status', 'conclusion', 'created_at', 'event',
                   'display_title', 'run_attempt', 'head_sha', 'path', 'head_branch')


@dataclass
class RemovalReport:
    repository: str
    execute: bool
    workflow_id: int | None = None
    current_run_id: int | None = None
    cutoff: str | None = None
    candidate_ids: list[int] = field(default_factory=list)
    preserved_ids: list[int] = field(default_factory=list)
    deleted_ids: list[int] = field(default_factory=list)
    gone_ids: list[int] = field(default_factory=list)
    error: str | None = None
    finished: bool = False

    def as_dict(self):
        return {'repository': self.repository, 'workflow': WORKFLOW, 'workflow_id': self.workflow_id,
                'current_run_id': self.current_run_id, 'cutoff': self.cutoff,
                'mode': 'execute' if self.execute else 'dry-run',
                'status': 'completed' if self.finished and not self.error else 'failed',
                'candidate_ids': self.candidate_ids, 'preserved_ids': self.preserved_ids,
                'deleted_ids': self.deleted_ids, 'gone_ids': self.gone_ids, 'error': self.error}


def check_only(run):
    # An old scheduled run could have synchronized data automatically. An
    # explicit check-only title is needed for both supported trigger kinds.
    return run['event'] in ('schedule', 'workflow_dispatch') and run['display_title'] in CHECK_TITLES


def validate_scope(run, repository, workflow_id):
    created = validate_run(run, workflow_id)
    if 'path' in run and run['path'] != WORKFLOW_PATH:
        raise CleanupError('Update-check run belongs to a different workflow path')
    for field in ('repository', 'head_repository'):
        if field in run and (not isinstance(run[field], dict)
                or not isinstance(run[field].get('full_name'), str)
                or run[field]['full_name'].casefold() != repository.casefold()):
            raise CleanupError('Update-check run belongs to a foreign repository')
    return created


def repository_identity(run):
    return tuple((run.get(field) or {}).get('full_name') for field in ('repository', 'head_repository'))


def complete_identity(run):
    # Historical entries without a fixed attempt/source remain untouched.
    # validate_run checks the type and shape whenever these fields are present.
    return positive_id(run.get('run_attempt')) and isinstance(run.get('head_sha'), str)


def remove_runs(client, *, execute=False, current_run_id=None, report=None):
    validate_repository(client.repository)
    if (isinstance(current_run_id, bool) or not isinstance(current_run_id, (str, int))
            or not re.fullmatch(r'[1-9][0-9]*', str(current_run_id))):
        raise CleanupError('A valid current GITHUB_RUN_ID is required')
    current_run_id = int(current_run_id)
    report = report if report is not None else RemovalReport(client.repository, execute)
    report.current_run_id = current_run_id
    workflow_id, runs = fetch_workflow_runs(client, WORKFLOW)
    # The shared reader validates workflow IDs throughout the complete
    # inventory. Also bind that ID to the fixed filename's authoritative path.
    metadata = client.request('/actions/workflows/' + WORKFLOW)
    if (not isinstance(metadata, dict) or not positive_id(metadata.get('id'))
            or metadata['id'] != workflow_id or metadata.get('path') != WORKFLOW_PATH):
        raise CleanupError('Update-check workflow metadata does not match its fixed path')
    report.workflow_id = workflow_id
    current = client.request(f'/actions/runs/{current_run_id}')
    cutoff = validate_scope(current, client.repository, workflow_id)
    if current['id'] != current_run_id or not complete_identity(current) or not check_only(current):
        raise CleanupError('Current run is not a verified check-only execution')
    report.cutoff = current['created_at']
    # Validate the full scoped inventory before the first DELETE. Both fences
    # are required: rerunning an old check must not delete later records, and
    # same-second or contradictory timestamps are conservatively preserved.
    dated = [(validate_scope(run, client.repository, workflow_id), run) for run in runs]
    original = {}
    for created, run in sorted(dated, key=lambda item: (item[0], item[1]['id']), reverse=True):
        run_id = run['id']
        if (run_id >= current_run_id or created >= cutoff or run['status'] != 'completed'
                or not check_only(run) or not complete_identity(run)):
            report.preserved_ids.append(run_id)
        else:
            report.candidate_ids.append(run_id)
            original[run_id] = run
    if execute:
        for run_id in report.candidate_ids:
            path = f'/actions/runs/{run_id}'
            actual = client.request(path, missing_ok=True)
            if actual is None:
                report.gone_ids.append(run_id)
                continue
            created = validate_scope(actual, client.repository, workflow_id)
            if (any(actual.get(key) != original[run_id].get(key) for key in IDENTITY_FIELDS)
                    or repository_identity(actual) != repository_identity(original[run_id])
                    or actual['id'] >= current_run_id or created >= cutoff
                    or actual['status'] != 'completed' or not check_only(actual) or not complete_identity(actual)):
                raise CleanupError('An update-check run changed before deletion; remaining runs preserved')
            result = client.request(path, method='DELETE')
            if result == 'deleted':
                report.deleted_ids.append(run_id)
            elif result == 'gone':
                report.gone_ids.append(run_id)
            else:
                raise CleanupError('GitHub update-check deletion returned an invalid result')
    report.finished = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', ''))
    parser.add_argument('--execute', action='store_true', help='delete older check-only rows; default is preview')
    args = parser.parse_args(argv)
    report = RemovalReport(args.repo, args.execute)
    try:
        validate_repository(args.repo)
        current = os.environ.get('GITHUB_RUN_ID', '')
        if not re.fullmatch(r'[1-9][0-9]*', current):
            raise CleanupError('A valid current GITHUB_RUN_ID is required')
        client = GitHub(args.repo, os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN', ''))
        remove_runs(client, execute=args.execute, current_run_id=current, report=report)
    except CleanupError:
        report.error = 'Previous update-check cleanup stopped; no further runs were deleted'
    print(json.dumps(report.as_dict(), ensure_ascii=False))
    if report.error:
        print(report.error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
