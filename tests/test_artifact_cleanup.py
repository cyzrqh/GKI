import contextlib
import copy
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import cleanup_build_artifacts as cleanup

SHA = 'a' * 40
ASSETS = [{'name': name, 'size': size, 'digest': 'sha256:' + digest * 64}
          for name, size, digest in [('SukiSU_v4.2.1_41050-release.apk', 123, 'b'),
                                    ('android12-5.10.246-2025-12-AnyKernel3.zip', 456, 'c'),
                                    ('SHA256SUMS.txt', 80, 'd')]]


class ArtifactCleanupTests(unittest.TestCase):
    def setUp(self):
        self.release = {'draft': False, 'prerelease': False, 'target_commitish': SHA, 'assets': copy.deepcopy(ASSETS)}
        self.artifacts = [{'id': i, 'name': asset['name'], 'size_in_bytes': asset['size'],
                           'digest': asset['digest'], 'expired': False} for i, asset in enumerate(ASSETS, 1)]
        self.artifacts += [{'id': 4, 'name': 'Release-Manager-abcdefg'},
                           {'id': 5, 'name': 'Manager-Identity-abcdefg'},
                           {'id': 6, 'name': 'Release-SukiSU-android12'},
                           {'id': 7, 'name': 'patch-rejects'}]
        self.deleted = []
        self.environment = patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/gki', 'GITHUB_RUN_ID': '123',
                                                  'GITHUB_SHA': SHA, 'GITHUB_RUN_ATTEMPT': '1'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def api(self, repo, resource, *, method='GET'):
        self.assertEqual(repo, 'owner/gki')
        if method == 'DELETE':
            self.deleted.append(resource)
            return None
        if resource == '/actions/runs/123':
            return {'head_sha': SHA, 'head_repository': {'full_name': 'owner/gki'}}
        if resource.startswith('/releases/tags/'):
            return self.release
        return {'total_count': len(self.artifacts), 'artifacts': self.artifacts}

    def invoke(self):
        with patch.object(cleanup, 'api', side_effect=self.api), contextlib.redirect_stdout(io.StringIO()):
            return cleanup.cleanup('123')

    def test_verified_raw_files_survive_and_only_staging_is_deleted(self):
        self.assertEqual(self.invoke(), ['Release-Manager-abcdefg', 'Manager-Identity-abcdefg', 'Release-SukiSU-android12'])
        self.assertEqual(self.deleted, ['/actions/artifacts/4', '/actions/artifacts/5', '/actions/artifacts/6'])

    def test_wrong_run_is_rejected_without_any_api_access(self):
        with patch.object(cleanup, 'api') as api, self.assertRaisesRegex(cleanup.CleanupError, 'current run'):
            cleanup.cleanup('456')
        api.assert_not_called()

    def test_missing_changed_expired_or_duplicate_raw_file_preserves_every_staging_file(self):
        original = copy.deepcopy(self.artifacts)
        for change in ('missing', 'digest', 'size', 'expired', 'duplicate'):
            self.artifacts = copy.deepcopy(original)
            if change == 'missing':
                self.artifacts.pop(1)
            elif change == 'duplicate':
                self.artifacts.append(copy.deepcopy(self.artifacts[1]))
            else:
                self.artifacts[1][{'digest': 'digest', 'size': 'size_in_bytes', 'expired': 'expired'}[change]] = True
            with self.subTest(change=change), self.assertRaises(cleanup.CleanupError):
                self.invoke()
            self.assertEqual(self.deleted, [])

    def test_unpublished_or_different_source_release_is_not_used(self):
        for field, value in (('draft', True), ('prerelease', True), ('target_commitish', 'main')):
            original = self.release[field]
            self.release[field] = value
            with self.subTest(field=field), self.assertRaises(cleanup.CleanupError):
                self.invoke()
            self.release[field] = original
        self.assertEqual(self.deleted, [])

    def test_partial_cleanup_can_resume_without_deleting_final_files(self):
        def partial(repo, resource, *, method='GET'):
            if method == 'DELETE' and resource.endswith('/5'):
                raise cleanup.CleanupError('temporary error')
            return self.api(repo, resource, method=method)
        with patch.object(cleanup, 'api', side_effect=partial), self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup('123')
        self.artifacts = [item for item in self.artifacts if item['id'] != 4]
        self.deleted.clear()
        self.invoke()
        self.assertEqual(self.deleted, ['/actions/artifacts/5', '/actions/artifacts/6'])

    def test_pagination_does_not_skip_final_files_on_second_page(self):
        fillers = [{'id': i + 100, 'name': f'diagnostic-{i}'} for i in range(100)]
        def pages(repo, resource, *, method='GET'):
            if '/artifacts?' in resource:
                batch = fillers if resource.endswith('page=1') else self.artifacts
                return {'total_count': 107, 'artifacts': batch}
            return self.api(repo, resource, method=method)
        with patch.object(cleanup, 'api', side_effect=pages), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(len(cleanup.cleanup('123')), 3)


if __name__ == '__main__':
    unittest.main()
