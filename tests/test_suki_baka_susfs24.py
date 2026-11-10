import shutil
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import suki_baka_susfs24 as compat
from ensure_bakasu_symbol_lookup import _matches_reviewed_dispatch
from ensure_susfs_priority import guard_consumer


SUKI_DISPATCH = '''int ksu_handle_sys_reboot(int magic2, unsigned int cmd, void **arg)
{
    if (magic2 == SUSFS_MAGIC && current_uid().val == 0) {
        switch(cmd) {
            case CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING:
                susfs_set_avc_log_spoofing(arg);
                return 0;
        }
    }
    return -EINVAL;
}
'''
BAKA_DISPATCH = '''#ifdef CONFIG_KSU_SUSFS
int ksu_handle_susfs_cmd(unsigned int cmd, void __user **arg)
{
    switch (cmd) {
    case CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING: {
        susfs_set_avc_log_spoofing(arg);
        return 0;
    }
    }
    return -EINVAL;
}
#endif
'''
BAKA_GATE = '''int ksu_handle_sys_reboot(int magic2, unsigned int cmd, void **arg)
{
    if (ksu_get_uid_t(current_uid()) != 0)
        return 0;
    if (magic2 == SUSFS_MAGIC) {
        return ksu_handle_susfs_cmd(cmd, arg);
    }
    return 0;
}
'''
PREINIT = '''int ksu_setup_preinit_file(struct filename **filename_ptr, struct user_arg_ptr *argv_user)
{
    return ksu_open_preinit(filename_ptr, argv_user);
}
int ksu_preinit_handle_post_execveat(int *fd, struct filename **filename_ptr, void *argv_user, void *envp_user,
                                    int *flags, int *retval, int *preinit_fd)
{
    return ksu_finish_preinit(fd, filename_ptr, argv_user, envp_user, flags, retval, preinit_fd);
}
'''


