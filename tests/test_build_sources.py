import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import resolve_build_sources as sources

KSU = "c" * 40
NEW_KSU = "d" * 40
REPO = "SukiSU-Ultra/SukiSU-Ultra"
PROFILE = {"schema_version": 1, "sukisu_susfs": {
    "ksu_commits": [KSU], "tiers": ["Stable(标准)", "Dev(开发)"],
    "version": "v2.2.0", "pins": {"android14-6.1": "a" * 40}}}


class BuildSourceTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "tests" / (".sources-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.profile = self.root / "build-compatibility.json"
        self.write_profile(PROFILE)
        self.output = self.root / "outputs"
        environment = patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.output),
                                             "GH_TOKEN": "", "GITHUB_TOKEN": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def write_profile(self, value):
        self.profile.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def resolve(self, **overrides):
        values = dict(root=self.root, variant="SukiSU", tier="Dev(开发)", susfs=True)
        values.update(overrides)
        return sources.resolve_sources(**values)

    def mock_upstream(self, ref=KSU, repository=REPO, branch="", tier="Dev(开发)", extra=""):
        def run(arguments, **kwargs):
            text = (f"BRANCH={ref}\nRESOLVED_KSU_SHA={ref}\nRESOLVED_KSU_REPO={repository}\n"
                    f"RESOLVED_KSU_SOURCE_BRANCH={branch}\nEFFECTIVE_KSU_BRANCH={tier}\n{extra}")
            Path(kwargs["env"]["GITHUB_ENV"]).write_text(text, encoding="utf-8")
            return subprocess.CompletedProcess(arguments, 0, "upstream-public-message", "")
        return run

    def test_compatible_profile_selects_first_full_commit_without_shell_or_network(self):
        with patch.object(sources.subprocess, "run") as run, patch.object(sources, "github_commit") as lookup:
            for tier in ("Stable(标准)", "Dev(开发)"):
                with self.subTest(tier=tier):
                    self.assertEqual(self.resolve(tier=tier), {
                        "ksu_ref": KSU, "ksu_repo": REPO, "ksu_short": KSU[:7],
                        "ksu_source_branch": "", "effective_ksu_branch": tier})
        run.assert_not_called()
        lookup.assert_not_called()

    def test_profile_source_ref_takes_precedence_and_is_normalized(self):
        profile = copy.deepcopy(PROFILE)
        profile["sukisu_susfs"]["source_ref"] = NEW_KSU.upper()
        self.write_profile(profile)
        with patch.object(sources.subprocess, "run") as run, patch.object(sources, "github_commit") as lookup:
            self.assertEqual(self.resolve()["ksu_ref"], NEW_KSU)
        run.assert_not_called()
        lookup.assert_not_called()

    def test_invalid_source_ref_fails_without_shell_or_network(self):
        for ref in ("main", NEW_KSU + "\n", None):
            profile = copy.deepcopy(PROFILE)
            profile["sukisu_susfs"]["source_ref"] = ref
            self.write_profile(profile)
            with self.subTest(ref=ref), patch.object(sources.subprocess, "run") as run:
                with self.assertRaisesRegex(sources.SourceResolutionError, "source_ref"):
                    self.resolve()
                run.assert_not_called()

    def test_explicit_custom_configuration_bypasses_even_missing_profile(self):
        config = self.root / "config/config"
        config.parent.mkdir()
        config.write_text(" custom = TRUE # selected manually\n", encoding="utf-8")
        self.profile.unlink()
        with patch.object(sources.subprocess, "run", side_effect=self.mock_upstream(NEW_KSU)) as run:
            self.assertEqual(self.resolve()["ksu_ref"], NEW_KSU)
        run.assert_called_once()

    def test_none_needs_no_profile_shell_or_network(self):
        self.profile.unlink()
        with patch.object(sources.subprocess, "run") as run, patch.object(sources, "github_commit") as lookup:
            result = self.resolve(variant="None")
        self.assertEqual(result, {"ksu_ref": "", "ksu_repo": "", "ksu_short": "",
                                  "ksu_source_branch": "", "effective_ksu_branch": "Dev(开发)"})
        run.assert_not_called()
        lookup.assert_not_called()

    def test_other_variants_tiers_and_disabled_susfs_use_upstream_without_profile(self):
        self.profile.unlink()
        for variant, tier, susfs in (("Official", "Stable(标准)", True), ("BakaSU", "Dev(开发)", True),
                                     ("SukiSU", "Latest(最新)", True), ("SukiSU", "Custom(自定义)", True),
                                     ("SukiSU", "Dev(开发)", False)):
            repository = sources.REPOSITORIES[variant]
            with self.subTest(variant=variant, tier=tier, susfs=susfs):
                with patch.object(sources.subprocess, "run", side_effect=self.mock_upstream(
                        KSU.upper(), repository=repository, tier=tier)) as run:
                    with patch.object(sources, "github_commit") as lookup:
                        result = self.resolve(variant=variant, tier=tier, susfs=susfs)
                self.assertEqual(result["ksu_ref"], KSU)
                self.assertEqual(result["ksu_repo"], repository)
                lookup.assert_not_called()
                arguments, = run.call_args.args
                self.assertEqual(arguments, ["bash", str(self.root / ".github/scripts/resolve-ksu-ref.sh")])
                self.assertNotIn("shell", run.call_args.kwargs)
                environment = run.call_args.kwargs["env"]
                self.assertEqual(environment["KSU_VARIANT"], variant)
                self.assertEqual(environment["KSU_BRANCH"], tier)
                self.assertEqual(environment["CUSTOM_REF"], "")
                self.assertNotIn("GITHUB_OUTPUT", environment)
                self.assertFalse(Path(environment["GITHUB_ENV"]).exists())

    def test_named_ref_is_looked_up_once_in_allowlisted_repository(self):
        with patch.object(sources.subprocess, "run", side_effect=self.mock_upstream(
                "feature/manager", branch="main", tier="Custom(自定义)")) as run:
            with patch.object(sources, "github_commit", return_value=NEW_KSU) as lookup:
                result = self.resolve(tier="Custom(自定义)", custom_ref="feature/manager:2")
        lookup.assert_called_once_with(REPO, "feature/manager")
        self.assertEqual(run.call_args.kwargs["env"]["CUSTOM_REF"], "feature/manager:2")
        self.assertEqual(result["ksu_ref"], NEW_KSU)
        self.assertEqual(result["ksu_source_branch"], "main")
        self.assertEqual(result["effective_ksu_branch"], "Custom(自定义)")

    def test_invalid_inputs_do_not_start_upstream(self):
        with patch.object(sources.subprocess, "run") as run:
            for changes in ({"variant": "other/repo"}, {"tier": "main\n"},
                            *({"custom_ref": ref} for ref in ("main\nINJECT=true", "main;echo", "a..b", "-main",
                                                                "main\r", "main\t", "a\\b", "main:0", "a:1:2"))):
                with self.subTest(changes=changes), self.assertRaises(sources.SourceResolutionError):
                    self.resolve(**changes)
        run.assert_not_called()

    def test_invalid_emitted_environment_is_rejected(self):
        for text in ("", "BRANCH=main\nUNKNOWN=value\n", "BRANCH=main\nBRANCH=other\n",
                     "BRANCH<<EOF\nmain\nEOF\n", "BRANCH=main\t\n", "BRANCH=main\x00\n",
                     "BRANCH=main\r", "BRANCH=main\x0b\n", "BRANCH=bad ref\n",
                     "BRANCH=main\nRESOLVED_KSU_SHA=other\n", "BRANCH=main\nRESOLVED_KSU_SOURCE_BRANCH=../escape\n"):
            with self.subTest(text=text), self.assertRaises(sources.SourceResolutionError):
                sources.parse_environment(text)
        self.assertEqual(sources.parse_environment("BRANCH=main\r\n"), {"BRANCH": "main"})

    def test_unexpected_repository_and_effective_tier_never_reach_network(self):
        for repository, tier in (("attacker/KernelSU", "Dev(开发)"), (REPO, "Injected")):
            with self.subTest(repository=repository, tier=tier):
                with patch.object(sources.subprocess, "run", side_effect=self.mock_upstream(
                        "main", repository=repository, tier=tier)), patch.object(sources, "github_commit") as lookup:
                    with self.assertRaises(sources.SourceResolutionError):
                        self.resolve(susfs=False)
                lookup.assert_not_called()

    def test_upstream_failure_is_sanitized_and_emits_no_job_outputs(self):
        failure = subprocess.CompletedProcess([], 1, "token-secret", "secret-from-upstream")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sources.subprocess, "run", return_value=failure):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = sources.main(["--root", str(self.root), "--variant", "Official",
                                       "--tier", "Dev(开发)", "--susfs", "false"])
        self.assertEqual(status, 1)
        self.assertIn("exit 1", stderr.getvalue())
        self.assertNotIn("secret", stdout.getvalue() + stderr.getvalue())
        self.assertFalse(self.output.exists())

    def test_cli_writes_only_public_shared_source_outputs(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = sources.main(["--root", str(self.root), "--variant", "SukiSU",
                                   "--tier", "Dev(开发)", "--susfs", "true"])
        self.assertEqual(status, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"),
                         f"ksu_ref={KSU}\nksu_repo={REPO}\nksu_short={KSU[:7]}\n"
                         "ksu_source_branch=\neffective_ksu_branch=Dev(开发)\n")
        self.assertIn(f"{REPO}@{KSU}", stdout.getvalue())


class GitHubSourceLookupTests(unittest.TestCase):
    def api_response(self, ref="feature/manager", sha=NEW_KSU):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.geturl.return_value = f"https://api.github.com/repos/{REPO}/commits/{ref.replace('/', '%2F')}"
        response.read.return_value = json.dumps({"sha": sha}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        return response, opener

    def test_fixed_https_api_url_encodes_ref_and_uses_optional_token(self):
        response, opener = self.api_response(sha=NEW_KSU.upper())
        with patch.dict(os.environ, {"GH_TOKEN": "private-token", "GITHUB_TOKEN": "other-token"}):
            with patch.object(sources, "build_opener", return_value=opener) as build:
                self.assertEqual(sources.github_commit(REPO, "feature/manager"), NEW_KSU)
        request, = opener.open.call_args.args
        self.assertEqual(request.full_url, f"https://api.github.com/repos/{REPO}/commits/feature%2Fmanager")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertIsInstance(build.call_args.args[0], sources.NoRedirect)

    def test_no_token_means_no_authorization_header(self):
        _, opener = self.api_response()
        with patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}):
            with patch.object(sources, "build_opener", return_value=opener):
                sources.github_commit(REPO, "feature/manager")
        self.assertIsNone(opener.open.call_args.args[0].get_header("Authorization"))

    def test_redirect_handler_rejects_every_destination(self):
        for destination in ("https://api.github.com/elsewhere", "https://example.com/steal", "http://api.github.com/ref"):
            with self.subTest(destination=destination), self.assertRaisesRegex(sources.SourceResolutionError, "redirects"):
                sources.NoRedirect().redirect_request(None, None, 302, "Found", {}, destination)

    def test_unexpected_response_urls_are_rejected_before_reading(self):
        for url in ("http://api.github.com/repos/ref", "https://api.github.com.evil.test/ref",
                    "https://user@api.github.com/ref", "https://api.github.com/elsewhere"):
            response, opener = self.api_response()
            response.geturl.return_value = url
            with self.subTest(url=url), patch.object(sources, "build_opener", return_value=opener):
                with self.assertRaisesRegex(sources.SourceResolutionError, "unexpected URL"):
                    sources.github_commit(REPO, "feature/manager")
            response.read.assert_not_called()

    def test_invalid_api_results_fail_without_ref_or_token_in_errors(self):
        for document in ({"sha": "main"}, {"sha": NEW_KSU + "\n"}, {"sha": None}, [], {}):
            response, opener = self.api_response()
            response.read.return_value = json.dumps(document).encode()
            with self.subTest(document=document), patch.object(sources, "build_opener", return_value=opener):
                with self.assertRaisesRegex(sources.SourceResolutionError, "full commit SHA"):
                    sources.github_commit(REPO, "feature/manager")
        error = HTTPError("https://secret-url", 302, "private-token", {}, None)
        with patch.object(sources, "build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(sources.SourceResolutionError) as raised:
                sources.github_commit(REPO, "feature/manager")
        self.assertNotIn("private-token", str(raised.exception))
        self.assertNotIn("secret-url", str(raised.exception))

    def test_unallowlisted_repository_and_bad_auth_never_open_network(self):
        with patch.object(sources, "build_opener") as build:
            with self.assertRaises(sources.SourceResolutionError):
                sources.github_commit("attacker/KernelSU", "main")
            with patch.dict(os.environ, {"GH_TOKEN": "token\nInjected: header"}):
                with self.assertRaises(sources.SourceResolutionError):
                    sources.github_commit(REPO, "main")
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
