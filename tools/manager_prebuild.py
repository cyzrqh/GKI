#!/usr/bin/env python3
"""Select immutable official manager sources to prebuild without changing source pins."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from check_official_updates import UpdateError, validate_snapshot, write_outputs
from manager_lkm_jobs import load_ddk_matrix
from manager_variants import MANAGERS, manager_profile
from resolve_build_sources import (NoStableRelease, SHA, SourceResolutionError, compare_sukisu_channels,
                                   resolve_sources, validate_ref)


ROOT = Path(__file__).resolve().parents[1]
STABLE = 'Stable(标准)'
LATEST = 'Latest(最新)'
CHANNELS = {STABLE: '正式版', LATEST: '最新版'}
MAX_STATE = 8192
MAX_SNAPSHOT = 32768
STATE_VERSION = 2
ChannelSources = dict[str, dict[str, tuple[str, str]]]


def slot_for(variant: str, tier: str) -> str:
    return variant.lower() + ('_stable' if tier == STABLE else '_latest')


def source(reference: str, branch: str) -> tuple[str, str]:
    if not isinstance(reference, str) or not SHA.fullmatch(reference):
        raise ValueError('预编译 APK 需要完整的官方源码提交 SHA')
    return reference.lower(), validate_ref(branch)


def snapshot_channels(document: dict) -> ChannelSources:
    """Use only verified candidate data and compare SukiSU's two frozen branch heads."""
    snapshot = validate_snapshot(document)
    if set(snapshot) != {'schema_version', 'susfs', *(variant.lower() for variant in MANAGERS)}:
        raise ValueError('预编译需要六种管理器的完整官方检查结果')
    channels = {}
    for variant in MANAGERS:
        item = snapshot[variant.lower()]
        latest = source(item['head'], item['branch'])
        if variant == 'SukiSU' and (development := item.get('development')):
            latest = compare_sukisu_channels(manager_profile(variant)['repository'],
                                             latest[1], latest[0], development['head'])
        release = item['release']
        selected = {LATEST: latest}
        if release is not None:
            selected[STABLE] = source(release['commit'], release['tag'])
        channels[variant] = selected
    return channels


def current_channels(root: Path) -> ChannelSources:
    """Discover available releases and heads through the normal build resolver."""
    channels = {}
    for variant in MANAGERS:
        selected = {}
        for tier in CHANNELS:
            try:
                item = resolve_sources(root, variant, tier, susfs=True)
            except NoStableRelease:
                if tier != STABLE:
                    raise
                continue
            selected[tier] = source(item['ksu_ref'], item['ksu_source_branch'])
        channels[variant] = selected
    return channels


def state_document(channels: ChannelSources) -> dict:
    return {'schema_version': STATE_VERSION, 'channels': {
        variant: {tier: {'reference': ref, 'source_branch': branch} for tier, (ref, branch) in selected.items()}
        for variant, selected in channels.items()
    }}


def validate_state(document: object) -> ChannelSources:
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'channels'}
            or type(document['schema_version']) is not int or document['schema_version'] != STATE_VERSION
            or not isinstance(document['channels'], dict) or set(document['channels']) != set(MANAGERS)):
        raise ValueError('无效的管理器预编译记录')
    result = {}
    for variant, selected in document['channels'].items():
        if not isinstance(selected, dict) or LATEST not in selected or selected.keys() - CHANNELS.keys():
            raise ValueError('无效的管理器预编译版本记录')
        sources = {}
        for tier, identity in selected.items():
            if not isinstance(identity, dict) or set(identity) != {'reference', 'source_branch'}:
                raise ValueError('无效的管理器预编译源码记录')
            sources[tier] = source(identity['reference'], identity['source_branch'])
        result[variant] = sources
    return result


def load_previous(path: Path) -> ChannelSources:
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_STATE:
            raise ValueError('预编译记录必须是小型普通文件')
        return validate_state(json.loads(path.read_bytes()))
    except (OSError, ValueError, SourceResolutionError):
        print('::warning::上次管理器预编译记录不可用，将重新核对当前安装包缓存', file=sys.stderr)
        return {}


def plan(channels: ChannelSources, previous: ChannelSources) -> tuple[dict, dict]:
    state = state_document(channels)
    validate_state(state)
    rows = []
    for variant in MANAGERS:
        targets = {}
        for tier, label in CHANNELS.items():
            if tier not in channels[variant]:
                continue
            reference, branch = channels[variant][tier]
            # A new release tag on an already built commit can change the
            # Gradle version produced by the official Git describe logic.
            if (reference, branch) == previous.get(variant, {}).get(tier):
                continue
            row = targets.setdefault(reference, {'variant': variant, 'tier': tier,
                                                  'slot': slot_for(variant, tier), 'reference': reference,
                                                  'source_branch': branch, 'channels': []})
            row['channels'].append(label)
        for row in targets.values():
            row['channels'] = ' / '.join(row['channels'])
            rows.append(row)
    return {'include': rows}, state


def compact(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    discover = commands.add_parser('discover')
    discover.add_argument('--candidates-json')
    discover.add_argument('--previous', type=Path)
    record = commands.add_parser('record')
    record.add_argument('--state-json', required=True)
    record.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'record':
            if (len(args.state_json.encode()) > MAX_STATE or args.output.is_symlink()
                    or args.output.exists() or not args.output.parent.is_dir()):
                raise ValueError('无效的预编译记录路径或内容')
            channels = validate_state(json.loads(args.state_json))
            args.output.write_text(compact(state_document(channels)) + '\n',
                                   encoding='utf-8', newline='\n')
        else:
            if args.candidates_json is not None and len(args.candidates_json.encode()) > MAX_SNAPSHOT:
                raise ValueError('官方管理器检查结果超过大小限制')
            selected = (current_channels(ROOT) if args.candidates_json is None
                        else snapshot_channels(json.loads(args.candidates_json)))
            previous = load_previous(args.previous) if args.previous else {}
            matrix, state = plan(selected, previous)
            ddk = load_ddk_matrix(ROOT)
            lkm_matrix = {'include': [{**row, **item} for row in matrix['include'] for item in ddk]}
            outputs = {'matrix': compact(matrix), 'state': compact(state),
                       'lkm_matrix': compact(lkm_matrix),
                       'state_changed': str(selected != previous).lower(),
                       'pending': 'true' if matrix['include'] else 'false'}
            write_outputs(outputs)
            if not os.environ.get('GITHUB_OUTPUT'):
                print(compact(matrix))
            print(f"需预编译 {len(matrix['include'])} 组官方管理器 APK", file=sys.stderr)
    except (OSError, ValueError, SourceResolutionError, UpdateError) as error:
        print(f'::error::管理器 APK 预编译来源检查失败。{error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
