"""Replay the failed 6.6.30 hooks against real, checksummed native functions."""
from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from oplus_compat import function_text
from oplus_susfs66 import adapt_susfs66

FIXTURE = ROOT / 'tests/fixtures/oplus-susfs66'


class OPlusSusfs66Tests(unittest.TestCase):
    def setUp(self):
        self.common = ROOT / 'tests' / ('.oplus-susfs66-' + uuid.uuid4().hex)
        (self.common / 'fs/proc').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.common)
        self.namespace = self.common / 'fs/namespace.c'
        self.task = self.common / 'fs/proc/task_mmu.c'
        self.namespace.write_bytes((FIXTURE / 'namespace-header.c').read_bytes())
        self.original = (FIXTURE / 'task-mmu-functions.c').read_text(encoding='utf-8')
        self.task.write_text(self.original, encoding='utf-8', newline='\n')
        self.patch = (FIXTURE / 'susfs-map-hooks.patch').read_bytes()

    def test_fixture_records_the_exact_official_source_and_patch(self):
        provenance = json.loads((FIXTURE / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual('sm8750-6.6.30', provenance['profile'])
        self.assertEqual('f9c1a38fe1bf6bf39f25e4167e8322813c7707b9', provenance['source_ref'])
        self.assertEqual('4a4cb1be85a4694372d3efb9420de69982eac29a', provenance['patch_ref'])
        for name, expected in provenance['sha256'].items():
            self.assertEqual(expected, hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest())

    def test_adaptation_retains_real_vma_scope_and_the_native_lock_and_iterator_flow(self):
        adapted = adapt_susfs66(self.common, self.patch)
        self.assertNotIn(b' #include <trace/hooks/fs.h>\n', adapted)
        self.assertIn(b' #include <trace/hooks/blk.h>\n', adapted)
        self.assertIn(b' \tstruct vm_area_struct *vma = get_data_vma(v);', adapted)
        self.assertIn(b'if (!vma->vm_file || !SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))', adapted)
        self.assertNotIn(b'+bypass_orig_flow:', adapted)
        self.assertEqual(self.original, self.task.read_text(encoding='utf-8'))

    @unittest.skipUnless(shutil.which('patch'), 'GNU patch is not available on this host')
    def test_real_failed_hunks_then_strict_forward_apply_reverse_and_function_scopes(self):
        prepared = self.common / 'official.patch'
        prepared.write_bytes(self.patch)
        def execute(*options):
            return subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(prepared), *options],
                                  cwd=self.common, capture_output=True, text=True)
        rejected = execute('--forward', '--dry-run')
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn('FAILED', rejected.stdout)
        self.assertEqual(self.original, self.task.read_text(encoding='utf-8'))
        prepared.write_bytes(adapt_susfs66(self.common, self.patch))
        for options in (('--forward', '--dry-run'), ('--forward',), ('--reverse', '--dry-run')):
            result = execute(*options)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        result = self.task.read_text(encoding='utf-8')
        smap, rollup = function_text(result, 'show_smap'), function_text(result, 'show_smaps_rollup')
        self.assertLess(smap.index('struct vm_area_struct *vma ='), smap.index('SUSFS_IS_INODE_SUS_MAP'))
        self.assertEqual(1, smap.count('SUSFS_IS_INODE_SUS_MAP'))
        self.assertEqual(2, rollup.count('SUSFS_IS_INODE_SUS_MAP'))
        self.assertEqual(2, rollup.count('smap_gather_stats('))
        # Remove only the new conditional wrappers. Apart from indentation,
        # disabled SUS_MAP must retain the original native function unchanged.
        disabled = re.sub(r'#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n[^\n]*\n'
                          r'#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n', '', rollup)
        original_rollup = function_text(self.original, 'show_smaps_rollup')
        self.assertEqual(re.sub(r'\s+', '', original_rollup), re.sub(r'\s+', '', disabled))
        self.assertEqual(1, rollup.count('last_vma_end = vma->vm_end;'))
        self.assertEqual(2, rollup.count('mmap_read_unlock(mm);'))
        self.assertEqual(2, rollup.count('release_task_mempolicy(priv);'))

    def test_unknown_declaration_duplicate_function_or_missing_gather_reject_without_writes(self):
        for source in (self.original.replace('get_data_vma(v)', 'unreviewed_vma(v)'),
                       self.original + '\n' + function_text(self.original, 'show_smap'),
                       self.original.replace('smap_gather_stats(vma, &mss, last_vma_end);', 'unknown_stats(vma);')):
            self.task.write_text(source, encoding='utf-8', newline='\n')
            with self.subTest(source=source[:80]), self.assertRaises(ValueError):
                adapt_susfs66(self.common, self.patch)
            self.assertEqual(source, self.task.read_text(encoding='utf-8'))

    def test_changed_upstream_feature_additions_or_removals_require_review(self):
        for data in (self.patch.replace(b'+\t\t\treturn 0;', b'+\t\t\treturn 1;'),
                     self.patch.replace(b'+\t\t\tgoto bypass_orig_flow;', b'+\t\t\tgoto other_flow;'),
                     self.patch.replace(b' \t\tlast_vma_end = vma->vm_end;', b'-\t\tlast_vma_end = vma->vm_end;')):
            with self.subTest(data=data[-40:]), self.assertRaisesRegex(ValueError, 'require review'):
                adapt_susfs66(self.common, data)
        self.assertEqual(self.original, self.task.read_text(encoding='utf-8'))

    def test_already_present_header_is_preserved_and_unfamiliar_header_context_rejected(self):
        source = self.namespace.read_text(encoding='utf-8')
        self.namespace.write_text(source.replace('#include <trace/hooks/blk.h>\n',
                                 '#include <trace/hooks/blk.h>\n#include <trace/hooks/fs.h>\n'),
                                  encoding='utf-8', newline='\n')
        self.assertIn(b' #include <trace/hooks/fs.h>\n', adapt_susfs66(self.common, self.patch))
        self.namespace.write_text(source.replace('100000', '200000'), encoding='utf-8', newline='\n')
        with self.assertRaisesRegex(ValueError, 'Unknown OKI namespace header context'):
            adapt_susfs66(self.common, self.patch)


if __name__ == '__main__':
    unittest.main()
