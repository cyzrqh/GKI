"""Validate fixed official manager sources before merging an update.

Only Git objects, native version formulas and the repository's own integration
patches are inspected. No upstream build/setup script is executed here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

from check_official_updates import validate_snapshot
from manager_variants import MANAGERS, manager_profile
from official_manager_versions import NETWORK, _gradle_version
from prepare_manager_lkm import native_version_code
from snapshot_ksu_source import git, temporary_directory, SnapshotError

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r'[0-9a-f]{40}\Z')


class ManagerUpdateError(ValueError):
    pass


class SourceIntegrationError(ManagerUpdateError):
    def __init__(self, message: str, version_code: int):
        self.version_code = version_code
        super().__init__(message)


def source_repository(cache: Path, variant: str) -> Path:
    repository = manager_profile(variant)['repository']
    cache = Path(cache).absolute()
    if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (cache, *cache.parents)):
        raise ManagerUpdateError('Manager compatibility cache cannot traverse links')
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (variant + '.git')
    identity = {'schema_version': 1, 'repository': repository}
    marker = target / 'gki-manager-compatibility-cache.json'
    if not target.exists():
        target.mkdir()
        git(target, 'init', '--bare', '--quiet')
        marker.write_text(json.dumps(identity), encoding='utf-8')
    if (target.is_symlink() or not target.is_dir() or not marker.is_file() or marker.is_symlink()
            or marker.stat().st_size > 1024 or json.loads(marker.read_text('utf-8')) != identity):
        raise ManagerUpdateError('Manager compatibility cache has an invalid identity')
    paths = list(target.rglob('*'))
    if len(paths) > 100000 or any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in paths):
        raise ManagerUpdateError('Manager compatibility cache contains links or excessive entries')
    if any((target / name).exists() for name in ('shallow', 'info/grafts',
            'objects/info/alternates', 'objects/info/http-alternates')):
        raise ManagerUpdateError('Manager compatibility cache must contain complete independent history')
    (target / 'config').write_text('[core]\n\trepositoryformatversion = 0\n\tbare = true\n'
        '[remote "origin"]\n\turl = https://github.com/' + repository + '.git\n',
        encoding='utf-8', newline='\n')
    if git(target, 'rev-parse', '--is-bare-repository') != 'true':
        raise ManagerUpdateError('Manager compatibility cache must be bare')
    return target


def fetch_reference(repository: Path, reference: str) -> int:
    if not isinstance(reference, str) or not SHA.fullmatch(reference):
        raise ManagerUpdateError('Manager update requires a fixed full official commit')
    local = 'refs/gki-manager-compatibility/' + reference
    try:
        present = git(repository, 'rev-parse', '--verify', local) == reference
    except SnapshotError:
        present = False
    if not present:
        for attempt in range(3):
            try:
                git(repository, *NETWORK, 'fetch', '--quiet', '--no-tags', '--no-recurse-submodules',
                    'origin', reference + ':' + local, timeout=90)
                break
            except SnapshotError:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
    if (git(repository, 'rev-parse', '--is-shallow-repository') != 'false'
            or git(repository, 'rev-parse', '--verify', local) != reference):
        raise ManagerUpdateError('Manager compatibility history differs from the selected official commit')
    count = git(repository, 'rev-list', '--count', reference)
    if not re.fullmatch(r'[1-9][0-9]{0,8}', count):
        raise ManagerUpdateError('Manager compatibility history has an invalid commit count')
    return int(count)


def check_source(repository: Path, variant: str, reference: str, count: int) -> dict:
    with temporary_directory(repository.parent, '.manager-compatibility-') as temporary:
        source = temporary / 'source'
        git(temporary, 'clone', '--quiet', '--shared', '--no-checkout', str(repository), str(source))
        git(source, 'checkout', '--quiet', '--detach', reference)
        git(source, 'remote', 'set-url', 'origin', 'https://github.com/' + manager_profile(variant)['repository'] + '.git')
        code = native_version_code(source, count, variant=variant)
        gradle = (source / 'manager/build.gradle.kts').read_text('utf-8')
        if _gradle_version(gradle, count, variant) != code:
            raise ManagerUpdateError('Official manager and kernel numeric version formulas disagree')
        try:
            if variant == 'SukiSU':
                from apply_susfs23 import apply_reviewed_port, PATCH_PATH
                apply_reviewed_port(source, PATCH_PATH, allow_variants=True)
                checks = ['SUSFS 2.3 完整接入及原生生命周期', 'KPM 原生接口保留', '管理器/内核版本公式一致']
            else:
                from restore_bakasu_kpm import restore_bakasu_kpm, verify_bakasu_kpm_port
                from ensure_bakasu_symbol_lookup import validate_native_symbol_lookup
                restore_bakasu_kpm(source, reference)
                verify_bakasu_kpm_port(source, reference)
                validate_native_symbol_lookup(source, reference, Path('kernel'))
                checks = ['KPM 完整还原及正反向校验', '原生符号接口', '管理器/内核版本公式一致']
        except (OSError, ValueError, RuntimeError) as error:
            raise SourceIntegrationError(str(error), code) from None
        return {'version_code': code, 'checks': checks}


def validate_manager_snapshot(snapshot: dict, *, cache: Path, report_path: Path | None = None,
                              before_snapshot: dict | None = None) -> dict:
    snapshot = validate_snapshot(snapshot)
    previous = validate_snapshot(before_snapshot) if before_snapshot is not None else None
    rows = []
    for variant in MANAGERS:
        record = snapshot.get(variant.lower())
        if record is None:
            continue
        repository = None
        for tier in ('正式版', '测试版'):
            row = {'source': variant, 'channel': tier, 'commit': None, 'version_code': None,
                   'status': 'unavailable', 'checks': [], 'reason': '官方未提供该版本', 'changed': True}
            rows.append(row)
            release = record.get('release')
            if tier == '正式版' and release is None:
                continue
            try:
                repository = repository or source_repository(cache, variant)
                reference = release['commit'] if tier == '正式版' else record['head']
                row['commit'] = reference
                count = fetch_reference(repository, reference)
                if tier == '测试版' and variant == 'SukiSU' and record.get('development'):
                    dev = record['development']['head']
                    dev_count = fetch_reference(repository, dev)
                    if dev_count > count:
                        reference, count = dev, dev_count
                        row['commit'] = reference
                if previous is not None:
                    old = previous.get(variant.lower(), {})
                    old_refs = ({old.get('release', {}).get('commit')} if tier == '正式版' and old.get('release')
                                else {old.get('head'), (old.get('development') or {}).get('head')}
                                if tier == '测试版' else set())
                    row['changed'] = reference not in old_refs
                result = check_source(repository, variant, reference, count)
                row.update(result, status='passed', reason='配套源码验证通过，未编译 APK 或内核')
            except (OSError, ValueError, RuntimeError) as error:
                row.update(status='failure', reason=str(error)[:1500])
                if isinstance(error, SourceIntegrationError):
                    row['version_code'] = error.version_code
    passed = bool(rows) and all(row['status'] in ('passed', 'unavailable') for row in rows)
    result = {'schema_version': 1, 'snapshot_sha256': hashlib.sha256(json.dumps(snapshot,
              sort_keys=True, separators=(',', ':')).encode()).hexdigest(), 'passed': passed, 'rows': rows}
    if report_path is not None:
        report_path = Path(report_path)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
        report_path.with_suffix('.md').write_text(render_report(result), encoding='utf-8', newline='\n')
    if not passed:
        failed = [row for row in rows if row['status'] == 'failure']
        detail = '; '.join(row['source'] + ' ' + row['channel'] + ': ' + row['reason'] for row in failed)
        raise ManagerUpdateError('官方源码尚未通过配套验证，未合并同步。' + detail)
    return result


def render_report(result: dict) -> str:
    lines = ['### 同步前的管理器配套验证', '',
             '验证固定的官方完整源码及本仓库适配；本次未编译 APK 或内核。', '',
             '| 管理器 | 版本类型 | 数字版本号 | 源码提交 | 配套结果 |', '|---|---|---|---|---|']
    for row in result['rows']:
        if row['status'] == 'unavailable' or not row['changed'] and row['status'] == 'passed':
            continue
        status = '通过' if row['status'] == 'passed' else '失败，未同步'
        lines.append(f"| {row['source']} | {row['channel']} | {row['version_code'] or '未确认'} | "
                     f"`{(row['commit'] or '')[:12]}` | {status} |")
        if row['status'] == 'failure':
            reason = row['reason'].replace('`', '').replace('|', '/').replace('\n', ' ')
            lines.append('\n' + row['source'] + ' ' + row['channel'] + '：' + reason + '\n')
    lines += ['', '所有可用正式版和测试版均做配套检查；上表只展示本次变更或失败项。']
    return '\n'.join(lines) + '\n'
