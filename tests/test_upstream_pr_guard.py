import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import upstream_pr_guard as guard


REPOSITORY = "owner/gki"
HEADS = "<!-- gki-official-heads:sha256=" + "a" * 64 + " -->"
OTHER_HEADS = "<!-- gki-official-heads:sha256=" + "b" * 64 + " -->"


def pr(state="open", body=None, repository=REPOSITORY, number=1):
    return {"number": number, "state": state,
            "body": guard.OWNERSHIP_MARKER + "\n" + HEADS if body is None else body,
            "head": {"ref": guard.BRANCH, "repo": {"full_name": repository}},
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
            "html_url": f"https://github.com/{REPOSITORY}/pull/{number}"}


class GuardTests(unittest.TestCase):
    def fetcher(self, exists, prs):
        def fetch(path):
            if "/git/ref/heads/" in path:
                if not exists:
                    raise guard.ApiNotFound("not found")
                return {"ref": "refs/heads/" + guard.BRANCH}
            return prs
        return Mock(side_effect=fetch)

    def test_absent_branch_and_no_open_pr_is_safe(self):
        result = guard.check_branch(REPOSITORY, HEADS, fetch=self.fetcher(False, []))
        self.assertEqual(result, guard.GuardResult(True, False, False))

    def test_absent_branch_ignores_closed_unmarked_history(self):
        result = guard.check_branch(REPOSITORY, HEADS,
                                    fetch=self.fetcher(False, [pr("closed", body="human PR")]))
        self.assertTrue(result.safe_to_update)

    def test_existing_unassociated_branch_refused(self):
        with self.assertRaisesRegex(guard.GuardError, "without an associated"):
            guard.check_branch(REPOSITORY, HEADS, fetch=self.fetcher(True, []))

    def test_same_open_candidate_is_already_pending(self):
        result = guard.check_branch(REPOSITORY, HEADS, fetch=self.fetcher(True, [pr()]))
        self.assertTrue(result.already_pending)
        self.assertEqual(result.pull_request_url, "https://github.com/owner/gki/pull/1")

    def test_changed_open_candidate_can_update(self):
        result = guard.check_branch(REPOSITORY, OTHER_HEADS, fetch=self.fetcher(True, [pr()]))
        self.assertFalse(result.already_pending)
        self.assertTrue(result.safe_to_update)

    def test_closed_and_merged_owned_pr_allow_reuse(self):
        for merged in (False, True):
            previous = pr("closed")
            previous["merged_at"] = "2026-10-21T00:00:00Z" if merged else None
            with self.subTest(merged=merged):
                result = guard.check_branch(REPOSITORY, HEADS, fetch=self.fetcher(True, [previous]))
                self.assertFalse(result.already_pending)
                self.assertTrue(result.safe_to_update)

    def test_unmarked_or_foreign_pr_is_refused(self):
        for previous in [pr(body=HEADS), pr(repository="other/gki"), pr(repository=""),
                         {**pr(), "head": {"ref": guard.BRANCH, "repo": None}}]:
            with self.subTest(previous=previous), self.assertRaisesRegex(guard.GuardError, "unmarked or foreign"):
                guard.check_branch(REPOSITORY, HEADS, fetch=self.fetcher(True, [previous]))

    def test_open_foreign_pr_refused_when_branch_absent(self):
        with self.assertRaises(guard.GuardError):
            guard.check_branch(REPOSITORY, HEADS, fetch=self.fetcher(False, [pr(repository="foreign/gki")]))

    def test_any_other_base_pr_is_refused(self):
        for exists in (False, True):
            for state in ("open", "closed"):
                other_base = pr(state)
                other_base["base"]["ref"] = "release"
                with self.subTest(exists=exists, state=state), self.assertRaisesRegex(guard.GuardError, "different base"):
                    guard.check_branch(REPOSITORY, HEADS, fetch=self.fetcher(exists, [other_base]))

    def test_correct_base_pr_cannot_hide_another_base_pr(self):
        other_base = pr("closed", number=2)
        other_base["base"]["ref"] = "release"
        with self.assertRaisesRegex(guard.GuardError, "different base"):
            guard.check_branch(REPOSITORY, HEADS, fetch=self.fetcher(True, [pr(), other_base]))

    def test_open_owned_pr_with_absent_branch_can_be_pending(self):
        result = guard.check_branch(REPOSITORY, HEADS, fetch=self.fetcher(False, [pr()]))
        self.assertTrue(result.already_pending)

    def test_invalid_user_inputs_never_fetch(self):
        fetch = Mock()
        for repository, marker, base in [("owner/repo/extra", HEADS, "main"),
                                          ("https://evil.example/repo", HEADS, "main"),
                                          ("owner/repo?token=x", HEADS, "main"),
                                          (REPOSITORY, "bad marker", "main"),
                                          (REPOSITORY, HEADS, "main\nanything"),
                                          (REPOSITORY, HEADS, "../other")]:
            with self.subTest(repository=repository, marker=marker, base=base), self.assertRaises(guard.GuardError):
                guard.check_branch(repository, marker, base, fetch)
        fetch.assert_not_called()

    def test_non_404_api_failure_is_not_absent_branch(self):
        with self.assertRaisesRegex(guard.GuardError, "HTTP 403"):
            guard.check_branch(REPOSITORY, HEADS, fetch=Mock(side_effect=guard.GuardError("HTTP 403")))

    def test_pr_query_is_scoped_and_url_encoded(self):
        fetch = self.fetcher(False, [])
        guard.check_branch(REPOSITORY, HEADS, fetch=fetch)
        path = fetch.call_args_list[1].args[0]
        self.assertTrue(path.startswith("/repos/owner/gki/pulls?"))
        query = parse_qs(urlsplit(path).query)
        self.assertEqual(query["head"], ["owner:automation/upstream-sync"])
        self.assertNotIn("base", query)
        self.assertEqual(query["state"], ["all"])

    def test_paginated_prs_are_inspected(self):
        responses = [{"ref": "refs/heads/" + guard.BRANCH},
                     [pr("closed", number=n) for n in range(1, 101)], [pr(number=101)]]
        fetch = Mock(side_effect=responses)
        result = guard.check_branch(REPOSITORY, HEADS, fetch=fetch)
        self.assertTrue(result.already_pending)
        self.assertEqual(result.pull_request_url, "https://github.com/owner/gki/pull/101")
        self.assertIn("page=2", fetch.call_args.args[0])

    def test_output_contains_only_booleans_and_public_pr_url(self):
        root = ROOT / "tests" / f".pr-guard-{uuid.uuid4().hex}"
        root.mkdir()
        self.addCleanup(shutil.rmtree, root)
        output = root / "output.txt"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            guard.write_outputs(guard.GuardResult(True, True, True, "https://github.com/owner/gki/pull/1"), output)
        values = json.loads(stdout.getvalue())
        self.assertIs(values["already_pending"], True)
        self.assertIn("already_pending=true\n", output.read_text(encoding="utf-8"))
        self.assertNotIn(HEADS, output.read_text(encoding="utf-8"))

    def test_response_html_url_cannot_inject_output(self):
        previous = pr()
        previous["html_url"] = "https://evil.example/\nGH_TOKEN=secret"
        result = guard.check_branch(REPOSITORY, HEADS, fetch=self.fetcher(True, [previous]))
        self.assertEqual(result.pull_request_url, "https://github.com/owner/gki/pull/1")


