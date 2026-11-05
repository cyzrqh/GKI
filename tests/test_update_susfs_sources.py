"""Prove metadata-only SUSFS pin advancement never accepts code/ABI changes."""
from contextlib import redirect_stdout, redirect_stderr
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import update_susfs_sources as update

OLD, NEW, DIFFERENT = '1' * 40, '2' * 40, '3' * 40


def profiles():
    return ({'schema_version': 1, 'sukisu_susfs': {'source_ref': '4' * 40,
                'version': 'v2.3.0', 'pins': {target: OLD for target in update.TARGETS},
                'source_policy': 'checked-patch', 'keep': ['native-setting']}},
            {'schema_version': 1, 'version': 'v2.3.0', 'reason': 'reviewed',
             'managers': {variant: {'source_refs': ['5' * 40],
                 'pins': {target: OLD for target in update.TARGETS}} for variant in update.MANAGERS[1:]}})


def transport(url, limit):
    reference = url.split('ref=')[1].split('&')[0]
    if '/repository/tree?' in url:
        assert '&page=1' in url
        rows = [{'name': 'kernel_patches', 'path': 'kernel_patches', 'mode': '040000', 'type': 'tree',
                 'id': ('a' if reference != DIFFERENT else 'b') * 40},
                {'name': 'README.md', 'path': 'README.md', 'mode': '100644', 'type': 'blob', 'id': reference},
                {'name': 'setup.sh', 'path': 'setup.sh', 'mode': '100755', 'type': 'blob', 'id': 'c' * 40}]
        return json.dumps(rows).encode()
    return b'#define SUSFS_VERSION "v2.3.0"\n'


class SusfsSyncTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.managers = profiles()
        self.sources = {target: NEW for target in update.TARGETS}

    def plan(self, sources=None, fetch=transport):
        return update.plan(sources or self.sources, self.profile, self.managers, fetch=fetch)

    def test_same_build_tree_advances_all_manager_pins_and_preserves_every_other_setting(self):
        receipt, profile, managers = self.plan()
        self.assertEqual({'current'}, {row['status'] for rows in receipt['pins'].values() for row in rows.values()})
        self.assertEqual({NEW}, set(profile['sukisu_susfs']['pins'].values()))
        self.assertEqual({NEW}, {ref for item in managers['managers'].values() for ref in item['pins'].values()})
        for target in update.TARGETS:
            profile['sukisu_susfs']['pins'][target] = OLD
            for variant in update.MANAGERS[1:]:
                managers['managers'][variant]['pins'][target] = OLD
        self.assertEqual(self.profile, profile)
        self.assertEqual(self.managers, managers)

    def test_changed_code_tree_records_pending_without_changing_any_pin(self):
        receipt, profile, managers = self.plan({target: DIFFERENT for target in update.TARGETS})
        self.assertEqual({'pending_adaptation'}, {row['status'] for rows in receipt['pins'].values() for row in rows.values()})
        self.assertEqual(self.profile, profile)
        self.assertEqual(self.managers, managers)
        for rows in receipt['pins'].values():
            self.assertEqual({OLD}, {row['selected_ref'] for row in rows.values()})
            self.assertEqual({'build-tree-changed'}, {row['reason'] for row in rows.values()})

    def test_root_script_or_hidden_tree_changes_are_included_even_when_kernel_patch_tree_matches(self):
        for name in ('setup.sh', '.github'):
            def changed(url, limit):
                raw = transport(url, limit)
                if '/repository/tree?' not in url:
                    return raw
                rows = json.loads(raw)
                if name == '.github':
                    rows.append({'name': name, 'path': name, 'mode': '040000', 'type': 'tree',
                                 'id': ('b' if 'ref=' + NEW in url else 'c') * 40})
                elif 'ref=' + NEW in url:
                    rows[2]['id'] = 'b' * 40
                return json.dumps(rows).encode()
            with self.subTest(name=name):
                receipt, profile, managers = self.plan(fetch=changed)
                self.assertEqual('pending_adaptation', receipt['pins']['KowSU'][update.TARGETS[0]]['status'])
                self.assertEqual(self.profile, profile)

    def test_version_change_is_pending_and_old_abi_is_retained(self):
        def changed(url, limit):
            raw = transport(url, limit)
            return raw.replace(b'v2.3.0', b'v2.4.0') if '/raw?ref=' + NEW in url else raw
        receipt, profile, _ = self.plan(fetch=changed)
        self.assertEqual('version-changed', receipt['pins']['KowSU'][update.TARGETS[0]]['reason'])
        self.assertEqual(self.profile, profile)

    def test_receipt_and_config_forgery_are_rejected_before_merge(self):
        receipt, profile, managers = self.plan({target: DIFFERENT for target in update.TARGETS})
        altered = copy.deepcopy(receipt)
        for variant in update.MANAGERS:
            for row in altered['pins'][variant].values():
                row.update(selected_ref=DIFFERENT, selected_build_tree_sha256=altered['candidates'][update.TARGETS[0]]['build_tree_sha256'],
                           status='current', reason='selected')
        with self.assertRaisesRegex(update.SusfsUpdateError, 'unverified pins'):
            update.validate_update(None, altered, self.profile, self.managers, fetch=transport)
        bad = copy.deepcopy(profile)
        bad['sukisu_susfs']['source_policy'] = 'unsafe'
        with self.assertRaisesRegex(update.SusfsUpdateError, 'unverified pins'):
            update.validate_update(None, receipt, self.profile, self.managers, new_profile=bad, fetch=transport)
        with self.assertRaisesRegex(update.SusfsUpdateError, 'effective compatibility pins'):
            update.verify_receipt_pins(altered, self.profile, self.managers)

    def test_repeat_receipt_is_stable_after_safe_promotion_and_after_pending_adaptation(self):
        for reference in (NEW, DIFFERENT):
            with self.subTest(reference=reference):
                sources = {target: reference for target in update.TARGETS}
                first, profile, managers = self.plan(sources)
                second, final_profile, final_managers = update.plan(sources, profile, managers, fetch=transport)
                self.assertEqual(first, second)
                self.assertEqual(profile, final_profile)
                self.assertEqual(managers, final_managers)

    def test_one_changed_target_does_not_block_safe_pin_advancement_on_other_targets(self):
        sources = {**self.sources, update.TARGETS[0]: DIFFERENT}
        receipt, profile, managers = self.plan(sources)
        self.assertEqual(OLD, profile['sukisu_susfs']['pins'][update.TARGETS[0]])
        self.assertEqual(NEW, profile['sukisu_susfs']['pins'][update.TARGETS[1]])
        self.assertEqual('pending_adaptation', receipt['pins']['KowSU'][update.TARGETS[0]]['status'])
        self.assertEqual('current', receipt['pins']['KowSU'][update.TARGETS[1]]['status'])

    def test_frozen_official_candidate_disagreement_blocks_merge(self):
        receipt, profile, managers = self.plan()
        with self.assertRaisesRegex(update.SusfsUpdateError, 'frozen official source snapshot'):
            update.validate_update(None, receipt, self.profile, self.managers,
                                   expected_sources={target: DIFFERENT for target in update.TARGETS}, fetch=transport)

    def test_incomplete_receipts_and_duplicate_json_keys_are_not_treated_as_updates(self):
        receipt, _, _ = self.plan()
        malformed = copy.deepcopy(receipt)
        malformed['pins'].pop('KowSU')
        with self.assertRaises(update.SusfsUpdateError):
            update.normalize_receipt(malformed)
        with self.assertRaisesRegex(update.SusfsUpdateError, 'Duplicate'):
            update.normalize_receipt('{"schema_version":1,"schema_version":1}')

    def test_api_duplicate_paths_unknown_modes_and_missing_kernel_tree_are_rejected(self):
        for change in ('duplicate', 'link', 'missing'):
            def changed(url, limit):
                raw = transport(url, limit)
                if '/repository/tree?' not in url:
                    return raw
                rows = json.loads(raw)
                if change == 'duplicate': rows.append(rows[0])
                elif change == 'link': rows[0]['mode'] = '120000'
                else: rows.pop(0)
                return json.dumps(rows).encode()
            with self.subTest(change=change), self.assertRaises(update.SusfsUpdateError):
                self.plan(fetch=changed)

    def test_cli_check_is_read_only_apply_uses_fixed_receipt_and_repeat_has_no_update(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'data').mkdir()
            for path, document in ((update.PROFILE_PATH, self.profile), (update.PINS_PATH, self.managers)):
                (root / path).write_text(json.dumps(document), encoding='utf-8')
            original = {path: (root / path).read_bytes() for path in (update.PROFILE_PATH, update.PINS_PATH)}
            output = root / 'outputs'
            def invoke(*args):
                output.write_text('', encoding='utf-8')
                with mock.patch.object(update, 'request_bytes', side_effect=transport), mock.patch.object(update, 'gitlab_head', side_effect=AssertionError('branch re-lookup')), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = update.main([*args, '--root', str(root), '--github-output', str(output)])
                values = dict(line.split('=', 1) for line in output.read_text('utf-8').splitlines())
                return result, values
            status, values = invoke('--check', '--candidates-json', json.dumps({'susfs': self.sources}))
            self.assertEqual(0, status)
            self.assertEqual('true', values['has_updates'])
            self.assertFalse((root / update.RECEIPT_PATH).exists())
            self.assertEqual(original, {path: (root / path).read_bytes() for path in original})
            status, applied = invoke('--apply-candidates-json', values['candidates_json'])
            self.assertEqual(0, status)
            self.assertEqual(values['candidates_json'], applied['candidates_json'])
            self.assertEqual('false', invoke('--check', '--candidates-json', json.dumps(self.sources))[1]['has_updates'])
            self.assertEqual('false', invoke('--apply-candidates-json', values['candidates_json'])[1]['has_updates'])

    def test_cli_independent_check_uses_only_five_susfs_heads(self):
        receipt, _, _ = self.plan()
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / update.PROFILE_PATH).write_text(json.dumps(self.profile), encoding='utf-8')
            (root / update.PINS_PATH).write_text(json.dumps(self.managers), encoding='utf-8')
            with mock.patch.object(update, 'gitlab_head', return_value=NEW) as heads, mock.patch.object(update, 'request_bytes', side_effect=transport), redirect_stdout(io.StringIO()):
                self.assertEqual(0, update.main(['--check', '--root', str(root)]))
            self.assertEqual(list(update.TARGETS), [call.args[0] for call in heads.call_args_list])


if __name__ == '__main__':
    unittest.main()
