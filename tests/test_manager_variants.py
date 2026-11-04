"""Keep menu groups explicit while every build worker has one manager."""
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import manager_variants as variants


class ManagerSelectionTests(unittest.TestCase):
    def test_both_reviewed_groups_expand_in_their_menu_order(self):
        expected = {
            'SukiSU + BakaSU + KernelSU-Next': ('SukiSU', 'BakaSU', 'KernelSU-Next'),
            'KowSU + KernelSU + FinaSU': ('KowSU', 'KernelSU', 'FinaSU'),
        }
        self.assertEqual(expected, variants.COMBINED_GROUPS)
        self.assertEqual(variants.COMBINED_MANAGERS,
                         variants.COMBINED_GROUPS[variants.COMBINED_SELECTION])
        for label, members in expected.items():
            with self.subTest(label=label):
                self.assertEqual(members, variants.selected_managers(label))
                self.assertEqual(3, len(set(members)))
        self.assertEqual(set(variants.MANAGERS), {item for group in expected.values() for item in group})

    def test_menu_retains_all_single_managers_and_appends_the_two_groups(self):
        self.assertEqual(variants.MANAGERS + tuple(variants.COMBINED_GROUPS), variants.MANAGER_SELECTIONS)
        self.assertEqual(8, len(set(variants.MANAGER_SELECTIONS)))
        for manager in variants.MANAGERS:
            self.assertEqual((manager,), variants.selected_managers(manager))

    def test_arbitrary_groups_reordering_and_invalid_types_are_rejected(self):
        for value in ('', 'KernelSU + KowSU + FinaSU', 'KowSU + KernelSU', 'SukiSU + FinaSU',
                      'KowSU + KernelSU + FinaSU + FinaSU', None, True, 1, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                variants.selected_managers(value)

    def test_groups_never_become_worker_or_package_identities(self):
        for label in variants.COMBINED_GROUPS:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    variants.manager_profile(label)
                with mock.patch.dict(os.environ, {'KSU_VARIANT': label}), self.assertRaises(ValueError):
                    variants.selected_variant()
        for manager in variants.MANAGERS:
            with mock.patch.dict(os.environ, {'KSU_VARIANT': manager}):
                self.assertEqual(manager, variants.selected_variant())


if __name__ == '__main__':
    unittest.main()
