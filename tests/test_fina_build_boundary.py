"""A canonical repack must never receive the permanent release signing key."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_fina_manager as builder


class FinaBuildBoundaryTests(unittest.TestCase):
    def test_canonical_stages_use_only_a_disposable_key_and_cleanup_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            observed = {}

            def generate(key, password, environment):
                self.assertNotIn('SUKISU_APK_KEYSTORE', environment)
                self.assertNotIn('SUKISU_APK_PASSWORD', environment)
                self.assertNotIn('ORG_GRADLE_PROJECT_KEYSTORE_PASSWORD', environment)
                self.assertNotEqual('permanent-password', password)
                key.write_bytes(b'disposable-key-fixture')
                observed['key'] = key

            class FailedNativeBuild:
                def __init__(self, args, **options):
                    self.args, self.options = args, options
                    self.stdout = io.StringIO('native build stopped\n')
                def __enter__(self):
                    self.assertions()
                    return self
                def assertions(self):
                    self_test.assertEqual(['nomount', 'sui', 'ksuinit', 'ksud', 'apk', 'repack'], self.args[2:])
                    env = self.options['env']
                    self_test.assertNotIn('SUKISU_APK_KEYSTORE', env)
                    self_test.assertNotIn('SUKISU_APK_PASSWORD', env)
                    self_test.assertIn('gki-fina-build-only', Path(env['KEYSTORE_PROPS']).read_text('utf-8'))
                    observed['properties'] = Path(env['KEYSTORE_PROPS'])
                def __exit__(self, *args):
                    return False
                def wait(self):
                    return 1

            self_test = self
            environment = {'RUNNER_TEMP': str(root), 'SUKISU_APK_KEYSTORE': 'permanent-private-key',
                'SUKISU_APK_PASSWORD': 'permanent-password', 'ORG_GRADLE_PROJECT_KEYSTORE_PASSWORD': 'permanent-password'}
            with patch.dict(os.environ, environment), patch.object(builder, 'prepare'), \
                    patch.object(builder, 'generate_build_key', side_effect=generate), \
                    patch.object(builder.subprocess, 'Popen', FailedNativeBuild):
                with self.assertRaisesRegex(ValueError, 'canonical native build'):
                    builder.build(source, root / 'output')
            self.assertFalse(observed['key'].exists())
            self.assertFalse(observed['properties'].exists())
            self.assertFalse((root / 'output/manager-unsigned.apk').exists())


if __name__ == '__main__':
    unittest.main()
