"""Pinned native scheduler sources, all-input validation and C control-flow replay."""
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_scheduler_compat as compat
from oplus_f2fs_compat import PROFILE, REPOSITORY, COMMIT

FIXTURE = ROOT / 'tests/fixtures/oplus-scheduler-141'
ORIGINAL = {n: gzip.decompress((FIXTURE / (n.replace('/', '_') + '.gz')).read_bytes()) for n in compat.FILES}
PROFILE = {'id': PROFILE, 'platform': 'sm8650', 'kernel': '6.1.141',
           'kernel_repo': REPOSITORY, 'kernel_ref': COMMIT}


def scheduling_regions(core, fair):
    """Replay exact scheduling boundaries, not a synthetic whole kernel."""
    start = core.index('\tschedule_debug(prev, !!sched_mode);')
    end = core.index('\n\tif (sched_feat(HRTICK)', start)
    result = [core[start:end]]
    start = fair.index('\tif (delta_exec > ideal_runtime) {')
    tail = '\t\tresched_curr(rq_of(cfs_rq));'
    end = fair.index(tail, start) + len(tail)
    result.append(fair[start:end] + '\n}\n')
    start = fair.index('\tif (queued) {', fair.index('entity_tick('))
    end = fair.index(tail, start) + len(tail)
    result.append(fair[start:end] + '\n}\n')
    start = fair.index('\npreempt:\n', fair.index('check_preempt_wakeup(')) + len('\npreempt:\n')
    tail = '\tresched_curr(rq);'
    end = fair.index(tail, start) + len(tail)
    result.append(fair[start:end])
    return result


HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
static bool request_skip;
static int calls, work, debug_calls, queue;
static void schedule_debug(void *prev, bool mode) { assert(!prev && !mode); debug_calls++; }
static void trace_android_vh_lock_delay_schedule(void *prev, int mode, bool *skip) {
    assert(!prev && !mode); calls++; *skip = request_skip;
}
static void trace_android_vh_resched_curr_lazy(void *rq, bool *skip) {
    assert(rq == &queue); calls++; *skip = request_skip;
}
static void *rq_of(void *cfs) { assert(cfs == &queue); return &queue; }
static void resched_curr(void *rq) { assert(rq == &queue); work++; }
static void core_boundary(void) {
    void *prev = 0; int sched_mode = 0; bool skip_schedule = false;
    @CORE@
    work++;
}
static void tick_boundary(void) {
    int delta_exec = 2, ideal_runtime = 1; bool skip_preempt = false; void *cfs_rq = &queue;
    @TICK@
}
static void entity_boundary(void) {
    int queued = 1; bool skip_preempt = false; void *cfs_rq = &queue;
    @ENTITY@
}
static void wake_boundary(void) {
    bool ignore = false; void *rq = &queue; void *cfs_rq = &queue;
    @WAKE@
}
int main(void) {
    void (*paths[])(void) = {core_boundary, tick_boundary, entity_boundary, wake_boundary};
    for (int flag = 0; flag < 2; flag++) for (int path = 0; path < 4; path++) {
        request_skip = flag; calls = work = debug_calls = 0; paths[path]();
        assert(work == (EXPECT_LAZY && flag ? 0 : 1));
        assert(calls == EXPECT_LAZY);
        assert(debug_calls == (path == 0 ? 1 : 0));
    }
    puts("four native scheduling boundaries, with and without vendor skip requests: passed");
    return 0;
}
'''


class OplusSchedulerCompatTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT.parent / '.gki-upstream-cache' / ('scheduler-compat-test-' + uuid.uuid4().hex)
        for n, raw in ORIGINAL.items():
            p = self.root / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        self.addCleanup(shutil.rmtree, self.root)

    def test_full_native_sources_become_exact_reference_and_keep_vendor_abi_files(self):
        preserved = self.root / 'include/trace/hooks/dtask.h'
        preserved.parent.mkdir(parents=True)
        preserved.write_bytes(b'preserve every vendor hook declaration and exported ABI\n')
        report = compat.prepare_scheduler_compat(self.root, PROFILE)
        self.assertEqual(report, compat.prepare_scheduler_compat(self.root, PROFILE))
        self.assertEqual(report, compat.verify_scheduler_compat(self.root, PROFILE))
        for n, (before, after) in compat.FILES.items():
            self.assertEqual(before, compat.digest(ORIGINAL[n]))
            self.assertEqual(after, compat.digest((self.root / n).read_bytes()))
        self.assertEqual(b'preserve every vendor hook declaration and exported ABI\n', preserved.read_bytes())

    def test_unknown_second_source_prevents_changing_first_source(self):
        name = 'kernel/sched/fair.c'
        (self.root / name).write_bytes(ORIGINAL[name] + b'\n')
        with self.assertRaisesRegex(ValueError, 'changed official'):
            compat.prepare_scheduler_compat(self.root, PROFILE)
        self.assertEqual(ORIGINAL['kernel/sched/core.c'], (self.root / 'kernel/sched/core.c').read_bytes())
        self.assertEqual([], list(self.root.rglob('.gki-scheduler-*')))

    def test_already_repaired_first_file_can_resume(self):
        name = 'kernel/sched/core.c'
        (self.root / name).write_bytes(compat.repair_source(name, ORIGINAL[name]))
        self.assertEqual(compat.provenance(PROFILE), compat.prepare_scheduler_compat(self.root, PROFILE))

    def test_changed_ref_or_repository_is_not_patched(self):
        for change in ({'kernel_ref': 'a' * 40}, {'kernel_repo': compat.REFERENCE_REPOSITORY}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                compat.prepare_scheduler_compat(self.root, {**PROFILE, **change})
        for n in ORIGINAL:
            self.assertEqual(ORIGINAL[n], (self.root / n).read_bytes())

    def test_other_platforms_and_versions_are_untouched(self):
        for identity in ('sm8650-6.1.118', 'mt6989-6.1.134', 'sm8750-6.6.89'):
            p = {**PROFILE, 'id': identity}
            self.assertEqual({}, compat.prepare_scheduler_compat(self.root, p))
            self.assertEqual({}, compat.verify_scheduler_compat(self.root, p))

    def test_packaging_cannot_claim_original_or_tampered_scheduler_sources(self):
        with self.assertRaisesRegex(ValueError, 'without its reviewed scheduler'):
            compat.verify_scheduler_compat(self.root, PROFILE)
        report = compat.prepare_scheduler_compat(self.root, PROFILE)
        self.assertEqual(report, compat.validate_scheduler_provenance(PROFILE, copy.deepcopy(report)))
        for k, v in [('schema_version', True), ('common_commit', 'a'*40), ('files', {})]:
            with self.subTest(k=k), self.assertRaises(ValueError):
                compat.validate_scheduler_provenance(PROFILE, {**report, k: v})

    def test_complete_fixture_sources_retain_original_license_and_pinned_identity(self):
        d = json.loads((FIXTURE / 'provenance.json').read_text())
        self.assertEqual(COMMIT, d['official_commit'])
        for n, raw in ORIGINAL.items():
            self.assertIn(b'SPDX-License-Identifier: GPL-2.0', raw[:400])
            self.assertEqual(compat.FILES[n][0], d['files'][n]['official_sha256'])
            self.assertEqual(compat.FILES[n][1], d['files'][n]['reference_sha256'])

    def test_actual_native_c_boundaries_schedule_after_vendor_skip_callback_removal(self):
        compiler = os.environ.get('OPLUS_SCHEDULER_CC') or os.environ.get('OPLUS_F2FS_CC') or shutil.which('cc')
        if not compiler:
            self.skipTest('Native C compiler required for scheduling boundary replay')
        for label, sources, lazy in [('official', ORIGINAL, 1),
                                    ('fixed', {n: compat.repair_source(n, b) for n, b in ORIGINAL.items()}, 0)]:
            snippets = scheduling_regions(sources['kernel/sched/core.c'].decode(), sources['kernel/sched/fair.c'].decode())
            text = HARNESS
            for key, snippet in zip(('CORE', 'TICK', 'ENTITY', 'WAKE'), snippets):
                text = text.replace('@'+key+'@', snippet)
            path = self.root / (label+'.c')
            path.write_text(text, encoding='utf-8', newline='\n')
            exe = self.root / (label + ('.exe' if os.name == 'nt' else ''))
            command = [compiler] + (['cc'] if Path(compiler).stem == 'zig' else [])
            result = subprocess.run(command+['-std=c11','-O2','-DEXPECT_LAZY='+str(lazy),str(path),'-o',str(exe)],
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(0, result.returncode, result.stderr)
            result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stdout+result.stderr)
            self.assertIn('passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
