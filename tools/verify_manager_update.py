"""Validate fixed official manager sources before merging an update.

Only Git objects, native version formulas and the repository's own integration
patches are inspected. No upstream build/setup script is executed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from approved_manager_builds import APPROVED_PATH, LATEST, STABLE, approved_document, read_approved, validate_approved
from check_official_updates import validate_snapshot
from manager_variants import MANAGERS, manager_profile
from official_manager_versions import NETWORK, _gradle_version
from prepare_manager_lkm import native_version_code
from resolve_build_sources import source_repository_url
from snapshot_ksu_source import git, temporary_directory, SnapshotError

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r'[0-9a-f]{40}\Z')
PROFILE = 'build-compatibility.json'
MANAGER_PINS = 'config/manager-susfs-pins.json'
SUSFS_RECEIPT = 'data/susfs-upstream-candidates.json'
CHANNELS = {STABLE: '正式版', LATEST: '测试版'}


class ManagerUpdateError(ValueError):
    pass


class SourceIntegrationError(ManagerUpdateError):
    def __init__(self, message: str, version_code: int):
        self.version_code = version_code
        super().__init__(message)


def verification_error(error):
    if isinstance(error, subprocess.CalledProcessError):
        return f'配套源码命令执行失败（退出码 {error.returncode}）'
    if isinstance(error, subprocess.SubprocessError):
        return '配套源码命令未完成'
    return str(error)[:1500]


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
        '[remote "origin"]\n\turl = ' + source_repository_url(repository) + '\n',
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


def check_source(repository: Path, variant: str, reference: str, count: int, *,
                 susfs24_pins: dict[str, str] | None = None, legacy_susfs: bool = True,
                 susfs24_cache: dict | None = None) -> dict:
    with temporary_directory(repository.parent, '.manager-compatibility-') as temporary:
        source = temporary / 'KernelSU'
        git(temporary, 'clone', '--quiet', '--shared', '--no-checkout', str(repository), str(source))
        git(source, 'checkout', '--quiet', '--detach', reference)
        git(source, 'remote', 'set-url', 'origin', source_repository_url(manager_profile(variant)['repository']))
        code = native_version_code(source, count, variant=variant)
        gradle = (source / 'manager/build.gradle.kts').read_text('utf-8')
        if _gradle_version(gradle, count, variant) != code:
            raise ManagerUpdateError('Official manager and kernel numeric version formulas disagree')
        try:
            checked24 = ()
            if susfs24_pins:
                from susfs24_manager_preflight import fetch_pins, manager_inputs, verify_inputs
                inputs_cache = {} if susfs24_cache is None else susfs24_cache
                identity = tuple(sorted(susfs24_pins.items()))
                if identity not in inputs_cache:
                    inputs_cache[identity] = manager_inputs(source, fetch_pins(source, susfs24_pins))
                checked24 = verify_inputs(source, reference, variant, inputs_cache[identity])
            if variant == 'SukiSU':
                from manager_variants import manager_source_package, SUKISU_NOVA_PACKAGE
                native_package = manager_source_package(variant, source)
                if native_package == SUKISU_NOVA_PACKAGE:
                    from sukisu_builtzygisk_source import verify_source
                    verify_source(source)
                from prepare_manager_icons import prepare_manager_icons
                prepare_manager_icons(source)
                from apply_susfs23 import apply_reviewed_port, PATCH_PATH
                if legacy_susfs:
                    apply_reviewed_port(source, PATCH_PATH, allow_variants=True)
                from configure_sukisu_kpm_ui import configure as configure_kpm_ui
                configure_kpm_ui(source)
                checks = ['KPM 原生接口保留',
                          '管理器图标依赖及 Kotlin 模块索引', 'KPM 原生页面授权门禁',
                          '管理器/内核版本公式一致']
                if native_package == SUKISU_NOVA_PACKAGE:
                    checks.append('BuiltZygisk 官方依赖地址与固定提交')
                if legacy_susfs:
                    checks.append('SUSFS 2.3 完整接入及原生生命周期')
            elif variant == 'BakaSU':
                from restore_bakasu_kpm import native_build_contract, restore_bakasu_kpm, verify_bakasu_kpm_port
                native_build_contract(source)
                from ensure_bakasu_symbol_lookup import validate_native_symbol_lookup
                restore_bakasu_kpm(source, reference)
                verify_bakasu_kpm_port(source, reference)
                validate_native_symbol_lookup(source, reference, Path('kernel'))
                from configure_bakasu_kpm_ui import configure as configure_kpm_ui
                configure_kpm_ui(source)
                checks = ['KPM 完整还原及正反向校验', '原生符号接口', 'KPM 页面主题及授权门禁',
                          '管理器/内核版本公式一致']
            else:
                from additional_manager_integration import apply_next_port, configure
                from additional_manager_kpm import port, verify, with_susfs
                port(source, reference, variant)
                verify(source, reference, variant)
                if legacy_susfs and variant in ('KernelSU-Next', 'KernelSU', 'KowSU'):
                    name = {'KernelSU-Next': 'next-susfs23', 'KernelSU': 'official-susfs23',
                            'KowSU': 'kow-susfs23'}[variant]
                    if not (source / 'kernel/preinit/preinit.c').is_file():
                        name += '-stable'
                    with_susfs(source, variant, lambda: apply_next_port(source, name=name))
                configure(temporary, '827', 'a' * 64, manager_profile(variant)['package'], str(code), variant=variant)
                checks = ['管理器/内核完整历史版本一致', '官方包名与固定自定义证书绑定接口',
                          'KPM ABI-2 内核接口、内置页面及持久加载源码接线']
                if legacy_susfs and variant in ('KernelSU-Next', 'KernelSU', 'KowSU'):
                    checks.append('完整 SUSFS 2.3 适配')
            if checked24:
                checks.append('SUSFS 2.4.0 管理器配套：' + '、'.join(checked24))
            from manager_ui_defaults import apply_defaults
            apply_defaults(source, variant)
            checks.append('管理器更新默认关闭，模块更新独立保留')
            if variant not in ('SukiSU', 'BakaSU'):
                from prepare_additional_gradle import prepare
                prepare(source, variant)
                checks.append('原生 APK 构建配置及官方包名')
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            raise SourceIntegrationError(verification_error(error), code) from None
        return {'version_code': code, 'checks': checks}


def susfs_options(variant, configuration, inputs_cache):
    if configuration is None or variant not in configuration[0]:
        return {}
    from pin_build_dependencies import SUSFS_24
    pins, versions = configuration
    pins24 = {target: pins[variant][target] for target, version in versions[variant].items()
              if version == SUSFS_24}
    if not pins24:
        return {}
    return {'susfs24_pins': pins24, 'legacy_susfs': len(pins24) < len(pins[variant]),
            'susfs24_cache': inputs_cache}


def observed_channel(snapshot, variant, tier):
    """Select the same stable/latest identity as the official version checker."""
    record = snapshot.get(variant.lower())
    if record is None:
        return None
    if tier == STABLE:
        release = record['release']
        return (release['commit'], release['tag']) if release else None
    if variant == 'SukiSU' and (development := record.get('development')):
        from resolve_build_sources import compare_sukisu_channels
        return compare_sukisu_channels(manager_profile(variant)['repository'],
                                       record['branch'], record['head'], development['head'])
    return record['head'], record['branch']


def changed_pins(before, after):
    """Changing a pin affects every approved channel for that manager."""
    return {variant for variant in MANAGERS
            if any(old.get(variant) != new.get(variant) for old, new in zip(before, after))}


def selected_checks(identities, *, cache, susfs_configuration):
    rows, repositories, results, inputs_cache = {}, {}, {}, {}
    for (variant, tier), (reference, branch) in identities.items():
        row = {'source': variant, 'channel': CHANNELS[tier], 'commit': reference,
               'ref': branch, 'version_code': None, 'checks': []}
        key = variant, reference
        if key not in results:
            try:
                if variant not in repositories:
                    repositories[variant] = source_repository(cache, variant)
                repository = repositories[variant]
                count = fetch_reference(repository, reference)
                proof = check_source(repository, variant, reference, count,
                                     **susfs_options(variant, susfs_configuration, inputs_cache))
                results[key] = {**proof, 'status': 'passed', 'reason': '配套源码验证通过，未编译 APK 或内核'}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                results[key] = {'status': 'failure', 'reason': verification_error(error),
                                'version_code': getattr(error, 'version_code', None)}
        rows[variant, tier] = {**row, **results[key]}
    return rows


def approved_slots(channels, variants=MANAGERS):
    return {(variant, tier): identity for variant in MANAGERS if variant in variants
            for tier, identity in channels[variant].items()}


def plan_approvals(channels, snapshot, *, cache, before_pins, proposed_pins):
    """Advance passing slots and retain each failing slot's approved identity."""
    snapshot = validate_snapshot(snapshot)
    retained = selected_checks(approved_slots(channels, changed_pins(before_pins, proposed_pins)),
                               cache=cache, susfs_configuration=proposed_pins)
    retain_pins = any(row['status'] == 'failure' for row in retained.values())
    configuration = before_pins if retain_pins else proposed_pins
    candidates, failures = {}, []
    for variant in MANAGERS:
        for tier, label in CHANNELS.items():
            try:
                observed = observed_channel(snapshot, variant, tier)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                failures.append({'source': variant, 'channel': label, 'commit': None, 'ref': None,
                                 'version_code': None, 'checks': [], 'status': 'failure',
                                 'reason': verification_error(error)})
                continue
            if observed is not None and observed != channels[variant].get(tier):
                candidates[variant, tier] = observed
    checks = selected_checks(candidates, cache=cache, susfs_configuration=configuration)
    updated = {name: dict(tiers) for name, tiers in channels.items()}
    for (variant, tier), row in checks.items():
        if row['status'] == 'passed':
            updated[variant][tier] = candidates[variant, tier]
    kept = [row for row in retained.values() if row['status'] == 'failure']
    rows = list(checks.values()) + failures
    return updated, {'schema_version': 1, 'rows': rows, 'pin_failures': kept,
                     'approved_changed': updated != channels, 'pins_retained': retain_pins,
                     'pending_slots': sum(row['status'] == 'failure' for row in rows)}


