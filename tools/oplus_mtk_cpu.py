"""Restore the fixed official MTK CPU Kconfig and native sched_tune closure."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
LOCK_SHA256 = 'e631ad4bbd16e049868d0b39be16b40fa950900fc020d9bf2f9f1e6c5afa383d'
TARGET = 'kernel/oplus_cpu'
LINK = '../../vendor/oplus/kernel/cpu'
PREFIX = 'vendor/oplus/kernel/cpu/'
SUPPORTED_FLAGS = frozenset(('OPLUS_FEATURE_PERFORMANCE_SCHED', 'OPLUS_SCHED_TUNE',
                             'OPLUS_FEATURE_SCHED_DDL', 'OPLUS_SCHED_GROUP_OPT'))
PROFILES = frozenset(('mt6897-6.1.75', 'mt6989-6.1.115', 'mt6897-6.1.128', 'mt6989-6.1.134'))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def is_link(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def read(path, maximum=1048576):
    if is_link(path) or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('Expected a bounded regular official MTK CPU source')
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError('Official MTK CPU source exceeds its size limit')
    return data


def load_lock():
    raw = read(ROOT / 'config/oplus-mtk-cpu.json', 65536)
    if digest(raw) != LOCK_SHA256:
        raise ValueError('Official MTK CPU source lock failed its fixed SHA256')
    return json.loads(raw)


def selected_lock(profile):
    if not isinstance(profile, dict):
        raise ValueError('Invalid official MTK CPU profile')
    if profile.get('id') not in PROFILES:
        return None
    lock = load_lock()
    selected = lock['profiles'][profile['id']]
    reference = profile.get('kernel_ref', profile.get('verified_kernel_ref'))
    if (profile.get('kernel_repo') != selected['common_repository'] or reference != selected['common_commit']
            or profile.get('verified_kernel_ref', reference) != reference
            or any(profile.get(key) != selected[key] for key in ('platform', 'kernel'))):
        raise ValueError('Official MTK CPU source pairing does not match the reviewed profile')
    return selected, lock['common_headers']


def native_head(common, expected):
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(common), 'rev-parse', '--verify', 'HEAD'],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    if result.returncode or result.stdout.strip() != expected:
        raise ValueError('Official MTK CPU restoration requires its exact common commit')


def validate_common(common, selected, headers):
    if not common.is_dir() or any(is_link(p) for p in (common, *common.parents)):
        raise ValueError('Official MTK common path must be a real directory')
    native_head(common, selected['common_commit'])
    # The root extension and paired native Kbuild are left byte-for-byte intact.
    for relative in ('Kconfig.ext', 'kernel/Makefile'):
        if digest(read(common / relative)) != selected['native'][relative]['sha256']:
            raise ValueError('Review the changed official MTK extension or paired Kbuild')
    for relative in (*headers, 'arch/arm64/configs/gki_defconfig'):
        path = common / relative
        if any(is_link(p) for p in (path, *path.parents) if p.is_relative_to(common)) or not path.is_file():
            raise ValueError('Official MTK CPU closure requires complete real common headers')
    symbols = set(selected['symbols'])
    for relative in ('arch/arm64/configs/gki_defconfig', 'out/.config'):
        path = common / relative
        if relative == 'out/.config' and not path.exists():
            continue
        text = read(path).decode('utf-8')
        requested = {name for name, value in re.findall(r'(?m)^CONFIG_([A-Z0-9_]+)=([^\r\n]+)$', text)
                     if value in ('y', 'm') and name in symbols and name not in SUPPORTED_FLAGS}
        if requested:
            raise ValueError('Selected MTK CPU feature requires its additional complete official sources: '
                             + ', '.join(sorted(requested)))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download_source(selected, relative, spec):
    if relative not in selected['files'] or selected['files'][relative] != spec:
        raise ValueError('Unreviewed official MTK CPU download request')
    url = ('https://raw.githubusercontent.com/' + selected['vendor_repository'] + '/'
           + selected['vendor_commit'] + '/' + PREFIX + relative)
    for attempt in range(3):
        try:
            request = Request(url, headers={'User-Agent': 'gki-official-mtk-cpu', 'Accept': 'application/octet-stream'})
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ValueError('Official MTK CPU source changed its status or destination')
                return response.read(spec['size'] + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            if status not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise ValueError('Fixed official MTK CPU download failed') from None
        except (OSError, URLError, TimeoutError):
            if attempt == 2:
                raise ValueError('Fixed official MTK CPU transport failed') from None
        time.sleep(attempt + 1)
    raise AssertionError('Unreachable official MTK CPU retry state')


def checked_bytes(data, spec):
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
    if len(data) != spec['size'] or digest(data) != spec['sha256'] or blob != spec['git_blob']:
        raise ValueError('Official MTK CPU file failed size, SHA256 or Git blob verification')


def verify_installed(target, selected):
    if is_link(target) or not target.is_dir():
        raise ValueError('Restored official MTK CPU must be a real directory')
    actual = set()
    directories = {parent.as_posix() for relative in selected['files']
                   for parent in Path(relative).parents if parent != Path('.')}
    for path in target.rglob('*'):
        if is_link(path):
            raise ValueError('Restored official MTK CPU cannot contain links')
        relative = path.relative_to(target).as_posix()
        if path.is_file():
            actual.add(relative)
        elif not path.is_dir() or relative not in directories:
            raise ValueError('Restored official MTK CPU contains an unexpected source entry')
    if actual != set(selected['files']):
        raise ValueError('Restored official MTK CPU closure is incomplete or contains extra files')
    for relative, spec in selected['files'].items():
        checked_bytes(read(target / relative, spec['size']), spec)


def provenance(selected):
    return {'schema_version': 1, 'repository': selected['vendor_repository'],
            'branch': selected['vendor_branch'], 'commit': selected['vendor_commit'],
            'common_repository': selected['common_repository'], 'common_commit': selected['common_commit'],
            'files': {TARGET + '/' + relative: {'source_path': PREFIX + relative,
                     'source_sha256': spec['sha256'], 'sha256': spec['sha256'], 'size': spec['size']}
                      for relative, spec in selected['files'].items()}}


def dangling(target):
    if not target.is_symlink() or os.readlink(target).replace('\\', '/') != LINK or target.exists():
        raise ValueError('Only the exact dangling official MTK CPU link may be replaced')


def restore_mtk_cpu(common, profile):
    loaded = selected_lock(profile)
    if loaded is None:
        return {}
    selected, headers = loaded
    common = Path(common).absolute()
    validate_common(common, selected, headers)
    target = common / TARGET
    if target.is_dir() and not is_link(target):
        verify_installed(target, selected)
        return provenance(selected)
    dangling(target)
    # Verify every original byte before touching the source link. No source,
    # config default, Kbuild line or manager feature is synthesized/disabled.
    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = dict(zip(selected['files'], pool.map(
            lambda relative: download_source(selected, relative, selected['files'][relative]), selected['files'])))
    for relative, data in sources.items():
        checked_bytes(data, selected['files'][relative])
    stage = target.parent / ('.oplus-mtk-cpu-' + uuid.uuid4().hex)
    stage.mkdir()
    published = False
    try:
        for relative, data in sources.items():
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(data)
            path.chmod(int(selected['files'][relative]['mode'], 8) & 0o777)
        verify_installed(stage, selected)
        validate_common(common, selected, headers)
        dangling(target)
        target.unlink()
        try:
            os.replace(stage, target)
            published = True
        except OSError:
            if not target.exists() and not target.is_symlink():
                os.symlink(LINK, target, target_is_directory=True)
            raise ValueError('Cannot publish the verified official MTK CPU closure') from None
    finally:
        if not published:
            for relative in sources:
                (stage / relative).unlink(missing_ok=True)
            # Remove only owned, known empty directories, deepest first.
            directories = {stage, *(p.parent for p in stage.rglob('*') if p.is_file())}
            directories.update(p for p in stage.rglob('*') if p.is_dir() and not is_link(p))
            for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                if path.exists():
                    path.rmdir()
    return provenance(selected)


def validate_mtk_cpu_provenance(profile, report):
    loaded = selected_lock(profile)
    expected = {} if loaded is None else provenance(loaded[0])
    if report != expected or expected and type(report.get('schema_version')) is not int:
        raise ValueError('MTK CPU provenance differs from its exact official source lock')
    return expected


def verify_mtk_cpu_report(common, profile, report):
    expected = validate_mtk_cpu_provenance(profile, report)
    if expected:
        selected, headers = selected_lock(profile)
        common = Path(common).absolute()
        validate_common(common, selected, headers)
        verify_installed(common / TARGET, selected)
    return expected
