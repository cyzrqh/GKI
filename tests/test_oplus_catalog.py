"""Verify official-source immutability and reviewed OPlus recipe boundaries."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_catalog as catalog


def makefile(version='6.1.57'):
    first, second, third = version.split('.')
    raw = f'VERSION = {first}\nPATCHLEVEL = {second}\nSUBLEVEL = {third}\nNAME = Official fixture\n'.encode()
    return {'type': 'file', 'path': 'Makefile', 'encoding': 'base64', 'size': len(raw),
            'content': base64.b64encode(raw).decode(),
            'sha': hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()}


class Reader:
    def __init__(self, profile, *, reference='b' * 40, version=None):
        self.calls = []
        self.branch = {'name': profile['kernel_branch'], 'commit': {'sha': reference,
                       'commit': {'committer': {'date': '2026-10-27T00:00:00Z'}}}}
        self.file = makefile(version or profile['kernel'])
        self.historical_file = makefile(profile['kernel'])
        self.historical = {'sha': profile['verified_kernel_ref'], 'tree': {'sha': 'c' * 40},
                           'committer': {'date': '2025-10-01T00:00:00Z'}}

    def get(self, resource):
        self.calls.append(resource)
        if '/git/commits/' in resource:
            return copy.deepcopy(self.historical)
        if '/contents/Makefile?' in resource:
            return copy.deepcopy(self.historical_file if resource.endswith(self.historical['sha']) else self.file)
        return copy.deepcopy(self.branch)


class Response:
    status = 200

    def __init__(self, raw=b'{}', url=None):
        self.raw, self.url = raw, url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, size):
        return self.raw[:size]


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.profiles = catalog.load_profiles(ROOT)
        self.profile = next(row for row in self.profiles if row['id'] == 'sm8650-6.1.57')

    def load_document(self, document):
        raw = json.dumps(document, ensure_ascii=False).encode()
        fake = mock.Mock()
        fake.is_symlink.return_value = False
        fake.is_file.return_value = True
        fake.stat.return_value.st_size = len(raw)
        fake.read_bytes.return_value = raw
        with mock.patch.object(catalog, 'Path') as path:
            path.return_value.__truediv__.return_value = fake
            return catalog.load_profiles(Path('fixture'))

    def test_checked_in_profiles_have_only_reviewed_official_oki_sources(self):
        self.assertEqual(22, len(self.profiles))
        self.assertEqual(22, len({(catalog.platform_family(row['platform']), row['kernel'])
                                  for row in self.profiles}))
        self.assertEqual({'sm8650', 'sm8750', 'sm8845', 'sm8850', 'mt6897', 'mt6989', 'mt6991', 'mt6993'},
                         {row['platform'] for row in self.profiles})
        for row in self.profiles:
            self.assertIn(row['kernel_repo'], catalog.OFFICIAL_REPOSITORIES)
            self.assertNotIn('_gki', row['recipe_workflow'])
            self.assertIn('不承诺', row['compatibility'])
        self.assertEqual(20, len({(row['recipe_repository'], row['recipe_workflow']) for row in self.profiles}))
        identities = {row['id'] for row in self.profiles}
        self.assertTrue({'sm8750-6.6.118', 'mt6991-6.6.118',
                         'sm8845-6.12.38', 'sm8750-6.6.30', 'mt6991-6.6.50',
                         'mt6989-6.1.115', 'mt6989-6.1.134', 'mt6897-6.1.75'} <= identities)
        self.assertFalse({'mt6989-6.1.75', 'mt6897-6.1.115', 'mt6897-6.1.134',
                          'sm8850-6.12.38'} & identities)

    def test_public_platform_labels_do_not_expose_soc_codes(self):
        for platform in ('sm8650', 'sm8750', 'sm8845', 'sm8850'):
            self.assertEqual('骁龙', catalog.platform_family(platform))
        for platform in ('mt6897', 'mt6989', 'mt6991', 'mt6993'):
            self.assertEqual('天玑', catalog.platform_family(platform))
        for platform in (None, [], 'sm0000', 'mt0000', 'SM8650', 'sm8650\n'):
            with self.subTest(platform=platform), self.assertRaises(catalog.CatalogError):
                catalog.platform_family(platform)

    def test_same_full_version_keeps_both_families_and_preserves_source_pins(self):
        both = [row for row in self.profiles if row['kernel'] == '6.6.118']
        loaded = self.load_document({'schema_version': 1, 'profiles': both[::-1]})
        self.assertEqual(['骁龙', '天玑'], [catalog.platform_family(row['platform']) for row in loaded])
        self.assertEqual({row['id']: row['verified_kernel_ref'] for row in both},
                         {row['id']: row['verified_kernel_ref'] for row in loaded})

    def test_same_family_version_cannot_be_reintroduced_with_a_different_soc(self):
        original = next(row for row in self.profiles if row['id'] == 'sm8845-6.12.38')
        duplicate = {**original, 'id': 'sm8850-6.12.38', 'platform': 'sm8850',
                     'kernel_repo': 'OnePlusOSS/android_kernel_common_oneplus_sm8850',
                     'kernel_branch': 'oneplus/sm8850_b_16.0_oneplus_15t'}
        with self.assertRaisesRegex(catalog.CatalogError, 'duplicates a platform-family'):
            self.load_document({'schema_version': 1, 'profiles': [original, duplicate]})
        original = next(row for row in self.profiles if row['id'] == 'mt6989-6.1.115')
        duplicate = {**original, 'id': 'mt6897-6.1.115', 'platform': 'mt6897',
                     'kernel_repo': 'OnePlusOSS/android_kernel_oneplus_mt6897',
                     'kernel_branch': 'oneplus/mt6897_v_15.0.2_nord_ce5'}
        with self.assertRaisesRegex(catalog.CatalogError, 'duplicates a platform-family'):
            self.load_document({'schema_version': 1, 'profiles': [original, duplicate]})

    def test_catalog_is_sorted_by_complete_numeric_version_not_profile_string(self):
        shuffled = [row for row in self.profiles if row['kernel'] in ('6.1.75', '6.1.118', '6.12.58')][::-1]
        loaded = self.load_document({'schema_version': 1, 'profiles': shuffled})
        actual = [tuple(map(int, row['kernel'].split('.'))) for row in loaded]
        self.assertEqual(sorted(actual), actual)
        self.assertEqual(['骁龙', '天玑'], [catalog.platform_family(row['platform'])
                                            for row in loaded if row['kernel'] == '6.1.75'])

    def test_recipe_mirrors_wrong_soc_unpinned_refs_and_gki_are_rejected(self):
        mutations = {'kernel_repo': 'cctv18/android_kernel_common_oneplus_sm8650',
                     'recipe_ref': 'main', 'recipe_sha256': 'unknown',
                     'recipe_workflow': '.github/workflows/fastbuild_6.1.57_gki.yml',
                     'kernel_version': '6.6', 'android_version': 'android15',
                     'sub_level': '75', 'platform': 'sm8750',
                     'kernel_branch': 'oneplus/../secret', 'system': 'unsafe\nline'}
        for field, value in mutations.items():
            row = {**self.profile, field: value}
            with self.subTest(field=field), self.assertRaises(catalog.CatalogError):
                self.load_document({'schema_version': 1, 'profiles': [row]})
        with self.assertRaises(catalog.CatalogError):
            self.load_document({'schema_version': 1, 'profiles': [self.profile, self.profile]})

    def test_current_official_ref_is_frozen_before_reading_makefile(self):
        reader = Reader(self.profile)
        row = catalog.resolve_profile(self.profile, reader)
        self.assertEqual('b' * 40, row['kernel_ref'])
        self.assertNotEqual(self.profile['verified_kernel_ref'], row['kernel_ref'])
        self.assertTrue(reader.calls[1].endswith('/contents/Makefile?ref=' + 'b' * 40))
        self.assertIn('%2F', reader.calls[0])
        self.assertNotIn('verified_kernel_ref', row)
        self.assertEqual(self.profile['recipe_ref'], row['recipe_ref'])

    def test_kernel_version_drift_stops_before_any_build_matrix_is_returned(self):
        reader = Reader(self.profile, version='6.1.75')
        reader.historical_file = makefile('6.1.75')
        with self.assertRaisesRegex(catalog.CatalogError, 'version changed.*expected 6.1.57, historical snapshot contains 6.1.75'):
            catalog.resolve_profile(self.profile, reader)

    def test_official_branch_upgrade_keeps_exact_reviewed_historical_kernel_and_commit_date(self):
        reader = Reader(self.profile, version='6.1.75')
        row = catalog.resolve_profile(self.profile, reader)
        reference = self.profile['verified_kernel_ref']
        self.assertEqual(reference, row['kernel_ref'])
        self.assertEqual('6.1.57', row['kernel'])
        self.assertEqual('2025-10-01T00:00:00Z', row['source_committed_at'])
        self.assertEqual('/repos/' + row['kernel_repo'] + '/git/commits/' + reference, reader.calls[2])
        self.assertTrue(reader.calls[3].endswith('/contents/Makefile?ref=' + reference))
        self.assertEqual('b' * 40, reader.branch['commit']['sha'])

    def test_official_historical_commit_metadata_must_match_immutable_pin(self):
        for changes in ({'sha': 'd' * 40}, {'tree': {}}, {'tree': {'sha': 'invalid'}},
                        {'committer': {'date': '2026-10-27'}}, {'committer': []}):
            reader = Reader(self.profile, version='6.1.75')
            reader.historical.update(changes)
            with self.subTest(changes=changes), self.assertRaises(catalog.CatalogError):
                catalog.resolve_profile(self.profile, reader)

    def test_older_branch_reversion_never_silently_uses_historical_fallback(self):
        reader = Reader(self.profile, version='6.1.25')
        with self.assertRaisesRegex(catalog.CatalogError, 'refusing an older'):
            catalog.resolve_profile(self.profile, reader)
        self.assertFalse(any('/git/commits/' in call for call in reader.calls))

    def test_bad_official_branch_metadata_is_rejected(self):
        examples = [None, [], {}, {'name': 'other', 'commit': {}},
                    {'name': self.profile['kernel_branch'], 'commit': {'sha': 'main'}},
                    {'name': self.profile['kernel_branch'], 'commit': {'sha': 'b' * 40, 'commit': {'committer': []}}}]
        for bad in examples:
            reader = Reader(self.profile)
            reader.branch = bad
            with self.subTest(bad=bad), self.assertRaises(catalog.CatalogError):
                catalog.resolve_profile(self.profile, reader)
        reader = Reader(self.profile)
        reader.branch['commit']['commit']['committer']['date'] = '2026-10-27'
        with self.assertRaises(catalog.CatalogError):
            catalog.resolve_profile(self.profile, reader)

    def test_makefile_must_match_exact_blob_size_path_and_unambiguous_version(self):
        for field, value in (('sha', 'a' * 40), ('size', 1), ('path', 'other'),
                             ('content', '*notbase64*'), ('encoding', 'none')):
            with self.subTest(field=field), self.assertRaises(catalog.CatalogError):
                catalog.kernel_version({**makefile(), field: value})
        self.assertEqual('6.1.57', catalog.kernel_version(makefile()))

    def test_discovery_sorts_numeric_versions_and_exposes_no_vendor_kernel_mirrors(self):
        selected = [self.profiles[-1], self.profile]
        readers = {row['kernel_repo']: Reader(row) for row in selected}
        class Combined:
            def get(_, resource):
                return next(reader.get(resource) for repo, reader in readers.items()
                            if resource.startswith('/repos/' + repo + '/'))
        with mock.patch.object(catalog, 'load_profiles', return_value=selected):
            matrix = catalog.discover_matrix(ROOT, Combined())
        self.assertEqual(['6.1.57', '6.12.58'], [row['kernel'] for row in matrix['include']])
        self.assertTrue(all(row['kernel_repo'] in catalog.OFFICIAL_REPOSITORIES for row in matrix['include']))

    def test_discovery_refuses_duplicate_family_after_resolution_instead_of_silently_picking_one(self):
        row = catalog.resolve_profile(self.profile, Reader(self.profile))
        duplicate = {**row, 'platform': 'sm8750', 'id': 'sm8750-' + row['kernel']}
        with mock.patch.object(catalog, 'load_profiles', return_value=[self.profile, self.profile]), \
                mock.patch.object(catalog, 'resolve_profile', side_effect=[row, duplicate]), \
                self.assertRaisesRegex(catalog.CatalogError, 'duplicates a platform-family'):
            catalog.discover_matrix(ROOT, mock.Mock())

    def test_cli_summary_uses_only_family_label_and_preserves_official_source_identity(self):
        directory = ROOT.parent / '.gki-upstream-cache' / ('oplus-summary-' + uuid.uuid4().hex)
        directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, directory)
        summary = directory / 'summary.md'
        row = catalog.resolve_profile(self.profile, Reader(self.profile))
        with mock.patch.object(catalog, 'discover_matrix', return_value={'include': [row]}), \
                mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}), \
                mock.patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(0, catalog.main(['--summary']))
        text = summary.read_text(encoding='utf-8')
        self.assertIn('| 骁龙 | 6.1.57 |', text)
        self.assertNotIn('| 骁龙 sm8650 |', text)
        self.assertIn(row['kernel_repo'], text)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.resource = '/repos/OnePlusOSS/android_kernel_common_oneplus_sm8650/branches/oneplus%2Fsm8650_u_14.0.0_oneplus12'

    def test_unreviewed_read_and_write_routes_are_refused_before_transport(self):
        opener = mock.Mock()
        reader = catalog.OfficialReader(token='secret-test-token', opener=opener)
        for resource in ('https://evil.test/', '/repos/cctv18/android_kernel_common_oneplus_sm8650/branches/main',
                         '/repos/OnePlusOSS/android_kernel_common_oneplus_sm8650/actions/runs',
                         '/repos/OnePlusOSS/android_kernel_common_oneplus_sm8650/git/commits/main',
                         self.resource + '\nAuthorization: unsafe'):
            with self.subTest(resource=resource), self.assertRaises(catalog.CatalogError):
                reader.get(resource)
        opener.open.assert_not_called()

    def test_historical_commit_read_is_limited_to_reviewed_official_repo_and_full_sha(self):
        resource = '/repos/OnePlusOSS/android_kernel_common_oneplus_sm8650/git/commits/' + 'b' * 40
        opener = mock.Mock()
        opener.open.return_value = Response(b'{"verified":true}', 'https://api.github.com' + resource)
        self.assertEqual({'verified': True}, catalog.OfficialReader(token='test-token', opener=opener).get(resource))
        request = opener.open.call_args.args[0]
        self.assertEqual('GET', request.get_method())
        self.assertEqual('Bearer test-token', request.get_header('Authorization'))

    def test_authentication_is_sent_only_to_exact_github_api_destination(self):
        opener = mock.Mock()
        response = Response(b'{"native":true}', 'https://api.github.com' + self.resource)
        opener.open.return_value = response
        reader = catalog.OfficialReader(token='secret-test-token', opener=opener)
        self.assertEqual({'native': True}, reader.get(self.resource))
        request = opener.open.call_args.args[0]
        self.assertEqual('https://api.github.com' + self.resource, request.full_url)
        self.assertEqual('Bearer secret-test-token', request.get_header('Authorization'))
        self.assertEqual('GET', request.get_method())

    def test_redirects_and_oversized_results_fail_without_exposing_token(self):
        for response in (Response(b'{}', 'https://other.example/'),
                         Response(b'x' * 32, 'https://api.github.com' + self.resource)):
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch.object(catalog, 'MAX_JSON', 16), self.assertRaises(catalog.CatalogError) as found:
                catalog.OfficialReader(token='secret-test-token', opener=opener).get(self.resource)
            self.assertNotIn('secret-test-token', str(found.exception))

    def test_http_error_redacts_detail_and_retries_only_transient_status(self):
        for status, count in ((404, 1), (503, 3)):
            opener = mock.Mock()
            opener.open.side_effect = HTTPError('https://api.github.com', status,
                                                'secret-test-token', {}, io.BytesIO(b'private'))
            with mock.patch.object(catalog.time, 'sleep'), self.assertRaises(catalog.CatalogError) as found:
                catalog.OfficialReader(token='secret-test-token', opener=opener).get(self.resource)
            self.assertEqual(count, opener.open.call_count)
            self.assertNotIn('secret-test-token', str(found.exception))
        self.assertIsNone(catalog.NoRedirect().redirect_request(None, None, 302, None, None, 'https://elsewhere.test/'))


if __name__ == '__main__':
    unittest.main()
