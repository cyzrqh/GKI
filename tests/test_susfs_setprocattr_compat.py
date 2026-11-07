import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from susfs_setprocattr_compat import resolved_text


# Reduced setprocattr bodies from the selected Next dev 0c9c4438a6 and the
# reviewed next-susfs23 port. Git generates the conflict markers in the tests.
PREFIX = '''int my_setprocattr(const char *name, void *value, size_t size)
{
    char *str = value;
    error = ksu_avc_has_perm_compat(mysid, mysid, SECCLASS_PROCESS, PROCESS__SETCURRENT, NULL);
    if (error)
        return error;

'''
SUFFIX = '''
    new = prepare_creds();
    return 0;
}
'''
OFFICIAL = PREFIX + r'''    /* Keep every non-empty value on the backup validation path */
    if (size && str[0]) {
        if (str[size - 1] == '\n') {
            str[size - 1] = 0;
            size--;
        }
        error2 = security_context_to_sid_with_policy(backup_sepolicy, value, size, &tmp_sid, SECSID_NULL, GFP_KERNEL,
                                                     &sid, &error);
        if (error2 || error)
            return error2 ?: error;
    }
''' + SUFFIX
BASE = OFFICIAL.replace('Keep every non-empty value on the backup validation path',
                        'Obtain a SID for the context, if one was specified.').replace(
                            'if (size && str[0])', r"if (size && str[0] && str[0] != '\n')")
OLD_PORT = PREFIX + r'''    /* Obtain a SID for the context, if one was specified. */
    if (size && str[0]) {
        bool newline_prefix = (str[0] == '\n');

        if (newline_prefix && size > 1) {
            str[0] = 'u';
        }
        if (str[0] != '\n') {
            if (str[size - 1] == '\n') {
                str[size - 1] = 0;
                size--;
            }
            error2 = __security_context_to_sid_with_policy(backup_sepolicy, value, size, &tmp_sid, SECSID_NULL,
                                                           GFP_KERNEL, &sid, &error);
            if (newline_prefix) {
                str[0] = '\n';
                return -EINVAL;
            }
            if (error2 || error)
                return error2 ?: error;
        }
    }
''' + SUFFIX


@unittest.skipUnless(shutil.which('git'), 'Git is required to generate a real diff3 conflict')
class SetprocattrPortTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.setprocattr-compat-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def conflict(self, ours, base, theirs):
        for filename, source in zip(('ours', 'base', 'theirs'), (ours, base, theirs)):
            (self.root / filename).write_text(source, encoding='utf-8', newline='\n')
        merge = subprocess.run(['git', 'merge-file', '-p', '--diff3', 'ours', 'base', 'theirs'],
                               cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertIn(merge.returncode, (1, 2), merge.stderr)
        return merge.stdout

    def test_forward_keeps_original_input_and_errors_and_reverse_reconstructs_source(self):
        merge = self.conflict(OFFICIAL, BASE, OLD_PORT)
        ported = resolved_text(merge, OFFICIAL, reverse=False)
        self.assertIsNotNone(ported)
        self.assertEqual(OFFICIAL, ported.replace('__security_context_to_sid_with_policy(',
                                                  'security_context_to_sid_with_policy(', 1))
        self.assertNotIn('newline_prefix', ported)
        reverse = self.conflict(ported, OLD_PORT, BASE)
        self.assertEqual(OFFICIAL, resolved_text(reverse, OFFICIAL, reverse=True))

    def test_new_input_validation_and_other_conflict_markers_require_review(self):
        unknown = OFFICIAL.replace('if (size && str[0])', 'if (size > 1 && str[0])')
        merge = self.conflict(unknown, BASE, OLD_PORT)
        self.assertIsNone(resolved_text(merge, unknown, reverse=False))
        merge = self.conflict(OFFICIAL, BASE, OLD_PORT)
        self.assertIsNone(resolved_text(merge + '<<<<<<< another\n', OFFICIAL, reverse=False))

    def test_reverse_rejects_changes_outside_the_reviewed_block(self):
        merged = self.conflict(OFFICIAL, BASE, OLD_PORT)
        ported = resolved_text(merged, OFFICIAL, reverse=False)
        reverse = self.conflict(ported, OLD_PORT, BASE).replace('return 0;', 'return -1;')
        self.assertIsNone(resolved_text(reverse, OFFICIAL, reverse=True))


if __name__ == '__main__':
    unittest.main()
