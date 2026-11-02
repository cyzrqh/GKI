from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from ensure_susfs_priority import complete_priority


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
                               'void susfs_set_priority_uid_filter(void **arg);\n#endif\n')
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
void susfs_set_priority_uid_filter(void **arg) { applied = *(int *)*arg; }
#endif
''')
        self.dispatch = self.ksu / 'dispatch.c'
        self.dispatch.write_text('''#include "susfs.c"
#define CMD_SUSFS_SET_PRIORITY_UID_FILTER 48
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
#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER
    return dispatch(48, &arg) || applied != value;
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
        self.header.write_text('void susfs_set_priority_uid_filter(void **arg);\n')
        self.assertEqual((), complete_priority(self.root))

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


if __name__ == '__main__':
    unittest.main()
