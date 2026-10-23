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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import export_manager_certificate as certificate

KEY_BYTES = b'private PKCS12 fixture: never public'
ENCODED_KEY = base64.b64encode(KEY_BYTES).decode('ascii')
PASSWORD = 'fixture-password-must-not-be-logged'
CERTIFICATE = b'\x30\x82public X509 DER fixture'
CERT_SHA = hashlib.sha256(CERTIFICATE).hexdigest()


class ManagerCertificateTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.public-cert-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.private = self.root / 'runner-temp'
        self.private.mkdir()
        self.output = self.root / 'public' / 'manager-public-certificate.json'
        self.outputs = self.root / 'github-output'
        environment = {certificate.signing.KEYSTORE_ENV: ENCODED_KEY,
                       certificate.signing.PASSWORD_ENV: PASSWORD,
                       'RUNNER_TEMP': str(self.private), 'GITHUB_OUTPUT': str(self.outputs),
                       'ANDROID_HOME': '', 'ANDROID_SDK_ROOT': ''}
        manager = patch.dict(os.environ, environment)
        manager.start()
        self.addCleanup(manager.stop)
        discovery = patch.object(certificate.shutil, 'which', return_value='keytool')
        discovery.start()
        self.addCleanup(discovery.stop)
        if os.name == 'nt':
            # Mirror the signing tests: Windows' private tempfile ACL excludes
            # the sandbox identity, so only mocked keytool fixtures use mkdir.
            directory = patch.object(certificate.tempfile, 'TemporaryDirectory',
                                     side_effect=self.private_directory)
            directory.start()
            self.addCleanup(directory.stop)
        self.calls, self.key_paths = [], []

    @contextlib.contextmanager
    def private_directory(self, *, prefix, dir):
        path = Path(dir) / (prefix + uuid.uuid4().hex)
        path.mkdir()
        try:
            yield str(path)
        finally:
            shutil.rmtree(path)

    def fake_keytool(self, arguments, **options):
        self.calls.append((arguments, options))
        self.assertEqual(arguments[0], 'keytool')
        if arguments[1] == '-exportcert':
            keystore = Path(arguments[arguments.index('-keystore') + 1])
            self.key_paths.append(keystore)
            self.assertEqual(KEY_BYTES, keystore.read_bytes())
            self.assertTrue(keystore.is_relative_to(self.private))
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(keystore.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(keystore.parent.stat().st_mode), 0o700)
            Path(arguments[arguments.index('-file') + 1]).write_bytes(CERTIFICATE)
        elif arguments[1] != '-printcert':
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout='Certificate exported', stderr='')

    def test_exports_only_public_identity_and_needs_no_apk_or_android_sdk(self):
        with patch.object(certificate.signing.subprocess, 'run', side_effect=self.fake_keytool):
            metadata = certificate.export_certificate(self.output)
        expected = {'schema_version': 1, 'manager_package': 'com.cyzrqh.gki.sukisu',
                    'manager_cert_size': len(CERTIFICATE), 'manager_cert_sha256': CERT_SHA}
        self.assertEqual(expected, metadata)
        self.assertEqual(expected, json.loads(self.output.read_text(encoding='utf-8')))
        self.assertEqual(['-exportcert', '-printcert'], [arguments[1] for arguments, _ in self.calls])
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])
        self.assertFalse(list(self.private.iterdir()))
        outputs = dict(line.split('=', 1) for line in self.outputs.read_text(encoding='utf-8').splitlines())
        self.assertEqual(outputs, {'package': expected['manager_package'], 'cert_size': str(len(CERTIFICATE)),
                                   'cert_sha256': CERT_SHA, 'public_json': str(self.output.resolve())})
        self.assertTrue(all(not path.exists() for path in self.key_paths))
        self.assertNotIn(ENCODED_KEY, self.output.read_text() + self.outputs.read_text())
        self.assertNotIn(PASSWORD, self.output.read_text() + self.outputs.read_text())

    def test_subprocess_never_receives_private_key_and_password_uses_environment_only(self):
        with patch.object(certificate.signing.subprocess, 'run', side_effect=self.fake_keytool):
            certificate.export_certificate(self.output)
        for arguments, options in self.calls:
            self.assertNotIn(PASSWORD, ' '.join(arguments))
            self.assertNotIn(ENCODED_KEY, ' '.join(arguments))
            self.assertNotIn(certificate.signing.KEYSTORE_ENV, options['env'])
            self.assertNotIn('shell', options)
            if arguments[1] == '-exportcert':
                self.assertEqual(options['env'][certificate.signing.PASSWORD_ENV], PASSWORD)
                self.assertIn('-storepass:env', arguments)
                self.assertEqual(arguments[arguments.index('-alias') + 1], 'gki-sukisu')
            else:
                self.assertNotIn(certificate.signing.PASSWORD_ENV, options['env'])

    def test_missing_secrets_invalid_base64_or_password_fail_without_running_keytool(self):
        for changes in ({certificate.signing.KEYSTORE_ENV: ''}, {certificate.signing.PASSWORD_ENV: ''},
                        {certificate.signing.KEYSTORE_ENV: 'bad base64!'}):
            with self.subTest(changes=changes), patch.dict(os.environ, changes):
                with patch.object(certificate.signing.subprocess, 'run') as run:
                    with self.assertRaises(certificate.signing.SigningError):
                        certificate.export_certificate(self.output)
                    run.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.outputs.exists())

    def test_keytool_or_runner_temp_missing_does_not_create_private_material(self):
        with patch.object(certificate.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(certificate.signing.SigningError, 'keytool was not found'):
                certificate.export_certificate(self.output)
        with patch.dict(os.environ, {'RUNNER_TEMP': ''}):
            with self.assertRaisesRegex(certificate.signing.SigningError, 'RUNNER_TEMP'):
                certificate.export_certificate(self.output)
        self.assertFalse(list(self.private.iterdir()))
        self.assertFalse(self.output.exists())

    def test_failed_export_preserves_previous_public_json_cleans_key_and_redacts_cli(self):
        self.output.parent.mkdir()
        self.output.write_text('previous public identity', encoding='utf-8')

        def failed(arguments, **options):
            self.fake_keytool(arguments, **options)
            return subprocess.CompletedProcess(arguments, 1, stdout=PASSWORD, stderr=ENCODED_KEY)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(certificate.signing.subprocess, 'run', side_effect=failed):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = certificate.main(['--output', str(self.output)])
        self.assertEqual(1, result)
        self.assertEqual('previous public identity', self.output.read_text(encoding='utf-8'))
        self.assertIn('[SIGNING_SECRET_REDACTED]', stderr.getvalue())
        for secret in (PASSWORD, ENCODED_KEY):
            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
        self.assertFalse(self.outputs.exists())
        self.assertFalse(list(self.private.iterdir()))

    def test_missing_oversized_or_invalid_certificate_is_not_published(self):
        for payload in (None, b'', b'x' * (64 * 1024 + 1), b'invalid X509 bytes'):
            def bad_certificate(arguments, **options):
                result = self.fake_keytool(arguments, **options)
                path = Path(arguments[arguments.index('-file') + 1])
                if arguments[1] == '-exportcert':
                    if payload is None:
                        path.unlink()
                    else:
                        path.write_bytes(payload)
                elif payload == b'invalid X509 bytes':
                    return subprocess.CompletedProcess(arguments, 1, stdout='', stderr='Not an X.509 certificate')
                return result
            with self.subTest(payload=None if payload is None else len(payload)):
                with patch.object(certificate.signing.subprocess, 'run', side_effect=bad_certificate):
                    with self.assertRaises(certificate.signing.SigningError):
                        certificate.export_certificate(self.output)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.outputs.exists())
                self.assertFalse(list(self.private.iterdir()))

    def test_successful_cli_prints_only_the_allowlisted_public_fields(self):
        stdout = io.StringIO()
        with patch.object(certificate.signing.subprocess, 'run', side_effect=self.fake_keytool):
            with contextlib.redirect_stdout(stdout):
                result = certificate.main(['--output', str(self.output)])
        self.assertEqual(0, result)
        self.assertEqual({'schema_version', 'manager_package', 'manager_cert_size', 'manager_cert_sha256'},
                         set(json.loads(stdout.getvalue())))
        self.assertNotIn(PASSWORD, stdout.getvalue())
        self.assertNotIn(ENCODED_KEY, stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
