import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import shutil
import unittest
import uuid
from urllib.error import HTTPError, URLError
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import set_kernel_ref as kernel_ref
from set_kernel_ref import (KernelRefTransportError, resolve_kernel_ref,
                            resolve_named_kernel_ref, set_kernel_ref, validate_ref)


BRANCH = "android14-6.1-2025-09"
REF = f"refs/heads/{BRANCH}"
DEPRECATED_REF = f"refs/heads/deprecated/{BRANCH}"


class KernelRefTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / f".kernel-ref-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_only_common_revision_changes(self):
        default = self.write("default.xml", '<manifest><!-- keep --><remote name="aosp" fetch=".."/><default revision="old" remote="aosp"/><project name="kernel/common" path="common" revision="old" groups="x"/><project name="toolchain" path="tools" revision="tools-ref"/></manifest>')
        ref, count = set_kernel_ref(self.root, REF, BRANCH, named_resolver=lambda branch, ref: ref)
        self.assertEqual((ref, count), (REF, 1))
        tree = ET.parse(default).getroot()
        self.assertEqual(tree.find("default").attrib, {"revision": "old", "remote": "aosp"})
        common, other = tree.findall("project")
        self.assertEqual(common.attrib, {"name": "kernel/common", "path": "common", "revision": REF, "groups": "x"})
        self.assertEqual(other.get("revision"), "tools-ref")
        self.assertIn("<!-- keep -->", default.read_text(encoding="utf-8"))

    def test_reachable_includes_only(self):
        self.write("default.xml", '<manifest><include name="parts/kernel.xml"/></manifest>')
        included = self.write("parts/kernel.xml", '<manifest><project name="common" path="common" revision="old"/></manifest>')
        unused = self.write("unused.xml", '<manifest><project path="common" revision="untouched"/></manifest>')
        self.assertEqual(set_kernel_ref(self.root, REF, BRANCH, named_resolver=lambda branch, ref: ref), (REF, 1))
        self.assertEqual(ET.parse(included).find("project").get("revision"), REF)
        self.assertEqual(ET.parse(unused).find("project").get("revision"), "untouched")

    def test_include_traversal_rejected_before_mutation(self):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/><include name="../escape.xml"/></manifest>')
        original = default.read_bytes()
        with self.assertRaises(ValueError):
            set_kernel_ref(self.root, REF, BRANCH)
        self.assertEqual(default.read_bytes(), original)

    def test_cycle_rejected_before_mutation(self):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/><include name="parts.xml"/></manifest>')
        self.write("parts.xml", '<manifest><include name="default.xml"/></manifest>')
        original = default.read_bytes()
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            set_kernel_ref(self.root, REF, BRANCH)
        self.assertEqual(default.read_bytes(), original)

    def test_missing_common_does_not_resolve_or_mutate(self):
        default = self.write("default.xml", '<manifest><project path="tools" revision="old"/></manifest>')
        original = default.read_bytes()
        resolver = unittest.mock.Mock()
        with self.assertRaisesRegex(ValueError, "No common"):
            set_kernel_ref(self.root, "", BRANCH, resolver)
        resolver.assert_not_called()
        self.assertEqual(default.read_bytes(), original)

    def test_invalid_ref_is_rejected(self):
        for ref in ["main", "refs/heads/a;echo", "refs/heads/a..b", "refs/heads/a//b", "refs/tags/.hidden", "refs/heads/a.lock", "refs/heads/a/", "refs/heads/a\\b"]:
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                validate_ref(ref)

    def test_full_commit_refs_are_accepted_without_a_network_lookup(self):
        self.write("default.xml", '<manifest><project name="common" path="common" revision="old"/></manifest>')
        for commit in ("A" * 40, "b" * 64):
            with self.subTest(commit=commit):
                resolver = unittest.mock.Mock()
                self.assertEqual(set_kernel_ref(self.root, commit, BRANCH, resolver), (commit.lower(), 1))
                resolver.assert_not_called()
                self.assertEqual(ET.parse(self.root / "default.xml").find("project").get("revision"), commit.lower())
        for bad in ("a" * 39, "a" * 41, "a" * 63, "a" * 65, "a" * 40 + "\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_ref(bad)

    def test_empty_ref_uses_resolver(self):
        self.write("default.xml", '<manifest><project path="common" revision="old"/></manifest>')
        resolver = unittest.mock.Mock(return_value=f"refs/heads/deprecated/{BRANCH}")
        self.assertEqual(set_kernel_ref(self.root, "", BRANCH, resolver), (f"refs/heads/deprecated/{BRANCH}", 1))
        resolver.assert_called_once_with(BRANCH)

    def test_explicit_named_ref_uses_only_named_resolver_and_updates_common(self):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/><project path="tools" revision="tools-ref"/></manifest>')
        resolver = unittest.mock.Mock()
        named_resolver = unittest.mock.Mock(return_value=DEPRECATED_REF)
        self.assertEqual(set_kernel_ref(self.root, REF, BRANCH, resolver, named_resolver), (DEPRECATED_REF, 1))
        named_resolver.assert_called_once_with(BRANCH, REF)
        resolver.assert_not_called()
        common, tools = ET.parse(default).getroot().findall("project")
        self.assertEqual(common.get("revision"), DEPRECATED_REF)
        self.assertEqual(tools.get("revision"), "tools-ref")

    def test_explicit_commits_tags_and_other_branches_never_resolve(self):
        self.write("default.xml", '<manifest><project path="common" revision="old"/></manifest>')
        for ref in ("A" * 40, "b" * 64, f"refs/tags/{BRANCH}_r1", "refs/heads/custom-build",
                    "refs/heads/android14-6.1-2025-08", "refs/heads/deprecated/android14-6.1-2025-08"):
            with self.subTest(ref=ref):
                resolver, named_resolver = unittest.mock.Mock(), unittest.mock.Mock()
                expected = ref.lower() if len(ref) in (40, 64) and not ref.startswith("refs/") else ref
                self.assertEqual(set_kernel_ref(self.root, ref, BRANCH, resolver, named_resolver), (expected, 1))
                resolver.assert_not_called()
                named_resolver.assert_not_called()

    def test_named_resolver_cannot_switch_month_family_or_use_tag(self):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/></manifest>')
        original = default.read_bytes()
        for ref in ("refs/heads/deprecated/android14-6.1-2025-08", "refs/heads/android13-5.15-2025-09",
                    f"refs/tags/{BRANCH}_r1", "a" * 40, "invalid"):
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "changed the selected"):
                set_kernel_ref(self.root, REF, BRANCH, named_resolver=lambda branch, preferred: ref)
            self.assertEqual(default.read_bytes(), original)
            self.assertFalse(list(self.root.glob(".gki-manifest-*")))

    def test_named_resolution_failure_does_not_mutate(self):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/></manifest>')
        original = default.read_bytes()
        for error in (ValueError("missing named refs"), subprocess.CalledProcessError(128, ["git"])):
            with self.subTest(error=type(error).__name__):
                named_resolver = unittest.mock.Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    set_kernel_ref(self.root, REF, BRANCH, named_resolver=named_resolver)
                self.assertEqual(default.read_bytes(), original)
                self.assertFalse(list(self.root.glob(".gki-manifest-*")))

    def test_resolver_failure_does_not_mutate(self):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/></manifest>')
        original = default.read_bytes()
        with self.assertRaises(ValueError):
            set_kernel_ref(self.root, "", BRANCH, lambda _: "invalid")
        self.assertEqual(default.read_bytes(), original)

    def remote(self, refs):
        return subprocess.CompletedProcess([], 0, "\n".join("a" * 40 + "\t" + ref for ref in refs), "")

    @patch("set_kernel_ref.subprocess.run")
    def test_live_preferred_to_deprecated_and_tags(self, run):
        run.return_value = self.remote([f"refs/tags/{BRANCH}_r99", f"refs/heads/deprecated/{BRANCH}", REF])
        self.assertEqual(resolve_kernel_ref(BRANCH), REF)
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("set_kernel_ref.subprocess.run")
    def test_deprecated_preferred_to_tags(self, run):
        run.return_value = self.remote([f"refs/tags/{BRANCH}_r99", f"refs/heads/deprecated/{BRANCH}"])
        self.assertEqual(resolve_kernel_ref(BRANCH), f"refs/heads/deprecated/{BRANCH}")

    @patch("set_kernel_ref.subprocess.run")
    def test_latest_tag_compares_revision_numerically(self, run):
        run.return_value = self.remote([f"refs/tags/{BRANCH}_r2", f"refs/tags/{BRANCH}_r10", f"refs/tags/{BRANCH}_r9", f"refs/tags/{BRANCH}_r100^{{}}"])
        self.assertEqual(resolve_kernel_ref(BRANCH), f"refs/tags/{BRANCH}_r10")

    @patch("set_kernel_ref.subprocess.run")
    def test_missing_remote_refs_fails(self, run):
        run.return_value = self.remote([])
        with self.assertRaisesRegex(ValueError, "No live"):
            resolve_kernel_ref(BRANCH)

    @patch("set_kernel_ref.subprocess.run")
    def test_bad_branch_never_runs_git(self, run):
        for branch in ["-bad", "android14-6.1-2025-13", "android14-6.1-2025-09;echo"]:
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                resolve_kernel_ref(branch)
        run.assert_not_called()

    @patch("set_kernel_ref.subprocess.run")
    def test_named_resolution_preserves_explicit_preference_when_both_exist(self, run):
        run.return_value = self.remote([REF, DEPRECATED_REF, f"refs/tags/{BRANCH}_r99"])
        for preferred in (REF, DEPRECATED_REF):
            with self.subTest(preferred=preferred):
                self.assertEqual(resolve_named_kernel_ref(BRANCH, preferred), preferred)
                self.assertEqual(run.call_args.args[0], ["git", "ls-remote", "--refs",
                    "https://android.googlesource.com/kernel/common", REF, DEPRECATED_REF])
                self.assertNotIn("shell", run.call_args.kwargs)

    @patch("set_kernel_ref.subprocess.run")
    def test_named_resolution_migrates_only_to_existing_same_name_alias(self, run):
        for preferred, surviving in ((REF, DEPRECATED_REF), (DEPRECATED_REF, REF)):
            with self.subTest(preferred=preferred):
                run.return_value = self.remote([surviving])
                self.assertEqual(resolve_named_kernel_ref(BRANCH, preferred), surviving)

    @patch("set_kernel_ref.time.sleep")
    @patch("set_kernel_ref.subprocess.run")
    def test_named_resolution_retries_transient_errors_with_bounded_backoff(self, run, sleep):
        run.side_effect = [subprocess.CalledProcessError(128, ["git"]),
                           subprocess.TimeoutExpired(["git"], 60), self.remote([DEPRECATED_REF])]
        self.assertEqual(resolve_named_kernel_ref(BRANCH, REF), DEPRECATED_REF)
        self.assertEqual(run.call_count, 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(2,), (4,)])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 60)
            self.assertEqual(call.args[0][-2:], [REF, DEPRECATED_REF])

    @patch("set_kernel_ref.time.sleep")
    @patch("set_kernel_ref.subprocess.run")
    def test_named_resolution_exhausted_network_errors_do_not_mutate(self, run, sleep):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/></manifest>')
        original = default.read_bytes()
        for error in (subprocess.CalledProcessError(128, ["git"]),
                      subprocess.TimeoutExpired(["git"], 60)):
            with self.subTest(error=type(error).__name__):
                run.reset_mock()
                sleep.reset_mock()
                run.side_effect = error
                with patch('set_kernel_ref._gitiles_named_ref',
                           side_effect=KernelRefTransportError('Gitiles connection failed')) as gitiles:
                    with self.assertRaises(KernelRefTransportError):
                        set_kernel_ref(self.root, REF, BRANCH)
                gitiles.assert_called_once_with(REF)
                self.assertEqual(run.call_count, 3)
                self.assertEqual([call.args for call in sleep.call_args_list], [(2,), (4,)])
                self.assertEqual(default.read_bytes(), original)
                self.assertFalse(list(self.root.glob(".gki-manifest-*")))

    @patch('set_kernel_ref.time.sleep')
    @patch('set_kernel_ref.subprocess.run')
    def test_named_git_transport_failure_uses_verified_same_official_ref(self, run, sleep):
        run.side_effect = subprocess.CalledProcessError(128, ['git'], stderr='curl 92 HTTP/2 stream error')
        with patch('set_kernel_ref._gitiles_named_ref', return_value=True) as gitiles:
            self.assertEqual(resolve_named_kernel_ref(BRANCH, REF), REF)
        gitiles.assert_called_once_with(REF)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0][:2], ['git', 'ls-remote'])
        for call in run.call_args_list[1:]:
            self.assertEqual(call.args[0][:4], ['git', '-c', 'http.version=HTTP/1.1', 'ls-remote'])
            self.assertEqual(call.args[0][-2:], [REF, DEPRECATED_REF])
        self.assertEqual([call.args for call in sleep.call_args_list], [(2,), (4,)])

    @patch('set_kernel_ref._query_git_refs', side_effect=KernelRefTransportError('Git unavailable'))
    def test_gitiles_fallback_obeys_preference_and_only_same_name_alias(self, query):
        for preferred, other in ((REF, DEPRECATED_REF), (DEPRECATED_REF, REF)):
            with self.subTest(preferred=preferred):
                with patch('set_kernel_ref._gitiles_named_ref', side_effect=[False, True]) as gitiles:
                    self.assertEqual(resolve_named_kernel_ref(BRANCH, preferred), other)
                self.assertEqual([call.args for call in gitiles.call_args_list], [(preferred,), (other,)])
        with patch('set_kernel_ref._gitiles_named_ref', return_value=False) as gitiles:
            with self.assertRaisesRegex(ValueError, 'explicitly selected'):
                resolve_named_kernel_ref(BRANCH, REF)
        self.assertEqual(gitiles.call_count, 2)

    @patch('set_kernel_ref._query_git_refs', side_effect=KernelRefTransportError('Git unavailable'))
    def test_gitiles_preferred_query_failure_never_switches_to_unverified_alias(self, query):
        default = self.write('default.xml', '<manifest><project path="common" revision="old"/></manifest>')
        original = default.read_bytes()
        for error in (KernelRefTransportError('HTTP 503'), ValueError('Invalid Gitiles JSON')):
            with self.subTest(error=type(error).__name__):
                with patch('set_kernel_ref._gitiles_named_ref', side_effect=error) as gitiles:
                    with self.assertRaises(type(error)):
                        set_kernel_ref(self.root, REF, BRANCH)
                gitiles.assert_called_once_with(REF)
                self.assertEqual(default.read_bytes(), original)

    @patch('set_kernel_ref.time.sleep')
    @patch('set_kernel_ref.subprocess.run')
    def test_unnamed_ref_query_also_retries_without_changing_exact_patterns(self, run, sleep):
        run.side_effect = [subprocess.TimeoutExpired(['git'], 60), self.remote([REF])]
        with patch('set_kernel_ref._gitiles_named_ref') as gitiles:
            self.assertEqual(resolve_kernel_ref(BRANCH), REF)
        gitiles.assert_not_called()
        self.assertEqual([call.args for call in sleep.call_args_list], [(2,)])
        self.assertEqual(run.call_args.args[0][-3:], [REF, DEPRECATED_REF, f'refs/tags/{BRANCH}_r*'])

    def gitiles_response(self, data=None, raw=None, status=200, url=None):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.geturl.return_value = url or kernel_ref.COMMON_URL + '/+' + REF + '?format=JSON'
        response.read.return_value = raw if raw is not None else b")]}'\n" + json.dumps(data).encode()
        return response

    @patch('set_kernel_ref.build_opener')
    def test_gitiles_reads_one_bounded_exact_ref_without_credentials_or_redirects(self, opener):
        response = self.gitiles_response({REF: {'value': 'a' * 40}})
        opener.return_value.open.return_value = response
        self.assertTrue(kernel_ref._gitiles_named_ref(REF))
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, kernel_ref.COMMON_URL + '/+' + REF + '?format=JSON')
        self.assertEqual(opener.return_value.open.call_args.kwargs, {'timeout': 20})
        self.assertNotIn('Authorization', dict(request.header_items()))
        self.assertIsInstance(opener.call_args.args[0], kernel_ref._NoRedirect)
        self.assertIsNone(opener.call_args.args[0].redirect_request(None, None, None, None, None, None))
        response.read.assert_called_once_with(8193)

    @patch('set_kernel_ref.build_opener')
    def test_gitiles_missing_exact_ref_is_not_a_transport_error(self, opener):
        opener.return_value.open.return_value = self.gitiles_response({})
        self.assertFalse(kernel_ref._gitiles_named_ref(REF))
        opener.return_value.open.side_effect = HTTPError('https://official.invalid', 404, 'not found', {}, None)
        self.assertFalse(kernel_ref._gitiles_named_ref(REF))

    @patch('set_kernel_ref.build_opener')
    def test_gitiles_rejects_other_names_partial_hashes_duplicates_or_oversized_payloads(self, opener):
        invalid = [self.gitiles_response({DEPRECATED_REF: {'value': 'a' * 40}}),
                   self.gitiles_response({REF + '/child': {'value': 'a' * 40}}),
                   self.gitiles_response({REF: {'value': 'a' * 41}}),
                   self.gitiles_response({REF: {'value': 'a' * 39}}),
                   self.gitiles_response({REF: {'value': 'a' * 40, 'peeled': 'b' * 40}}),
                   self.gitiles_response({REF: ['a' * 40]}),
                   self.gitiles_response([REF]),
                   self.gitiles_response(raw=b'{}'),
                   self.gitiles_response(raw=b")]}'\n" + b' ' * 8192),
                   self.gitiles_response(raw=b")]}'\n{\"duplicate\":1,\"duplicate\":2}"),
                   self.gitiles_response(raw=b")]}'\n{invalid}"),
                   self.gitiles_response({REF: {'value': 'a' * 40}}, url='https://other.invalid/'),
                   self.gitiles_response({REF: {'value': 'a' * 40}}, status=302)]
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                opener.return_value.open.return_value = response
                kernel_ref._gitiles_named_ref(REF)

    @patch('set_kernel_ref.time.sleep')
    @patch('set_kernel_ref.build_opener')
    def test_gitiles_retries_only_transient_failures_with_bounded_timeout(self, opener, sleep):
        for error in (HTTPError('https://official.invalid', 503, 'unavailable', {}, None),
                      URLError('temporary connection failure')):
            with self.subTest(error=type(error).__name__):
                opener.return_value.open.reset_mock()
                sleep.reset_mock()
                opener.return_value.open.side_effect = [error, self.gitiles_response({REF: {'value': 'a' * 40}})]
                self.assertTrue(kernel_ref._gitiles_named_ref(REF))
                self.assertEqual(opener.return_value.open.call_count, 2)
                sleep.assert_called_once_with(2)
                for call in opener.return_value.open.call_args_list:
                    self.assertEqual(call.kwargs['timeout'], 20)
        opener.return_value.open.side_effect = URLError('unavailable')
        opener.return_value.open.reset_mock()
        with self.assertRaises(KernelRefTransportError):
            kernel_ref._gitiles_named_ref(REF)
        self.assertEqual(opener.return_value.open.call_count, 2)

    @patch('set_kernel_ref.time.sleep')
    @patch('set_kernel_ref.build_opener')
    def test_gitiles_redirect_or_authorization_failure_never_means_missing(self, opener, sleep):
        for code in (301, 302, 401, 403):
            with self.subTest(code=code):
                opener.return_value.open.reset_mock()
                opener.return_value.open.side_effect = HTTPError('https://official.invalid', code, 'denied', {}, None)
                with self.assertRaises(KernelRefTransportError):
                    kernel_ref._gitiles_named_ref(REF)
                opener.return_value.open.assert_called_once()
        sleep.assert_not_called()

    @patch('set_kernel_ref.time.sleep')
    @patch('set_kernel_ref.subprocess.run')
    def test_transport_diagnostics_keep_status_codes_and_never_echo_credentials(self, run, sleep):
        error = subprocess.CalledProcessError(128, ['git'], stderr=(
            'fatal: https://user:private-password@proxy.invalid/?token=private-token\n'
            'error: RPC failed; HTTP 503 curl 22 TLS connection reset\n'))
        run.side_effect = error
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(KernelRefTransportError) as failure:
            kernel_ref._query_git_refs((REF, DEPRECATED_REF))
        diagnosis = output.getvalue() + str(failure.exception)
        for expected in ('Git transport exit 128', 'HTTP 503', 'curl 22', 'TLS failure', 'connection reset'):
            self.assertIn(expected, diagnosis)
        for secret in ('private-password', 'private-token', 'proxy.invalid', 'https://'):
            self.assertNotIn(secret, diagnosis)

    @patch("set_kernel_ref.time.sleep")
    @patch("set_kernel_ref.subprocess.run")
    def test_named_resolution_empty_remote_response_does_not_retry_or_mutate(self, run, sleep):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/></manifest>')
        original = default.read_bytes()
        run.return_value = self.remote([])
        with self.assertRaisesRegex(ValueError, "explicitly selected"):
            set_kernel_ref(self.root, REF, BRANCH)
        run.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(default.read_bytes(), original)

    @patch("set_kernel_ref.subprocess.run")
    def test_named_resolution_never_falls_back_to_tags_or_other_branches(self, run):
        for refs in ([], [f"refs/tags/{BRANCH}_r99"], ["refs/heads/android14-6.1-2025-08"]):
            with self.subTest(refs=refs):
                run.return_value = self.remote(refs)
                with self.assertRaisesRegex(ValueError, "explicitly selected"):
                    resolve_named_kernel_ref(BRANCH, REF)

    @patch("set_kernel_ref.subprocess.run")
    def test_named_resolution_requires_valid_full_remote_commit_id(self, run):
        for revision in ("a" * 39, "a" * 41, "a" * 63, "z" * 40):
            with self.subTest(revision=revision):
                run.return_value = subprocess.CompletedProcess([], 0, revision + "\t" + REF, "")
                with self.assertRaises(ValueError):
                    resolve_named_kernel_ref(BRANCH, REF)
        run.return_value = subprocess.CompletedProcess([], 0, "B" * 64 + "\t" + REF, "")
        self.assertEqual(resolve_named_kernel_ref(BRANCH, REF), REF)

    @patch("set_kernel_ref.subprocess.run")
    def test_named_resolution_rejects_mismatched_input_before_git(self, run):
        for preferred in (f"refs/tags/{BRANCH}_r1", "refs/heads/android14-6.1-2025-08", "a" * 40):
            with self.subTest(preferred=preferred), self.assertRaises(ValueError):
                resolve_named_kernel_ref(BRANCH, preferred)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