def validate_approved_changes(channels, new_channels, snapshot, *, cache, before_pins, proposed_pins):
    """Independently verify the exact subset that the frozen PR advances."""
    snapshot = validate_snapshot(snapshot) if snapshot is not None else None
    previous, proposed = approved_slots(channels), approved_slots(new_channels)
    if previous.keys() - proposed.keys():
        raise ManagerUpdateError('Official sync cannot remove an approved manager channel')
    promoted = {key: identity for key, identity in proposed.items() if previous.get(key) != identity}
    for (variant, tier), identity in promoted.items():
        if snapshot is None or observed_channel(snapshot, variant, tier) != identity:
            raise ManagerUpdateError(f'{variant} {CHANNELS[tier]} 与本次固定的上游版本不符')
    retained_slots = approved_slots(channels, changed_pins(before_pins, proposed_pins))
    # Check both old and newly promoted references when a shared pin moves.
    retained = selected_checks(retained_slots, cache=cache, susfs_configuration=proposed_pins)
    checks_needed = {key: identity for key, identity in promoted.items() if retained_slots.get(key) != identity}
    checks = selected_checks(checks_needed, cache=cache, susfs_configuration=proposed_pins)
    failures = [row for row in (*retained.values(), *checks.values()) if row['status'] == 'failure']
    if failures:
        detail = '; '.join(row['source'] + ' ' + row['channel'] + ': ' + row['reason'] for row in failures)
        raise ManagerUpdateError('同步变更未通过配套验证，未合并。' + detail)
    return {'schema_version': 1, 'passed': True, 'rows': [*retained.values(), *checks.values()]}


