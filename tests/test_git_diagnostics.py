"""Git failure diagnostics expose fixed classifications, never captured text."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import snapshot_ksu_source as snapshot
import shared_susfs_source as shared


class GitDiagnosticsTests(unittest.TestCase):
    SECRET = 'private-token-and-path'
    URL = 'https://user:private-token-and-path@example.invalid/private.git'

    def git_error(self, stderr, *, arguments=('fetch',), returncode=128):
        result = subprocess.CompletedProcess(['git'], returncode, stdout='', stderr=stderr)
        with mock.patch.object(snapshot.subprocess, 'run', return_value=result):
            with self.assertRaises(snapshot.GitCommandError) as found:
                snapshot.git(Path('.'), *arguments)
        message = str(found.exception)
        self.assertNotIn(self.SECRET, message)
        self.assertNotIn('https://', message)
        self.assertNotIn('example.invalid', message)
        self.assertIn(f'exit {returncode}', message)
        return message

    def test_whitelisted_failure_categories_never_include_original_stderr(self):
        cases = (
            ('fatal: remote error: GitLab is currently unable to handle this request due to load', 'GitLab server load'),
            ('fatal: unable to access URL: The requested URL returned error: 503', 'HTTP error 503'),
            ('error: RPC failed; HTTP 502 curl 22', 'HTTP error 502'),
            ('fatal: Could not resolve host: example.invalid', 'DNS resolution'),
            ('fatal: Failed to connect to example.invalid port 443: Connection refused', 'connection failure'),
            ('fatal: server certificate verification failed', 'TLS failure'),
            ('fatal: TLS connect error', 'TLS failure'),
            ('fatal: redirecting to another site is not allowed', 'redirect refused'),
            ("fatal: couldn't find remote ref secret-branch", 'missing remote ref'),
            ('fatal: remote error: upload-pack: not our ref secret-hash', 'missing remote ref'),
            ('fatal: Authentication failed', 'authentication failure'),
            ('fatal: Operation timed out', 'network timeout'),
            ('fatal: unexpected secret server output', 'unclassified Git failure'),
        )
        for raw, category in cases:
            with self.subTest(category=category, raw=raw):
                message = self.git_error(raw + '\nAuthorization: Bearer ' + self.SECRET + '\n' + self.URL)
                self.assertIn(category, message)
                self.assertNotIn('Authorization', message)
                self.assertNotIn('secret-branch', message)
                self.assertNotIn('secret-hash', message)

    def test_config_arguments_and_unknown_operation_do_not_leak(self):
        message = self.git_error(self.URL, arguments=('-c', 'http.extraheader=' + self.SECRET,
                                                      'fetch', self.URL))
        self.assertIn('Git fetch failed', message)
        message = self.git_error(self.URL, arguments=(self.SECRET, self.URL))
        self.assertIn('Git command failed', message)

    def test_bytes_and_unstructured_stderr_are_safe(self):
        self.assertEqual(('TLS failure', None), snapshot.git_failure_category(b'fatal: TLS failure \xff'))
        self.assertEqual(('unclassified Git failure', None), snapshot.git_failure_category(None))
        self.assertEqual(('unclassified Git failure', None), snapshot.git_failure_category(object()))

    def test_timeout_and_oserror_hide_command_and_exception_details(self):
        errors = (
            subprocess.TimeoutExpired([self.URL, self.SECRET], 10, stderr=self.SECRET),
            OSError(self.URL + ' ' + self.SECRET),
        )
        for error in errors:
            with self.subTest(kind=type(error).__name__), mock.patch.object(
                    snapshot.subprocess, 'run', side_effect=error):
                with self.assertRaises(snapshot.GitCommandError) as found:
                    snapshot.git(Path('.'), 'fetch', self.URL, timeout=180)
                message = str(found.exception)
                self.assertNotIn(self.SECRET, message)
                self.assertNotIn(self.URL, message)
                self.assertIsNone(found.exception.__cause__)
                self.assertTrue(found.exception.__suppress_context__)
                self.assertIn('timeout after 180s' if isinstance(error, subprocess.TimeoutExpired)
                              else 'process execution failure', message)

    def test_exception_constructor_limits_labels_to_known_constants(self):
        message = str(snapshot.GitCommandError(self.SECRET, category=self.URL,
                                               returncode=self.SECRET, http_status=self.SECRET))
        self.assertEqual('Source Git command failed (unclassified Git failure)', message)
        self.assertEqual('Source Git fetch could not complete (timeout)',
                         str(snapshot.GitCommandError('fetch', timeout=self.SECRET)))

    def test_success_returns_native_stdout_unchanged(self):
        result = subprocess.CompletedProcess(['git'], 0, stdout='native version\n', stderr=self.SECRET)
        with mock.patch.object(snapshot.subprocess, 'run', return_value=result):
            self.assertEqual('native version', snapshot.git(Path('.'), 'describe', '--tags'))

    def test_shared_retry_and_final_error_preserve_only_safe_git_diagnostic(self):
        safe = snapshot.GitCommandError('fetch', returncode=128, category='HTTP error', http_status=503)
        for error, expected in ((safe, 'Source Git fetch failed (exit 128; HTTP error 503)'),
                                (snapshot.SnapshotError(self.URL + self.SECRET), 'Git request failed (unclassified)')):
            output = io.StringIO()
            with (self.subTest(kind=type(error).__name__),
                  mock.patch.object(shared, 'git', side_effect=error) as git,
                  mock.patch.object(shared.time, 'sleep') as sleep,
                  contextlib.redirect_stdout(output)):
                with self.assertRaises(snapshot.SnapshotError) as found:
                    shared.fetch(Path('.'), ['+refs/heads/target:refs/gki-susfs/target'])
            message = str(found.exception)
            self.assertIn('four spaced download attempts: ' + expected, message)
            self.assertEqual(4, output.getvalue().count(expected))
            self.assertEqual(4, git.call_count)
            self.assertEqual([mock.call(15), mock.call(30), mock.call(60)], sleep.call_args_list)
            for rendered in (message, output.getvalue()):
                self.assertNotIn(self.SECRET, rendered)
                self.assertNotIn(self.URL, rendered)


if __name__ == '__main__':
    unittest.main()
