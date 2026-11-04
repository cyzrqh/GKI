"""Link Kow's actual policy helpers from a separate SELinux consumer TU."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from verify_kow_susfs import function_source

HELPERS = ('security_context_to_sid_with_policy',
           'security_sid_to_context_with_policy',
           'security_compute_av_user_with_policy')
PRIVATE_CONTEXT = '__security_context_to_sid_with_policy'
FIXTURE = ROOT / 'tests/fixtures/kow-policy-linkage'
COMPILER = (shlex.split(os.environ['GKI_TEST_CC']) if os.environ.get('GKI_TEST_CC') else
            [command] if (command := (shutil.which('cc') or shutil.which('gcc') or shutil.which('clang'))) else [])

# Only kernel services below the three production helpers are simulated. Their
# complete production bodies and storage-class declarations come from the
# reviewed patch, not from a second implementation of the helpers in this test.
MODEL = r'''#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define KERNEL_VERSION(a,b,c) (((a)<<16) | ((b)<<8) | (c))
#define __nocfi
#define EINVAL 22
#define ENOMEM 12
#define ENOENT 2
#define ESTALE 116
#define SECSID_NULL 0
#define AVD_FLAGS_PERMISSIVE 1
#define unlikely(x) (x)
#define rcu_dereference(x) (x)
#define pr_err(...) ((void)0)
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned int gfp_t;
struct context { u32 value, type; };
struct sidtab_entry { struct context context; };
struct sidtab {
    u32 sid;
    struct sidtab_entry entry;
    int fail, calls;
    bool stale_once;
};
struct policydb { int marker, permissive_map; bool allow_unknown; };
struct selinux_policy { struct policydb policydb; struct sidtab *sidtab; u32 latest_granting; };
struct selinux_state {
#if LINUX_VERSION_CODE < KERNEL_VERSION(6,6,0)
    bool initialized;
#endif
    struct selinux_policy *policy;
};
struct av_decision { u32 allowed, auditallow, auditdeny, seqno, flags; };
struct extended_perms;
extern struct selinux_state selinux_state;
extern int allocation_fail, parse_error, original_error;
extern int parse_calls, destroys, rcu_depth, compute_calls;
extern void (*context_struct_compute_av_fn)(struct policydb *, struct context *,
    struct context *, u16, struct av_decision *, struct extended_perms *);
'''

LEAVES = r'''
struct selinux_state selinux_state;
int allocation_fail, parse_error, original_error;
int parse_calls, destroys, rcu_depth, compute_calls;
void (*context_struct_compute_av_fn)(struct policydb *, struct context *,
    struct context *, u16, struct av_decision *, struct extended_perms *);
static char *kmemdup_nul(const void *source, size_t size, gfp_t flags) {
    (void)flags;
    if (allocation_fail) return NULL;
    char *copy = malloc(size + 1);
    assert(copy);
    memcpy(copy, source, size); copy[size] = '\0'; return copy;
}
static void kfree(void *pointer) { free(pointer); }
static void rcu_read_lock(void) { rcu_depth++; }
static void rcu_read_unlock(void) { assert(rcu_depth > 0); rcu_depth--; }
static void context_destroy(struct context *context) { (void)context; destroys++; }
static int string_to_context_struct(struct policydb *policy, struct policydb *original,
    struct sidtab *table, struct sidtab *original_table, char *copy, const char *source,
    u32 size, struct context *context, struct context *original_context,
    u32 default_sid, int *original_rc) {
    assert(policy->marker == 11 && original->marker == 22);
    assert(table != original_table && original_table == selinux_state.policy->sidtab);
    assert(size == 3 && !memcmp(source, "ctx", size) && !memcmp(copy, source, size));
    assert(copy[size] == '\0' && default_sid == 7 && rcu_depth == 1);
    parse_calls++;
    context->value = policy->marker; context->type = 7;
    original_context->value = original->marker; original_context->type = 9;
    *original_rc = original_error;
    return parse_error;
}
static int sidtab_context_to_sid(struct sidtab *table, struct context *context, u32 *sid) {
    table->calls++;
    if (table->stale_once) { table->stale_once = false; return -ESTALE; }
    if (table->fail) return table->fail;
    assert(context->value == (table == selinux_state.policy->sidtab ? 22 : 11));
    table->entry.context = *context; *sid = table->sid; return 0;
}
static struct sidtab_entry *sidtab_search_entry(struct sidtab *table, u32 sid) {
    return sid == table->sid ? &table->entry : NULL;
}
static int sidtab_entry_to_string(struct policydb *policy, struct sidtab *table,
    struct sidtab_entry *entry, char **text, u32 *size) {
    assert(policy->marker == 11 && entry == &table->entry);
    assert(entry->context.value == 11);
    *size = 6;
    if (text) { *text = malloc(*size); assert(*text); memcpy(*text, "saved", *size); }
    return 0;
}
static struct context *sidtab_search(struct sidtab *table, u32 sid) {
    struct sidtab_entry *entry = sidtab_search_entry(table, sid);
    return entry ? &entry->context : NULL;
}
static bool ebitmap_get_bit(int *map, u32 bit) { assert(bit == 7); return *map != 0; }
static void context_struct_compute_av(struct policydb *policy, struct context *source,
    struct context *target, u16 tclass, struct av_decision *decision, struct extended_perms *extra) {
    assert(policy->marker == 11 && source->value == 11 && target->value == 11);
    assert(tclass == 6 && extra == NULL); compute_calls++; decision->allowed = 0x1234;
}
'''

CONSUMER = r'''
static void dynamic_compute(struct policydb *policy, struct context *source,
    struct context *target, u16 tclass, struct av_decision *decision, struct extended_perms *extra) {
    assert(policy->marker == 11 && source->value == 11 && target->value == 11);
    assert(tclass == 6 && extra == NULL); compute_calls++; decision->allowed = 0x5678;
}
int main(void) {
    struct sidtab backup_table = { .sid = 41 }, live_table = { .sid = 99 };
    struct selinux_policy backup = { .policydb = { .marker = 11 }, .sidtab = &backup_table,
                                    .latest_granting = 123 };
    struct selinux_policy live = { .policydb = { .marker = 22 }, .sidtab = &live_table };
    u32 sid = 77, original_sid = 77; int original_rc = 77;
    selinux_state.policy = &live;
    assert(test_internal_context_to_sid(&backup, "", 0, &sid, 7, 0,
                                               &original_sid, &original_rc) == -EINVAL);
    assert(parse_calls == 0 && rcu_depth == 0);
    allocation_fail = 1;
    assert(test_internal_context_to_sid(&backup, "ctx", 3, &sid, 7, 0,
                                               &original_sid, &original_rc) == -ENOMEM);
    assert(parse_calls == 0 && rcu_depth == 0);
    allocation_fail = 0;
    assert(test_internal_context_to_sid(&backup, "ctx", 3, &sid, 7, 0,
                                               &original_sid, &original_rc) == 0);
    assert(sid == 41 && original_sid == 99 && original_rc == 0);
    assert(parse_calls == 1 && destroys == 2 && rcu_depth == 0);
    live_table.stale_once = true;
    assert(test_internal_context_to_sid(&backup, "ctx", 3, &sid, 7, 0,
                                               &original_sid, &original_rc) == 0);
    assert(parse_calls == 3 && rcu_depth == 0 && original_sid == 99);
    original_error = -EINVAL;
    int previous_live_calls = live_table.calls;
    assert(test_internal_context_to_sid(&backup, "ctx", 3, &sid, 7, 0,
                                               &original_sid, &original_rc) == 0);
    assert(sid == 41 && original_sid == SECSID_NULL && original_rc == -EINVAL);
    assert(live_table.calls == previous_live_calls && rcu_depth == 0);
    original_error = 0; backup_table.fail = -ENOMEM;
    assert(test_internal_context_to_sid(&backup, "ctx", 3, &sid, 7, 0,
                                               &original_sid, &original_rc) == -ENOMEM);
    assert(original_rc == -ENOMEM && rcu_depth == 0);
    backup_table.fail = 0; parse_error = -EINVAL;
    assert(test_internal_context_to_sid(&backup, "ctx", 3, &sid, 7, 0,
                                               &original_sid, &original_rc) == -EINVAL);
    assert(rcu_depth == 0);
    parse_error = 0;
    assert(test_internal_context_to_sid(&backup, "ctx", 3, &sid, 7, 0, NULL, NULL) == 0);
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6,6,0)
    /* Exact six-argument call used by the official common SELinux hooks. */
    assert(security_context_to_sid_with_policy(&backup, "ctx", 3, &sid, 7, 0) == 0);
#else
    assert(security_context_to_sid_with_policy(&backup, "ctx", 3, &sid, 7, 0, NULL, NULL) == 0);
#endif
    assert(sid == 41 && rcu_depth == 0);
    char *text = NULL; u32 size = 0;
    assert(security_sid_to_context_with_policy(&backup, 41, &text, &size) == 0);
    assert(size == 6 && !strcmp(text, "saved")); free(text);
    text = (char *)(uintptr_t)1; size = 99;
    assert(security_sid_to_context_with_policy(&backup, 1000, &text, &size) == -EINVAL);
    assert(text == NULL && size == 0);
    assert(security_sid_to_context_with_policy(&backup, 41, NULL, &size) == 0 && size == 6);
    struct av_decision decision;
    backup.policydb.permissive_map = 1;
    security_compute_av_user_with_policy(&backup, 41, 41, 6, &decision);
    assert(decision.allowed == 0x1234 && decision.seqno == 123);
    assert(decision.flags == AVD_FLAGS_PERMISSIVE && compute_calls == 1);
    context_struct_compute_av_fn = dynamic_compute;
    security_compute_av_user_with_policy(&backup, 41, 41, 6, &decision);
    assert(decision.allowed == 0x5678 && compute_calls == 2);
    security_compute_av_user_with_policy(&backup, 1000, 41, 6, &decision);
    assert(decision.allowed == 0 && compute_calls == 2);
    security_compute_av_user_with_policy(&backup, 41, 1000, 6, &decision);
    assert(decision.allowed == 0 && compute_calls == 2);
    backup.policydb.allow_unknown = false;
    security_compute_av_user_with_policy(&backup, 41, 41, 0, &decision);
    assert(decision.allowed == 0 && compute_calls == 2);
    backup.policydb.allow_unknown = true;
    security_compute_av_user_with_policy(&backup, 41, 41, 0, &decision);
    assert(decision.allowed == 0xffffffff && compute_calls == 2);
    return 0;
}
'''


def declarations(hide):
    return '\n'.join(re.search(r'(?m)^(?:static\s+)?(?:int|void)(?:\s+__nocfi)?\s+'
                              + name + r'\s*\([^;{}]*\);', hide)[0] for name in HELPERS)


def definition(hide, name):
    body = function_source(hide, name)
    position = hide.index(body)
    return hide[hide.rfind('\n', 0, position) + 1:position] + body


@unittest.skipUnless(shutil.which('git'), 'Git is required for the reviewed patch replay')
class KowPolicyLinkageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='kow-policy-link-', dir=ROOT / 'tests')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.workspace = Path(cls.temporary.name)
        patch = ROOT / 'tools/patches/kow-susfs23.patch'
        metadata = patch.with_name('kow-susfs23-base')
        inventory = json.loads((metadata / 'objects.json').read_text(encoding='utf-8'))
        raw_patch = patch.read_bytes()
        if hashlib.sha256(raw_patch).hexdigest() != inventory['patch_sha256']:
            raise AssertionError('The reviewed Kow patch digest differs')
        relative = 'kernel/feature/selinux_hide.c'
        section = re.search(r'(?ms)^diff --git a/' + re.escape(relative) + r' b/'
                            + re.escape(relative) + r'\n.*?(?=^diff --git |\Z)', raw_patch.decode())
        identity = re.search(r'^index ([0-9a-f]{40})\.\.', section[0], re.M)[1]
        digest = inventory['objects'][identity]
        data = (metadata / (digest + '.source')).read_bytes()
        if (hashlib.sha256(data).hexdigest() != digest
                or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != identity):
            raise AssertionError('The reviewed Kow native preimage differs')
        target = cls.workspace / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
        for command in (['init', '--quiet'], ['-c', 'core.autocrlf=false', 'apply', '--include=' + relative, str(patch)]):
            result = subprocess.run(['git', '-C', str(cls.workspace), *command], capture_output=True, timeout=30)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
        cls.hide = target.read_text(encoding='utf-8')
        (cls.workspace / 'model.h').write_text(MODEL, encoding='utf-8', newline='\n')
        consumers = json.loads((FIXTURE / 'provenance.json').read_text(encoding='utf-8'))
        cls.abi = {}
        for record in consumers['consumers']:
            data = (FIXTURE / record['fixture']).read_bytes()
            if hashlib.sha256(data).hexdigest() != record['fixture_sha256']:
                raise AssertionError('The pinned official SUSFS consumer declaration differs')
            cls.abi[record['kernel']] = data.decode('utf-8')

    def normalized(self, hide, version, label):
        # Expand the actual production version branches before extracting C
        # definitions. In particular, the modern 6-argument public wrapper and
        # private 8-argument Kow helper must not be conflated with the legacy API.
        source = self.workspace / (label + '-preprocess.c')
        source.write_text('#include "model.h"\n' + re.sub(r'(?m)^#\s*include[^\n]*\n', '', hide),
                          encoding='utf-8', newline='\n')
        result = subprocess.run([*COMPILER, '-E', '-P', '-DCONFIG_KSU_SUSFS=1',
                                 '-DLINUX_VERSION_CODE=' + str((version[0] << 16) | (version[1] << 8)),
                                 str(source)], capture_output=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.decode('utf-8')

    def build(self, hide, version, label, *, normalized=False, enforce_abi=True, provider_may_fail=False):
        hide = hide if normalized else self.normalized(hide, version, label)
        modern = version >= (6, 6)
        # The shared ABI comes from the *consumer's* pinned official patch, not
        # from the provider being tested. Include it in the provider as well so
        # an externally visible 8-argument function cannot pass a plain C link
        # against real 6-argument callers while corrupting their stack at runtime.
        public = self.abi['.'.join(map(str, version))] if modern else re.sub(r'(?m)^static\s+', '', declarations(hide))
        header = self.workspace / (label + '-abi.h')
        header.write_text(public, encoding='utf-8', newline='\n')
        abi_include = '#include "' + header.name + '"\n'
        internal = PRIVATE_CONTEXT if re.search(r'\b' + PRIVATE_CONTEXT + r'\s*\(', hide) else HELPERS[0]
        private_declaration = (re.search(r'(?m)^static int ' + PRIVATE_CONTEXT + r'\s*\([^;{}]*\);', hide)[0] + '\n'
                               if internal == PRIVATE_CONTEXT else '')
        private_definition = definition(hide, PRIVATE_CONTEXT) + '\n' if internal == PRIVATE_CONTEXT else ''
        bridge_declaration = ('int test_internal_context_to_sid(struct selinux_policy *policy, const char *scontext, '
                              'u32 scontext_len, u32 *sid, u32 def_sid, gfp_t gfp_flags, '
                              'u32 *orig_sid_p, int *orig_rc_p)')
        bridge = (bridge_declaration + ' { return ' + internal
                  + '(policy, scontext, scontext_len, sid, def_sid, gfp_flags, orig_sid_p, orig_rc_p); }\n')
        provider = self.workspace / (label + '.c')
        provider.write_text('#include "model.h"\n' + (abi_include if enforce_abi else '')
                            + declarations(hide) + private_declaration + LEAVES + '\n'
                            + definition(hide, 'avd_init') + '\n'
                            + private_definition + '\n'.join(definition(hide, name) for name in HELPERS)
                            + '\n' + bridge,
                            encoding='utf-8', newline='\n')
        consumer = self.workspace / (label + '-consumer.c')
        consumer.write_text('#include "model.h"\n' + abi_include + bridge_declaration + ';\n' + CONSUMER,
                            encoding='utf-8', newline='\n')
        suffix = '.exe' if os.name == 'nt' else ''
        output = self.workspace / (label + suffix)
        flags = ['-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
                 '-DLINUX_VERSION_CODE=' + str((version[0] << 16) | (version[1] << 8))]
        # Compile independently: an accidental static definition must get as far
        # as the linker, where the common/hooks.c-style external caller fails.
        objects = []
        for source in (provider, consumer):
            target = source.with_name(label + '-' + source.stem + '.o')
            result = subprocess.run([*COMPILER, *flags, '-c', str(source), '-o', str(target)],
                                    capture_output=True, timeout=120)
            if source == provider and provider_may_fail and result.returncode:
                return result, output
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            objects.append(str(target))
        linked = subprocess.run([*COMPILER, *objects, '-o', str(output)], capture_output=True, timeout=120)
        return linked, output

    @unittest.skipUnless(COMPILER, 'A native C compiler is required for the separate-TU link test')
    def test_real_policy_helpers_link_and_preserve_behavior_on_all_supported_api_branches(self):
        for version in ((5, 10), (5, 15), (6, 1), (6, 6), (6, 12)):
            with self.subTest(kernel=version):
                result, output = self.build(self.hide, version, 'policy-' + '-'.join(map(str, version)))
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                ran = subprocess.run([str(output)], capture_output=True, timeout=10)
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)

    @unittest.skipUnless(COMPILER, 'A native C compiler is required for the linkage regression')
    def test_restoring_static_to_any_helper_reproduces_the_native_link_failure(self):
        for name in HELPERS:
            with self.subTest(helper=name):
                broken, count = re.subn(r'(?m)^((?:int|void)(?:\s+__nocfi)?\s+' + name + r'\s*\()',
                                        r'static \1', self.hide)
                self.assertGreaterEqual(count, 2, 'The real declaration and definition must be changed')
                linked, _ = self.build(broken, (6, 6), 'missing-' + name, enforce_abi=False)
                self.assertNotEqual(0, linked.returncode)
                self.assertIn(name.encode(), linked.stdout + linked.stderr)

    @unittest.skipUnless(COMPILER, 'A native C compiler is required for the consumer ABI check')
    def test_modern_consumers_reject_an_external_eight_argument_context_helper(self):
        for version in ((6, 6), (6, 12)):
            with self.subTest(kernel=version):
                expanded = self.normalized(self.hide, version, 'old-abi-' + str(version[1]))
                # Reproduce the earlier tempting but unsafe fix: export the
                # original 8-argument Kow implementation directly, with no
                # wrapper for the real 6-argument SUSFS consumer.
                public = definition(expanded, HELPERS[0])
                expanded = expanded.replace(public, '', 1)
                expanded = re.sub(r'(?m)^int ' + HELPERS[0] + r'\s*\([^;{}]*\);\n', '', expanded)
                expanded = expanded.replace(PRIVATE_CONTEXT, HELPERS[0])
                expanded = re.sub(r'(?m)^static int (' + HELPERS[0] + r'\s*\()', r'int \1', expanded)
                result, _ = self.build(expanded, version, 'bad-abi-' + str(version[1]),
                                       normalized=True, provider_may_fail=True)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(HELPERS[0].encode(), result.stdout + result.stderr)
                self.assertIn(b'conflicting types', result.stdout + result.stderr)

    def test_build_gate_rejects_private_policy_helpers_before_kernel_compilation(self):
        from verify_kow_susfs import verify_policy_helpers
        verify_policy_helpers(self.hide)
        for name in HELPERS:
            with self.subTest(helper=name):
                broken, count = re.subn(r'(?m)^((?:int|void)(?:\s+__nocfi)?\s+' + name + r'\s*\()',
                                        r'static \1', self.hide)
                self.assertGreaterEqual(count, 2)
                with self.assertRaises(ValueError):
                    verify_policy_helpers(broken)


if __name__ == '__main__':
    unittest.main()
