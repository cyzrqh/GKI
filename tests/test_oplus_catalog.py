"""Verify official-source immutability and reviewed OPlus recipe boundaries."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

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

    def get(self, resource):
        self.calls.append(resource)
        return copy.deepcopy(self.file if '/contents/Makefile?' in resource else self.branch)


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
        self.assertEqual(14, len(self.profiles))
        self.assertEqual(14, len({(row['platform'], row['kernel']) for row in self.profiles}))
        self.assertEqual({'sm8650', 'sm8750', 'sm8845', 'sm8850', 'mt6897', 'mt6989', 'mt6991', 'mt6993'},
                         {row['platform'] for row in self.profiles})
        for row in self.profiles:
            self.assertIn(row['kernel_repo'], catalog.OFFICIAL_REPOSITORIES)
            self.assertNotIn('_gki', row['recipe_workflow'])
            self.assertIn('不承诺', row['compatibility'])

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
        with self.assertRaisesRegex(catalog.CatalogError, 'version changed.*expected 6.1.57, found 6.1.75'):
            catalog.resolve_profile(self.profile, reader)

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


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.resource = '/repos/OnePlusOSS/android_kernel_common_oneplus_sm8650/branches/oneplus%2Fsm8650_u_14.0.0_oneplus12'

    def test_unreviewed_read_and_write_routes_are_refused_before_transport(self):
        opener = mock.Mock()
        reader = catalog.OfficialReader(token='secret-test-token', opener=opener)
        for resource in ('https://evil.test/', '/repos/cctv18/android_kernel_common_oneplus_sm8650/branches/main',
                         '/repos/OnePlusOSS/android_kernel_common_oneplus_sm8650/actions/runs',
                         self.resource + '\nAuthorization: unsafe'):
            with self.subTest(resource=resource), self.assertRaises(catalog.CatalogError):
                reader.get(resource)
        opener.open.assert_not_called()

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
