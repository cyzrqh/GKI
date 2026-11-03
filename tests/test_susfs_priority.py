from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from ensure_susfs_priority import complete_priority, priority_api, OPTION


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.priority-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.ksu = self.root / 'KernelSU/kernel'
        self.ksu.mkdir(parents=True)
        self.header = self.root / 'common/include/linux/susfs.h'
        self.header.parent.mkdir(parents=True)
        self.header.write_text('#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER\n'
                               'void susfs_set_priority_uid_filter(void **arg);\n'
                               'void susfs_parse_priority_app_strings(void);\n#endif\n')
        self.config = self.ksu / 'Kconfig'
        self.config.write_text('menu "KernelSU"\nconfig KSU_SUSFS\n\tbool "SUSFS"\nendmenu\n')
        self.implementation = self.root / 'common/fs/susfs.c'
        self.implementation.parent.mkdir()
        self.implementation.write_text('''#include <stddef.h>
#define __maybe_unused __attribute__((unused))
struct cred { int uid; };
static struct cred *susfs_switch_to_ksu_domain(void) { return NULL; }
static void susfs_restore_creds(struct cred *old) { (void)old; }
#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER
int applied;
int parsed;
void susfs_set_priority_uid_filter(void **arg) { applied = *(int *)*arg; }
void susfs_parse_priority_app_strings(void) { parsed++; }
#endif
''')
        self.dispatch = self.ksu / 'dispatch.c'
        self.dispatch.write_text('''#include "susfs.c"
#define CMD_SUSFS_SET_PRIORITY_UID_FILTER 48
void refresh(void) {
    susfs_parse_priority_app_strings();
}
int dispatch(int command, void **arg) {
    (void)arg;
    switch (command) {
        case CMD_SUSFS_SET_PRIORITY_UID_FILTER:
            susfs_set_priority_uid_filter(arg);
            return 0;
        default: return -1;
    }
}
int main(void) {
    int value = 42;
    void *arg = &value;
    refresh();
#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER
    return dispatch(48, &arg) || applied != value || parsed != 1;
#else
    return dispatch(48, &arg) != -1;
#endif
}
''')

    def test_adds_real_configuration_and_is_idempotent(self):
        self.assertEqual(3, len(complete_priority(self.root)))
        text = self.config.read_text()
        self.assertIn('depends on KSU_SUSFS\n\tdefault y', text)
        state = {path: path.read_bytes() for path in (self.config, self.dispatch, self.implementation)}
        self.assertEqual((), complete_priority(self.root))
        self.assertEqual(state, {path: path.read_bytes() for path in state})

    def test_unknown_dispatcher_fails_before_any_source_is_modified(self):
        self.dispatch.write_text('void f(void **arg) { susfs_set_priority_uid_filter(arg); }')
        state = self.config.read_bytes(), self.implementation.read_bytes()
        with self.assertRaisesRegex(ValueError, 'dispatcher'):
            complete_priority(self.root)
        self.assertEqual(state, (self.config.read_bytes(), self.implementation.read_bytes()))

    def test_old_unconditional_api_remains_unchanged(self):
        self.header.write_text('void susfs_set_priority_uid_filter(void **arg);\n'
                               'void susfs_parse_priority_app_strings(void);\n')
        self.implementation.write_text('void susfs_set_priority_uid_filter(void **arg) { (void)arg; }\n'
                                       'void susfs_parse_priority_app_strings(void) { renew(); }\n')
        state = {path: path.read_bytes() for path in (self.header, self.config, self.dispatch, self.implementation)}
        self.assertEqual((), complete_priority(self.root))
        self.assertEqual(state, {path: path.read_bytes() for path in state})

    def test_old_source_without_api_guards_only_consumers_without_fabricating_provider(self):
        # The actual 5.15 bd27d727 SUSFS header/implementation have no priority
        # feature at all. A newer manager port must not unconditionally call it.
        self.header.write_text('#define SUSFS_VERSION "v2.3.0"\nvoid susfs_init(void);\n')
        self.implementation.write_text('int native_susfs_started;\nvoid susfs_init(void) { native_susfs_started++; }\n')
        state = {path: path.read_bytes() for path in (self.header, self.config, self.implementation)}
        self.assertEqual((self.dispatch,), complete_priority(self.root))
        self.assertEqual(state, {path: path.read_bytes() for path in state})
        text = self.dispatch.read_text()
        self.assertEqual(3, text.count('#ifdef CONFIG_' + OPTION))  # Two consumers plus the original test branch.
        self.assertIn('susfs_parse_priority_app_strings();', text)
        self.assertIn('susfs_set_priority_uid_filter(arg);', text)
        self.assertEqual((), complete_priority(self.root))

    def test_partial_or_wrong_api_fails_before_any_source_is_modified(self):
        header = self.header.read_text()
        implementation = self.implementation.read_text()
        for changed_header, changed_implementation in (
            (header, implementation.replace('void susfs_parse_priority_app_strings(void) { parsed++; }', '')),
            (header.replace('void susfs_parse_priority_app_strings(void);', ''), implementation),
            (header.replace('void susfs_set_priority_uid_filter(void **arg);',
                            'int susfs_set_priority_uid_filter(void **arg);'), implementation),
            (header, implementation.replace('void susfs_parse_priority_app_strings(void) { parsed++; }',
                                            'void susfs_parse_priority_app_strings(void);')),
        ):
            with self.subTest(header=changed_header, implementation=changed_implementation):
                self.header.write_text(changed_header)
                self.implementation.write_text(changed_implementation)
                state = {path: path.read_bytes() for path in (self.config, self.dispatch, self.implementation)}
                with self.assertRaisesRegex(ValueError, 'priority API is incomplete'):
                    complete_priority(self.root)
                self.assertEqual(state, {path: path.read_bytes() for path in state})

    def test_old_absent_provider_cannot_advertise_enabled_configuration(self):
        self.header.write_text('void susfs_init(void);\n')
        self.implementation.write_text('void susfs_init(void) { init(); }\n')
        self.config.write_text(self.config.read_text() + '\nconfig ' + OPTION + '\n\tbool "priority"\n')
        before = self.dispatch.read_bytes()
        with self.assertRaisesRegex(ValueError, 'lacks its advertised priority'):
            complete_priority(self.root)
        self.assertEqual(before, self.dispatch.read_bytes())

    def test_unknown_refresh_shape_fails_before_any_source_is_modified(self):
        self.dispatch.write_text('void f(void) { susfs_parse_priority_app_strings(); }\n')
        state = {path: path.read_bytes() for path in (self.config, self.dispatch, self.implementation)}
        with self.assertRaisesRegex(ValueError, 'priority refresh'):
            complete_priority(self.root)
        self.assertEqual(state, {path: path.read_bytes() for path in state})

    def test_comments_and_strings_do_not_create_fake_calls(self):
        path = self.ksu / 'unused.c'
        original = ('/*\nsusfs_parse_priority_app_strings();\n*/\n'
                    'const char *text = "susfs_set_priority_uid_filter(arg);";\n')
        path.write_text(original)
        complete_priority(self.root)
        self.assertEqual(original, path.read_text())

    @unittest.skipUnless(shutil.which('cc'), 'Linux C compiler required')
    def test_filter_both_enabled_and_disabled_compile_with_werror_and_run(self):
        complete_priority(self.root)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                output = self.root / ('priority-on' if enabled else 'priority-off')
                command = ['cc', '-Wall', '-Wextra', '-Werror', '-I', str(self.implementation.parent)]
                if enabled:
                    command.append('-DCONFIG_KSU_SUSFS_PRIORITY_UID_FILTER=1')
                subprocess.run([*command, str(self.dispatch), '-o', str(output)], check=True, capture_output=True)
                subprocess.run([str(output)], check=True, capture_output=True)

    @unittest.skipUnless(shutil.which('cc'), 'Linux C compiler required')
    def test_absent_native_feature_compiles_and_returns_original_unknown_command(self):
        self.header.write_text('void susfs_init(void);\n')
        self.implementation.write_text('int original_feature;\nvoid susfs_init(void) { original_feature++; }\n')
        complete_priority(self.root)
        output = self.root / 'priority-absent'
        subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-I', str(self.implementation.parent),
                        str(self.dispatch), '-o', str(output)], check=True, capture_output=True)
        subprocess.run([str(output)], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
