from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from kernel_build_identity import build_id


class KernelBuildIdentityTests(unittest.TestCase):
    def test_same_fixed_kernel_gets_the_same_eight_digit_android_build_id(self):
        reference = '394605f7e8266e836a8acf09a8deabd4ab6d7e70'
        first = build_id(reference)
        self.assertRegex(first, r'^ab[1-9][0-9]{7}$')
        self.assertEqual(first, build_id(reference))

    def test_different_fixed_source_revisions_have_different_representative_ids(self):
        revisions = ('a' * 40, 'b' * 40, 'c' * 40,
                     '394605f7e8266e836a8acf09a8deabd4ab6d7e70')
        values = {build_id(reference) for reference in revisions}
        self.assertEqual(len(revisions), len(values))

    def test_unknown_or_short_references_are_not_given_a_shared_fallback_identity(self):
        for reference in (None, 7, '', 'HEAD', 'main', 'a' * 13, 'a' * 39,
                          'a' * 41, 'A' * 40, 'z' * 40, 'a' * 40 + '\n'):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                build_id(reference)

    def test_cli_uses_only_a_verified_full_commit(self):
        command = [sys.executable, str(ROOT / 'tools/kernel_build_identity.py'), '--source-ref']
        reference = '394605f7e8266e836a8acf09a8deabd4ab6d7e70'
        valid = subprocess.run([*command, reference], capture_output=True, text=True, timeout=20)
        self.assertEqual(0, valid.returncode, valid.stderr)
        self.assertEqual(build_id(reference), valid.stdout.strip())
        invalid = subprocess.run([*command, 'main'], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(0, invalid.returncode)
        self.assertEqual('', invalid.stdout)
        self.assertIn('fixed full kernel Git commit', invalid.stderr)

    def test_native_template_keeps_custom_version_and_kmi_suffixes(self):
        text = (ROOT / 'templates/kernel-job.yml').read_text(encoding='utf-8')
        block = text.split('  - name: 配置内核名称\n', 1)[1].split('  - name: 设置自定义构建时间\n', 1)[0]
        custom, default = block.split('elif [ ! -f "build/build.sh" ]; then', 1)
        self.assertIn('if [ -n "$VERSION_INPUT" ]; then', custom)
        self.assertNotIn('kernel_build_identity.py', custom)
        self.assertIn('cd ./common', default)
        self.assertIn('KERNEL_SOURCE_REF="$(git rev-parse --verify HEAD)"', default)
        self.assertIn('--source-ref "$KERNEL_SOURCE_REF"', default)
        self.assertIn('GHASH="${KERNEL_SOURCE_REF:0:13}"', default)
        self.assertNotIn('RANDOM', default)
        self.assertIn('KMI_LOCAL="-${KMI_TAG}-g${GHASH}-${BID}-4k"', default)
        self.assertIn('SUFFIX="-${KMI_TAG}-g${GHASH}-${BID}"', default)


if __name__ == '__main__':
    unittest.main()
