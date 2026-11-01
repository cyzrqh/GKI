"""Real cache archive round trips, failed transfers and isolated workflow wiring."""
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import unittest
from unittest import mock
from urllib.error import HTTPError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import persistent_kernel_cache as cache
import generate_workflow as generator
import oplus_workflow

KEY = 'gki-ccache-ecs-v1-Linux-X64-toolhash-android12-5.10-236-2025-05-SukiSU'
PRODUCER = 'cyzrqh/GKI'


class MemoryApi:
    def __init__(self):
        self.release = None
        self.blobs = {}
        self.operations = []
        self.fail_upload = False
        self.fail_downloads = 0

    def json(self, path, *, method='GET', document=None, missing=False):
        self.operations.append((method, path))
        if method == 'GET':
            if path.startswith('/releases?'):
                return [] if self.release is None else [copy.deepcopy(self.release)]
            if self.release is not None and self.release['draft']:
                return None  # The real by-tag API excludes drafts.
            return copy.deepcopy(self.release)
        if method == 'POST':
            self.release = {**document, 'id': 123, 'assets': []}
        elif method == 'PATCH':
            self.release.update(document)
        elif method == 'DELETE':
            identity = int(path.rsplit('/', 1)[1])
            self.release['assets'] = [a for a in self.release['assets'] if a['id'] != identity]
            self.blobs.pop(identity, None)
        return copy.deepcopy(self.release)

    def stream(self, path, file, *, upload=False):
        if upload:
            if self.fail_upload:
                raise cache.TransferError('Simulated interrupted upload')
            from urllib.parse import parse_qs, urlsplit
            identity = len(self.blobs) + 1000
            raw = file.read_bytes()
            self.blobs[identity] = raw
            self.release['assets'].append({'id': identity, 'name': parse_qs(urlsplit(path).query)['name'][0],
                                           'size': len(raw), 'state': 'uploaded'})
        else:
            if self.fail_downloads:
                self.fail_downloads -= 1
                file.write_bytes(b'truncated download')
                raise cache.TransferError('Simulated interrupted download')
            file.write_bytes(self.blobs[int(path.rsplit('/', 1)[1])])


class PersistentCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT.parent / '.gki-upstream-cache' / ('persistent-test-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.source = self.root / 'objects'
        self.source.mkdir()
        (self.source / 'a/0').mkdir(parents=True)
        (self.source / 'a/0/sampleR').write_bytes(b'compiled object bytes\0' * 100)
        (self.source / 'ecs_cache/deps').mkdir(parents=True)
        (self.source / 'ecs_cache/deps/source.txt').write_bytes(b'CONFIG_KSU\n')
        (self.source / 'ccache.conf').write_bytes(b'prefix_command = unsafe-restored-command\n')
        (self.source / 'stats').write_bytes(b'old run counters')
        self.destination = self.root / 'restored'
        self.api = MemoryApi()

    def save(self):
        return cache.save(self.api, self.source, KEY, 'ccache', PRODUCER)

    def restore(self):
        return cache.restore(self.api, self.destination, KEY, 'ccache', PRODUCER)

    def test_shared_manager_restore_keeps_kernel_key_and_never_changes_saved_namespace(self):
        class ScopedApi(MemoryApi):
            def json(self, path, *, method='GET', document=None, missing=False):
                if (method == 'GET' and path.startswith('/releases/tags/')
                        and self.release and path.rsplit('/', 1)[-1] != self.release['tag_name']):
                    self.operations.append((method, path))
                    return None
                return super().json(path, method=method, document=document, missing=missing)
        api = ScopedApi()
        self.assertTrue(cache.save(api, self.source, KEY, 'ccache', PRODUCER))
        key = KEY.removesuffix('SukiSU') + 'KowSU'
        before = len(api.operations)
        restored, variant = cache.restore_shared(api, self.destination, key, 'ccache', PRODUCER)
        self.assertTrue(restored)
        self.assertEqual('SukiSU', variant)
        self.assertEqual((self.source / 'a/0/sampleR').read_bytes(), (self.destination / 'a/0/sampleR').read_bytes())
        self.assertTrue(all(method == 'GET' for method, _ in api.operations[before:]))
        miss = key.replace('5.10-236', '5.10-237')
        self.assertEqual((False, None), cache.restore_shared(api, self.root / 'other-kernel', miss, 'ccache', PRODUCER))
        self.assertFalse((self.root / 'other-kernel').exists())

    def test_cross_manager_thinlto_or_unknown_variant_is_never_restored(self):
        for kind, key in (('thinlto', KEY), ('bazel', KEY), ('ccache', KEY + '-unknown')):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                cache.restore_shared(self.api, self.destination, key, kind, PRODUCER)
        self.assertEqual([], self.api.operations)

    def test_real_archive_roundtrip_keeps_objects_and_ecs_but_never_cache_commands_or_stats(self):
        self.assertTrue(self.save())
        self.assertTrue(self.restore())
        self.assertEqual((self.source / 'a/0/sampleR').read_bytes(), (self.destination / 'a/0/sampleR').read_bytes())
        self.assertEqual(b'CONFIG_KSU\n', (self.destination / 'ecs_cache/deps/source.txt').read_bytes())
        self.assertFalse((self.destination / 'ccache.conf').exists())
        self.assertFalse((self.destination / 'stats').exists())
        self.assertFalse(self.api.release['draft'])
        self.assertTrue(self.api.release['prerelease'])

    def test_unchanged_cache_does_not_upload_a_second_copy(self):
        self.save()
        original = copy.deepcopy(self.api.release)
        (self.source / 'stats').write_bytes(b'new counters must not change the snapshot')
        self.save()
        self.assertEqual(original, self.api.release)
        self.assertEqual(1, len(self.api.blobs))

    def test_repeated_build_replaces_only_previous_owned_objects_and_retains_other_files(self):
        self.save()
        previous_id = self.api.release['assets'][0]['id']
        self.api.release['assets'].append({'id': 444, 'name': 'keep-other-data.json', 'size': 2, 'state': 'uploaded'})
        (self.source / 'a/0/newR').write_bytes(b'another real object')
        self.save()
        self.assertNotIn(previous_id, [a['id'] for a in self.api.release['assets']])
        self.assertIn(444, [a['id'] for a in self.api.release['assets']])
        self.assertTrue(self.restore())
        self.assertEqual(b'another real object', (self.destination / 'a/0/newR').read_bytes())

    def test_interrupted_first_upload_can_be_recovered_without_abandoning_hidden_draft(self):
        self.api.fail_upload = True
        with self.assertRaises(cache.TransferError):
            self.save()
        self.assertTrue(self.api.release['draft'])
        self.api.fail_upload = False
        self.assertTrue(self.save())
        self.assertTrue(self.restore())
        self.assertEqual(1, sum(method == 'POST' for method, _ in self.api.operations))

    def test_pending_draft_lookup_follows_release_pagination(self):
        tag = cache.key_identity(KEY, 'ccache')
        api = mock.Mock()
        api.json.side_effect = [None, [{'tag_name': 'other-' + str(index)} for index in range(100)],
                                [{'tag_name': tag, 'draft': True, 'id': 77}]]
        self.assertEqual(77, cache.find_for_save(api, tag)['id'])
        self.assertEqual('/releases?per_page=100&page=2', api.json.call_args.args[0])

    def test_failed_new_upload_leaves_previous_published_cache_intact(self):
        self.save()
        original = copy.deepcopy(self.api.release)
        (self.source / 'a/0/newR').write_bytes(b'new object')
        self.api.fail_upload = True
        with self.assertRaises(cache.TransferError):
            self.save()
        self.assertEqual(original, self.api.release)
        self.assertTrue(self.restore())

    def test_download_retries_discard_partial_files_before_restore(self):
        self.save()
        self.api.fail_downloads = 2
        with mock.patch.object(cache.time, 'sleep') as sleep:
            self.assertTrue(self.restore())
        self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)
        self.assertEqual([], list(self.root.glob('.gki-object-*')))

    def test_corrupt_archive_or_foreign_cache_is_never_published(self):
        self.save()
        identity = self.api.release['assets'][0]['id']
        self.api.blobs[identity] = b'tampered'
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.restore()
        self.assertEqual([], list(self.destination.iterdir()))
        for field, value in (('key', KEY + '-other'), ('producer', 'someone/else'), ('kind', 'bazel')):
            changed = copy.deepcopy(self.api.release)
            body = json.loads(changed['body'][len(cache.MARKER):])
            body[field] = value
            changed['body'] = cache.MARKER + json.dumps(body)
            with self.subTest(field=field), self.assertRaises(ValueError):
                cache.control(changed, KEY, 'ccache', PRODUCER)

    def test_archive_traversal_symlink_duplicate_and_config_are_rejected_before_any_publish(self):
        self.destination.mkdir()
        (self.destination / 'keep').write_bytes(b'original')
        for kind, name in (('regular', '../outside'), ('symlink', 'a/unsafe'),
                           ('duplicate', 'a/file'), ('regular', 'ccache.conf')):
            output = self.root / ('bad-' + kind + '-' + uuid.uuid4().hex + '.tar.gz')
            with tarfile.open(output, 'w:gz') as tar:
                for _ in range(2 if kind == 'duplicate' else 1):
                    item = tarfile.TarInfo(name)
                    item.size = 1
                    if kind == 'symlink':
                        item.type, item.linkname = tarfile.SYMTYPE, '/outside'
                    tar.addfile(item, io.BytesIO(b'x'))
            expected = {'size': output.stat().st_size, 'sha256': cache.digest(output), 'expanded': 2, 'files': 2}
            with self.subTest(kind=kind, name=name), self.assertRaises(ValueError):
                cache.unpack(output, self.destination, expected)
            self.assertEqual([self.destination / 'keep'], list(self.destination.iterdir()))
            self.assertEqual(b'original', (self.destination / 'keep').read_bytes())

    def test_owner_cache_repository_is_separate_from_real_kernel_releases(self):
        cache.validate_repo('cyzrqh/gki-cache', PRODUCER)
        for repository in ('cyzrqh/GKI', 'other/gki-cache', 'cyzrqh/../GKI', ''):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                cache.validate_repo(repository, PRODUCER)
        self.assertNotEqual(cache.key_identity(KEY, 'ccache'), cache.key_identity(KEY, 'bazel'))

    def test_download_redirect_never_forwards_repository_credential(self):
        requests = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def __init__(self): self.stream = io.BytesIO(b'object-data')
            def read(self, size): return self.stream.read(size)
        def open_request(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise HTTPError(request.full_url, 302, 'redirect',
                                {'Location': 'https://release-assets.githubusercontent.com/data?signature=x'}, None)
            return Response()
        api = cache.CacheGitHub('cyzrqh/gki-cache', 'test-only-credential')
        api.opener = mock.Mock()
        api.opener.open.side_effect = open_request
        path = self.root / 'download'
        api.stream('/releases/assets/1', path)
        self.assertEqual(b'object-data', path.read_bytes())
        self.assertEqual('Bearer test-only-credential', requests[0].get_header('Authorization'))
        self.assertIsNone(requests[1].get_header('Authorization'))

    def test_upload_streams_the_file_with_an_explicit_length(self):
        path = self.root / 'upload'
        path.write_bytes(b'cache-object' * 1000)
        requests = []
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        def open_request(request, timeout):
            requests.append(request)
            self.assertTrue(hasattr(request.data, 'read'))
            self.assertEqual(str(path.stat().st_size), request.get_header('Content-length'))
            self.assertEqual(path.read_bytes(), request.data.read())
            return Response(b'{}')
        api = cache.CacheGitHub('cyzrqh/gki-cache', 'test-only-credential')
        api.opener = mock.Mock()
        api.opener.open.side_effect = open_request
        api.stream('/releases/123/assets?name=fixture.tar.gz', path, upload=True)
        self.assertEqual('uploads.github.com', __import__('urllib.parse', fromlist=['urlsplit']).urlsplit(requests[0].full_url).hostname)

    def test_missing_permissions_are_visible_and_do_not_fail_a_valid_kernel_build(self):
        output, summary = self.root / 'output', self.root / 'summary'
        with mock.patch.dict(os.environ, {'CACHE_REPOSITORY': 'cyzrqh/gki-cache',
                'GITHUB_REPOSITORY': PRODUCER, 'CACHE_TOKEN': '',
                'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)}):
            self.assertEqual(0, cache.main(['restore', '--kind', 'ccache', '--key', KEY,
                                          '--directory', str(self.destination)]))
        self.assertIn('restored=false', output.read_text())
        self.assertIn('unavailable', summary.read_text())


class PersistentWorkflowTests(unittest.TestCase):
    def test_gki_and_oplus_use_dedicated_cache_before_actions_and_keep_fallback_after_save(self):
        base = generator.create_workflow(ROOT)
        oplus = oplus_workflow.create_workflow(ROOT, base)
        for worker in (base['jobs']['build-kernel'], oplus['jobs']['build-oplus-kernel']):
            steps = worker['steps']
            self.assertEqual('1536M', worker['env']['CCACHE_MAXSIZE'])
            for kind in ('ccache', 'bazel') if worker is base['jobs']['build-kernel'] else ('ccache',):
                restore = next(step for step in steps if step.get('id') == 'persistent-' + kind + '-restore')
                save = next(step for step in steps if step.get('id') == 'persistent-' + kind + '-save')
                self.assertEqual(restore['env']['OBJECT_CACHE_KEY'], save['env']['OBJECT_CACHE_KEY'])
                self.assertIn('${{ inputs.kernelsu_variant }}', restore['env']['OBJECT_CACHE_KEY'])
                for step in (restore, save):
                    self.assertEqual('${{ github.repository_owner }}/gki-cache', step['env']['CACHE_REPOSITORY'])
                    self.assertIs(step['continue-on-error'], True)
                    self.assertNotIn('github.run_id', step['env']['OBJECT_CACHE_KEY'])
                local = next(step for step in steps if step.get('uses') == 'actions/cache/restore@v5'
                             and 'steps.persistent-' + kind + '-restore.outputs.restored' in step.get('if', ''))
                self.assertLess(steps.index(restore), steps.index(local))
                self.assertIn("!= 'true'", local['if'])
                fallback = next(step for step in steps if step.get('uses') == 'actions/cache/save@v5'
                                and 'steps.persistent-' + kind + '-save.outputs.saved' in step.get('if', ''))
                self.assertLess(steps.index(save), steps.index(fallback))


if __name__ == '__main__':
    unittest.main()
