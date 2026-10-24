from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from validate_kernel_source import validate_source


class KernelSourceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ('.kernel-source-' + uuid.uuid4().hex)
        (self.root / 'common').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.makefile = self.root / 'common/Makefile'
        self.makefile.write_text('VERSION = 5\nPATCHLEVEL = 10\nSUBLEVEL = 237\n', encoding='utf-8')

    def test_migrated_target_keeps_exact_full_version(self):
        self.assertEqual(validate_source(self.root, '5.10', '237'), '5.10.237')

    def test_different_sublevel_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'matrix requested 5.10.236'):
            validate_source(self.root, '5.10', '236')

    def test_different_family_is_rejected_even_for_lts(self):
        with self.assertRaises(ValueError):
            validate_source(self.root, '5.15', 'X')

    def test_lts_allows_current_sublevel_in_selected_family(self):
        self.assertEqual(validate_source(self.root, '5.10', 'X'), '5.10.237')

    def test_bad_fields_and_duplicate_version_are_rejected(self):
        for family, sublevel in [('5.4', '237'), ('5.10', 'lts'), ('5.10', '0237')]:
            with self.subTest(family=family, sublevel=sublevel), self.assertRaises(ValueError):
                validate_source(self.root, family, sublevel)
        self.makefile.write_text('VERSION=5\nVERSION=6\nPATCHLEVEL=10\nSUBLEVEL=237\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            validate_source(self.root, '5.10', '237')


if __name__ == '__main__':
    unittest.main()
