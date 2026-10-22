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
        self.providers = []
        for arch in susfs.PATCH_MEMORY_ARCHES:
            provider = self.repository / f"kernel/hook/{arch}/patch_memory.c"
            provider.parent.mkdir(parents=True)
            provider.write_text("int ksu_patch_text(void *dst, void *src, int len, int flags)\n"
                                "{ return stop_machine(patch_callback, dst, cpu_online_mask); }\n",
                                encoding="utf-8", newline="\n")
            self.providers.append(provider)
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

    def test_missing_or_modified_text_provider_fails_before_applying_any_hunk(self):
        for removed in (False, True):
            with self.subTest(removed=removed):
                provider = self.providers[0]
                if removed:
                    provider.unlink()
                else:
                    provider.write_text("int ksu_patch_text(void) { return 0; }\n",
                                        encoding="utf-8", newline="\n")
                before = self.state()
                with mock.patch.object(susfs, "SUKISU_COMMIT", self.head), \
                        self.assertRaisesRegex(ValueError, "text patch providers differ"):
                    susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
                self.assertEqual(before, self.state())
                self.run_git("restore", ".")


class PatchMemoryBuildTests(unittest.TestCase):
    RULES = """ifeq ($(CONFIG_ARM64),y)
kernelsu-objs += hook/arm64/patch_memory.o
else ifeq ($(CONFIG_X86_64),y)
kernelsu-objs += hook/x86_64/patch_memory.o
else ifeq ($(CONFIG_RISCV),y)
kernelsu-objs += hook/riscv64/patch_memory.o
endif
"""

    def test_arch_providers_remain_available_without_dynamic_syscall_registration(self):
        susfs.verify_patch_memory_build(self.RULES)

    def test_dropped_arch_provider_cannot_pass_integration_validation(self):
        for arch in susfs.PATCH_MEMORY_ARCHES:
            with self.subTest(arch=arch), self.assertRaisesRegex(ValueError, "real patch_memory object"):
                susfs.verify_patch_memory_build(self.RULES.replace(f"hook/{arch}/patch_memory.o", ""))

    def test_old_syscall_registration_cannot_be_restored_as_a_provider_fix(self):
        with self.assertRaisesRegex(ValueError, "Legacy syscall hook registration"):
            susfs.verify_patch_memory_build(self.RULES + "kernelsu-objs += hook/syscall_hook_manager.o\n")


class ModuleBlacklistLifecycleTests(unittest.TestCase):
    CORE = "void kernelsu_exit(void) { ksu_module_blacklist_exit(); }\n"
    RUNTIME = '''void second_stage(void) {
    if (check_argv(*argv, 1, "second_stage", buf, sizeof(buf))) {
        setup_ksu_cred();
        ksu_feature_apply_forced();
        ksu_module_blacklist_apply();
        static_branch_disable(&is_init_second_stage_not_executed);
    }
}
'''

    def test_sukisu_static_param_and_second_stage_lifecycle_is_preserved(self):
        susfs.verify_blacklist_lifecycle(self.CORE, self.RUNTIME)

    def test_foreign_kernelsu_init_api_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "foreign init hook"):
            susfs.verify_blacklist_lifecycle(self.CORE + "void init(void) { ksu_module_blacklist_init(); }",
                                             self.RUNTIME)

    def test_moving_apply_to_early_core_init_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "before second-stage"):
            susfs.verify_blacklist_lifecycle(self.CORE + "void init(void) { ksu_module_blacklist_apply(); }",
                                             self.RUNTIME.replace("ksu_module_blacklist_apply();", ""))

    def test_missing_second_stage_apply_or_exit_cannot_silently_disable_feature(self):
        with self.assertRaisesRegex(ValueError, "exactly once"):
            susfs.verify_blacklist_lifecycle(self.CORE, self.RUNTIME.replace("ksu_module_blacklist_apply();", ""))
        with self.assertRaisesRegex(ValueError, "teardown hook"):
            susfs.verify_blacklist_lifecycle("", self.RUNTIME)

    def test_apply_outside_second_stage_guard_is_rejected(self):
        runtime = (self.RUNTIME.replace("ksu_module_blacklist_apply();", "")
                   + "void other(void) { ksu_module_blacklist_apply(); }")
        with self.assertRaisesRegex(ValueError, "exactly once"):
            susfs.verify_blacklist_lifecycle(self.CORE, runtime)


if __name__ == "__main__":
    unittest.main()
