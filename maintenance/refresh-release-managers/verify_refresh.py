"""Verify a UI-only manager APK refresh; never build, sign or publish assets."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import zipfile


REPOSITORY = 'cyzrqh/GKI'
CHECKS = ('version', 'package', 'signer', 'source', 'native_payload', 'abi_set', 'lkm_payload', 'ui_only')
DEX = re.compile(r'classes(?:[2-9]|[1-9][0-9]+)?\.dex\Z')
SIGNATURE = re.compile(r'META-INF/(?:MANIFEST\.MF|[^/]+\.(?:SF|RSA|DSA|EC))\Z', re.I)
PRODUCER_FIELDS = {'producer_repository', 'producer_head_sha', 'producer_run_id',
                   'producer_run_attempt', 'built_at_utc', 'apk_sha256'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def inspect_payload(raw):
    from apk_manifest import manifest_identity
    from embedded_build_metadata import APK_MEMBER, validate_members
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = validate_members(archive)
        manifest_raw = archive.read('AndroidManifest.xml')
        all_files, native, dex_files = {}, {}, {}
        for name, info in members.items():
            if info.is_dir():
                continue
            payload = archive.read(info)
            row = {'size': len(payload), 'sha256': digest(payload)}
            all_files[name] = row
            if DEX.fullmatch(name):
                require(re.match(rb'dex\n[0-9]{3}\0', payload) is not None, 'APK contains invalid DEX bytes')
                dex_files[name] = row
            # All assets are frozen, not just files named like native code.
            # The signed build identity is the sole asset allowed to change.
            if (name != APK_MEMBER and (name.startswith(('lib/', 'assets/'))
                    or name.endswith(('.so', '.ko', '.kpm'))
                    or payload.startswith((b'\x7fELF', b'KP1158\0\0'))
                    or payload[56:60] == b'ARMd')):
                native[name] = row
        require('classes.dex' in dex_files, 'APK lacks its primary DEX')
        abis = sorted({name.split('/')[1] for name in native if name.startswith('lib/')})
        require(abis, 'APK has no native ABI libraries')
        for abi in abis:
            for filename in ('libksud.so', 'libkernelsu.so'):
                name = 'lib/' + abi + '/' + filename
                require(name in native, 'APK lacks a required native library: ' + name)
                require(archive.read(name).startswith(b'\x7fELF'), 'Native library is not ELF: ' + name)
        return {'manifest': manifest_identity(manifest_raw), 'manifest_sha256': digest(manifest_raw),
                'all_files': all_files, 'native': native, 'dex': dex_files, 'abis': abis}


def frozen_metadata(metadata):
    result = {key: value for key, value in metadata.items() if key not in PRODUCER_FIELDS}
    for key in ('ksu_version_code', 'manager_cert_size'):
        result[key] = int(result[key])
    return result


def ui_member(name):
    from embedded_build_metadata import APK_MEMBER
    return (DEX.fullmatch(name) is not None or name.startswith('res/') or name == 'resources.arsc'
            or name == APK_MEMBER or SIGNATURE.fullmatch(name) is not None
            or name.startswith('META-INF/') and name.endswith('.kotlin_module'))


def sdk_verify(sdk, original, updated):
    if sdk is None:
        return {'performed': False, 'reason': 'No SDK requested; strict APK-v2/native signer verification still performed'}
    directory = sdk / 'build-tools' / '37.0.0'
    if not directory.is_dir():
        directory = sdk  # Accept the exact build-tools directory too.
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('SUKISU_APK_KEYSTORE', 'SUKISU_APK_PASSWORD', 'GH_TOKEN', 'GITHUB_TOKEN')}
    for name, options in (('apksigner', ['verify', '--verbose', '--print-certs']),
                          ('zipalign', ['-c', '-P', '16', '4'])):
        tool = next((path for path in (directory / name, directory / (name + '.exe'),
                                     directory / (name + '.bat')) if path.is_file()), None)
        require(tool is not None, 'Android build-tools 37.0.0 is missing ' + name)
        for label, path in (('original', original), ('updated', updated)):
            result = subprocess.run([str(tool), *options, str(path.resolve(strict=True))],
                                    capture_output=True, timeout=180, env=environment)
            require(result.returncode == 0, 'SDK ' + name + ' rejected the ' + label + ' APK')
    return {'performed': True, 'apksigner': 'passed', 'zipalign_16k': 'passed',
            'build_tools_directory': str(directory.resolve())}


def verify(args):
    from embedded_build_metadata import MAX_APK_BYTES, read_manager_apk, verify_sukisu_apk
    for value, label in ((args.expected_source_ref, 'manager source'), (args.ui_head, 'UI producer')):
        require(re.fullmatch(r'[0-9a-f]{40}', value) is not None, 'Expected a full lowercase ' + label + ' SHA')
    for path in (args.original, args.updated):
        require(not path.is_symlink() and path.is_file() and 0 < path.stat().st_size <= MAX_APK_BYTES,
                'APK input must be a bounded regular file')
    original_raw, updated_raw = args.original.read_bytes(), args.updated.read_bytes()
    # Parse from frozen bytes; temporary download aliases do not change the
    # signed public filename embedded inside the APK.
    original = read_manager_apk(original_raw)
    updated = read_manager_apk(updated_raw)
    original_certificate = verify_sukisu_apk(original_raw)
    updated_certificate = verify_sukisu_apk(updated_raw)
    require(original_certificate == updated_certificate, 'Actual APK signing certificate changed')
    require(original['ksu_ref'] == updated['ksu_ref'] == args.expected_source_ref,
            'Manager source differs from the original kernel pairing or expected source')
    require(updated['producer_repository'] == REPOSITORY and updated['producer_head_sha'] == args.ui_head,
            'Updated APK does not carry the real current maintenance producer')
    require(frozen_metadata(original) == frozen_metadata(updated),
            'A frozen manager identity field changed beyond producer/build time')
    before, after = inspect_payload(original_raw), inspect_payload(updated_raw)
    for metadata, payload in ((original, before), (updated, after)):
        require(payload['manifest'] == {'package': metadata['package'],
                    'version_code': int(metadata['ksu_version_code']),
                    'version_name': metadata['ksu_version_name']},
                'Compiled Android manifest differs from the signed metadata')
    require(before['manifest_sha256'] == after['manifest_sha256'],
            'Android manifest changed beyond the UI-only replacement scope')
    require(before['abis'] == after['abis'], 'Original native ABI set changed')
    require(before['native'] == after['native'],
            'A native library, embedded LKM/KPM payload or another existing asset changed')
    require(before['dex'] != after['dex'], 'DEX did not change; this is not a rebuilt UI replacement')
    changed = sorted(name for name in set(before['all_files']) | set(after['all_files'])
                     if before['all_files'].get(name) != after['all_files'].get(name))
    require(all(ui_member(name) for name in changed), 'An unreviewed non-UI ZIP member changed')
    old_sha, new_sha = digest(original_raw), digest(updated_raw)
    require(old_sha != new_sha, 'Updated APK bytes did not change')
    sdk = sdk_verify(args.sdk, args.original, args.updated)
    return {
        'schema_version': 1, 'status': 'passed', 'verified': True,
        'old_apk_sha256': old_sha, 'new_apk_sha256': new_sha,
        'old_sha256': old_sha, 'new_sha256': new_sha,
        'old_size': len(original_raw), 'new_size': len(updated_raw),
        'asset_name': original['file'],
        'identity': {'variant': original['ksu_variant'], 'package': original['package'],
                     'version_code': int(original['ksu_version_code']), 'version_name': original['ksu_version_name'],
                     'ksu_ref': original['ksu_ref'], 'certificate_sha256': digest(updated_certificate)},
        'checks': {name: True for name in CHECKS},
        'producer': {'repository': REPOSITORY, 'head_sha': updated['producer_head_sha'],
                     'run_id': int(updated['producer_run_id']), 'run_attempt': int(updated['producer_run_attempt'])},
        'old_metadata': original, 'original_metadata': original, 'new_metadata': updated,
        'manifest': {'original': before['manifest'], 'updated': after['manifest'],
                     'original_sha256': before['manifest_sha256'], 'updated_sha256': after['manifest_sha256']},
        'certificate': {'size': len(updated_certificate), 'sha256': digest(updated_certificate),
                        'actual_der_bytes_equal': True, 'strict_v2_native_policy': True},
        'native_inventory': after['native'], 'abis': after['abis'],
        'lkm_payload_basis': 'All libksud.so bytes and every other existing lib/asset byte are identical; embedded LKMs therefore remain identical',
        'dex': {'original': before['dex'], 'updated': after['dex']},
        'changed_members': changed, 'sdk': sdk,
        'kernel_binding_note': 'Original AK3 metadata remains bound to old_apk_sha256; this receipt maps that original APK to its verified UI-only successor',
    }


def write_report(path, report):
    path = path.absolute()
    require(not path.is_symlink() and path.name not in ('', '.', '..'), 'Refusing a linked report output')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--updated', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-source-ref', required=True)
    parser.add_argument('--ui-head', required=True)
    parser.add_argument('--sdk', type=Path)
    default_tools = Path.cwd() / 'tools'
    if not (default_tools / 'embedded_build_metadata.py').is_file():
        default_tools = Path(r'D:\666\gki-kpm-theme-20261106\tools')
    parser.add_argument('--tools', type=Path, default=default_tools)
    args = parser.parse_args()
    require(args.output.suffix.lower() == '.json', 'Verification report must be a JSON file')
    require(args.output.resolve() not in (args.original.resolve(), args.updated.resolve()),
            'Verification report must not overwrite an APK input')
    # A crash/interruption must never leave an earlier success receipt reusable.
    write_report(args.output, {'schema_version': 1, 'status': 'checking', 'verified': False})
    try:
        sys.path.insert(0, str(args.tools.resolve(strict=True)))
        report = verify(args)
    except Exception as error:
        report = {'schema_version': 1, 'status': 'failed', 'verified': False,
                  'error': type(error).__name__ + ': ' + str(error),
                  'checks': {name: False for name in CHECKS}}
        write_report(args.output, report)
        print(json.dumps({'status': 'failed', 'error': report['error']}, ensure_ascii=False), file=sys.stderr)
        return 1
    write_report(args.output, report)
    print(json.dumps({'status': 'passed', 'verified': True, 'output': str(args.output),
                      'old_apk_sha256': report['old_apk_sha256'], 'new_apk_sha256': report['new_apk_sha256']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
