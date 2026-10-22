import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import sign_sukisu_manager as signing

KEY_BYTES = b"private PKCS12 test contents"
ENCODED_KEY = base64.b64encode(KEY_BYTES).decode("ascii")
PASSWORD = "persistent-password-that-must-not-be-logged"
CERTIFICATE = b"\x30\x82public DER certificate fixture"
CERTIFICATE_SHA = hashlib.sha256(CERTIFICATE).hexdigest()


class ManagerSigningTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "tests" / (".manager-signing-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.private_root = self.root / "private"
        self.private_root.mkdir()
        self.sdk = self.root / "sdk"
        tools = self.sdk / "build-tools" / signing.BUILD_TOOLS_VERSION
        tools.mkdir(parents=True)
        for name in ("zipalign", "apksigner", "aapt2"):
            (tools / name).write_text("", encoding="utf-8")
        self.unsigned = self.root / "build" / "manager-unsigned.apk"
        self.output = self.root / "artifacts" / "SukiSU.apk"
        self.workflow_output = self.root / "github-output.txt"
        self.make_apk(self.unsigned)
        self.calls = []
        self.private_paths = []
        self.mode_seen = None
        environment = {
            signing.KEYSTORE_ENV: ENCODED_KEY,
            signing.PASSWORD_ENV: PASSWORD,
            "ANDROID_HOME": str(self.sdk),
            "ANDROID_SDK_ROOT": "",
            "GITHUB_OUTPUT": str(self.workflow_output),
            "RUNNER_TEMP": str(self.private_root),
        }
        self.environ = patch.dict(os.environ, environment)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.which = patch.object(signing.shutil, "which", return_value="keytool")
        self.which.start()
        self.addCleanup(self.which.stop)
        if os.name == "nt":
            # Python's private-directory Windows ACL excludes the execution
            # sandbox identity. Keep the production secure directory code;
            # use an ordinary disposable fixture for mocked SDK commands here.
            self.temporary = patch.object(signing.tempfile, "TemporaryDirectory",
                                          side_effect=self.windows_temporary_directory)
            self.temporary.start()
            self.addCleanup(self.temporary.stop)

    @contextlib.contextmanager
    def windows_temporary_directory(self, *, prefix, dir):
        path = Path(dir) / (prefix + uuid.uuid4().hex)
        path.mkdir()
        try:
            yield str(path)
        finally:
            shutil.rmtree(path)

    @staticmethod
    def make_apk(path, *, include_daemon=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"manifest")
            archive.writestr("classes.dex", b"dex")
            if include_daemon:
                archive.writestr("lib/arm64-v8a/libksud.so", b"nonempty daemon")

    def fake_sdk(self, args, **kwargs):
        self.calls.append((args, kwargs))
        command = Path(args[0]).name
        stdout = ""
        if command == "zipalign":
            shutil.copyfile(args[-2], args[-1])
        elif command == "apksigner" and args[1] == "sign":
            keystore = Path(args[args.index("--ks") + 1])
            self.private_paths.append(keystore)
            self.assertEqual(keystore.read_bytes(), KEY_BYTES)
            self.mode_seen = stat.S_IMODE(keystore.stat().st_mode)
            shutil.copyfile(args[-1], args[args.index("--out") + 1])
        elif command == "keytool":
            Path(args[args.index("-file") + 1]).write_bytes(CERTIFICATE)
        elif command == "apksigner" and args[1] == "verify":
            stdout = (f"Signer #1 certificate SHA-256 digest: {CERTIFICATE_SHA}\n"
                      "-----BEGIN CERTIFICATE-----\n" + base64.b64encode(CERTIFICATE).decode() +
                      "\n-----END CERTIFICATE-----\n")
        elif command == "aapt2":
            stdout = "package: name='com.sukisu.ultra' versionCode='41030' versionName='v4.2.1'\n"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    def run_signing(self):
        return signing.sign_manager(self.unsigned, self.output)

    def test_orders_alignment_signing_export_and_verification_with_public_outputs(self):
        with patch.object(signing.subprocess, "run", side_effect=self.fake_sdk):
            metadata = self.run_signing()
        self.assertEqual([Path(args[0]).name + (":" + args[1] if Path(args[0]).name == "apksigner" else "")
                          for args, _ in self.calls],
                         ["zipalign", "apksigner:sign", "keytool", "apksigner:verify", "aapt2"])
        self.assertEqual(self.calls[0][0][1:5], ["-P", "16", "-f", "4"])
        sign_args = self.calls[1][0]
        self.assertEqual(sign_args[sign_args.index("--ks-type") + 1], "PKCS12")
        self.assertEqual(sign_args[sign_args.index("--ks-key-alias") + 1], "gki-sukisu")
        self.assertIn(f"env:{signing.PASSWORD_ENV}", sign_args)
        self.assertIn("-storepass:env", self.calls[2][0])
        self.assertEqual(metadata["manager_package"], "com.sukisu.ultra")
        self.assertEqual(metadata["manager_cert_size"], len(CERTIFICATE))
        self.assertEqual(metadata["manager_cert_sha256"], CERTIFICATE_SHA)
        self.assertEqual(metadata["apk_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        outputs = self.workflow_output.read_text(encoding="utf-8")
        self.assertIn(f"manager_cert_size={len(CERTIFICATE)}\n", outputs)
        self.assertIn(f"manager_cert_sha256={CERTIFICATE_SHA}\n", outputs)
        for value in (PASSWORD, ENCODED_KEY, str(KEY_BYTES)):
            self.assertNotIn(value, outputs)
        self.assertTrue(self.output.is_file())
        self.assertFalse(list(self.private_root.iterdir()))
        self.assertTrue(all(not path.exists() for path in self.private_paths))
        if os.name != "nt":
            self.assertEqual(self.mode_seen, 0o600)

    def test_password_is_not_in_arguments_and_private_key_never_enters_subprocess_env(self):
        with patch.object(signing.subprocess, "run", side_effect=self.fake_sdk):
            self.run_signing()
        for args, kwargs in self.calls:
            self.assertIsInstance(args, list)
            self.assertNotIn("shell", kwargs)
            self.assertNotIn(PASSWORD, " ".join(args))
            self.assertNotIn(ENCODED_KEY, " ".join(args))
            self.assertNotIn(signing.KEYSTORE_ENV, kwargs["env"])
            password_needed = Path(args[0]).name == "keytool" or args[1] == "sign"
            self.assertEqual(signing.PASSWORD_ENV in kwargs["env"], password_needed)

    def test_missing_secrets_or_invalid_base64_does_not_run_sdk(self):
        for change in ({signing.KEYSTORE_ENV: ""}, {signing.PASSWORD_ENV: ""},
                       {signing.KEYSTORE_ENV: "not-base64!"}):
            with self.subTest(change=change), patch.dict(os.environ, change):
                with patch.object(signing.subprocess, "run") as run:
                    with self.assertRaises(signing.SigningError):
                        self.run_signing()
                    run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_or_empty_daemon_is_rejected_before_signing(self):
        for daemon in (None, b""):
            self.make_apk(self.unsigned, include_daemon=False)
            if daemon is not None:
                with zipfile.ZipFile(self.unsigned, "a") as archive:
                    archive.writestr("lib/arm64-v8a/libksud.so", daemon)
            with self.subTest(daemon=daemon), patch.object(signing.subprocess, "run") as run:
                with self.assertRaisesRegex(signing.SigningError, "lib/arm64-v8a/libksud"):
                    self.run_signing()
                run.assert_not_called()
        self.assertFalse(self.workflow_output.exists())

    def test_certificate_mismatch_leaves_previous_output_untouched_and_cleans_key(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b"previous verified APK")

        def mismatch(args, **kwargs):
            result = self.fake_sdk(args, **kwargs)
            if Path(args[0]).name == "apksigner" and args[1] == "verify":
                result.stdout = "Signer #1 certificate SHA-256 digest: " + "0" * 64 + "\n"
            return result

        with patch.object(signing.subprocess, "run", side_effect=mismatch):
            with self.assertRaisesRegex(signing.SigningError, "does not match"):
                self.run_signing()
        self.assertEqual(self.output.read_bytes(), b"previous verified APK")
        self.assertFalse(self.workflow_output.exists())
        self.assertFalse(list(self.private_root.iterdir()))

    def test_pem_verification_does_not_depend_on_human_signer_label(self):
        pem = "-----BEGIN CERTIFICATE-----\n" + base64.b64encode(CERTIFICATE).decode() + "\n-----END CERTIFICATE-----\n"
        for label in ('Signer #1', 'Signer (minSdkVersion=33, maxSdkVersion=2147483647)'):
            with self.subTest(label=label):
                report = label + ' certificate DN: CN=manager\n' + pem
                self.assertEqual(signing.verify_certificate(report, CERTIFICATE), CERTIFICATE_SHA)
                self.assertEqual(signing.verify_certificate(report.replace('\n', '\r\n'), CERTIFICATE), CERTIFICATE_SHA)

    def test_additional_different_pem_signer_is_rejected(self):
        def pem(value):
            return '-----BEGIN CERTIFICATE-----\n' + base64.b64encode(value).decode() + '\n-----END CERTIFICATE-----\n'
        with self.assertRaisesRegex(signing.SigningError, 'does not match'):
            signing.verify_certificate(pem(CERTIFICATE) + pem(b'another signer'), CERTIFICATE)

    def test_sdk_error_redacts_both_password_and_base64_from_cli_logs(self):
        def failure(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, stdout=PASSWORD, stderr=ENCODED_KEY)
        stderr, stdout = io.StringIO(), io.StringIO()
        with patch.object(signing.subprocess, "run", side_effect=failure):
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
                code = signing.main(["--unsigned", str(self.unsigned), "--output", str(self.output)])
        self.assertEqual(code, 1)
        self.assertNotIn(PASSWORD, stderr.getvalue() + stdout.getvalue())
        self.assertNotIn(ENCODED_KEY, stderr.getvalue() + stdout.getvalue())
        self.assertIn("[SIGNING_SECRET_REDACTED]", stderr.getvalue())
        self.assertFalse(self.output.exists())
        self.assertFalse(self.workflow_output.exists())
        self.assertFalse(list(self.private_root.iterdir()))

    def test_bad_package_name_cannot_inject_workflow_outputs(self):
        def bad_package(args, **kwargs):
            result = self.fake_sdk(args, **kwargs)
            if Path(args[0]).name == "aapt2":
                result.stdout = "package: name='com.sukisu.ultra\nmanager_cert_size=1' versionCode='1'\n"
            return result
        with patch.object(signing.subprocess, "run", side_effect=bad_package):
            with self.assertRaisesRegex(signing.SigningError, "ASCII application ID"):
                self.run_signing()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.workflow_output.exists())
        self.assertFalse(list(self.private_root.iterdir()))

    def test_signed_apk_is_checked_again_after_sdk_commands(self):
        def corrupt(args, **kwargs):
            result = self.fake_sdk(args, **kwargs)
            if Path(args[0]).name == "aapt2":
                self.make_apk(Path(args[-1]), include_daemon=False)
            return result
        with patch.object(signing.subprocess, "run", side_effect=corrupt):
            with self.assertRaisesRegex(signing.SigningError, "lib/arm64-v8a/libksud"):
                self.run_signing()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.private_root.iterdir()))

    def test_missing_sdk_tool_or_java_fails_before_creating_key(self):
        (self.sdk / "build-tools" / signing.BUILD_TOOLS_VERSION / "aapt2").unlink()
        with patch.object(signing.subprocess, "run") as run:
            with self.assertRaisesRegex(signing.SigningError, "missing aapt2"):
                self.run_signing()
            run.assert_not_called()
        self.assertFalse(list(self.private_root.iterdir()))

    def test_same_input_output_is_rejected_without_touching_unsigned_apk(self):
        before = self.unsigned.read_bytes()
        with self.assertRaisesRegex(signing.SigningError, "separate file"):
            signing.sign_manager(self.unsigned, self.unsigned)
        self.assertEqual(self.unsigned.read_bytes(), before)

    def test_successful_cli_prints_only_public_metadata(self):
        stdout = io.StringIO()
        with patch.object(signing.subprocess, "run", side_effect=self.fake_sdk):
            with contextlib.redirect_stdout(stdout):
                code = signing.main(["--unsigned", str(self.unsigned), "--output", str(self.output)])
        self.assertEqual(code, 0)
        metadata = json.loads(stdout.getvalue())
        self.assertEqual(set(metadata), {"manager_package", "manager_cert_size", "manager_cert_sha256", "apk_sha256"})
        self.assertNotIn(PASSWORD, stdout.getvalue())
        self.assertNotIn(ENCODED_KEY, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
