"""Freeze selected sources, skipping only missing stable releases in a group."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from manager_variants import selected_managers
from resolve_build_sources import (NoStableRelease, REPOSITORIES, SHA, TIERS, TIER_ALIASES,
                                   SourceResolutionError, resolve_sources, validate_ref)

ROOT = Path(__file__).resolve().parents[1]
SKIP_REASON = '官方尚未发布正式版'
SOURCE_FIELDS = {'ksu_ref', 'ksu_repo', 'ksu_short', 'ksu_source_branch', 'effective_ksu_branch'}
FIELDS = {'schema_version', 'selection', 'tier', 'active_managers', 'skipped_managers', 'sources'}


def effective_tier(tier):
    if not isinstance(tier, str) or tier not in TIERS:
        raise ValueError('Unsupported manager version channel')
    return TIER_ALIASES.get(tier, tier)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate manager selection field')
        result[key] = value
    return result


def validate_receipt(document, selection=None):
    if isinstance(document, str):
        if len(document) > 32768:
            raise ValueError('Manager selection receipt is too large')
        document = json.loads(document, object_pairs_hook=_unique_object)
    if (not isinstance(document, dict) or set(document) != FIELDS
            or type(document['schema_version']) is not int or document['schema_version'] != 1):
        raise ValueError('Invalid manager selection receipt')
    requested = selected_managers(document['selection'])
    if selection is not None and document['selection'] != selection:
        raise ValueError('Manager selection receipt belongs to another requested selection')
    tier = effective_tier(document['tier'])
    active, skipped, sources = (document[key] for key in ('active_managers', 'skipped_managers', 'sources'))
    if (not isinstance(active, list) or any(not isinstance(name, str) for name in active)
            or active != [name for name in requested if name in active]):
        raise ValueError('Active managers must be an ordered subset of the requested managers')
    omitted = [name for name in requested if name not in active]
    if not isinstance(skipped, list) or len(skipped) != len(omitted):
        raise ValueError('Every omitted manager requires an explicit missing-release reason')
    if omitted and (len(requested) < 2 or tier != 'Stable(标准)'):
        raise ValueError('Only combined stable builds can skip an unavailable official release')
    for name, row in zip(omitted, skipped):
        if row != {'variant': name, 'reason': SKIP_REASON}:
            raise ValueError('Unexpected skipped-manager identity or reason')
    if not isinstance(sources, dict) or set(sources) != set(active):
        raise ValueError('Fixed source records do not match the active managers')
    for name in active:
        source = sources[name]
        if (not isinstance(source, dict) or set(source) != SOURCE_FIELDS
                or not isinstance(source['ksu_ref'], str) or not SHA.fullmatch(source['ksu_ref'])
                or source['ksu_ref'] != source['ksu_ref'].lower()
                or source['ksu_short'] != source['ksu_ref'][:7]
                or source['ksu_repo'] != REPOSITORIES[name]
                or source['effective_ksu_branch'] != tier):
            raise ValueError('Invalid fixed official source for selected manager: ' + name)
        validate_ref(source['ksu_source_branch'], empty=tier == 'Custom(自定义)')
    return document


def active_managers(selection, resolution=None):
    if resolution is None:
        return selected_managers(selection)
    return tuple(validate_receipt(resolution, selection=selection)['active_managers'])


def resolve_selection(selection, tier, custom_ref='', susfs=False):
    requested = selected_managers(selection)
    channel = effective_tier(tier)
    active, skipped, sources = [], [], {}
    for variant in requested:
        try:
            source = resolve_sources(ROOT, variant, tier, custom_ref, susfs)
        except NoStableRelease:
            if len(requested) < 2 or channel != 'Stable(标准)':
                raise
            skipped.append({'variant': variant, 'reason': SKIP_REASON})
            continue
        active.append(variant)
        sources[variant] = source
    return validate_receipt({'schema_version': 1, 'selection': selection, 'tier': tier,
                             'active_managers': active, 'skipped_managers': skipped, 'sources': sources})


def _outputs(values):
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as stream:
            for key, value in values.items():
                stream.write(key + '=' + value + '\n')


def selection_summary(receipt):
    receipt = validate_receipt(receipt)
    lines = ['### 本次管理器选择', '', '参与构建：' + ('、'.join(receipt['active_managers']) or '无') + '。']
    for row in receipt['skipped_managers']:
        lines.append('- 跳过 ' + row['variant'] + '：' + row['reason'] + '。')
    if not receipt['active_managers']:
        lines += ['', '所选管理器均未发布正式版，本次不编译、不发行。']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection')
    parser.add_argument('--tier')
    parser.add_argument('--custom-ref', default='')
    parser.add_argument('--susfs', choices=('true', 'false'), default='false')
    parser.add_argument('--source-for')
    args = parser.parse_args(argv)
    try:
        if args.source_for:
            receipt = validate_receipt(os.environ.get('MANAGER_RESOLUTION', ''))
            if args.source_for not in receipt['sources']:
                raise ValueError('The requested manager was not selected for this build')
            source = receipt['sources'][args.source_for]
            _outputs(source)
            print('Fixed official source: ' + args.source_for + '@' + source['ksu_ref'])
        else:
            receipt = resolve_selection(args.selection, args.tier, args.custom_ref, args.susfs == 'true')
            encoded = json.dumps(receipt, ensure_ascii=False, separators=(',', ':'))
            _outputs({'active_managers': json.dumps(receipt['active_managers'], separators=(',', ':')),
                      'active_count': str(len(receipt['active_managers'])), 'resolution': encoded})
            summary = selection_summary(receipt)
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
                    stream.write(summary)
            print(summary)
    except (SourceResolutionError, ValueError, OSError) as error:
        print('Manager selection failed: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
