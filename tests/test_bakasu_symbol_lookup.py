import contextlib
import copy
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import ensure_bakasu_symbol_lookup as lookup
import restore_bakasu_kpm as port

FIXTURE = ROOT / 'tests/fixtures/bakasu-symbol-lookup'
COMPILER = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class BakaSUSymbolLookupTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / f'.bakasu-lookup-{uuid.uuid4().hex}'
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.ksu = self.root / 'KernelSU/kernel'
        for target, fixture in (('infra/symbol_resolver.c', 'symbol_resolver.c'),
                                ('infra/symbol_resolver.h', 'symbol_resolver.h'),
                                ('supercall/dispatch.c', 'dispatch.c'), ('Kbuild', 'Kbuild')):
            path = self.ksu / target
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIXTURE / fixture, path)
        self.repository = self.root / 'KernelSU'
        self.git('init', '-q')
        for key, value in (('core.autocrlf', 'false'), ('user.name', 'Tests'), ('user.email', 'tests@example.invalid'), ('commit.gpgsign', 'false')):
            self.git('config', key, value)
        self.commit()

    def git(self, *arguments):
        result = subprocess.run(['git', '-C', str(self.repository), *arguments], capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout.strip()

    def cleanup(self):
        target = self.root.resolve()
        if target.parent != (ROOT / 'tests').resolve() or not target.name.startswith('.bakasu-lookup-'):
            raise ValueError('Unexpected test cleanup target')
        def writable_delete(function, path, _error):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)
        shutil.rmtree(target, onerror=writable_delete)

    def commit(self):
        self.git('add', '--all')
        self.git('commit', '-qm', 'Official native fixture')
        self.reference = self.git('rev-parse', 'HEAD')

    def state(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def replace(self, relative, old, new):
        path = self.ksu / relative
        text = path.read_text(encoding='utf-8')
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding='utf-8', newline='\n')

    def apply_port_fixture(self):
        """Exercise real Git replay/receipts with a small locked caller port.

        The complete production 26-file port is separately tested against
        official Git sources. This fixture isolates the caller guard without
        embedding another copy of the manager and KPM implementation.
        """
        self.git('remote', 'add', 'origin', 'https://github.com/Baka-SU/BakaSU.git')
        before = {name: (self.repository / name).read_bytes()
                  for name in ('kernel/Kbuild', 'kernel/supercall/dispatch.c')}
        after = dict(before)
        after['kernel/Kbuild'] += b'\nifeq ($(CONFIG_KPM),y)\nkernelsu-objs += kpm/kpm.o\nendif\n'
        after['kernel/supercall/dispatch.c'] = (b'#include "kpm/kpm.h"\n'
            + before['kernel/supercall/dispatch.c'] + b'\nstatic const struct ksu_ioctl_cmd_map fixture_kpm_handler = {\n'
            b'    .cmd = KSU_IOCTL_KPM,\n    .name = "KPM",\n'
            b'    .handler = do_kpm,\n    .perm_check = manager_or_root\n};\n')
        patch = ''.join('diff --git a/' + name + ' b/' + name + '\n'
            + ''.join(difflib.unified_diff(before[name].decode().splitlines(keepends=True),
                data.decode().splitlines(keepends=True), fromfile='a/' + name, tofile='b/' + name))
            for name, data in after.items()).encode()
        manifest = {'schema_version': 1, 'repository': port.REPOSITORY,
            'audited_ref': port.AUDITED_REF, 'api': dict(port.API),
            'patch': {'filename': 'bakasu-kpm.patch', 'size': len(patch), 'sha256': port.digest(patch)},
            'files': [{'path': name, 'kind': 'update', 'before_sha256': port.digest(before[name]),
                       'after_sha256': port.digest(data), 'after_size': len(data)} for name, data in after.items()]}
        lock_root = self.root / 'port-lock'
        (lock_root / 'config').mkdir(parents=True)
        (lock_root / 'tools/patches').mkdir(parents=True)
        (lock_root / 'config/bakasu-kpm-port.json').write_text(json.dumps(manifest), encoding='utf-8')
        (lock_root / 'tools/patches/bakasu-kpm.patch').write_bytes(patch)
        override = mock.patch.object(port, 'ROOT', lock_root)
        override.start()
        self.addCleanup(override.stop)
        receipt = port.restore_bakasu_kpm(self.repository, self.reference)
        return before, after, receipt

    def test_exact_reviewed_kpm_caller_is_accepted_without_changing_sources(self):
        self.apply_port_fixture()
        before = self.state()
        self.assertTrue(lookup.validate_native_symbol_lookup(self.root, self.reference))
        self.assertTrue(lookup.validate_native_symbol_lookup(self.repository, self.reference, Path('kernel')))
        self.assertEqual(before, self.state())

    def test_port_caller_accepts_later_susfs_and_manager_signature_configuration(self):
        self.apply_port_fixture()
        # GKI and Oplus run this guard after their different downstream
        # integration stages, when a complete port-only tree check is invalid.
        build = self.ksu / 'Kbuild'
        build.write_text(build.read_text() + '\nKSU_MANAGER_PACKAGE := org.bakasu.bakasu\n'
                         'KSU_EXPECTED_SIZE := 0x123\nKSU_VERSION := 35252\n', encoding='utf-8')
        (self.ksu / 'Kconfig').write_text('config KSU_SUSFS\n    bool "SUSFS"\n    default y\n', encoding='utf-8')
        before = self.state()
        self.assertTrue(lookup.validate_native_symbol_lookup(self.root, self.reference))
        self.assertEqual(before, self.state())

    def test_reviewed_port_caller_cannot_be_tampered_or_rolled_back(self):
        original, after, _receipt = self.apply_port_fixture()
        path = self.ksu / 'supercall/dispatch.c'
        for data in (after['kernel/supercall/dispatch.c'] + b'/* unreviewed extra change */\n',
                     original['kernel/supercall/dispatch.c'],
                     after['kernel/supercall/dispatch.c'].replace(b'.handler = do_kpm', b'.handler = NULL'),
                     after['kernel/supercall/dispatch.c'].replace(lookup.SYMBOL.encode(), b'unreviewed_lookup')):
            with self.subTest(digest=port.digest(data)):
                path.write_bytes(data)
                before = self.state()
                with self.assertRaisesRegex(ValueError, 'exact audited KPM port'):
                    lookup.validate_native_symbol_lookup(self.root, self.reference)
                self.assertEqual(before, self.state())

    def test_modified_dispatch_requires_an_authentic_port_receipt(self):
        self.apply_port_fixture()
        receipt = self.repository / port.RECEIPT
        receipt.unlink()
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'without an audited KPM port receipt'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)
        self.assertEqual(before, self.state())

    def test_port_receipt_binds_all_files_source_tree_patch_and_strict_types(self):
        _before, _after, original = self.apply_port_fixture()
        receipt = self.repository / port.RECEIPT
        variants = []
        for field, value in (('source_ref', 'f' * 40), ('source_tree', 'e' * 40), ('schema_version', True)):
            changed = copy.deepcopy(original)
            changed[field] = value
            variants.append(changed)
        changed = copy.deepcopy(original)
        changed['files']['kernel/Kbuild']['after_sha256'] = 'a' * 64
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed['patch']['sha256'] = 'b' * 64
        variants.append(changed)
        for value in variants:
            with self.subTest(receipt=value):
                receipt.write_text(json.dumps(value), encoding='utf-8')
                before = self.state()
                with self.assertRaisesRegex(ValueError, 'receipt differs'):
                    lookup.validate_native_symbol_lookup(self.root, self.reference)
                self.assertEqual(before, self.state())

    def test_malformed_and_nonregular_port_receipts_are_rejected(self):
        self.apply_port_fixture()
        receipt = self.repository / port.RECEIPT
        for raw in (b'[]', b'{"schema_version":1,"schema_version":1}', b'{"schema_version":NaN}'):
            with self.subTest(raw=raw):
                receipt.write_bytes(raw)
                with self.assertRaises(ValueError):
                    lookup.validate_native_symbol_lookup(self.root, self.reference)
        receipt.unlink()
        receipt.mkdir()
        with self.assertRaisesRegex(ValueError, 'bounded regular'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_port_receipt_symlink_is_rejected(self):
        self.apply_port_fixture()
        receipt = self.repository / port.RECEIPT
        target = self.root / 'receipt-copy.json'
        receipt.replace(target)
        try:
            receipt.symlink_to(target)
        except OSError:
            self.skipTest('File symlinks are not available')
        with self.assertRaisesRegex(ValueError, 'symlink or junction'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_audited_caller_never_allows_native_resolver_or_link_rule_changes(self):
        self.apply_port_fixture()
        for relative in ('infra/symbol_resolver.c', 'infra/symbol_resolver.h', 'Kbuild'):
            path = self.ksu / relative
            original = path.read_bytes()
            modified = (original.replace(b'kernelsu-objs += infra/symbol_resolver.o', b'# removed resolver object')
                        if relative == 'Kbuild' else original + b'/* unreviewed native change */\n')
            path.write_bytes(modified)
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                lookup.validate_native_symbol_lookup(self.root, self.reference)
            path.write_bytes(original)

    def test_audited_caller_requires_official_origin_and_fixed_patch_bytes(self):
        self.apply_port_fixture()
        self.git('remote', 'set-url', 'origin', 'https://github.com/example/unreviewed.git')
        with self.assertRaisesRegex(ValueError, 'official BakaSU repository'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)
        self.git('remote', 'set-url', 'origin', 'https://github.com/Baka-SU/BakaSU.git')
        patch = port.ROOT / 'tools/patches/bakasu-kpm.patch'
        patch.write_bytes(patch.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'fixed size/SHA256'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_exact_official_fixture_and_read_only_validation(self):
        provenance = json.loads((FIXTURE / 'provenance.json').read_text())
        for relative, details in provenance['files'].items():
            path = FIXTURE / Path(relative).name
            self.assertEqual(details['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        before = self.state()
        self.assertTrue(lookup.validate_native_symbol_lookup(self.root, self.reference))
        self.assertTrue(lookup.validate_native_symbol_lookup(self.root, self.reference, Path('KernelSU/kernel')))
        self.assertEqual(before, self.state())

    def test_legacy_whole_file_replacement_is_rejected(self):
        path = self.ksu / 'infra/symbol_resolver.c'
        path.write_text('/* ABK: Android 13 5.15.{41,74,78,94,104,119,123,137} BakaSU kallsyms lookup rewrite. */\n'
                        'void *ksu_lookup_symbol(const char *name) { return 0; }\n', encoding='utf-8')
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'differs from the selected official commit'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)
        self.assertEqual(before, self.state())

    def test_actual_link_failure_provider_removed_from_otherwise_native_source(self):
        path = self.ksu / 'infra/symbol_resolver.c'
        source = path.read_text()
        source = source.replace(lookup.native_function(source, lookup.SYMBOL), '', 1)
        path.write_text(source, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'differs from the selected official commit'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_static_conditional_and_stub_batch_providers_are_rejected(self):
        path = self.ksu / 'infra/symbol_resolver.c'
        original = path.read_text()
        function = lookup.native_function(original, lookup.SYMBOL)
        variants = ('static ' + function,
                    '#ifdef UNREVIEWED_CONFIG\n' + function + '\n#endif',
                    function[:function.index('{')] + '{}',
                    function[:function.index('{')] + '{ return 0; }')
        for replacement in variants:
            with self.subTest(replacement=replacement[:75]):
                path.write_text(original.replace(function, replacement, 1), encoding='utf-8')
                before = self.state()
                with self.assertRaises(ValueError):
                    lookup.validate_native_symbol_lookup(self.root, self.reference)
                self.assertEqual(before, self.state())

    def test_changed_header_signature_is_rejected(self):
        self.replace('infra/symbol_resolver.h', 'u32 count', 'u64 count')
        with self.assertRaisesRegex(ValueError, 'differs from the selected official commit'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_missing_provider_object_and_conditional_provider_are_rejected(self):
        path = self.ksu / 'Kbuild'
        original = path.read_text()
        rule = 'kernelsu-objs += infra/symbol_resolver.o'
        for replacement in ('', '# ' + rule, 'ifdef CONFIG_NEW\n' + rule + '\nendif', rule + '\n' + rule):
            with self.subTest(replacement=replacement):
                path.write_text(original.replace(rule, replacement, 1), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'official symbol resolver object rules'):
                    lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_old_native_api_without_batch_caller_remains_compatible(self):
        source = self.ksu / 'infra/symbol_resolver.c'
        text = source.read_text()
        source.write_text(text.replace(lookup.native_function(text, lookup.SYMBOL), '', 1), encoding='utf-8')
        header = self.ksu / 'infra/symbol_resolver.h'
        header.write_text('\n'.join(line for line in header.read_text().splitlines() if lookup.SYMBOL not in line), encoding='utf-8')
        (self.ksu / 'supercall/dispatch.c').write_text('int dispatch(void) { return 1; }\n', encoding='utf-8')
        self.commit()
        self.assertFalse(lookup.validate_native_symbol_lookup(self.root, self.reference))

    def test_missing_real_dispatch_caller_cannot_be_satisfied_by_comment(self):
        (self.ksu / 'supercall/dispatch.c').write_text('/* ksu_lookup_kernel_symbols(names, addrs, count); */\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'differs from the selected official commit'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_macro_alias_is_not_accepted_as_native_provider(self):
        path = self.ksu / 'infra/symbol_resolver.c'
        path.write_text(path.read_text() + '\n#define ksu_lookup_kernel_symbols other_provider\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'differs from the selected official commit'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_core_function_is_not_synthesized_or_stubbed(self):
        path = self.ksu / 'infra/symbol_resolver.c'
        text = path.read_text()
        function = lookup.native_function(text, 'ksu_init_symbol_resolver')
        path.write_text(text.replace(function, 'void ksu_init_symbol_resolver(void) {}'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'differs from the selected official commit'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_result_positions_are_checked(self):
        self.replace('infra/symbol_resolver.c', 'addrs[ctx->entries[i].idx] = ctx->entries[i].addr;', 'addrs[i] = ctx->entries[i].addr;')
        with self.assertRaisesRegex(ValueError, 'differs from the selected official commit'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_other_variant_is_rejected(self):
        self.replace('Kbuild', 'REPO_NAME := BakaSU', 'REPO_NAME := SukiSU')
        with self.assertRaisesRegex(ValueError, 'selected native BakaSU'):
            lookup.validate_native_symbol_lookup(self.root, self.reference)

    def test_future_official_parameter_and_algorithm_changes_are_accepted(self):
        for relative in ('infra/symbol_resolver.c', 'infra/symbol_resolver.h'):
            path = self.ksu / relative
            text = re.sub(r'\bnames\b', 'requested_names', path.read_text())
            path.write_text(text, encoding='utf-8')
        self.commit()
        self.assertTrue(lookup.validate_native_symbol_lookup(self.root, self.reference))
        # An upstream API rename is allowed when all compiled files come from
        # that newly selected immutable official commit.
        for relative in ('infra/symbol_resolver.c', 'infra/symbol_resolver.h', 'supercall/dispatch.c'):
            path = self.ksu / relative
            path.write_text(path.read_text().replace(lookup.SYMBOL, 'ksu_resolve_kernel_symbols'), encoding='utf-8')
        self.commit()
        self.assertFalse(lookup.validate_native_symbol_lookup(self.root, self.reference))

    def test_expected_full_commit_is_mandatory_and_wrong_head_fails(self):
        for reference in ('main', self.reference[:7], 'f' * 40):
            with self.subTest(reference=reference):
                with self.assertRaisesRegex(ValueError, 'immutable|Git HEAD'):
                    lookup.validate_native_symbol_lookup(self.root, reference)

    def test_certificate_version_kbuild_changes_are_allowed(self):
        path = self.ksu / 'Kbuild'
        path.write_text(path.read_text() + '\nKSU_MANAGER_PACKAGE := com.resukisu.resukisu\nKSU_VERSION := 35252\n', encoding='utf-8')
        self.assertTrue(lookup.validate_native_symbol_lookup(self.root, self.reference))

    def test_cli_success_failure_and_sources_untouched(self):
        before = self.state()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, lookup.main(['--kernel-root', str(self.root), '--expected-ref', self.reference]))
        self.assertEqual(before, self.state())
        self.replace('infra/symbol_resolver.h', 'u32 count', 'u64 count')
        before = self.state()
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(1, lookup.main(['--kernel-root', str(self.root), '--expected-ref', self.reference]))
        self.assertIn('differs from the selected official commit', error.getvalue())
        self.assertEqual(before, self.state())

    @unittest.skipUnless(COMPILER and os.name != 'nt', 'A native POSIX C compiler is required')
    def test_real_official_provider_compiles_links_and_preserves_batch_semantics_across_kernel_versions(self):
        # Keep every official C function and conditional byte-for-byte; model
        # only kernel headers/services. This catches the real deleted-provider
        # linker failure and exercises old fallback plus new indexed/walk paths.
        native = (FIXTURE / 'symbol_resolver.c').read_text(encoding='utf-8')
        native = re.sub(r'(?m)^#include[^\n]*', '', native)
        for version in ((5, 10, 236), (5, 15, 94), (6, 1, 75), (6, 6, 30)):
            with self.subTest(version=version):
                program = C_KERNEL_MODEL.replace('TEST_KERNEL_VERSION', ','.join(map(str, version))) + native + C_BEHAVIOR
                source = self.root / 'native-lookup.c'
                executable = self.root / 'native-lookup'
                source.write_text(program, encoding='utf-8')
                command = [COMPILER, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', '-Wno-unused-function',
                           '-Wno-int-conversion', str(source), '-o', str(executable)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                missing = program.replace(lookup.native_function(program, lookup.SYMBOL), '', 1)
                source.write_text(missing, encoding='utf-8')
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(lookup.SYMBOL, result.stderr)


C_KERNEL_MODEL = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#define KERNEL_VERSION(a,b,c) (((a)<<16)|((b)<<8)|(c))
#define LINUX_VERSION_CODE KERNEL_VERSION(TEST_KERNEL_VERSION)
#define __nocfi
#define __init
#define GFP_KERNEL 0
#define KSYM_NAME_LEN 128
#define KSYM_SYMBOL_LEN 256
#define likely(value) (value)
#define unlikely(value) (value)
#define pr_warn(...) ((void)0)
#define pr_info(...) ((void)0)
typedef uint32_t u32;
struct module { int unused; };
static void *current;
static int interrupted, allocation_fail, allocations, indexed_calls, walked_calls;
static void *kzalloc(size_t size, int flags) { if (allocation_fail == 1) return NULL; void *p=calloc(1,size); if (p) allocations++; return p; }
static void *vmalloc(size_t size) { if (allocation_fail == 2) return NULL; void *p=malloc(size); if (p) allocations++; return p; }
static void kfree(void *p) { if (p) allocations--; free(p); }
static void vfree(void *p) { kfree(p); }
static int fatal_signal_pending(void *task) { return interrupted; }
static void cond_resched(void) {}
static void sort(void *base,size_t n,size_t size,int (*cmp)(const void *,const void *),void *swap) { qsort(base,n,size,cmp); }
void *ksu_resolve_symbol_for_functable_hook(const char *symbol_name);
unsigned long find_kernel_symbol_exact(const char *symbol_name);
int ksu_lookup_kernel_symbols(const char *const *names,unsigned long *addrs,u32 count);
void ksu_init_symbol_resolver(void);
static const struct { const char *name; unsigned long addr; } symbols[] = {
    {"alpha", 1010}, {"beta", 2020}, {"variant.llvm.42", 3030}, {"alpha$duplicate", 4040},
};
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6,6,0)
static int kallsyms_on_each_symbol(int (*cb)(void *,const char *,unsigned long),void *data)
#else
static int kallsyms_on_each_symbol(int (*cb)(void *,const char *,struct module *,unsigned long),void *data)
#endif
{
    walked_calls++;
    for (size_t i=0;i<sizeof(symbols)/sizeof(symbols[0]);i++) {
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6,6,0)
        int ret=cb(data,symbols[i].name,symbols[i].addr);
#else
        int ret=cb(data,symbols[i].name,NULL,symbols[i].addr);
#endif
        if (ret) return ret;
    }
    return 0;
}
static int kallsyms_on_each_match_symbol(int (*cb)(void *,unsigned long),const char *name,void *data)
{
    indexed_calls++;
    for (size_t i=0;i<sizeof(symbols)/sizeof(symbols[0]);i++)
        if (!strcmp(name,symbols[i].name)) { int ret=cb(data,symbols[i].addr); if (ret) return ret; }
    return 0;
}
static unsigned long kallsyms_lookup_name(const char *name)
{
    if (!strcmp(name,"kallsyms_on_each_symbol")) return (unsigned long)kallsyms_on_each_symbol;
    if (!strcmp(name,"kallsyms_on_each_match_symbol")) return (unsigned long)kallsyms_on_each_match_symbol;
    if (!strcmp(name,"module_only")) return 9090;
    for (size_t i=0;i<sizeof(symbols)/sizeof(symbols[0]);i++)
        if (!strcmp(name,symbols[i].name)) return symbols[i].addr;
    return 0;
}
static const char *kallsyms_lookup(unsigned long addr,void *size,void *offset,char **module,char *name)
{ *module=addr==9090 ? "module" : NULL; return ""; }
'''

C_BEHAVIOR = r'''
int main(void)
{
    const char *names[]={"alpha","missing","beta","alpha",NULL,"variant"};
    unsigned long addrs[6]={99,99,99,99,99,99};
    ksu_init_symbol_resolver();
    assert(find_kernel_symbol_exact("module_only")==0);
    assert(ksu_lookup_kernel_symbols(names,addrs,6)==4);
    assert(addrs[0]==1010 && addrs[1]==0 && addrs[2]==2020 && addrs[3]==1010 && addrs[4]==0 && addrs[5]==3030);
    assert(walked_calls>0 && allocations==0);
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6,1,0)
    assert(indexed_calls>0);
#else
    assert(indexed_calls==0);
#endif
    allocation_fail=1;
    assert(ksu_lookup_kernel_symbols(names,addrs,6)==-ENOMEM && allocations==0);
    allocation_fail=2;
    assert(ksu_lookup_kernel_symbols(names,addrs,6)==-ENOMEM && allocations==0);
    allocation_fail=0;
    interrupted=1;
    assert(ksu_lookup_kernel_symbols(names,addrs,6)==-EINTR && allocations==0);
    interrupted=0;
#if LINUX_VERSION_CODE < KERNEL_VERSION(5,19,0)
    kallsyms_on_each_symbol_fn=NULL;
    const char *fallback[]={"alpha","beta","missing"};
    assert(ksu_lookup_kernel_symbols(fallback,addrs,3)==2);
    assert(addrs[0]==1010 && addrs[1]==2020 && addrs[2]==0 && allocations==0);
#endif
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