def validate_manager_snapshot(snapshot: dict, *, cache: Path, report_path: Path | None = None,
                              before_snapshot: dict | None = None, susfs_configuration: tuple | None = None) -> dict:
    snapshot = validate_snapshot(snapshot)
    previous = validate_snapshot(before_snapshot) if before_snapshot is not None else None
    rows = []
    susfs24_cache = {}
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
                selected = susfs_options(variant, susfs_configuration, susfs24_cache)
                result = check_source(repository, variant, reference, count, **selected)
                row.update(result, status='passed', reason='配套源码验证通过，未编译 APK 或内核')
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                row.update(status='failure', reason=verification_error(error))
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
    lines = ['### 同步配套验证', '', '配套验证通过。' if result['passed'] else '**配套验证失败，未同步。**']
    for row in (row for row in result['rows'] if row['status'] == 'failure'):
        reason = row['reason'].replace('`', '').replace('|', '/').replace('\n', ' ')
        name = 'SukiSU-Nova' if row['source'] == 'SukiSU' else row['source']
        lines.append('\n' + name + ' ' + row['channel'] + '：失败，未同步。' + reason)
    return '\n'.join(lines) + '\n'


def write_promotion_report(path: Path, plan: dict):
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    lines = ['### 管理器配套验证', '']
    for row in plan['rows']:
        detail = row['reason'].replace('\n', ' ')
        version = (' ' + str(row['version_code'])) if row['version_code'] is not None else ''
        state = '通过，可同步' if row['status'] == 'passed' else '待适配，保留当前版本'
        name = 'SukiSU-Nova' if row['source'] == 'SukiSU' else row['source']
        lines.append(f"- {name}{version} {row['channel']} · {state}。{detail}")
    for row in plan['pin_failures']:
        reason = row['reason'].replace('\n', ' ')
        name = 'SukiSU-Nova' if row['source'] == 'SukiSU' else row['source']
        lines.append(f"- {name} {row['channel']} · 保留原 SUSFS 构建引用。{reason}")
    if not plan['rows'] and not plan['pin_failures']:
        lines.append('本次没有待同步的管理器版本。')
    path.with_suffix('.md').write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')


