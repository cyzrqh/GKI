"""Bounded KPM public asset retries retain host, size and payload validation."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import http.client
import io
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


if __name__ == '__main__':
    unittest.main()
