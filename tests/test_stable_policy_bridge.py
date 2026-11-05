"""Link the real stable policy consumers to the fixed SUSFS kernel wrappers."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from additional_manager_integration import verify_stable_policy_bridge
import additional_manager_integration as integration
from verify_kow_susfs import function_source
from validate_susfs_hooks import strip_comments_and_literals

FIXTURE = ROOT / 'tests/fixtures/next-direct-policy'
PORTS = ('next-susfs23-stable', 'official-susfs23-stable')
BAD_AUDIT = ('    if (security_dump_masked_av_fn)\n'
             '        security_dump_masked_av_fn(policydb, scontext, tcontext, tclass, masked, "bounds");')
GOOD_AUDIT = '    security_dump_masked_av_fn(policydb, scontext, tcontext, tclass, masked, "bounds");'
BAD_COMPUTE = ('    if (context_struct_compute_av_fn) {\n'
               '        context_struct_compute_av_fn(policydb, scontext, tcontext, tclass, avd, NULL);\n'
               '    } else {\n'
               '        context_struct_compute_av(policydb, scontext, tcontext, tclass, avd, NULL);\n    }')
GOOD_COMPUTE = '    context_struct_compute_av_fn(policydb, scontext, tcontext, tclass, avd, NULL);'


def compiler():
    value = os.environ.get('GKI_TEST_CC')
    if value:
        return json.loads(value) if value.startswith('[') else shlex.split(value)
    path = next((shutil.which(name) for name in ('clang', 'gcc', 'cc') if shutil.which(name)), None)
    return [path] if path else None


def seed_port(directory, name):
    inventory_path = ROOT / 'tools/patches' / (name + '-base')
    inventory = json.loads((inventory_path / 'objects.json').read_text(encoding='utf-8'))
    patch = ROOT / 'tools/patches' / (name + '.patch')
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == inventory['patch_sha256']
    subprocess.run(['git', 'init', '-q', str(directory)], check=True, capture_output=True)
    for block in patch.read_text(encoding='utf-8').split('diff --git ')[1:]:
        relative = re.match(r'a/([^\n ]+) b/', block)[1]
        identity = re.search(r'^index ([0-9a-f]{40})\.\.', block, re.M)[1]
        if identity == '0' * 40: continue
        digest = inventory['objects'][identity]
        raw = (inventory_path / (digest + '.source')).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        assert hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == identity
        path = directory / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    originals = {path.relative_to(directory): path.read_bytes() for path in directory.rglob('*')
                 if path.is_file() and '.git' not in path.parts}
    for arguments in (['--check'], [], ['--reverse', '--check'], ['--reverse']):
        subprocess.run(['git', '-C', str(directory), '-c', 'core.autocrlf=false', 'apply', *arguments, str(patch)],
                       check=True, capture_output=True)
    assert originals == {path.relative_to(directory): path.read_bytes() for path in directory.rglob('*')
                         if path.is_file() and '.git' not in path.parts}
    subprocess.run(['git', '-C', str(directory), '-c', 'core.autocrlf=false', 'apply', str(patch)], check=True, capture_output=True)
    for block in patch.read_text(encoding='utf-8').split('diff --git ')[1:]:
        relative = re.match(r'a/[^\n ]+ b/([^\n ]+)', block)[1]
        identity = re.search(r'^index [0-9a-f]{40}\.\.([0-9a-f]{40})', block, re.M)[1]
        path = directory / relative
        if identity == '0' * 40: assert not path.exists()
        else:
            raw = path.read_bytes()
            assert hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == identity
    return (directory / 'kernel/feature/selinux_hide.c').read_text(encoding='utf-8')


HEADER = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#define __nocfi
#define likely(x) (x)
#define unlikely(x) (x)
#define BUG_ON(x) assert(!(x))
#define pr_info(...) ((void)0)
#define pr_err(...) ((void)(errors++))
#define KERNEL_VERSION(a,b,c) (((a)<<16)|((b)<<8)|(c))
#define AVD_FLAGS_PERMISSIVE 1
typedef uint32_t u32; typedef uint16_t u16;
struct context { u32 type; };
struct type_datum { u32 bounds; };
struct extended_perms { int unused; };
struct av_decision { u32 allowed, auditallow, auditdeny, seqno, flags; };
struct policydb { struct type_datum **type_val_to_struct; u32 permissive_map; bool allow_unknown; };
struct sidtab { struct context *entries[4]; };
struct selinux_policy { struct policydb policydb; struct sidtab *sidtab; u32 latest_granting; };
extern unsigned native_audits, native_computes, errors;
extern u32 audited_mask, lower_allowed;
extern const char *audited_reason;
extern struct policydb *last_policy;
extern struct context *last_source, *last_target;
extern u16 last_class;
'''
STATE = r'''
unsigned native_audits, native_computes, errors;
u32 audited_mask, lower_allowed;
const char *audited_reason;
struct policydb *last_policy;
struct context *last_source, *last_target;
u16 last_class;
'''
PROVIDER = r'''
static void security_dump_masked_av(struct policydb *p, struct context *s, struct context *t,
                                    u16 cls, u32 permissions, const char *reason) {
    native_audits++; last_policy=p; last_source=s; last_target=t; last_class=cls;
    audited_mask=permissions; audited_reason=reason;
}
static void context_struct_compute_av(struct policydb *p, struct context *s, struct context *t,
                                      u16 cls, struct av_decision *avd, struct extended_perms *x) {
    assert(x==NULL); native_computes++; last_policy=p; last_source=s; last_target=t; last_class=cls;
    avd->allowed=0xa5;
}
'''
CONSUMER_ENV = r'''
struct selinux_state { bool initialized; struct selinux_policy *policy; } fake_state;
struct selinux_policy *backup_sepolicy;
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6,6,0)
static struct context *sidtab_search(struct sidtab *s, u32 id) { return id<4 ? s->entries[id] : NULL; }
static bool ebitmap_get_bit(u32 *bitmap, u32 type) { return (*bitmap & (1U<<type))!=0; }
static void context_struct_compute_av(struct policydb *p, struct context *s, struct context *t,
                                      u16 cls, struct av_decision *avd, struct extended_perms *x) {
    (void)p; (void)cls; assert(s->type==2 && t->type==2 && x==NULL); avd->allowed=lower_allowed;
}
#endif
'''
MAIN = r'''
int main(void) {
    struct selinux_policy policy={0};
    backup_sepolicy=NULL; assert(ksu_selinux_hide_enable()==-EAGAIN);
    backup_sepolicy=&policy; assert(ksu_selinux_hide_enable()==0);
    errors=0;
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6,6,0)
    struct context s={.type=1}, t={.type=1};
    struct type_datum source={.bounds=2}, target={.bounds=2};
    struct type_datum *types[]={&source,&target};
    struct sidtab sidtab={.entries={NULL,&s,&t,NULL}};
    policy.policydb.type_val_to_struct=types; policy.sidtab=&sidtab; policy.latest_granting=17;
    struct av_decision avd={.allowed=0xf}; lower_allowed=0x5;
    type_attribute_bounds_av(&policy.policydb,&s,&t,7,&avd);
    assert(avd.allowed==0x5 && native_audits==1 && audited_mask==0xa && !strcmp(audited_reason,"bounds"));
    assert(last_policy==&policy.policydb && last_source==&s && last_target==&t && last_class==7);
    avd.allowed=0x5; type_attribute_bounds_av(&policy.policydb,&s,&t,7,&avd);
    assert(native_audits==1 && avd.allowed==0x5);
    source.bounds=0; avd.allowed=0xf; type_attribute_bounds_av(&policy.policydb,&s,&t,7,&avd);
    assert(native_audits==1 && avd.allowed==0xf);
    policy.policydb.permissive_map=(1U<<1);
    security_compute_av_user_with_policy(&policy,1,2,7,&avd);
    assert(native_computes==1 && avd.allowed==0xa5 && avd.flags==AVD_FLAGS_PERMISSIVE && avd.seqno==17);
    assert(avd.auditallow==0 && avd.auditdeny==0xffffffffU && last_source==&s && last_target==&t);
    security_compute_av_user_with_policy(&policy,3,2,7,&avd);
    assert(native_computes==1 && avd.allowed==0 && errors==1);
    security_compute_av_user_with_policy(&policy,1,3,7,&avd);
    assert(native_computes==1 && avd.allowed==0 && errors==2);
    policy.policydb.allow_unknown=false; security_compute_av_user_with_policy(&policy,1,2,0,&avd);
    assert(native_computes==1 && avd.allowed==0);
    policy.policydb.allow_unknown=true; security_compute_av_user_with_policy(&policy,1,2,0,&avd);
    assert(native_computes==1 && avd.allowed==0xffffffffU);
#else
    assert(fake_state.initialized && fake_state.policy==&policy);
#endif
    return 0;
}
'''


class StablePolicyBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gki-stable-policy-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sources = {}
        for name in PORTS:
            path = self.root / name; path.mkdir(); self.sources[name] = seed_port(path, name)

    def test_complete_ports_round_trip_and_only_the_two_unreachable_null_checks_changed(self):
        info = json.loads((FIXTURE / 'provenance.json').read_text(encoding='utf-8'))
        for name, source in self.sources.items():
            raw = source.encode(); expected = info['sources'][name]
            self.assertEqual(expected['fixed_postimage_blob'], hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest())
            restored = source.replace(GOOD_AUDIT, BAD_AUDIT, 1).replace(GOOD_COMPUTE, BAD_COMPUTE, 1).encode()
            self.assertEqual(expected['failed_postimage_blob'], hashlib.sha1(b'blob '+str(len(restored)).encode()+b'\0'+restored).hexdigest())
            verify_stable_policy_bridge(self.root / name)
        for series, provider in info['providers'].items():
            self.assertEqual(provider['fixture_sha256'], hashlib.sha256((FIXTURE / ('provider-'+series+'.c')).read_bytes()).hexdigest())

    def test_old_compile_bug_is_rejected_before_source_promotion(self):
        for name, source in self.sources.items():
            path = self.root / name / 'kernel/feature/selinux_hide.c'
            for broken in (source.replace(GOOD_AUDIT, BAD_AUDIT, 1), source.replace(GOOD_COMPUTE, BAD_COMPUTE, 1)):
                path.write_text(broken, encoding='utf-8', newline='\n')
                with self.subTest(port=name), self.assertRaisesRegex(ValueError, 'without a NULL check'):
                    verify_stable_policy_bridge(self.root / name)
            path.write_text(source, encoding='utf-8', newline='\n')

    def test_preflight_is_scoped_to_stable_ports_and_does_not_reject_other_nullable_callbacks(self):
        for name in ('next-susfs23', 'official-susfs23', 'kow-susfs23', *PORTS):
            with self.subTest(port=name), ExitStack() as stack:
                for callback in ('verify_native_preinit_cleanup', 'verify_legacy_next_lifecycle', 'verify_native_policy_generation'):
                    stack.enter_context(mock.patch.object(integration, callback))
                stack.enter_context(mock.patch('verify_kow_susfs.verify_kow_port'))
                stack.enter_context(mock.patch('ensure_official_init_compat.ensure_official_init_compat'))
                stack.enter_context(mock.patch.object(integration.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([],0,stdout=b'',stderr=b'')))
                checked=stack.enter_context(mock.patch.object(integration,'verify_stable_policy_bridge'))
                integration.apply_next_port(self.root / PORTS[0],name=name)
                if name in PORTS: checked.assert_called_once_with(self.root / PORTS[0])
                else: checked.assert_not_called()

    def units(self, source, series, *, broken=False):
        if broken: source=source.replace(GOOD_AUDIT,BAD_AUDIT,1).replace(GOOD_COMPUTE,BAD_COMPUTE,1)
        # Retain every byte of each definition, including its real storage,
        # return type and CFI annotation; only surrounding kernel types are stubbed.
        functions=[]
        for name in ('avd_init','type_attribute_bounds_av','security_compute_av_user_with_policy'):
            body=function_source(source,name); start=source.index(body)
            functions.append(source[source.rfind('\n',0,start)+1:start]+body)
        functions='\n\n'.join(functions)
        declarations='\n'.join(re.search(r'extern void '+symbol+r'\([^;]+;',source)[0]
                               for symbol in ('security_dump_masked_av_fn','context_struct_compute_av_fn'))
        enable='static int '+function_source(source,'ksu_selinux_hide_enable')
        consumer='#include "contract.h"\n'+CONSUMER_ENV+'\n'+enable+'\n#if LINUX_VERSION_CODE >= KERNEL_VERSION(6,6,0)\n'+declarations+'\n'+functions+'\n#endif\n'+MAIN
        provider='#include "contract.h"\n'+STATE+'\n'+PROVIDER+'\n'+(FIXTURE/('provider-'+series+'.c')).read_text(encoding='utf-8')
        return consumer, provider

    def test_real_c_links_exact_kernel_wrappers_and_preserves_policy_boundaries_on_five_series(self):
        cc=compiler()
        if not cc: self.skipTest('A native C compiler is required')
        for name, source in self.sources.items():
            for major, minor, series in ((5,10,'6.6'),(5,15,'6.6'),(6,1,'6.6'),(6,6,'6.6'),(6,12,'6.12')):
                with self.subTest(port=name, kernel=f'{major}.{minor}'):
                    self.compile_and_run(cc,source,series,major,minor)

    def compile_and_run(self, cc, source, series, major, minor, *, broken=False, no_provider=False):
        consumer,provider=self.units(source,series,broken=broken)
        (self.root/'contract.h').write_text(HEADER,encoding='utf-8')
        (self.root/'consumer.c').write_text(consumer,encoding='utf-8')
        (self.root/'provider.c').write_text('#include "contract.h"\n'+STATE if no_provider else provider,encoding='utf-8')
        binary=self.root/('policy.exe' if os.name=='nt' else 'policy')
        command=[*cc,'-std=c11','-Wall','-Wextra','-Werror','-DCONFIG_KSU_SUSFS=1',
                 '-DLINUX_VERSION_CODE='+str((major<<16)|(minor<<8)),str(self.root/'consumer.c'),str(self.root/'provider.c'),'-o',str(binary)]
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=90)
        if broken or no_provider:
            self.assertNotEqual(0,result.returncode)
            self.assertIn('security_dump_masked_av_fn',result.stderr)
            self.assertIn('context_struct_compute_av_fn',result.stderr)
        else:
            self.assertEqual(0,result.returncode,result.stderr)
            executed=subprocess.run([str(binary)],capture_output=True,text=True,encoding='utf-8',timeout=10)
            self.assertEqual(0,executed.returncode,executed.stdout+executed.stderr)

    def test_old_strong_function_null_checks_fail_real_compilation_and_missing_provider_fails_link(self):
        cc=compiler()
        if not cc: self.skipTest('A native C compiler is required')
        for name,source in self.sources.items():
            for minor,series in ((6,'6.6'),(12,'6.12')):
                with self.subTest(port=name,kernel='6.'+str(minor)):
                    self.compile_and_run(cc,source,series,6,minor,broken=True)
                    self.compile_and_run(cc,source,series,6,minor,no_provider=True)


if __name__ == '__main__': unittest.main()
