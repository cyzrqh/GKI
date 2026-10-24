#!/usr/bin/env python3
"""Sign an ARM64 manager APK containing its authenticated public build identity."""
from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from embedded_build_metadata import APK_MEMBER, read_member, validate_members, verify_sukisu_apk


KEYSTORE_ENV = "SUKISU_APK_KEYSTORE"
PASSWORD_ENV = "SUKISU_APK_PASSWORD"
KEY_ALIAS = "gki-sukisu"
BUILD_TOOLS_VERSION = "37.0.0"
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")


class SigningError(RuntimeError):
    """Stop without publishing an unverified or unsigned APK."""


def redact(text: str, secrets: tuple[str, ...]) -> str:
    for value in sorted({value for value in secrets if value}, key=len, reverse=True):
        text = text.replace(value, "[SIGNING_SECRET_REDACTED]")
    return text


def signing_material() -> tuple[bytes, str, tuple[str, ...]]:
    encoded = os.environ.get(KEYSTORE_ENV, "")
    password = os.environ.get(PASSWORD_ENV, "")
    if not encoded or not password:
        raise SigningError(f"Configure both {KEYSTORE_ENV} and {PASSWORD_ENV} repository secrets.")
    if "\0" in password:
        raise SigningError("The signing password cannot contain a NUL character.")
    compact = "".join(encoded.split())
    try:
        keystore = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise SigningError("The signing keystore secret is not valid base64.") from None
    if not keystore or len(keystore) > 256 * 1024:
        raise SigningError("The signing keystore secret has an invalid size.")
    return keystore, password, (encoded, compact, password)


def sdk_tools() -> dict[str, str]:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk:
        raise SigningError("ANDROID_HOME must point to the prepared Android SDK.")
    directory = Path(sdk) / "build-tools" / BUILD_TOOLS_VERSION
    result = {}
    for name in ("zipalign", "apksigner", "aapt2"):
        choices = (directory / name, directory / f"{name}.exe", directory / f"{name}.bat")
        found = next((path for path in choices if path.is_file()), None)
        if found is None:
            raise SigningError(f"Android build-tools {BUILD_TOOLS_VERSION} is missing {name}.")
        result[name] = str(found)
    keytool = shutil.which("keytool")
    if keytool is None:
        raise SigningError("Java keytool was not found; prepare Java 21 before signing.")
    result["keytool"] = keytool
    return result


def run_command(arguments: list[str], password: str, secrets: tuple[str, ...],
                *, needs_password: bool = False) -> str:
    # Source compilation never needs these secrets. Only the signing commands
    # receive the password, and no subprocess receives the base64 private key.
    environment = {key: value for key, value in os.environ.items()
                   if key not in (KEYSTORE_ENV, PASSWORD_ENV)}
    if needs_password:
        environment[PASSWORD_ENV] = password
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, env=environment,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise SigningError(f"{Path(arguments[0]).name} could not complete.") from None
    if result.returncode:
        details = redact((result.stdout or "") + "\n" + (result.stderr or ""), secrets).strip()[-2000:]
        message = f"{Path(arguments[0]).name} failed (exit {result.returncode})."
        if details:
            message += "\n" + details
        raise SigningError(message)
    return result.stdout


def validate_apk(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            required = ("AndroidManifest.xml", "classes.dex", "lib/arm64-v8a/libksud.so")
            for name in required:
                if name not in archive.namelist() or archive.getinfo(name).file_size == 0:
                    raise SigningError(f"The manager APK is missing a nonempty {name}.")
            if archive.testzip() is not None:
                raise SigningError("The manager APK ZIP integrity check failed.")
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, SigningError):
            raise
        raise SigningError("The manager APK is not a readable, valid ZIP archive.") from None


def parse_package(badging: str) -> str:
    matches = re.findall(r"^package:\s+name='([^']+)'(?:\s|$)", badging, re.M)
    if len(matches) != 1 or not PACKAGE.fullmatch(matches[0]):
        raise SigningError("aapt2 did not return one valid ASCII application ID.")
    return matches[0]


