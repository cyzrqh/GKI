import contextlib
import hashlib
import io
from pathlib import Path
import shutil
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import validate_susfs_hooks as hooks


class SusfsHookTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "tests" / f".susfs-hooks-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def source(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path.resolve()

    def call(self, symbol, file="common/fs/exec.c"):
        return self.source(file, f"extern void {symbol}(void);\nvoid caller(void) {{ {symbol}(); }}\n")

    def test_all_five_global_ksu_definitions_satisfy_kernel_calls(self):
        self.source("common/fs/exec.c", "void caller(void) {\n" + "\n".join(f"  {symbol}();" for symbol in hooks.HOOKS) + "\n}\n")
        self.source("KernelSU/kernel/compat.c", "\n".join(f"void {symbol}(void) {{ }}" for symbol in hooks.HOOKS))
        result = hooks.validate_hooks(self.root)
        self.assertTrue(result.ok)
        self.assertEqual(set(hooks.HOOKS), set(result.calls))

    def test_v22_local_static_selinux_helpers_satisfy_same_file_calls(self):
        for index, symbol in enumerate(("my_setprocattr", "my_write_context", "my_write_access")):
            self.source(f"common/security/selinux/helper{index}.c",
                        f"static int {symbol}(int value) {{ return value; }}\n"
                        f"int caller{index}(void) {{ return {symbol}(1); }}\n")
        result = hooks.validate_hooks(self.root)
        self.assertTrue(result.ok)
        self.assertEqual({"my_setprocattr", "my_write_context", "my_write_access"}, set(result.calls))

    def test_static_in_another_kernel_file_is_not_a_global_implementation(self):
        self.call("my_write_context", "common/security/selinux/hooks.c")
        self.source("common/security/selinux/selinuxfs.c", "static void my_write_context(void) {}\n")
        self.assertEqual({"my_write_context"}, set(hooks.validate_hooks(self.root).missing))

    def test_global_kernel_helper_can_satisfy_another_file(self):
        self.call("my_write_context")
        self.source("common/security/selinux/compat.c", "void my_write_context(void) {}\n")
        self.assertTrue(hooks.validate_hooks(self.root).ok)

    def test_prototype_static_comment_and_string_are_not_ksu_global_definitions(self):
        for implementation in (
            "extern void my_write_access(void);",
            "static void my_write_access(void) {}",
            "/* void my_write_access(void) {} */",
            'const char *text = "void my_write_access(void) {}";',
            "// void my_write_access(void) {}\n",
        ):
            with self.subTest(implementation=implementation):
                self.call("my_write_access")
                self.source("KernelSU/kernel/helper.c", implementation)
                result = hooks.validate_hooks(self.root)
                self.assertEqual({"my_write_access"}, set(result.missing))

    def test_no_calls_do_not_require_ksu(self):
        self.source("common/fs/exec.c", "extern void my_setprocattr(void);\n"
                    "/* my_write_access(); */\n"
                    'void f(void) { const char *x = "my_write_context()"; }\n')
        result = hooks.validate_hooks(self.root)
        self.assertTrue(result.ok)
        self.assertFalse(result.calls)
        self.assertIsNone(result.ksu_dir)

    def test_block_scope_prototype_is_not_a_call(self):
        self.source("common/fs/exec.c", "void f(void) { extern int my_setprocattr(int x); }\n")
        self.assertFalse(hooks.validate_hooks(self.root).calls)

    def test_calls_after_else_and_do_are_not_mistaken_for_local_prototypes(self):
        self.source("common/fs/exec.c", "void f(int enabled) {\n"
                    " if (enabled) {} else my_setprocattr();\n"
                    " do my_write_context(); while (enabled);\n}\n")
        result = hooks.validate_hooks(self.root)
        self.assertEqual({"my_setprocattr", "my_write_context"}, set(result.missing))

    def test_identifier_substrings_and_preprocessor_definitions_are_ignored(self):
        self.source("common/fs/exec.c", "#define MACRO() my_write_context()\n"
                    "#define OTHER() \\\n+  my_write_access()\n"
                    "void f(void) { not_my_setprocattr(); }\n")
        self.assertFalse(hooks.validate_hooks(self.root).calls)

    def test_ksu_internal_calls_are_excluded_from_kernel_external_requirements(self):
        self.source("common/fs/exec.c", "void f(void) {}\n")
        self.source("common/drivers/kernelsu/internal.c", "void f(void) { my_write_access(); }\n")
        self.assertFalse(hooks.validate_hooks(self.root).calls)

    def test_direct_source_root_and_explicit_relative_ksu_path(self):
        self.call("ksu_setup_preinit_file", "fs/exec.c")
        self.source("custom-ksu/helper.c", "void ksu_setup_preinit_file(void) {}\n")
        result = hooks.validate_hooks(self.root, Path("custom-ksu"))
        self.assertTrue(result.ok)
        self.assertEqual((self.root / "custom-ksu").resolve(), result.ksu_dir)

    def test_attributed_multiline_definition_is_recognized(self):
        self.call("ksu_preinit_handle_post_execveat")
        self.source("KernelSU/kernel/helper.c", "int __maybe_unused\nksu_preinit_handle_post_execveat(\n void\n) __attribute__((unused))\n{ return 0; }\n")
        self.assertTrue(hooks.validate_hooks(self.root).ok)

    def test_comment_and_escaped_string_contents_do_not_confuse_definition_detection(self):
        self.call("my_setprocattr")
        self.source("KernelSU/kernel/helper.c", 'const char *x = "quote: \\\" static void my_setprocattr() {}";\n'
                    "/* static */ int my_setprocattr(void) { return 0; }\n")
        self.assertTrue(hooks.validate_hooks(self.root).ok)

    def test_multiple_call_sites_report_only_unsatisfied_file_local_calls(self):
        self.source("common/fs/exec.c", "static void my_write_context(void) {}\nvoid f(void) { my_write_context(); }\n")
        missing_path = self.call("my_write_context", "common/security/selinux/hooks.c")
        result = hooks.validate_hooks(self.root)
        self.assertEqual([missing_path], [site.path for site in result.missing["my_write_context"]])

    def test_legal_ksu_symlink_resolves_actual_helper_source(self):
        self.call("my_write_access")
        target = self.source("KernelSU/kernel/helper.c", "void my_write_access(void) {}\n").parent
        link = self.root / "common/drivers/kernelsu"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Directory symlinks are not supported here: {error}")
        result = hooks.validate_hooks(self.root)
        self.assertTrue(result.ok)
        self.assertEqual(target, result.ksu_dir)

    def test_repeat_validation_is_pure_read_only(self):
        self.call("my_write_access")
        self.source("KernelSU/kernel/helper.c", "void my_write_access(void) {}\n")
        def state():
            return {path.relative_to(self.root).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                    for path in self.root.rglob("*") if path.is_file()}
        before = state()
        first = hooks.validate_hooks(self.root)
        second = hooks.validate_hooks(self.root)
        self.assertTrue(first.ok)
        self.assertEqual(first, second)
        self.assertEqual(before, state())

    def test_cli_reports_missing_and_compatible_version_guidance(self):
        self.call("ksu_setup_preinit_file")
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            status = hooks.main(["--kernel-root", str(self.root)])
        self.assertEqual(1, status)
        self.assertIn("ksu_setup_preinit_file", error.getvalue())
        self.assertIn("exec.c:2", error.getvalue())
        self.assertIn(hooks.COMPATIBILITY_MESSAGE, error.getvalue())


if __name__ == "__main__":
    unittest.main()
