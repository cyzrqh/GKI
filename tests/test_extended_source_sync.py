"""Exercise independent source detection and the extended merge boundary."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import workflow_jobs
import auto_merge_official_update as merge
from test_auto_merge_official_update import FakeAPI, BASE, HEAD, REPO, contents


class IndependentSourceChecks(unittest.TestCase):
    def probe(self, changed=(), failed=(), candidates=None):
        steps = workflow_jobs.updater_job()['steps']
        script = next(step for step in steps if step.get('id') == 'probe')['run']
        body = script.split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            output = Path(directory) / 'output'
            env = {'GITHUB_OUTPUT': str(output)}
            for name in ('OFFICIAL', 'CATALOG', 'SUSFS', 'OPLUS'):
                env[name + '_RESULT'] = 'failure' if name in failed else 'success'
                env[name + '_CHANGED'] = str(name in changed).lower()
                env[name + '_CANDIDATES'] = ('' if name in failed else
                    json.dumps((candidates or {}).get(name, {})))
            with patch.dict(os.environ, env, clear=True):
                exec(compile(body, '<generated-source-probe>', 'exec'), {})
            return dict(line.split('=', 1) for line in output.read_text(encoding='utf-8').splitlines())

    def test_susfs_or_oplus_alone_can_trigger_an_update_without_manager_changes(self):
        for source in ('OFFICIAL', 'CATALOG', 'SUSFS', 'OPLUS'):
            with self.subTest(source=source):
                result = self.probe(changed=(source,))
                self.assertEqual('true', result['has_updates'])
                self.assertEqual('false', result['partial_failure'])
        self.assertEqual('false', self.probe()['has_updates'])

    def test_failed_source_is_not_parsed_or_reported_as_complete(self):
        result = self.probe(changed=('SUSFS',), failed=('OFFICIAL',))
        self.assertEqual('true', result['has_updates'])
        self.assertEqual('true', result['partial_failure'])
        self.assertEqual('false', self.probe(changed=('OPLUS',), failed=('OPLUS',))['has_updates'])

    def test_frozen_platform_or_susfs_candidate_change_changes_dedup_marker(self):
        for source in ('OPLUS', 'SUSFS'):
            self.assertNotEqual(self.probe()['heads_marker'],
                self.probe(candidates={source: {'head': 'a' * 40}})['heads_marker'])


class ExtendedMergeBoundary(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.addCleanup(patch.stopall)
        patch.object(merge, 'validate_manager_snapshot', return_value={'passed': True}).start()

    def add(self, path, before, after):
        if before is not None:
            self.api.documents[path, BASE] = copy.deepcopy(before)
        self.api.documents[path, HEAD] = copy.deepcopy(after)
        item = {'filename': path, 'status': 'added' if before is None else 'modified',
                'sha': contents(path, after)['sha']}
        self.api.files.append(item)
        self.api.comparison['files'] = copy.deepcopy(self.api.files)
        self.api.pr['changed_files'] = len(self.api.files)

    def test_susfs_pin_changes_without_source_receipt_are_not_merged(self):
        profile = self.api.documents[merge.PROFILE, BASE]
        self.add(merge.PROFILE, profile, profile)
        with self.assertRaisesRegex(merge.MergeError, 'matching verified source receipt'):
            merge.sync(REPO, 1, 'main', self.api)
        self.assertFalse(self.api.mutations)

    def test_oplus_profile_changes_without_source_receipt_are_not_merged(self):
        self.add(merge.OPLUS_PROFILES, {'profiles': []}, {'profiles': []})
        with self.assertRaisesRegex(merge.MergeError, 'matching verified source receipt'):
            merge.sync(REPO, 1, 'main', self.api)
        self.assertFalse(self.api.mutations)

    def test_susfs_verification_receives_exact_base_and_head_and_can_block_merge(self):
        profile = self.api.documents[merge.PROFILE, BASE]
        self.api.documents[merge.PROFILE, HEAD] = copy.deepcopy(profile)
        self.api.documents[merge.MANAGER_PINS, BASE] = {'base': 1}
        self.api.documents[merge.MANAGER_PINS, HEAD] = {'base': 1}
        receipt = {'frozen': 'source'}
        self.add(merge.SUSFS_RECEIPT, None, receipt)
        with patch('update_susfs_sources.validate_update', side_effect=ValueError('incompatible')) as verify:
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                merge.sync(REPO, 1, 'main', self.api)
        self.assertEqual((None, receipt, profile, {'base': 1}), verify.call_args.args)
        self.assertFalse(self.api.mutations)

    def test_validated_susfs_only_receipt_can_merge_without_changing_pins(self):
        profile = self.api.documents[merge.PROFILE, BASE]
        self.api.documents[merge.PROFILE, HEAD] = copy.deepcopy(profile)
        self.api.documents[merge.MANAGER_PINS, BASE] = {'base': 1}
        self.api.documents[merge.MANAGER_PINS, HEAD] = {'base': 1}
        self.add(merge.SUSFS_RECEIPT, None, {'frozen': 'source'})
        with patch('update_susfs_sources.validate_update', return_value={}):
            self.assertTrue(merge.sync(REPO, 1, 'main', self.api)['merged'])
        self.assertEqual(1, len(self.api.mutations))

    def test_oplus_receipt_requires_frozen_upstream_verification_before_merge(self):
        from update_oplus_sources import allowed_paths
        profiles = json.loads((ROOT / merge.OPLUS_PROFILES).read_text(encoding='utf-8'))
        self.api.documents[merge.OPLUS_PROFILES, BASE] = copy.deepcopy(profiles)
        self.api.documents[merge.OPLUS_PROFILES, HEAD] = copy.deepcopy(profiles)
        receipt = {'fixed': 'candidate'}
        self.add(merge.OPLUS_RECEIPT, None, receipt)
        original = merge.read_bytes
        def frozen_bytes(api, path, ref, **options):
            if path in allowed_paths(profiles) - {merge.OPLUS_PROFILES, merge.OPLUS_RECEIPT}:
                self.assertEqual(BASE, ref)  # The verified inventory proves unchanged HEAD bytes.
                return (ROOT / path).read_bytes()
            return original(api, path, ref, **options)
        with patch.object(merge, 'read_bytes', side_effect=frozen_bytes), \
                patch('update_oplus_sources.validate_update', return_value={}) as validate, \
                patch('update_oplus_sources.verify_remote_snapshot', side_effect=ValueError('wrong upstream')) as verify:
            with self.assertRaisesRegex(ValueError, 'wrong upstream'):
                merge.sync(REPO, 1, 'main', self.api)
        self.assertEqual(profiles, validate.call_args.args[0])
        self.assertEqual(receipt, validate.call_args.args[3])
        self.assertEqual(validate.call_args.args[4], validate.call_args.args[5])
        verify.assert_called_once_with(receipt, profiles_document=profiles)
        self.assertFalse(self.api.mutations)


if __name__ == '__main__':
    unittest.main()
