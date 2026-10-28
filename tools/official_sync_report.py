#!/usr/bin/env python3
"""Describe the exact local official-data delta before creating its update PR.

This helper is offline and read-only apart from its explicitly requested report
files. Candidate observations, verified build pins and built APKs are separate
facts; none is presented as a successful kernel build.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from check_official_updates import TARGETS, head_state, validate_snapshot
from matrix import validate_catalog

ROOT = Path(__file__).resolve().parents[1]
FILES = ('data/upstream-candidates.json', 'data/kernels.json', 'build-compatibility.json')
SHA = re.compile(r'[0-9a-f]{40}\Z')
MAX_FILE = 512 * 1024
CHECK_STATES = frozenset(('success', 'failure', 'skipped', 'cancelled', 'unknown'))


class ReportError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def parse_document(raw: bytes):
    if len(raw) > MAX_FILE:
        raise ReportError('Official report input exceeds its size bound')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReportError('Duplicate JSON field in official report input')
            result[key] = value
        return result
    def reject(_):
        raise ReportError('Non-finite number in official report input')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=reject)
    except (UnicodeError, ValueError):
        raise ReportError('Official report input is not bounded strict UTF-8 JSON') from None


def source_snapshot(document: dict | None) -> dict | None:
    if document is None:
        return None
    if not isinstance(document, dict) or type(document.get('schema_version')) is not int or document['schema_version'] != 1:
        raise ReportError('Unsupported official report candidate record')
    snapshot = validate_snapshot(document.get('sources'))
    if document.get('acknowledged_heads') != head_state(snapshot):
        raise ReportError('Official report candidate acknowledgement is inconsistent')
    return snapshot


def observed_sources(snapshot: dict | None) -> dict[tuple[str, str], dict]:
    if snapshot is None:
        return {}
    result = {}
    for key, name in (('sukisu', 'SukiSU'), ('bakasu', 'BakaSU')):
        if key not in snapshot:
            continue
        item = snapshot[key]
        result[(name, '默认分支')] = {'ref': item['branch'], 'commit': item['head']}
        for channel, label in (('release', '正式版'), ('prerelease', '预发行版')):
            if channel not in item:
                continue
            release = item[channel]
            result[(name, label)] = ({'ref': release['tag'], 'commit': release['commit'],
                                     'version': release['tag']} if release else {'available': False})
        if 'development' in item:
            development = item['development']
            result[(name, '开发分支')] = ({'ref': development['branch'], 'commit': development['head']}
                                          if development else {'available': False})
    for target in TARGETS:
        result[('SUSFS', 'gki-' + target)] = {'ref': 'gki-' + target, 'commit': snapshot['susfs'][target]}
    return result


def source_delta(before, after) -> tuple[list[dict], dict]:
    old, new = observed_sources(source_snapshot(before)), observed_sources(source_snapshot(after))
    # A missing channel in an older schema is unobserved, not evidence that an
    # official release/branch was deleted. Its first observed absence is useful
    # state but is not itself an upstream source change.
    changes = [{'source': name, 'channel': channel, 'before': old.get((name, channel)),
                'after': new.get((name, channel)),
                'kind': 'first-observation' if (name, channel) not in old else
                        'not-observed' if (name, channel) not in new else 'changed'}
               for name, channel in sorted(old.keys() | new.keys())
               if old.get((name, channel)) != new.get((name, channel))
               and not ((name, channel) not in old and new.get((name, channel)) == {'available': False})]
    state = [{'source': name, 'channel': channel, **entry} for (name, channel), entry in sorted(new.items())]
    return changes, {'before': [{'source': name, 'channel': channel, **entry} for (name, channel), entry in sorted(old.items())],
                     'after': state}


def catalog_rows(document: dict) -> dict[tuple, dict]:
    validate_catalog(document)
    rows = {}
    for entry in document['entries']:
        if set(entry) - {'android_version', 'kernel_version', 'kernel', 'date', 'ref', 'revision'}:
            raise ReportError('Official catalog report contains unknown entry fields')
        key = tuple(entry[name] for name in ('android_version', 'kernel_version', 'kernel', 'date'))
        if key in rows:
            raise ReportError('Official catalog report contains duplicate target identities')
        rows[key] = entry
    return rows


def row_key(entry: dict) -> tuple:
    return (int(entry['android_version'][7:]), tuple(map(int, entry['kernel'].split('.'))), entry['date'])


def selected_rows(rows: dict) -> dict[tuple, dict]:
    selected = {}
    for row in rows.values():
        if row['date'] == 'lts':
            continue
        key = row['android_version'], row['kernel']
        if key not in selected or row['date'] > selected[key]['date']:
            selected[key] = row
    return selected


def catalog_delta(before: dict, after: dict) -> dict:
    old, new = catalog_rows(before), catalog_rows(after)
    selected_before, selected_after = selected_rows(old), selected_rows(new)
    return {'before_count': len(old), 'after_count': len(new),
            'selected_before_count': len(selected_before), 'selected_after_count': len(selected_after),
            'added': sorted((new[key] for key in new.keys() - old.keys()), key=row_key),
            'removed': sorted((old[key] for key in old.keys() - new.keys()), key=row_key),
            'updated': sorted(({'before': old[key], 'after': new[key]} for key in old.keys() & new.keys()
                               if old[key] != new[key]), key=lambda change: row_key(change['after'])),
            'selected_changes': sorted(({'before': selected_before.get(key), 'after': selected_after.get(key)}
                                        for key in selected_before.keys() | selected_after.keys()
                                        if selected_before.get(key) != selected_after.get(key)),
                                       key=lambda change: row_key(change['after'] or change['before']))}


def compatibility_state(document: dict) -> dict:
    profile = document.get('sukisu_susfs') if isinstance(document, dict) else None
    if (not isinstance(profile, dict) or not isinstance(profile.get('version'), str)
            or not re.fullmatch(r'v?[0-9]+\.[0-9]+\.[0-9]+', profile['version'])
            or not isinstance(profile.get('pins'), dict) or set(profile['pins']) != set(TARGETS)
            or any(not isinstance(value, str) or not SHA.fullmatch(value) for value in profile['pins'].values())):
        raise ReportError('Invalid verified compatibility metadata in official report')
    source = profile.get('source_ref')
    if source is None:
        refs = profile.get('ksu_commits')
        source = refs[0] if isinstance(refs, list) and refs else None
    if not isinstance(source, str) or not SHA.fullmatch(source):
        raise ReportError('Invalid verified manager source in official report')
    return {'sukisu_source_ref': source, 'susfs_version': profile['version'], 'susfs_pins': profile['pins'],
            'profile_sha256': hashlib.sha256(canonical(document)).hexdigest()}


def build_report(before_candidate, after_candidate, before_catalog, after_catalog,
                 before_profile, after_profile, *, base_ref: str, checks: dict | None = None) -> dict:
    if not isinstance(base_ref, str) or not SHA.fullmatch(base_ref):
        raise ReportError('Official report requires a fixed base commit')
    checks = checks or {'official': 'success', 'catalog': 'success'}
    if set(checks) != {'official', 'catalog'} or any(value not in CHECK_STATES for value in checks.values()):
        raise ReportError('Invalid official report check outcomes')
    sources, observations = source_delta(before_candidate, after_candidate)
    catalog = catalog_delta(before_catalog, after_catalog)
    old_profile, new_profile = compatibility_state(before_profile), compatibility_state(after_profile)
    if (checks['official'] != 'success' and sources or checks['catalog'] != 'success'
            and any(catalog[key] for key in ('added', 'removed', 'updated'))):
        raise ReportError('Unchecked data cannot be described as synchronized updates')
    report = {'schema_version': 1, 'base_ref': base_ref, 'checks': checks,
              'source_changes': sources, 'source_observations': observations, 'catalog': catalog,
              'compatibility': {'changed': old_profile != new_profile, 'before': old_profile, 'after': new_profile},
              'manager_version_codes': {'status': 'not-observed',
                                       'reason': 'APK versionCode requires the selected frozen source and build metadata; release labels are recorded separately.'},
              'build_performed': False}
    report['has_changes'] = bool(sources or any(catalog[key] for key in ('added', 'removed', 'updated'))
                                 or report['compatibility']['changed'])
    # The repository base is audit context, not an official-source change.
    # Unrelated code commits must not create duplicate update notifications.
    report['digest'] = hashlib.sha256(canonical({key: value for key, value in report.items()
                                                if key != 'base_ref'})).hexdigest()
    report['summary_markdown'] = render_report(report)
    return report


def code(value: str) -> str:
    return '`' + html.escape(value, quote=False).replace('`', '&#96;').replace('|', '&#124;').replace('@', '&#64;') + '`'


def source_label(value: dict | None) -> str:
    if value is None:
        return '此前未记录／本次未观测'
    if value.get('available') is False:
        return '官方未提供'
    return code(value['ref']) + ' · ' + code(value['commit'][:12])


def target_label(row: dict | None) -> str:
    return '无' if row is None else code(row['android_version'] + '-' + row['kernel'] + '-' + row['date'])


def render_report(report: dict) -> str:
    lines = ['## 本次同步的具体变化', '', '以下比较本次检查前的记录与准备提交的数据，不代表已编译或已通过真机验证。', '',
             '| 来源 | 检查结果 |', '| --- | --- |']
    for key, label in (('official', 'SukiSU / BakaSU / SUSFS'), ('catalog', 'Android GKI 目录')):
        value = report['checks'][key]
        lines.append('| ' + label + ' | ' + ('完成' if value == 'success' else '未完成（' + value + '），不能确认有无更新') + ' |')
    if report['source_changes']:
        lines.extend(['', '### 官方源码和版本标签', '', '| 来源 | 分支／版本 | 之前 | 本次 |', '| --- | --- | --- | --- |'])
        for item in report['source_changes']:
            lines.append(f"| {item['source']} | {item['channel']} | {source_label(item['before'])} | {source_label(item['after'])} |")
    else:
        lines.extend(['', '官方源码观察记录无变化。' if report['checks']['official'] == 'success'
                      else '官方源码检查未完成；沿用原记录。'])
    catalog = report['catalog']
    lines.extend(['', '### GKI 内核目录', '',
                  f"目录记录：{catalog['before_count']} → {catalog['after_count']}；每个完整内核只取最新月份后的可构建目标：{catalog['selected_before_count']} → {catalog['selected_after_count']}。",
                  f"新增 {len(catalog['added'])} 条，移除 {len(catalog['removed'])} 条，既有记录变更 {len(catalog['updated'])} 条。"])
    for key, label in (('added', '新增'), ('removed', '移除')):
        if catalog[key]:
            lines.extend(['', label + '：', ''])
            lines.extend('- ' + target_label(row) for row in catalog[key])
    if catalog['updated']:
        lines.extend(['', '既有目录记录的源码或修订变更：', '', '| 目标 | 之前源码 | 本次源码 | 修订 |', '| --- | --- | --- | --- |'])
        for item in catalog['updated']:
            before, after = item['before'], item['after']
            lines.append('| ' + target_label(after) + ' | ' + code(before.get('ref', '未固定')) + ' | '
                         + code(after.get('ref', '未固定')) + ' | ' + code(before.get('revision', 'r1')) + ' → '
                         + code(after.get('revision', 'r1')) + ' |')
    if catalog['selected_changes']:
        lines.extend(['', '实际构建目标变化（同一完整内核仅保留最新月份）：', '', '| 之前 | 本次 |', '| --- | --- |'])
        for item in catalog['selected_changes']:
            lines.append('| ' + target_label(item['before']) + ' | ' + target_label(item['after']) + ' |')
    compatibility = report['compatibility']
    lines.extend(['', '### 构建适配与管理器', ''])
    if compatibility['changed']:
        lines.append('**构建兼容性基线发生变化；本报告仅记录差异，不能视为已验证适配成功。**')
    else:
        lines.append('本次只同步官方候选观察和内核目录；已验证的 SUSFS 配对及适配基线保持不变。')
    before, after = compatibility['before'], compatibility['after']
    lines.extend(['', '| 已验证构建基线 | 之前 | 本次 |', '| --- | --- | --- |',
                  '| SukiSU 适配提交 | ' + code(before['sukisu_source_ref'][:12]) + ' | ' + code(after['sukisu_source_ref'][:12]) + ' |',
                  '| SUSFS 版本 | ' + code(before['susfs_version']) + ' | ' + code(after['susfs_version']) + ' |'])
    for target in TARGETS:
        lines.append('| SUSFS ' + target + ' | ' + code(before['susfs_pins'][target][:12]) + ' | ' + code(after['susfs_pins'][target][:12]) + ' |')
    lines.extend(['', 'SukiSU / BakaSU 的正式版和最新版在下一次构建时解析所选官方源码。上表中的发布标签为官方版本标签；管理器数字版本号以该次实际 APK 元数据为准，本次检查没有编译 APK 或内核。', ''])
    return '\n'.join(lines)


def git(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *arguments], capture_output=True, timeout=30)
    if result.returncode:
        raise ReportError('Cannot read the fixed local Git report baseline')
    return result.stdout


def read_git_document(root: Path, ref: str, path: str, *, missing: bool = False):
    entry = git(root, 'ls-tree', '-z', ref, '--', path)
    if not entry and missing:
        return None
    if not re.fullmatch(rb'100(?:644|755) blob [0-9a-f]{40}\t' + re.escape(path.encode()) + b'\x00', entry):
        raise ReportError('Official report baseline must contain bounded regular data files')
    return parse_document(git(root, 'show', ref + ':' + path))


def read_worktree_document(root: Path, path: str, *, missing: bool = False):
    target = root / path
    for component in (target, *target.parents):
        if component.is_symlink() or getattr(component, 'is_junction', lambda: False)():
            raise ReportError('Official report inputs cannot traverse filesystem links')
        if component == root:
            break
    if missing and not target.exists():
        return None
    if not target.is_file() or target.stat().st_size > MAX_FILE:
        raise ReportError('Official report input must be a bounded regular file')
    return parse_document(target.read_bytes())


def repository_report(root: Path, base_ref: str = 'HEAD', *, checks: dict | None = None) -> dict:
    root = root.resolve()
    if base_ref != 'HEAD' and not SHA.fullmatch(base_ref):
        raise ReportError('Report baseline must be HEAD or a fixed full commit SHA')
    resolved = git(root, 'rev-parse', '--verify', base_ref + '^{commit}').decode('ascii').strip()
    if not SHA.fullmatch(resolved):
        raise ReportError('Invalid fixed Git report baseline')
    before = [read_git_document(root, resolved, path, missing=path == FILES[0]) for path in FILES]
    after = [read_worktree_document(root, path, missing=path == FILES[0]) for path in FILES]
    return build_report(before[0], after[0], before[1], after[1], before[2], after[2], base_ref=resolved, checks=checks)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--base-ref', default='HEAD')
    parser.add_argument('--report-json', type=Path, required=True)
    parser.add_argument('--report-markdown', type=Path, required=True)
    parser.add_argument('--official-result', choices=sorted(CHECK_STATES), default='success')
    parser.add_argument('--catalog-result', choices=sorted(CHECK_STATES), default='success')
    args = parser.parse_args(argv)
    try:
        report = repository_report(args.root, args.base_ref, checks={'official': args.official_result, 'catalog': args.catalog_result})
        for path, content in ((args.report_json, json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n'),
                              (args.report_markdown, report['summary_markdown'])):
            if path.is_symlink() or path.exists() and not path.is_file():
                raise ReportError('Official report outputs must be regular files')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='\n')
        if os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as stream:
                stream.write('report_json=' + str(args.report_json.resolve()) + '\nreport_markdown='
                             + str(args.report_markdown.resolve()) + '\nreport_digest=' + report['digest']
                             + '\nreport_has_changes=' + str(report['has_changes']).lower() + '\n')
        print(json.dumps({'has_changes': report['has_changes'], 'digest': report['digest'],
                          'source_changes': len(report['source_changes']),
                          'catalog_added': len(report['catalog']['added']), 'catalog_removed': len(report['catalog']['removed'])}))
        return 0
    except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.SubprocessError):
        print('::error::Cannot generate a verified before/after official sync report', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
