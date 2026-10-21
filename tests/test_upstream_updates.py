"""Offline tests for the daily upstream-update planner."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock
import uuid


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "check_upstream_updates.py"
SPEC = importlib.util.spec_from_file_location("check_upstream_updates", MODULE_PATH)
updates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updates)
sync = updates.sync


def inventory(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class UpstreamUpdateTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.fixture = self.parent / (".sync-update-" + uuid.uuid4().hex)
        self.fixture.mkdir()
        self.addCleanup(self.cleanup)
        self.root = self.fixture / "repo"
        self.root.mkdir()
        self.lock = {
            "schema_version": 1,
            "abk": {"repository": "example/abk", "branch": "dev", "commit": "a" * 40},
            "catalog": {"repository": "example/catalog", "branch": "dev", "commit": "b" * 40},
        }
        self.latest = {"abk": "c" * 40, "catalog": "b" * 40}
        self.files = {"config/example.txt": b"active support", ".upstream/abk-build.yml": b"snapshot"}
        for name, content in self.files.items():
            self.write(name, content)
        self.write(sync.LOCK_PATH, json.dumps(self.lock).encode())
        self.write(sync.MANIFEST_PATH, sync._json_bytes({"schema_version": 1, "files": {name: sync._sha256(content) for name, content in self.files.items()}}))
        self.workflow = (f"# ABK {'a' * 40}\n# Version catalog {'b' * 40}\nname: build\non: workflow_dispatch\njobs: {{}}\n").encode()
        self.write(sync.WORKFLOW_PATH, self.workflow)
        self.write("README.md", b"local README")
        self.calls = []
        self.active_change = True
        self.fail_tests = False

    def cleanup(self):
        target = self.fixture.resolve()
        if target.parent != self.parent.resolve() or not target.name.startswith(".sync-update-"):
            raise AssertionError("Refusing to remove unexpected test directory")
        shutil.rmtree(target)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def runner(self, arguments, root, **kwargs):
        self.calls.append((arguments, root))
        if arguments[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if "tools/sync_upstream.py" in arguments:
            lock = copy.deepcopy(self.lock)
            for source in ("abk", "catalog"):
                lock[source]["commit"] = self.latest[source]
            (root / sync.LOCK_PATH).write_bytes(sync._json_bytes(lock))
            current_files = dict(self.files)
            current_files[".upstream/abk-build.yml"] = b"updated snapshot"
            if self.active_change:
                current_files["config/example.txt"] = b"updated active support"
            for name, content in current_files.items():
                (root / name).write_bytes(content)
            (root / sync.MANIFEST_PATH).write_bytes(sync._json_bytes({
                "schema_version": 1, "files": {name: sync._sha256(content) for name, content in current_files.items()},
            }))
            (root / sync.WORKFLOW_PATH).write_bytes(self.workflow.replace(("a" * 40).encode(), ("c" * 40).encode()))
        elif "unittest" in arguments and self.fail_tests:
            raise updates.UpdateError("full unit tests failed")
        return subprocess.CompletedProcess(arguments, 0, "OK", "")

    def test_same_commits_are_quiet_and_do_not_run_commands(self):
        before = inventory(self.root)
        latest = {source: self.lock[source]["commit"] for source in ("abk", "catalog")}
        with mock.patch.object(updates, "run_command", side_effect=AssertionError("unexpected command")):
            result = updates.prepare_updates(self.root, self.lock, latest)
        self.assertFalse(result["has_updates"])
        self.assertEqual(before, inventory(self.root))

    def test_metadata_and_snapshot_only_commits_do_not_leave_changes(self):
        self.active_change = False
        before = inventory(self.root)
        with mock.patch.object(updates, "run_command", side_effect=self.runner):
            result = updates.prepare_updates(self.root, self.lock, self.latest)
        self.assertFalse(result["has_updates"])
        self.assertEqual(before, inventory(self.root))
        self.assertFalse(any("unittest" in arguments for arguments, _ in self.calls))

    def test_meaningful_update_uses_pinned_shas_and_full_validation(self):
        with mock.patch.object(updates, "run_command", side_effect=self.runner):
            result = updates.prepare_updates(self.root, self.lock, self.latest)
        self.assertTrue(result["has_updates"])
        self.assertTrue(result["workflow_changed"])
        self.assertIn("config/example.txt", result["meaningful_files"])
        sync_call = next(arguments for arguments, _ in self.calls if "tools/sync_upstream.py" in arguments)
        self.assertEqual(sync_call[sync_call.index("--abk-ref") + 1], self.latest["abk"])
        self.assertEqual(sync_call[sync_call.index("--catalog-ref") + 1], self.latest["catalog"])
        self.assertTrue(any("tools/generate_workflow.py" in arguments for arguments, _ in self.calls))
        self.assertTrue(any("-c" in arguments for arguments, _ in self.calls))
        self.assertTrue(any("unittest" in arguments and "discover" in arguments for arguments, _ in self.calls))
        self.assertEqual((self.root / "README.md").read_bytes(), b"local README")
        self.assertEqual((self.root / "config/example.txt").read_bytes(), b"updated active support")
        self.assertEqual(sync.read_lock(self.root)["abk"]["commit"], self.latest["abk"])

    def test_validation_failure_never_changes_live_checkout(self):
        self.fail_tests = True
        before = inventory(self.root)
        with mock.patch.object(updates, "run_command", side_effect=self.runner):
            with self.assertRaisesRegex(updates.UpdateError, "unit tests failed"):
                updates.prepare_updates(self.root, self.lock, self.latest)
        self.assertEqual(before, inventory(self.root))

    def test_dirty_checkout_is_rejected_before_import(self):
        before = inventory(self.root)
        dirty = subprocess.CompletedProcess([], 0, " M README.md\n", "")
        with mock.patch.object(updates, "run_command", return_value=dirty) as run:
            with self.assertRaisesRegex(updates.UpdateError, "clean checkout"):
                updates.prepare_updates(self.root, self.lock, self.latest)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(before, inventory(self.root))

    def test_report_has_stable_ownership_marker_and_no_auto_merge_claim(self):
        result = {"meaningful_files": ["config/example.txt"]}
        text = updates.report_text(self.lock, self.latest, result)
        self.assertTrue(text.startswith(updates.PR_MARKER))
        marker = "<!-- gki-upstream-heads:abk=" + "c" * 40 + ";catalog=" + "b" * 40 + " -->"
        self.assertEqual(text.splitlines()[1], marker)
        self.assertEqual(text.count(marker), 1)
        self.assertIn("/compare/" + "a" * 40 + "..." + "c" * 40, text)
        self.assertIn("没有编译内核", text)
        self.assertIn("审核", text)

    def test_check_only_never_imports_or_creates_report(self):
        output = self.fixture / "outputs.txt"
        before = inventory(self.root)
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            with mock.patch.object(updates, "find_latest", return_value=self.latest):
                with mock.patch.object(updates, "prepare_updates", side_effect=AssertionError("unexpected prepare")):
                    with mock.patch.object(updates, "write_report", side_effect=AssertionError("unexpected report")):
                        with contextlib.redirect_stdout(io.StringIO()):
                            status = updates.main(["--root", str(self.root), "--check-only"])
        self.assertEqual(status, 0)
        self.assertIn("has_updates=true", output.read_text())
        self.assertIn("current_json=", output.read_text())
        self.assertIn("latest_json=", output.read_text())
        self.assertIn("heads_marker=" + updates.heads_marker(self.latest), output.read_text())
        self.assertEqual(before, inventory(self.root))

    def test_main_reports_only_validated_changes(self):
        output = self.fixture / "outputs.txt"
        result = {"has_updates": True, "workflow_changed": True, "changed_files": ["config/example.txt"], "meaningful_files": ["config/example.txt"]}
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            with mock.patch.object(updates, "find_latest", return_value=self.latest):
                with mock.patch.object(updates, "prepare_updates", return_value=result):
                    with contextlib.redirect_stdout(io.StringIO()):
                        status = updates.main(["--root", str(self.root), "--prepare", "--report-dir", str(self.fixture)])
        self.assertEqual(status, 0)
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(values["has_updates"], "true")
        self.assertEqual(values["workflow_changed"], "true")
        self.assertEqual(values["branch"], "automation/upstream-sync")
        self.assertTrue(Path(values["body_path"]).read_text(encoding="utf-8").startswith(updates.PR_MARKER))

    def test_failed_main_keeps_publication_output_false(self):
        output = self.fixture / "outputs.txt"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            with mock.patch.object(updates, "find_latest", side_effect=updates.UpdateError("GitHub unavailable")):
                with contextlib.redirect_stderr(io.StringIO()):
                    status = updates.main(["--root", str(self.root), "--prepare"])
        self.assertEqual(status, 1)
        self.assertIn("has_updates=false", output.read_text())
        self.assertNotIn("has_updates=true", output.read_text())

    def test_workflow_behavior_changes_are_meaningful(self):
        staging = self.fixture / "staging"
        shutil.copytree(self.root, staging)
        (staging / sync.WORKFLOW_PATH).write_bytes(self.workflow + b"# new overlay behavior\n")
        before = sync.read_manifest(self.root)
        self.assertIn(sync.WORKFLOW_PATH, updates.meaningful_changes(self.root, staging, before, before))

    def test_heads_marker_rejects_non_sha_candidates(self):
        with self.assertRaisesRegex(updates.UpdateError, "Invalid candidate SHA"):
            updates.heads_marker({"abk": "dev\nunsafe", "catalog": "b" * 40})

    def test_prepare_pinned_candidates_never_resolves_latest_again(self):
        output = self.fixture / "pinned-output.txt"
        result = {"has_updates": False, "workflow_changed": False, "changed_files": [], "meaningful_files": []}
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            with mock.patch.object(updates, "find_latest", side_effect=AssertionError("must use probed SHAs")):
                with mock.patch.object(updates, "prepare_updates", return_value=result) as prepare:
                    with contextlib.redirect_stdout(io.StringIO()):
                        status = updates.main([
                            "--root", str(self.root), "--prepare",
                            "--abk-commit", "C" * 40, "--catalog-commit", "B" * 40,
                        ])
        self.assertEqual(status, 0)
        self.assertEqual(prepare.call_args.args[2], self.latest)
        self.assertIn("heads_marker=" + updates.heads_marker(self.latest), output.read_text())

    def test_pinned_commit_options_reject_missing_or_invalid_candidates(self):
        invalid_arguments = [
            ["--prepare", "--abk-commit", "a" * 40],
            ["--prepare", "--catalog-commit", "b" * 40],
            ["--prepare", "--abk-commit", "dev", "--catalog-commit", "b" * 40],
            ["--prepare", "--abk-commit", "a" * 40, "--catalog-commit", "z" * 40],
            ["--prepare", "--abk-commit", "a" * 39, "--catalog-commit", "b" * 40],
            ["--prepare", "--abk-commit", "a" * 40, "--catalog-commit", "b" * 40 + "\n"],
            ["--check-only", "--abk-commit", "a" * 40, "--catalog-commit", "b" * 40],
        ]
        before = inventory(self.root)
        with mock.patch.object(updates, "find_latest", side_effect=AssertionError("invalid input must not resolve")):
            with mock.patch.object(updates, "prepare_updates", side_effect=AssertionError("invalid input must not prepare")):
                for arguments in invalid_arguments:
                    with self.subTest(arguments=arguments):
                        with contextlib.redirect_stderr(io.StringIO()):
                            with self.assertRaises(SystemExit) as error:
                                updates.main(["--root", str(self.root), *arguments])
                        self.assertEqual(error.exception.code, 2)
        self.assertEqual(before, inventory(self.root))


if __name__ == "__main__":
    unittest.main()
