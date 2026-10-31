"""Exact Git application, rollback and same-source guards for the KPM port."""
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import restore_bakasu_kpm as port


class BakaKpmPortTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT.parent / '.gki-upstream-cache' / ('bakasu-port-test-' + uuid.uuid4().hex)
        self.source = self.directory / 'source'
        self.source.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.command('init', '-q')
        self.command('remote', 'add', 'origin', 'https://github.com/Baka-SU/BakaSU.git')
        self.before = b'/* Synthetic native source context. */\nint before(void)\n{\n    return 0;\n}\n'
        self.after = self.before.replace(b'return 0;', b'return 1;')
        self.relative = 'kernel/port_fixture.c'
        self.write(self.relative, self.before)
        self.command('add', '.')
        self.reference = self.commit('frozen synthetic source')
        # Take a complete *real new file* from the production patch rather
        # than maintaining a second implementation or a dummy Kconfig.
        real, raw = port.load_port_manifest()
        section = raw.decode().split('diff --git a/kernel/kpm/bridge.h b/kernel/kpm/bridge.h\n')[1]
        section = section.split('\ndiff --git ', 1)[0]
        self.new_data = ''.join(line[1:] + '\n' for line in section.splitlines()
                                if line.startswith('+') and not line.startswith('+++')).encode()
        self.assertTrue(any(item['path'] == 'kernel/kpm/bridge.h' for item in real['files']))
        self.new_path = 'kernel/kpm/bridge.h'
        update = ('diff --git a/' + self.relative + ' b/' + self.relative + '\n'
            + ''.join(difflib.unified_diff(self.before.decode().splitlines(keepends=True),
                self.after.decode().splitlines(keepends=True), fromfile='a/' + self.relative,
                tofile='b/' + self.relative)))
        new = ('diff --git a/' + self.new_path + ' b/' + self.new_path + '\nnew file mode 100644\n'
            '--- /dev/null\n+++ b/' + self.new_path + '\n@@ -0,0 +1,'
            + str(len(self.new_data.splitlines())) + ' @@\n'
            + ''.join('+' + line for line in self.new_data.decode().splitlines(keepends=True)))
        self.patch = (update + new).encode()
        self.manifest = {'schema_version': 1, 'repository': port.REPOSITORY,
            'audited_ref': port.AUDITED_REF, 'api': dict(port.API),
            'patch': {'filename': 'bakasu-kpm.patch', 'size': len(self.patch), 'sha256': port.digest(self.patch)},
            'files': [{'path': self.relative, 'kind': 'update', 'before_sha256': port.digest(self.before),
                       'after_sha256': port.digest(self.after), 'after_size': len(self.after)},
                      {'path': self.new_path, 'kind': 'new', 'before_sha256': None,
                       'after_sha256': port.digest(self.new_data), 'after_size': len(self.new_data)}]}
        self.patch_file = self.directory / 'tools/patches/bakasu-kpm.patch'
        self.patch_file.parent.mkdir(parents=True)
        self.patch_file.write_bytes(self.patch)
        self.config = self.directory / 'config/bakasu-kpm-port.json'
        self.config.parent.mkdir()
        self.save_manifest()
        override = mock.patch.object(port, 'ROOT', self.directory)
        override.start()
        self.addCleanup(override.stop)

    def cleanup(self):
        if (self.directory.resolve().parent != (ROOT.parent / '.gki-upstream-cache').resolve()
                or not self.directory.name.startswith('bakasu-port-test-')):
            raise ValueError('Invalid synthetic test cleanup path')
        def writable(function, name, error):
            if not isinstance(error, PermissionError):
                raise error
            os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
            function(name)
        shutil.rmtree(self.directory, onexc=writable)

    def command(self, *arguments):
        result = subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(self.source), *arguments],
            capture_output=True, check=True, timeout=30)
        return result.stdout.decode().strip()

    def commit(self, message):
        self.command('-c', 'user.name=Port test', '-c', 'user.email=port-test@example.invalid',
                     '-c', 'commit.gpgsign=false', 'commit', '-qm', message)
        return self.command('rev-parse', 'HEAD')

    def write(self, name, data):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def save_manifest(self):
        self.config.write_text(json.dumps(self.manifest), encoding='utf-8')

    def restore(self):
        return port.restore_bakasu_kpm(self.source, self.reference)

    def test_exact_application_and_idempotent_verification_keep_original_git_identity(self):
        original_tree = self.command('rev-parse', 'HEAD^{tree}')
        result = self.restore()
        self.assertEqual((self.source / self.relative).read_bytes(), self.after)
        self.assertEqual((self.source / self.new_path).read_bytes(), self.new_data)
        self.assertEqual(self.reference, self.command('rev-parse', 'HEAD'))
        self.assertEqual(original_tree, self.command('rev-parse', 'HEAD^{tree}'))
        self.assertEqual('', self.command('diff', '--cached', '--name-only'))
        self.assertEqual(result, self.restore())
        self.assertEqual(result, port.verify_bakasu_kpm_port(self.source, self.reference))
        self.assertEqual(result, json.loads((self.source / port.RECEIPT).read_text()))
        self.assertEqual(result['files'][self.relative]['after_sha256'], port.digest(self.after))
        self.assertRegex(result['port_sha256'], r'^[0-9a-f]{64}$')

    def test_unrelated_upstream_commit_is_preserved_and_changes_actual_source_binding(self):
        self.write(self.relative, self.before + b'/* Unrelated official upstream addition. */\n')
        self.command('add', self.relative)
        self.reference = self.commit('unrelated upstream context')
        result = self.restore()
        expected = self.after + b'/* Unrelated official upstream addition. */\n'
        self.assertEqual(expected, (self.source / self.relative).read_bytes())
        self.assertEqual(result['files'][self.relative]['after_sha256'], port.digest(expected))
        self.assertNotEqual(result['files'][self.relative]['after_sha256'], self.manifest['files'][0]['after_sha256'])
        self.assertEqual(result, port.verify_bakasu_kpm_port(self.source, self.reference))

    def three_way_context(self, *, conflict=False):
        def blob(data):
            return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        marker = ('diff --git a/' + self.relative + ' b/' + self.relative + '\n').encode()
        self.patch = self.patch.replace(marker, marker +
            f'index {blob(self.before)}..{blob(self.after)} 100644\n'.encode(), 1)
        self.patch_file.write_bytes(self.patch)
        self.manifest['patch'].update(size=len(self.patch), sha256=port.digest(self.patch))
        self.save_manifest()
        changed = (self.before.replace(b'return 0;', b'return 5;') if conflict else
                   self.before.replace(b'Synthetic native source context.', b'Updated independent upstream context.'))
        self.write(self.relative, changed)
        self.command('add', self.relative)
        self.reference = self.commit('upstream source context changed near the KPM port')
        return changed

    def test_changed_hunk_context_is_merged_reversibly_without_pinning_an_old_version(self):
        upstream = self.three_way_context()
        result = self.restore()
        self.assertEqual(upstream.replace(b'return 0;', b'return 1;'), (self.source / self.relative).read_bytes())
        self.assertEqual(result, self.restore())
        self.assertEqual(result, port.verify_bakasu_kpm_port(self.source, self.reference))
        self.assertEqual('', self.command('diff', '--cached', '--name-only'))

    def test_real_three_way_conflict_never_changes_the_selected_source(self):
        upstream = self.three_way_context(conflict=True)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.restore()
        self.assertEqual(upstream, (self.source / self.relative).read_bytes())
        self.assertFalse((self.source / self.new_path).exists())
        self.assertFalse((self.source / port.RECEIPT).exists())

    def test_rewritten_upstream_history_uses_locked_base_without_remote_old_objects(self):
        upstream = self.three_way_context()
        base_blob = hashlib.sha1(b'blob ' + str(len(self.before)).encode() + b'\0' + self.before).hexdigest()
        self.command('checkout', '--orphan', 'rewritten-official-history')
        self.reference = self.commit('new official root retains changed native source')
        bundle = self.directory / 'current-history.bundle'
        self.command('bundle', 'create', str(bundle), 'HEAD')
        imported = self.directory / 'bundled-source'
        subprocess.run(['git', 'clone', '--quiet', '--config', 'core.autocrlf=false',
                        str(bundle), str(imported)], check=True, capture_output=True)
        self.source = imported
        self.command('checkout', '--quiet', '--detach', self.reference)
        self.command('remote', 'set-url', 'origin', 'https://github.com/Baka-SU/BakaSU.git')
        missing = subprocess.run(['git', '-C', str(imported), 'cat-file', '-e', base_blob], capture_output=True)
        self.assertNotEqual(0, missing.returncode)
        base_path = self.directory / 'tools/patches/bakasu-kpm-base' / (port.digest(self.before) + '.source')
        base_path.parent.mkdir(parents=True)
        base_path.write_bytes(self.before)
        result = self.restore()
        self.assertEqual(upstream.replace(b'return 0;', b'return 1;'), (imported / self.relative).read_bytes())
        self.assertEqual(result, port.verify_bakasu_kpm_port(imported, self.reference))
        self.assertEqual(self.reference, self.command('rev-parse', 'HEAD'))

    def test_executable_official_text_blob_is_preserved_through_apply_and_verify(self):
        source = self.source / self.relative
        source.chmod(0o755)
        self.command('update-index', '--chmod=+x', self.relative)
        self.reference = self.commit('official source with executable text mode')
        native_tree = self.command('rev-parse', 'HEAD^{tree}')
        self.assertTrue(self.command('ls-tree', 'HEAD', '--', self.relative).startswith('100755 blob '))
        self.assertEqual(self.before, port.head_file(self.source, self.reference, self.relative))
        # Match the production Rust CLI patch: its index header must carry
        # the executable mode from the real official Git tree.
        self.patch = self.patch.replace(('diff --git a/' + self.relative + ' b/' + self.relative + '\n').encode(),
            ('diff --git a/' + self.relative + ' b/' + self.relative + '\nindex aaaaaaa..bbbbbbb 100755\n').encode(), 1)
        self.patch_file.write_bytes(self.patch)
        self.manifest['patch'].update(size=len(self.patch), sha256=port.digest(self.patch))
        self.save_manifest()
        result = self.restore()
        self.assertEqual(self.after, source.read_bytes())
        self.assertEqual(result, self.restore())
        self.assertEqual(result, port.verify_bakasu_kpm_port(self.source, self.reference))
        self.assertEqual(native_tree, self.command('rev-parse', 'HEAD^{tree}'))
        self.assertEqual('', self.command('diff', '--cached', '--name-only'))
        self.assertEqual('', self.command('diff', '--summary'))
        if os.name != 'nt':
            self.assertEqual(0o755, stat.S_IMODE(source.stat().st_mode))

    def test_production_patch_retains_the_official_rust_cli_executable_mode(self):
        # setUp redirects ROOT to the synthetic transaction fixture. Read
        # production locks explicitly for this native metadata regression.
        with mock.patch.object(port, 'ROOT', ROOT):
            manifest, patch = port.load_port_manifest()
        header = ('diff --git a/userspace/ksud/src/android/cli.rs b/userspace/ksud/src/android/cli.rs\n'
                  'index 545cbed..c178e93 100755\n').encode()
        self.assertIn(header, patch)
        self.assertEqual(len(patch), manifest['patch']['size'])

    def test_head_blob_rejects_links_submodules_wrong_paths_and_binary_text(self):
        original = port.git
        for mode in (b'120000', b'160000', b'040000', b'100664'):
            record = mode + b' blob ' + b'a' * 40 + b'\t' + self.relative.encode() + b'\0'
            with self.subTest(mode=mode), mock.patch.object(port, 'git', return_value=record), \
                    self.assertRaisesRegex(ValueError, 'regular text Git blob'):
                port.head_file(self.source, self.reference, self.relative)
        record = b'100755 blob ' + b'a' * 40 + b'\tother/file.c\0'
        with mock.patch.object(port, 'git', return_value=record), self.assertRaises(ValueError):
            port.head_file(self.source, self.reference, self.relative)
        def binary(source, *args, **kwargs):
            if args[0] == 'show':
                return b'not\0text'
            return original(source, *args, **kwargs)
        with mock.patch.object(port, 'git', side_effect=binary), self.assertRaisesRegex(ValueError, 'bounded frozen'):
            port.head_file(self.source, self.reference, self.relative)

    def test_mode_only_change_on_an_already_ported_file_cannot_hide_in_content_changes(self):
        self.restore()
        original = port.git
        def changed_mode(source, *args, **kwargs):
            if args[:4] == ('diff', '--no-ext-diff', '--no-renames', '--summary'):
                return b' mode change 100644 => 100755 kernel/port_fixture.c\n'
            return original(source, *args, **kwargs)
        with mock.patch.object(port, 'git', side_effect=changed_mode), \
                self.assertRaisesRegex(ValueError, 'original tracked source file modes'):
            port.verify_bakasu_kpm_port(self.source, self.reference)

    def test_conflicting_upstream_context_and_partial_application_fail_without_further_edits(self):
        self.write(self.relative, self.before.replace(b'return 0;', b'return 99;'))
        self.command('add', self.relative)
        self.reference = self.commit('incompatible upstream')
        with self.assertRaises(ValueError):
            self.restore()
        self.assertFalse((self.source / self.new_path).exists())
        self.assertIn(b'return 99;', (self.source / self.relative).read_bytes())
        self.write(self.relative, self.before)
        self.command('add', self.relative)
        self.reference = self.commit('clean baseline')
        self.write(self.relative, self.after)
        with self.assertRaisesRegex(ValueError, 'clean, unmodified'):
            self.restore()
        self.assertEqual(self.after, (self.source / self.relative).read_bytes())
        self.assertFalse((self.source / self.new_path).exists())

    def test_wrong_ref_or_origin_is_rejected_without_applying(self):
        with self.assertRaisesRegex(ValueError, 'HEAD differs'):
            port.restore_bakasu_kpm(self.source, '0' * 40)
        self.command('remote', 'set-url', 'origin', 'https://github.com/unreviewed/BakaSU.git')
        with self.assertRaisesRegex(ValueError, 'official BakaSU'):
            self.restore()
        self.assertEqual(self.before, (self.source / self.relative).read_bytes())
        self.command('remote', 'set-url', 'origin', 'https://github.com/Baka-SU/BakaSU')
        self.restore()

    def test_patch_lock_and_inventory_tampering_are_rejected(self):
        self.patch_file.write_bytes(self.patch + b'changed')
        with self.assertRaisesRegex(ValueError, 'size/SHA256'):
            self.restore()
        self.patch_file.write_bytes(self.patch)
        cases = [('api', {**port.API, 'feature_id': 3}), ('api', {**port.API, 'bridge_abi': True}),
                 ('audited_ref', '0' * 40), ('repository', 'unreviewed/BakaSU')]
        for field, value in cases:
            manifest = copy.deepcopy(self.manifest)
            manifest[field] = value
            self.config.write_text(json.dumps(manifest))
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.restore()
        self.assertEqual(self.before, (self.source / self.relative).read_bytes())

    def test_paths_modes_binary_and_rename_instructions_cannot_escape_allowlist(self):
        for value in ('../outside.c', '.git/config', 'kernel/../outside.c', 'kernel/README.md',
                      'kernel/AGENTS.md', '/kernel/path.c', 'kernel/link.exe', 'kernel\\path.c'):
            with self.subTest(path=value), self.assertRaises(ValueError):
                port.path_name(value)
        for patch in (self.patch.replace(b'new file mode 100644', b'new file mode 120000'),
                      self.patch.replace(b'new file mode 100644', b'old mode 100644\nnew mode 100755'),
                      self.patch.replace(b'new file mode 100644', b'rename from other.c\nrename to target.c'),
                      self.patch + b'\0', self.patch.replace(b'new file mode 100644', b'GIT binary patch')):
            with self.subTest(patch=patch[:80]), self.assertRaises(ValueError):
                port.validate_patch(patch, self.manifest['files'])

    def test_extra_kernel_or_assets_changes_are_not_hidden_by_verification_scope_or_gitignore(self):
        result = self.restore()
        self.write('manager/unrelated.kt', b'// unrelated manager source\n')
        self.assertEqual(result, port.verify_bakasu_kpm_port(self.source, self.reference,
            scope=('kernel', 'uapi', 'userspace/ksud/src/assets.rs')))
        with self.assertRaisesRegex(ValueError, 'Additional source changes'):
            port.verify_bakasu_kpm_port(self.source, self.reference)
        self.write('kernel/.gitignore', b'hidden.c\n')
        self.write('kernel/hidden.c', b'// additional driver hidden by .gitignore\n')
        with self.assertRaisesRegex(ValueError, 'Additional source changes'):
            port.verify_bakasu_kpm_port(self.source, self.reference, scope=('kernel', 'uapi'))

    def test_modified_port_file_receipt_or_staged_changes_are_rejected(self):
        result = self.restore()
        path = self.source / self.new_path
        path.write_bytes(self.new_data + b'changed')
        with self.assertRaisesRegex(ValueError, 'exact audited port'):
            port.verify_bakasu_kpm_port(self.source, self.reference)
        path.write_bytes(self.new_data)
        receipt = self.source / port.RECEIPT
        receipt.write_text(json.dumps({**result, 'port_sha256': '0' * 64}))
        with self.assertRaisesRegex(ValueError, 'receipt differs'):
            port.verify_bakasu_kpm_port(self.source, self.reference)
        receipt.write_text(json.dumps(result))
        self.command('add', self.relative)
        with self.assertRaisesRegex(ValueError, 'staged'):
            port.verify_bakasu_kpm_port(self.source, self.reference)

    def test_native_ddk_outputs_are_allowed_but_extra_module_source_is_rejected(self):
        result = self.restore()
        for name in ('kernel/core/init.o', 'kernel/core/.init.o.cmd', 'kernel/core/.init.o.d',
                     'kernel/kernelsu.ko', 'kernel/kernelsu.mod', 'kernel/kernelsu.mod.c',
                     'kernel/Module.symvers', 'kernel/modules.order'):
            self.write(name, b'synthetic compiler output\n')
        self.assertEqual(result, port.verify_bakasu_kpm_port(self.source, self.reference,
            scope=('kernel', 'uapi', 'userspace/ksud/src/assets.rs')))
        self.write('kernel/unknown.mod.c', b'/* not an audited generated source filename */\n')
        with self.assertRaisesRegex(ValueError, 'Additional source changes'):
            port.verify_bakasu_kpm_port(self.source, self.reference, scope=('kernel', 'uapi'))

    def native_build_inputs(self):
        fixtures = ROOT / 'tests/fixtures/bakasu-native-build'
        for name, expected in port.NATIVE_BUILD_INPUTS.items():
            data = bytes.fromhex((fixtures / (name.replace('/', '_') + '.hex')).read_text('ascii'))
            self.assertEqual(expected, port.digest(data))
            self.write(name, data)
        self.command('add', 'kernel')
        self.reference = self.commit('unchanged native build generators')

    def native_build_outputs(self):
        # Non-executable recognition fixtures: these test classification only,
        # never claim that a synthetic ELF header is a compiled module/tool.
        def elf(machine):
            data = bytearray(64)
            data[:7] = b'\x7fELF\x02\x01\x01'
            struct.pack_into('<HH', data, 16, 2, machine)
            struct.pack_into('<H', data, 52, 64)
            return bytes(data)
        header = (port.SYSCALL_HEADER_PREFIX + '#include <asm/syscall.h>\n'
                  '#include <asm/unistd_compat_32.h>\n\n' + port.SYSCALL_COMPAT_TAIL).encode()
        database = json.dumps([{'directory': '/opt/ddk/kdir/android15-6.6',
                               'file': 'kernel/core/init.c', 'command': 'clang -c kernel/core/init.c'}]).encode()
        for name in port.NATIVE_BUILD_OUTPUTS:
            data = (header if name.endswith('syscall_no.h') else database if name.endswith('.json')
                    else (port.CFI_LINKER_PREFIX + '\t\tnative_target.cfi_jt = . + 0x0;\n'
                          + port.CFI_LINKER_SUFFIX).encode() if name.endswith('.lds')
                    else elf(183 if name.endswith('runner.elf') else 62))
            self.write(name, data)

    def test_actual_native_output_closure_passes_without_allowing_other_sources(self):
        self.native_build_inputs()
        self.restore()
        self.native_build_outputs()
        result = port.verify_bakasu_kpm_port(self.source, self.reference)
        self.assertEqual(self.reference, result['source_ref'])
        self.assertEqual('', self.command('diff', '--cached', '--name-only'))
        self.write('kernel/compat/unreviewed.h', b'#define UNREVIEWED 1\n')
        with self.assertRaisesRegex(ValueError, r'Additional source changes.*kernel/compat/unreviewed.h'):
            port.verify_bakasu_kpm_port(self.source, self.reference)

    def test_native_contract_rejects_changed_generator_before_exempting_outputs(self):
        self.native_build_inputs()
        self.restore()
        self.native_build_outputs()
        name = 'kernel/tools/code_generation/syscall_no/syscall_no.c'
        self.write(name, (self.source / name).read_bytes() + b'/* changed */\n')
        with self.assertRaisesRegex(ValueError, 'changed official.*generator'):
            port.verify_bakasu_kpm_port(self.source, self.reference)

    def test_native_outputs_reject_unexpected_formats_or_any_link_traversal(self):
        self.native_build_inputs()
        self.restore()
        self.native_build_outputs()
        for name in sorted(port.NATIVE_BUILD_OUTPUTS):
            path = self.source / name
            original = path.read_bytes()
            path.write_bytes(b'unreviewed source text')
            with self.subTest(name=name), self.assertRaises(ValueError):
                port.verify_bakasu_kpm_port(self.source, self.reference)
            path.write_bytes(original)
        link = self.source / 'kernel/compat'
        real = port.is_link
        with mock.patch.object(port, 'is_link', side_effect=lambda p: p == link or real(p)), \
                self.assertRaisesRegex(ValueError, 'symlink or junction'):
            port.native_build_output(self.source, 'kernel/compat/syscall_no.h')

    def test_generated_header_only_accepts_native_constant_forms(self):
        head = port.SYSCALL_HEADER_PREFIX + '#include <asm/syscall.h>\n'
        origin = '/** Generated from /opt/ddk/kdir/android14-6.1/arch/arm64/include/asm/unistd32.h **/\n'
        table = '#define __NR_compat32_restart_syscall 0\n#define __NR_compat32_ugetrlimit 191\t/* SuS compliant getrlimit */\n'
        real = (head + origin + table + '\n' + port.SYSCALL_COMPAT_TAIL).encode()
        port.native_syscall_header(real)
        port.native_syscall_header(real.replace(b'\n', b'\r\n'))
        cases = [real.replace(b' 191\t', b' arbitrary_function()\t'),
                 real.replace(b' 191\t', b' 191; unreviewed();\t'),
                 real.replace(b'#define __NR_compat32_restart_syscall 0', b'#include "unreviewed.h"'),
                 real.replace(b'__NR_compat32_ugetrlimit', b'__NR_compat32_restart_syscall'),
                 real.replace(b'/opt/ddk/kdir/', b'/unreviewed;escape/'),
                 real.replace(b'asm/unistd32.h', b'asm/unreviewed.h'),
                 real.replace(b'asm/unistd32.h', b'asm/unistd32.h */ injected /*'),
                 real.replace(table.encode(), b''), real + b'void injected(void);\n']
        for data in cases:
            with self.subTest(data=data[:100]), self.assertRaises(ValueError):
                port.native_syscall_header(data)
        for path in ('/opt/ddk/kdir/android14-6.1/arch/arm64/include/asm/unistd32.h',
                     '/opt/ddk/src/android14-6.1/arch/arm64/include/asm/unistd32.h',
                     './arch/arm64/include/asm/unistd32.h', '../arch/arm64/include/asm/unistd32.h',
                     'arch/arm64/include/asm/unistd32.h'):
            with self.subTest(path=path):
                changed = real.replace(b'/opt/ddk/kdir/android14-6.1/arch/arm64/include/asm/unistd32.h', path.encode())
                port.native_syscall_header(changed)

    def test_android12_cfi_module_rule_and_native_formatter_are_preserved(self):
        fixtures = ROOT / 'tests/fixtures/bakasu-native-build'
        proof = json.loads((fixtures / 'android12-cfi-provenance.json').read_bytes())
        files = {}
        for name, item in proof['files'].items():
            raw = bytes.fromhex((fixtures / name).read_text('ascii'))
            self.assertEqual(item['sha256'], port.digest(raw))
            self.assertEqual(item['size'], len(raw))
            files[item['path']] = raw.decode('ascii')
        rule = files['scripts/Makefile.modfinal']
        self.assertIn('ifdef CONFIG_CFI_CLANG', rule)
        self.assertIn('scripts/generate_cfi_kallsyms.pl --module', rule)
        self.assertIn('$@ > $(@:.ko=.lds)', rule)
        script = files['scripts/generate_cfi_kallsyms.pl']
        self.assertIn('push(@symbols, "\\t\\t$cfi_jt_symbol = . + 0x$last_branch_addr;")', script)
        self.assertIn('if (!scalar(@symbols)) {\n\t\treturn;\n\t}', script)
        self.assertIn('print "SECTIONS {\\n"', script)
        self.assertIn('__patchable_function_entries : { *(__patchable_function_entries) }', script)

    def test_cfi_generated_script_rejects_extra_linker_features_and_wrong_names(self):
        script = (port.CFI_LINKER_PREFIX + '\t\tnative_target.llvm.123.cfi_jt = . + 0x10;\n'
                  + port.CFI_LINKER_SUFFIX).encode()
        port.native_cfi_linker_script(script)
        port.native_cfi_linker_script(script.replace(b'\n', b'\r\n'))
        port.native_cfi_linker_script(b'')
        cases = [script + b'INCLUDE unreviewed.lds\n',
                 script.replace(b' = . + 0x10;', b' = evil_symbol;'),
                 script.replace(b'.cfi_jt = ', b' = '),
                 script.replace(b' = . + 0x10;', b' = . + 0x10; INCLUDE unreviewed.lds;'),
                 script.replace(b'0x10', b'0x10000000000000000'),
                 script.replace(b'native_target.llvm.123', b'unsafe"symbol'),
                 script.replace(b'\t\tnative_target.llvm.123.cfi_jt = . + 0x10;\n', b''),
                 b'\0' + script, b'\n', b' \n']
        for raw in cases:
            with self.subTest(raw=raw[:90]), self.assertRaises(ValueError):
                port.native_cfi_linker_script(raw)
        self.native_build_inputs()
        self.restore()
        self.write('kernel/kernelsu.lds', script)
        port.verify_bakasu_kpm_port(self.source, self.reference)
        self.write('kernel/kernelsu.lds', b'')
        port.verify_bakasu_kpm_port(self.source, self.reference)
        self.write('kernel/unreviewed.lds', script)
        with self.assertRaisesRegex(ValueError, 'Additional source changes.*kernel/unreviewed.lds'):
            port.verify_bakasu_kpm_port(self.source, self.reference)

    def test_cfi_alias_script_links_a_real_relocatable_module(self):
        compiler = os.environ.get('BAKASU_NATIVE_CC') or shutil.which('cc')
        if not compiler:
            self.skipTest('Requires a native C compiler and ELF linker')
        source = self.directory / 'cfi-input.c'
        source.write_bytes(b'void native_target(void) {}\n'
                           b'__attribute__((used, section("__patchable_function_entries")))\n'
                           b'void *native_entry = (void *)native_target;\n')
        linker = self.directory / 'kernelsu.lds'
        command = [compiler]
        if Path(compiler).stem.lower() == 'zig':
            command += ['cc', '-target', 'aarch64-linux-musl']
        obj = self.directory / 'cfi-input.o'
        module = self.directory / 'cfi-linked.ko'
        subprocess.run(command + ['-c', str(source), '-o', str(obj)], check=True,
                       capture_output=True, timeout=120)
        alias_script = (port.CFI_LINKER_PREFIX + '\t\tnative_target.cfi_jt = . + 0x0;\n'
                        + port.CFI_LINKER_SUFFIX).encode()
        for script in (alias_script, b''):
            with self.subTest(empty=not bool(script)):
                linker.write_bytes(script)
                port.native_cfi_linker_script(script)
                result = subprocess.run(command + ['-r', '-nostdlib', '-no-pie', str(obj), '-Wl,-T,' + str(linker),
                                                  '-o', str(module)], capture_output=True, timeout=120)
                self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))
                data = module.read_bytes()
                self.assertEqual(b'\x7fELF\x02\x01\x01', data[:7])
                self.assertEqual(1, struct.unpack_from('<H', data, 16)[0])
                if script:
                    self.assertIn(b'native_target.cfi_jt\0', data)
                else:
                    self.assertNotIn(b'native_target.cfi_jt\0', data)

    def test_actual_native_generator_runs_for_arm64_compat_and_legacy_table(self):
        compiler = os.environ.get('BAKASU_NATIVE_CC') or shutil.which('cc')
        if not compiler:
            self.skipTest('Requires a native C compiler')
        data = bytes.fromhex((ROOT / 'tests/fixtures/bakasu-native-build/kernel_tools_code_generation_syscall_no_syscall_no.c.hex').read_text('ascii'))
        source = self.directory / 'syscall_no.c'
        source.write_bytes(data)
        executable = self.directory / ('syscall_no.exe' if os.name == 'nt' else 'syscall_no')
        command = [compiler] + (['cc'] if Path(compiler).stem.lower() == 'zig' else [])
        subprocess.run(command + [str(source), '-o', str(executable)], check=True, capture_output=True, timeout=120)
        legacy = self.directory / 'unistd32.h'
        legacy.write_bytes(b'#define __NR_restart_syscall 0\n#define __NR_ugetrlimit 191\t/* SuS compliant getrlimit */\n')
        for args in (['--arm64'], ['--arm64-compat'], ['--arm64-compat', str(legacy)]):
            with self.subTest(args=args):
                output = self.directory / 'syscall_no.h'
                subprocess.run([str(executable), str(output), *args], check=True, capture_output=True, timeout=15)
                header = output.read_bytes()
                if len(args) == 2:
                    # The generator includes its real input path in a comment.
                    # Substitute only this host-specific comment for the DDK path.
                    for path in (b'/opt/ddk/kdir/android14-6.1/arch/arm64/include/asm/unistd32.h',
                                 b'/opt/ddk/src/android14-6.1/arch/arm64/include/asm/unistd32.h'):
                        port.native_syscall_header(header.replace(str(legacy).encode(), path))
                    header = header.replace(str(legacy).encode(), b'/opt/ddk/src/android14-6.1/arch/arm64/include/asm/unistd32.h')
                port.native_syscall_header(header)
        # Run the unmodified generator with real relative srctree arguments.
        # Both resolve to files owned by this test, while their spelling in
        # the native comment remains exactly as passed by Kbuild.
        relative = self.directory / 'arch/arm64/include/asm/unistd32.h'
        relative.parent.mkdir(parents=True)
        relative.write_bytes(legacy.read_bytes())
        child = self.directory / 'obj'
        child.mkdir()
        for cwd, path in ((self.directory, './arch/arm64/include/asm/unistd32.h'),
                          (child, '../arch/arm64/include/asm/unistd32.h')):
            with self.subTest(srctree=path):
                output = self.directory / 'relative-syscall_no.h'
                subprocess.run([str(executable), str(output), '--arm64-compat', path],
                               cwd=cwd, check=True, capture_output=True, timeout=15)
                port.native_syscall_header(output.read_bytes())

    def test_symlinked_destination_is_rejected_before_application(self):
        parent = self.source / 'kernel/kpm'
        original = port.is_link
        with mock.patch.object(port, 'is_link', side_effect=lambda path: path == parent or original(path)), \
                self.assertRaisesRegex(ValueError, 'symlink or junction'):
            self.restore()
        self.assertEqual(self.before, (self.source / self.relative).read_bytes())
        self.assertFalse((self.source / self.new_path).exists())

    def test_late_apply_failure_restores_only_this_invocations_known_files(self):
        original = port.git
        def fail(source, *args, **kwargs):
            if source == self.source and args[:3] == ('apply', '--reverse', '--check'):
                raise port.KpmPortError('injected reverse-check failure')
            return original(source, *args, **kwargs)
        with mock.patch.object(port, 'git', side_effect=fail), self.assertRaisesRegex(ValueError, 'injected'):
            self.restore()
        self.assertEqual(self.before, (self.source / self.relative).read_bytes())
        self.assertFalse((self.source / self.new_path).exists())
        self.assertFalse((self.source / port.RECEIPT).exists())
        self.assertEqual('', self.command('diff', '--name-only'))

    def test_independent_change_during_failure_is_never_overwritten_by_rollback(self):
        original = port.git
        changed = self.after + b'/* concurrently modified; keep */\n'
        def fail(source, *args, **kwargs):
            if source == self.source and args[:3] == ('apply', '--reverse', '--check'):
                self.write(self.relative, changed)
                raise port.KpmPortError('injected failure')
            return original(source, *args, **kwargs)
        with mock.patch.object(port, 'git', side_effect=fail), \
                self.assertRaisesRegex(ValueError, 'independently changed'):
            self.restore()
        self.assertEqual(changed, (self.source / self.relative).read_bytes())
        self.assertFalse((self.source / self.new_path).exists())


if __name__ == '__main__':
    unittest.main()
