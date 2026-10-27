"""Exact Git application, rollback and same-source guards for the KPM port."""
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
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
