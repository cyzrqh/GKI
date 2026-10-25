import base64
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import matrix
import update_kernel_catalog as catalog


def makefile(family="6.1", sublevel=999):
    major, minor = family.split(".")
    return f"VERSION = {major}\nPATCHLEVEL = {minor}\nSUBLEVEL = {sublevel}\nEXTRAVERSION =\n"


class FakeProvider:
    def __init__(self):
        self.repositories = {"manifest": {}, "common": {}}
        self.tag_refs = {}
        self.makefiles = {}
        self.reads = []

    def branch(self, android="android14", family="6.1", month="2026-12", sublevel=999,
               commit="a" * 40, deprecated=False, manifest=True, common=True):
        prefix = "deprecated/" if deprecated else ""
        name = f"{android}-{family}-{month}"
        if manifest:
            self.repositories["manifest"][f"refs/heads/{prefix}common-{name}"] = commit
        if common:
            self.repositories["common"][f"refs/heads/{prefix}{name}"] = commit
        self.makefiles[commit] = makefile(family, sublevel)
        return self

    def refs(self, repository):
        self.reads.append(("refs", repository))
        return self.repositories[repository]

    def makefile(self, commit):
        self.reads.append(("makefile", commit))
        return self.makefiles[commit]

    def tags(self):
        self.reads.append(("tags", "common"))
        return self.tag_refs


class KernelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "tests" / (".official-catalog-" + uuid.uuid4().hex)
        (self.root / "data").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.path = self.root / "data/kernels.json"
        shutil.copyfile(ROOT / "data/kernels.json", self.path)
        self.original = self.path.read_bytes()
        self.total = len(json.loads(self.original)["entries"])
        self.stable_count = len(matrix.build_matrix(self.root))
        self.lts_count = len(matrix.build_matrix(self.root, include_lts=True))

    def test_check_discovers_new_official_month_without_writing(self):
        provider = FakeProvider().branch()
        report = catalog.update_catalog(self.root, provider)
        self.assertEqual({key: report[key] for key in ("has_updates", "added", "total")},
                         {"has_updates": True, "added": 1, "total": self.total + 1})
        candidates = json.loads(report["candidates_json"])
        self.assertEqual(candidates[0]["ref"], "a" * 40)
        self.assertEqual(candidates[0]["kernel"], "6.1.999")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(provider.reads, [("refs", "manifest"), ("refs", "common"), ("makefile", "a" * 40)])

    def test_update_appends_preserves_every_known_month_lts_ref_and_revision(self):
        provider = FakeProvider().branch().branch(month="2027-01", commit="b" * 40)
        report = catalog.update_catalog(self.root, provider, write=True)
        self.assertEqual(report["added"], 2)
        before = json.loads(self.original)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(after["entries"][:self.total], before["entries"])
        added = after["entries"][self.total:]
        self.assertEqual([entry["date"] for entry in added], ["2026-12", "2027-01"])
        self.assertEqual([entry["kernel"] for entry in added], ["6.1.999", "6.1.999"])
        self.assertEqual([entry["date"] for entry in after["entries"]].count("lts"), 5)
        self.assertEqual(len(matrix.build_matrix(self.root)), self.stable_count + 1)
        self.assertEqual(len(matrix.build_matrix(self.root, include_lts=True)), self.lts_count + 1)
        latest = matrix.build_matrix(self.root, targets="6.1.999")
        self.assertEqual(len(latest), 1)
        self.assertEqual((latest[0]["os_patch_level"], latest[0]["kernel_ref"]), ("2027-01", "b" * 40))
        self.assertEqual(catalog.update_catalog(self.root, provider, write=True),
                         {"has_updates": False, "added": 0, "total": self.total + 2, "candidates_json": "[]"})

    def test_existing_monthly_identity_keeps_its_original_release_tag(self):
        provider = FakeProvider().branch(month="2025-08", sublevel=145)
        report = catalog.update_catalog(self.root, provider, write=True)
        self.assertFalse(report["has_updates"])
        self.assertEqual(self.path.read_bytes(), self.original)
        historical = next(entry for entry in matrix.load_catalog(self.root)["entries"]
                          if entry["kernel"] == "6.1.145" and entry["date"] == "2025-08")
        self.assertTrue(historical["ref"].startswith("refs/tags/"))
        latest = matrix.build_matrix(self.root, targets="6.1.145")
        self.assertEqual([row["os_patch_level"] for row in latest], ["2025-09"])

    def test_new_month_for_existing_version_replaces_build_and_preserves_history(self):
        before = matrix.build_matrix(self.root, targets="6.1.145")[0]
        provider = FakeProvider().branch(month="2027-01", sublevel=145)
        report = catalog.update_catalog(self.root, provider, write=True)
        self.assertEqual((report["added"], report["total"]), (1, self.total + 1))
        self.assertEqual(len(matrix.build_matrix(self.root)), self.stable_count)
        latest = matrix.build_matrix(self.root, targets="6.1.145")
        self.assertEqual(len(latest), 1)
        self.assertEqual((latest[0]["os_patch_level"], latest[0]["kernel_ref"]), ("2027-01", "a" * 40))
        history = matrix.load_catalog(self.root)["entries"]
        self.assertEqual(history[:self.total], json.loads(self.original)["entries"])
        self.assertIn(before["os_patch_level"], [entry["date"] for entry in history
                                               if entry["kernel"] == "6.1.145"])
        self.assertFalse(catalog.update_catalog(self.root, provider)["has_updates"])

    def test_new_sublevel_in_an_existing_month_is_preserved_as_another_choice(self):
        provider = FakeProvider().branch(month="2025-08", sublevel=999)
        self.assertEqual(catalog.update_catalog(self.root, provider, write=True)["added"], 1)
        rows = matrix.build_matrix(self.root, targets="6.1.145,6.1.999@2025-08")
        self.assertEqual([(row["sub_level"], row["os_patch_level"]) for row in rows],
                         [("145", "2025-09"), ("999", "2025-08")])

    def test_only_months_with_both_manifest_and_common_branches_are_added(self):
        provider = FakeProvider().branch()
        provider.branch(month="2027-01", common=False, commit="b" * 40)
        provider.branch(month="2027-02", manifest=False, commit="c" * 40)
        _, added = catalog.discover_updates(self.root, provider)
        self.assertEqual([entry["date"] for entry in added], ["2026-12"])
        self.assertNotIn(("makefile", "b" * 40), provider.reads)
        self.assertNotIn(("makefile", "c" * 40), provider.reads)

    def test_live_branch_wins_over_deprecated_regardless_of_enumeration_order(self):
        for active_first in (True, False):
            with self.subTest(active_first=active_first):
                provider = FakeProvider()
                order = (False, True) if active_first else (True, False)
                for deprecated in order:
                    provider.branch(deprecated=deprecated, commit=("b" if deprecated else "a") * 40,
                                    sublevel=998 if deprecated else 999)
                _, added = catalog.discover_updates(self.root, provider)
                self.assertEqual(added[0]["kernel"], "6.1.999")
                self.assertEqual("a" * 40, added[0]["ref"])

    def test_deprecated_months_and_all_five_fixed_android_kernel_pairs_are_supported(self):
        provider = FakeProvider()
        for index, (family, android) in enumerate(matrix.KERNELS.items()):
            provider.branch(android, family, deprecated=True, commit=str(index + 1) * 40)
        _, added = catalog.discover_updates(self.root, provider)
        self.assertEqual(len(added), 5)
        self.assertEqual({entry["ref"] for entry in added}, {str(index) * 40 for index in range(1, 6)})
        self.assertEqual({(entry["android_version"], entry["kernel_version"]) for entry in added},
                         {(android, family) for family, android in matrix.KERNELS.items()})

    def test_untracked_branch_pair_is_not_guessed_and_makefile_mismatch_aborts(self):
        provider = FakeProvider().branch()
        provider.repositories["manifest"]["refs/heads/common-android14-6.6-2026-12"] = "b" * 40
        provider.repositories["common"]["refs/heads/android14-6.6-2026-12"] = "b" * 40
        self.assertEqual(catalog.update_catalog(self.root, provider)["added"], 1)
        self.assertNotIn(("makefile", "b" * 40), provider.reads)
        provider = FakeProvider().branch()
        provider.makefiles["a" * 40] = makefile("6.6", 999)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            catalog.update_catalog(self.root, provider, write=True)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_bad_dates_missing_or_ambiguous_versions_and_empty_sources_fail(self):
        for provider in (FakeProvider(), FakeProvider().branch(month="2026-13")):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                catalog.discover_updates(self.root, provider)
        for content in ("VERSION = 6\nPATCHLEVEL = 1\n", makefile() + "SUBLEVEL = 888\n",
                        makefile() + "SUBLEVEL += 1\n",
                        "VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = $(shell bad)\n"):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "version fields"):
                catalog.makefile_version(content, "6.1")

    def test_unknown_future_pairs_are_ignored_and_unmatched_sources_are_explicit(self):
        provider = FakeProvider().branch().branch("android17", "6.18", commit="c" * 40)
        provider.branch("android13", "5.10", commit="d" * 40)
        provider.branch("android14", "5.15", commit="e" * 40)
        self.assertEqual(catalog.update_catalog(self.root, provider)["added"], 1)
        provider = FakeProvider().branch(common=False)
        provider.repositories["common"]["refs/heads/main"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "No matching monthly branches"):
            catalog.discover_updates(self.root, provider)

    def test_full_commits_are_required_and_identical_commits_are_read_once(self):
        provider = FakeProvider().branch().branch(month="2027-01")
        self.assertEqual(catalog.update_catalog(self.root, provider)["added"], 2)
        self.assertEqual(provider.reads.count(("makefile", "a" * 40)), 1)
        provider.repositories["common"]["refs/heads/android14-6.1-2026-12"] = "main"
        with self.assertRaisesRegex(ValueError, "invalid ref/commit"):
            catalog.discover_updates(self.root, provider)

    def test_check_update_and_github_outputs_cli_contract(self):
        provider = FakeProvider().branch()
        output = self.root / "outputs"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = catalog.main(["--root", str(self.root), "--check", "--github-output", str(output)], provider=provider)
        self.assertEqual(status, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual({key: report[key] for key in ("has_updates", "added", "total")},
                         {"has_updates": True, "added": 1, "total": self.total + 1})
        self.assertEqual(output.read_text(encoding="utf-8"),
                         f"has_updates=true\nadded=1\ntotal={self.total + 1}\ncandidates_json={report['candidates_json']}\n")
        self.assertEqual(self.path.read_bytes(), self.original)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(catalog.main(["--root", str(self.root), "--update"], provider=provider), 0)
        self.assertNotEqual(self.path.read_bytes(), self.original)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = catalog.main(["--root", str(self.root)], provider=FakeProvider())
        self.assertEqual(status, 1)
        self.assertIn("empty or invalid", stderr.getvalue())

    def test_probe_candidates_are_applied_offline_without_observing_later_ref_moves(self):
        provider = FakeProvider().branch()
        probed = catalog.update_catalog(self.root, provider)["candidates_json"]
        provider.branch(commit="b" * 40, sublevel=1000)
        with patch.object(catalog, "OfficialProvider") as network, redirect_stdout(io.StringIO()):
            self.assertEqual(catalog.main(["--root", str(self.root), "--apply-candidates-json", probed]), 0)
        network.assert_not_called()
        rows = matrix.build_matrix(self.root, targets="6.1.999@2026-12")
        self.assertEqual(rows[0]["kernel_ref"], "a" * 40)
        result = catalog.apply_candidates(self.root, json.loads(probed))
        self.assertFalse(result["has_updates"])
        self.assertEqual(result["candidates_json"], "[]")

    def test_invalid_offline_candidates_fail_before_writing(self):
        provider = FakeProvider().branch()
        candidate = json.loads(catalog.update_catalog(self.root, provider)["candidates_json"])[0]
        invalid = [None, {}, [None], [candidate, candidate], [{**candidate, "ref": "refs/heads/android14-6.1-2026-12"}],
                   [{**candidate, "ref": "a" * 39}], [{**candidate, "date": "lts"}],
                   [{**candidate, "android_version": "android15"}],
                   [{**candidate, "kernel": "6.6.999"}], [{**candidate, "extra": "injected"}],
                   [{key: value for key, value in candidate.items() if key != "revision"}]]
        for candidates in invalid:
            with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                catalog.apply_candidates(self.root, candidates)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_incremental_snapshot_reuses_unchanged_commit_without_losing_unmerged_candidates(self):
        snapshot = self.root / "cache/refs.json"
        provider = FakeProvider().branch()
        first = catalog.update_catalog(self.root, provider, snapshot_file=snapshot)
        self.assertEqual(first["added"], 1)
        self.assertEqual(json.loads(snapshot.read_text())["versions"], {"a" * 40: "6.1.999"})
        provider.reads.clear()
        second = catalog.update_catalog(self.root, provider, snapshot_file=snapshot)
        self.assertEqual(second, first)
        self.assertEqual(provider.reads, [("refs", "manifest"), ("refs", "common")])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_same_month_changed_head_is_verified_and_new_sublevel_remains_a_choice(self):
        snapshot = self.root / "refs.json"
        provider = FakeProvider().branch(month="2025-08", sublevel=145)
        self.assertFalse(catalog.update_catalog(self.root, provider, snapshot_file=snapshot)["has_updates"])
        provider.branch(month="2025-08", sublevel=999, commit="b" * 40)
        provider.reads.clear()
        report = catalog.update_catalog(self.root, provider, snapshot_file=snapshot, write=True)
        self.assertEqual(report["added"], 1)
        self.assertEqual([item for item in provider.reads if item[0] == "makefile"], [("makefile", "b" * 40)])
        rows = matrix.build_matrix(self.root, targets="6.1.145,6.1.999@2025-08")
        self.assertEqual([(row["sub_level"], row["os_patch_level"]) for row in rows],
                         [("145", "2025-09"), ("999", "2025-08")])

    def test_rate_limit_keeps_only_verified_reads_and_next_probe_resumes_without_bursting_again(self):
        snapshot = self.root / "refs.json"
        provider = FakeProvider().branch().branch(month="2027-01", sublevel=1000, commit="b" * 40)
        read = provider.makefile

        def limited(commit):
            if commit == "b" * 40:
                raise catalog.CatalogError("HTTP 429")
            return read(commit)

        with patch.object(provider, "makefile", side_effect=limited), self.assertRaisesRegex(catalog.CatalogError, "429"):
            catalog.update_catalog(self.root, provider, snapshot_file=snapshot)
        self.assertEqual(json.loads(snapshot.read_text())["versions"], {"a" * 40: "6.1.999"})
        self.assertEqual(self.path.read_bytes(), self.original)
        provider.reads.clear()
        report = catalog.update_catalog(self.root, provider, snapshot_file=snapshot)
        self.assertEqual(report["added"], 2)
        self.assertEqual([item for item in provider.reads if item[0] == "makefile"], [("makefile", "b" * 40)])

    def test_catalog_fixed_commit_and_exact_official_tag_can_seed_snapshot(self):
        for ref in ("a" * 40, "refs/tags/android14-release"):
            with self.subTest(ref=ref):
                document = json.loads(self.original)
                document["entries"].append({"android_version": "android14", "kernel_version": "6.1",
                                            "kernel": "6.1.999", "date": "2026-12", "ref": ref, "revision": "r1"})
                self.path.write_text(json.dumps(document))
                provider = FakeProvider().branch()
                provider.tag_refs["refs/tags/android14-release"] = "a" * 40
                snapshot = self.root / ("refs-" + uuid.uuid4().hex + ".json")
                report = catalog.update_catalog(self.root, provider, snapshot_file=snapshot)
                self.assertFalse(report["has_updates"])
                self.assertNotIn(("makefile", "a" * 40), provider.reads)
                self.assertEqual(json.loads(snapshot.read_text())["versions"], {"a" * 40: "6.1.999"})

    def test_invalid_snapshot_fails_before_network_or_catalog_changes(self):
        snapshot = self.root / "refs.json"
        invalid = [{"schema_version": 1, "versions": {"main": "6.1.999"}},
                   {"schema_version": 1, "versions": {"a" * 40: "6.18.1"}},
                   {"schema_version": True, "versions": {}}, {"schema_version": 2, "versions": {}}]
        for value in invalid:
            with self.subTest(value=value):
                snapshot.write_text(json.dumps(value))
                provider = FakeProvider().branch()
                with self.assertRaises(ValueError):
                    catalog.update_catalog(self.root, provider, snapshot_file=snapshot)
                self.assertEqual(provider.reads, [])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_check_cli_can_persist_only_the_explicit_snapshot(self):
        snapshot = self.root / "cache/refs.json"
        with redirect_stdout(io.StringIO()):
            status = catalog.main(["--root", str(self.root), "--check", "--snapshot-file", str(snapshot)],
                                  provider=FakeProvider().branch())
        self.assertEqual(status, 0)
        self.assertTrue(snapshot.is_file())
        self.assertEqual(self.path.read_bytes(), self.original)


class OfficialProviderTests(unittest.TestCase):
    def test_annotated_official_tag_uses_peeled_commit(self):
        provider = catalog.OfficialProvider()
        value = {"android14-release": {"value": "a" * 40, "peeled": "B" * 40}}
        with patch.object(provider, "get", return_value=(")]}'\n" + json.dumps(value)).encode()) as get:
            self.assertEqual(provider.tags(), {"refs/tags/android14-release": "b" * 40})
        get.assert_called_once_with("common/+refs/tags?format=JSON")

    def test_gitiles_refs_and_base64_makefile_responses(self):
        provider = catalog.OfficialProvider()
        refs = {"android14-6.1-2026-12": {"value": "A" * 40}}
        with patch.object(provider, "get", return_value=(")]}'\n" + json.dumps(refs)).encode()) as get:
            self.assertEqual(provider.refs("common"), {"refs/heads/android14-6.1-2026-12": "a" * 40})
            get.assert_called_once_with("common/+refs/heads?format=JSON")
        with patch.object(provider, "get", return_value=base64.b64encode(makefile().encode())) as get:
            self.assertEqual(provider.makefile("a" * 40), makefile())
            get.assert_called_once_with("common/+/" + "a" * 40 + "/Makefile?format=TEXT")

    def test_branch_namespace_keeps_deprecated_months_and_excludes_full_gerrit_refs(self):
        provider = catalog.OfficialProvider()
        for repository, prefix in (("manifest", "common-"), ("common", "")):
            with self.subTest(repository=repository):
                branches = {prefix + "android14-6.1-2026-12": {"value": "a" * 40},
                            "deprecated/" + prefix + "android14-6.1-2025-08": {"value": "b" * 40}}
                raw = (")]}'\n" + json.dumps(branches)).encode()
                with patch.object(provider, "get", return_value=raw) as get:
                    refs = provider.refs(repository)
                get.assert_called_once_with(repository + "/+refs/heads?format=JSON")
                selected = catalog.monthly_refs(refs, repository)
                self.assertEqual(len(selected), 2)
                self.assertEqual(selected[("android14", "6.1", "2025-08")],
                                 ("refs/heads/deprecated/" + prefix + "android14-6.1-2025-08", "b" * 40))

    def test_only_fixed_official_repositories_and_full_commits_reach_http(self):
        provider = catalog.OfficialProvider()
        with patch.object(provider, "get") as get:
            with self.assertRaises(catalog.CatalogError):
                provider.refs("https://attacker.test")
            with self.assertRaises(catalog.CatalogError):
                provider.makefile("main/../../other")
        get.assert_not_called()

    def test_invalid_gitiles_data_is_rejected(self):
        provider = catalog.OfficialProvider()
        for raw in (b"[]", b"invalid", b'{"main":{"value":"not-sha"}}', b'{"":{"value":"' + b'a' * 40 + b'"}}'):
            with self.subTest(raw=raw), patch.object(provider, "get", return_value=raw):
                with self.assertRaises(catalog.CatalogError):
                    provider.refs("common")
        with patch.object(provider, "get", return_value=b"not-base64!"):
            with self.assertRaises(catalog.CatalogError):
                provider.makefile("a" * 40)

    def test_http_is_fixed_https_and_redirects_are_rejected(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.geturl.return_value = catalog.BASE + "common/+refs?format=JSON"
        response.read.return_value = b"{}"
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(catalog, "build_opener", return_value=opener) as build:
            self.assertEqual(catalog.OfficialProvider().get("common/+refs?format=JSON"), b"{}")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://android.googlesource.com/kernel/common/+refs?format=JSON")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsInstance(build.call_args.args[0], catalog.NoRedirect)
        with self.assertRaises(catalog.CatalogError):
            catalog.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://attacker.test")
        response.geturl.return_value = "https://attacker.test"
        with patch.object(catalog, "build_opener", return_value=opener):
            with self.assertRaises(catalog.CatalogError):
                catalog.OfficialProvider().get("common/+refs?format=JSON")

    def test_transient_official_http_errors_retry_but_permanent_errors_stop(self):
        path = "common/+refs/heads?format=JSON"
        url = catalog.BASE + path
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.geturl.return_value = url
        response.read.return_value = b"{}"
        opener = MagicMock()
        opener.open.side_effect = [HTTPError(url, 503, "Unavailable", {}, None),
                                   HTTPError(url, 429, "Rate limited", {}, None), response]
        with patch.object(catalog, "build_opener", return_value=opener), patch.object(catalog.time, "sleep") as sleep:
            self.assertEqual(catalog.OfficialProvider().get(path), b"{}")
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        for status, attempts in ((404, 1), (503, 3)):
            with self.subTest(status=status):
                opener = MagicMock()
                opener.open.side_effect = HTTPError(url, status, "Unavailable", {}, None)
                with patch.object(catalog, "build_opener", return_value=opener), patch.object(catalog.time, "sleep"):
                    with self.assertRaisesRegex(catalog.CatalogError, f"HTTP {status}"):
                        catalog.OfficialProvider().get(path)
                self.assertEqual(opener.open.call_count, attempts)

    def test_network_failure_is_not_relabelled_as_invalid_json_or_base64(self):
        provider = catalog.OfficialProvider()
        with patch.object(provider, "get", side_effect=catalog.CatalogError("HTTP 503")):
            with self.assertRaisesRegex(catalog.CatalogError, "^HTTP 503$"):
                provider.refs("common")
            with self.assertRaisesRegex(catalog.CatalogError, "^HTTP 503$"):
                provider.makefile("a" * 40)


if __name__ == "__main__":
    unittest.main()
