#!/usr/bin/env python3
"""Package platform-checked OPlus kernels and verify native same-run assets."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import uuid
import zipfile
import zlib

import apply_kpm_patch as kpm
from capture_build_metadata import collect as capture, parse_config, state
from embedded_build_metadata import metadata_json, read_manager_apk, read_member, validate_members
from manager_variants import MANAGERS, manager_profile, valid_manager_apk_filename
from prepare_release import release_title
import oplus_catalog as catalog
from oplus_vendor_sources import validate_vendor_provenance, verify_vendor_report
from validate_arm64_image import validate_image


MEMBER = 'META-INF/oplus-kernel.json'
PAYLOAD_MEMBER = 'META-INF/oplus-kpm/kpimg'
SCHEMA = 'gki-oplus-kernel'
MAX_ZIP = 512 * 1024 * 1024
MAX_IMAGE = 256 * 1024 * 1024
MAX_CONFIG = 8 * 1024 * 1024
PROFILE_FIELDS = (catalog.FIELDS - {'verified_kernel_ref'}) | {'kernel_ref', 'source_committed_at'}
REQUIRED_RUNTIME = {'tools/ak3-core.sh', 'META-INF/com/google/android/update-binary'}
PRODUCER = ('producer_repository', 'producer_run_id', 'producer_run_attempt', 'producer_head_sha')
MANAGER_BINDINGS = ('manager_package', 'manager_cert_size', 'manager_cert_sha256',
                    'manager_apk_filename', 'manager_apk_sha256')


class AssetError(ValueError):
    """An incomplete or mismatched OPlus build must not be published."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def regular(path: Path, maximum: int = MAX_ZIP) -> bytes:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise AssetError('Expected a bounded nonempty regular build asset')
    return path.read_bytes()


def bounded_config(image: bytes) -> tuple[dict, str, str]:
    start = image.find(b'IKCFG_ST')
    end = image.find(b'IKCFG_ED', start + 8)
    if start < 0 or end < 0 or image.count(b'IKCFG_ST') != 1 or image.count(b'IKCFG_ED') != 1:
        raise AssetError('OPlus Image requires one unambiguous embedded IKCONFIG')
    try:
        inflater = zlib.decompressobj(31)
        raw = inflater.decompress(image[start + 8:end], MAX_CONFIG + 1)
        if len(raw) > MAX_CONFIG or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise AssetError('OPlus IKCONFIG is oversized or contains trailing compressed data')
        text = raw.decode('utf-8')
        config = parse_config(text)
    except (zlib.error, UnicodeError, ValueError) as error:
        raise AssetError('OPlus Image has invalid or oversized IKCONFIG') from error
    return config, 'Image / IKCONFIG', digest(raw)


def json_input(value: str | Path) -> dict:
    raw = str(value).encode('utf-8') if str(value).lstrip().startswith('{') else regular(Path(value), 4 * 1024 * 1024)
    result = catalog.strict_json(raw)
    if not isinstance(result, dict):
        raise AssetError('Expected a JSON build profile or matrix object')
    return result


def profile_identity(document: dict) -> dict:
    if (not isinstance(document, dict) or not PROFILE_FIELDS <= set(document)
            or any(not isinstance(document[field], str) for field in PROFILE_FIELDS)):
        raise AssetError('OPlus build requires a complete frozen official-source profile')
    row = {field: document[field] for field in PROFILE_FIELDS}
    platform, kernel = row['platform'], row['kernel']
    match = catalog.KERNEL.fullmatch(kernel)
    series = '.'.join(kernel.split('.')[:2])
    if (not catalog.PLATFORM.fullmatch(platform) or match is None
            or row['id'] != platform + '-' + kernel or row['kernel_version'] != series
            or row['sub_level'] != match[2] or row['android_version'] != catalog.ANDROID.get(series)
            or row['kernel_repo'] not in catalog.OFFICIAL_REPOSITORIES
            or not row['kernel_repo'].endswith('_' + platform)
            or row['recipe_repository'] != catalog.RECIPE_FOR_SERIES.get(series)
            or any(not catalog.SHA.fullmatch(row[field]) for field in ('kernel_ref', 'recipe_ref'))
            or not catalog.SHA256.fullmatch(row['recipe_sha256'])
            or not re.fullmatch(r'\.github/workflows/fastbuild_' + re.escape(kernel) + r'(?:_mtk)?\.yml', row['recipe_workflow'])):
        raise AssetError('OPlus source, platform, version or reviewed recipe identity is inconsistent')
    branch = row['kernel_branch']
    if (not re.fullmatch(r'[A-Za-z0-9_./-]{1,255}', branch)
            or any(part in ('', '.', '..') for part in branch.split('/'))
            or not branch.startswith(('oneplus/' + platform + '_', 'oppo/' + platform + '_'))):
        raise AssetError('OPlus source branch does not match its reviewed platform')
    for field in ('system', 'compatibility'):
        if not row[field] or len(row[field]) > 1024 or any(ord(char) < 32 for char in row[field]):
            raise AssetError('OPlus compatibility text is missing or unsafe')
    timestamp(row['source_committed_at'])
    reviewed = {item['id']: item for item in catalog.load_profiles()}.get(row['id'])
    if reviewed is None or any(row[field] != reviewed[field] for field in catalog.FIELDS - {'verified_kernel_ref'}):
        raise AssetError('OPlus profile differs from the reviewed official source and recipe catalog')
    return row


