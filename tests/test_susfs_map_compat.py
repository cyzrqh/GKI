import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import ensure_susfs_map_compat as compat

FIXTURES = ROOT / 'tests/fixtures/susfs-map-compat'
COMPILER = next((shutil.which(name) for name in ('cc', 'gcc', 'clang') if shutil.which(name)), None)


class SusfsMapCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.map-compat-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.common = self.root / 'common'

    def fixture(self, month='2024-07', patched=True):
        for relative in ('fs/proc/task_mmu.c', 'include/linux/page_size_compat.h', 'mm/page_size_compat.c'):
            target = self.common / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIXTURES / (month + '-' + relative.replace('/', '_')), target)
        if patched:
            # Isolate patch paths from the parent repository's tests/ prefix.
            subprocess.run(['git', 'init', '--quiet'], cwd=self.common, check=True, capture_output=True)
            patch = FIXTURES / 'susfs23-task_mmu.patch'
            result = subprocess.run(['git', '-c', 'core.autocrlf=false', 'apply', str(patch)],
                                    cwd=self.common, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return self.common / 'fs/proc/task_mmu.c'

    def state(self):
        return {str(path.relative_to(self.common)): path.read_bytes() for path in self.common.rglob('*') if path.is_file()}

    def test_fixtures_are_exact_pinned_official_sources(self):
        document = json.loads((FIXTURES / 'provenance.json').read_text(encoding='utf-8'))
        for record in document['files']:
            with self.subTest(file=record['fixture']):
                raw = (FIXTURES / record['fixture']).read_bytes()
                self.assertEqual(record['sha256'], hashlib.sha256(raw).hexdigest())
                self.assertRegex(record['commit'], r'^[0-9a-f]{40}$')
        patch = document['susfs_patch']
        self.assertEqual(patch['sha256'], hashlib.sha256((FIXTURES / patch['fixture']).read_bytes()).hexdigest())

    def test_real_july_undefined_call_is_repaired_without_changing_susfs_or_padding(self):
        task = self.fixture()
        before_task = task.read_bytes()
        header = self.common / 'include/linux/page_size_compat.h'
        implementation = self.common / 'mm/page_size_compat.c'
        before_header, before_implementation = header.read_text(), implementation.read_text()
        self.assertIn(compat.SYMBOL + '(', task.read_text())
        self.assertNotIn(compat.SYMBOL, before_header + before_implementation)
        changed = compat.ensure_map_compat(self.root)
        self.assertEqual((header, implementation), changed)
        self.assertEqual(before_task, task.read_bytes())
        text = task.read_text()
        for original in ('if (flags & __VM_NO_COMPAT)', 'show_map_pad_vma(vma, pad_vma, m, show_map_vma, false)',
                         'susfs_open_redirect_spoof_show_map_vma_srcu', 'SUSFS_IS_INODE_SUS_MAP'):
            self.assertIn(original, text)
        self.assertIn(compat.DECLARATION, header.read_text())
        self.assertTrue(implementation.read_text().startswith(before_implementation.rstrip()))
        self.assertTrue(implementation.read_text().endswith(compat.IMPLEMENTATION))
        official = (FIXTURES / '2024-08-mm_page_size_compat.c').read_text(encoding='utf-8')
        self.assertIn(compat.IMPLEMENTATION, official)

    def test_repair_is_idempotent_and_accepts_common_root(self):
        self.fixture()
        compat.ensure_map_compat(self.common)
        before = self.state()
        times = {str(p): p.stat().st_mtime_ns for p in self.common.rglob('*') if p.is_file()}
        self.assertEqual((), compat.ensure_map_compat(self.root))
        self.assertEqual(before, self.state())
        self.assertEqual(times, {str(p): p.stat().st_mtime_ns for p in self.common.rglob('*') if p.is_file()})

    def test_modern_august_native_helper_remains_byte_identical(self):
        self.fixture('2024-08')
        before = self.state()
        self.assertEqual((), compat.ensure_map_compat(self.root))
        self.assertEqual(before, self.state())

    def test_unpatched_july_without_susfs_call_is_unchanged(self):
        self.fixture(patched=False)
        before = self.state()
        self.assertEqual((), compat.ensure_map_compat(self.root))
        self.assertEqual(before, self.state())

    def test_partial_native_backport_repairs_only_its_missing_half(self):
        self.fixture()
        header = self.common / 'include/linux/page_size_compat.h'
        implementation = self.common / 'mm/page_size_compat.c'
        original_header, original_implementation = header.read_bytes(), implementation.read_bytes()
        header.write_text(header.read_text().replace('#endif /* __LINUX_PAGE_SIZE_COMPAT_H */',
                          compat.DECLARATION + '\n#endif /* __LINUX_PAGE_SIZE_COMPAT_H */'),
                          encoding='utf-8', newline='\n')
        existing_header = header.read_bytes()
        self.assertEqual((implementation,), compat.ensure_map_compat(self.root))
        self.assertEqual(existing_header, header.read_bytes())
        header.write_bytes(original_header)
        implementation.write_bytes(original_implementation + b'\n' + compat.IMPLEMENTATION.encode())
        existing_implementation = implementation.read_bytes()
        self.assertEqual((header,), compat.ensure_map_compat(self.root))
        self.assertEqual(existing_implementation, implementation.read_bytes())

    def test_missing_native_prerequisites_or_unknown_header_refuse_all_writes(self):
        self.fixture()
        changes = [('mm/page_size_compat.c', 'DEFINE_STATIC_KEY_FALSE(page_shift_compat_enabled)', 'removed_key'),
                   ('mm/page_size_compat.c', 'void ___filemap_fixup(', 'void different_api('),
                   ('include/linux/page_size_compat.h', '#define __VM_NO_COMPAT', '#define DIFFERENT_FLAG'),
                   ('include/linux/page_size_compat.h', '#endif /* __LINUX_PAGE_SIZE_COMPAT_H */', '#endif')]
        original = self.state()
        for relative, old, new in changes:
            with self.subTest(path=relative, old=old):
                for name, raw in original.items():
                    (self.common / name).write_bytes(raw)
                path = self.common / relative
                path.write_text(path.read_text().replace(old, new), encoding='utf-8', newline='\n')
                before = self.state()
                with self.assertRaises(ValueError):
                    compat.ensure_map_compat(self.root)
                self.assertEqual(before, self.state())

    def test_unknown_existing_signature_is_rejected_not_duplicated(self):
        self.fixture()
        path = self.common / 'mm/page_size_compat.c'
        path.write_text(path.read_text() + '\nvoid ' + compat.SYMBOL + '(void) {}\n', encoding='utf-8', newline='\n')
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'unsupported signature'):
            compat.ensure_map_compat(self.root)
        self.assertEqual(before, self.state())

    @unittest.skipUnless(COMPILER and os.name != 'nt', 'A native POSIX C compiler is required')
    def test_real_backported_function_compiles_and_preserves_iterator_semantics(self):
        self.fixture()
        compat.ensure_map_compat(self.root)
        text = (self.common / 'mm/page_size_compat.c').read_text()
        self.assertIn(compat.IMPLEMENTATION, text)
        # The function under test is the actual production/official C body.
        # Model only its kernel iterator/static-key dependencies, not its logic.
        program = r'''
#include <assert.h>
#include <stddef.h>
#define __VM_NO_COMPAT (1UL << 58)
struct vm_area_struct { unsigned long vm_flags; unsigned long vm_end; };
struct vma_iterator { struct vm_area_struct *next; int advances; int rewinds; };
static int page_shift_compat_enabled;
static int static_branch_unlikely(int *key) { return *key; }
static struct vm_area_struct *vma_next(struct vma_iterator *iter) { iter->advances++; return iter->next; }
static void vma_prev(struct vma_iterator *iter) { iter->rewinds++; }
''' + compat.IMPLEMENTATION + r'''
int main(void) {
    struct vm_area_struct fixup = { __VM_NO_COMPAT, 0x9000 };
    struct vm_area_struct ordinary = { 0, 0xa000 };
    struct vma_iterator iter = { &fixup, 0, 0 };
    unsigned long end = 0x8000;
    __fold_filemap_fixup_entry(&iter, &end);
    assert(end == 0x8000 && iter.advances == 0 && iter.rewinds == 0);
    page_shift_compat_enabled = 1;
    __fold_filemap_fixup_entry(&iter, &end);
    assert(end == 0x9000 && iter.advances == 1 && iter.rewinds == 0);
    iter.next = &ordinary; end = 0x8000;
    __fold_filemap_fixup_entry(&iter, &end);
    assert(end == 0x8000 && iter.advances == 2 && iter.rewinds == 1);
    iter.next = NULL;
    __fold_filemap_fixup_entry(&iter, &end);
    assert(end == 0x8000 && iter.advances == 3 && iter.rewinds == 2);
    return 0;
}
'''
        source = self.root / 'fold.c'
        output = self.root / 'fold-test'
        source.write_text(program.replace(compat.IMPLEMENTATION, '', 1), encoding='utf-8', newline='\n')
        missing = subprocess.run([COMPILER, '-std=c11', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(output)],
                                 capture_output=True, text=True, timeout=30)
        self.assertNotEqual(0, missing.returncode)
        self.assertIn(compat.SYMBOL, missing.stderr)
        source.write_text(program, encoding='utf-8', newline='\n')
        compiled = subprocess.run([COMPILER, '-std=c11', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(output)],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(0, compiled.returncode, compiled.stdout + compiled.stderr)
        result = subprocess.run([str(output)], capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
