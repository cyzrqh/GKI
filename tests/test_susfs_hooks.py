import contextlib
import hashlib
import io
import os
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

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

    def baka_alias(self):
        self.source('common/security/selinux/hooks.c',
                    'int caller(void) { if (ksu_selinux_hide_running) return my_setprocattr(); return 0; }\n')
        self.source('KernelSU/kernel/Kbuild', 'REPO_NAME := BakaSU\nelse ifdef CONFIG_KSU_SUSFS\n'
                    '  $(info -- $(REPO_NAME): using SuSFS Inline hook)\n'
                    '  include $(KSU_SRC)/tools/inline_hook_check.mk\n'
                    '  include $(KSU_SRC)/tools/susfs_compat.mk\n')
        self.source('KernelSU/kernel/tools/susfs_compat.mk',
                    'ifeq ($(shell grep -q "ksu_selinux_hide_running" $(srctree)/security/selinux/hooks.c; echo $$?),0)\n'
                    '$(info -- $(REPO_NAME)/susfs_feature_check: selinux_hide manual hook found)\n'
                    'ccflags-y += -DKSU_COMPAT_HAS_SUSFS_FEATURE_SELINUX_HIDE\nendif\n')
        return self.source('KernelSU/kernel/feature/selinux_hide.c',
                           '#ifdef KSU_COMPAT_HAS_SUSFS_FEATURE_SELINUX_HIDE\n'
                           '    #define ksu_handle_selinux_setprocattr my_setprocattr\n#else\n#endif\n'
                           '#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 11, 0)\n'
                           'int __nocfi ksu_handle_selinux_setprocattr(const char *name, void *value, size_t size)\n'
                           '#else\n'
                           'int __nocfi ksu_handle_selinux_setprocattr(struct task_struct *p, char *name, void *value, size_t size)\n'
                           '#endif\n{\n'
                           ' int error = ksu_avc_has_perm_compat();\n'
                           ' struct cred *new = prepare_creds();\n'
                           ' if (error) { abort_creds(new); return error; }\n'
                           ' commit_creds(new);\n return size;\n}\n')

    def test_reviewed_bakasu_native_alias_with_conditional_prototypes_satisfies_hook(self):
        self.baka_alias()
        result = hooks.validate_hooks(self.root, variant='BakaSU')
        self.assertTrue(result.ok)
        self.assertEqual({'my_setprocattr'}, set(result.calls))
        self.assertTrue(any(item.symbol == 'my_setprocattr' and not item.is_static for item in result.definitions))
        self.assertEqual({'my_setprocattr'}, set(hooks.validate_hooks(self.root, variant='SukiSU').missing))

    def test_bakasu_alias_requires_native_guard_build_flag_and_actual_kernel_hook(self):
        self.baka_alias()
        cases = (
            ('KernelSU/kernel/feature/selinux_hide.c', 'KSU_COMPAT_HAS_SUSFS_FEATURE_SELINUX_HIDE', 'UNREVIEWED_FLAG'),
            ('KernelSU/kernel/feature/selinux_hide.c', '#else', '#undef ksu_handle_selinux_setprocattr\n#else'),
            ('KernelSU/kernel/Kbuild', 'REPO_NAME := BakaSU', 'REPO_NAME := SukiSU'),
            ('KernelSU/kernel/Kbuild', 'else ifdef CONFIG_KSU_SUSFS', 'else ifdef CONFIG_UNRELATED'),
            ('KernelSU/kernel/tools/susfs_compat.mk', '-DKSU_COMPAT_HAS_SUSFS_FEATURE_SELINUX_HIDE', '-DOTHER_FLAG'),
            ('common/security/selinux/hooks.c', 'ksu_selinux_hide_running', 'unrelated_state'),
        )
        for relative, original, changed in cases:
            path = self.root / relative
            text = path.read_text(encoding='utf-8')
            try:
                path.write_text(text.replace(original, changed), encoding='utf-8')
                with self.subTest(path=relative, changed=changed):
                    self.assertEqual({'my_setprocattr'}, set(hooks.validate_hooks(self.root, variant='BakaSU').missing))
            finally:
                path.write_text(text, encoding='utf-8')

    def test_alias_declaration_static_stub_or_unknown_source_file_cannot_satisfy_hook(self):
        path = self.baka_alias()
        original = path.read_text(encoding='utf-8')
        alias = original.split('#if LINUX_VERSION_CODE', 1)[0]
        bodies = (
            'int ksu_handle_selinux_setprocattr(const char *name, void *value, size_t size);\n',
            original[len(alias):].replace('int __nocfi', 'static int __nocfi'),
            'int ksu_handle_selinux_setprocattr(void) { return 0; }\n',
            '/* int ksu_handle_selinux_setprocattr(void) { prepare_creds(); commit_creds(); abort_creds(); ksu_avc_has_perm_compat(); return size; } */\n',
        )
        for body in bodies:
            with self.subTest(body=body):
                path.write_text(alias + body, encoding='utf-8')
                self.assertEqual({'my_setprocattr'}, set(hooks.validate_hooks(self.root, variant='BakaSU').missing))
        path.write_text('', encoding='utf-8')
        self.source('KernelSU/kernel/unreviewed_helper.c', original)
        self.assertEqual({'my_setprocattr'}, set(hooks.validate_hooks(self.root, variant='BakaSU').missing))

    def test_bakasu_alias_validation_is_read_only_and_cli_uses_selected_variant(self):
        self.baka_alias()
        before = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                  for path in self.root.rglob('*') if path.is_file()}
        with patch.dict(os.environ, {'KSU_VARIANT': 'BakaSU'}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, hooks.main(['--kernel-root', str(self.root)]))
        after = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

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
