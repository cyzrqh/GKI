"""Fixed-commit reuse and bounded GitLab throttling without external requests."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from snapshot_ksu_source import temporary_directory, SnapshotError
import susfs_sync_transport as transport
import update_susfs_sources as update
import test_update_susfs_sources as fixtures

URL = transport.API + '/repository/tree?ref=' + fixtures.NEW + '&per_page=100&page=1'


class Gate:
    def __init__(self):
        self.waited, self.delays = 0, []

    def wait(self):
        self.waited += 1

    def cooldown(self, delay):
        self.delays.append(delay)


class Response(io.BytesIO):
    status = 200
    headers = {}

    def geturl(self):
        return URL


def limited(seconds='25', code=429):
    headers = Message()
    if seconds is not None:
        headers['Retry-After'] = seconds
    return HTTPError(URL, code, 'upstream body must never be logged', headers, io.BytesIO(b'private error text'))


class SusfsTransportTests(unittest.TestCase):
    def environment(self, directory):
        return {'RUNNER_TEMP': str(directory), 'GKI_SUSFS_METADATA_CACHE': str(directory / 'observations'),
                'GITHUB_RUN_ID': '42015235663', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_REPOSITORY': 'cyzrqh/GKI'}

    def test_numeric_and_http_date_retry_after_are_respected(self):
        instant = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.assertEqual(25, transport.retry_delay('25', 10))
        self.assertEqual(30, transport.retry_delay(format_datetime(instant + timedelta(seconds=30)), 10, now=instant))
        self.assertEqual(10, transport.retry_delay('0', 10))
        self.assertEqual(20, transport.retry_delay(None, 20))
        for value in ('9999', 'invalid', '-1'):
            with self.subTest(value=value), self.assertRaises(SnapshotError):
                transport.retry_delay(value, 10, now=instant)

    def test_429_retries_share_cooldown_and_eventually_return_the_real_response(self):
        gate = Gate()
        opener = mock.Mock()
        opener.open.side_effect = [limited(), Response(b'[]')]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(b'[]', transport.request_bytes(URL, 1024, opener=opener, gate=gate))
        self.assertEqual([25], gate.delays)
        self.assertEqual(2, gate.waited)
        self.assertNotIn('private', output.getvalue())
        request = opener.open.call_args.args[0]
        self.assertNotIn('Authorization', request.headers)

    def test_persistent_429_is_bounded_and_never_becomes_success(self):
        gate = Gate()
        opener = mock.Mock()
        opener.open.side_effect = [limited(None) for _ in range(transport.MAX_ATTEMPTS)]
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(SnapshotError, 'HTTP 429'):
            transport.request_bytes(URL, 1024, opener=opener, gate=gate)
        self.assertEqual(4, opener.open.call_count)
        self.assertEqual([10, 20, 40], gate.delays)

    def test_retry_after_beyond_job_budget_does_not_retry_early(self):
        opener = mock.Mock()
        opener.open.side_effect = limited('120')
        gate = Gate()
        with self.assertRaisesRegex(SnapshotError, 'later retry'):
            transport.request_bytes(URL, 1024, opener=opener, gate=gate)
        self.assertEqual(1, opener.open.call_count)
        self.assertEqual([], gate.delays)

    def test_request_gate_shares_rate_limit_window_and_spaces_following_requests(self):
        now = [0.0]
        gate = transport.RequestGate(clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        gate.wait()
        self.assertEqual(0, now[0])
        gate.cooldown(25)
        gate.wait()
        self.assertEqual(25, now[0])
        gate.wait()
        self.assertEqual(25 + transport.MIN_REQUEST_INTERVAL, now[0])

    def test_other_hosts_branches_redirects_and_oversized_responses_are_rejected(self):
        opener = mock.Mock()
        for url in (URL.replace('gitlab.com', 'example.com'), URL.replace(fixtures.NEW, 'main'),
                    transport.API + '/repository/archive.tar.gz?sha=' + fixtures.NEW):
            with self.subTest(url=url), self.assertRaises(SnapshotError):
                transport.request_bytes(url, 100, opener=opener, gate=Gate())
        opener.open.assert_not_called()
        bad = Response(b'[]')
        bad.geturl = lambda: 'https://example.com/'
        opener.open.return_value = bad
        with self.assertRaisesRegex(SnapshotError, 'unexpected'):
            transport.request_bytes(URL, 100, opener=opener, gate=Gate())
        opener.open.return_value = Response(b'x' * 101)
        with self.assertRaisesRegex(SnapshotError, 'bound'):
            transport.request_bytes(URL, 100, opener=opener, gate=Gate())

    def test_check_apply_and_merge_validation_reuse_only_this_runs_verified_fixed_metadata(self):
        with temporary_directory(ROOT / 'tests', '.susfs-sync-run-') as directory:
            profile, managers = fixtures.profiles()
            (directory / 'config').mkdir()
            (directory / 'data').mkdir()
            (directory / update.PROFILE_PATH).write_text(json.dumps(profile), encoding='utf-8')
            (directory / update.PINS_PATH).write_text(json.dumps(managers), encoding='utf-8')
            output = directory / 'outputs'
            sources = {target: fixtures.NEW for target in update.TARGETS}
            with mock.patch.dict(os.environ, self.environment(directory), clear=False), \
                    mock.patch.object(update, 'request_bytes', side_effect=fixtures.transport) as fetched, \
                    mock.patch.object(update, 'gitlab_head', side_effect=AssertionError('branch drift')), redirect_stdout(io.StringIO()):
                self.assertEqual(0, update.main(['--check', '--root', str(directory), '--candidates-json', json.dumps(sources), '--github-output', str(output)]))
                values = dict(line.split('=', 1) for line in output.read_text('utf-8').splitlines())
                receipt = json.loads(values['candidates_json'])
                initial_calls = fetched.call_count
                self.assertEqual(4, initial_calls)  # Two fixed commits: root inventory + original version header.
                self.assertEqual(0, update.main(['--apply-candidates-json', values['candidates_json'], '--root', str(directory)]))
                result = update.validate_update(None, receipt, profile, managers)
                self.assertEqual(receipt, result['receipt'])
                self.assertEqual(initial_calls, fetched.call_count)
                altered = json.loads(values['candidates_json'])
                altered['pins']['KowSU'][update.TARGETS[0]]['selected_ref'] = fixtures.OLD
                with self.assertRaises(ValueError):
                    update.validate_update(None, altered, profile, managers)
                update.observe_metadata(fixtures.DIFFERENT)
                self.assertEqual(initial_calls + 2, fetched.call_count)

    def test_stale_corrupt_and_cross_commit_cache_entries_cannot_authorize_reuse(self):
        with temporary_directory(ROOT / 'tests', '.susfs-sync-run-') as directory:
            with mock.patch.dict(os.environ, self.environment(directory), clear=False), \
                    mock.patch.object(update, 'request_bytes', side_effect=fixtures.transport) as fetched:
                expected = update.observe_metadata(fixtures.NEW)
                cache = transport.RunMetadataCache.from_environment()
                path = cache.path(fixtures.NEW)
                original = path.read_bytes()
                for kind in ('run', 'attempt', 'source', 'digest', 'ref', 'malformed'):
                    document = json.loads(original)
                    if kind == 'run': document['run']['GITHUB_RUN_ID'] = '9'
                    elif kind == 'attempt': document['run']['GITHUB_RUN_ATTEMPT'] = '2'
                    elif kind == 'source': document['source'] = 'https://example.com'
                    elif kind == 'digest': document['sha256'] = '0' * 64
                    elif kind == 'ref':
                        document['metadata']['ref'] = fixtures.OLD
                        document['sha256'] = hashlib.sha256(transport._canonical(document['metadata'])).hexdigest()
                    path.write_bytes(b'not json' if kind == 'malformed' else json.dumps(document).encode())
                    before = fetched.call_count
                    with self.subTest(kind=kind):
                        self.assertEqual(expected, update.observe_metadata(fixtures.NEW))
                        self.assertEqual(before + 2, fetched.call_count)

    def test_malformed_official_source_is_not_written_to_the_temporary_cache(self):
        with temporary_directory(ROOT / 'tests', '.susfs-sync-run-') as directory:
            with mock.patch.dict(os.environ, self.environment(directory), clear=False), \
                    mock.patch.object(update, 'request_bytes', return_value=b'[]'):
                with self.assertRaisesRegex(ValueError, 'complete kernel patch tree'):
                    update.observe_metadata(fixtures.NEW)
                cache = transport.RunMetadataCache.from_environment()
                self.assertFalse(cache.path(fixtures.NEW).exists())

    def test_cache_path_cannot_leave_the_run_temporary_directory(self):
        with temporary_directory(ROOT / 'tests', '.susfs-sync-run-') as directory:
            environment = self.environment(directory)
            environment['GKI_SUSFS_METADATA_CACHE'] = str(ROOT / 'data')
            with mock.patch.dict(os.environ, environment, clear=False), self.assertRaisesRegex(SnapshotError, 'temporary directory'):
                transport.RunMetadataCache.from_environment()


if __name__ == '__main__':
    unittest.main()
