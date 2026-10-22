import hashlib
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import apply_susfs23 as susfs


@unittest.skipUnless(shutil.which("git"), "Git is required")
class Susfs23ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "tests" / f".susfs23-{uuid.uuid4().hex}"
        self.addCleanup(self.cleanup_directory)
        self.repository = self.directory / "kernel-root/KernelSU"
        self.repository.mkdir(parents=True)
        self.run_git("init", "--quiet")
        self.run_git("config", "core.autocrlf", "false")
        self.run_git("config", "user.name", "SUSFS integration test")
        self.run_git("config", "user.email", "test@example.invalid")
        (self.repository / "kernel/core").mkdir(parents=True)
        self.build = self.repository / "kernel/Kbuild"
        self.core = self.repository / "kernel/core/init.c"
        self.build.write_text("kernelsu-objs := core/init.o\n", encoding="utf-8", newline="\n")
        self.core.write_text("/* original kernel init */\n", encoding="utf-8", newline="\n")
        self.run_git("add", ".")
        self.run_git("commit", "--quiet", "-m", "test source")
        self.head = self.run_git("rev-parse", "HEAD").stdout.strip()
        self.susfs = self.directory / "susfs/kernel_patches/include/linux"
        self.susfs.mkdir(parents=True)
        self.header = self.susfs / "susfs.h"
        self.header.write_text('#define SUSFS_VERSION "v2.3.0"\n', encoding="utf-8")
        self.core.write_text("/* integrated kernel init */\n", encoding="utf-8", newline="\n")
        self.build.write_text("kernelsu-objs := core/init.o preinit/preinit.o\n", encoding="utf-8", newline="\n")
        self.patch = self.directory / "fixture.patch"
        self.patch.write_text(self.run_git("diff", "--full-index").stdout, encoding="utf-8", newline="\n")
        self.run_git("restore", ".")

    def cleanup_directory(self):
        target = self.directory.resolve()
        if target.parent != (ROOT / "tests").resolve() or not target.name.startswith(".susfs23-"):
            raise ValueError("Unexpected test cleanup target")
        def remove_readonly(function, name, error):
            Path(name).chmod(stat.S_IWRITE)
            function(name)
        shutil.rmtree(target, onexc=remove_readonly)

    def run_git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.repository), *arguments], check=True,
                              capture_output=True, text=True, encoding="utf-8")

    def state(self):
        return {str(path.relative_to(self.repository)): (hashlib.sha256(path.read_bytes()).hexdigest(),
                                                       path.stat().st_mtime_ns)
                for path in (self.build, self.core)}

    def apply_fixture(self):
        # Exercise Git's real atomic apply/check/reverse behavior on a small repo.
        # The actual pinned source and complete patch are verified separately.
        with mock.patch.object(susfs, "SUKISU_COMMIT", self.head), \
                mock.patch.object(susfs, "verify_integration") as verify:
            result = susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
            verify.assert_called_once_with(self.repository.resolve())
            return result

    def test_complete_application_and_idempotent_reverification(self):
        self.assertTrue(self.apply_fixture())
        self.assertIn("integrated", self.core.read_text())
        self.assertIn("preinit/preinit.o", self.build.read_text())
        applied = self.state()
        self.assertFalse(self.apply_fixture())
        self.assertEqual(applied, self.state())

    def test_wrong_sukisu_commit_refused_before_modification(self):
        before = self.state()
        with self.assertRaisesRegex(ValueError, "requires SukiSU"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())

    def test_wrong_susfs_version_refused_before_modification(self):
        self.header.write_text('#define SUSFS_VERSION "v2.2.0"\n', encoding="utf-8")
        before = self.state()
        with mock.patch.object(susfs, "SUKISU_COMMIT", self.head), \
                self.assertRaisesRegex(ValueError, "requires v2.3.0"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())

    def test_source_drift_and_staged_drift_refused_without_overwriting(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                self.core.write_text("/* local change */\n", encoding="utf-8", newline="\n")
                if staged:
                    self.run_git("add", ".")
                before = self.state()
                with mock.patch.object(susfs, "SUKISU_COMMIT", self.head), \
                        self.assertRaisesRegex(ValueError, "differ from the pinned source"):
                    susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
                self.assertEqual(before, self.state())
                self.run_git("reset", "--hard", "--quiet", "HEAD")

    def test_one_failed_hunk_does_not_apply_other_hunks(self):
        content = self.patch.read_text().replace("-/* original kernel init */", "-/* another upstream revision */")
        self.patch.write_text(content, encoding="utf-8", newline="\n")
        before = self.state()
        with mock.patch.object(susfs, "SUKISU_COMMIT", self.head), \
                self.assertRaisesRegex(ValueError, "Complete SUSFS 2.3 patch does not apply"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())

    def test_partial_prior_application_is_rejected(self):
        self.core.write_text("/* integrated kernel init */\n", encoding="utf-8", newline="\n")
        before = self.state()
        with mock.patch.object(susfs, "SUKISU_COMMIT", self.head), \
                self.assertRaisesRegex(ValueError, "partial or conflicting patch"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())

    def test_complete_reverse_check_does_not_skip_integration_validation(self):
        self.apply_fixture()
        before = self.state()
        with mock.patch.object(susfs, "SUKISU_COMMIT", self.head), \
                mock.patch.object(susfs, "verify_integration", side_effect=ValueError("missing runner")), \
                self.assertRaisesRegex(ValueError, "missing runner"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())


if __name__ == "__main__":
    unittest.main()
