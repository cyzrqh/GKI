#!/usr/bin/env python3
"""Verify and stage unchanged, locally signed manager APKs from Actions cache."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import zipfile
import zlib

from apk_manifest import MAX_MANIFEST, manifest_identity
from embedded_build_metadata import MAX_APK_BYTES, metadata_json, read_manager_apk
from manager_lkm_jobs import KMIS
from manager_variants import MANAGERS, manager_profile


CHANNELS = ('Stable(标准)', 'Latest(最新)')
MAX_ADOPTION_BYTES = 16 * 1024
SHA1 = re.compile(r'[0-9a-f]{40}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z')
RECIPE_INPUTS = (
    'config/bakasu-kpm-port.json', 'config/manager-ddk-images.json',
    'tools/additional_manager_jobs.py', 'tools/additional_manager_kpm.py',
    'tools/additional_manager_kpm_ui.py', 'tools/apk_manifest.py',
    'tools/build_additional_manager.sh', 'tools/build_bakasu_manager.sh',
    'tools/build_fina_manager.py', 'tools/build_manager_extras.sh',
    'tools/build_manager_extras_container.sh', 'tools/build_manager_native.sh',
    'tools/build_sukisu_manager.sh', 'tools/checkout_ksu_snapshot.py',
    'tools/configure_bakasu_kpm_ui.py', 'tools/configure_bakasu_manager_notice.py',
    'tools/configure_sukisu_kpm_ui.py', 'tools/embedded_build_metadata.py',
    'tools/generate_workflow.py', 'tools/kpm_theme.py', 'tools/manager_full_native.py',
    'tools/manager_lkm_jobs.py', 'tools/manager_ui_defaults.py', 'tools/manager_variants.py',
    'tools/patches/bakasu-kpm.patch', 'tools/prepare_additional_gradle.py',
    'tools/prepare_manager_icons.py', 'tools/prepare_manager_lkm.py',
    'tools/restore_bakasu_kpm.py', 'tools/reusable_manager_apk.py',
    'tools/sign_sukisu_manager.py', 'tools/snapshot_ksu_source.py',
    'tools/verify_manager_boot_patch.py',
)
RECIPE_PATTERNS = ('tools/patches/bakasu-kpm-base/*.source', 'tools/templates/*.kt')


def compact(document: object) -> str:
    return json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def matches(value: object, pattern: re.Pattern) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def positive(value: object, maximum: int = 2**63 - 1) -> int:
    if type(value) not in (int, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', str(value)):
        raise ValueError('Invalid positive identity number')
    number = int(value)
    if number > maximum:
        raise ValueError('Identity number exceeds its limit')
    return number


def read_regular(path: Path, limit: int) -> bytes:
    if path.resolve() != path.absolute() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError('Expected a bounded regular file: ' + path.name)
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if not 0 < len(data) <= limit:
        raise ValueError('File changed while reading: ' + path.name)
    return data


def recipe_hash(root: Path) -> str:
    """Bind signed output to local build, patch, ABI lock and signing inputs."""
    root = root.resolve(strict=True)
    files = {root / path for path in RECIPE_INPUTS}
    for pattern in RECIPE_PATTERNS:
        matched = set(root.glob(pattern))
        if not matched:
            raise ValueError('Missing manager recipe inputs: ' + pattern)
        files.update(matched)
    manifest = {path.relative_to(root).as_posix(): hashlib.sha256(read_regular(path, 32 * 1024 * 1024)).hexdigest()
                for path in sorted(files)}
    return hashlib.sha256(compact(manifest).encode('ascii')).hexdigest()


def manager_source_identity(document: dict, ref: str) -> dict:
    fields = {'ref', 'git_tree', 'git_count', 'git_describe'}
    if (not isinstance(document, dict) or set(document) not in (fields, fields | {'susfs_sources'})
            or document.get('ref') != ref or not matches(document.get('git_tree'), SHA1)
            or type(document.get('git_count')) is not int or not 1 <= document['git_count'] <= 2**63 - 1
            or not isinstance(description := document.get('git_describe'), str) or not 1 <= len(description) <= 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in description)):
        raise ValueError('Invalid snapshot source tree, full history or tag-derived manager version')
    # The manager's APK/LKM builds use the same official source for either
    # kernel SUSFS mode. A new tag can change the APK version on the same HEAD.
    return {key: document[key] for key in fields}


def manager_selection(variant: str, channel: str, ref: str, cert_size: int | str,
                      cert_sha256: str, recipe_sha256: str, source_identity: dict) -> dict:
    package = manager_profile(variant)['package']
    size = positive(cert_size, 4096)
    if (channel not in CHANNELS or size < 64 or not matches(ref, SHA1)
            or not matches(cert_sha256, SHA256) or not matches(recipe_sha256, SHA256)):
        raise ValueError('Invalid formal/latest manager source, recipe or signing identity')
    return {'ksu_variant': variant, 'ksu_branch': channel, 'ksu_ref': ref, 'package': package,
            'manager_cert_size': size, 'manager_cert_sha256': cert_sha256,
            'manager_recipe_sha256': recipe_sha256, 'manager_source_identity': manager_source_identity(source_identity, ref)}


def workflow_consumer() -> dict:
    repository, head = os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GITHUB_SHA', '')
    if not matches(repository, REPOSITORY) or not matches(head, SHA1):
        raise ValueError('Invalid consuming workflow repository or head SHA')
    return {'repository': repository, 'head_sha': head, 'run_id': positive(os.environ.get('GITHUB_RUN_ID')),
            'run_attempt': positive(os.environ.get('GITHUB_RUN_ATTEMPT'))}


def cache_key(selection: dict) -> str:
    # One official source can be both a release tag and the current branch head.
    identity = {key: value for key, value in selection.items() if key != 'ksu_branch'}
    return 'gki-manager-apk-v1-' + hashlib.sha256(compact(identity).encode('ascii')).hexdigest()


def rows_by_kmi(rows: object) -> dict:
    if (not isinstance(rows, list) or len(rows) != len(KMIS)
            or any(not isinstance(row, dict) or row.get('kmi') not in KMIS for row in rows)):
        raise ValueError('Manager native proof requires all eight KMI records')
    result = {row['kmi']: row for row in rows}
    if set(result) != set(KMIS):
        raise ValueError('Manager native proof contains duplicate or missing KMI records')
    return result


def native_proof(module_report: Path, boot_report: Path, identity: dict) -> dict:
    """Bind the successful same-source LKM and synthetic boot checks before signing."""
    lkm_raw, boot_raw = (read_regular(path, 64 * 1024) for path in (module_report, boot_report))
    lkm, boot = metadata_json(lkm_raw), metadata_json(boot_raw)
    source = {'repository': manager_profile(identity['ksu_variant'])['repository'], 'ksu_ref': identity['ksu_ref'],
              **{key: value for key, value in identity['manager_source_identity'].items() if key != 'ref'}}
    signing = {'manager_package': identity['package'], 'manager_cert_size': identity['manager_cert_size'],
               'manager_cert_sha256': identity['manager_cert_sha256']}
    kmis = sorted(KMIS)
    provenance = (*source, 'ksu_version_code', 'bakasu_kpm_port_sha256', 'additional_kpm_port_sha256')
    if (any(type(report.get('schema_version')) is not int or report['schema_version'] != 1
            for report in (lkm, boot))
            or lkm.get('supported_kmis') != kmis
            or boot.get('supported_kmis') != ['aarch64/' + kmi for kmi in kmis]
            or any(lkm.get(key) != value for key, value in {**source, **signing}.items())
            or any(lkm.get(key) != boot.get(key) for key in provenance)
            or lkm.get('mode') != 'native-lkm'
            or boot.get('verification') != 'official-native-cli-synthetic-image-patch-and-restore'
            or boot.get('synthetic_fixture') is not True or boot.get('fixture_bootable') is not False
            or boot.get('device_boot_test') is not False
            or any(not matches(boot.get(key), SHA256) for key in ('fixture_sha256', 'host_ksud_sha256'))):
        raise ValueError('Manager native reports do not match the selected source and signing identity')
    modules, checks = rows_by_kmi(lkm.get('modules')), rows_by_kmi(boot.get('results'))
    for kmi in KMIS:
        module, check = modules[kmi], checks[kmi]
        if (module.get('arch') != 'aarch64' or module.get('file') != kmi + '_kernelsu.ko'
                or any(module.get(key) != lkm.get(key) for key in (*provenance, *signing))
                or check.get('module_sha256') != module.get('sha256')
                or any(not matches(check.get(key), SHA256) for key in (
                    'module_sha256', 'image_sha256', 'restored_sha256', 'ksuinit_sha256', 'original_init_sha256'))):
            raise ValueError('Manager boot evidence differs from the staged native module: ' + kmi)
    return {'schema_version': 1, 'supported_kmis': kmis,
            'lkm_report_sha256': hashlib.sha256(lkm_raw).hexdigest(),
            'boot_report_sha256': hashlib.sha256(boot_raw).hexdigest()}


def validate_cached_metadata(metadata: dict, *, repository: str, selection: dict) -> None:
    if not isinstance(selection, dict) or set(selection) != {
        'ksu_variant', 'ksu_branch', 'ksu_ref', 'package', 'manager_cert_size',
        'manager_cert_sha256', 'manager_recipe_sha256', 'manager_source_identity',
    }:
        raise ValueError('Invalid cached manager selection fields')
    normalized = manager_selection(selection['ksu_variant'], selection['ksu_branch'], selection['ksu_ref'],
                                   selection['manager_cert_size'], selection['manager_cert_sha256'],
                                   selection['manager_recipe_sha256'], selection['manager_source_identity'])
    if compact(selection) != compact(normalized):
        raise ValueError('Cached manager selection differs from its canonical identity')
    proof = metadata.get('manager_native_proof')
    if (not isinstance(proof, dict)
            or set(proof) != {'schema_version', 'supported_kmis', 'lkm_report_sha256', 'boot_report_sha256'}
            or type(proof['schema_version']) is not int or proof['schema_version'] != 1
            or proof['supported_kmis'] != sorted(KMIS)
            or not all(matches(proof[key], SHA256) for key in ('lkm_report_sha256', 'boot_report_sha256'))):
        raise ValueError('Signed APK lacks the complete native build proof')
    if (metadata['producer_repository'].casefold() != repository.casefold()
            or metadata.get('ksu_branch') not in CHANNELS
            or any(compact(metadata.get(key)) != compact(value) for key, value in selection.items()
                   if key not in {'ksu_branch', 'manager_cert_size'})
            or positive(metadata.get('manager_cert_size'), 4096) != selection['manager_cert_size']):
        raise ValueError('Signed APK source, signing identity or recipe differs from the selected manager')


def validate_adoption(receipt: dict, metadata: dict, *, consumer: dict, selection: dict) -> dict:
    """Project the current artifact acceptance only after matching its signed original."""
    expected = {'schema_version': 1, 'consumer': consumer, 'selection': selection, 'producer': metadata}
    if compact(receipt) != compact(expected):
        raise ValueError('Cached APK adoption does not match this run, selection and signed producer')
    validate_cached_metadata(metadata, repository=consumer['repository'], selection=selection)
    return {**metadata, 'ksu_branch': selection['ksu_branch'], 'producer_repository': consumer['repository'],
            'producer_head_sha': consumer['head_sha'], 'producer_run_id': consumer['run_id'],
            'producer_run_attempt': consumer['run_attempt']}


def stage(cache_dir: Path, output_dir: Path, *, consumer: dict, selection: dict) -> dict:
    if cache_dir.resolve() != cache_dir.absolute() or not cache_dir.is_dir():
        raise ValueError('No reusable manager APK was restored')
    files = list(cache_dir.iterdir())
    if len(files) != 1 or files[0].suffix != '.apk':
        raise ValueError('Expected exactly one original APK in the cache')
    path = files[0]
    data = read_regular(path, MAX_APK_BYTES)
    metadata = read_manager_apk(data, filename=path.name)
    validate_cached_metadata(metadata, repository=consumer['repository'], selection=selection)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.getinfo('AndroidManifest.xml').file_size > MAX_MANIFEST:
            raise ValueError('Cached APK manifest exceeds its size limit')
        actual = manifest_identity(archive.read('AndroidManifest.xml'))
    if actual != {'package': metadata['package'], 'version_name': metadata['ksu_version_name'],
                  'version_code': positive(metadata['ksu_version_code'], 2100000000)}:
        raise ValueError('Cached APK manifest differs from its signed package and native versions')
    receipt = {'schema_version': 1, 'consumer': consumer, 'selection': selection, 'producer': metadata}
    adoption = compact(receipt)
    if len(adoption) > MAX_ADOPTION_BYTES:
        raise ValueError('Cached APK adoption receipt is too large')
    if output_dir.resolve() != output_dir.absolute():
        raise ValueError('Reusable manager APK output must be a normal directory')
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / path.name
    if destination.exists() or destination.is_symlink():
        raise ValueError('Reusable manager APK output already exists')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output_dir, prefix='.gki-adopt-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'manager_apk_reused': 'true', 'manager_adoption': adoption,
            'manager_package': metadata['package'], 'manager_cert_size': selection['manager_cert_size'],
            'manager_cert_sha256': metadata['manager_cert_sha256'], 'apk_sha256': metadata['apk_sha256'],
            'manager_version_name': metadata['ksu_version_name'], 'manager_version_code': actual['version_code'],
            'apk_filename': path.name}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('key', 'stage'))
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--variant', required=True, choices=MANAGERS)
    parser.add_argument('--channel', required=True, choices=CHANNELS)
    parser.add_argument('--ksu-ref', required=True)
    parser.add_argument('--cert-size', required=True)
    parser.add_argument('--cert-sha256', required=True)
    parser.add_argument('--source-identity', required=True)
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args(argv)
    if args.operation == 'stage' and (args.cache_dir is None or args.output_dir is None):
        parser.error('stage requires --cache-dir and --output-dir')
    try:
        recipe = recipe_hash(args.root)
        source_identity = metadata_json(args.source_identity.encode('utf-8'))
        selection = manager_selection(args.variant, args.channel, args.ksu_ref, args.cert_size, args.cert_sha256,
                                      recipe, source_identity)
        result = {'apk_cache_key': cache_key(selection), 'manager_recipe_sha256': recipe}
        if args.operation == 'stage':
            consumer = workflow_consumer()
            try:
                result.update(stage(args.cache_dir, args.output_dir, consumer=consumer, selection=selection))
            except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, struct.error, zlib.error) as error:
                print('Manager APK cache miss: ' + str(error), file=sys.stderr)
                result['manager_apk_reused'] = 'false'
        if output := os.environ.get('GITHUB_OUTPUT'):
            with Path(output).open('a', encoding='utf-8', newline='\n') as stream:
                for key, value in result.items():
                    stream.write(f'{key}={value}\n')
        else:
            print(compact(result))
    except (OSError, ValueError) as error:
        print('Cannot determine reusable manager APK identity: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
