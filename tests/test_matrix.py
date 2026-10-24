import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import matrix


IDENTITY_FIELDS = ("android_version", "kernel_version", "sub_level", "os_patch_level")


def expected_identities(root):
    """Read the declared version/month identities independently of matrix.py."""
    document = json.loads((root / "data/kernels.json").read_text(encoding="utf-8"))
    return {(entry["android_version"], entry["kernel_version"], entry["kernel"].rsplit(".", 1)[1], entry["date"])
            for entry in document["entries"] if entry["date"] != "lts"}


def row_identities(rows):
    return {tuple(row[field] for field in IDENTITY_FIELDS) for row in rows}


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "tests" / f".matrix-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "data").mkdir()
        shutil.copyfile(ROOT / "data/kernels.json", self.root / "data/kernels.json")

    def edit(self, family, transform):
        path = self.root / "data/kernels.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        selected = {"entries": [entry for entry in document["entries"]
                                 if entry["kernel_version"] == family and entry["date"] != "lts"]}
        transform(selected)
        preserved = [entry for entry in document["entries"]
                     if entry["kernel_version"] != family or entry["date"] == "lts"]
        for entry in selected["entries"]:
            entry.setdefault("kernel_version", family)
            entry.setdefault("android_version", matrix.KERNELS[family])
        document["entries"] = preserved + selected["entries"]
        path.write_text(json.dumps(document), encoding="utf-8")

    def test_self_contained_catalog_preserves_every_declared_monthly_identity(self):
        for root in (ROOT, self.root):
            with self.subTest(root=root):
                rows = matrix.build_matrix(root)
                declared = expected_identities(root)
                self.assertEqual(declared, row_identities(rows))
                self.assertEqual(len(declared), len(rows))
                self.assertGreaterEqual(len(rows), 128)
                self.assertEqual(rows, matrix.build_matrix(root))

    def test_same_sublevel_preserves_different_months(self):
        rows = matrix.build_matrix(self.root, targets="6.1.145")
        months = sorted((identity[3] for identity in expected_identities(self.root)
                         if identity[1:3] == ("6.1", "145")), reverse=True)
        self.assertEqual(months, [row["os_patch_level"] for row in rows])
        by_month = {row["os_patch_level"]: row for row in rows}
        self.assertTrue(by_month["2025-08"]["kernel_ref"].startswith("refs/tags/"))
        self.assertTrue(by_month["2025-09"]["kernel_ref"].startswith("refs/heads/"))

    def test_all_groups_families_with_newest_target_first_and_keeps_history(self):
        rows = matrix.build_matrix(self.root)
        declared = expected_identities(self.root)
        families = ["5.10", "5.15", "6.1", "6.6", "6.12"]
        transitions = [row["kernel_version"] for index, row in enumerate(rows)
                       if index == 0 or row["kernel_version"] != rows[index - 1]["kernel_version"]]
        self.assertEqual(families, transitions)
        for family in families:
            newest = max((identity for identity in declared if identity[1] == family),
                         key=lambda identity: (int(identity[2]), identity[3]))
            first = next(row for row in rows if row["kernel_version"] == family)
            self.assertEqual(newest, tuple(first[field] for field in IDENTITY_FIELDS))
        self.assertEqual(declared, row_identities(rows))
        self.assertEqual(len(declared), len(rows))

    def test_grouped_families_keep_versions_months_and_lts_in_order(self):
        entries = []
        for family, sublevel, month in (
            ("5.10", "90", "2023-01"), ("5.10", "100", "2024-01"),
            ("5.10", "100", "2024-02"), ("5.15", "20", "2023-09"),
            ("5.15", "21", "2023-01"), ("6.1", "5", "2023-02"),
            ("5.10", "101", "lts"),
        ):
            entries.append({"android_version": matrix.KERNELS[family], "kernel_version": family,
                            "kernel": f"{family}.{sublevel}", "date": month, "ref": ""})
        (self.root / "data/kernels.json").write_text(
            json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8")
        rows = matrix.build_matrix(self.root, include_lts=True)
        self.assertEqual([
            ("5.10", "100", "2024-02"), ("5.10", "100", "2024-01"),
            ("5.10", "90", "2023-01"), ("5.10", "X", "lts"),
            ("5.15", "21", "2023-01"), ("5.15", "20", "2023-09"),
            ("6.1", "5", "2023-02"),
        ], [(row["kernel_version"], row["sub_level"], row["os_patch_level"]) for row in rows])
        monthly = matrix.build_matrix(self.root)
        self.assertEqual(expected_identities(self.root), row_identities(monthly))
        self.assertEqual(6, len(monthly))

    def test_exact_target_and_kernel_filter(self):
        rows = matrix.build_matrix(self.root, "6.1,6.12", "6.1.145@2025-08,6.12.92@2026-09")
        self.assertEqual(2, len(rows))
        self.assertEqual("2025-08", rows[0]["os_patch_level"])
        self.assertEqual("6.12", rows[1]["kernel_version"])

    def test_lts_is_opt_in_and_adds_five_rows(self):
        stable = matrix.build_matrix(self.root)
        self.assertFalse(any(row["sub_level"] == "X" for row in stable))
        rows = matrix.build_matrix(self.root, include_lts=True)
        self.assertEqual(len(stable) + 5, len(rows))
        lts = [row for row in rows if row["sub_level"] == "X"]
        self.assertEqual(5, len(lts))
        self.assertTrue(all(row["os_patch_level"] == "lts" and row["kernel_ref"].endswith("-lts") for row in lts))

    def test_known_android12_revision(self):
        rows = matrix.build_matrix(self.root, targets="5.10.101@2022-04")
        self.assertEqual("r28", rows[0]["revision"])

    def test_unknown_and_outside_selected_targets_fail(self):
        absent_sublevel = max(int(identity[2]) for identity in expected_identities(self.root)
                              if identity[1] == "6.1") + 1000
        for kernels, targets in [("all", f"6.1.{absent_sublevel}"), ("5.10", "6.1.145"), ("all", "6.1.145@2020-01")]:
            with self.subTest(kernels=kernels, targets=targets):
                with self.assertRaisesRegex(ValueError, "not found"):
                    matrix.build_matrix(self.root, kernels, targets)

    def test_invalid_inputs_fail(self):
        for kernels, targets in [("", ""), ("all,6.1", ""), ("6.9", ""), ("all", "6.1.145;echo x"), ("all", "6.1.145,"), ("all", "6.1.145@2025-13")]:
            with self.subTest(kernels=kernels, targets=targets):
                with self.assertRaises(ValueError):
                    matrix.build_matrix(self.root, kernels, targets)
        with self.assertRaises(ValueError):
            matrix.build_matrix(self.root, include_lts="false")

    def test_mismatching_version_and_bad_date_fail(self):
        self.edit("6.1", lambda data: data["entries"][0].update(kernel="6.6.25"))
        with self.assertRaisesRegex(ValueError, "does not belong"):
            matrix.build_matrix(self.root)

    def test_invalid_catalog_month_fails(self):
        self.edit("6.1", lambda data: data["entries"][0].update(date="2025-00"))
        with self.assertRaisesRegex(ValueError, "invalid security-patch month"):
            matrix.build_matrix(self.root)

    def test_invalid_catalog_ref_fails(self):
        self.edit("6.1", lambda data: data["entries"][0].update(ref="refs/heads/a; echo bad"))
        with self.assertRaisesRegex(ValueError, "invalid kernel ref"):
            matrix.build_matrix(self.root)

    def test_immutable_full_commit_refs_are_accepted_and_normalized(self):
        self.edit("6.1", lambda data: data["entries"][0].update(ref="A" * 40))
        rows = matrix.build_matrix(self.root, "6.1")
        self.assertTrue(any(row["kernel_ref"] == "a" * 40 for row in rows))

    def test_exact_duplicate_is_removed(self):
        before = len(expected_identities(self.root))
        self.edit("6.1", lambda data: data["entries"].append(dict(data["entries"][0])))
        self.assertEqual(before, len(matrix.build_matrix(self.root)))

    def test_future_catalog_versions_and_months_grow_without_changing_lts_count(self):
        before = expected_identities(self.root)
        next_sublevel = max(int(identity[2]) for identity in before if identity[1] == "6.1") + 1
        latest_month = max(identity[3] for identity in before)
        year, month = map(int, latest_month.split("-"))
        months = [f"{year + (month + offset - 1) // 12:04d}-{(month + offset - 1) % 12 + 1:02d}"
                  for offset in (1, 2)]
        full = f"6.1.{next_sublevel}"
        self.edit("6.1", lambda data: data["entries"].extend(
            {"date": date, "kernel": full,
             "ref": f"refs/heads/android14-6.1-{date}"} for date in months))
        stable = matrix.build_matrix(self.root)
        self.assertEqual(len(before) + 2, len(stable))
        self.assertEqual(expected_identities(self.root), row_identities(stable))
        self.assertEqual(list(reversed(months)), [row["os_patch_level"] for row in
                                                 matrix.build_matrix(self.root, targets=full)])
        self.assertEqual(len(stable) + 5, len(matrix.build_matrix(self.root, include_lts=True)))

    def test_empty_selected_catalog_fails(self):
        self.edit("6.1", lambda data: data.update(entries=[]))
        with self.assertRaisesRegex(ValueError, "empty build matrix"):
            matrix.build_matrix(self.root, "6.1")

    def test_legacy_workflows_and_snapshot_catalogs_are_not_read(self):
        before = expected_identities(self.root)
        upstream = self.root / ".upstream"
        upstream.mkdir()
        (upstream / "abk-build.yml").write_text("malformed old workflow", encoding="utf-8")
        legacy = self.root / "data/android14"
        legacy.mkdir()
        (legacy / "6.1.json").write_text("malformed old snapshot", encoding="utf-8")
        rows = matrix.build_matrix(self.root)
        self.assertEqual(len(before), len(rows))
        self.assertEqual(expected_identities(self.root), row_identities(rows))
        existing = next(row for row in rows if row["kernel_version"] == "6.1" and row["os_patch_level"] == "2025-08")
        self.assertTrue(existing["kernel_ref"].startswith("refs/tags/"))

    def test_changed_local_schema_and_bad_android_pair_fail(self):
        path = self.root / "data/kernels.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schema_version"] = 2
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported kernel catalog schema"):
            matrix.build_matrix(self.root)
        document["schema_version"] = 1
        document["entries"][0]["android_version"] = "android14"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported Android/kernel pair"):
            matrix.build_matrix(self.root)

    def test_lts_full_version_target_preserves_original_filtering(self):
        document = json.loads((self.root / "data/kernels.json").read_text(encoding="utf-8"))
        lts = next(entry for entry in document["entries"] if entry["date"] == "lts")
        rows = matrix.build_matrix(self.root, targets=lts["kernel"], include_lts=True)
        self.assertTrue(any(row["sub_level"] == "X" and row["os_patch_level"] == "lts" for row in rows))

    def test_matrix_limit_fails_with_actionable_error(self):
        def expand(data):
            data["entries"] = [{"date": f"{2000 + n // 12:04d}-{n % 12 + 1:02d}", "kernel": f"6.1.{n}", "ref": ""} for n in range(257)]
        self.edit("6.1", expand)
        with self.assertRaisesRegex(ValueError, "at most 256"):
            matrix.build_matrix(self.root, "6.1")

    def test_cli_github_outputs_and_boolean_strings(self):
        expected_count = sum(identity[1] == "6.12" for identity in expected_identities(self.root))
        output, summary = self.root / "output.txt", self.root / "summary.md"
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            result = matrix.main(["--root", str(self.root), "--kernel-versions", "6.12", "--include-lts", "false", "--github-output", str(output), "--summary", str(summary)])
        self.assertEqual(0, result)
        self.assertEqual(expected_count, len(json.loads(capture.getvalue())["include"]))
        self.assertIn(f"count={expected_count}\n", output.read_text(encoding="utf-8"))
        self.assertIn("full_catalog=false\n", output.read_text(encoding="utf-8"))
        self.assertIn("包含 LTS：否", summary.read_text(encoding="utf-8"))
        self.assertIn("所选系列的全部历史版本与补丁月份", summary.read_text(encoding="utf-8"))
        self.assertIn("按 5.10、5.15、6.1、6.6、6.12 系列分组", summary.read_text(encoding="utf-8"))

    def cli_outputs(self, *arguments):
        output = self.root / ("output-" + uuid.uuid4().hex)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, matrix.main(["--root", str(self.root), "--github-output", str(output), *arguments]))
        return dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())

    def test_full_catalog_output_compares_actual_rows_for_all_explicit_families_and_targets(self):
        complete = self.cli_outputs()
        self.assertEqual("true", complete["full_catalog"])
        self.assertEqual({"include"}, set(json.loads(complete["matrix"])))
        families = self.cli_outputs("--kernel-versions", "6.12,6.6,6.1,5.15,5.10")
        self.assertEqual("true", families["full_catalog"])
        document = json.loads((self.root / "data/kernels.json").read_text(encoding="utf-8"))
        targets = ",".join(entry["kernel"] + "@" + entry["date"] for entry in document["entries"]
                           if entry["date"] != "lts")
        specified = self.cli_outputs("--targets", targets)
        self.assertEqual("true", specified["full_catalog"])
        self.assertEqual(complete["count"], specified["count"])
        missing_one = self.cli_outputs("--targets", ",".join(targets.split(",")[1:]))
        self.assertEqual("false", missing_one["full_catalog"])

    def test_full_catalog_requires_every_month_not_just_five_families_or_same_count(self):
        rows = matrix.build_matrix(self.root)
        latest_each = [next(row for row in rows if row["kernel_version"] == family) for family in matrix.KERNELS]
        self.assertFalse(matrix.covers_full_catalog(self.root, latest_each))
        targets = ",".join(f'{row["kernel_version"]}.{row["sub_level"]}@{row["os_patch_level"]}'
                           for row in latest_each)
        self.assertEqual("false", self.cli_outputs("--targets", targets)["full_catalog"])
        duplicate_with_missing = rows[:-1] + [dict(rows[0])]
        self.assertEqual(len(rows), len(duplicate_with_missing))
        self.assertFalse(matrix.covers_full_catalog(self.root, duplicate_with_missing))
        for field, value in (("kernel_ref", "a" * 40), ("revision", "r999"), ("os_patch_level", "2000-01")):
            changed = [dict(row) for row in rows]
            changed[0][field] = value
            with self.subTest(field=field):
                self.assertFalse(matrix.covers_full_catalog(self.root, changed))

    def test_full_catalog_lts_scope_uses_the_same_switch_and_accepts_explicit_complete_targets(self):
        monthly = matrix.build_matrix(self.root)
        with_lts = matrix.build_matrix(self.root, include_lts=True)
        self.assertTrue(matrix.covers_full_catalog(self.root, monthly))
        self.assertFalse(matrix.covers_full_catalog(self.root, monthly, include_lts=True))
        self.assertFalse(matrix.covers_full_catalog(self.root, with_lts))
        self.assertTrue(matrix.covers_full_catalog(self.root, with_lts, include_lts=True))
        self.assertEqual("true", self.cli_outputs("--include-lts", "true")["full_catalog"])
        document = json.loads((self.root / "data/kernels.json").read_text(encoding="utf-8"))
        monthly_targets = ",".join(entry["kernel"] + "@" + entry["date"] for entry in document["entries"]
                                   if entry["date"] != "lts")
        self.assertEqual("false", self.cli_outputs("--include-lts", "true", "--targets", monthly_targets)["full_catalog"])
        all_versions = ",".join(sorted({entry["kernel"] for entry in document["entries"]}))
        self.assertEqual("true", self.cli_outputs("--include-lts", "true", "--targets", all_versions)["full_catalog"])

    def test_incomplete_catalog_cannot_qualify_as_all_five_families(self):
        path = self.root / "data/kernels.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["entries"] = [entry for entry in document["entries"] if entry["kernel_version"] != "6.12"]
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual("false", self.cli_outputs()["full_catalog"])

    def test_large_catalog_still_allows_partial_build_with_release_disabled(self):
        def expand(data):
            data["entries"] = [{"date": f"{2000 + n // 12:04d}-{n % 12 + 1:02d}",
                                "kernel": f"6.1.{n}", "ref": ""} for n in range(257)]
        self.edit("6.1", expand)
        values = self.cli_outputs("--kernel-versions", "6.1", "--targets", "6.1.0@2000-01")
        self.assertEqual("1", values["count"])
        self.assertEqual("false", values["full_catalog"])

    def test_cli_summary_distinguishes_explicit_targets_from_all_history(self):
        summary = self.root / "summary.md"
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            matrix.main(["--root", str(self.root), "--targets", "6.1.145@2025-08",
                         "--summary", str(summary)])
        self.assertEqual(1, len(json.loads(capture.getvalue())["include"]))
        content = summary.read_text(encoding="utf-8")
        self.assertIn("构建任务：1", content)
        self.assertIn("指定版本（6.1.145@2025-08）", content)
        self.assertNotIn("全部历史版本", content)


if __name__ == "__main__":
    unittest.main()