def parse_apk_metadata(badging: str) -> dict[str, str | int]:
    package = parse_package(badging)
    rows = re.findall(r"^package:[^\r\n]*", badging, re.M)
    if len(rows) != 1:
        raise SigningError("aapt2 did not return one package metadata row.")
    fields = {}
    for field in ("versionName", "versionCode"):
        values = re.findall(r"(?:^|\s)" + field + r"='([^'\r\n]*)'(?=\s|$)", rows[0])
        if len(values) != 1:
            raise SigningError(f"aapt2 did not return one valid {field}.")
        fields[field] = values[0]
    version = fields["versionName"]
    if not version or len(version) > 128 or any(ord(char) < 32 or ord(char) > 126 for char in version):
        raise SigningError("aapt2 did not return a nonempty printable ASCII versionName.")
    code = fields["versionCode"]
    if not re.fullmatch(r"[1-9][0-9]{0,9}", code) or int(code) > 2147483647:
        raise SigningError("aapt2 did not return a positive Android versionCode.")
    return {"manager_package": package, "manager_version_name": version,
            "manager_version_code": int(code)}


def apk_filename(build_metadata: Path, actual: dict[str, str | int]) -> str:
    """Keep the builder's native filename after proving its APK identity."""
    path = Path(build_metadata)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 16 * 1024:
        raise SigningError("The manager build metadata file is missing or invalid.")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise SigningError("The manager build metadata file is not valid JSON.") from None
    if (not isinstance(metadata, dict)
            or type(metadata.get("manager_version_code")) is not int
            or any(metadata.get(field) != value for field, value in actual.items())):
        raise SigningError("The manager build metadata package/version does not match the source APK.")
    source, output = metadata.get("source_apk_filename"), metadata.get("apk_filename")
    # Filenames come from Gradle rather than being recreated from versionName:
    # official development builds can use manager-ci or a version without v.
    for name in (source, output):
        if (not isinstance(name, str) or len(name) > 250 or ".." in name
                or not re.fullmatch(r"SukiSU_[A-Za-z0-9][A-Za-z0-9._+-]*-release(?:-unsigned)?\.apk", name)):
            raise SigningError("The manager build metadata must contain safe native SukiSU APK basenames.")
    if (not source.endswith("-release-unsigned.apk") or not output.endswith("-release.apk")
            or output != source.removesuffix("-unsigned.apk") + ".apk"):
        raise SigningError("The signed APK filename must preserve the native source name, removing only -unsigned.")
    return output


def verify_certificate(report: str, certificate: bytes) -> str:
    if not certificate or len(certificate) > 64 * 1024:
        raise SigningError("keytool did not export a valid-sized DER certificate.")
    expected = hashlib.sha256(certificate).hexdigest()
    # apksigner varies the human-readable signer label by signature scheme.
    # Compare the certificates themselves, using its supported PEM output.
    values = re.findall(r"^-----BEGIN CERTIFICATE-----\r?\n([A-Za-z0-9+/=\r\n]+)"
                        r"\r?\n-----END CERTIFICATE-----\r?$", report, re.M)
    try:
        actual = {hashlib.sha256(base64.b64decode(''.join(value.split()), validate=True)).hexdigest()
                  for value in values}
    except (binascii.Error, ValueError):
        raise SigningError("apksigner returned an invalid PEM signer certificate.") from None
    if actual != {expected}:
        raise SigningError("The APK signer certificate does not match the exported keystore certificate.")
    return expected


