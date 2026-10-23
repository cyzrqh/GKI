import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import checkout_ksu_snapshot as checkout
import snapshot_ksu_source as snapshot


@unittest.skipUnless(shutil.which("git"), "Git is required")
class KsuSourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = snapshot.temporary_directory(ROOT / "tests", ".ksu-snapshot-")
        self.root = self.temporary.__enter__()
        self.addCleanup(self.temporary.__exit__, None, None, None)
        self.source = self.root / "official"
        self.source.mkdir()
        self.git(self.source, "init", "--quiet")
        self.git(self.source, "config", "user.name", "Snapshot regression test")
        self.git(self.source, "config", "user.email", "test@example.invalid")
        self.git(self.source, "config", "core.autocrlf", "false")
        self.git(self.source, "remote", "add", "origin", "https://github.com/" + snapshot.REPOSITORY + ".git")
        (self.source / "kernel").mkdir()
        (self.source / "kernel/Kbuild").write_text("original kernel\n", encoding="utf-8", newline="\n")
        (self.source / "uapi").mkdir()
        (self.source / "uapi/ioctl.h").write_text("original UAPI\n", encoding="utf-8", newline="\n")
        self.commit("initial reviewed source")
        self.git(self.source, "tag", "-a", "v4.2.1-r1", "-m", "official version tag")
        (self.source / "manager.txt").write_text("manager update\n", encoding="utf-8", newline="\n")
        self.commit("manager revision")
        self.reference = self.git(self.source, "rev-parse", "HEAD")
        self.output = self.root / "bundle"
        self.destination = self.root / "restored"

    def git(self, directory, *arguments):
        return subprocess.run(["git", "-C", str(directory), *arguments], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()

    def commit(self, message):
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "--quiet", "-m", message)

    def create(self, **kwargs):
        return snapshot.snapshot_source(self.source, self.output, self.reference, root=self.root, **kwargs)

    def manifest(self):
        return json.loads((self.output / snapshot.MANIFEST_NAME).read_text(encoding="utf-8"))

    def change_manifest(self, **changes):
        document = self.manifest()
        document.update(changes)
        (self.output / snapshot.MANIFEST_NAME).write_text(json.dumps(document), encoding="utf-8")

    def restore(self):
        return checkout.checkout_snapshot(self.output, self.destination, self.reference)

    def test_full_history_tags_tree_and_native_version_survive_upstream_rewrite(self):
        original = self.create()
        self.assertEqual(original["git_count"], 2)
        self.assertTrue(original["git_describe"].startswith("v4.2.1-r1-1-g"))
        # The source branch is rewritten and even its local object store removed.
        # Later matrix jobs must not require any upstream object or connection.
        for path in (self.source / ".git").rglob("*"):
            if path.is_file():
                path.chmod(stat.S_IWRITE | stat.S_IREAD)
        shutil.rmtree(self.source)
        restored = self.restore()
        self.assertEqual(restored, original)
        self.assertEqual(self.git(self.destination, "rev-parse", "HEAD"), self.reference)
        self.assertEqual(self.git(self.destination, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(self.git(self.destination, "describe", "--tags", "--always"), original["git_describe"])
        self.assertEqual(self.git(self.destination, "rev-parse", "HEAD^{tree}"), original["git_tree"])
        self.assertEqual(self.git(self.destination, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.destination, "rev-parse", "--is-shallow-repository"), "false")

    def test_git_authentication_configuration_is_never_exported(self):
        secret = "AUTHORIZATION: basic snapshot-fixture-secret"
        self.git(self.source, "config", "http.https://github.com/.extraheader", secret)
        self.create()
        self.assertNotIn(secret.encode(), (self.output / snapshot.BUNDLE_NAME).read_bytes())
        self.restore()
        self.assertNotIn(secret, self.git(self.destination, "config", "--list"))

    def test_mismatched_expected_head_fails_before_export(self):
        with self.assertRaisesRegex(snapshot.SnapshotError, "HEAD does not match"):
            snapshot.snapshot_source(self.source, self.output, "a" * 40, root=self.root)
        self.assertFalse(self.output.exists())

    def test_shallow_history_is_refused_before_export(self):
        # Git's documented shallow boundary is enough to reproduce a depth-one
        # checkout without invoking a remote transport in the regression suite.
        (self.source / ".git/shallow").write_text(self.reference + "\n", encoding="ascii")
        self.assertEqual(self.git(self.source, "rev-list", "--count", "HEAD"), "1")
        with self.assertRaisesRegex(snapshot.SnapshotError, "shallow"):
            self.create()

    def test_dirty_or_credential_bearing_source_is_refused(self):
        (self.source / "private.key").write_text("never export", encoding="utf-8")
        with self.assertRaisesRegex(snapshot.SnapshotError, "must be clean"):
            self.create()
        (self.source / "private.key").unlink()
        self.git(self.source, "remote", "set-url", "origin",
                 "https://sensitive-token@github.com/" + snapshot.REPOSITORY + ".git")
        with self.assertRaisesRegex(snapshot.SnapshotError, "without URL credentials") as error:
            self.create()
        self.assertNotIn("sensitive-token", str(error.exception))

    def test_existing_export_or_checkout_is_never_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("existing output", encoding="utf-8")
        with self.assertRaisesRegex(snapshot.SnapshotError, "already exists"):
            self.create()
        self.assertEqual(sentinel.read_text(), "existing output")
        sentinel.unlink()
        self.output.rmdir()
        self.create()
        self.destination.mkdir()
        existing = self.destination / "keep.txt"
        existing.write_text("existing checkout", encoding="utf-8")
        with self.assertRaisesRegex(snapshot.SnapshotError, "already exists"):
            self.restore()
        self.assertEqual(existing.read_text(), "existing checkout")

    def test_corrupted_bundle_digest_is_rejected_without_checkout(self):
        self.create()
        with (self.output / snapshot.BUNDLE_NAME).open("ab") as stream:
            stream.write(b"corruption")
        with self.assertRaisesRegex(snapshot.SnapshotError, "size or SHA-256"):
            self.restore()
        self.assertFalse(self.destination.exists())

    def test_invalid_git_bundle_rejected_even_with_updated_size_and_hash(self):
        self.create()
        bundle = self.output / snapshot.BUNDLE_NAME
        bundle.write_bytes(b"not a Git bundle")
        self.change_manifest(bundle_size=bundle.stat().st_size, bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest())
        with self.assertRaisesRegex(snapshot.SnapshotError, "Git bundle failed"):
            self.restore()
        self.assertFalse(self.destination.exists())

    def test_manifest_cannot_switch_commit_repository_filename_or_native_version(self):
        self.create()
        original = self.manifest()
        for field, value in (("ref", "b" * 40), ("repository", "other/repo"),
                             ("bundle_filename", "../outside.bundle"), ("git_count", 999),
                             ("git_describe", "fake-release"), ("git_tree", "a" * 40),
                             ("bundle_size", True), ("schema_version", True)):
            with self.subTest(field=field):
                (self.output / snapshot.MANIFEST_NAME).write_text(json.dumps({**original, field: value}), encoding="utf-8")
                with self.assertRaises(snapshot.SnapshotError):
                    self.restore()
                self.assertFalse(self.destination.exists())

    def test_susfs_tree_drift_fails_before_patch_or_matrix_export(self):
        profile = {"version": "v2.3.0", "reviewed_kernel_tree": "a" * 40, "reviewed_uapi_tree": "b" * 40}
        with mock.patch.object(snapshot, "load_profile", return_value=profile):
            with self.assertRaisesRegex(snapshot.SnapshotError, "kernel tree changed"):
                self.create(susfs=True, tier="Latest(最新)")
        self.assertFalse(self.output.exists())

    def test_custom_configuration_does_not_force_reviewed_port(self):
        configuration = self.root / "config/config"
        configuration.parent.mkdir()
        configuration.write_text("custom=true\n", encoding="utf-8")
        with mock.patch.object(snapshot, "load_profile", side_effect=AssertionError("custom profile should be preserved")):
            self.create(susfs=True, tier="Latest(最新)")

    def test_reviewed_port_is_checked_on_isolated_clone_and_export_stays_original(self):
        import apply_susfs23
        profile = {"version": "v2.3.0",
                   "reviewed_kernel_tree": self.git(self.source, "rev-parse", "HEAD:kernel"),
                   "reviewed_uapi_tree": self.git(self.source, "rev-parse", "HEAD:uapi")}
        original = (self.source / "kernel/Kbuild").read_bytes()
        (self.source / "kernel/Kbuild").write_text("ported kernel\n", encoding="utf-8", newline="\n")
        patch = self.git(self.source, "diff", "--full-index") + "\n"
        self.git(self.source, "restore", ".")
        patch_path = self.root / "tools/patches/sukisu-susfs23.patch"
        patch_path.parent.mkdir(parents=True)
        patch_path.write_text(patch, encoding="utf-8", newline="\n")
        def verify_port(directory):
            self.assertNotEqual(directory, self.source)
            self.assertEqual((directory / "kernel/Kbuild").read_text(), "ported kernel\n")
        with mock.patch.object(snapshot, "load_profile", return_value=profile), \
                mock.patch.object(apply_susfs23, "verify_patch_memory_sources") as provider, \
                mock.patch.object(apply_susfs23, "verify_integration", side_effect=verify_port) as verify:
            self.create(susfs=True, tier="Stable(标准)")
            provider.assert_called_once()
            verify.assert_called_once()
        self.assertEqual((self.source / "kernel/Kbuild").read_bytes(), original)
        self.assertEqual(self.git(self.source, "status", "--porcelain"), "")
        self.restore()
        self.assertEqual((self.destination / "kernel/Kbuild").read_bytes(), original)

    def test_timed_out_git_command_is_sanitized(self):
        error = subprocess.TimeoutExpired("credential-bearing-command", 1, stderr="sensitive-token")
        with mock.patch.object(snapshot.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(snapshot.SnapshotError, "could not complete") as found:
                snapshot.git(self.source, "rev-parse", "HEAD")
        self.assertNotIn("sensitive-token", str(found.exception))


if __name__ == "__main__":
    unittest.main()
