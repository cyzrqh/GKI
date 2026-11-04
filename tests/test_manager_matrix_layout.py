import copy
import unittest

from manager_matrix_layout import expand, layout
from manager_variants import MANAGERS, COMBINED_GROUPS
from resolve_build_sources import REPOSITORIES, source_result


def receipt(selection, active):
    requested = COMBINED_GROUPS[selection]
    return {'schema_version': 1, 'selection': selection, 'tier': '正式版',
            'active_managers': active,
            'skipped_managers': [{'variant': name, 'reason': '官方尚未发布正式版'}
                                 for name in requested if name not in active],
            'sources': {name: source_result('a' * 40, REPOSITORIES[name], 'v1.0', 'Stable(标准)')
                        for name in active}}


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
        for selection, variants in COMBINED_GROUPS.items():
            with self.subTest(selection=selection):
                result = layout(self.kernel, self.ddk, selection)
                self.assertEqual((256, 35), (len(result['kernel_primary']['include']), len(result['kernel_overflow']['include'])))
                rows = result['kernel_primary']['include'] + result['kernel_overflow']['include']
                self.assertEqual(291, len({(row['variant'], row['kernel']) for row in rows}))
                self.assertEqual(set(variants), {row['variant'] for row in rows})
                for variant in variants:
                    self.assertEqual(self.kernel['include'], [{k: v for k, v in row.items() if k != 'variant'}
                        for row in rows if row['variant'] == variant])
                    self.assertEqual(self.ddk['include'], [{k: v for k, v in row.items() if k != 'variant'}
                        for row in result['ddk_build_matrix']['include'] if row['variant'] == variant])
                self.assertEqual(24, len(result['ddk_build_matrix']['include']))
                self.assertEqual(set(variants), {row['variant'] for row in result['ddk_build_matrix']['include']})
        self.assertEqual(before, self.kernel)

    def test_apk_only_mode_has_no_kernel_rows_to_execute(self):
        for selection, variants in [('BakaSU', ('BakaSU',)), *COMBINED_GROUPS.items()]:
            with self.subTest(selection=selection):
                result = layout({'include': []}, self.ddk, selection)
                self.assertEqual('0', result['overflow_count'])
                self.assertEqual({'include': [{}]}, result['kernel_primary'])
                self.assertEqual({'include': [{}]}, result['kernel_overflow'])
                self.assertEqual(8 * len(variants), len(result['ddk_build_matrix']['include']))
                self.assertEqual(set(variants), {row['variant'] for row in result['ddk_build_matrix']['include']})

    def test_missing_formal_releases_filter_both_kernel_and_native_module_rows(self):
        for selection, requested in COMBINED_GROUPS.items():
            for active in (list(requested[1:]), list(requested[:1]), list(requested[-1:])):
                with self.subTest(selection=selection, active=active):
                    result = layout(self.kernel, self.ddk, selection, receipt(selection, active))
                    self.assertEqual(97 * len(active), len(result['kernel_primary']['include']))
                    self.assertEqual('0', result['overflow_count'])
                    self.assertEqual(8 * len(active), len(result['ddk_build_matrix']['include']))
                    self.assertEqual(set(active), {row['variant'] for row in result['kernel_primary']['include']})
                    self.assertEqual(set(active), {row['variant'] for row in result['ddk_build_matrix']['include']})

    def test_all_unavailable_managers_produce_no_expanded_work(self):
        for selection in COMBINED_GROUPS:
            self.assertEqual([], expand(self.kernel, selection, receipt(selection, [])))
            self.assertEqual([], expand(self.ddk, selection, receipt(selection, [])))

    def test_selection_receipt_cannot_silently_omit_or_inject_a_manager(self):
        selection = next(iter(COMBINED_GROUPS))
        variants = list(COMBINED_GROUPS[selection])
        malformed = receipt(selection, variants[1:])
        malformed['skipped_managers'] = []
        with self.assertRaises(ValueError):
            layout(self.kernel, self.ddk, selection, malformed)
        unrelated = receipt(selection, variants)
        unrelated['active_managers'].append('KowSU')
        with self.assertRaises(ValueError):
            layout(self.kernel, self.ddk, selection, unrelated)

    def test_route_injection_unknown_manager_and_oversized_matrices_are_rejected(self):
        for matrix, selection in (({'include': [{'variant': 'BakaSU'}]}, 'SukiSU'),
                                  (self.kernel, 'unreviewed'),
                                  ({'include': [{}] * 513}, 'SukiSU')):
            with self.assertRaises(ValueError):
                layout(matrix, self.ddk, selection)


if __name__ == '__main__':
    unittest.main()