def write_outputs(metadata: dict[str, str | int]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
            for key, value in metadata.items():
                stream.write(f"{key}={value}\n")


def public_build_identity(actual: dict, certificate: bytes, filename: str) -> dict:
    """Only trusted workflow/source values and measured public APK identity."""
    ref = os.environ.get('KSU_REF', '')
    branch = os.environ.get('KSU_BRANCH', '')
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    head = os.environ.get('GITHUB_SHA', '')
    if (not re.fullmatch(r'[0-9a-f]{40}', ref) or not re.fullmatch(r'[0-9a-f]{40}', head)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', repository)
            or any(not re.fullmatch(r'[1-9][0-9]*', value) for value in (run_id, attempt))
            or not branch or len(branch) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in branch)):
        raise SigningError('Missing or invalid immutable SukiSU/workflow provenance for the signed APK.')
    return {'schema_version': 1, 'file': filename, 'package': actual['manager_package'],
            'ksu_variant': 'SukiSU', 'ksu_ref': ref, 'ksu_branch': branch,
            'ksu_version_code': actual['manager_version_code'], 'ksu_version_name': actual['manager_version_name'],
            'manager_cert_size': len(certificate), 'manager_cert_sha256': hashlib.sha256(certificate).hexdigest(),
            'producer_repository': repository, 'producer_run_id': run_id,
            'producer_run_attempt': attempt, 'producer_head_sha': head,
            'built_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}


def embed_identity(unsigned: Path, destination: Path, identity: dict) -> None:
    # Rebuild before zipalign/sign so the metadata is covered by the real APK
    # signature. Existing APK content and file attributes are preserved.
    with zipfile.ZipFile(unsigned) as source, zipfile.ZipFile(destination, 'w') as target:
        validate_members(source)
        target.comment = source.comment
        for member in source.infolist():
            if member.filename != APK_MEMBER:
                target.writestr(member, source.read(member))
        entry = zipfile.ZipInfo(APK_MEMBER)
        entry.create_system = 3
        entry.external_attr = (0o100644 << 16)
        entry.compress_type = zipfile.ZIP_DEFLATED
        target.writestr(entry, (json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                          allow_nan=False) + '\n').encode('utf-8'))


