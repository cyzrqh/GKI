"""Verify source-only OPlus sync preserves reviewed executable adaptations."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import update_oplus_sources as update


def file_document(path, raw):
    return {'type': 'file', 'path': path, 'size': len(raw), 'encoding': 'base64',
            'sha': hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(),
            'content': base64.b64encode(raw).decode()}


class FakeReader:
    def __init__(self, profile, recipe):
        self.profile, self.recipe = profile, recipe
        self.version = profile['kernel']
        self.head = 'a' * 40
        self.recipe_head = 'b' * 40
        self.timestamp = '2026-11-05T01:00:00Z'
        self.calls = []

    def commit(self, ref):
        return {'sha': ref, 'committer': {'date': self.timestamp}}

    def get(self, repository, suffix=''):
        self.calls.append((repository, suffix))
        if suffix == '': return {'full_name': repository, 'default_branch': 'main'}
        if suffix.startswith('git/commits/'):
            return self.commit(suffix.split('/')[-1])
        if suffix.startswith('branches/'):
            from urllib.parse import unquote
            name = unquote(suffix.removeprefix('branches/'))
            commit = self.commit(self.recipe_head if repository in update.catalog.RECIPE_REPOSITORIES else self.head)
            commit['commit'] = {'committer': {'date': self.timestamp}}
            return {'name': name, 'commit': commit}
        path = suffix.split('?', 1)[0].removeprefix('contents/')
        if path == '.github/workflows':
            if repository == self.profile['recipe_repository']:
                return [{'type': 'file', 'path': self.profile['recipe_workflow'], 'sha': 'c' * 40},
                        {'type': 'file', 'path': '.github/workflows/fastbuild_6.1.999.yml', 'sha': 'd' * 40},
                        {'type': 'file', 'path': '.github/workflows/unrelated.yml', 'sha': 'e' * 40}]
            return []
        if path == 'Makefile':
            a, b, c = self.version.split('.')
            return file_document(path, f'VERSION = {a}\nPATCHLEVEL = {b}\nSUBLEVEL = {c}\n'.encode())
        if path == self.profile['recipe_workflow']:
            return file_document(path, self.recipe)
        raise AssertionError((repository, suffix))


class OplusSourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.oplus-source-sync-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.profile = copy.deepcopy(json.loads((ROOT / update.PROFILE_PATH).read_text(encoding='utf-8'))['profiles'][0])
        self.before = {'schema_version': 1, 'profiles': [self.profile]}
        self.raw = (ROOT / update.recipe_path(self.profile)).read_bytes()
        path = self.root / update.PROFILE_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.before), encoding='utf-8')
        path = self.root / update.recipe_path(self.profile)
        path.parent.mkdir(parents=True)
        path.write_bytes(self.raw)
        self.reader = FakeReader(self.profile, self.raw)

    def snapshot(self):
        return update.fetch_snapshot(self.root, self.reader)

    def maps(self, after=None):
        return {update.recipe_path(self.profile): self.raw}, {update.recipe_path(self.profile): self.raw if after is None else after}

    def test_check_covers_kernel_recipe_and_new_version_without_executing_downloaded_yaml(self):
        snapshot = self.snapshot()
        self.assertEqual(self.reader.head, snapshot['profiles'][0]['source_ref'])
        self.assertEqual(self.reader.recipe_head, snapshot['profiles'][0]['recipe_ref'])
        self.assertEqual('unchanged', snapshot['profiles'][0]['recipe_status'])
        self.assertEqual([{'repository': self.profile['recipe_repository'], 'ref': self.reader.recipe_head,
                          'path': '.github/workflows/fastbuild_6.1.999.yml', 'kernel': '6.1.999', 'family': '骁龙'}],
                         snapshot['new_recipes'])
        self.assertFalse((self.root / update.CANDIDATE_PATH).exists())
        self.assertEqual(self.before, json.loads((self.root / update.PROFILE_PATH).read_text(encoding='utf-8')))

    def test_apply_binds_every_byte_to_frozen_upstream_and_updates_only_source_metadata(self):
        snapshot = self.snapshot()
        after = update.apply_snapshot(self.root, snapshot, self.reader)
        self.assertEqual(self.reader.head, after['profiles'][0]['kernel_ref'])
        self.assertEqual(self.reader.recipe_head, after['profiles'][0]['recipe_ref'])
        old, new = self.maps()
        update.validate_update(self.before, after, None, snapshot, old, new)
        self.assertEqual(self.raw, (self.root / update.recipe_path(self.profile)).read_bytes())
        self.assertEqual(snapshot, json.loads((self.root / update.CANDIDATE_PATH).read_text(encoding='utf-8')))
        for before_key in set(self.profile) - {'kernel_ref', 'source_committed_at', 'recipe_ref'}:
            self.assertEqual(self.profile[before_key], after['profiles'][0][before_key])

    def test_newer_branch_version_stays_candidate_and_never_relabels_existing_binary(self):
        self.reader.version = '6.1.999'
        snapshot = self.snapshot()
        after = update.planned_profiles(self.before, snapshot)
        for key in ('id', 'kernel', 'kernel_ref', 'source_committed_at'):
            self.assertEqual(self.profile[key], after['profiles'][0][key])
        self.assertEqual('6.1.999', snapshot['profiles'][0]['source_version'])

    def test_changed_executable_recipe_is_reported_pending_and_never_activated(self):
        self.reader.recipe = self.raw.replace(b'ANDROID_VERSION:', b'CHANGED_ANDROID_VERSION:', 1)
        snapshot = self.snapshot()
        self.assertEqual('pending-adaptation', snapshot['profiles'][0]['recipe_status'])
        after = update.apply_snapshot(self.root, snapshot, self.reader)
        self.assertEqual(self.profile['recipe_ref'], after['profiles'][0]['recipe_ref'])
        self.assertEqual(self.profile['recipe_sha256'], after['profiles'][0]['recipe_sha256'])
        self.assertEqual(self.raw, (self.root / update.recipe_path(self.profile)).read_bytes())

    def test_menu_only_recipe_change_can_record_new_immutable_recipe_without_changing_execution(self):
        self.reader.recipe = b'# New upstream menu comment\n' + self.raw
        snapshot = self.snapshot()
        self.assertEqual('execution-equivalent', snapshot['profiles'][0]['recipe_status'])
        after = update.apply_snapshot(self.root, snapshot, self.reader)
        self.assertEqual(self.reader.recipe_head, after['profiles'][0]['recipe_ref'])
        self.assertEqual(self.reader.recipe, (self.root / update.recipe_path(self.profile)).read_bytes())
        self.assertEqual(update.execution_identity(self.raw), update.execution_identity(self.reader.recipe))

    def test_forged_recipe_status_cannot_smuggle_changed_commands_through_apply_or_pr_validation(self):
        self.reader.recipe = self.raw.replace(b'ANDROID_VERSION:', b'CHANGED_ANDROID_VERSION:', 1)
        snapshot = self.snapshot()
        snapshot['profiles'][0]['recipe_status'] = 'execution-equivalent'
        with self.assertRaises(update.UpdateError): update.apply_snapshot(self.root, snapshot, self.reader)
        after = update.planned_profiles(self.before, snapshot)
        old, new = self.maps(self.reader.recipe)
        with self.assertRaises(update.UpdateError): update.validate_update(self.before, after, None, snapshot, old, new)
        self.assertEqual(self.before, json.loads((self.root / update.PROFILE_PATH).read_text(encoding='utf-8')))

    def test_changed_remote_fixed_blob_is_rejected_before_any_local_write(self):
        snapshot = self.snapshot()
        self.reader.recipe += b'\n# not the same immutable file\n'
        with self.assertRaisesRegex(update.UpdateError, 'immutable upstream bytes'):
            update.apply_snapshot(self.root, snapshot, self.reader)
        self.assertFalse((self.root / update.CANDIDATE_PATH).exists())

    def test_snapshot_cannot_add_remove_duplicate_or_reidentify_current_targets(self):
        snapshot = self.snapshot()
        variants = []
        value = copy.deepcopy(snapshot); value['profiles'] = []; variants.append(value)
        value = copy.deepcopy(snapshot); value['profiles'] *= 2; variants.append(value)
        value = copy.deepcopy(snapshot); value['profiles'][0]['id'] = 'mt6993-6.12.23'; variants.append(value)
        value = copy.deepcopy(snapshot); value['new_recipes'][0]['repository'] = 'attacker/source'; variants.append(value)
        value = copy.deepcopy(snapshot); value['new_recipes'][0]['path'] = '../../arbitrary.yml'; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(update.UpdateError):
                update.validate_snapshot(value, self.before)

    def test_pr_transition_cannot_change_catalog_or_unrelated_local_adaptations(self):
        snapshot = self.snapshot()
        after = update.planned_profiles(self.before, snapshot)
        for key, value in (('kernel_repo', 'cctv18/android_kernel_oneplus_mt6991'), ('kernel', '6.1.999'),
                           ('compatibility', 'claims every device boots'), ('kernel_branch', 'another/branch')):
            changed = copy.deepcopy(after); changed['profiles'][0][key] = value
            with self.subTest(key=key), self.assertRaises(update.UpdateError):
                update.validate_update(self.before, changed, None, snapshot, *self.maps())

    def test_allowed_write_paths_are_exact_existing_recipe_paths_and_receipt_delta_is_idempotent(self):
        self.assertEqual(frozenset((*update.FILES, update.recipe_path(self.profile))), update.allowed_paths(self.before))
        snapshot = self.snapshot()
        self.assertEqual({'updated': [], 'new_recipes': []}, update.receipt_delta(snapshot, snapshot))
        self.assertEqual(1, len(update.receipt_delta(None, snapshot)['updated']))
        self.assertEqual(snapshot, update.normalize_receipt(snapshot))

    def test_post_sync_recipe_classification_does_not_create_another_upstream_update(self):
        before = self.snapshot()
        before['profiles'][0]['recipe_status'] = 'execution-equivalent'
        after = copy.deepcopy(before); after['profiles'][0]['recipe_status'] = 'unchanged'
        self.assertEqual(update.head_state(before), update.head_state(after))
        self.assertEqual({'updated': [], 'new_recipes': []}, update.receipt_delta(before, after))

    def test_network_reader_rejects_unreviewed_destinations_and_paths_before_opening(self):
        opener = mock.Mock()
        reader = update.Reader(token='private-token', opener=opener)
        for repo, path in (('attacker/clone', ''), (self.profile['kernel_repo'], 'contents/passwords?ref=' + 'a' * 40),
                           (self.profile['recipe_repository'], 'contents/.github/workflows/arbitrary.yml?ref=' + 'b' * 40)):
            with self.subTest(repo=repo, path=path), self.assertRaises(update.UpdateError): reader.get(repo, path)
        opener.open.assert_not_called()

    def test_remote_verification_rejects_fabricated_new_recipe_and_mismatched_frozen_head(self):
        snapshot = self.snapshot()
        snapshot['new_recipes'][0]['path'] = '.github/workflows/fastbuild_6.1.998.yml'
        snapshot['new_recipes'][0]['kernel'] = '6.1.998'
        with self.assertRaisesRegex(update.UpdateError, 'does not exist'):
            update.verify_remote_snapshot(snapshot, self.before, self.reader)
        snapshot['new_recipes'][0]['ref'] = 'f' * 40
        with self.assertRaises(update.UpdateError): update.normalize_receipt(snapshot)


if __name__ == '__main__':
    unittest.main()
