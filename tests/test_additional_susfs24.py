"""Exercise the native authorization and refresh boundaries of the 2.4 adapter."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from additional_susfs24 import PRIORITY_CASE, PRIORITY_REFRESH, priority_dispatch, priority_observer
from verify_kow_susfs import function_source


DISPATCH = '''int ksu_handle_sys_reboot(int magic1, int magic2, unsigned int cmd, void **arg)
{
    if (magic1 != KSU_INSTALL_MAGIC1)
        return -EINVAL;
    if (magic2 == SUSFS_MAGIC && current_uid().val == 0) {
        switch (cmd) {
        case CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING:
            susfs_set_avc_log_spoofing(arg);
            return 0;
        default:
            return -EINVAL;
        }
    }
    return ksu_handle_toolkit_reboot(magic2, cmd, *arg);
}
'''


class AdditionalSusfs24Tests(unittest.TestCase):
    def test_priority_command_remains_root_only_and_native_toolkit_is_unchanged(self):
        updated = priority_dispatch(DISPATCH)
        self.assertIn('if (magic2 == SUSFS_MAGIC && current_uid().val == 0)', updated)
        self.assertIn('return ksu_handle_toolkit_reboot(magic2, cmd, *arg);', updated)
        self.assertIn('#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER\n        case ' + PRIORITY_CASE, updated)
        self.assertEqual(priority_dispatch(updated), updated)
        unauthorized = DISPATCH.replace(' && current_uid().val == 0', '')
        with self.assertRaisesRegex(ValueError, 'native root-only dispatch'):
            priority_dispatch(unauthorized)

    def test_priority_command_cannot_move_to_a_different_switch(self):
        split = DISPATCH.replace('        switch (cmd) {\n',
                                 '        switch (cmd) { default: return -EINVAL; }\n    }\n    {\n        switch (cmd) {\n', 1)
        with self.assertRaisesRegex(ValueError, 'native root-only dispatch'):
            priority_dispatch(split)

    def test_kow_refresh_runs_after_the_deferred_safe_scan(self):
        before = '''#include <linux/version.h>

static void ksu_track_throne_tw_func(struct callback_head *cb)
{
    kfree(cb);
    if (current->flags & PF_EXITING)
        return;
    track_throne(false);
}

static void ksu_defer_track_throne(void)
{
    struct callback_head *cb;

    // Renew the getpriority uid bitmap filter
    susfs_parse_priority_app_strings();

    queue_existing_task_work(cb);
}
'''
        updated = priority_observer(before)
        self.assertEqual(updated.count(PRIORITY_REFRESH), 1)
        callback = function_source(updated, 'ksu_track_throne_tw_func')
        self.assertLess(callback.index('track_throne(false);'), callback.index(PRIORITY_REFRESH))
        self.assertIn('queue_existing_task_work(cb);', function_source(updated, 'ksu_defer_track_throne'))
        self.assertEqual(priority_observer(updated), updated)


if __name__ == '__main__':
    unittest.main()
