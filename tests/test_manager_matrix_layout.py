import copy
import unittest

from manager_matrix_layout import layout
from manager_variants import MANAGERS, COMBINED_MANAGERS, COMBINED_SELECTION


class ManagerMatrixLayoutTests(unittest.TestCase):
    def setUp(self):
        self.kernel = {'include': [{'kernel': '6.1.' + str(i), 'revision': 'a' * 40} for i in range(97)]}
        self.ddk = {'include': [{'kmi': 'kmi-' + str(i), 'image': 'image@sha256:' + 'b' * 64} for i in range(8)]}

    def test_single_selection_does_not_generate_any_other_manager(self):
        for variant in MANAGERS:
            result = layout(self.kernel, self.ddk, variant)
            self.assertEqual('0', result['overflow_count'])
            self.assertEqual(97, len(result['kernel_primary']['include']))
            self.assertEqual(8, len(result['ddk_build_matrix']['include']))
            for matrix in ('kernel_primary', 'ddk_build_matrix'):
                self.assertEqual({variant}, {row['variant'] for row in result[matrix]['include']})

    def test_full_combined_matrix_preserves_every_target_once_within_github_limits(self):
        before = copy.deepcopy(self.kernel)
        result = layout(self.kernel, self.ddk, COMBINED_SELECTION)
        self.assertEqual((256, 35), (len(result['kernel_primary']['include']), len(result['kernel_overflow']['include'])))
        rows = result['kernel_primary']['include'] + result['kernel_overflow']['include']
        self.assertEqual(291, len({(row['variant'], row['kernel']) for row in rows}))
        for variant in COMBINED_MANAGERS:
            self.assertEqual(self.kernel['include'], [{k: v for k, v in row.items() if k != 'variant'}
                for row in rows if row['variant'] == variant])
        self.assertEqual(24, len(result['ddk_build_matrix']['include']))
        self.assertEqual(before, self.kernel)

    def test_apk_only_mode_has_no_kernel_rows_to_execute(self):
        result = layout({'include': []}, self.ddk, 'BakaSU')
        self.assertEqual('0', result['overflow_count'])
        self.assertEqual({'include': [{}]}, result['kernel_primary'])
        self.assertEqual(8, len(result['ddk_build_matrix']['include']))

    def test_route_injection_unknown_manager_and_oversized_matrices_are_rejected(self):
        for matrix, selection in (({'include': [{'variant': 'BakaSU'}]}, 'SukiSU'),
                                  (self.kernel, 'unreviewed'),
                                  ({'include': [{}] * 513}, 'SukiSU')):
            with self.assertRaises(ValueError):
                layout(matrix, self.ddk, selection)


if __name__ == '__main__':
    unittest.main()
