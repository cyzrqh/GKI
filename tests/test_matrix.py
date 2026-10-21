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

    def test_snapshot_has_128_distinct_monthly_combinations(self):
        rows = matrix.build_matrix(self.root)
        self.assertEqual(128, len(rows))
        self.assertEqual(rows, matrix.build_matrix(self.root))
        self.assertEqual(128, len({tuple(row.items()) for row in rows}))

    def test_same_sublevel_preserves_different_months(self):
        rows = matrix.build_matrix(self.root, targets="6.1.145")
        self.assertEqual(["2025-08", "2025-09"], [row["os_patch_level"] for row in rows])
        self.assertTrue(rows[0]["kernel_ref"].startswith("refs/tags/"))
        self.assertTrue(rows[1]["kernel_ref"].startswith("refs/heads/"))

    def test_exact_target_and_kernel_filter(self):
        rows = matrix.build_matrix(self.root, "6.1,6.12", "6.1.145@2025-08,6.12.92")
        self.assertEqual(2, len(rows))
        self.assertEqual("2025-08", rows[0]["os_patch_level"])
        self.assertEqual("6.12", rows[1]["kernel_version"])

    def test_lts_is_opt_in_and_adds_five_rows(self):
        self.assertFalse(any(row["sub_level"] == "X" for row in matrix.build_matrix(self.root)))
        rows = matrix.build_matrix(self.root, include_lts=True)
        self.assertEqual(133, len(rows))
        lts = [row for row in rows if row["sub_level"] == "X"]
        self.assertEqual(5, len(lts))
        self.assertTrue(all(row["os_patch_level"] == "lts" and row["kernel_ref"].endswith("-lts") for row in lts))

    def test_known_android12_revision(self):
        rows = matrix.build_matrix(self.root, targets="5.10.101@2022-04")
        self.assertEqual("r28", rows[0]["revision"])

    def test_unknown_and_outside_selected_targets_fail(self):
        for kernels, targets in [("all", "6.1.999"), ("5.10", "6.1.145"), ("all", "6.1.145@2020-01")]:
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
        self.edit("6.1", lambda data: data["entries"].append(dict(data["entries"][0])))
        self.assertEqual(128, len(matrix.build_matrix(self.root)))

    def test_empty_selected_catalog_fails(self):
        self.edit("6.1", lambda data: data.update(entries=[]))
        with self.assertRaisesRegex(ValueError, "empty build matrix"):
            matrix.build_matrix(self.root, "6.1")

    def test_abk_new_rows_are_added_and_catalog_refs_win(self):
        upstream = self.root / ".upstream"
        upstream.mkdir()
        (upstream / "abk-build.yml").write_text("KNOWN_KERNEL_PAIRS=$(cat <<'EOF'\nandroid14 6.1 145 2025-08\nandroid14 6.1 200 2026-12\nEOF\n)\n", encoding="utf-8")
        rows = matrix.build_matrix(self.root)
        self.assertEqual(129, len(rows))
        existing = next(row for row in rows if row["kernel_version"] == "6.1" and row["os_patch_level"] == "2025-08")
        self.assertTrue(existing["kernel_ref"].startswith("refs/tags/"))
        added = next(row for row in rows if row["sub_level"] == "200")
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
        output, summary = self.root / "output.txt", self.root / "summary.md"
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            result = matrix.main(["--root", str(self.root), "--kernel-versions", "6.12", "--include-lts", "false", "--github-output", str(output), "--summary", str(summary)])
        self.assertEqual(0, result)
        self.assertEqual(8, len(json.loads(capture.getvalue())["include"]))
        self.assertIn("count=8\n", output.read_text(encoding="utf-8"))
        self.assertIn("包含 LTS：否", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
