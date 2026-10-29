"""Replay the real official/SUSFS function and check legacy map boundaries."""
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
import oplus_vma_compat as compat

FIXTURES = ROOT / 'tests/fixtures/oplus-vma57'
COMPILER = os.environ.get('OPLUS_VMA_CC') or next(
    (shutil.which(name) for name in ('cc', 'gcc', 'clang') if shutil.which(name)), None)


def fixture(name):
    return bytes.fromhex((FIXTURES / (name + '.hex')).read_text(encoding='ascii'))


HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <stdarg.h>
typedef unsigned long vm_flags_t;
typedef unsigned long dev_t;
typedef long long loff_t;
#define PAGE_SHIFT 12
struct seq_file { int headers, paths, newlines; unsigned long start, end, ino, dev; unsigned long long pgoff; const char *name; };
struct super_block { dev_t s_dev; };
struct inode { struct super_block *i_sb; unsigned long i_ino; bool redirect, hidden; };
struct file { struct inode *inode; };
struct mm_struct { unsigned long brk, start_brk; };
struct anon_vma_name { const char *name; };
struct vm_area_struct;
struct vm_operations_struct { const char *(*name)(struct vm_area_struct *); };
struct vm_area_struct { struct mm_struct *vm_mm; struct file *vm_file; unsigned long vm_start, vm_end, vm_pgoff; vm_flags_t vm_flags; struct vm_operations_struct *vm_ops; };
struct srcu_struct { int held, locks, unlocks; };
static struct srcu_struct susfs_srcu_open_redirect;
static int redirect_result;
static const char *redirect_name;
static struct inode *file_inode(struct file *f) { return f->inode; }
#define SUSFS_IS_INODE_OPEN_REDIRECT(p) ((p)->redirect)
#define SUSFS_IS_INODE_SUS_MAP(p) ((p)->hidden)
int srcu_read_lock(struct srcu_struct *p) { assert(!p->held); p->held = 1; p->locks++; return 7; }
void srcu_read_unlock(struct srcu_struct *p, int idx) { assert(p->held && idx == 7); p->held = 0; p->unlocks++; }
int susfs_open_redirect_spoof_show_map_vma_srcu(struct inode *p, unsigned long *ino, dev_t *dev, char **name)
{ assert(p->redirect && susfs_srcu_open_redirect.held); *ino=789; *dev=42; *name=(char *)redirect_name; return redirect_result; }
void susfs_sus_kstat_spoof_show_map_vma(struct inode *p, dev_t *dev, unsigned long *ino)
{ (void)p; *dev=73; *ino=91; }
static void show_vma_header_prefix(struct seq_file *m, unsigned long start, unsigned long end, vm_flags_t flags,
                                   unsigned long long pgoff, dev_t dev, unsigned long ino)
{ (void)flags; m->headers++; m->start=start; m->end=end; m->pgoff=pgoff; m->dev=dev; m->ino=ino; }
static void seq_pad(struct seq_file *m, char c) { (void)m; assert(c==' '); }
static void seq_puts(struct seq_file *m, const char *s) { m->name=s; }
static void seq_putc(struct seq_file *m, char c) { assert(c=='\n'); m->newlines++; }
static void seq_file_path(struct seq_file *m, struct file *f, const char *escape)
{ (void)f; assert(strcmp(escape,"\n")==0); m->paths++; }
static const char *arch_vma_name(struct vm_area_struct *vma) { (void)vma; return NULL; }
static bool is_stack(struct vm_area_struct *vma) { (void)vma; return false; }
static struct anon_vma_name *anon_vma_name(struct vm_area_struct *vma) { (void)vma; return NULL; }
static void seq_printf(struct seq_file *m, const char *s, ...) { (void)m; (void)s; }
@SHOW_MAP@
static void reset(struct seq_file *m)
{ memset(m,0,sizeof(*m)); memset(&susfs_srcu_open_redirect,0,sizeof(susfs_srcu_open_redirect)); redirect_result=0; redirect_name="redirected"; }
int main(void)
{
    struct super_block sb={13}; struct inode inode={&sb,29,true,false}; struct file file={&inode};
    struct mm_struct mm={0,0}; struct vm_area_struct vma={&mm,&file,0x11000UL,0x19000UL,3,5,NULL};
    struct seq_file m;
    reset(&m); show_map_vma(&m,&vma);
    assert(m.headers==1 && m.start==0x11000UL && m.end==0x19000UL && m.pgoff==0x3000UL && m.newlines==1);
#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT
    assert(m.paths==0 && m.ino==789 && m.dev==42 && strcmp(m.name,"redirected")==0);
    assert(susfs_srcu_open_redirect.locks==1 && susfs_srcu_open_redirect.unlocks==1 && !susfs_srcu_open_redirect.held);
    reset(&m); redirect_name=NULL; show_map_vma(&m,&vma);
    assert(m.headers==1 && m.end==vma.vm_end && !m.name && !m.paths && m.newlines==1);
    assert(susfs_srcu_open_redirect.locks==1 && susfs_srcu_open_redirect.unlocks==1);
    reset(&m); redirect_result=-1; show_map_vma(&m,&vma);
    assert(m.headers==1 && m.paths==1 && m.end==vma.vm_end && m.newlines==1);
    assert(susfs_srcu_open_redirect.locks==1 && susfs_srcu_open_redirect.unlocks==1);
#else
    assert(m.paths==1 && susfs_srcu_open_redirect.locks==0 && susfs_srcu_open_redirect.unlocks==0);
#endif
    reset(&m); inode.redirect=false; inode.hidden=true; show_map_vma(&m,&vma);
#ifdef CONFIG_KSU_SUSFS_SUS_MAP
    assert(m.headers==0 && m.paths==0 && m.newlines==0);
#else
    assert(m.headers==1 && m.paths==1 && m.end==vma.vm_end);
#endif
    reset(&m); inode.hidden=false; show_map_vma(&m,&vma);
    assert(m.headers==1 && m.paths==1 && m.end==vma.vm_end && m.newlines==1);
#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT
    assert(m.dev==73 && m.ino==91);
#else
    assert(m.dev==13 && m.ino==29);
#endif
    reset(&m); vma.vm_file=NULL; vma.vm_mm=NULL; show_map_vma(&m,&vma);
    assert(m.headers==1 && m.end==vma.vm_end && m.paths==0 && strcmp(m.name,"[vdso]")==0 && m.newlines==1);
    return 0;
}
'''


class OplusVmaCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.common = ROOT / 'tests' / ('.vma-compat-' + uuid.uuid4().hex)
        self.common.mkdir()
        self.addCleanup(shutil.rmtree, self.common)
        self.profile = {'id':compat.PROFILE, 'kernel_repo':compat.REPOSITORY,
                        'kernel_ref':compat.COMMIT, 'platform':'sm8650', 'kernel':'6.1.57'}

    def prepare(self, patched=True):
        for relative in compat.NATIVE_HEADERS:
            path=self.common/relative; path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(fixture(relative.replace('/','_')))
        path=self.common/'fs/proc/task_mmu.c'; path.parent.mkdir(parents=True)
        path.write_bytes(fixture('native-show_map_vma.c')+b'\n')
        subprocess.run(['git','init','--quiet'],cwd=self.common,check=True,capture_output=True)
        if patched:
            result=subprocess.run(['git','-c','core.autocrlf=false','apply','-'],input=fixture('susfs23-show_map_vma.patch'),
                                  cwd=self.common,capture_output=True)
            self.assertEqual(0,result.returncode,result.stderr.decode())
        return path

    def state(self):
        return {p.relative_to(self.common).as_posix():p.read_bytes() for p in self.common.rglob('*')
                if p.is_file() and '.git' not in p.parts}

    def run_prepare(self):
        with mock.patch.object(compat,'native_head'):
            return compat.prepare_vma_compat(self.common,self.profile)

    def test_pinned_fixture_provenance_and_native_no_padding_semantics(self):
        document=json.loads((FIXTURES/'provenance.json').read_text(encoding='utf-8'))
        for record in document['files']:
            raw=bytes.fromhex((FIXTURES/record['fixture']).read_text())
            self.assertEqual(record['size'],len(raw))
            self.assertEqual(record['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertRegex(record['commit'],r'^[a-f0-9]{40}$')
        modern=fixture('75-include_linux_pgsize_migration.h').decode()
        self.assertIn('#define VMA_PAD_START(vma)\t(vma->vm_end - (vma_pad_pages(vma) << PAGE_SHIFT))',modern)
        self.assertIn('static inline unsigned long vma_pad_pages(struct vm_area_struct *vma)\n{\n\treturn 0;\n}',modern)
        for relative,fingerprint in compat.NATIVE_HEADERS.items():
            raw=fixture(relative.replace('/','_'))
            self.assertEqual(fingerprint,compat.digest(raw))
            self.assertNotIn('VMA_PAD_START',raw.decode())

    def test_actual_official_function_and_susfs_hunk_only_change_native_end(self):
        path=self.prepare(); before=self.state(); original=path.read_text()
        self.assertEqual(compat.SUSFS_FUNCTION_SHA256,compat.digest(compat.show_map_function(original).encode()))
        self.assertTrue(self.run_prepare())
        self.assertEqual(original.replace(compat.PADDED_END,compat.NATIVE_END,1),path.read_text())
        after=self.state(); self.assertEqual(before.keys(),after.keys())
        for name in before.keys()-{'fs/proc/task_mmu.c'}: self.assertEqual(before[name],after[name])
        state=self.state(); times={name:(self.common/name).stat().st_mtime_ns for name in state}
        self.assertFalse(self.run_prepare()); self.assertEqual(state,self.state())
        self.assertEqual(times,{name:(self.common/name).stat().st_mtime_ns for name in state})

    def test_native_unpatched_and_other_profiles_unchanged(self):
        self.prepare(patched=False); before=self.state()
        self.assertFalse(self.run_prepare()); self.assertEqual(before,self.state())
        for profile in ('sm8650-6.1.75','sm8650-6.1.118','sm8650-6.1.141','mt6897-6.1.75','sm8750-6.6.30'):
            self.assertFalse(compat.prepare_vma_compat(self.common,{'id':profile}))
        self.assertEqual(before,self.state())

    def test_changed_profile_commit_headers_or_hook_fail_before_write(self):
        path=self.prepare(); original=self.state()
        for key,value in (('kernel_ref','f'*40),('kernel_repo','other/kernel'),('platform','mt6897'),('kernel','6.1.75')):
            with self.subTest(key=key):
                with self.assertRaises(ValueError): compat.prepare_vma_compat(self.common,{**self.profile,key:value})
        with mock.patch.object(compat.subprocess,'run',return_value=mock.Mock(returncode=0,stdout='f'*40)):
            with self.assertRaisesRegex(ValueError,'exact reviewed'): compat.prepare_vma_compat(self.common,self.profile)
        for relative in ('include/linux/mm.h','include/linux/mm_types.h','fs/proc/task_mmu.c'):
            for name,raw in original.items(): (self.common/name).write_bytes(raw)
            target=self.common/relative
            if relative.endswith('task_mmu.c'):
                target.write_text(target.read_text().replace('srcu_read_unlock','different_unlock'),encoding='utf-8',newline='\n')
            else: target.write_bytes(target.read_bytes()+b'\n')
            before=self.state()
            with self.assertRaises(ValueError): self.run_prepare()
            self.assertEqual(before,self.state())

    def test_new_migration_source_and_concurrent_change_refuse_overwrite(self):
        path=self.prepare(); extra=self.common/'include/linux/pgsize_migration.h'; extra.write_bytes(b'new representation')
        before=self.state()
        with self.assertRaisesRegex(ValueError,'unexpectedly contains'): self.run_prepare()
        self.assertEqual(before,self.state()); extra.unlink()
        reader=compat.read_source; reads=0
        def racing(common,relative):
            nonlocal reads
            if relative=='fs/proc/task_mmu.c':
                reads+=1
                if reads==2: path.write_bytes(path.read_bytes()+b'/* concurrent change */')
            return reader(common,relative)
        with mock.patch.object(compat,'read_source',side_effect=racing):
            with self.assertRaisesRegex(ValueError,'changed during'): self.run_prepare()
        self.assertIn(b'VMA_PAD_START(vma)',path.read_bytes())
        self.assertTrue(path.read_bytes().endswith(b'/* concurrent change */'))
        self.assertEqual([],list(path.parent.glob('.oplus-vma-*.tmp')))

    @unittest.skipUnless(COMPILER,'A native C compiler is required')
    def test_real_repaired_function_compiles_and_preserves_srcu_mapping_and_spoofing(self):
        path=self.prepare(); self.run_prepare()
        source=self.common/'native-vma.c'; source.write_text(HARNESS.replace('@SHOW_MAP@',path.read_text()),encoding='utf-8',newline='\n')
        compiler=[COMPILER,'cc'] if Path(COMPILER).stem.lower()=='zig' else [COMPILER]
        for name,defines in [('all',('CONFIG_KSU_SUSFS_OPEN_REDIRECT','CONFIG_KSU_SUSFS_SUS_MAP','CONFIG_KSU_SUSFS_SUS_KSTAT')),
                             ('redirect',('CONFIG_KSU_SUSFS_OPEN_REDIRECT',)),('native',())]:
            with self.subTest(configuration=name):
                output=self.common/('native-vma-'+name+('.exe' if os.name=='nt' else ''))
                result=subprocess.run([*compiler,'-std=c11','-Wall','-Wextra','-Werror',*[('-D'+value) for value in defines],
                                       str(source),'-o',str(output)],capture_output=True,text=True,encoding='utf-8',timeout=90)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                result=subprocess.run([str(output)],capture_output=True,text=True,timeout=30)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)


if __name__=='__main__': unittest.main()
