import contextlib
import io
import json
from pathlib import Path
import re
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
    identities = set()
    for path in (root / "data").glob("android*/*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        for entry in document["entries"]:
            identities.add((document["android_version"], document["kernel_version"],
                            entry["kernel"].rsplit(".", 1)[1], entry["date"]))
    primary = root / ".upstream" / "abk-build.yml"
    if primary.exists():
        source = primary.read_text(encoding="utf-8")
        block = source.split("KNOWN_KERNEL_PAIRS=$(cat <<'EOF'", 1)[1].split("EOF", 1)[0]
        identities.update(re.findall(
            r"^\s*(android\d+)\s+(\d+\.\d+)\s+(\d+)\s+(\d{4}-\d{2})\s*$", block, re.M))
    return identities


def row_identities(rows):
    return {tuple(row[field] for field in IDENTITY_FIELDS) for row in rows}


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "tests" / f".matrix-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copytree(ROOT / "data", self.root / "data")

    def edit(self, family, transform):
        path = self.root / "data" / matrix.KERNELS[family] / f"{family}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        transform(document)
        path.write_text(json.dumps(document), encoding="utf-8")

    def test_catalog_and_abk_union_preserves_every_identity(self):
        for root in (ROOT, self.root):
            with self.subTest(root=root):
                rows = matrix.build_matrix(root)
                declared = expected_identities(root)
                self.assertEqual(declared, row_identities(rows))
                self.assertEqual(len(declared), len(rows))
                self.assertEqual(rows, matrix.build_matrix(root))

    def test_same_sublevel_preserves_different_months(self):
        rows = matrix.build_matrix(self.root, targets="6.1.145")
        months = sorted(identity[3] for identity in expected_identities(self.root)
                        if identity[1:3] == ("6.1", "145"))
        self.assertEqual(months, [row["os_patch_level"] for row in rows])
        by_month = {row["os_patch_level"]: row for row in rows}
        self.assertTrue(by_month["2025-08"]["kernel_ref"].startswith("refs/tags/"))
        self.assertTrue(by_month["2025-09"]["kernel_ref"].startswith("refs/heads/"))

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
        self.assertEqual(months, [row["os_patch_level"] for row in
                                 matrix.build_matrix(self.root, targets=full)])
        self.assertEqual(len(stable) + 5, len(matrix.build_matrix(self.root, include_lts=True)))

    def test_empty_selected_catalog_fails(self):
        self.edit("6.1", lambda data: data.update(entries=[]))
        with self.assertRaisesRegex(ValueError, "empty build matrix"):
            matrix.build_matrix(self.root, "6.1")

    def test_abk_new_rows_are_added_and_catalog_refs_win(self):
        before = expected_identities(self.root)
        added_sublevel = max(int(identity[2]) for identity in before if identity[1] == "6.1") + 1
        upstream = self.root / ".upstream"
        upstream.mkdir()
        (upstream / "abk-build.yml").write_text(
            "KNOWN_KERNEL_PAIRS=$(cat <<'EOF'\nandroid14 6.1 145 2025-08\n"
            f"android14 6.1 {added_sublevel} 2026-12\nEOF\n)\n", encoding="utf-8")
        rows = matrix.build_matrix(self.root)
        self.assertEqual(len(before) + 1, len(rows))
        self.assertEqual(expected_identities(self.root), row_identities(rows))
        existing = next(row for row in rows if row["kernel_version"] == "6.1" and row["os_patch_level"] == "2025-08")
        self.assertTrue(existing["kernel_ref"].startswith("refs/tags/"))
        added = next(row for row in rows if row["kernel_version"] == "6.1" and row["sub_level"] == str(added_sublevel))
        self.assertEqual("", added["kernel_ref"])

    def test_changed_primary_schema_is_not_silently_ignored(self):
        upstream = self.root / ".upstream"
        upstream.mkdir()
        (upstream / "abk-build.yml").write_text("changed schema", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "review the upstream schema"):
            matrix.build_matrix(self.root)

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
        self.assertIn("包含 LTS：否", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
