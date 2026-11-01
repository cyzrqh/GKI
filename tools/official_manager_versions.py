#!/usr/bin/env python3
"""Observe native manager version codes from fixed official source histories.

This never builds an APK or executes an upstream script. A number is reported
only when the full commit history and both native Kbuild and manager Gradle
formulas agree. A failed observation is informational and remains unknown.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

from manager_variants import MANAGERS, manager_profile
from prepare_manager_lkm import LkmError, native_version_code
from snapshot_ksu_source import SnapshotError, git, temporary_directory


SHA = re.compile(r'[0-9a-f]{40}\Z')
MARKER = 'gki-manager-version-cache.json'
MAX_BLOB = 1024 * 1024
BUDGET = 180
NETWORK = ('-c', 'http.version=HTTP/1.1', '-c', 'http.lowSpeedLimit=1024',
           '-c', 'http.lowSpeedTime=20', '-c', 'http.extraHeader=',
           '-c', 'http.https://github.com/.extraHeader=', '-c', 'credential.helper=',
           '-c', 'http.followRedirects=false', '-c', 'protocol.version=2',
           '-c', 'core.fsmonitor=false', '-c', 'core.attributesFile=')


def _call(directory: Path, deadline: float, *arguments: str) -> str:
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise SnapshotError('Manager version observation exhausted its time budget')
    return git(directory, *NETWORK, *arguments, timeout=min(45, remaining))


def _regular(path: Path) -> bool:
    return (not path.is_symlink() and not getattr(path, 'is_junction', lambda: False)()
            and path.is_file())


def _repository(root: Path, variant: str, deadline: float) -> Path:
    # The cache is only an object/history accelerator, never executable Git
    # configuration. Reinstall its entire known public config before using it.
    for path in (root, *root.parents):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise SnapshotError('Linked manager history cache is not permitted')
    root.mkdir(parents=True, exist_ok=True)
    repository = manager_profile(variant)['repository']
    target = root / (variant + '.git')
    identity = {'schema_version': 1, 'repository': repository}
    if not target.exists():
        with temporary_directory(root, '.manager-history-') as temporary:
            staged = temporary / 'repo'
            staged.mkdir()
            _call(staged, deadline, 'init', '--bare', '--quiet')
            (staged / MARKER).write_text(json.dumps(identity), encoding='utf-8')
            staged.rename(target)
    if target.is_symlink() or not target.is_dir():
        raise SnapshotError('Manager history cache is not a regular directory')
    paths = list(target.rglob('*'))
    if len(paths) > 100000 or any(path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
                                   for path in paths):
        raise SnapshotError('Manager history cache contains linked or excessive entries')
    marker = target / MARKER
    if (not _regular(marker) or marker.stat().st_size > 1024
            or json.loads(marker.read_text(encoding='utf-8')) != identity
            or not _regular(target / 'config') or not _regular(target / 'HEAD')
            or not (target / 'objects').is_dir()
            or any((target / name).exists() for name in ('shallow', 'info/grafts', 'objects/info/alternates',
                                                        'objects/info/http-alternates'))):
        raise SnapshotError('Manager history cache identity or full-history state is invalid')
    (target / 'config').write_text(
        '[core]\n\trepositoryformatversion = 0\n\tbare = true\n'
        '[remote "origin"]\n\turl = https://github.com/' + repository + '.git\n'
        '\tpromisor = true\n\tpartialclonefilter = blob:none\n', encoding='utf-8')
    if _call(target, deadline, 'rev-parse', '--is-bare-repository') != 'true':
        raise SnapshotError('Manager history cache is not bare')
    return target


def _count(repository: Path, reference: str, deadline: float) -> int:
    if not isinstance(reference, str) or not SHA.fullmatch(reference):
        raise SnapshotError('Manager version requires a fixed full commit')
    try:
        # cat-file can lazily fetch a missing object in a promisor repository.
        # Inspect our local pinned ref instead so every network fetch below is
        # explicit, bounded and participates in the same retry policy.
        present = (_call(repository, deadline, 'show-ref', '--verify', '--hash',
                         'refs/gki-manager-versions/' + reference) == reference)
    except SnapshotError:
        present = False
    if not present:
        for attempt in range(3):
            try:
                _call(repository, deadline, 'fetch', '--quiet', '--no-tags', '--filter=blob:none',
                      '--no-recurse-submodules', 'origin',
                      reference + ':refs/gki-manager-versions/' + reference)
                break
            except SnapshotError:
                if attempt == 2 or deadline - time.monotonic() < 3:
                    raise
                time.sleep(attempt + 1)
    if (_call(repository, deadline, 'rev-parse', '--is-shallow-repository') != 'false'
            or _call(repository, deadline, 'rev-parse', '--verify', reference + '^{commit}') != reference):
        raise SnapshotError('Manager source history is not complete or differs from its selected commit')
    count = _call(repository, deadline, 'rev-list', '--count', reference)
    if not re.fullmatch(r'[1-9][0-9]{0,8}', count):
        raise SnapshotError('Manager source history count is invalid')
    return int(count)


def _blob(repository: Path, reference: str, name: str, deadline: float) -> str:
    entry = _call(repository, deadline, 'ls-tree', reference, '--', name)
    if not re.fullmatch(r'100(?:644|755) blob [0-9a-f]{40}\t' + re.escape(name), entry):
        raise SnapshotError('Manager version source is not a regular committed file')
    size = _call(repository, deadline, 'cat-file', '-s', reference + ':' + name)
    if not size.isdigit() or not 0 < int(size) <= MAX_BLOB:
        raise SnapshotError('Manager version source exceeds its size bound')
    return _call(repository, deadline, 'show', reference + ':' + name)


def _compact(value: str) -> str:
    return re.sub(r'\s+', '', value)


def _gradle_version(text: str, count: int, variant: str) -> int:
    """Recognize only the audited native Gradle formulas and full HEAD count."""
    if variant in ('KernelSU-Next', 'KernelSU', 'KowSU', 'FinaSU'):
        counters = re.findall(r'^fun getGitCommitCount\(\): Int \{(.*?)^\}', text, re.M | re.S)
        formulas = re.findall(r'^fun getVersionCode\(\): Int \{(.*?)^\}', text, re.M | re.S)
        expected_count = ('valprocess=Runtime.getRuntime().exec(arrayOf("git","rev-list","--count","HEAD"))'
                          'returnprocess.inputStream.bufferedReader().use{it.readText().trim().toInt()}')
        expected_formula = ('valcommitCount=getGitCommitCount()valmajor=1returnmajor*30000+commitCount'
                            if variant == 'KernelSU-Next' else 'valcommitCount=getGitCommitCount()return30000+commitCount')
        if (len(counters) != 1 or _compact(counters[0]) != expected_count
                or len(formulas) != 1 or _compact(formulas[0]) != expected_formula
                or not re.search(r'(?:extra\["managerVersionCode"\]\s*=\s*getVersionCode\(\)|'
                                 r'val managerVersionCode by extra\(getVersionCode\(\)\))', text)):
            raise SnapshotError('Native manager Gradle history formula changed')
        return 30000 + count
    assignments = re.findall(r'^extra\["managerVersionCode"\][ \t]*=[^\r\n]+', text, re.M)
    if len(assignments) != 1:
        raise SnapshotError('Manager Gradle version formula changed')
    if variant == 'BakaSU':
        expected = 'extra["managerVersionCode"]=30000+getGitCommitCount()+700'
        counters = re.findall(r'^fun getGitCommitCount\(\): Int = providers.exec \{(.*?)^\}'
                              r'\.standardOutput\.asText\.get\(\)\.trim\(\)\.toInt\(\)', text, re.M | re.S)
        if (_compact(assignments[0]) != expected or len(counters) != 1
                or _compact(counters[0]) != 'commandLine("git","rev-list","--count","HEAD")'):
            raise SnapshotError('Native BakaSU manager history calculation changed')
        return 30700 + count
    counters = re.findall(r'^fun getGitCommitCount\(\): Int \{(.*?)^\}', text, re.M | re.S)
    bodies = re.findall(r'^fun getVersionCode\(\): Int \{(.*?)^\}', text, re.M | re.S)
    expected_counter = ('valprocess=Runtime.getRuntime().exec(arrayOf("git","rev-list","--count","HEAD"))'
                        'returnprocess.inputStream.bufferedReader().use{it.readText().trim().toInt()}')
    if (_compact(assignments[0]) != 'extra["managerVersionCode"]=getVersionCode()'
            or len(counters) != 1 or _compact(counters[0]) != expected_counter or len(bodies) != 1):
        raise SnapshotError('Native SukiSU manager history calculation changed')
    formula = re.fullmatch(r'valcommitCount=getGitCommitCount\(\)valmajor=([0-9]+)valend=([0-9]+)'
                           r'returnmajor\*10000\+commitCount-end', _compact(bodies[0]))
    if not formula:
        raise SnapshotError('Native SukiSU manager version formula changed')
    return int(formula[1]) * 10000 + count - int(formula[2])


def _version(repository: Path, reference: str, count: int, variant: str, deadline: float) -> int:
    kbuild = _blob(repository, reference, 'kernel/Kbuild', deadline)
    gradle = _blob(repository, reference, 'manager/build.gradle.kts', deadline)
    with temporary_directory(repository.parent, '.manager-formula-') as source:
        (source / 'kernel').mkdir()
        (source / 'kernel/Kbuild').write_text(kbuild, encoding='utf-8')
        kernel_version = native_version_code(source, count, variant=variant)
    manager_version = _gradle_version(gradle, count, variant)
    if manager_version != kernel_version or not 1 <= manager_version <= 2100000000:
        raise SnapshotError('Manager and native kernel version formulas disagree')
    return manager_version


def observe_versions(before_snapshot: dict | None, after_snapshot: dict | None,
                     cache_root: Path) -> dict:
    """Return four source-derived channels per provided snapshot, never APK claims."""
    deadline = time.monotonic() + BUDGET
    repositories: dict[str, Path | None] = {}
    counts: dict[tuple[str, str], int | None] = {}
    versions: dict[tuple[str, str], int | None] = {}

    def history(variant: str, reference: str) -> int:
        key = variant, reference
        if variant not in repositories:
            try:
                repositories[variant] = _repository(Path(cache_root).absolute(), variant, deadline)
            except (SnapshotError, OSError, ValueError):
                repositories[variant] = None
        repository = repositories[variant]
        if repository is None:
            raise SnapshotError('Manager history observation unavailable')
        if key not in counts:
            try:
                counts[key] = _count(repository, reference, deadline)
            except (SnapshotError, OSError, ValueError):
                counts[key] = None
        if counts[key] is None:
            raise SnapshotError('Manager full history could not be observed')
        return counts[key]

    def rows(snapshot: dict | None) -> list[dict]:
        if snapshot is None:
            return []
        result = []
        for variant in MANAGERS:
            source = snapshot.get(variant.lower())
            if source is None:
                continue
            for channel in ('正式版', '测试版'):
                row = {'source': variant, 'channel': channel, 'ref': None, 'commit': None,
                       'version_code': None, 'status': 'unknown'}
                result.append(row)
                if channel == '正式版':
                    release = source.get('release')
                    if release is None:
                        row['status'] = 'unavailable'
                        continue
                    row.update(ref=release['tag'], commit=release['commit'])
                else:
                    row.update(ref=source['branch'], commit=source['head'])
                try:
                    reference = row['commit']
                    count = history(variant, reference)
                    if channel == '测试版' and variant == 'SukiSU' and source.get('development'):
                        development = source['development']
                        try:
                            dev_count = history(variant, development['head'])
                        except (SnapshotError, OSError, ValueError):
                            # Without both histories the newer branch cannot
                            # be proven; don't invent a selected latest source.
                            row.update(ref=None, commit=None)
                            raise
                        if dev_count > count:
                            reference, count = development['head'], dev_count
                            row.update(ref=development['branch'], commit=reference)
                    key = variant, reference
                    if key not in versions:
                        try:
                            versions[key] = _version(repositories[variant], reference, count, variant, deadline)
                        except (SnapshotError, LkmError, OSError, ValueError):
                            versions[key] = None
                    if versions[key] is not None:
                        row.update(version_code=versions[key], status='observed')
                except (SnapshotError, LkmError, OSError, ValueError):
                    if channel == '测试版' and variant == 'SukiSU' and source.get('development'):
                        row.update(ref=None, commit=None)
        return result

    # Current sources get the network budget first; old unreachable commits
    # after a force-push must not prevent showing the new official version.
    after = rows(after_snapshot)
    before = rows(before_snapshot)
    states = [row['status'] for row in before + after]
    status = ('observed' if 'unknown' not in states else
              'partial' if 'observed' in states else 'not-observed')
    return {'status': status, 'before': before, 'after': after}
