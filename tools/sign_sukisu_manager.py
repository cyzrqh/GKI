#!/usr/bin/env python3
"""Sign a built ARM64 manager APK and export only public certificate metadata."""
from __future__ import annotations

import argparse
import base64
import binascii
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


def verify_certificate(report: str, certificate: bytes) -> str:
    if not certificate or len(certificate) > 64 * 1024:
        raise SigningError("keytool did not export a valid-sized DER certificate.")
    expected = hashlib.sha256(certificate).hexdigest()
    values = re.findall(r"^Signer #\d+ certificate SHA-256 digest:\s*([0-9a-fA-F:]+)\s*$", report, re.M)
    actual = {value.replace(":", "").lower() for value in values}
    if actual != {expected}:
        raise SigningError("The APK signer certificate does not match the exported keystore certificate.")
    return expected


def write_outputs(metadata: dict[str, str | int]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
            for key, value in metadata.items():
                stream.write(f"{key}={value}\n")


def sign_manager(unsigned: Path, output: Path) -> dict[str, str | int]:
    unsigned = Path(unsigned).resolve()
    output = Path(output).resolve()
    if unsigned == output:
        raise SigningError("The signed output must be a separate file from the unsigned input.")
    keystore_bytes, password, secrets = signing_material()
    tools = sdk_tools()
    validate_apk(unsigned)
    output.parent.mkdir(parents=True, exist_ok=True)

    # The private key stays outside the source/artifact tree and is deleted by
    # this context on both success and failure. Only a verified signed APK is
    # copied into the public output directory.
    temporary_root = os.environ.get("RUNNER_TEMP") or None
    with tempfile.TemporaryDirectory(prefix="gki-apk-signing-", dir=temporary_root) as private:
        directory = Path(private)
        keystore = directory / "manager.p12"
        certificate_path = directory / "certificate.der"
        aligned = directory / "aligned.apk"
        signed = directory / "signed.apk"
        descriptor = os.open(keystore, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(keystore_bytes)
        keystore.chmod(0o600)

        run_command([tools["zipalign"], "-P", "16", "-f", "4", str(unsigned), str(aligned)],
                    password, secrets)
        run_command([
            tools["apksigner"], "sign", "--ks", str(keystore), "--ks-type", "PKCS12",
            "--ks-key-alias", KEY_ALIAS, "--ks-pass", f"env:{PASSWORD_ENV}",
            "--key-pass", f"env:{PASSWORD_ENV}", "--out", str(signed), str(aligned),
        ], password, secrets, needs_password=True)
        run_command([
            tools["keytool"], "-exportcert", "-storetype", "PKCS12", "-keystore", str(keystore),
            "-alias", KEY_ALIAS, "-storepass:env", PASSWORD_ENV, "-file", str(certificate_path),
        ], password, secrets, needs_password=True)
        try:
            certificate = certificate_path.read_bytes()
        except OSError:
            raise SigningError("keytool did not export the signing certificate.") from None
        verification = run_command([tools["apksigner"], "verify", "--print-certs", str(signed)],
                                   password, secrets)
        certificate_sha256 = verify_certificate(verification, certificate)
        badging = run_command([tools["aapt2"], "dump", "badging", str(signed)], password, secrets)
        package = parse_package(badging)
        validate_apk(signed)
        metadata = {
            "manager_package": package,
            "manager_cert_size": len(certificate),
            "manager_cert_sha256": certificate_sha256,
            "apk_sha256": hashlib.sha256(signed.read_bytes()).hexdigest(),
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
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        metadata = sign_manager(args.unsigned, args.output)
    except (SigningError, OSError, UnicodeError) as error:
        values = (os.environ.get(KEYSTORE_ENV, ""), os.environ.get(PASSWORD_ENV, ""))
        print("Manager APK signing stopped: " + redact(str(error), values), file=sys.stderr)
        return 1
    print(json.dumps(metadata, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
