"""Execute the generated feature configuration for every paired manager."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from generate_workflow import create_workflow
from manager_variants import MANAGERS

BASH = os.environ.get('GKI_TEST_BASH') or shutil.which('bash')


@unittest.skipUnless(BASH, 'Bash is required to execute the generated configuration')
class ManagerFeatureConfigTests(unittest.TestCase):
    def test_requested_kpm_is_enabled_or_explicitly_disabled_for_all_six_managers(self):
        root = ROOT / 'tests' / ('.feature-config-' + uuid.uuid4().hex)
        root.mkdir()
        self.addCleanup(shutil.rmtree, root)
        (root / 'common').mkdir()
        (root / 'Kconfig').write_text('config KPM\n\tbool "KPM"\n')
        workflow = create_workflow(ROOT)
        step = next(s for s in workflow['jobs']['build-kernel']['steps'] if s.get('name') == '配置内核选项')
        for manager in MANAGERS:
            for enabled in (False, True):
                with self.subTest(manager=manager, kpm=enabled):
                    values = {'inputs.kernelsu_variant': manager, 'inputs.use_kpm': str(enabled).lower(),
                              'inputs.enable_susfs': 'false', 'matrix.sub_level': '236',
                              'matrix.android_version': 'android12', 'matrix.kernel_version': '5.10',
                              "'aosp'": 'aosp'}
                    text = re.sub(r'\$\{\{\s*(.*?)\s*\}\}', lambda m: values[m[1]], step['run'])
                    script = root / 'configure.sh'
                    script.write_text(text, encoding='utf-8', newline='\n')
                    config = root / 'gki_defconfig'
                    config.write_text('CONFIG_64BIT=y\nCONFIG_ARM64=y\n')
                    (root / 'common/build.config.gki').write_text('check_defconfig\n')
                    result = subprocess.run([BASH, '--noprofile', '--norc', '-c',
                        'export PATH="/usr/bin:$PATH"; source "$1"', 'feature-config', script.as_posix()], cwd=root,
                        env={**os.environ, 'DEFCONFIG': config.as_posix(), 'ABK_KPM_KCONFIG_READY': 'true',
                             'ABK_KPM_KCONFIG_PATH': (root / 'Kconfig').as_posix()},
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
                    self.assertEqual(0, result.returncode, result.stderr)
                    lines = config.read_text().splitlines()
                    self.assertIn('CONFIG_KSU=y', lines)
                    self.assertIn('CONFIG_KPM=y' if enabled else '# CONFIG_KPM is not set', lines)
                    self.assertNotIn('# CONFIG_KPM is not set' if enabled else 'CONFIG_KPM=y', lines)


if __name__ == '__main__':
    unittest.main()
