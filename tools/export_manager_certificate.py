#!/usr/bin/env python3
"""Export public manager-certificate identity without building or reading an APK."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import sign_sukisu_manager as signing
from manager_variants import MANAGERS, manager_profile


MANAGER_PACKAGE = 'com.cyzrqh.gki.sukisu'


def export_certificate(output: Path, variant: str = 'SukiSU') -> dict[str, str | int]:
    package = manager_profile(variant)['package']
    output = Path(output).resolve()
    if any(ord(character) < 32 for character in str(output)):
        raise signing.SigningError('The public certificate JSON path contains a control character.')
    temporary_root = os.environ.get('RUNNER_TEMP', '')
    if not temporary_root or not Path(temporary_root).is_dir():
        raise signing.SigningError('RUNNER_TEMP must be an existing private temporary directory.')
    keytool = shutil.which('keytool')
    if keytool is None:
        raise signing.SigningError('Java keytool was not found; it is required to export the public certificate.')
    keystore_bytes, password, secrets = signing.signing_material()
    with tempfile.TemporaryDirectory(prefix='gki-public-cert-', dir=temporary_root) as private:
        directory = Path(private)
        keystore = directory / 'manager.p12'
        certificate_path = directory / 'certificate.der'
        descriptor = os.open(keystore, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(keystore_bytes)
        keystore.chmod(0o600)
        signing.run_command([
            keytool, '-exportcert', '-storetype', 'PKCS12', '-keystore', str(keystore),
            '-alias', signing.KEY_ALIAS, '-storepass:env', signing.PASSWORD_ENV,
            '-file', str(certificate_path),
        ], password, secrets, needs_password=True)
        try:
            certificate = certificate_path.read_bytes()
        except OSError:
            raise signing.SigningError('keytool did not export the manager certificate.') from None
        if not certificate or len(certificate) > 64 * 1024:
            raise signing.SigningError('keytool did not export a valid-sized DER certificate.')
        # Validate the exported X.509 certificate using the same installed
        # Java tool; no Android SDK or Python crypto dependency is required.
        signing.run_command([keytool, '-printcert', '-file', str(certificate_path)], password, secrets)
        metadata = {'schema_version': 1, 'manager_package': package,
                    'manager_cert_size': len(certificate),
                    'manager_cert_sha256': hashlib.sha256(certificate).hexdigest()}

    # The private directory has been removed before any affirmative public
    # output appears. Publish only this allowlisted JSON, never the keystore,
    # password, or any path within the temporary private directory.
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        descriptor, name = tempfile.mkstemp(prefix='.gki-public-cert-', suffix='.json', dir=output.parent)
        staged = Path(name)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(metadata, ensure_ascii=True, sort_keys=True, indent=2) + '\n')
        os.replace(staged, output)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    signing.write_outputs({'package': metadata['manager_package'],
                           'cert_size': metadata['manager_cert_size'],
                           'cert_sha256': metadata['manager_cert_sha256'], 'public_json': str(output)})
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--variant', choices=MANAGERS, default=os.environ.get('KSU_VARIANT', 'SukiSU'))
    args = parser.parse_args(argv)
    try:
        metadata = export_certificate(args.output, args.variant)
    except (signing.SigningError, OSError, UnicodeError, ValueError) as error:
        encoded = os.environ.get(signing.KEYSTORE_ENV, '')
        values = (encoded, ''.join(encoded.split()), os.environ.get(signing.PASSWORD_ENV, ''))
        print('Manager certificate export stopped: ' + signing.redact(str(error), values), file=sys.stderr)
        return 1
    print(json.dumps(metadata, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