class ManagerSusfs24Tests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.suki-baka-susfs24-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def source(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def test_priority_command_stays_in_reviewed_root_only_dispatch(self):
        self.source('kernel/supercall/supercall.c', BAKA_GATE)
        for variant, original in (('SukiSU', SUKI_DISPATCH), ('BakaSU', BAKA_DISPATCH)):
            with self.subTest(variant=variant):
                updated = compat.dispatch_with_priority(self.root, original, variant)
                self.assertEqual(1, updated.count('case ' + compat.COMMAND))
                self.assertIn('#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER\n', updated)
                self.assertEqual(updated, compat.dispatch_with_priority(self.root, updated, variant))
                self.assertEqual(updated, guard_consumer(self.root / compat.DISPATCH, updated))
        self.source('kernel/supercall/supercall.c', BAKA_GATE.replace('current_uid()) != 0', 'current_uid()) == 0'))
        with self.assertRaisesRegex(ValueError, 'native root gate'):
            compat.dispatch_with_priority(self.root, BAKA_DISPATCH, 'BakaSU')
        with self.assertRaisesRegex(ValueError, 'native root gate'):
            compat.dispatch_with_priority(self.root, SUKI_DISPATCH.replace(' && current_uid().val == 0', ''), 'SukiSU')

    def test_refresh_runs_after_each_reviewed_bakasu_package_scan(self):
        header = '#include "klog.h" // IWYU pragma: keep\n\n'
        one = 'void scan(void) {\n        track_throne(0);\n}\n'
        two = '''void scan(void) {
    track_throne(TRACK_THRONE_FORCE_SYNCHRONOUS);
}
void fallback(void) {
    track_throne(TRACK_THRONE_FORCE_SYNCHRONOUS);
}
'''
        for scans, count in ((one, 1), (two, 2)):
            with self.subTest(count=count):
                updated = compat.observer_with_priority(header + scans, 'BakaSU')
                self.assertEqual(count, updated.count('susfs_parse_priority_app_strings();'))
                self.assertEqual(1, updated.count('#include <linux/susfs.h>'))
                self.assertEqual(updated, compat.observer_with_priority(updated, 'BakaSU'))
                self.assertEqual(updated, guard_consumer(self.root / compat.OBSERVER, updated))
        with self.assertRaisesRegex(ValueError, 'package-list refresh callback'):
            compat.observer_with_priority(header + one.replace('track_throne(0)', 'track_throne(true)'), 'BakaSU')

    def test_bakasu_lookup_only_accepts_the_exact_susfs_dispatch_delta(self):
        self.source('kernel/supercall/supercall.c', BAKA_GATE)
        native = BAKA_DISPATCH.encode()
        reviewed = compat.dispatch_with_priority(self.root, BAKA_DISPATCH, 'BakaSU').encode()
        self.assertTrue(_matches_reviewed_dispatch(self.root, reviewed, native))
        self.assertFalse(_matches_reviewed_dispatch(self.root, reviewed + b'/* other change */\n', native))
        self.assertFalse(_matches_reviewed_dispatch(self.root, reviewed.replace(b'susfs_set_priority_uid_filter(arg);',
                                                                                 b'unreviewed_call(arg);'), native))

    def test_native_preinit_option_requires_real_providers_and_their_link_rule(self):
        preinit = self.source('kernel/preinit/preinit.c', PREINIT)
        self.source('kernel/Kbuild', 'kernelsu-objs += preinit/preinit.o\n')
        self.assertTrue(compat.native_preinit(self.root, 'SukiSU'))
        config = 'config KSU_SUSFS\n\tbool\n'
        result = compat.configure(config, modern=True)
        self.assertIn('config KSU_SUSFS_NATIVE_PREINIT\n\tbool\n\tdepends on KSU_SUSFS\n\tdefault y', result)
        self.assertEqual(result, compat.configure(result, modern=True))
        self.assertNotIn('config KSU_SUSFS_NATIVE_PREINIT', compat.configure(config, modern=False))
        preinit.write_text(PREINIT.replace('int *preinit_fd)', 'unsigned int *preinit_fd)'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'incomplete native preinit providers'):
            compat.native_preinit(self.root, 'SukiSU')
        preinit.write_text(PREINIT, encoding='utf-8')
        self.source('kernel/Kbuild', '# kernelsu-objs += preinit/preinit.o\n')
        with self.assertRaisesRegex(ValueError, 'not linked'):
            compat.native_preinit(self.root, 'SukiSU')

    def test_legacy_bakasu_retains_native_exec_and_input_hooks(self):
        self.source('kernel/feature/sucompat.c', 'int ksu_handle_execveat(void) { return native_exec(); }\n')
        path = self.source('kernel/runtime/ksud_integration.c',
                           'int ksu_handle_input_handle_event(void) { return native_input(); }\n')
        self.assertFalse(compat.native_preinit(self.root, 'BakaSU'))
        path.write_text('/* int ksu_handle_input_handle_event(void) { return native_input(); } */\n')
        with self.assertRaisesRegex(ValueError, 'legacy SUSFS hook provider'):
            compat.native_preinit(self.root, 'BakaSU')

    def test_suki_direct_hooks_keep_avc_lifecycle_without_unlinked_lsm_init(self):
        original = '''int __init kernelsu_init(void)
{
    ksu_feature_init();

    ksu_lsm_hook_init();
    ksu_avc_spoof_init();
    return 0;
}
'''
        expected = original.replace('    ksu_lsm_hook_init();\n', '')
        self.assertEqual(expected, compat.remove_unlinked_lsm_init(original))
        self.assertEqual(expected, compat.remove_unlinked_lsm_init(expected))
        with self.assertRaisesRegex(ValueError, 'direct-hook lifecycle'):
            compat.remove_unlinked_lsm_init(original.replace('ksu_avc_spoof_init();', 'ksu_lsm_hook_exit();'))


if __name__ == '__main__':
    unittest.main()
