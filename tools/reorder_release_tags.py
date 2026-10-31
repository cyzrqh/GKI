"""Compact owned published r1.x releases without replacing releases or assets.

Only unoccupied numbered tag refs may be reassigned. Release IDs, exact
producer commits, notes, titles and complete asset inventories are preserved.
Interrupted compaction is resumable; unknown or actively published data stops
mutation. The publishing jobs and this workflow share a concurrency group.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re

from release_tags import (REPOSITORY, SHA, VERSION_TAG, list_releases, list_tag_refs,
                          release_run_identity)
from transfer_ksu_snapshot import GitHub as Transport

NUMBER = r'r1\.(?:0|[1-9][0-9]{0,8})'
PUBLISHERS = ('kernel-full-feature-matrix.yml', 'oplus-full-feature-matrix.yml')
PUBLISH_JOBS = {'整理产物与校验（全量成功才发行）', '整理原生文件与详细配置（全量成功才发行）'}
PRESERVED = ('id', 'target_commitish', 'name', 'body', 'draft', 'prerelease', 'published_at', 'immutable')


class Deferred(ValueError):
    """An active publication owns the current numbering; retry on its completion."""


class MetadataTransport(Transport):
    def request(self, url, **kwargs):
        kwargs.setdefault('limit', 4 * 1024 * 1024)
        return super().request(url, **kwargs)


class GitHub:
    def __init__(self, repository: str, *, transport=None):
        if not REPOSITORY.fullmatch(repository) or any(part in ('.', '..') for part in repository.split('/')):
            raise ValueError('Invalid release repository')
        self.repository = repository
        self.transport = transport or MetadataTransport(repository)

    def request(self, resource: str, *, method='GET', data=None, missing=False):
        ref = re.fullmatch(r'/git/refs/tags/' + NUMBER, resource)
        release = re.fullmatch(r'/releases/[1-9][0-9]*', resource)
        read = (resource in ('/git/matching-refs/tags/r1.', '/releases/latest')
                or re.fullmatch(r'/releases\?per_page=20&page=[1-9][0-9]*', resource)
                or release or re.fullmatch(r'/git/ref/tags/' + NUMBER, resource)
                or re.fullmatch(r'/releases/[1-9][0-9]*/assets\?per_page=100&page=[1-9][0-9]*', resource)
                or re.fullmatch(r'/actions/workflows/(?:kernel-full-feature-matrix|oplus-full-feature-matrix)\.yml/runs\?status=in_progress&per_page=100&page=[1-9][0-9]*', resource)
                or re.fullmatch(r'/actions/runs/[1-9][0-9]*/jobs\?per_page=100&page=[1-9][0-9]*', resource))
        write_ref = (method == 'POST' and resource == '/git/refs' and isinstance(data, dict)
                     and set(data) == {'ref', 'sha'} and isinstance(data['ref'], str)
                     and re.fullmatch('refs/tags/' + NUMBER, data['ref']) and SHA.fullmatch(str(data['sha'])))
        update_ref = (method == 'PATCH' and ref and isinstance(data, dict) and set(data) == {'sha', 'force'}
                      and SHA.fullmatch(str(data['sha'])) and data['force'] is True)
        update_release = (method == 'PATCH' and release and isinstance(data, dict)
                          and (set(data) == {'tag_name'} and VERSION_TAG.fullmatch(str(data['tag_name']))
                               or data == {'make_latest': 'true'}))
        if not (method == 'GET' and data is None and read or write_ref or update_ref
                or update_release or method == 'DELETE' and ref and data is None):
            raise ValueError('Operation is outside numbered release-tag maintenance')
        return self.transport.json(resource, method=method, document=data, missing=missing)


def inventory(api, release_id: int) -> list[tuple]:
    assets = []
    for page in range(1, 101):
        batch = api.request(f'/releases/{release_id}/assets?per_page=100&page={page}')
        if not isinstance(batch, list) or len(batch) > 100:
            raise ValueError('Invalid release asset inventory')
        for asset in batch:
            if (not isinstance(asset, dict) or type(asset.get('id')) is not int or asset['id'] < 1
                    or not isinstance(asset.get('name'), str) or type(asset.get('size')) is not int
                    or asset['size'] < 0 or asset.get('state') != 'uploaded'):
                raise ValueError('An incomplete or unknown release asset must not be retagged')
            assets.append((asset['id'], asset['name'], asset['size'], asset.get('digest'), asset.get('label'), asset.get('content_type')))
        if len(batch) < 100:
            if len({asset[0] for asset in assets}) != len(assets):
                raise ValueError('Duplicate release assets')
            return sorted(assets)
    raise ValueError('Release asset inventory exceeds its lookup budget')


def no_active_publisher(api, current_run_id=''):
    for workflow in PUBLISHERS:
        for page in range(1, 11):
            document = api.request(f'/actions/workflows/{workflow}/runs?status=in_progress&per_page=100&page={page}')
            runs = document.get('workflow_runs') if isinstance(document, dict) else None
            if not isinstance(runs, list) or len(runs) > 100:
                raise ValueError('Cannot verify active release publishers')
            for run in runs:
                if not isinstance(run, dict) or type(run.get('id')) is not int:
                    raise ValueError('Invalid active workflow identity')
                if str(run['id']) == str(current_run_id):
                    continue
                for job_page in range(1, 21):
                    document = api.request(f"/actions/runs/{run['id']}/jobs?per_page=100&page={job_page}")
                    jobs = document.get('jobs') if isinstance(document, dict) else None
                    if not isinstance(jobs, list) or len(jobs) > 100 or any(not isinstance(job, dict) for job in jobs):
                        raise ValueError('Cannot verify active publishing jobs')
                    if any(job.get('name') in PUBLISH_JOBS and job.get('status') == 'in_progress' for job in jobs):
                        raise Deferred('An existing publication is still running; no tags were changed by this check')
                    if len(jobs) < 100:
                        break
                else:
                    raise ValueError('Publisher job listing exceeds its lookup budget')
            if len(runs) < 100:
                break
        else:
            raise ValueError('Active publishers exceed the lookup budget')


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('Published release timestamp is missing')
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Release timestamp has no timezone')
    return result


def snapshot(api) -> tuple[list[dict], dict[str, str]]:
    releases, refs = [], list_tag_refs(api)
    if any(not SHA.fullmatch(value) for value in refs.values()):
        raise ValueError('Annotated numbered tags require manual review before reordering')
    for release in list_releases(api):
        if not VERSION_TAG.fullmatch(release['tag_name']):
            continue
        if release['draft']:
            raise Deferred('A numbered draft publication is unfinished; its reserved tag is preserved')
        identity = release_run_identity(release)
        if (identity['repository'].casefold() != api.repository.casefold()
                or refs.get(release['tag_name']) != identity['head_sha']
                or release.get('immutable') is not False or type(release.get('prerelease')) is not bool):
            raise ValueError('Release ownership, mutable state or exact commit could not be verified')
        timestamp(release.get('published_at'))
        releases.append(release)
    releases.sort(key=lambda row: (timestamp(row['published_at']), row['id']))
    # A compacting migration moves into holes in chronological order. Reject
    # manually permuted/cyclic labels before changing even one tag.
    owners = {release['tag_name']: release['id'] for release in releases}
    for index, release in enumerate(releases):
        tag = 'r1.' + str(index)
        if owners.get(tag) not in (None, release['id']):
            raise ValueError('Chronological release labels are cyclic; automatic compaction stopped')
        owners.pop(release['tag_name'], None)
        owners[tag] = release['id']
    return releases, refs


def same_release(expected, actual, tag):
    return (isinstance(actual, dict) and actual.get('tag_name') == tag
            and all(actual.get(field) == expected.get(field) for field in PRESERVED))


def read_ref(api, tag):
    value = api.request('/git/ref/tags/' + tag, missing=True)
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get('ref') != 'refs/tags/' + tag
            or not isinstance(value.get('object'), dict) or value['object'].get('type') != 'commit'
            or not SHA.fullmatch(str(value['object'].get('sha')))):
        raise ValueError('Only verified lightweight numbered tag refs may be changed')
    return value['object']['sha']


def reorder(api, *, execute=False, current_run_id='', report_path: Path | None = None) -> dict:
    no_active_publisher(api, current_run_id)
    releases, refs = snapshot(api)
    latest = api.request('/releases/latest', missing=True)
    rows = [{'release_id': release['id'], 'old': release['tag_name'], 'new': 'r1.' + str(index),
             'sha': release['target_commitish'], 'name': release.get('name')}
            for index, release in enumerate(releases)]
    wanted = {row['new'] for row in rows}
    orphan_tags = sorted(set(refs) - wanted, key=lambda tag: int(VERSION_TAG.fullmatch(tag)[1]))
    result = {'repository': api.repository, 'mode': 'execute' if execute else 'preview',
              'state': 'planned', 'mapping': rows, 'deleted_orphan_tags': [], 'old_refs': refs}
    assets = {release['id']: inventory(api, release['id']) for release in releases}
    result['assets'] = assets
    if report_path:
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    if not execute:
        return result
    no_active_publisher(api, current_run_id)
    fresh, fresh_refs = snapshot(api)
    if (len(fresh) != len(releases) or fresh_refs != refs
            or any(not same_release(before, after, before['tag_name']) for before, after in zip(releases, fresh))):
        raise ValueError('Release inventory changed after planning; no labels were changed')
    for release, row in zip(releases, rows):
        if row['old'] == row['new']:
            continue
        actual = api.request('/releases/' + str(release['id']))
        if not same_release(release, actual, row['old']) or inventory(api, release['id']) != assets[release['id']]:
            raise ValueError('Release or asset changed before retagging; remaining labels preserved')
        occupied = [item for item in list_releases(api) if item['tag_name'] == row['new']]
        if occupied:
            raise ValueError('Target tag is attached to another release; no live tag was overwritten')
        if read_ref(api, row['old']) != row['sha']:
            raise ValueError('Original release tag changed before retagging')
        current = read_ref(api, row['new'])
        if current != refs.get(row['new']):
            raise ValueError('Destination tag changed after planning')
        if current != row['sha']:
            if current is None:
                api.request('/git/refs', method='POST', data={'ref': 'refs/tags/' + row['new'], 'sha': row['sha']})
            else:
                api.request('/git/refs/tags/' + row['new'], method='PATCH', data={'sha': row['sha'], 'force': True})
        if read_ref(api, row['new']) != row['sha']:
            raise ValueError('Destination tag does not point to the preserved producer commit')
        api.request('/releases/' + str(release['id']), method='PATCH', data={'tag_name': row['new']})
        actual = api.request('/releases/' + str(release['id']))
        if not same_release(release, actual, row['new']) or inventory(api, release['id']) != assets[release['id']]:
            raise ValueError('Release preservation check failed; no remaining tags were changed')
    # Re-read all releases before pruning only the originally observed refs.
    final, final_refs = snapshot(api)
    if ([release['id'] for release in final] != [release['id'] for release in releases]
            or [release['tag_name'] for release in final] != [row['new'] for row in rows]):
        raise ValueError('Published releases changed during reordering; orphan cleanup stopped')
    no_active_publisher(api, current_run_id)
    for tag in orphan_tags:
        if final_refs.get(tag) != refs[tag] or read_ref(api, tag) != refs[tag]:
            raise ValueError('An orphan ref changed before removal; remaining refs preserved')
        api.request('/git/refs/tags/' + tag, method='DELETE')
        result['deleted_orphan_tags'].append(tag)
    if isinstance(latest, dict) and any(release['id'] == latest.get('id') for release in final):
        actual_latest = api.request('/releases/latest', missing=True)
        if not isinstance(actual_latest, dict) or actual_latest.get('id') != latest['id']:
            api.request('/releases/' + str(latest['id']), method='PATCH', data={'make_latest': 'true'})
    final, final_refs = snapshot(api)
    if set(final_refs) != wanted:
        raise ValueError('New numbered refs appeared during maintenance; they were preserved for the next run')
    for release in final:
        if inventory(api, release['id']) != assets[release['id']]:
            raise ValueError('Final release asset preservation check failed')
    result['state'] = 'completed'
    if report_path:
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''))
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        result = reorder(GitHub(args.repository), execute=args.execute,
                         current_run_id=os.environ.get('GITHUB_RUN_ID', ''), report_path=args.report)
    except Deferred as error:
        print('Release label reordering deferred: ' + str(error))
        return 75
    except (ValueError, OSError):
        print('::error::Release tag maintenance stopped; review the preserved plan before retrying.')
        return 1
    print(json.dumps({key: value for key, value in result.items() if key not in ('assets', 'old_refs')}, ensure_ascii=False))
    if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(summary).open('a', encoding='utf-8') as output:
            output.write('\n发行版标签重排：' + result['state'] + '\n\n| 原标签 | 新标签 |\n|---|---|\n')
            for row in result['mapping']:
                if row['old'] != row['new']:
                    output.write('| ' + row['old'] + ' | ' + row['new'] + ' |\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