class ReaderTests(unittest.TestCase):
    def test_token_is_only_sent_to_api_github(self):
        opener = Mock()
        response = io.BytesIO(b'{"ok":true}')
        opener.open.return_value = response
        reader = guard.GitHubReader("secret-token", opener)
        self.assertEqual(reader.get("/repos/owner/gki/pulls?state=all"), {"ok": True})
        request = opener.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).netloc, "api.github.com")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        for path in ["https://evil.example/repos/owner/gki", "//evil.example/repos/x/y", "/user", "/repos/x/y\n",
                     "/repos/owner/gki/../../user", "/repos/owner/gki/pulls#fragment",
                     "/repos/owner/gki/%2e%2e/secrets"]:
            with self.subTest(path=path), self.assertRaises(guard.GuardError):
                reader.get(path)
        self.assertEqual(opener.open.call_count, 1)

    def test_redirects_are_blocked(self):
        with self.assertRaisesRegex(guard.GuardError, "redirects"):
            guard.NoRedirect().redirect_request(None, None, 302, "Moved", {}, "https://evil.example")

    def test_only_404_maps_to_not_found(self):
        for code, expected in [(404, guard.ApiNotFound), (401, guard.GuardError), (403, guard.GuardError), (500, guard.GuardError)]:
            opener = Mock()
            opener.open.side_effect = HTTPError("https://api.github.com/repos/owner/gki/pulls", code, "error", {}, None)
            with self.subTest(code=code), self.assertRaises(expected):
                guard.GitHubReader("secret", opener).get("/repos/owner/gki/pulls")


if __name__ == "__main__":
    unittest.main()
