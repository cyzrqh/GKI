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
            "KSU_REF": "b" * 40, "KSU_BRANCH": "Dev(开发)",
            "GITHUB_REPOSITORY": "cyzrqh/gki", "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
        }
        self.environ = patch.dict(os.environ, environment)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.which = patch.object(signing.shutil, "which", return_value="keytool")
        self.which.start()
        self.addCleanup(self.which.stop)
        # These SDK command fixtures copy unsigned ZIP bytes. Genuine RSA and
        # kernel-layout checks have separate binary-fixture regression tests.
        self.authenticator = patch.object(signing, 'verify_sukisu_apk', return_value=CERTIFICATE)
        self.kernel_auth = self.authenticator.start()
        self.addCleanup(self.authenticator.stop)
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

    def build_metadata(self, filename='SukiSU_v4.2.1_41030-release.apk', version='v4.2.1', **changes):
        metadata = {'source_apk_filename': filename.removesuffix('.apk') + '-unsigned.apk',
                    'apk_filename': filename, 'manager_package': 'com.sukisu.ultra',
                    'manager_version_name': version, 'manager_version_code': 41030}
        metadata.update(changes)
        path = self.unsigned.parent / 'manager-build-metadata.json'
        path.write_text(json.dumps(metadata), encoding='utf-8')
        return path

    def test_orders_alignment_signing_export_and_verification_with_public_outputs(self):
        with patch.object(signing.subprocess, "run", side_effect=self.fake_sdk):
            metadata = self.run_signing()
        self.assertEqual([Path(args[0]).name + (":" + args[1] if Path(args[0]).name == "apksigner" else "")
                          for args, _ in self.calls],
                         ["aapt2", "keytool", "zipalign", "apksigner:sign", "apksigner:verify", "aapt2"])
        self.assertEqual(self.calls[0][0][-1], str(self.unsigned.resolve()))
        self.assertEqual(self.calls[2][0][1:5], ["-P", "16", "-f", "4"])
        sign_args = self.calls[3][0]
        self.assertEqual(sign_args[sign_args.index("--ks-type") + 1], "PKCS12")
        self.assertEqual(sign_args[sign_args.index("--ks-key-alias") + 1], "gki-sukisu")
        for scheme, enabled in ((1, 'false'), (2, 'true'), (3, 'false'), (4, 'false')):
            self.assertEqual(sign_args[sign_args.index(f'--v{scheme}-signing-enabled') + 1], enabled)
        self.assertIn(f"env:{signing.PASSWORD_ENV}", sign_args)
        self.assertIn("-storepass:env", self.calls[1][0])
        self.assertEqual(metadata["manager_package"], "com.sukisu.ultra")
        self.assertEqual(metadata["manager_version_name"], "v4.2.1")
        self.assertEqual(metadata["manager_version_code"], 41030)
        self.assertEqual(metadata["apk_filename"], "SukiSU.apk")
        self.assertEqual(metadata["manager_cert_size"], len(CERTIFICATE))
        self.assertEqual(metadata["manager_cert_sha256"], CERTIFICATE_SHA)
        self.assertEqual(metadata["apk_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        outputs = self.workflow_output.read_text(encoding="utf-8")
        self.assertIn(f"manager_cert_size={len(CERTIFICATE)}\n", outputs)
        self.assertIn(f"manager_cert_sha256={CERTIFICATE_SHA}\n", outputs)
        for value in (PASSWORD, ENCODED_KEY, str(KEY_BYTES)):
            self.assertNotIn(value, outputs)
        self.assertTrue(self.output.is_file())
        self.kernel_auth.assert_called_once_with(self.output.read_bytes())
        self.assertFalse(list(self.private_root.iterdir()))
        self.assertTrue(all(not path.exists() for path in self.private_paths))
        if os.name != "nt":
            self.assertEqual(self.mode_seen, 0o600)

    def test_public_identity_is_inside_apk_before_alignment_and_signing(self):
        unsigned = self.unsigned.read_bytes()
        observed = []
        def inspect(args, **kwargs):
            if Path(args[0]).name == 'zipalign':
                with zipfile.ZipFile(args[-2]) as archive:
                    identity = json.loads(archive.read(signing.APK_MEMBER))
                    observed.append(identity)
                    self.assertEqual(identity['ksu_ref'], 'b' * 40)
                    self.assertEqual(identity['producer_head_sha'], 'a' * 40)
                    self.assertEqual(identity['manager_cert_sha256'], CERTIFICATE_SHA)
                    self.assertEqual(identity['ksu_version_code'], 41030)
                    self.assertNotIn('apk_sha256', identity)
                    self.assertNotIn(PASSWORD, json.dumps(identity))
                    self.assertNotIn(ENCODED_KEY, json.dumps(identity))
            return self.fake_sdk(args, **kwargs)
        with patch.object(signing.subprocess, 'run', side_effect=inspect):
            self.run_signing()
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.unsigned.read_bytes(), unsigned)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(json.loads(archive.read(signing.APK_MEMBER)), observed[0])

    def test_android_valid_but_kernel_rejected_signature_never_replaces_output(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b'previous verified APK')
        self.kernel_auth.side_effect = ValueError('APK-v3 is rejected by native kernel verifier')
        with patch.object(signing.subprocess, 'run', side_effect=self.fake_sdk):
            with self.assertRaisesRegex(signing.SigningError, 'kernel manager authentication'):
                self.run_signing()
        self.assertEqual(self.output.read_bytes(), b'previous verified APK')
        self.assertFalse(self.workflow_output.exists())

    def test_missing_immutable_source_provenance_never_publishes_an_apk(self):
        for field, value in (('KSU_REF', ''), ('KSU_REF', 'main'), ('KSU_BRANCH', ''), ('GITHUB_SHA', 'bad')):
            with self.subTest(field=field, value=value), patch.dict(os.environ, {field: value}):
                with patch.object(signing.subprocess, 'run', side_effect=self.fake_sdk):
                    with self.assertRaisesRegex(signing.SigningError, 'immutable SukiSU/workflow provenance'):
                        self.run_signing()
            self.assertFalse(self.output.exists())
            self.assertFalse(self.workflow_output.exists())

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
            if Path(args[0]).name == "aapt2" and Path(args[-1]) != self.unsigned.resolve():
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
        self.assertEqual(set(metadata), {"manager_package", "manager_cert_size", "manager_cert_sha256", "apk_sha256",
                                         "manager_version_name", "manager_version_code", "apk_filename"})
        self.assertNotIn(PASSWORD, stdout.getvalue())
        self.assertNotIn(ENCODED_KEY, stdout.getvalue())

    def test_output_directory_keeps_native_filename_and_real_versions(self):
        directory = self.root / 'versioned-artifacts'
        build_metadata = self.build_metadata()
        with patch.object(signing.subprocess, 'run', side_effect=self.fake_sdk):
            metadata = signing.sign_manager(self.unsigned, output_dir=directory, build_metadata=build_metadata)
        filename = 'SukiSU_v4.2.1_41030-release.apk'
        self.assertEqual(metadata['apk_filename'], filename)
        self.assertTrue((directory / filename).is_file())
        self.assertEqual(list(directory.iterdir()), [directory / filename])
        outputs = self.workflow_output.read_text(encoding='utf-8')
        self.assertIn('manager_version_name=v4.2.1\n', outputs)
        self.assertIn('manager_version_code=41030\n', outputs)
        self.assertIn('apk_filename=' + filename + '\n', outputs)

    def test_native_development_and_release_names_preserve_prefix_exactly(self):
        directory = self.root / 'versioned-artifacts'
        for version in ('manager-ci', 'v4.2.1', '4.2.1'):
            filename = 'SukiSU_' + version + '_41030-release.apk'
            path = self.build_metadata(filename, version)
            def native_version(args, **kwargs):
                result = self.fake_sdk(args, **kwargs)
                if Path(args[0]).name == 'aapt2':
                    result.stdout = result.stdout.replace("versionName='v4.2.1'", "versionName='" + version + "'")
                return result
            with self.subTest(version=version), patch.object(signing.subprocess, 'run', side_effect=native_version):
                metadata = signing.sign_manager(self.unsigned, output_dir=directory, build_metadata=path)
            self.assertEqual(metadata['manager_version_name'], version)
            self.assertEqual(metadata['apk_filename'], filename)
            self.assertTrue((directory / filename).is_file())

    def test_native_filename_cannot_escape_directory_or_be_silently_renamed(self):
        directory = self.root / 'versioned-artifacts'
        names = ('../SukiSU_v4.2.1_41030-release.apk', '/tmp/SukiSU_v4.2.1_41030-release.apk',
                 'C:\\SukiSU_v4.2.1_41030-release.apk', 'SukiSU_../bad-release.apk',
                 'SukiSU_bad\nname_41030-release.apk', 'Other_v4.2.1_41030-release.apk',
                 'SukiSU_v4.2.1_41030-release.apk:stream', 'SukiSU_' + 'a' * 250 + '-release.apk')
        for filename in names:
            path = self.build_metadata(filename)
            with self.subTest(filename=filename), patch.object(signing.subprocess, 'run', side_effect=self.fake_sdk):
                with self.assertRaisesRegex(signing.SigningError, 'safe native SukiSU APK basenames'):
                    signing.sign_manager(self.unsigned, output_dir=directory, build_metadata=path)
            self.assertFalse(self.private_paths)
            self.assertFalse(directory.exists())
        path = self.build_metadata(apk_filename='SukiSU_changed_41030-release.apk')
        with patch.object(signing.subprocess, 'run', side_effect=self.fake_sdk):
            with self.assertRaisesRegex(signing.SigningError, 'removing only -unsigned'):
                signing.sign_manager(self.unsigned, output_dir=directory, build_metadata=path)
        self.assertFalse(self.private_paths)

    def test_build_metadata_identity_must_match_actual_unsigned_apk(self):
        for changes in ({'manager_package': 'com.other.manager'}, {'manager_version_code': 99999},
                        {'manager_version_code': '41030'}, {'manager_version_code': True},
                        {'manager_version_name': 'different-version'}):
            path = self.build_metadata(**changes)
            with self.subTest(changes=changes), patch.object(signing.subprocess, 'run', side_effect=self.fake_sdk):
                with self.assertRaisesRegex(signing.SigningError, 'package/version does not match'):
                    signing.sign_manager(self.unsigned, output_dir=self.output.parent, build_metadata=path)
            self.assertFalse(self.private_paths)
            self.assertFalse(self.workflow_output.exists())

    def test_missing_invalid_or_incomplete_build_metadata_stops_before_signing(self):
        with patch.object(signing.subprocess, 'run') as run:
            with self.assertRaisesRegex(signing.SigningError, 'requires --build-metadata'):
                signing.sign_manager(self.unsigned, output_dir=self.output.parent)
            run.assert_not_called()
        path = self.unsigned.parent / 'missing-metadata.json'
        for content in (None, 'not-json', '[]', '{}', '{"manager_version_code":41030}'):
            if content is not None:
                path.write_text(content, encoding='utf-8')
            with self.subTest(content=content), patch.object(signing.subprocess, 'run', side_effect=self.fake_sdk):
                with self.assertRaises(signing.SigningError):
                    signing.sign_manager(self.unsigned, output_dir=self.output.parent, build_metadata=path)
            self.assertFalse(self.private_paths)
            self.assertFalse(self.workflow_output.exists())

    def test_invalid_version_metadata_fails_before_creating_signing_key(self):
        path = self.build_metadata()
        for fragment in ["versionCode='0'", "versionCode='2147483648'", "versionCode='01'",
                         "versionCode='not-a-number'", "versionName=''", "versionName='bad\nversion'",
                         "versionName='v4.2.1' versionName='duplicate'"]:
            def invalid(args, **kwargs):
                result = self.fake_sdk(args, **kwargs)
                if Path(args[0]).name == 'aapt2':
                    field = 'versionCode' if fragment.startswith('versionCode') else 'versionName'
                    result.stdout = result.stdout.replace(
                        "versionCode='41030'" if field == 'versionCode' else "versionName='v4.2.1'", fragment)
                return result
            with self.subTest(fragment=fragment), patch.object(signing.subprocess, 'run', side_effect=invalid):
                with self.assertRaises(signing.SigningError):
                    signing.sign_manager(self.unsigned, output_dir=self.output.parent, build_metadata=path)
                self.assertFalse(self.private_paths)
                self.assertFalse(self.workflow_output.exists())

    def test_signed_version_and_package_must_match_source_actual_metadata(self):
        for before, after in [("versionName='v4.2.1'", "versionName='v9.0'"),
                              ("versionCode='41030'", "versionCode='99999'"),
                              ("com.sukisu.ultra", "com.example.changed")]:
            def different(args, **kwargs):
                result = self.fake_sdk(args, **kwargs)
                if Path(args[0]).name == 'aapt2' and Path(args[-1]) != self.unsigned.resolve():
                    result.stdout = result.stdout.replace(before, after)
                return result
            with self.subTest(after=after), patch.object(signing.subprocess, 'run', side_effect=different):
                with self.assertRaisesRegex(signing.SigningError, 'does not match the source APK'):
                    self.run_signing()
                self.assertFalse(self.output.exists())
                self.assertFalse(self.workflow_output.exists())
                self.assertFalse(list(self.private_root.iterdir()))

    def test_output_directory_cli_is_supported_and_output_flags_are_exclusive(self):
        stdout = io.StringIO()
        directory = self.root / 'versioned-artifacts'
        metadata_path = self.build_metadata()
        with patch.object(signing.subprocess, 'run', side_effect=self.fake_sdk):
            with contextlib.redirect_stdout(stdout):
                code = signing.main(['--unsigned', str(self.unsigned), '--output-dir', str(directory),
                                     '--build-metadata', str(metadata_path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())['apk_filename'], 'SukiSU_v4.2.1_41030-release.apk')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            signing.main(['--unsigned', str(self.unsigned), '--output', str(self.output),
                          '--output-dir', str(directory)])


if __name__ == "__main__":
    unittest.main()
