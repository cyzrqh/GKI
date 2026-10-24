#!/usr/bin/env python3
"""Re-sign the measured existing manager payload with its fixed trusted identity."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import zipfile

from embedded_build_metadata import APK_MEMBER, MAX_APK_BYTES, read_manager_apk, validate_members
import sign_sukisu_manager as signing
from snapshot_ksu_source import temporary_directory

PACKAGE = 'com.cyzrqh.gki.sukisu'
CERTIFICATE_SHA256 = 'c8e9af2b51e0f76b4e9133726c17a8034a131fec4235f6b779ab8b1e83127c12'
CERTIFICATE_SIZE = 730
VERSION_CODE = 41065
JAR_SIGNATURE = re.compile(r'META-INF/(?:MANIFEST\.MF|[^/]+\.(?:SF|RSA|DSA|EC))\Z', re.I)


class RepairError(ValueError):
    pass


def measure_apk(path: Path) -> dict:
    tools = signing.sdk_tools()
    badging = signing.run_command([tools['aapt2'], 'dump', 'badging', str(path)], '', ())
    return signing.parse_apk_metadata(badging)


def payload_inventory(path: Path) -> dict:
    """Compare every retained uncompressed member, including native code/resources."""
    with zipfile.ZipFile(path) as archive:
        members = validate_members(archive)
        return {name: hashlib.sha256(archive.read(info)).hexdigest() for name, info in members.items()
                if name != APK_MEMBER and not JAR_SIGNATURE.fullmatch(name)}


def unsigned_copy(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, 'w') as target:
        validate_members(original)
        target.comment = original.comment
        for entry in original.infolist():
            if entry.filename != APK_MEMBER and not JAR_SIGNATURE.fullmatch(entry.filename):
                target.writestr(copy.copy(entry), original.read(entry))


@contextmanager
def source_environment(reference: str, branch: str):
    values = {'KSU_REF': reference, 'KSU_BRANCH': branch}
    previous = {key: os.environ.get(key) for key in values}
    if any(previous[key] not in (None, '', value) for key, value in values.items()):
        raise RepairError('The repair workflow selected a different original SukiSU source')
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def repair(input_path: Path, expected_sha256: str, expected_ref: str, output_dir: Path) -> dict:
    input_path, output_dir = Path(input_path), Path(output_dir).absolute()
    if (not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
            or not re.fullmatch(r'[0-9a-f]{40}', expected_ref)):
        raise RepairError('Repair requires fixed full input SHA256 and SukiSU commit SHA')
    if input_path.is_symlink() or not input_path.is_file() or not 0 < input_path.stat().st_size <= MAX_APK_BYTES:
        raise RepairError('Repair input must be a bounded original APK regular file')
    raw = input_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RepairError('Original manager APK SHA256 does not match the explicitly selected input')
    # Legacy schemes are permitted only for the measured input. RSA, Android
    # chunk digests, public certificate and embedded source identity stay checked.
    # A downloaded local input may use a temporary alias; its signed embedded
    # filename, rather than that local alias, remains the final native name.
    original = read_manager_apk(raw, allow_legacy_signing_blocks=True)
    if (original['package'] != PACKAGE or original['manager_cert_sha256'] != CERTIFICATE_SHA256
            or int(original['manager_cert_size']) != CERTIFICATE_SIZE
            or original['ksu_ref'] != expected_ref or int(original['ksu_version_code']) != VERSION_CODE):
        raise RepairError('Original APK package, certificate, source or version is not the selected fixed manager')
    expected_android = {'manager_package': original['package'],
                        'manager_version_code': int(original['ksu_version_code']),
                        'manager_version_name': original['ksu_version_name']}
    if (output_dir.is_symlink() or not output_dir.parent.is_dir()
            or output_dir.parent.resolve() != output_dir.parent
            or output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir()))):
        raise RepairError('Repair output must be an empty unlinked directory with an existing parent')
    filename = original['file']
    with temporary_directory(output_dir.parent, '.gki-manager-repair-') as temporary:
        # Every later SDK/ZIP read uses these same measured bytes, even if the
        # caller replaces the downloaded input path while repair is running.
        frozen = temporary / filename
        frozen.write_bytes(raw)
        actual = measure_apk(frozen)
        if actual != expected_android:
            raise RepairError('Actual original APK package/version differs from its cryptographically verified identity')
        build_metadata = {'source_apk_filename': filename.removesuffix('.apk') + '-unsigned.apk',
                          'apk_filename': filename, **actual}
        original_payload = payload_inventory(frozen)
        unsigned = temporary / build_metadata['source_apk_filename']
        unsigned_copy(frozen, unsigned)
        if payload_inventory(unsigned) != original_payload:
            raise RepairError('APK payload changed while preparing its unsigned repair copy')
        build_path = temporary / 'manager-build-metadata.json'
        build_path.write_text(json.dumps(build_metadata, ensure_ascii=True), encoding='utf-8')
        with source_environment(expected_ref, original['ksu_branch']):
            signed_metadata = signing.sign_manager(unsigned, output_dir=temporary / 'signed', build_metadata=build_path)
        signed = temporary / 'signed' / filename
        verified = read_manager_apk(signed)  # Default strict native SukiSU scheme validation.
        for field in ('file', 'package', 'ksu_ref', 'ksu_branch', 'ksu_version_name', 'manager_cert_sha256'):
            if verified.get(field) != original[field]:
                raise RepairError('Repaired manager changed its original public identity: ' + field)
        for field in ('ksu_version_code', 'manager_cert_size'):
            if str(verified.get(field)) != str(original[field]):
                raise RepairError('Repaired manager changed its original public identity: ' + field)
        for field, env in (('producer_repository', 'GITHUB_REPOSITORY'), ('producer_run_id', 'GITHUB_RUN_ID'),
                           ('producer_run_attempt', 'GITHUB_RUN_ATTEMPT'), ('producer_head_sha', 'GITHUB_SHA')):
            if str(verified.get(field)) != os.environ.get(env, ''):
                raise RepairError('Repaired APK lacks this repair workflow provenance')
        if payload_inventory(signed) != original_payload:
            raise RepairError('Repaired APK changed compiled code, resources or native module payloads')
        if (signed_metadata.get('apk_filename') != filename or signed_metadata.get('apk_sha256') != verified['apk_sha256']
                or signed_metadata.get('manager_package') != PACKAGE
                or signed_metadata.get('manager_cert_sha256') != CERTIFICATE_SHA256):
            raise RepairError('Signer outputs do not match the verified repaired APK')
        output_dir.mkdir(exist_ok=True)
        destination = output_dir / filename
        if destination.exists() or destination.is_symlink():
            raise RepairError('Refusing to overwrite an existing repaired manager APK')
        # No sidecars, temporary unsigned files, logs or signing material enter
        # the public output. Hard-link publication fails if this name appears.
        os.link(signed, destination)
    return signed_metadata


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--expected-ref', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = repair(args.input, args.expected_sha256, args.expected_ref, args.output_dir)
    except (OSError, ValueError, signing.SigningError, zipfile.BadZipFile):
        print('Manager signature repair stopped: original identity, payload or strict repaired signature could not be verified.', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
