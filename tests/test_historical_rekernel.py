import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import historical_rekernel as compatibility

FIXTURES = ROOT / 'tests/fixtures/historical-rekernel'
OLD = 'android13-5.15-2022-06'
MODERN = 'android13-5.15-2025-12'
OLD510 = 'android12-5.10-2022-06'


class HistoricalReKernelTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.historical-rekernel-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.common = self.root / 'common'

    def cleanup(self):
        target = self.root.resolve()
        if target.parent != (ROOT / 'tests').resolve() or not target.name.startswith('.historical-rekernel-'):
            raise ValueError('Unexpected test cleanup target')
        shutil.rmtree(target)

    def fixture(self, label=OLD):
        shutil.copytree(FIXTURES / label, self.common)
        return self.state()

    def state(self):
        return {item: (self.common / item).read_bytes() for item in compatibility.PATHS}

    def test_real_old_515_backport_only_adds_native_hooks_and_is_idempotent(self):
        before = self.fixture()
        family, changed = compatibility.apply_compat(self.root)
        self.assertEqual(family, '5.15')
        self.assertEqual(set(changed), set(compatibility.PATHS))
        after = self.state()
        header = after[compatibility.PATHS[0]].decode()
        binder = after[compatibility.PATHS[1]].decode()
        allocator = after[compatibility.PATHS[2]].decode()
        exports = after[compatibility.PATHS[3]].decode()
        for hook in compatibility.HOOKS:
            header = header.replace(compatibility.declaration(hook, '5.15'), '', 1)
            line = compatibility.call(hook, '5.15')
            if hook == 'alloc_new_buf_locked':
                allocator = allocator.replace('\t' + line + '\n', '', 1)
            else:
                indent = '\t' if hook == 'preset' else '\t\t'
                binder = binder.replace(indent + line + '\n', '', 1)
            exports = exports.replace(f'EXPORT_TRACEPOINT_SYMBOL_GPL({compatibility.PREFIX}{hook});\n', '', 1)
        allocator = allocator.replace('#include <trace/hooks/binder.h>\n', '', 1)
        self.assertEqual([header.encode(), binder.encode(), allocator.encode(), exports.encode()],
                         [before[item] for item in compatibility.PATHS])
        with patch.object(compatibility, 'atomic_write', side_effect=AssertionError('unnecessary rewrite')):
            self.assertEqual(compatibility.apply_compat(self.root), ('5.15', ()))
        self.assertEqual(after, self.state())

    def test_real_events_use_correct_objects_and_preserve_allocator_check_order(self):
        self.fixture()
        compatibility.apply_compat(self.root)
        allocator = (self.common / compatibility.PATHS[2]).read_text()
        trace = compatibility.call('alloc_new_buf_locked', '5.15')
        self.assertIn('size, &alloc->free_async_space, is_async', trace)
        self.assertLess(allocator.index(trace), allocator.index('if (is_async &&'))
        self.assertLess(allocator.index('if (is_async &&'), allocator.index('size = max(size, sizeof(void *));'))
        binder = (self.common / compatibility.PATHS[1]).read_text()
        self.assertIn('binder_inner_proc_unlock(target_thread->proc);\n\t\t'
                      + compatibility.call('reply', '5.15'), binder)
        self.assertIn(compatibility.call('trans', '5.15')
                      + '\n\t\tif (security_binder_transaction(proc->cred,', binder)
        self.assertIn('hlist_add_head(&proc->proc_node, &binder_procs);\n'
                      '\tmutex_unlock(&binder_procs_lock);\n\t'
                      + compatibility.call('preset', '5.15'), binder)

    def test_complete_official_510_and_modern_515_are_never_rewritten(self):
        for label, family in ((OLD510, '5.10'), (MODERN, '5.15')):
            with self.subTest(label=label):
                if self.common.exists():
                    shutil.rmtree(self.common)
                before = self.fixture(label)
                with patch.object(compatibility, 'atomic_write', side_effect=AssertionError('must retain native hooks')):
                    self.assertEqual(compatibility.apply_compat(self.root), (family, ()))
                self.assertEqual(before, self.state())
        self.assertIn('struct binder_alloc *alloc', compatibility.declaration('alloc_new_buf_locked', '5.10'))
        self.assertEqual(compatibility.call('alloc_new_buf_locked', '5.10'),
                         'trace_android_vh_binder_alloc_new_buf_locked(size, alloc, is_async);')

    def test_modern_callback_with_wrong_family_abi_is_rejected_before_any_write(self):
        self.fixture(OLD510)
        header = self.common / compatibility.PATHS[0]
        header.write_bytes(header.read_bytes().replace(b'struct binder_alloc *alloc, int is_async',
                                                      b'size_t *free_async_space, int is_async'))
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'Unexpected Binder hook declaration'):
            compatibility.apply_compat(self.root)
        self.assertEqual(before, self.state())

    def test_unknown_event_context_rejects_the_entire_backport(self):
        self.fixture()
        allocator = self.common / compatibility.PATHS[2]
        allocator.write_bytes(allocator.read_bytes().replace(b'if (is_async &&', b'if (is_async == 1 &&', 1))
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'Unknown binder_alloc_new_buf_locked event site'):
            compatibility.apply_compat(self.root)
        self.assertEqual(before, self.state())

    def test_duplicate_native_hook_calls_are_rejected(self):
        self.fixture(MODERN)
        binder = self.common / compatibility.PATHS[1]
        native = ('\t\t' + compatibility.call('trans', '5.15') + '\n').encode()
        binder.write_bytes(binder.read_bytes().replace(native, native + native, 1))
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'Unexpected trace_android_vh_binder_trans callback contract'):
            compatibility.apply_compat(self.root)
        self.assertEqual(before, self.state())

    def test_export_only_repairs_keep_the_existing_runtime_hooks(self):
        self.fixture(MODERN)
        exports = self.common / compatibility.PATHS[3]
        native = b'EXPORT_TRACEPOINT_SYMBOL_GPL(android_vh_binder_reply);\n'
        exports.write_bytes(exports.read_bytes().replace(native, b'', 1))
        before = self.state()
        _, changed = compatibility.apply_compat(self.root)
        self.assertEqual(changed, (compatibility.PATHS[3],))
        for item in compatibility.PATHS[:-1]:
            self.assertEqual((self.common / item).read_bytes(), before[item])
        self.assertEqual(exports.read_bytes().count(native), 1)

    def test_missing_tracepoint_instantiation_is_an_error_not_a_stub(self):
        self.fixture()
        exports = self.common / compatibility.PATHS[3]
        exports.write_bytes(exports.read_bytes().replace(b'#define CREATE_TRACE_POINTS', b'/* no tracepoint owner */'))
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'does not instantiate the real tracepoints'):
            compatibility.apply_compat(self.root)
        self.assertEqual(before, self.state())

    def test_crlf_sources_keep_their_line_endings(self):
        self.fixture()
        for item in compatibility.PATHS:
            path = self.common / item
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        compatibility.apply_compat(self.root)
        for data in self.state().values():
            self.assertNotIn(b'\n', data.replace(b'\r\n', b''))

    def test_other_families_do_not_receive_historical_prototypes(self):
        self.fixture()
        (self.common / 'Makefile').write_text('VERSION = 6\nPATCHLEVEL = 6\n')
        before = self.state()
        with patch.object(compatibility, 'atomic_write', side_effect=AssertionError('wrong kernel family')):
            self.assertEqual(compatibility.apply_compat(self.root), ('6.6', ()))
        self.assertEqual(before, self.state())

    def test_fixtures_are_complete_unmodified_official_files(self):
        expected = json.loads((FIXTURES / 'sha256.json').read_text())
        for label, files in expected.items():
            for item, digest in files.items():
                with self.subTest(label=label, file=item):
                    data = (FIXTURES / label / item).read_bytes()
                    self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                    self.assertIn(b'SPDX-License-Identifier: GPL-2.0', data[:100])


if __name__ == '__main__':
    unittest.main()