def timestamp(value: str):
    if not isinstance(value, str) or len(value) > 64:
        raise AssetError('Missing bounded source/build timestamp')
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise AssetError('Invalid source/build timestamp') from error
    if parsed.tzinfo is None:
        raise AssetError('Source/build timestamp must include its timezone')
    return parsed


def git(directory: Path, *args: str) -> str:
    try:
        result = subprocess.run(['git', '--no-replace-objects', '-C', str(directory), *args],
                                capture_output=True, text=True, encoding='utf-8', timeout=30,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    except (OSError, subprocess.TimeoutExpired):
        raise AssetError('Cannot read the pinned source Git identity') from None
    if result.returncode:
        raise AssetError('Cannot verify the pinned source Git identity')
    return result.stdout.strip()


def provenance() -> dict:
    result = dict(zip(PRODUCER, (os.environ.get(key, '') for key in
                  ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA'))))
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', result['producer_repository'])
            or any(not re.fullmatch(r'[1-9][0-9]*', result[field]) for field in PRODUCER[1:3])
            or not catalog.SHA.fullmatch(result['producer_head_sha'])):
        raise AssetError('OPlus assets require the current repository/run/attempt/commit identity')
    return result


def filename(profile: dict, variant: str) -> str:
    if variant not in MANAGERS:
        raise AssetError('Only the paired SukiSU and BakaSU managers are supported')
    family = {'骁龙': 'Snapdragon', '天玑': 'Dimensity'}[catalog.platform_family(profile['platform'])]
    return f"Oplus-{family}-{profile['android_version']}-{profile['kernel']}-{variant}-AnyKernel3.zip"


def release_prefix(release: str, profile: dict) -> str:
    if (not isinstance(release, str) or len(release) > 256
            or not re.fullmatch(r'[A-Za-z0-9_.+-]+', release)):
        raise AssetError('Native kernel.release is missing or unsafe')
    match = re.match('^(' + re.escape(profile['kernel'] + '-' + profile['android_version'])
                     + r'-[0-9]+)(?:-|$)', release)
    if match is None:
        raise AssetError('Native kernel.release must preserve the full kernel, Android and KMI generation')
    return match[1]


def anykernel_script(profile: dict, variant: str, prefix: str) -> bytes:
    # This replaces the upstream example's device/ramdisk/fstab modifications.
    # Explicit boot avoids AnyKernel3's auto preference for init_boot. The
    # split/flash pair preserves an existing ramdisk and permits ramdisk-free
    # boot images; platform/KMI guards run before either operation.
    return f'''#!/system/bin/sh
properties() {{ '
kernel.string=Oplus {catalog.platform_family(profile['platform'])} {profile['kernel']} {variant}
do.devicecheck=0
do.modules=0
do.systemless=0
do.cleanup=1
do.cleanuponabort=1
supported.versions=
supported.patchlevels=
'; }}
BLOCK=boot;
IS_SLOT_DEVICE=auto;
RAMDISK_COMPRESSION=auto;
PATCH_VBMETA_FLAG=0;
. tools/ak3-core.sh;
oplus_soc_match=0;
for oplus_prop in ro.soc.model ro.vendor.soc.model ro.boot.soc.model; do
  oplus_soc="$(getprop "$oplus_prop" | tr '[:upper:]' '[:lower:]')";
  case "$oplus_soc" in
    {profile['platform']}|{profile['platform']}[!0-9]*) oplus_soc_match=1 ;;
  esac;
done;
[ "$oplus_soc_match" = 1 ] || abort "Oplus platform mismatch or unknown SoC: requires {profile['platform']}";
oplus_running_release="$(uname -r)";
case "$oplus_running_release" in
  {prefix}|{prefix}-*) ;;
  *) abort "Oplus kernel/Android/KMI mismatch: requires {prefix}" ;;
esac;
ui_print "Verified Oplus platform and full kernel/KMI prefix: {catalog.platform_family(profile['platform'])} {prefix}";
split_boot;
flash_boot;
'''.encode('utf-8')


def verify_kpm(image: bytes, report: dict, payload: bytes) -> None:
    if not isinstance(report, dict):
        raise AssetError('KPM report must identify its fixed verified official release')
    try:
        release = kpm.release_for_provenance(report.get('release'))
        payload_info = kpm.payload_metadata(payload, release)
    except ValueError as error:
        raise AssetError(str(error)) from None
    lock = release['assets']['kpimg']
    if (len(payload) != lock['size'] or digest(payload) != lock['sha256']
            or not isinstance(report, dict) or report.get('schema_version') != 1
            or report.get('release') != {key: release[key] for key in ('repository', 'tag', 'commit')}
            or report.get('tool') != {key: release['assets']['kptools-linux'][key] for key in ('size', 'sha256')}
            or report.get('payload') != {key: lock[key] for key in ('size', 'sha256')} | payload_info
            or report.get('post_image_sha256') != digest(image) or report.get('post_image_size') != len(image)
            or report.get('post_entry') != validate_image(image)):
        raise AssetError('KPM Image/report/payload differs from the fixed verified official release')
    try:
        before = kpm.reconstruct_original_image(image, report)
    except ValueError as error:
        raise AssetError(str(error)) from None
    if (digest(before) != report.get('pre_image_sha256') or validate_image(before) != report.get('pre_entry')
            or kpm.verify_patched(before, image, payload) != report.get('layout')):
        raise AssetError('KPM preset/layout does not describe the actual complete patched Image')


def template_files(directory: Path) -> tuple[dict[str, tuple[bytes, int]], str]:
    if directory.is_symlink() or not directory.is_dir():
        raise AssetError('AnyKernel3 must be an independent official checkout')
    origin = git(directory, 'remote', 'get-url', 'origin')
    if origin not in ('https://github.com/osm0sis/AnyKernel3', 'https://github.com/osm0sis/AnyKernel3.git'):
        raise AssetError('Only the official osm0sis/AnyKernel3 runtime may be packaged')
    reference = git(directory, 'rev-parse', 'HEAD')
    expected = os.environ.get('AK3_REF') or os.environ.get('ANYKERNEL3_REF') or reference
    if not catalog.SHA.fullmatch(reference) or expected != reference:
        raise AssetError('AnyKernel3 runtime differs from its pinned commit')
    if git(directory, 'status', '--porcelain', '--untracked-files=all'):
        raise AssetError('AnyKernel3 runtime checkout contains changes outside its pinned commit')
    files, total = {}, 0
    for path in directory.rglob('*'):
        relative = path.relative_to(directory).as_posix()
        if relative == '.git' or relative.startswith('.git/'):
            continue
        if path.is_symlink():
            raise AssetError('Linked AnyKernel3 paths are refused')
        if not path.is_file():
            continue
        if not (relative.startswith(('tools/', 'META-INF/')) or path.parent == directory and
                (path.name.casefold().startswith('license') or path.name == 'COPYING')):
            continue
        if any(part in ('', '.', '..') for part in relative.split('/')) or '\\' in relative:
            raise AssetError('Unsafe AnyKernel3 runtime path')
        data = regular(path, 64 * 1024 * 1024)
        total += len(data)
        if total > MAX_ZIP or relative in (MEMBER, PAYLOAD_MEMBER):
            raise AssetError('Unexpected AnyKernel3 runtime extent or metadata collision')
        files[relative] = data, stat.S_IMODE(path.stat().st_mode)
    if not REQUIRED_RUNTIME <= files.keys() or not any(Path(name).name.casefold().startswith('license') for name in files):
        raise AssetError('Official AnyKernel3 runtime is incomplete')
    return files, reference


def package(profile: dict, kernel_root: Path, image_path: Path, template: Path, output: Path) -> dict:
    profile = profile_identity(profile)
    variant, reference = os.environ.get('KSU_VARIANT'), os.environ.get('KSU_REF', '')
    if variant not in MANAGERS or not catalog.SHA.fullmatch(reference):
        raise AssetError('Missing paired manager variant or immutable KernelSU source')
    common = kernel_root / 'common'
    if git(common, 'rev-parse', 'HEAD') != profile['kernel_ref']:
        raise AssetError('The compiled official kernel HEAD differs from the prepared source')
    if git(common, 'remote', 'get-url', 'origin') not in (
            'https://github.com/' + profile['kernel_repo'], 'https://github.com/' + profile['kernel_repo'] + '.git'):
        raise AssetError('Kernel checkout origin is not the reviewed official repository')
    text = regular(common / 'Makefile', 2 * 1024 * 1024).decode('utf-8')
    values = [re.findall(r'^' + field + r'\s*=\s*(0|[1-9][0-9]*)\s*$', text, re.M)
              for field in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL')]
    if any(len(value) != 1 for value in values) or '.'.join(value[0] for value in values) != profile['kernel']:
        raise AssetError('Official kernel Makefile differs from the reviewed complete version')
    native = regular(common / 'out/include/config/kernel.release', 1024).decode('utf-8').strip()
    prefix = release_prefix(native, profile)
    image = regular(image_path, MAX_IMAGE)
    bounded_config(image)
    if b'Linux version ' + native.encode('ascii') not in image:
        raise AssetError('Native kernel.release is not present in the actual Image banner')
    enable_susfs = os.environ.get('ENABLE_SUSFS', 'true').lower()
    if enable_susfs not in ('true', 'false'):
        raise AssetError('ENABLE_SUSFS must be the actual boolean menu selection')
    context = {'file': filename(profile, variant), 'android_version': profile['android_version'],
               'kernel_version': profile['kernel_version'], 'sub_level': profile['sub_level'],
               'ksu_variant': variant, 'ksu_branch': os.environ.get('KSU_BRANCH', ''), 'ksu_ref': reference,
               'ksu_version_code': os.environ.get('MANAGER_VERSION_CODE', ''),
               'ksu_version_name': os.environ.get('MANAGER_VERSION_NAME', ''),
               'enable_susfs': enable_susfs, 'expected_susfs_version': 'v2.3.0'}
    metadata = capture(kernel_root, image_path, context)
    metadata.pop('os_patch_level', None)
    if metadata['features']['susfs'] != (enable_susfs == 'true'):
        raise AssetError('Actual SUSFS configuration differs from the menu selection')
    metadata.update(schema=SCHEMA, schema_version=1, profile=profile, kernel_release=native,
                    compatibility_prefix=prefix, **provenance())
    vendor_report_path = common / 'out/gki-oplus-vendor-sources.json'
    vendor_report = (metadata_json(regular(vendor_report_path, 65536))
                     if vendor_report_path.exists() or vendor_report_path.is_symlink() else {})
    metadata['vendor_sources'] = verify_vendor_report(common, profile, vendor_report)
    for field in MANAGER_BINDINGS:
        metadata[field] = os.environ.get(field.upper(), '')
    runtime, ak3_ref = template_files(template)
    metadata['ak3_template'] = {'repository': 'osm0sis/AnyKernel3', 'ref': ak3_ref}
    metadata['runtime_sha256'] = {name: digest(data) for name, (data, _) in runtime.items()}
    report_path = common / 'out/gki-kpm-patch.json'
    payload = None
    if report_path.exists() or report_path.is_symlink():
        report = metadata_json(regular(report_path, 65536))
        cache = os.environ.get('KPM_CACHE_DIR')
        if not cache:
            raise AssetError('Verified KPM report requires its fixed original payload cache')
        payload = regular(Path(cache) / 'kpimg', 16 * 1024 * 1024)
        verify_kpm(image, report, payload)
        metadata['kpm_patch'] = report
    else:
        if kpm.MAGIC in image:
            raise AssetError('Image contains KPM without its verified original-payload report')
        metadata['kpm_patch'] = None
    config, _, _ = bounded_config(image)
    metadata['features']['kpm_native_config'] = state(config, 'KPM')
    metadata['features']['kpm'] = payload is not None
    validate_document(metadata)
    verify_image(metadata, image, payload)
    if output.is_symlink():
        raise AssetError('Linked artifact output directories are refused')
    output.mkdir(parents=True, exist_ok=True)
    target = output / metadata['file']
    if target.exists() or target.is_symlink():
        raise AssetError('Refusing to replace an existing native OPlus artifact')
    temporary = output / ('.oplus-' + uuid.uuid4().hex + '.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            entries = {**runtime, 'Image': (image, 0o644),
                       'anykernel.sh': (anykernel_script(profile, variant, prefix), 0o755),
                       MEMBER: ((json.dumps(metadata, ensure_ascii=False, sort_keys=True) + '\n').encode(), 0o644)}
            if payload is not None:
                entries[PAYLOAD_MEMBER] = payload, 0o644
            for name, (data, mode) in sorted(entries.items()):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, data)
        read_kernel(temporary, expected_filename=metadata['file'])
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {'file': target.name, 'path': str(target), 'sha256': digest(target.read_bytes())}


def validate_document(document: dict) -> None:
    if (not isinstance(document, dict) or document.get('schema') != SCHEMA
            or type(document.get('schema_version')) is not int or document['schema_version'] != 1):
        raise AssetError('Invalid independent OPlus asset schema')
    profile = profile_identity(document.get('profile'))
    try:
        validate_vendor_provenance(profile, document.get('vendor_sources', {}))
    except ValueError as error:
        raise AssetError(str(error)) from None
    variant = document.get('ksu_variant')
    if document.get('file') != filename(profile, variant):
        raise AssetError('OPlus artifact filename does not match its platform, version and variant')
    if any(document.get(key) != profile[key] for key in ('android_version', 'kernel_version', 'sub_level')):
        raise AssetError('OPlus compiled target metadata differs from the official profile')
    prefix = release_prefix(document.get('kernel_release'), profile)
    if document.get('compatibility_prefix') != prefix:
        raise AssetError('OPlus kernel compatibility prefix is inconsistent')
    timestamp(document.get('built_at_utc'))
    if (not catalog.SHA.fullmatch(str(document.get('ksu_ref', '')))
            or not isinstance(document.get('ksu_branch'), str) or not document['ksu_branch']
            or any(ord(char) < 32 for char in document['ksu_branch'])
            or not re.fullmatch(r'[1-9][0-9]*', str(document.get('ksu_version_code', '')))
            or not isinstance(document.get('ksu_version_name'), str) or not document['ksu_version_name']):
        raise AssetError('OPlus kernel lacks its native paired manager version/source identity')
    if (document.get('manager_package') != manager_profile(variant)['package']
            or not re.fullmatch(r'[1-9][0-9]*', str(document.get('manager_cert_size', '')))
            or not 64 <= int(document['manager_cert_size']) <= 4096
            or not valid_manager_apk_filename(document.get('manager_apk_filename'), variant)):
        raise AssetError('OPlus kernel manager package/certificate/filename identity is invalid')
    for field in ('image_sha256', 'config_sha256', 'manager_cert_sha256', 'manager_apk_sha256'):
        if not catalog.SHA256.fullmatch(str(document.get(field, ''))):
            raise AssetError('OPlus kernel is missing an actual asset/config/signer digest')
    runtime = document.get('runtime_sha256')
    template = document.get('ak3_template')
    if (document.get('config_source') != 'Image / IKCONFIG' or not isinstance(document.get('features'), dict)
            or 'kpm_patch' not in document or document['kpm_patch'] is not None and not isinstance(document['kpm_patch'], dict)
            or not isinstance(document.get('image_entry'), dict)
            or not isinstance(document.get('runtime_sha256'), dict)
            or not REQUIRED_RUNTIME <= document['runtime_sha256'].keys()
            or any(not isinstance(name, str) or not catalog.SHA256.fullmatch(str(value))
                   for name, value in document['runtime_sha256'].items())
            or not isinstance(template, dict) or template.get('repository') != 'osm0sis/AnyKernel3'
            or not catalog.SHA.fullmatch(str(template.get('ref', '')))):
        raise AssetError('OPlus asset has invalid compiled configuration or official runtime identity')
    if (len(runtime) > 10000 or not any(Path(name).name.casefold().startswith('license') for name in runtime)
            or any(not (name.startswith(('tools/', 'META-INF/')) or '/' not in name and
                         (name.casefold().startswith('license') or name == 'COPYING'))
                   or any(part in ('', '.', '..') for part in name.split('/')) or '\\' in name
                   or name in (MEMBER, PAYLOAD_MEMBER) for name in runtime)):
        raise AssetError('OPlus runtime identity contains unsafe or unexpected members')
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', str(document.get('producer_repository', '')))
            or any(not re.fullmatch(r'[1-9][0-9]*', str(document.get(field, ''))) for field in PRODUCER[1:3])
            or not catalog.SHA.fullmatch(str(document.get('producer_head_sha', '')))):
        raise AssetError('OPlus asset has invalid workflow provenance')


def verify_image(document: dict, image: bytes, payload: bytes | None) -> None:
    if digest(image) != document['image_sha256'] or validate_image(image) != document.get('image_entry'):
        raise AssetError('OPlus Image bytes or ARM64 entry differ from the embedded identity')
    config, origin, checksum = bounded_config(image)
    if payload is not None and config.get('CONFIG_KPM') != 'y':
        raise AssetError('External KPM payload requires its enabled native KPM control bridge')
    if document.get('vendor_sources') and config.get('CONFIG_OPLUS_SCHED_TUNE') != 'y':
        raise AssetError('Restored official scheduler must remain enabled in the actual Image')
    if checksum != document['config_sha256'] or origin != document['config_source']:
        raise AssetError('OPlus actual IKCONFIG differs from its embedded configuration')
    if (config.get('CONFIG_KSU') != 'y' or document['ksu_variant'] == 'BakaSU' and
            config.get('CONFIG_KSU_MULTI_MANAGER_SUPPORT') != 'y'):
        raise AssetError('OPlus actual kernel cannot accept the selected paired manager')
    if any(str(document[field]).encode() not in image for field in ('manager_package', 'manager_cert_sha256')):
        raise AssetError('OPlus Image does not contain its verified paired manager trust identity')
    if b'Linux version ' + document['kernel_release'].encode() not in image:
        raise AssetError('OPlus native release metadata differs from the real Linux banner')
    backend = any(name.startswith('CONFIG_ZRAM_BACKEND_') for name in config)
    actual = {'susfs': config.get('CONFIG_KSU_SUSFS') == 'y',
              'lto': ('thin' if state(config, 'LTO_CLANG_THIN') is True else
                      'full' if state(config, 'LTO_CLANG_FULL') is True else
                      'none' if state(config, 'LTO_NONE') is True else None),
              'bbg': state(config, 'BBG'), 'zram': state(config, 'ZRAM'),
              'lz4kd': bool(state(config, 'ZRAM')) and state(config, 'ZRAM_BACKEND_LZ4KD' if backend else 'CRYPTO_LZ4KD'),
              'ntsync': state(config, 'NTSYNC'), 'rekernel': state(config, 'REKERNEL'),
              'virtualization': state(config, 'KVM'), 'ddk': state(config, 'XINGGUANG_DDK', 'DDK'),
              'kpm_native_config': state(config, 'KPM'), 'kpm': payload is not None}
    for key, value in actual.items():
        if type(document['features'].get(key)) is not type(value) or document['features'][key] != value:
            raise AssetError('OPlus reported feature differs from actual Image: ' + key)
    if actual['susfs']:
        if document.get('susfs_version') != 'v2.3.0' or b'v2.3.0' not in image:
            raise AssetError('OPlus Image does not contain the required SUSFS v2.3.0')
    elif document.get('susfs_version') is not None:
        raise AssetError('OPlus reports SUSFS for a kernel where it is disabled')
    if payload is not None:
        verify_kpm(image, document.get('kpm_patch'), payload)
    elif document.get('kpm_patch') is not None or kpm.MAGIC in image:
        raise AssetError('OPlus KPM payload/report is missing or unexpected')


def read_kernel(path: Path, *, expected_filename: str | None = None) -> dict:
    regular(path)
    with zipfile.ZipFile(path) as archive:
        members = validate_members(archive)
        document = read_member(archive, MEMBER)
        validate_document(document)
        if (expected_filename or path.name) != document['file']:
            raise AssetError('OPlus ZIP filename differs from its embedded native artifact identity')
        expected = set(document['runtime_sha256']) | {'Image', 'anykernel.sh', MEMBER}
        if document['kpm_patch'] is not None:
            expected.add(PAYLOAD_MEMBER)
        if set(members) != expected or any(item.is_dir() for item in members.values()):
            raise AssetError('OPlus ZIP contains missing, duplicate or unexpected runtime files')
        if sum(item.file_size for item in members.values()) > MAX_ZIP or members['Image'].file_size > MAX_IMAGE:
            raise AssetError('OPlus ZIP has an oversized expanded payload')
        for name, checksum in document['runtime_sha256'].items():
            if digest(archive.read(name)) != checksum:
                raise AssetError('OPlus official AnyKernel3 runtime bytes changed')
        script = anykernel_script(document['profile'], document['ksu_variant'], document['compatibility_prefix'])
        if archive.read('anykernel.sh') != script:
            raise AssetError('OPlus platform/full-version/KMI installation guards changed')
        payload = archive.read(PAYLOAD_MEMBER) if PAYLOAD_MEMBER in members else None
        verify_image(document, archive.read('Image'), payload)
        if archive.testzip() is not None:
            raise AssetError('OPlus ZIP CRC verification failed')
        return document


def variants_list(value: str) -> list[str]:
    values = value.split(',') if isinstance(value, str) else []
    if not values or len(values) != len(set(values)) or any(item not in MANAGERS for item in values):
        raise AssetError('Expected explicit unique SukiSU and/or BakaSU variant names')
    return values


def feature_label(value) -> str:
    if value is True:
        return '开启'
    if value is False:
        return '关闭'
    if value is None:
        return '未检测'
    return str(value)


def collect(source: Path, output: Path, matrix: dict, variants: str) -> dict:
    expected_variants = variants_list(variants)
    if not isinstance(matrix, dict) or set(matrix) != {'include'} or not isinstance(matrix['include'], list):
        raise AssetError('Expected the complete prepared OPlus source matrix')
    profiles = [profile_identity(row) for row in matrix['include']]
    identities = {(row['platform'], row['kernel']): row for row in profiles}
    if not profiles or len(identities) != len(profiles):
        raise AssetError('OPlus matrix is empty or duplicates a platform/full-version identity')
    if source.is_symlink() or not source.is_dir() or output.is_symlink():
        raise AssetError('Expected independent native input and report output directories')
    paths = list(source.rglob('*'))
    if len(paths) > 10000 or any(path.is_symlink() for path in paths):
        raise AssetError('Linked or oversized native artifact input trees are refused')
    files = [path for path in paths if path.is_file()]
    names = [path.name for path in files]
    if len(names) != len(set(names)):
        raise AssetError('Native OPlus assets contain duplicate public filenames')
    apk_paths = [path for path in files if path.suffix == '.apk']
    kernel_paths = [path for path in files if path.name.startswith('Oplus-') and path.name.endswith('-AnyKernel3.zip')]
    if len(apk_paths) != len(expected_variants) or len(kernel_paths) != len(profiles) * len(expected_variants) or len(files) != len(apk_paths) + len(kernel_paths):
        raise AssetError('Native OPlus APK/kernel asset set is incomplete or contains unexpected files')
    current = provenance()
    managers = {}
    for path in apk_paths:
        manager = read_manager_apk(path)
        variant = manager['ksu_variant']
        if variant not in expected_variants or variant in managers or manager['package'] != manager_profile(variant)['package']:
            raise AssetError('Unexpected, duplicate or wrongly packaged OPlus manager APK')
        if any(str(manager[field]).casefold() != str(current[field]).casefold() for field in
               ('producer_repository', 'producer_run_id', 'producer_head_sha')):
            raise AssetError('OPlus manager APK belongs to another run or producer commit')
        managers[variant] = manager
    records, seen = [], set()
    for path in kernel_paths:
        kernel = read_kernel(path)
        variant = kernel['ksu_variant']
        key = kernel['profile']['platform'], kernel['profile']['kernel']
        if key not in identities or kernel['profile'] != identities[key] or variant not in managers or (*key, variant) in seen:
            raise AssetError('OPlus kernel does not match a unique prepared source/variant target')
        manager = managers[variant]
        if any(str(kernel[field]).casefold() != str(current[field]).casefold() for field in
               ('producer_repository', 'producer_run_id', 'producer_head_sha')):
            raise AssetError('OPlus kernel belongs to another run or producer commit')
        if int(kernel['producer_run_attempt']) < int(manager['producer_run_attempt']):
            raise AssetError('OPlus kernel predates its selected manager producer attempt')
        pairs = {'manager_package': manager['package'], 'manager_cert_size': manager['manager_cert_size'],
                 'manager_cert_sha256': manager['manager_cert_sha256'], 'manager_apk_filename': manager['file'],
                 'manager_apk_sha256': manager['apk_sha256']}
        pairs.update({field: manager[field] for field in ('ksu_variant', 'ksu_branch', 'ksu_ref', 'ksu_version_code', 'ksu_version_name')})
        if any(str(kernel[field]) != str(value) for field, value in pairs.items()):
            raise AssetError('OPlus kernel does not match its actual signed manager source/certificate/APK bytes')
        seen.add((*key, variant))
        records.append(kernel)
    if seen != {(*key, variant) for key in identities for variant in expected_variants}:
        raise AssetError('OPlus native outputs do not cover the complete prepared build matrix')
    title = ' / '.join(release_title(managers[variant], platform='OnePlus') for variant in sorted(managers))
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise AssetError('OPlus report output must be empty; existing files are preserved')
    hashes = {path.name: digest(regular(path)) for path in files}
    (output / 'SHA256SUMS.txt').write_text(''.join(hashes[name] + '  ' + name + '\n' for name in sorted(hashes)), encoding='utf-8', newline='\n')
    lines = ['# 欧加真全属性内核构建结果', '',
             '来源：官方源码；构建方法参考已固定的 CCTV 配方。刷入前会检查平台及完整内核/Android/KMI。', '',
             '| 管理器 | 版本 |', '|---|---|']
    for variant in sorted(managers):
        manager = managers[variant]
        lines.append('| ' + variant + ' | ' + str(manager['ksu_version_name']) + ' / ' + str(manager['ksu_version_code']) + ' |')
    lines += ['', '## 构建配置', '', '| 项目 | 实际配置 |', '|---|---|']
    labels = {'susfs': 'SUSFS', 'hook': 'Hook 类型', 'mount': '挂载方式', 'lto': 'LTO',
              'bbg': 'BBG', 'zram': 'ZRAM', 'lz4kd': 'LZ4KD', 'kpm': 'KPM', 'ntsync': 'NTsync',
              'rekernel': 'Re-Kernel', 'virtualization': '虚拟化', 'ddk': 'DDK'}
    for field, label in labels.items():
        values = {feature_label(row['features'].get(field)) for row in records}
        lines.append('| ' + label + ' | ' + ', '.join(sorted(values)).replace('|', '\\|') + ' |')
    lines += ['', '## 适用构建范围', '', '| 平台 | 完整内核 | 原生 release | 系统源码依据 |', '|---|---|---|---|']
    for row in sorted(records, key=lambda item: (catalog.order(item['profile']), item['ksu_variant'])):
        profile = row['profile']
        platform = catalog.platform_family(profile['platform'])
        lines.append('| ' + platform + ' | ' + profile['kernel'] + ' | `' + row['kernel_release']
                     + '` | ' + profile['system'].replace('|', '\\|') + ' |')
    lines += ['', '仅证明产物及编译配置配套；同内核版本不代表所有机型或固件均可通刷。', '']
    (output / 'summary.md').write_text('\n'.join(lines), encoding='utf-8', newline='\n')
    (output / 'title.txt').write_text(title, encoding='utf-8')
    return {'count': len(records), 'managers': sorted(managers), 'files': hashes}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    pack = sub.add_parser('package')
    pack.add_argument('--profile', required=True)
    pack.add_argument('--kernel-root', type=Path, required=True)
    pack.add_argument('--image', type=Path, required=True)
    pack.add_argument('--ak3-template', type=Path, required=True)
    pack.add_argument('--output', type=Path, required=True)
    gather = sub.add_parser('collect')
    gather.add_argument('--input', type=Path, required=True)
    gather.add_argument('--output', type=Path, required=True)
    gather.add_argument('--matrix', required=True)
    gather.add_argument('--variants', default=os.environ.get('KSU_VARIANT'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'package':
            result = package(json_input(args.profile), args.kernel_root, args.image, args.ak3_template, args.output)
            if os.environ.get('GITHUB_OUTPUT'):
                with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as out:
                    for key, value in result.items():
                        out.write(key + '=' + value + '\n')
        else:
            result = collect(args.input, args.output, json_input(args.matrix), args.variants)
    except (ValueError, OSError, KeyError, TypeError, struct.error, zipfile.BadZipFile) as error:
        print('OPlus native asset verification stopped: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