def sign_manager(unsigned: Path, output: Path | None = None, *, output_dir: Path | None = None,
                 build_metadata: Path | None = None) -> dict[str, str | int]:
    unsigned = Path(unsigned).resolve()
    if (output is None) == (output_dir is None):
        raise SigningError("Choose exactly one signed output file or output directory.")
    if output_dir is not None and build_metadata is None:
        raise SigningError("--output-dir requires --build-metadata to preserve the native APK filename.")
    output = Path(output).resolve() if output is not None else None
    if unsigned == output:
        raise SigningError("The signed output must be a separate file from the unsigned input.")
    keystore_bytes, password, secrets = signing_material()
    tools = sdk_tools()
    validate_apk(unsigned)
    source_badging = run_command([tools["aapt2"], "dump", "badging", str(unsigned)], password, secrets)
    source_metadata = parse_apk_metadata(source_badging)
    native_filename = apk_filename(build_metadata, source_metadata) if build_metadata is not None else None
    if output is None:
        output = Path(output_dir).resolve() / native_filename
        if unsigned == output:
            raise SigningError("The signed output must be a separate file from the unsigned input.")
    if any(ord(char) < 32 or ord(char) > 126 for char in output.name):
        raise SigningError("The signed APK filename must contain only printable ASCII characters.")
    output.parent.mkdir(parents=True, exist_ok=True)

    # The private key stays outside the source/artifact tree and is deleted by
    # this context on both success and failure. Only a verified signed APK is
    # copied into the public output directory.
    temporary_root = os.environ.get("RUNNER_TEMP") or None
    with tempfile.TemporaryDirectory(prefix="gki-apk-signing-", dir=temporary_root) as private:
        directory = Path(private)
        keystore = directory / "manager.p12"
        certificate_path = directory / "certificate.der"
        embedded = directory / "embedded-unsigned.apk"
        aligned = directory / "aligned.apk"
        signed = directory / "signed.apk"
        descriptor = os.open(keystore, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(keystore_bytes)
        keystore.chmod(0o600)

        run_command([
            tools["keytool"], "-exportcert", "-storetype", "PKCS12", "-keystore", str(keystore),
            "-alias", KEY_ALIAS, "-storepass:env", PASSWORD_ENV, "-file", str(certificate_path),
        ], password, secrets, needs_password=True)
        try:
            certificate = certificate_path.read_bytes()
        except OSError:
            raise SigningError("keytool did not export the signing certificate.") from None
        if not certificate or len(certificate) > 4096:
            raise SigningError('keytool did not export a valid-sized public manager certificate.')
        embedded_identity = public_build_identity(source_metadata, certificate, output.name)
        embed_identity(unsigned, embedded, embedded_identity)
        run_command([tools["zipalign"], "-P", "16", "-f", "4", str(embedded), str(aligned)],
                    password, secrets)
        run_command([
            tools["apksigner"], "sign", "--ks", str(keystore), "--ks-type", "PKCS12",
            "--ks-key-alias", KEY_ALIAS, "--ks-pass", f"env:{PASSWORD_ENV}",
            "--v1-signing-enabled", "false", "--v2-signing-enabled", "true",
            "--v3-signing-enabled", "false", "--v4-signing-enabled", "false",
            "--key-pass", f"env:{PASSWORD_ENV}", "--out", str(signed), str(aligned),
        ], password, secrets, needs_password=True)
        verification = run_command([tools["apksigner"], "verify", "--print-certs-pem", str(signed)],
                                   password, secrets)
        certificate_sha256 = verify_certificate(verification, certificate)
        badging = run_command([tools["aapt2"], "dump", "badging", str(signed)], password, secrets)
        signed_metadata = parse_apk_metadata(badging)
        if signed_metadata != source_metadata:
            raise SigningError("The signed APK package/version metadata does not match the source APK.")
        validate_apk(signed)
        # Android accepts APK-v3, but SukiSU's native manager authenticator
        # rejects its extra signing block even when the v2 certificate matches.
        # Check the final bytes against that policy as well as Android's SDK.
        try:
            native_certificate = verify_sukisu_apk(signed.read_bytes())
        except ValueError:
            raise SigningError('The signed APK is incompatible with SukiSU kernel manager authentication.') from None
        if native_certificate != certificate:
            raise SigningError('The native APK certificate differs from the fixed signing certificate.')
        with zipfile.ZipFile(signed) as archive:
            if read_member(archive, APK_MEMBER) != embedded_identity:
                raise SigningError('The signed APK embedded build identity changed during signing.')
        metadata = {
            **signed_metadata,
            "manager_cert_size": len(certificate),
            "manager_cert_sha256": certificate_sha256,
            "apk_sha256": hashlib.sha256(signed.read_bytes()).hexdigest(),
            "apk_filename": output.name,
        }

        # Publish atomically. Any signing/verification error leaves an existing
        # public output untouched and does not set affirmative workflow outputs.
        staged = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".gki-signed-", suffix=".tmp", dir=output.parent)
            staged = Path(name)
            os.close(descriptor)
            shutil.copyfile(signed, staged)
            os.replace(staged, output)
            staged = None
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
    write_outputs(metadata)
    return metadata


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unsigned", required=True, type=Path)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path, help="Explicit signed APK path (compatibility mode)")
    destination.add_argument("--output-dir", type=Path, help="Directory for the APK retaining its original Gradle filename")
    parser.add_argument("--build-metadata", type=Path, help="Verified manager-build-metadata.json written by the build step")
    args = parser.parse_args(argv)
    try:
        metadata = sign_manager(args.unsigned, args.output, output_dir=args.output_dir,
                                build_metadata=args.build_metadata)
    except (SigningError, OSError, UnicodeError, ValueError) as error:
        values = (os.environ.get(KEYSTORE_ENV, ""), os.environ.get(PASSWORD_ENV, ""))
        print("Manager APK signing stopped: " + redact(str(error), values), file=sys.stderr)
        return 1
    print(json.dumps(metadata, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
