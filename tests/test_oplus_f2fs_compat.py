"""Pinned real F2FS source repair, packaging gate and native checkpoint behavior."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_f2fs_compat as compat
from oplus_rtmutex_compat import function_text

FIXTURE = ROOT / 'tests/fixtures/oplus-f2fs-141'
ORIGINAL = (FIXTURE / 'checkpoint-official.c').read_bytes()
PROFILE = {'id': compat.PROFILE, 'platform': 'sm8650', 'kernel': '6.1.141',
           'kernel_repo': compat.REPOSITORY, 'kernel_ref': compat.COMMIT}

HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#define MERGE_CHECKPOINT 1
#define CP_SYNC 2
struct task_struct { int nice; };
struct completion { int done; };
struct ckpt_req { struct completion wait; int llnode; int ret; };
struct cp_control { int reason; };
struct ckpt_req_control {
    struct task_struct *f2fs_issue_ckpt;
    int issue_list, queued_ckpt, ckpt_wait_queue;
};
struct f2fs_sb_info { struct ckpt_req_control cprc_info; int gc_lock; };
static struct task_struct caller, worker;
static struct task_struct *current = &caller;
static struct f2fs_sb_info sb;
static struct ckpt_req *active;
static bool merge_opt, ux, lose_thread;
static int reason, direct, syncs, waits, flushes, priority_calls, ux_calls, barriers, wakeups;
static int __get_cp_reason(struct f2fs_sb_info *s) { assert(s == &sb); return reason; }
static bool test_opt(struct f2fs_sb_info *s, int opt) { assert(s == &sb && opt == 1); return merge_opt; }
static void f2fs_down_write(int *lock) { assert(!*lock); *lock = 1; }
static void f2fs_up_write(int *lock) { assert(*lock); *lock = 0; }
static int f2fs_write_checkpoint(struct f2fs_sb_info *s, struct cp_control *c) {
    assert(s == &sb && s->gc_lock && c->reason == reason); direct++; return -17;
}
static int __write_checkpoint_sync(struct f2fs_sb_info *s) { assert(s == &sb); syncs++; return -23; }
static void init_ckpt_req(struct ckpt_req *r) { memset(r, 0, sizeof(*r)); active = r; }
static void llist_add(int *node, int *list) { assert(node == &active->llnode && list == &sb.cprc_info.issue_list); }
static void atomic_inc(int *p) { (*p)++; }
static void smp_mb(void) { barriers++; }
static bool waitqueue_active(int *q) { assert(q == &sb.cprc_info.ckpt_wait_queue); return *q; }
static void wake_up(int *q) {
    assert(q == &sb.cprc_info.ckpt_wait_queue); wakeups++;
    if (lose_thread) sb.cprc_info.f2fs_issue_ckpt = NULL;
}
static bool test_task_ux(struct task_struct *task) { assert(task == current); ux_calls++; return ux; }
static int task_nice(struct task_struct *task) { assert(task == &worker); return task->nice; }
static void sched_set_fifo_low(struct task_struct *task) { assert(task == &worker); priority_calls++; }
static void sched_set_normal(struct task_struct *task, int nice) { assert(task == &worker && nice == worker.nice); priority_calls++; }
static void wait_for_completion(struct completion *c) { assert(c == &active->wait); waits++; active->ret = -31; }
static void flush_remained_ckpt_reqs(struct f2fs_sb_info *s, struct ckpt_req *r) {
    assert(s == &sb && r == active); flushes++; r->ret = -47;
}
@FUNCTION@
static void reset(void) {
    memset(&sb, 0, sizeof(sb)); active = NULL; merge_opt = true; ux = false; lose_thread = false;
    reason = CP_SYNC; sb.cprc_info.f2fs_issue_ckpt = &worker; sb.cprc_info.ckpt_wait_queue = 1;
    direct = syncs = waits = flushes = priority_calls = ux_calls = barriers = wakeups = 0;
}
int main(void) {
    reset(); merge_opt = false; assert(f2fs_issue_checkpoint(&sb) == -17);
    assert(direct == 1 && !sb.gc_lock && !syncs && !waits && !priority_calls);
    reset(); reason = 7; assert(f2fs_issue_checkpoint(&sb) == -17);
    assert(direct == 1 && !sb.gc_lock && !syncs && !waits && !priority_calls);
    reset(); sb.cprc_info.f2fs_issue_ckpt = NULL; assert(f2fs_issue_checkpoint(&sb) == -23);
    assert(syncs == 1 && !waits && !flushes && !sb.cprc_info.queued_ckpt && !priority_calls);
    for (int flag = 0; flag < 2; flag++) {
        reset(); ux = flag; assert(f2fs_issue_checkpoint(&sb) == -31);
        assert(waits == 1 && !direct && !syncs && !flushes && barriers == 1 && wakeups == 1);
        assert(sb.cprc_info.queued_ckpt == 1);
        assert(priority_calls == (EXPECT_BOOST && flag ? 2 : 0));
        assert(ux_calls == (EXPECT_BOOST ? 1 : 0));
    }
    reset(); lose_thread = true; assert(f2fs_issue_checkpoint(&sb) == -47);
    assert(flushes == 1 && !waits && !priority_calls && !ux_calls && barriers == 1 && wakeups == 1);
    puts("native F2FS checkpoint: direct/sync/queued/vanished-thread paths and error propagation passed");
    return 0;
}
'''


class OplusF2fsCompatTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT.parent / '.gki-upstream-cache' / ('f2fs-compat-test-' + uuid.uuid4().hex)
        self.source = self.root / compat.RELATIVE
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(ORIGINAL)
        self.addCleanup(shutil.rmtree, self.root)

    def test_real_source_matches_reference_exactly_and_other_vendor_files_are_untouched(self):
        self.assertEqual(compat.ORIGINAL, hashlib.sha256(ORIGINAL).hexdigest())
        native = {name: b'preserve existing source bytes' for name in
                  ('fs/f2fs/file.c', 'include/linux/blk-mq.h', 'block/blk-mq.c')}
        for name, raw in native.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        report = compat.prepare_f2fs_compat(self.root, PROFILE)
        self.assertEqual(compat.FIXED, hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(report, compat.prepare_f2fs_compat(self.root, PROFILE))
        self.assertEqual(report, compat.verify_f2fs_compat(self.root, PROFILE))
        for name, raw in native.items(): self.assertEqual(raw, (self.root / name).read_bytes())
        self.assertEqual([], list(self.source.parent.glob('.gki-f2fs-*')))

    def test_affected_unpatched_kernel_cannot_be_packaged(self):
        with self.assertRaisesRegex(ValueError, 'Refusing to package'):
            compat.verify_f2fs_compat(self.root, PROFILE)
        self.assertEqual(ORIGINAL, self.source.read_bytes())

    def test_unknown_source_or_changed_official_ref_is_never_patched(self):
        for raw, profile in ((ORIGINAL + b'\n', PROFILE),
                             (ORIGINAL, {**PROFILE, 'kernel_ref': 'a' * 40}),
                             (ORIGINAL, {**PROFILE, 'kernel_repo': compat.REFERENCE_REPOSITORY})):
            self.source.write_bytes(raw)
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                compat.prepare_f2fs_compat(self.root, profile)
            self.assertEqual(raw, self.source.read_bytes())

    def test_other_kernel_versions_and_platforms_are_not_changed(self):
        for identity in ('sm8650-6.1.118', 'mt6989-6.1.134', 'sm8750-6.6.89'):
            other = {**PROFILE, 'id': identity}
            self.assertEqual({}, compat.prepare_f2fs_compat(self.root, other))
            self.assertEqual({}, compat.verify_f2fs_compat(self.root, other))
            self.assertEqual(ORIGINAL, self.source.read_bytes())

    def test_metadata_is_bound_to_the_actual_repaired_source_and_profile(self):
        report = compat.prepare_f2fs_compat(self.root, PROFILE)
        self.assertEqual(report, compat.validate_f2fs_provenance(PROFILE, copy.deepcopy(report)))
        for key, value in (('sha256', compat.ORIGINAL), ('schema_version', True), ('common_commit', 'a' * 40)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                compat.validate_f2fs_provenance(PROFILE, {**report, key: value})
        self.source.write_bytes(ORIGINAL)
        with self.assertRaises(ValueError): compat.verify_f2fs_compat(self.root, PROFILE)

    def test_atomic_write_failure_preserves_official_source(self):
        with mock.patch.object(compat.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError): compat.prepare_f2fs_compat(self.root, PROFILE)
        self.assertEqual(ORIGINAL, self.source.read_bytes())
        self.assertEqual([], list(self.source.parent.glob('.gki-f2fs-*')))

    def test_fixture_license_official_ref_and_reference_digest_are_recorded(self):
        document = json.loads((FIXTURE / 'provenance.json').read_text())
        self.assertEqual(compat.ORIGINAL, document['official']['sha256'])
        self.assertEqual(compat.FIXED, document['reference']['sha256'])
        self.assertIn(b'SPDX-License-Identifier: GPL-2.0', ORIGINAL[:1800])

    def test_real_c_runs_before_and_after_fix_and_preserves_checkpoint_results(self):
        compiler = os.environ.get('OPLUS_F2FS_CC') or os.environ.get('OPLUS_VMA_CC') or shutil.which('cc')
        if not compiler:
            self.skipTest('A native C compiler is required for checkpoint behavior replay')
        original = function_text(ORIGINAL.decode(), 'f2fs_issue_checkpoint')
        compat.prepare_f2fs_compat(self.root, PROFILE)
        fixed = function_text(self.source.read_text(), 'f2fs_issue_checkpoint')
        for label, function, boost in (('official', original, 1), ('fixed', fixed, 0)):
            source = self.root / (label + '-harness.c')
            source.write_text(HARNESS.replace('@FUNCTION@', function), encoding='utf-8', newline='\n')
            executable = self.root / (label + ('.exe' if os.name == 'nt' else ''))
            command = [compiler] + (['cc'] if Path(compiler).stem == 'zig' else [])
            command += ['-std=c11', '-O2', '-DEXPECT_BOOST=' + str(boost), str(source), '-o', str(executable)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            self.assertEqual(0, result.returncode, result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn('error propagation passed', result.stdout)
