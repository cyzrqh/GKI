"""Check the real MTK maps body against native inactive and active padding."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import oplus_mtk_vma_compat as compat
from test_oplus_vma_compat import HARNESS

FIXTURES = ROOT / 'tests/fixtures/oplus-mtk-vma'
COMPILER = os.environ.get('OPLUS_VMA_CC') or next(
    (shutil.which(name) for name in ('cc', 'gcc', 'clang') if shutil.which(name)), None)


def fixture(name):
    return bytes.fromhex((FIXTURES / (name + '.hex')).read_text('ascii'))


class MtkVmaCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.common = ROOT / 'tests' / ('.mtk-vma-' + uuid.uuid4().hex)
        (self.common / 'fs/proc').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.common)
        self.path = self.common / 'fs/proc/task_mmu.c'
        self.profile = {'id': 'mt6897-6.1.128', 'kernel_repo': compat.PROFILES['mt6897-6.1.128']['repository'],
                        'kernel_ref': compat.PROFILES['mt6897-6.1.128']['commit'],
                        'platform': 'mt6897', 'kernel': '6.1.128'}

    def prepare(self, native=False):
        self.path.write_bytes(b'/* unaffected source prefix */\n'
                              + fixture(('native' if native else 'susfs') + '-show_map_vma.c')
                              + b'\n/* unaffected source suffix */\n')

    def call(self):
        with mock.patch.object(compat, 'check_native', return_value={}):
            return compat.prepare_mtk_vma_compat(self.common, self.profile)

    def test_actual_native_and_susfs_functions_match_reviewed_fingerprints(self):
        self.assertEqual(compat.NATIVE_FUNCTION_SHA256, compat.digest(fixture('native-show_map_vma.c')))
        self.assertEqual(compat.SUSFS_FUNCTION_SHA256, compat.digest(fixture('susfs-show_map_vma.c')))
        proof = json.loads((FIXTURES / 'provenance.json').read_bytes())['source_profiles']
        for pid, selected in compat.PROFILES.items():
            self.assertEqual(selected['commit'], proof[pid]['common_commit'])
            self.assertEqual(selected['repository'], proof[pid]['common_repository'])
            self.assertFalse(proof[pid]['native_kbuild_padding'])
            self.assertFalse(proof[pid]['native_madvise_padding'])
            self.assertTrue(all(proof[pid]['files'][name] == fingerprint for name, fingerprint in selected['native'].items()))
        for pid in ('mt6989-6.1.115', 'mt6989-6.1.134'):
            self.assertTrue(proof[pid]['native_kbuild_padding'])
            self.assertTrue(proof[pid]['native_madvise_padding'])

    def test_one_expression_only_and_idempotence_preserve_native_maps(self):
        self.prepare()
        before = self.path.read_bytes()
        self.assertTrue(self.call())
        after = self.path.read_bytes()
        self.assertEqual(before.replace(compat.PADDED_END.encode(), compat.NATIVE_END.encode(), 1), after)
        self.assertFalse(self.call())
        self.assertEqual(after, self.path.read_bytes())
        self.prepare(native=True)
        native = self.path.read_bytes()
        self.assertFalse(self.call())
        self.assertEqual(native, self.path.read_bytes())

    def test_active_padding_profiles_and_other_platforms_are_never_modified(self):
        self.prepare()
        before = self.path.read_bytes()
        for pid in ('mt6989-6.1.115', 'mt6989-6.1.134', 'sm8650-6.1.57', 'mt6991-6.6.118'):
            with self.subTest(profile=pid), mock.patch.object(compat, 'check_native') as check:
                self.assertFalse(compat.prepare_mtk_vma_compat(self.common, {'id': pid}))
                check.assert_not_called()
        self.assertEqual(before, self.path.read_bytes())

    def test_profile_mixups_and_changed_complete_hook_are_refused(self):
        self.prepare()
        for field, wrong in (('kernel_ref', 'a' * 40), ('verified_kernel_ref', 'b' * 40),
                             ('kernel_repo', 'foreign/kernel'), ('platform', 'mt6989'), ('kernel', '6.1.115')):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'pairing'):
                compat.prepare_mtk_vma_compat(self.common, {**self.profile, field: wrong})
        self.path.write_bytes(self.path.read_bytes().replace(b'srcu_read_unlock', b'different_unlock'))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'OPEN_REDIRECT'):
            self.call()
        self.assertEqual(before, self.path.read_bytes())

    def test_native_lifecycle_inputs_and_new_feature_wiring_cannot_bypass_guard(self):
        # A small synthetic native tree exercises the integrity/absence guards;
        # immutable official source hashes are separately audited above.
        selected = copy.deepcopy(compat.PROFILES[self.profile['id']])
        selected['native'] = {}
        for relative in ('mm/Makefile', 'mm/madvise.c'):
            target = self.common / relative
            target.parent.mkdir(exist_ok=True)
            raw = ('native input ' + relative + '\n').encode()
            target.write_bytes(raw)
            selected['native'][relative] = compat.digest(raw)
        with mock.patch.object(compat, 'native_head'):
            self.assertEqual(2, len(compat.check_native(self.common, selected)))
            (self.common / 'mm/Makefile').write_bytes(b'obj-y += pgsize_migration.o\n')
            with self.assertRaisesRegex(ValueError, 'padding lifecycle'):
                compat.check_native(self.common, selected)
            (self.common / 'mm/Makefile').write_bytes(b'native input mm/Makefile\n')
            extra = self.common / selected['absent'][0]
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_bytes(b'new page size API')
            with self.assertRaisesRegex(ValueError, 'unexpectedly gained'):
                compat.check_native(self.common, selected)

    def test_concurrent_hook_change_is_never_overwritten(self):
        self.prepare()
        reader = compat.read_source
        reads = 0
        def racing(common, relative):
            nonlocal reads
            if relative == 'fs/proc/task_mmu.c':
                reads += 1
                if reads == 2:
                    self.path.write_bytes(self.path.read_bytes() + b'/* independent change */\n')
            return reader(common, relative)
        with mock.patch.object(compat, 'read_source', side_effect=racing), self.assertRaisesRegex(ValueError, 'changed during'):
            self.call()
        self.assertIn(b'VMA_PAD_START', self.path.read_bytes())
        self.assertTrue(self.path.read_bytes().endswith(b'/* independent change */\n'))
        self.assertFalse(list(self.path.parent.glob('.oplus-mtk-vma-*')))

    @unittest.skipUnless(COMPILER, 'A native C compiler is required')
    def test_actual_repaired_mtk_function_preserves_srcu_and_maps_in_three_configurations(self):
        self.prepare()
        self.call()
        source = HARNESS.replace('@SHOW_MAP@', compat.show_map_function(self.path.read_text('utf-8')))
        self.compile_run(source, [('all', ('CONFIG_KSU_SUSFS_OPEN_REDIRECT', 'CONFIG_KSU_SUSFS_SUS_MAP', 'CONFIG_KSU_SUSFS_SUS_KSTAT')),
                                  ('redirect', ('CONFIG_KSU_SUSFS_OPEN_REDIRECT',)), ('native', ())])

    @unittest.skipUnless(COMPILER, 'A native C compiler is required')
    def test_real_active_padding_implementation_and_maps_boundary_are_retained(self):
        # 115/134 genuinely link this exact official implementation. Exercise
        # nonzero padding and its runtime switch, not a made-up replacement.
        prefix = HARNESS.split('@SHOW_MAP@')[0].replace('typedef unsigned long vm_flags_t;', 'typedef unsigned long long vm_flags_t;')
        macros = '\n'.join(line for line in fixture('pgsize_migration_inline.h').decode().splitlines()
                           if line.startswith(('#define VM_PAD_', '#define VM_TOTAL_PAD_PAGES', '#define VMA_PAD_START')))
        native = ('\n#define BITS_PER_LONG 64\n' + macros
                  + '\nstatic bool migration_enabled = true;\n'
                  + '#define is_pgsize_migration_enabled() (migration_enabled)\n'
                  + fixture('vma_pad_pages.c').decode() + '\n')
        body = fixture('susfs-show_map_vma.c').decode()
        source = prefix + native + body + r'''
int main(void) {
    struct super_block sb={13}; struct inode inode={&sb,29,true,false}; struct file file={&inode};
    struct mm_struct mm={0,0}; struct vm_area_struct vma={&mm,&file,0x11000UL,0x19000UL,3,3ULL<<60,NULL};
    struct seq_file m={0};
    redirect_name="redirected";
    show_map_vma(&m,&vma);
    assert(m.headers==1 && m.end==0x16000UL && m.ino==789 && m.dev==42 && m.newlines==1);
    assert(susfs_srcu_open_redirect.locks==1 && susfs_srcu_open_redirect.unlocks==1);
    migration_enabled=false;
    memset(&m,0,sizeof(m)); show_map_vma(&m,&vma);
    assert(m.end==0x19000UL);
    migration_enabled=true; vma.vm_flags=0;
    memset(&m,0,sizeof(m)); show_map_vma(&m,&vma); assert(m.end==0x19000UL);
    return 0;
}
'''
        self.compile_run(source, [('padding', ('CONFIG_KSU_SUSFS_OPEN_REDIRECT', 'CONFIG_KSU_SUSFS_SUS_MAP', 'CONFIG_KSU_SUSFS_SUS_KSTAT'))])

    def compile_run(self, text, configurations):
        source = self.common / 'native-mtk-vma.c'
        source.write_text(text, encoding='utf-8', newline='\n')
        compiler = [COMPILER, 'cc'] if Path(COMPILER).stem.lower() == 'zig' else [COMPILER]
        for label, defines in configurations:
            with self.subTest(configuration=label):
                output = self.common / (label + ('.exe' if os.name == 'nt' else ''))
                result = subprocess.run([*compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                         *['-D' + name for name in defines], str(source), '-o', str(output)],
                                        capture_output=True, text=True, encoding='utf-8', timeout=120)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                result = subprocess.run([str(output)], capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
