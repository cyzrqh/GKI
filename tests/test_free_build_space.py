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

    def test_partial_sdk_cleanup_resumes_after_other_tools_and_reclaims_real_fixture(self):
        other = self.root / "other-unused-tool"
        other.mkdir()
        (other / "payload").write_text("unused tool", encoding="utf-8")
        (self.unused / "remaining-sdk").write_text("partially removed SDK", encoding="utf-8")
        free = 13.6 * space.GIB
        attempts = []

        def delete_fixture(command, **kwargs):
            nonlocal free
            path = Path(command[-1])
            self.assertIn(path, (self.unused, other))
            self.assertEqual(path.parent, self.root)
            self.assertEqual(command[-4:-1], ["/usr/bin/rm", "-rf", "--"])
            attempts.append(path)
            if len(attempts) == 1:
                free = 20 * space.GIB
                return SimpleNamespace(returncode=124)
            shutil.rmtree(path)
            free = (34.9 if path == other else 51) * space.GIB
            return SimpleNamespace(returncode=0)

        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(space, "print_disk_space"), \
                mock.patch.object(space, "CLEANUP_DIRECTORIES", (self.unused, other)), \
                mock.patch.object(space.shutil, "disk_usage", side_effect=lambda _: SimpleNamespace(free=free)), \
                mock.patch.object(space.time, "monotonic", side_effect=(0, 0, 0, 90, 90, 90, 192, 192, 192, 250)), \
                mock.patch.object(space.subprocess, "run", side_effect=delete_fixture) as run:
            space.clean(self.workspace, (self.workspace,))

        self.assertEqual(attempts, [self.unused, other, self.unused])
        self.assertEqual([call.args[0][4] for call in run.call_args_list], ["90.0s", "90.0s", "180.0s"])
        self.assertFalse(self.unused.exists())
        self.assertFalse(other.exists())
        self.assertTrue(self.workspace.is_dir())

    def test_retry_is_clipped_to_global_deadline(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(space, "print_disk_space"), \
                mock.patch.object(space, "CLEANUP_DIRECTORIES", (self.unused,)), \
                mock.patch.object(space.shutil, "disk_usage", return_value=SimpleNamespace(free=45 * space.GIB)), \
                mock.patch.object(space.time, "monotonic", side_effect=(0, 0, 0, 90, 400, 400, 410)), \
                mock.patch.object(space.subprocess, "run", side_effect=(SimpleNamespace(returncode=124), SimpleNamespace(returncode=0))) as run:
            space.clean(self.workspace, (self.workspace,))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][4], "14.0s")
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_retry_stops_when_global_budget_is_already_exhausted(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(space, "print_disk_space"), \
                mock.patch.object(space, "CLEANUP_DIRECTORIES", (self.unused,)), \
                mock.patch.object(space.shutil, "disk_usage", return_value=SimpleNamespace(free=39 * space.GIB)), \
                mock.patch.object(space.time, "monotonic", side_effect=(0, 0, 0, 90, space.TOTAL_SECONDS)), \
                mock.patch.object(space.subprocess, "run", return_value=SimpleNamespace(returncode=124)) as run:
            with self.assertRaisesRegex(ValueError, "at least 40 GiB"):
                space.clean(self.workspace, (self.workspace,))
        run.assert_called_once()

    def test_retry_is_skipped_when_first_attempt_reached_the_target(self):
        free = 45 * space.GIB

        def timed_out_after_reclaiming(*args, **kwargs):
            nonlocal free
            free = 50 * space.GIB
            return SimpleNamespace(returncode=124)

        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(space, "print_disk_space"), \
                mock.patch.object(space, "CLEANUP_DIRECTORIES", (self.unused,)), \
                mock.patch.object(space.shutil, "disk_usage", side_effect=lambda _: SimpleNamespace(free=free)), \
                mock.patch.object(space.subprocess, "run", side_effect=timed_out_after_reclaiming) as run:
            space.clean(self.workspace, (self.workspace,))
        run.assert_called_once()

    def test_retry_rechecks_path_safety_before_deletion(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(space, "print_disk_space"), \
                mock.patch.object(space, "CLEANUP_DIRECTORIES", (self.unused,)), \
                mock.patch.object(space.shutil, "disk_usage", return_value=SimpleNamespace(free=45 * space.GIB)), \
                mock.patch.object(space, "safe_directory", side_effect=(self.unused, ValueError("Path changed to a symlink"))) as safe, \
                mock.patch.object(space.subprocess, "run", return_value=SimpleNamespace(returncode=124)) as run:
            space.clean(self.workspace, (self.workspace,))
        self.assertEqual(safe.call_count, 2)
        run.assert_called_once()

    def test_timeout_or_failed_deletion_does_not_bypass_final_space_protection(self):
        for result, attempts in ((SimpleNamespace(returncode=124), 2), (SimpleNamespace(returncode=137), 2),
                                 (subprocess.TimeoutExpired("timeout", 90), 2),
                                 (SimpleNamespace(returncode=1), 1), (OSError("sudo unavailable"), 1)):
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
                self.assertEqual(run.call_count, attempts)


if __name__ == "__main__":
    unittest.main()
