from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from check_official_updates import TARGETS
from manager_prebuild import CHANNELS, LATEST, STABLE, current_channels, plan, snapshot_channels, validate_state
from manager_prebuild_workflow import STATE_KEY, WORKER, add_updater_call, create_workflow
from manager_variants import MANAGERS
from resolve_build_sources import NoStableRelease, SourceResolutionError
from workflow_jobs import updater_workflow


def ref(number: int) -> str:
    return f'{number:040x}'


def source_snapshot() -> dict:
    document = {'schema_version': 1, 'susfs': {target: ref(50) for target in TARGETS}}
    for number, variant in enumerate(MANAGERS, start=1):
        document[variant.lower()] = {'branch': 'main', 'head': ref(number),
                                     'release': {'tag': 'v4.0', 'commit': ref(number + 10)}}
    document['sukisu']['release']['newer_than_baseline'] = True
    document['sukisu']['development'] = {'branch': 'dev', 'head': ref(31)}
    document['bakasu'].update(release=None, prerelease={'tag': 'rc1', 'commit': ref(32)})
    document['finasu']['release']['commit'] = document['finasu']['head']
    return document


class ManagerPrebuildTest(unittest.TestCase):
    def snapshot(self, document=None):
        with patch('manager_prebuild.compare_sukisu_channels', return_value=(ref(31), 'dev')) as compare:
            channels = snapshot_channels(source_snapshot() if document is None else document)
        compare.assert_called_once_with('SukiSU-Ultra/SukiSU-Ultra', 'main', ref(1), ref(31))
        return channels

    def test_backfill_uses_frozen_official_tiers_and_deduplicates_same_sha(self):
        channels = self.snapshot()
        matrix, state = plan(channels, {})
        self.assertEqual(len(matrix['include']), 10)
        by_manager = {}
        for row in matrix['include']:
            by_manager.setdefault(row['variant'], []).append(row)
        self.assertEqual([(row['tier'], row['source_branch']) for row in by_manager['SukiSU']],
                         [(STABLE, 'v4.0'), (LATEST, 'dev')])
        self.assertEqual([row['tier'] for row in by_manager['BakaSU']], [LATEST])
        self.assertEqual(by_manager['FinaSU'][0]['channels'], '正式版 / 最新版')
        self.assertEqual(by_manager['FinaSU'][0]['tier'], STABLE)
        self.assertEqual(validate_state(json.loads(json.dumps(state)))['SukiSU'][LATEST], (ref(31), 'dev'))

    def test_automatic_only_builds_changed_source_independent_of_susfs_and_prerelease(self):
        _, state = plan(self.snapshot(), {})
        document = source_snapshot()
        document['susfs'][TARGETS[0]] = ref(77)
        document['bakasu']['prerelease']['commit'] = ref(81)
        document['kowsu']['head'] = ref(82)
        matrix, _ = plan(self.snapshot(document), validate_state(state))
        self.assertEqual([(row['variant'], row['tier'], row['reference']) for row in matrix['include']],
                         [('KowSU', LATEST, ref(82))])

    def test_new_formal_channel_is_built_even_if_its_commit_is_already_latest(self):
        channels = self.snapshot()
        _, state = plan(channels, {})
        channels['BakaSU'][STABLE] = (channels['BakaSU'][LATEST][0], 'v4.0')
        matrix, _ = plan(channels, validate_state(state))
        self.assertEqual(matrix['include'], [{'variant': 'BakaSU', 'tier': STABLE, 'reference': ref(2),
                                             'source_branch': 'v4.0', 'channels': '正式版'}])

    def test_changed_formal_tag_on_same_commit_is_one_new_build(self):
        channels = self.snapshot()
        _, state = plan(channels, {})
        channels['FinaSU'][STABLE] = (ref(6), 'v4.0-r2')
        matrix, _ = plan(channels, validate_state(state))
        self.assertEqual(matrix['include'], [{'variant': 'FinaSU', 'tier': STABLE, 'reference': ref(6),
                                             'source_branch': 'v4.0-r2', 'channels': '正式版'}])

    def test_removed_channel_has_no_build_but_a_reintroduced_release_is_selected(self):
        channels = self.snapshot()
        _, before = plan(channels, {})
        stable = channels['FinaSU'].pop(STABLE)
        matrix, observed = plan(channels, validate_state(before))
        self.assertEqual(matrix, {'include': []})
        self.assertNotEqual(before, observed)
        channels['FinaSU'][STABLE] = stable
        matrix, _ = plan(channels, validate_state(observed))
        self.assertEqual([row['variant'] for row in matrix['include']], ['FinaSU'])

    def test_backfill_ignores_only_confirmed_missing_stable_release(self):
        def resolve(root, variant, tier):
            if variant == 'BakaSU' and tier == STABLE:
                raise NoStableRelease('no formal release')
            return {'ksu_ref': ref(MANAGERS.index(variant) + (10 if tier == STABLE else 1)),
                    'ksu_source_branch': 'main' if tier == LATEST else 'v4.0'}

        with patch('manager_prebuild.resolve_sources', side_effect=resolve) as lookup:
            channels = current_channels(ROOT)
        self.assertEqual(lookup.call_count, len(MANAGERS) * len(CHANNELS))
        self.assertNotIn(STABLE, channels['BakaSU'])
        with patch('manager_prebuild.resolve_sources', side_effect=SourceResolutionError('lookup failed')):
            with self.assertRaises(SourceResolutionError):
                current_channels(ROOT)

    def test_rejects_incomplete_snapshot_and_invalid_observation_state(self):
        document = source_snapshot()
        del document['finasu']
        with self.assertRaises(ValueError):
            snapshot_channels(document)
        _, state = plan(self.snapshot(), {})
        state['channels']['BakaSU'][LATEST]['reference'] = 'main'
        with self.assertRaises(ValueError):
            validate_state(state)

    def test_workflows_use_shared_worker_only_for_nonempty_matrix(self):
        original = updater_workflow()
        workflow = add_updater_call(updater_workflow())
        self.assertEqual(workflow['on'], original['on'])
        auto = workflow['jobs']['prebuild-managers']
        manual = create_workflow()['jobs']['prebuild-managers']
        self.assertEqual(auto['uses'], WORKER)
        self.assertEqual(auto['with'], manual['with'])
        self.assertEqual(set(auto['with']), {'variant', 'tier', 'reference', 'source_branch'})
        self.assertIn("manager_prebuild_pending == 'true'", auto['if'])
        recorder = workflow['jobs']['record-manager-prebuild']
        self.assertIn("prebuild-managers.result == 'success'", recorder['if'])
        self.assertIn("prebuild-managers.result == 'skipped'", recorder['if'])
        self.assertIn("manager_prebuild_state_changed == 'true'", recorder['if'])
        self.assertEqual(recorder['steps'][-1]['with']['key'], STATE_KEY)
        self.assertEqual(set(create_workflow()['on']), {'workflow_dispatch'})

    def test_updater_can_discover_managers_when_the_combined_official_probe_fails(self):
        original = updater_workflow()['jobs']['check-upstream']['steps']
        steps = add_updater_call(updater_workflow())['jobs']['check-upstream']['steps']
        new = [step for step in steps if step not in original]
        self.assertEqual([step for step in steps if step not in new], original)
        self.assertEqual(len(new), 2)
        for step in new:
            self.assertIn("steps.official.outcome == 'success'", step['if'])
            self.assertIn("steps.official.outcome == 'failure'", step['if'])
        discover = new[1]
        self.assertEqual(discover['env']['OFFICIAL_RESULT'], '${{ steps.official.outcome }}')
        self.assertIn('if [ "$OFFICIAL_RESULT" = success ]', discover['run'])
        self.assertIn('args+=(--candidates-json "$OFFICIAL_CANDIDATES")', discover['run'])
        self.assertIn('python3 tools/manager_prebuild.py discover "${args[@]}"', discover['run'])


if __name__ == '__main__':
    unittest.main()
