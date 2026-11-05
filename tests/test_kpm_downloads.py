"""Bounded KPM public asset retries retain host, size and payload validation."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import http.client
import io
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import apply_kpm_patch as kpm

URL = f'https://github.com/{kpm.REPOSITORY}/releases/download/{kpm.TAG}/kpimg'
CDN = 'https://release-assets.githubusercontent.com/public?signature=do-not-log'
METADATA_URL = f'https://api.github.com/repos/{kpm.REPOSITORY}/releases/tags/{kpm.TAG}'
ASSET_URL = f'https://api.github.com/repos/{kpm.REPOSITORY}/releases/assets/777'


def response(data=b'validated elsewhere'):
    value = io.BytesIO(data)
    value.status = 200
    value.headers = {'Content-Length': str(len(data))}
    return value


def failure(code, url=URL, headers=None):
    return urllib.error.HTTPError(url, code, 'upstream-secret-body', headers or {}, io.BytesIO(b'secret'))


class KPMDownloadTests(unittest.TestCase):
    def setUp(self):
        self.opener = mock.Mock()
        self.patch = mock.patch.object(kpm.urllib.request, 'build_opener', return_value=self.opener)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        sleep_patch = mock.patch.object(kpm.time, 'sleep')
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def download(self, events):
        self.opener.open.side_effect = events
        with redirect_stdout(io.StringIO()) as output:
            data = kpm.download(URL, len(b'validated elsewhere'))
        return data, output.getvalue()

    def test_transient_http_and_partial_transport_restart_the_original_url(self):
        for error in (failure(502), urllib.error.URLError('private url/token'), http.client.IncompleteRead(b'partial', 20)):
            self.opener.reset_mock(); self.sleep.reset_mock()
            with self.subTest(error=type(error).__name__):
                data, output = self.download([error, response()])
                self.assertEqual(b'validated elsewhere', data)
                self.assertEqual([URL, URL], [call.args[0].full_url for call in self.opener.open.call_args_list])
                self.assertEqual(2, self.sleep.call_args.args[0])
                self.assertNotIn('private', output)
                self.assertNotIn('secret', output)

    def test_expired_cdn_signature_refreshes_original_release_redirect(self):
        data, output = self.download([failure(302, headers={'Location': CDN}), failure(403, url=CDN),
                                      failure(302, headers={'Location': CDN + '-fresh'}), response()])
        self.assertEqual(b'validated elsewhere', data)
        requests = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual([URL, CDN, URL, CDN + '-fresh'], [request.full_url for request in requests])
        self.assertEqual('no-cache', requests[2].get_header('Cache-control'))
        for request in requests:
            self.assertIsNone(request.get_header('Authorization'))
            self.assertIsNone(request.get_header('Cookie'))
        self.assertNotIn('do-not-log', output)
        self.assertIn('HTTP 403', output)

    def test_retry_after_is_respected_for_rate_limit_and_dates(self):
        self.download([failure(429, headers={'Retry-After': '25'}), response()])
        self.assertEqual(25, self.sleep.call_args.args[0])
        future = datetime.now(timezone.utc) + timedelta(seconds=40)
        delay = kpm._asset_retry_delay(format_datetime(future), 5)
        self.assertGreater(delay, 38)
        self.assertLessEqual(delay, 40)
        for value in ('999', 'invalid'):
            with self.subTest(value=value), self.assertRaises(kpm.KPMValidationError):
                kpm._asset_retry_delay(value, 5)

    def test_persistent_http_failure_stops_after_four_attempts_with_public_status(self):
        self.opener.open.side_effect = [failure(503) for _ in range(4)]
        with redirect_stdout(io.StringIO()) as output, self.assertRaisesRegex(kpm.KPMValidationError, '0.13.9/kpimg: HTTP 503 after four attempts'):
            kpm.download(URL, 10)
        self.assertEqual(4, self.opener.open.call_count)
        self.assertEqual([2, 5, 10], [call.args[0] for call in self.sleep.call_args_list])
        self.assertNotIn('upstream-secret', output.getvalue())

    def test_permanent_missing_asset_is_clear_and_is_not_retried(self):
        self.opener.open.side_effect = failure(404)
        with self.assertRaisesRegex(kpm.KPMValidationError, '0.13.9/kpimg: HTTP 404'):
            kpm.download(URL, 10)
        self.assertEqual(1, self.opener.open.call_count)
        self.sleep.assert_not_called()

    def test_size_or_unsafe_redirect_failure_never_becomes_a_retry_success(self):
        for first in (response(b'wrong size'), failure(302, headers={'Location': 'https://example.com/steal'})):
            self.opener.reset_mock(); self.sleep.reset_mock()
            self.opener.open.side_effect = [first, response()]
            with self.subTest(first=type(first).__name__), self.assertRaises(kpm.KPMValidationError):
                kpm.download(URL, len(b'validated elsewhere'))
            self.sleep.assert_not_called()
            self.assertEqual(1, self.opener.open.call_count)

    def test_retry_after_larger_than_budget_does_not_send_an_early_request(self):
        self.opener.open.side_effect = [failure(429, headers={'Retry-After': '120'}), response()]
        with self.assertRaisesRegex(kpm.KPMValidationError, 'later retry'):
            kpm.download(URL, 10)
        self.assertEqual(1, self.opener.open.call_count)
        self.sleep.assert_not_called()

    def test_correct_declared_length_but_short_body_retries_from_original_url(self):
        partial = response(b'partial')
        partial.headers['Content-Length'] = str(len(b'validated elsewhere'))
        data, output = self.download([partial, response()])
        self.assertEqual(b'validated elsewhere', data)
        self.assertIn('incomplete response', output)
        self.assertEqual([URL, URL], [call.args[0].full_url for call in self.opener.open.call_args_list])

    def test_persistent_short_body_without_length_is_bounded_and_never_returned(self):
        replies = [response(b'partial') for _ in range(4)]
        for reply in replies:
            reply.headers = {}
        self.opener.open.side_effect = replies
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(kpm.KPMValidationError, 'incomplete response after four attempts'):
            kpm.download(URL, len(b'validated elsewhere'))
        self.assertEqual(4, self.opener.open.call_count)


class KPMAssetApiRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.payload = b'validated elsewhere'
        self.lock = copy.deepcopy(kpm.load_release())
        self.lock['assets']['kpimg'].update(size=len(self.payload), sha256=kpm.digest(self.payload))
        self.metadata = {'tag_name': kpm.TAG, 'draft': False, 'prerelease': False,
            'html_url': f'https://github.com/{kpm.REPOSITORY}/releases/tag/{kpm.TAG}',
            'assets': [{'id': 777, 'url': ASSET_URL, 'name': 'kpimg', 'size': len(self.payload),
                        'state': 'uploaded', 'digest': 'sha256:' + kpm.digest(self.payload),
                        'browser_download_url': URL}]}
        self.opener = mock.Mock()
        for target, value in ((mock.patch.object(kpm, 'load_release'), self.lock),
                              (mock.patch.object(kpm.urllib.request, 'build_opener'), self.opener)):
            target.start().return_value = value
            self.addCleanup(target.stop)
        sleep = mock.patch.object(kpm.time, 'sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def metadata_response(self, document=None):
        value = response(json.dumps(self.metadata if document is None else document).encode())
        value.geturl = lambda: METADATA_URL
        return value

    def test_504_uses_a_different_verified_official_asset_route_and_strips_cdn_auth(self):
        self.opener.open.side_effect = [failure(504), self.metadata_response(),
                                        failure(302, url=ASSET_URL, headers={'Location': CDN}), response(self.payload)]
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'read-token-must-not-leak'}), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.payload, kpm.download(URL, len(self.payload)))
        requests = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual([URL, METADATA_URL, ASSET_URL, CDN], [request.full_url for request in requests])
        for index, request in enumerate(requests):
            self.assertIsNone(request.get_header('Cookie'))
            self.assertEqual('Bearer read-token-must-not-leak' if index in (1, 2) else None,
                             request.get_header('Authorization'))
        self.assertEqual('application/vnd.github+json', requests[1].get_header('Accept'))
        self.assertEqual('application/octet-stream', requests[2].get_header('Accept'))
        self.assertNotIn('do-not-log', output.getvalue())
        self.assertNotIn('read-token-must-not-leak', output.getvalue())
        self.assertIn('fixed official asset API', output.getvalue())

    def test_metadata_cannot_select_another_release_id_size_url_or_digest(self):
        for mutation in ('tag', 'draft', 'prerelease', 'release_url', 'id', 'bool_id', 'name', 'size',
                         'asset_url', 'browser_url', 'digest', 'duplicate', 'state'):
            metadata = copy.deepcopy(self.metadata)
            asset = metadata['assets'][0]
            if mutation == 'tag': metadata['tag_name'] = 'new-unreviewed-tag'
            elif mutation == 'draft': metadata['draft'] = True
            elif mutation == 'prerelease': metadata['prerelease'] = True
            elif mutation == 'release_url': metadata['html_url'] = 'https://example.com/release'
            elif mutation == 'id': asset['id'] = -1
            elif mutation == 'bool_id': asset['id'] = True
            elif mutation == 'name': asset['name'] = 'another-tool'
            elif mutation == 'size': asset['size'] += 1
            elif mutation == 'asset_url': asset['url'] = ASSET_URL.replace(kpm.REPOSITORY, 'another/repository')
            elif mutation == 'browser_url': asset['browser_download_url'] = URL.replace(kpm.TAG, 'another-tag')
            elif mutation == 'digest': asset['digest'] = 'sha256:' + '0' * 64
            elif mutation == 'duplicate': metadata['assets'].append(copy.deepcopy(asset))
            elif mutation == 'state': asset['state'] = 'unprocessed'
            self.opener.reset_mock()
            self.opener.open.side_effect = [failure(504), self.metadata_response(metadata)]
            with self.subTest(mutation=mutation), redirect_stdout(io.StringIO()), self.assertRaises(kpm.KPMValidationError):
                kpm.download(URL, len(self.payload))
            self.assertEqual(2, self.opener.open.call_count)

    def test_metadata_redirect_and_asset_redirect_cannot_forward_token_elsewhere(self):
        for phase in ('metadata', 'binary'):
            events = [failure(504)]
            if phase == 'binary':
                events.append(self.metadata_response())
            events.append(failure(302, headers={'Location': 'https://example.com/steal'}))
            self.opener.reset_mock(); self.opener.open.side_effect = events
            with self.subTest(phase=phase), mock.patch.dict(os.environ, {'GH_TOKEN': 'private-token'}), \
                    redirect_stdout(io.StringIO()), self.assertRaises(kpm.KPMValidationError):
                kpm.download(URL, len(self.payload))
            self.assertTrue(all('example.com' not in call.args[0].full_url for call in self.opener.open.call_args_list))

    def test_api_transport_retry_refreshes_metadata_and_remains_bounded(self):
        self.opener.open.side_effect = [failure(504), self.metadata_response(), failure(504, url=ASSET_URL),
                                        self.metadata_response(), response(self.payload)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.payload, kpm.download(URL, len(self.payload)))
        self.assertEqual([URL, METADATA_URL, ASSET_URL, METADATA_URL, ASSET_URL],
                         [call.args[0].full_url for call in self.opener.open.call_args_list])
        self.assertEqual([2, 5], [call.args[0] for call in self.sleep.call_args_list])

    def test_api_body_still_requires_the_original_local_byte_hash(self):
        wrong = b'X' * len(self.payload)
        self.opener.open.side_effect = [failure(504), self.metadata_response(), response(wrong)]
        with redirect_stdout(io.StringIO()):
            raw = kpm.download(URL, len(self.payload))
        with self.assertRaisesRegex(kpm.KPMValidationError, 'size/SHA256'):
            kpm._verified(raw, 'kpimg', self.lock)

    def test_unlocked_url_cannot_gain_official_api_request_authority(self):
        unknown = URL.replace(kpm.REPOSITORY, 'unknown/repository')
        self.opener.open.side_effect = [failure(504) for _ in range(4)]
        with redirect_stdout(io.StringIO()), self.assertRaises(kpm.KPMValidationError):
            kpm.download(unknown, len(self.payload))
        self.assertEqual([unknown] * 4, [call.args[0].full_url for call in self.opener.open.call_args_list])

    def test_malformed_and_oversized_api_metadata_are_rejected_without_asset_download(self):
        for raw in (b'not-json', b'x' * (1024 * 1024 + 1)):
            value = response(raw)
            value.geturl = lambda: METADATA_URL
            self.opener.reset_mock(); self.opener.open.side_effect = [failure(504), value]
            with self.subTest(size=len(raw)), redirect_stdout(io.StringIO()), self.assertRaises(kpm.KPMValidationError):
                kpm.download(URL, len(self.payload))
            self.assertEqual(2, self.opener.open.call_count)

    def test_short_metadata_with_correct_declared_length_retries_the_fixed_api(self):
        partial = self.metadata_response()
        complete = partial.getvalue()
        partial = response(complete[:20])
        partial.geturl = lambda: METADATA_URL
        partial.headers['Content-Length'] = str(len(complete))
        self.opener.open.side_effect = [failure(504), partial, self.metadata_response(), response(self.payload)]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.payload, kpm.download(URL, len(self.payload)))
        self.assertEqual([URL, METADATA_URL, METADATA_URL, ASSET_URL],
                         [call.args[0].full_url for call in self.opener.open.call_args_list])
        self.assertEqual([2, 5], [call.args[0] for call in self.sleep.call_args_list])
        self.assertIn('incomplete response', output.getvalue())

    def test_invalid_or_oversized_declared_metadata_length_is_not_retried(self):
        for length in ('invalid', '-1', str(1024 * 1024 + 1), '0'):
            value = self.metadata_response()
            value.headers['Content-Length'] = length
            self.opener.reset_mock(); self.sleep.reset_mock()
            self.opener.open.side_effect = [failure(504), value]
            with self.subTest(length=length), redirect_stdout(io.StringIO()), self.assertRaises(kpm.KPMValidationError):
                kpm.download(URL, len(self.payload))
            self.assertEqual(2, self.opener.open.call_count)
            self.assertEqual(1, self.sleep.call_count)


if __name__ == '__main__':
    unittest.main()
