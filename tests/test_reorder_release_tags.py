"""Stateful release compaction preserves exact commits, IDs and asset inventories."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import reorder_release_tags as reorder
from release_tags import append_to_notes
import workflow_jobs
import generate_workflow
import oplus_workflow

REPO = 'cyzrqh/GKI'


def release(index, tag, *, asset_count=1):
    context = {'repository': REPO, 'run_id': 100 + index, 'run_attempt': 1,
               'head_sha': f'{index + 1:040x}'}
    return {'id': 10 + index, 'tag_name': tag, 'name': 'Preserved manager ' + str(index),
            'target_commitish': context['head_sha'], 'draft': False, 'prerelease': False,
            'immutable': False, 'published_at': f'2026-10-{20 + index:02d}T12:00:00Z',
            'body': append_to_notes('Original configuration notes', context),
            'assets': [{'id': 1000 + index * 1000 + number, 'name': f'kernel-{number}.zip',
                        'size': 100 + number, 'state': 'uploaded', 'digest': 'sha256:' + 'f' * 64,
                        'label': None, 'content_type': 'application/zip', 'download_count': 0}
                       for number in range(asset_count)]}


class FakeAPI:
    repository = REPO

    def __init__(self, releases=None):
        self.releases = copy.deepcopy(releases if releases is not None else [release(0, 'r1.0'), release(1, 'r1.2'), release(2, 'r1.4')])
        self.refs = {item['tag_name']: item['target_commitish'] for item in self.releases}
        for tag, sha in {'r1.1': 'a' * 40, 'r1.3': 'b' * 40, 'r1.5': 'c' * 40}.items():
            self.refs.setdefault(tag, sha)
        self.latest = self.releases[-1]['id'] if self.releases else None
        self.writes = []
        self.publisher = False
        self.fail_release = None
        self.download_counts = False

    def request(self, path, *, method='GET', data=None, missing=False):
        if method != 'GET':
            self.writes.append((method, path, copy.deepcopy(data)))
        if path.startswith('/actions/workflows/'):
            return {'workflow_runs': [{'id': 77}] if self.publisher else []}
        if path.startswith('/actions/runs/'):
            return {'jobs': [{'name': next(iter(reorder.PUBLISH_JOBS)), 'status': 'in_progress'}]}
        if path == '/git/matching-refs/tags/r1.':
            return [{'ref': 'refs/tags/' + tag, 'object': {'sha': sha, 'type': 'commit'}} for tag, sha in self.refs.items()]
        if path == '/releases/latest':
            return copy.deepcopy(next((item for item in self.releases if item['id'] == self.latest), None))
        if path.startswith('/releases?'):
            if self.download_counts:
                for item in self.releases:
                    for asset in item['assets']: asset['download_count'] += 1
            page = int(path.rsplit('=', 1)[1])
            return copy.deepcopy(self.releases[(page - 1) * 20:page * 20])
        if '/assets?' in path:
            identity = int(path.split('/')[2])
            page = int(path.rsplit('=', 1)[1])
            assets = next(item['assets'] for item in self.releases if item['id'] == identity)
            return copy.deepcopy(assets[(page - 1) * 100:page * 100])
        if path.startswith('/releases/'):
            identity = int(path.rsplit('/', 1)[1])
            item = next(item for item in self.releases if item['id'] == identity)
            if method == 'PATCH':
                if identity == self.fail_release:
                    raise ValueError('Simulated interrupted retag')
                if data == {'make_latest': 'true'}:
                    self.latest = identity
                else:
                    item.update(data)
            return copy.deepcopy(item)
        if path == '/git/refs':
            tag = data['ref'].removeprefix('refs/tags/')
            if tag in self.refs: raise ValueError('Duplicate ref')
            self.refs[tag] = data['sha']
        else:
            tag = path.rsplit('/', 1)[1]
            if method == 'PATCH': self.refs[tag] = data['sha']
            if method == 'DELETE': self.refs.pop(tag)
        tag = path.rsplit('/', 1)[1] if data is None or 'ref' not in data else data['ref'].removeprefix('refs/tags/')
        if tag not in self.refs: return None
        return {'ref': 'refs/tags/' + tag, 'object': {'sha': self.refs[tag], 'type': 'commit'}}


class ReorderTests(unittest.TestCase):
    def test_preview_has_no_mutations_and_reports_the_exact_mapping(self):
        api = FakeAPI()
        result = reorder.reorder(api)
        self.assertEqual([('r1.0', 'r1.0'), ('r1.2', 'r1.1'), ('r1.4', 'r1.2')],
                         [(row['old'], row['new']) for row in result['mapping']])
        self.assertEqual([], api.writes)

    def test_compacts_and_prunes_orphans_without_recreating_releases_or_assets(self):
        api = FakeAPI([release(0, 'r1.0', asset_count=101), release(1, 'r1.2'), release(2, 'r1.4')])
        before = copy.deepcopy(api.releases)
        result = reorder.reorder(api, execute=True)
        self.assertEqual('completed', result['state'])
        self.assertEqual({'r1.0', 'r1.1', 'r1.2'}, set(api.refs))
        for index, (original, actual) in enumerate(zip(before, api.releases)):
            self.assertEqual({**original, 'tag_name': 'r1.' + str(index)}, actual)
            self.assertEqual(original['target_commitish'], api.refs[actual['tag_name']])
        self.assertEqual(12, api.latest)
        self.assertFalse(any('/assets' in path or method == 'DELETE' and path.startswith('/releases/')
                             for method, path, _ in api.writes))
        api.writes.clear()
        self.assertEqual('completed', reorder.reorder(api, execute=True)['state'])
        self.assertEqual([], api.writes)

    def test_interrupted_tag_attachment_can_resume_without_replacing_any_asset(self):
        api = FakeAPI()
        original_assets = [copy.deepcopy(item['assets']) for item in api.releases]
        api.fail_release = 11
        with self.assertRaises(ValueError): reorder.reorder(api, execute=True)
        self.assertEqual('r1.2', api.releases[1]['tag_name'])
        api.fail_release = None
        self.assertEqual('completed', reorder.reorder(api, execute=True)['state'])
        self.assertEqual(original_assets, [item['assets'] for item in api.releases])

    def test_active_publication_or_numbered_draft_defers_before_any_write(self):
        for draft in (False, True):
            api = FakeAPI()
            api.publisher = not draft
            api.releases[0]['draft'] = draft
            with self.subTest(draft=draft), self.assertRaises(reorder.Deferred):
                reorder.reorder(api, execute=True)
            self.assertEqual([], api.writes)

    def test_foreign_provenance_or_immutable_release_rejects_the_whole_plan(self):
        for field, value in (('body', 'No exact producer marker'), ('immutable', True), ('target_commitish', 'e' * 40)):
            api = FakeAPI()
            api.releases[1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): reorder.reorder(api, execute=True)
            self.assertEqual([], api.writes)

    def test_cyclic_user_labels_are_not_overwritten(self):
        api = FakeAPI([release(0, 'r1.1'), release(1, 'r1.0')])
        with self.assertRaisesRegex(ValueError, 'cyclic'): reorder.reorder(api, execute=True)
        self.assertEqual([], api.writes)

    def test_download_counters_do_not_abort_metadata_preservation(self):
        api = FakeAPI()
        api.download_counts = True
        self.assertEqual('completed', reorder.reorder(api, execute=True)['state'])

    def test_no_releases_cleans_only_numbered_orphan_refs(self):
        api = FakeAPI([])
        self.assertEqual('completed', reorder.reorder(api, execute=True)['state'])
        self.assertEqual({}, api.refs)

    def test_http_scope_never_allows_release_asset_or_workflow_run_deletion(self):
        transport = mock.Mock()
        api = reorder.GitHub(REPO, transport=transport)
        for path, method, data in (('/releases/12', 'DELETE', None),
                ('/releases/assets/999', 'DELETE', None),
                ('/git/refs/tags/main', 'DELETE', None),
                ('/actions/runs/77', 'DELETE', None),
                ('/releases/12', 'PATCH', {'tag_name': 'r1.0', 'body': 'replacement'})):
            with self.subTest(path=path), self.assertRaises(ValueError):
                api.request(path, method=method, data=data)
        transport.json.assert_not_called()

    def test_workflows_share_a_non_cancelling_publication_queue_and_no_poll_schedule(self):
        base = generate_workflow.create_workflow(ROOT)
        oplus = oplus_workflow.create_workflow(ROOT, base)
        maintenance = workflow_jobs.reorder_release_workflow()
        self.assertEqual({'release', 'workflow_dispatch'}, set(maintenance['on']))
        self.assertEqual(['deleted'], maintenance['on']['release']['types'])
        self.assertEqual(base['jobs']['publish-release']['concurrency'], oplus['jobs']['verify-oplus-assets']['concurrency'])
        self.assertEqual(base['jobs']['publish-release']['concurrency'], maintenance['jobs']['reorder']['concurrency'])
        self.assertIs(maintenance['jobs']['reorder']['concurrency']['cancel-in-progress'], False)
        self.assertEqual('max', maintenance['jobs']['reorder']['concurrency']['queue'])
        self.assertNotIn('build-kernel', maintenance['jobs'])
        self.assertEqual('${{ secrets.UPSTREAM_SYNC_TOKEN || github.token }}',
                         maintenance['jobs']['reorder']['steps'][1]['env']['GH_TOKEN'])
        fallback = workflow_jobs.updater_workflow()['jobs']['reorder-old-release-tags']
        self.assertEqual("github.event_name == 'schedule'", fallback['if'])
        self.assertEqual(maintenance['jobs']['reorder']['concurrency'], fallback['concurrency'])
