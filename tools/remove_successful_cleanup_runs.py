#!/usr/bin/env python3
"""Remove completed successful history-cleanup rows on a later repository run.

The only selectable workflow is cleanup-build-history.yml. The default is a
read-only preview; --execute enables deletion after complete listing and
per-run identity checks. Builds, unsuccessful cleanup runs, and active runs
are never deletion targets.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
import re
import sys

from cleanup_workflow_runs import (CleanupError, GitHub, fetch_workflow_runs,
                                   validate_repository, validate_run)


WORKFLOW = 'cleanup-build-history.yml'
IDENTITY_FIELDS = ('id', 'workflow_id', 'status', 'conclusion', 'created_at',
                   'event', 'display_title', 'run_attempt', 'head_sha')


@dataclass
class RemovalReport:
    repository: str
    execute: bool
    workflow_id: int | None = None
    candidate_ids: list[int] = field(default_factory=list)
    preserved_ids: list[int] = field(default_factory=list)
    deleted_ids: list[int] = field(default_factory=list)
    gone_ids: list[int] = field(default_factory=list)
    error: str | None = None
    finished: bool = False

    def as_dict(self):
        return {'repository': self.repository, 'workflow': WORKFLOW,
                'workflow_id': self.workflow_id,
                'mode': 'execute' if self.execute else 'dry-run',
                'status': 'completed' if self.finished and not self.error else 'failed',
                'candidate_count': len(self.candidate_ids),
                'preserved_count': len(self.preserved_ids),
                'deleted_ids': self.deleted_ids, 'gone_ids': self.gone_ids,
                'error': self.error}


def remove_runs(client, *, execute=False, current_run_id=None, report=None):
    """Inspect just the fixed cleanup workflow; no other workflow can be chosen."""
    validate_repository(client.repository)
    if current_run_id is not None and not re.fullmatch(r'[1-9][0-9]*', str(current_run_id)):
        raise CleanupError('GITHUB_RUN_ID is invalid')
    report = report if report is not None else RemovalReport(client.repository, execute)
    workflow_id, runs = fetch_workflow_runs(client, WORKFLOW)
    report.workflow_id = workflow_id
    # fetch_workflow_runs validates the entire paginated inventory first.
    ordered = sorted(runs, key=lambda run: (validate_run(run, workflow_id), run['id']), reverse=True)
    original = {}
    for run in ordered:
        run_id = run['id']
        if (str(run_id) == str(current_run_id) or run['status'] != 'completed'
                or run['conclusion'] != 'success'):
            report.preserved_ids.append(run_id)
        else:
            report.candidate_ids.append(run_id)
            original[run_id] = run
    if execute:
        for run_id in report.candidate_ids:
            path = f'/actions/runs/{run_id}'
            current = client.request(path, missing_ok=True)
            if current is None:
                report.gone_ids.append(run_id)
                continue
            validate_run(current, workflow_id)
            if (str(current['id']) == str(current_run_id)
                    or any(current.get(key) != original[run_id].get(key) for key in IDENTITY_FIELDS)):
                raise CleanupError('A successful cleanup run changed before deletion; remaining runs preserved')
            result = client.request(path, method='DELETE')
            if result == 'deleted':
                report.deleted_ids.append(run_id)
            elif result == 'gone':
                report.gone_ids.append(run_id)
            else:
                raise CleanupError('GitHub cleanup-run deletion returned an invalid result')
    report.finished = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', ''))
    parser.add_argument('--execute', action='store_true', help='delete successful cleanup rows; default is preview')
    args = parser.parse_args(argv)
    report = RemovalReport(args.repo, args.execute)
    try:
        validate_repository(args.repo)
        current_run_id = os.environ.get('GITHUB_RUN_ID') or None
        if current_run_id is not None and not re.fullmatch(r'[1-9][0-9]*', current_run_id):
            raise CleanupError('GITHUB_RUN_ID is invalid')
        token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN', '')
        client = GitHub(args.repo, token)
        remove_runs(client, execute=args.execute, current_run_id=current_run_id, report=report)
    except CleanupError as error:
        report.error = str(error)
    print(json.dumps(report.as_dict(), ensure_ascii=False))
    if report.error:
        print('Successful cleanup-row maintenance failed: ' + report.error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
