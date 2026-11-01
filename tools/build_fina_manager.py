"""Build FinaSU through its own full native packaging driver."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import struct
import subprocess
import tempfile
import zipfile

from apk_manifest import manifest_identity
from manager_lkm_jobs import KMIS
from manager_variants import manager_profile, valid_manager_apk_filename
from prepare_additional_gradle import prepare
from sign_sukisu_manager import redact

BUILD_ALIAS = 'gki-fina-build-only'


def generate_build_key(key: Path, password: str, environment: dict) -> None:
    """The canonical repacker needs a key; never give it the release key."""
    keytool = shutil.which('keytool')
    if not keytool:
        raise ValueError('Java keytool is required for the disposable FinaSU build key')
    result = subprocess.run([keytool, '-genkeypair', '-storetype', 'PKCS12',
        '-keystore', str(key), '-alias', BUILD_ALIAS, '-keyalg', 'RSA', '-keysize', '2048',
        '-dname', 'CN=Disposable FinaSU build key', '-validity', '1', '-noprompt',
        '-storepass:env', 'GKI_FINA_BUILD_PASSWORD', '-keypass:env', 'GKI_FINA_BUILD_PASSWORD'],
        env={**environment, 'GKI_FINA_BUILD_PASSWORD': password}, capture_output=True, timeout=90)
    if result.returncode or not key.is_file():
        raise ValueError('Cannot generate the disposable FinaSU build key')
    key.chmod(0o600)


def build(source: Path, output: Path):
    source = source.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    prepare(source, 'FinaSU')
    # Build the vendored engines through their native entry points as well;
    # an APK containing the page alone would silently lose FinaSU features.
    environment = {**{name: value for name, value in os.environ.items()
        if name not in ('SUKISU_APK_KEYSTORE', 'SUKISU_APK_PASSWORD')
        and not name.startswith('ORG_GRADLE_PROJECT_KEY')},
                   'ABIS': 'arm64-v8a', 'KMIS': ' '.join(KMIS),
                   'CARGO_TARGET_DIR': str(source / 'target'),
                   'OUT_DIR': str(output / 'native-fina'), 'APP_BUILD_TYPE': 'release',
                   'KSUD_BUILD_TYPE': 'release', 'KSU_PACKAGE_NAME': manager_profile('FinaSU')['package']}
    with tempfile.TemporaryDirectory(prefix='gki-fina-signing-', dir=os.environ.get('RUNNER_TEMP')) as directory:
        private = Path(directory)
        key = private / 'signing.jks'
        password = secrets.token_urlsafe(32)
        generate_build_key(key, password, environment)
        properties = private / 'keystore.properties'
        properties.write_text('\n'.join(name + '=' + shlex.quote(value) for name, value in {
            'KEYSTORE_FILE': str(key), 'KEYSTORE_PASSWORD': password, 'KEY_ALIAS': BUILD_ALIAS,
            'KEY_PASSWORD': password}.items()) + '\n', encoding='utf-8')
        properties.chmod(0o600)
        environment['KEYSTORE_PROPS'] = str(properties)
        # Paired, certificate-checked KernelSU LKMs are staged by the existing
        # eight-KMI job. Preserve them; build the remaining native dependencies.
        arguments = ['bash', 'build.sh', 'nomount', 'sui', 'ksuinit', 'ksud', 'apk', 'repack']
        with subprocess.Popen(arguments, cwd=source, env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace') as process:
            for line in process.stdout:
                print(redact(line.rstrip(), (password,)), flush=True)
            if process.wait():
                raise ValueError('FinaSU canonical native build/packaging failed')
    apks = list((output / 'native-fina').rglob('*.apk'))
    if len(apks) != 1 or apks[0].is_symlink():
        raise ValueError('Expected exactly one canonically repacked ARM64 FinaSU APK')
    apk = apks[0]
    with zipfile.ZipFile(apk) as archive:
        identity = manifest_identity(archive.read('AndroidManifest.xml'))
        names = archive.namelist()
        abis = {name.split('/')[1] for name in names if name.startswith('lib/') and len(name.split('/')) >= 3}
        if abis != {'arm64-v8a'} or archive.testzip() is not None:
            raise ValueError('FinaSU must contain only the verified ARM64 native build')
        daemon = archive.read('lib/arm64-v8a/libksud.so')
        if (len(daemon) < 64 or daemon[:7] != b'\x7fELF\x02\x01\x01'
                or struct.unpack_from('<H', daemon, 18)[0] != 183):
            raise ValueError('The canonically repacked FinaSU daemon is not ARM64')
        if any((kmi + '_kernelsu.ko').encode() not in daemon for kmi in KMIS):
            raise ValueError('FinaSU is missing paired native KMI modules')
    if identity['package'] != manager_profile('FinaSU')['package']:
        raise ValueError('FinaSU package differs from the official application ID')
    final_name = apk.name.removesuffix('-unsigned.apk') + '.apk' if apk.name.endswith('-unsigned.apk') else apk.name
    if not valid_manager_apk_filename(final_name, 'FinaSU'):
        raise ValueError('Unexpected canonical FinaSU release filename')
    metadata = {'source_apk_filename': apk.name, 'apk_filename': final_name,
                'manager_package': identity['package'], 'manager_version_name': identity['version_name'],
                'manager_version_code': identity['version_code']}
    shutil.copyfile(apk, output / 'manager-unsigned.apk')
    (output / 'manager-build-metadata.json').write_text(json.dumps(metadata, sort_keys=True) + '\n', encoding='utf-8')
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
            for name, value in metadata.items():
                stream.write(f'{name}={value}\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    build(args.source, args.output)