def prepare_promotions(root: Path, sources: Path, cache: Path, report: Path) -> dict:
    from official_susfs_archive import git_bytes
    from official_sync_report import read_git_document, read_worktree_document
    from prepare_source_updates import restore_files
    from update_susfs_sources import configured_pins

    root = root.resolve(strict=True)
    sources = root / sources
    if not sources.resolve(strict=True).is_relative_to(root):
        raise ManagerUpdateError('Manager observation must stay inside the repository')
    observed = read_worktree_document(root, sources.relative_to(root).as_posix())['sources']
    base = git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
    approved = read_approved(root)
    if approved != validate_approved(read_git_document(root, base, APPROVED_PATH)):
        raise ManagerUpdateError('Approved manager channels changed before planning the sync')
    before = configured_pins(read_git_document(root, base, PROFILE), read_git_document(root, base, MANAGER_PINS))
    proposed = configured_pins(read_worktree_document(root, PROFILE), read_worktree_document(root, MANAGER_PINS))
    advanced, plan = plan_approvals(approved, observed, cache=cache, before_pins=before, proposed_pins=proposed)
    if git(root, 'rev-parse', '--verify', 'HEAD^{commit}') != base:
        raise ManagerUpdateError('Default branch changed while planning manager promotions')
    if plan['pins_retained']:
        previous = {}
        for name in (PROFILE, MANAGER_PINS, SUSFS_RECEIPT):
            previous[name] = (git_bytes(root, 'show', base + ':' + name)
                              if git_bytes(root, 'ls-tree', '-z', base, '--', name) else None)
        restore_files(root, previous)
    if plan['approved_changed']:
        raw = json.dumps(approved_document(advanced), ensure_ascii=False, indent=2) + '\n'
        restore_files(root, {APPROVED_PATH: raw.encode('utf-8')})
    write_promotion_report(report, plan)
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--sources', required=True, type=Path)
    parser.add_argument('--source-cache', required=True, type=Path)
    parser.add_argument('--compatibility-report', required=True, type=Path)
    parser.add_argument('--github-output', type=Path,
                        default=Path(os.environ['GITHUB_OUTPUT']) if os.environ.get('GITHUB_OUTPUT') else None)
    args = parser.parse_args(argv)
    try:
        result = prepare_promotions(args.root, args.sources, args.source_cache, args.compatibility_report)
        if args.github_output:
            with args.github_output.open('a', encoding='utf-8', newline='\n') as stream:
                for name in ('approved_changed', 'pending_slots', 'pins_retained'):
                    stream.write(f'{name}={str(result[name]).lower()}\n')
        return 0
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as error:
        message = str(error).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print('::error::无法准备管理器配套更新：' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
