import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import free_build_space as space


class FreeBuildSpaceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = (ROOT / "tests" / (".cleanup-test-" + uuid.uuid4().hex)).resolve()
        self.root.mkdir()
        self.addCleanup(self.remove_fixture)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.unused = self.root / "unused-preinstalled"
        self.unused.mkdir()
        self.env = {"GITHUB_ACTIONS": "true", "GKI_RUNNER_ENVIRONMENT": "github-hosted",
                    "GITHUB_WORKSPACE": str(self.workspace), "HOME": str(self.root / "home"),
                    "RUNNER_TEMP": str(self.root / "runner-temp")}

    def remove_fixture(self):
        if self.root.parent != (ROOT / "tests").resolve() or not self.root.name.startswith(".cleanup-test-"):
            raise ValueError("Unexpected test cleanup directory")
        shutil.rmtree(self.root)

    def test_cleanup_requires_all_host_guards_before_any_deletion(self):
        for platform, override in (("win32", {}), ("linux", {"GITHUB_ACTIONS": "false"}),
                                   ("linux", {"GKI_RUNNER_ENVIRONMENT": "self-hosted"}),
                                   ("linux", {"GKI_RUNNER_ENVIRONMENT": ""})):
            with self.subTest(platform=platform, override=override), mock.patch.object(space.sys, "platform", platform), \
                    mock.patch.dict(os.environ, {**self.env, **override}, clear=True), \
                    mock.patch.object(space, "clean") as clean, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(space.main(), 1)
                clean.assert_not_called()

    def test_workspace_and_runtime_paths_are_reserved(self):
        with mock.patch.object(space.sys, "platform", "linux"):
            workspace, protected = space.environment_paths(self.env)
        self.assertEqual(workspace, self.workspace)
        for path in (self.workspace, Path(self.env["RUNNER_TEMP"]), self.root / "home/.cache",
                     self.root / "home/.ccache", Path(sys.executable).resolve()):
            self.assertIn(path, protected)

    def test_missing_root_or_nonexistent_workspace_is_rejected(self):
        for value in ("", str(Path(self.workspace.anchor)), str(self.root / "missing")):
            with self.subTest(value=value), mock.patch.object(space.sys, "platform", "linux"), \
                    self.assertRaises((OSError, ValueError)):
                space.environment_paths({**self.env, "GITHUB_WORKSPACE": value})

    def test_system_workspace_and_cache_paths_are_not_cleanup_targets(self):
        for path in (Path("/usr"), Path("/usr/bin/python3"), Path("/opt/hostedtoolcache"),
                     Path("/opt/hostedtoolcache/Python"), Path("/opt/hostedtoolcache/Node"),
                     self.workspace, self.root / "home/.ccache"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "explicitly allowed"):
                space.safe_directory(path, ())

    def test_allowed_directory_cannot_overlap_a_protected_path_in_either_direction(self):
        with mock.patch.object(space, "CLEANUP_DIRECTORIES", (self.unused,)):
            for protected in (self.unused, self.unused / "cache", self.root):
                with self.subTest(protected=protected), self.assertRaisesRegex(ValueError, "overlaps"):
                    space.safe_directory(self.unused, (protected,))

    def test_symlink_and_symlink_parent_are_never_followed(self):
        link = self.root / "preinstalled-link"
        try:
            link.symlink_to(self.unused, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this host")
        child = self.unused / "child"
        child.mkdir()
        for candidate in (link, link / "child"):
            with self.subTest(candidate=candidate), mock.patch.object(space, "CLEANUP_DIRECTORIES", (candidate,)), \
                    self.assertRaisesRegex(ValueError, "symlink"):
                space.safe_directory(candidate, ())

    def invoke_clean(self, free, **patches):
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(mock.patch.object(space, "print_disk_space"))
            stack.enter_context(mock.patch.object(space, "CLEANUP_DIRECTORIES", (self.unused,)))
            stack.enter_context(mock.patch.object(space.shutil, "disk_usage", side_effect=lambda _: SimpleNamespace(free=free())))
            run = stack.enter_context(mock.patch.object(space.subprocess, "run", return_value=SimpleNamespace(returncode=0)))
            for key, value in patches.items():
                stack.enter_context(mock.patch.object(space, key, value))
            space.clean(self.workspace, (self.workspace,))
            return run

    def test_enough_space_skips_every_deletion(self):
        run = self.invoke_clean(lambda: 50 * space.GIB)
        run.assert_not_called()
        self.assertTrue(self.unused.is_dir())

    def test_deletion_uses_fixed_argument_array_and_a_real_child_timeout(self):
        run = self.invoke_clean(lambda: 45 * space.GIB)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["sudo", "-n", "/usr/bin/timeout", "--kill-after=5s"])
        self.assertEqual(command[-4:], ["/usr/bin/rm", "-rf", "--", str(self.unused)])
        self.assertLessEqual(float(command[4][:-1]), space.DIRECTORY_SECONDS)
        self.assertLessEqual(run.call_args.kwargs["timeout"], space.DIRECTORY_SECONDS + 6)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_not_enough_space_fails_even_if_deletion_command_succeeded(self):
        with self.assertRaisesRegex(ValueError, "at least 40 GiB"):
            self.invoke_clean(lambda: 39 * space.GIB)

    def test_global_budget_exhaustion_stops_before_another_directory(self):
        with mock.patch.object(space.time, "monotonic", side_effect=(0, space.TOTAL_SECONDS + 1)):
            run = self.invoke_clean(lambda: 45 * space.GIB)
        run.assert_not_called()

    def test_timeout_or_failed_deletion_does_not_bypass_final_space_protection(self):
        for result in (SimpleNamespace(returncode=124), subprocess.TimeoutExpired("timeout", 90)):
            with self.subTest(result=result), contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(mock.patch.object(space, "print_disk_space"))
                stack.enter_context(mock.patch.object(space, "CLEANUP_DIRECTORIES", (self.unused,)))
                stack.enter_context(mock.patch.object(space.shutil, "disk_usage", return_value=SimpleNamespace(free=39 * space.GIB)))
                run = stack.enter_context(mock.patch.object(space.subprocess, "run"))
                if isinstance(result, Exception):
                    run.side_effect = result
                else:
                    run.return_value = result
                with self.assertRaisesRegex(ValueError, "at least 40 GiB"):
                    space.clean(self.workspace, (self.workspace,))


if __name__ == "__main__":
    unittest.main()
