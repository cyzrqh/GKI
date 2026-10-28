"""Exact official 6.1 CVE functions and real-C locking/lifetime regressions."""
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
import oplus_rtmutex_compat as compat

HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#define __sched
#define unlikely(x) (x)
#define RT_MUTEX_MIN_CHAINWALK 0
struct raw_lock { bool held; };
struct rt_mutex_base;
struct task_struct { struct raw_lock pi_lock; struct rt_mutex_base *pi_blocked_on; int refs; };
struct rt_mutex_waiter { struct task_struct *task; };
struct rt_mutex_base {
    struct raw_lock wait_lock;
    struct task_struct *owner;
    struct rt_mutex_waiter *top, *next;
};
static bool irqs_disabled;
static int dequeues, owner_updates, owner_enqueues, chains, proxy_result;
static struct task_struct caller, proxy, owner;
static struct task_struct *current = &caller;
static struct rt_mutex_base lock, blocked;
static struct rt_mutex_waiter waiter, second;
static void raw_spin_lock(struct raw_lock *p) { assert(irqs_disabled && !p->held); p->held = true; }
static void raw_spin_unlock(struct raw_lock *p) { assert(irqs_disabled && p->held); p->held = false; }
static void raw_spin_lock_irq(struct raw_lock *p) { assert(!irqs_disabled); irqs_disabled = true; raw_spin_lock(p); }
static void raw_spin_unlock_irq(struct raw_lock *p) { raw_spin_unlock(p); irqs_disabled = false; }
static void lockdep_assert_held(struct raw_lock *p) { assert(p->held && irqs_disabled); }
static struct rt_mutex_waiter *rt_mutex_top_waiter(struct rt_mutex_base *p) { return p->top; }
static struct task_struct *rt_mutex_owner(struct rt_mutex_base *p) { return p->owner; }
static void rt_mutex_dequeue(struct rt_mutex_base *p, struct rt_mutex_waiter *w)
{
    assert(p->wait_lock.held && irqs_disabled && w->task == &proxy && proxy.pi_lock.held);
    assert(!current->pi_lock.held);
    w->task = NULL; /* Model dequeue invalidating waiter::task. */
    if (p->top == w) p->top = p->next;
    dequeues++;
}
static void rt_mutex_dequeue_pi(struct task_struct *p, struct rt_mutex_waiter *w)
{
    assert(p == &owner && p->pi_lock.held && w == &waiter); owner_updates++;
}
static bool rt_mutex_has_waiters(struct rt_mutex_base *p) { return p->top != NULL; }
static void rt_mutex_enqueue_pi(struct task_struct *p, struct rt_mutex_waiter *w)
{
    assert(p == &owner && p->pi_lock.held && w == &second); owner_enqueues++;
}
static void rt_mutex_adjust_prio(struct task_struct *p) { assert(p == &owner && p->pi_lock.held); }
static struct rt_mutex_base *task_blocked_on_lock(struct task_struct *p) { return p->pi_blocked_on; }
static void get_task_struct(struct task_struct *p) { p->refs++; }
static void rt_mutex_adjust_prio_chain(struct task_struct *p, int mode, struct rt_mutex_base *origin,
                                      struct rt_mutex_base *next, void *unused, struct task_struct *top)
{
    assert(p == &owner && mode == RT_MUTEX_MIN_CHAINWALK && origin == &lock && next == &blocked);
    assert(!unused && top == &proxy); /* Must use saved waiter_task, never current or cleared waiter::task. */
    assert(!irqs_disabled && !lock.wait_lock.held && !owner.pi_lock.held && p->refs == 1);
    p->refs--; chains++;
}
static int __rt_mutex_start_proxy_lock(struct rt_mutex_base *p, struct rt_mutex_waiter *w,
                                      struct task_struct *task)
{
    assert(p == &lock && p->wait_lock.held && w == &waiter && task == &proxy);
    return proxy_result;
}
@REMOVE_WAITER@
@PROXY_LOCK@
static void reset(void)
{
    memset(&caller, 0, sizeof(caller)); memset(&proxy, 0, sizeof(proxy)); memset(&owner, 0, sizeof(owner));
    memset(&lock, 0, sizeof(lock)); memset(&blocked, 0, sizeof(blocked));
    waiter.task = &proxy; second.task = &caller; lock.top = &waiter;
    caller.pi_blocked_on = &blocked; proxy.pi_blocked_on = &lock;
    dequeues = owner_updates = owner_enqueues = chains = proxy_result = 0;
    irqs_disabled = true; lock.wait_lock.held = true;
}
static void direct_check(void)
{
    assert(irqs_disabled && lock.wait_lock.held && !proxy.pi_lock.held && !owner.pi_lock.held);
    assert(!caller.pi_lock.held && caller.pi_blocked_on == &blocked);
}
int main(void)
{
    reset(); waiter.task = NULL; remove_waiter(&lock, &waiter);
    direct_check(); assert(!dequeues && !owner_updates && !chains);
    reset(); remove_waiter(&lock, &waiter); direct_check();
    assert(dequeues == 1 && !proxy.pi_blocked_on && !owner_updates && !chains);
    reset(); lock.owner = &owner; lock.top = &second; remove_waiter(&lock, &waiter);
    direct_check(); assert(dequeues == 1 && !owner_updates && !chains);
    reset(); lock.owner = &owner; remove_waiter(&lock, &waiter);
    direct_check(); assert(owner_updates == 1 && !owner_enqueues && !chains && !owner.refs);
    reset(); lock.owner = &owner; lock.next = &second; owner.pi_blocked_on = &blocked;
    remove_waiter(&lock, &waiter); direct_check();
    assert(dequeues == 1 && owner_updates == 1 && owner_enqueues == 1 && chains == 1 && !owner.refs);
    /* The unchanged CVE proxy guard removes only on a negative result. */
    for (int result = -1; result <= 1; result++) {
        reset(); irqs_disabled = false; lock.wait_lock.held = false; proxy_result = result;
        assert(rt_mutex_start_proxy_lock(&lock, &waiter, &proxy) == result);
        assert(!irqs_disabled && !lock.wait_lock.held);
        assert(dequeues == (result < 0) && !caller.pi_lock.held && caller.pi_blocked_on == &blocked);
    }
    reset(); irqs_disabled = false; lock.wait_lock.held = false; waiter.task = NULL; proxy_result = -1;
    assert(rt_mutex_start_proxy_lock(&lock, &waiter, &proxy) == -1 && !dequeues);
    puts("real rtmutex C: null guard, proxy-task lifetime, lock/IRQ balance, priority chain and negative-result guard passed");
    return 0;
}
'''


class RtmutexCompatTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.rtmutex-compat-' + uuid.uuid4().hex)
        (self.root / 'kernel/locking').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.makefile = self.root / 'Makefile'
        self.makefile.write_text('VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 57\n', encoding='utf-8', newline='\n')
        self.source = self.root / 'kernel/locking/rtmutex.c'
        self.api = self.root / 'kernel/locking/rtmutex_api.c'
        self.source.write_text('/* unrelated source retained */\n' + SCOPED_FUNCTION + '\n', encoding='utf-8', newline='\n')
        self.api.write_text(PROXY_FUNCTION + '\n', encoding='utf-8', newline='\n')

    def test_preserves_the_whole_cve_fix_and_only_replaces_its_lock_scope(self):
        original, api = self.source.read_text(), self.api.read_bytes()
        self.assertTrue(compat.prepare_rtmutex_compat(self.root))
        changed = self.source.read_text()
        self.assertEqual(original.replace(compat.SCOPED_LOCK, compat.EXPLICIT_LOCK, 1), changed)
        self.assertEqual(api, self.api.read_bytes())
        self.assertNotIn('scoped_guard', changed)
        self.assertIn('if (!waiter_task) /* never enqueued */', changed)
        self.assertIn('next_lock, NULL, waiter_task)', changed)
        self.assertFalse(compat.prepare_rtmutex_compat(self.root))
        self.assertEqual(changed, self.source.read_text())

    def test_other_kernel_series_are_untouched(self):
        before = self.source.read_bytes()
        for minor in (6, 12):
            self.makefile.write_text(f'VERSION = 6\nPATCHLEVEL = {minor}\n', newline='\n')
            self.assertFalse(compat.prepare_rtmutex_compat(self.root))
            self.assertEqual(before, self.source.read_bytes())

    def test_unknown_or_incomplete_security_fixes_are_refused_without_writing(self):
        changes = (
            SCOPED_FUNCTION.replace('if (!waiter_task)', 'if (false)'),
            SCOPED_FUNCTION.replace('waiter_task->pi_blocked_on = NULL', 'current->pi_blocked_on = NULL'),
            SCOPED_FUNCTION.replace('next_lock, NULL, waiter_task)', 'next_lock, NULL, current)'),
            SCOPED_FUNCTION.replace('struct task_struct *waiter_task = waiter->task;', ''),
            SCOPED_FUNCTION.replace('scoped_guard(raw_spinlock', 'scoped_guard(spinlock'),
            SCOPED_FUNCTION + '\n' + SCOPED_FUNCTION,
        )
        for source in changes:
            self.source.write_text(source, encoding='utf-8', newline='\n')
            before = self.source.read_bytes()
            with self.subTest(source=source), self.assertRaises(ValueError):
                compat.prepare_rtmutex_compat(self.root)
            self.assertEqual(before, self.source.read_bytes())
            self.assertEqual([], list(self.source.parent.glob('.oplus-rtmutex-*')))

    def test_missing_negative_result_guard_or_version_stops_before_source_changes(self):
        before = self.source.read_bytes()
        self.api.write_text(PROXY_FUNCTION.replace('unlikely(ret < 0)', 'unlikely(ret)'), encoding='utf-8', newline='\n')
        with self.assertRaisesRegex(ValueError, 'negative-result'):
            compat.prepare_rtmutex_compat(self.root)
        self.assertEqual(before, self.source.read_bytes())
        for makefile in ('VERSION = 6\n', 'VERSION = 6\nVERSION = 5\nPATCHLEVEL = 1\n'):
            self.makefile.write_text(makefile, encoding='utf-8', newline='\n')
            with self.assertRaisesRegex(ValueError, 'series'):
                compat.prepare_rtmutex_compat(self.root)
            self.assertEqual(before, self.source.read_bytes())

    def test_atomic_replace_failure_preserves_original_and_removes_staging(self):
        before = self.source.read_bytes()
        with mock.patch.object(compat.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                compat.prepare_rtmutex_compat(self.root)
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual([], list(self.source.parent.glob('.oplus-rtmutex-*')))

    def test_changed_api_before_commit_preserves_source_and_the_independent_change(self):
        before = self.source.read_bytes()
        real_read, api_reads = compat.read_source, []
        changed_api = b'independent concurrent change\n'
        def read(common, relative):
            if relative.endswith('rtmutex_api.c'):
                api_reads.append(relative)
                if len(api_reads) == 2:
                    self.api.write_bytes(changed_api)
            return real_read(common, relative)
        with mock.patch.object(compat, 'read_source', side_effect=read):
            with self.assertRaisesRegex(ValueError, 'changed during'):
                compat.prepare_rtmutex_compat(self.root)
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(changed_api, self.api.read_bytes())
        self.assertEqual([], list(self.source.parent.glob('.oplus-rtmutex-*')))

    @unittest.skipUnless(shutil.which('cc') or shutil.which('gcc') or shutil.which('clang'),
                         'Real C execution needs GCC or Clang; standalone portable builds are also checked separately')
    def test_real_c_preserves_proxy_task_lifetime_locking_irqs_and_priority_chain(self):
        compat.prepare_rtmutex_compat(self.root)
        function = compat.function_text(self.source.read_text(), 'remove_waiter')
        translation_unit = HARNESS.replace('@REMOVE_WAITER@', function).replace('@PROXY_LOCK@', PROXY_FUNCTION)
        source = self.root / 'behavior.c'
        source.write_text(translation_unit, encoding='utf-8', newline='\n')
        compiler = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
        output = self.root / ('behavior.exe' if os.name == 'nt' else 'behavior')
        built = subprocess.run([compiler, '-std=gnu11', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(output)],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        ran = subprocess.run([str(output)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertIn('real rtmutex C', ran.stdout)

# Real native functions from official OnePlusOSS common e02429a7, with the
# fixed CCTV f4e3a808 CVE patch. Same byte hashes audited on all eight fixed
# official OPlus 6.1 profiles. C execution uses the production adapter's output
# from this native input, while the surrounding kernel interfaces are stubbed.
SCOPED_FUNCTION = "static void __sched remove_waiter(struct rt_mutex_base *lock,\n\t\t\t\t  struct rt_mutex_waiter *waiter)\n{\n\tbool is_top_waiter = (waiter == rt_mutex_top_waiter(lock));\n\tstruct task_struct *owner = rt_mutex_owner(lock);\n\tstruct task_struct *waiter_task = waiter->task;\n\tstruct rt_mutex_base *next_lock;\n\n\tlockdep_assert_held(&lock->wait_lock);\n\n\tif (!waiter_task) /* never enqueued */\n\t\treturn;\n\n\tscoped_guard(raw_spinlock, &waiter_task->pi_lock) {\n\t\trt_mutex_dequeue(lock, waiter);\n\t\twaiter_task->pi_blocked_on = NULL;\n\t}\n\n\t/*\n\t * Only update priority if the waiter was the highest priority\n\t * waiter of the lock and there is an owner to update.\n\t */\n\tif (!owner || !is_top_waiter)\n\t\treturn;\n\n\traw_spin_lock(&owner->pi_lock);\n\n\trt_mutex_dequeue_pi(owner, waiter);\n\n\tif (rt_mutex_has_waiters(lock))\n\t\trt_mutex_enqueue_pi(owner, rt_mutex_top_waiter(lock));\n\n\trt_mutex_adjust_prio(owner);\n\n\t/* Store the lock on which owner is blocked or NULL */\n\tnext_lock = task_blocked_on_lock(owner);\n\n\traw_spin_unlock(&owner->pi_lock);\n\n\t/*\n\t * Don't walk the chain, if the owner task is not blocked\n\t * itself.\n\t */\n\tif (!next_lock)\n\t\treturn;\n\n\t/* gets dropped in rt_mutex_adjust_prio_chain()! */\n\tget_task_struct(owner);\n\n\traw_spin_unlock_irq(&lock->wait_lock);\n\n\trt_mutex_adjust_prio_chain(owner, RT_MUTEX_MIN_CHAINWALK, lock,\n\t\t\t\t   next_lock, NULL, waiter_task);\n\n\traw_spin_lock_irq(&lock->wait_lock);\n}"

PROXY_FUNCTION = 'int __sched rt_mutex_start_proxy_lock(struct rt_mutex_base *lock,\n\t\t\t\t      struct rt_mutex_waiter *waiter,\n\t\t\t\t      struct task_struct *task)\n{\n\tint ret;\n\n\traw_spin_lock_irq(&lock->wait_lock);\n\tret = __rt_mutex_start_proxy_lock(lock, waiter, task);\n\tif (unlikely(ret < 0))\n\t\tremove_waiter(lock, waiter);\n\traw_spin_unlock_irq(&lock->wait_lock);\n\n\treturn ret;\n}'
