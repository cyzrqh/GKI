import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import prepare_manager_lkm as lkm


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class ManagerLkmTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.manager-lkm-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.source = self.root / 'source'
        self.source.mkdir()
        for arguments in (('init', '--quiet'), ('config', 'user.name', 'LKM test'),
                          ('config', 'user.email', 'test@example.invalid'), ('config', 'core.autocrlf', 'false')):
            self.git(*arguments)
        (self.source / 'kernel').mkdir()
        (self.source / 'kernel/Kbuild').write_text(
            'kernelsu-objs := original.o\nVERSION_BASE := 40000\nVERSION_OFFSET := 2815\n'
            'KSU_VERSION := $(if $(LOCAL_COUNT),$(shell expr $(VERSION_BASE) + $(LOCAL_COUNT) - $(VERSION_OFFSET)),41000)\n',
            encoding='utf-8')
        self.assets = self.source / 'userspace/ksud/bin/aarch64'
        self.assets.mkdir(parents=True)
        (self.assets / 'busybox').write_bytes(b'original busybox')
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'original source')
        self.reference = self.git('rev-parse', 'HEAD')
        self.version = 40000 + 1 - 2815
        (self.source / 'kernel/core').mkdir()
        self.compiler_record = self.source / 'kernel/core/.init.o.cmd'
        self.compiler_record.write_text('cmd_core/init.o := clang -DCONFIG_KSU=1 -DKSU_VERSION='
                                        + str(self.version) + ' -c core/init.c\n', encoding='utf-8')
        self.identity = {'manager_package': 'com.cyzrqh.gki.sukisu', 'manager_cert_size': 730,
                         'manager_cert_sha256': 'c8' * 32}
        self.identity_file = self.root / 'identity.json'
        self.identity_file.write_text(json.dumps(self.identity), encoding='utf-8')
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        self.report = self.root / 'verified-lkm.json'

    def cleanup(self):
        import stat
        def writable(function, name, error):
            Path(name).chmod(stat.S_IWRITE | stat.S_IREAD)
            function(name)
        shutil.rmtree(self.root, onexc=writable)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.source), *args], check=True,
                              capture_output=True, encoding='utf-8').stdout.strip()

    def module(self, kmi='android13-5.15', **overrides):
        # A tiny ELF test object exercises structural checks. It is deliberately
        # not a loadable module and is never exported outside the test fixture.
        names = b'\0.shstrtab\0.modinfo\0.rodata\0'
        release = lkm.locked_ddk(kmi)['kernel_release']
        info = overrides.get('modinfo', ('name=kernelsu\0vermagic=' + release + ' SMP preempt mod_unload modversions aarch64\0').encode())
        constants = overrides.get('constants', (self.identity['manager_package'] + '\0' + self.identity['manager_cert_sha256'] +
                                                '\0v4.2.1-' + self.reference[:8] + '@HEAD\0').encode())
        header = b'\x7fELF\x02\x01\x01' + b'\0' * 9
        header += struct.pack('<HHIQQQIHHHHHH', overrides.get('elf_type', 1), overrides.get('machine', 183),
                              1, 0, 0, 64, 0, 64, 0, 0, 64, 4, 1)
        offset = 64 + 4 * 64
        sections = [b'\0' * 64]
        for name, kind, flags, content in (('.shstrtab', 3, 0, names), ('.modinfo', 1, 2, info), ('.rodata', 1, 2, constants)):
            sections.append(struct.pack('<IIQQQQIIQQ', names.index(name.encode()), kind, flags,
                                        0, offset, len(content), 0, 0, 1, 0))
            offset += len(content)
        path = self.root / (kmi + '-fixture.ko')
        path.write_bytes(header + b''.join(sections) + names + info + constants)
        return path

    def record(self, kmi='android13-5.15', **overrides):
        module = self.module(kmi, **overrides)
        output = self.artifacts / kmi
        return lkm.record(self.source, module, kmi, self.identity_file, self.reference, output,
                          lkm.locked_ddk(kmi)['image'])

    def stage(self, expected='android13-5.15'):
        return lkm.stage(self.source, self.artifacts, self.identity_file, self.reference, expected, self.report)

    def test_real_source_history_identity_and_elf_are_bound_before_embedding(self):
        metadata = self.record()
        result = self.stage()
        self.assertEqual(result['supported_kmis'], ['android13-5.15'])
        self.assertEqual(result['mode'], 'native-lkm')
        self.assertEqual(result['ksu_ref'], self.reference)
        self.assertEqual(result['git_count'], 1)
        self.assertEqual(result['ksu_version_code'], self.version)
        self.assertEqual(metadata['compiler_records'], 1)
        self.assertRegex(metadata['compiler_records_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(result['manager_cert_sha256'], self.identity['manager_cert_sha256'])
        target = self.assets / 'android13-5.15_kernelsu.ko'
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), metadata['sha256'])
        self.assertEqual((self.assets / 'busybox').read_bytes(), b'original busybox')
        self.assertEqual(json.loads(self.report.read_text()), result)
        self.assertEqual(self.stage(), result)  # Idempotent exact same modules.

    def test_multiple_kmis_require_complete_unique_verified_set(self):
        self.record('android12-5.10')
        self.record('android13-5.15')
        for expected in ('android12-5.10', 'android12-5.10,android13-5.15,android14-6.1',
                         'android13-5.15,android13-5.15'):
            with self.subTest(expected=expected), self.assertRaises(lkm.LkmError):
                self.stage(expected)
            self.assertFalse(list(self.assets.glob('*.ko')))
        result = self.stage('android13-5.15,android12-5.10')
        self.assertEqual(result['supported_kmis'], ['android12-5.10', 'android13-5.15'])

    def test_elf_arch_type_modinfo_and_kmi_must_be_real_and_consistent(self):
        for changes in ({'machine': 62}, {'elf_type': 3}, {'modinfo': b'name=other\0vermagic=5.15.41-android13\0'},
                        {'modinfo': b'name=kernelsu\0vermagic=6.1.0-android14\0'},
                        {'modinfo': b'name=kernelsu\0'}, {'modinfo': b'name=kernelsu\0name=kernelsu\0vermagic=5.15.41-android13\0'}):
            module = self.module(**changes)
            with self.subTest(changes=changes), self.assertRaises(lkm.LkmError):
                lkm.inspect_module(module, 'android13-5.15', self.identity, self.reference)

    def test_official_signature_module_or_wrong_source_revision_is_rejected(self):
        for binding in ('manager_package', 'manager_cert_sha256', 'revision'):
            values = [self.identity['manager_package'], self.identity['manager_cert_sha256'], self.reference[:8]]
            values[('manager_package', 'manager_cert_sha256', 'revision').index(binding)] = 'official-other-binding'
            module = self.module(constants=('\0'.join(values)).encode())
            with self.subTest(binding=binding), self.assertRaisesRegex(lkm.LkmError, 'native .* binding'):
                lkm.inspect_module(module, 'android13-5.15', self.identity, self.reference)

    def test_corrupt_or_truncated_elf_is_rejected(self):
        module = self.module()
        valid = module.read_bytes()
        for payload in (b'not ELF' * 10, valid[:64], valid[:20], valid[:300]):
            module.write_bytes(payload)
            with self.subTest(length=len(payload)), self.assertRaises((lkm.LkmError, struct.error)):
                lkm.inspect_module(module, 'android13-5.15', self.identity, self.reference)

    def test_tampered_source_version_identity_filename_or_digest_stops_before_copy(self):
        metadata = self.record()
        path = self.artifacts / 'android13-5.15/module.json'
        for field, value in (('ksu_ref', 'f' * 40), ('git_count', 999), ('git_describe', 'wrong'),
                             ('ksu_version_code', 41000), ('compiler_records', 0), ('compiler_records_sha256', 'bad'),
                             ('ddk_image', None), ('ddk_image', 'ghcr.io/ylarod/ddk-min:latest'),
                             ('ddk_image', lkm.locked_ddk('android14-5.15')['image']),
                             ('manager_package', 'com.sukisu.ultra'), ('manager_cert_size', 860),
                             ('manager_cert_sha256', 'f' * 64), ('file', '../outside.ko'),
                             ('sha256', '0' * 64), ('size', 1), ('kmi', 'android14-6.1')):
            path.write_text(json.dumps({**metadata, field: value}), encoding='utf-8')
            with self.subTest(field=field), self.assertRaises(lkm.LkmError):
                self.stage()
            self.assertFalse(list(self.assets.glob('*.ko')))
            self.assertFalse(self.report.exists())

    def test_digest_change_with_unchanged_elf_stops_before_embedding(self):
        self.record()
        path = self.artifacts / 'android13-5.15/android13-5.15_kernelsu.ko'
        with path.open('ab') as stream:
            stream.write(b'different module bytes')
        with self.assertRaisesRegex(lkm.LkmError, 'digest or ELF identity changed'):
            self.stage()
        self.assertFalse(list(self.assets.glob('*.ko')))

    def test_inline_susfs_modified_kernel_source_cannot_be_used_for_native_lkm(self):
        (self.source / 'kernel/Kbuild').write_text('ported inline hooks\n', encoding='utf-8')
        with self.assertRaises(lkm.SnapshotError):
            self.record()

    def test_wrong_expected_source_and_invalid_public_identity_are_refused(self):
        module = self.module()
        with self.assertRaises(lkm.SnapshotError):
            lkm.record(self.source, module, 'android13-5.15', self.identity_file, 'a' * 40,
                       self.artifacts / 'wrong', 'ghcr.io/ylarod/ddk-min:android13-5.15-20260828')
        for changes in ({'manager_cert_size': True}, {'manager_cert_sha256': 'bad'},
                        {'manager_package': '../bad'}, {'manager_cert_size': 0}):
            self.identity_file.write_text(json.dumps({**self.identity, **changes}), encoding='utf-8')
            with self.subTest(changes=changes), self.assertRaises(lkm.LkmError):
                lkm.public_identity(self.identity_file)

    def test_existing_unverified_module_is_preserved_and_prevents_partial_staging(self):
        self.record()
        existing = self.assets / 'android13-5.15_kernelsu.ko'
        existing.write_bytes(b'prior different module')
        with self.assertRaisesRegex(lkm.LkmError, 'existing ksud LKM asset'):
            self.stage()
        self.assertEqual(existing.read_bytes(), b'prior different module')
        self.assertFalse(self.report.exists())
        existing.unlink()
        (self.assets / 'android99-9.9_kernelsu.ko').write_bytes(b'unverified')
        with self.assertRaisesRegex(lkm.LkmError, 'Unexpected pre-existing'):
            self.stage()

    def test_record_refuses_overwrite_and_cli_failure_is_explicit(self):
        self.record()
        with self.assertRaisesRegex(lkm.LkmError, 'already exists'):
            self.record()
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            status = lkm.main(['stage', '--source', str(self.source), '--artifacts', str(self.artifacts),
                               '--identity', str(self.identity_file), '--expected-ref', self.reference,
                               '--expected-kmis', 'android13-5.15,android14-6.1', '--report', str(self.report)])
        self.assertEqual(status, 1)
        self.assertIn('incomplete', output.getvalue())

    def test_detached_snapshot_version_counts_head_without_main_or_network(self):
        branch = self.git('branch', '--show-current')
        self.git('checkout', '--quiet', '--detach', self.reference)
        self.git('branch', '-D', branch)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = lkm.main(['version-code', '--source', str(self.source), '--expected-ref', self.reference])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue().strip(), str(self.version))
        self.git('commit', '--quiet', '--allow-empty', '-m', 'second fixed source')
        next_reference = self.git('rev-parse', 'HEAD')
        self.assertEqual(lkm.selected_source(self.source, next_reference)['ksu_version_code'], self.version + 1)
        with self.assertRaises(lkm.SnapshotError):
            lkm.selected_source(self.source, self.reference)

    def test_actual_compiler_version_or_missing_evidence_refuses_module_record(self):
        for command in ('cmd_init.o := clang -DKSU_VERSION=41000 -c init.c\n',
                        'cmd_init.o := clang -c init.c\n',
                        'cmd_init.o := clang -DKSU_VERSION=' + str(self.version) + ' -DKSU_VERSION=41000 -c init.c\n'):
            self.compiler_record.write_text(command, encoding='utf-8')
            with self.subTest(command=command), self.assertRaisesRegex(lkm.LkmError, 'compiler version'):
                self.record()
            self.assertFalse(list(self.artifacts.iterdir()))

    def test_unknown_native_version_formula_cannot_silently_fall_back(self):
        path = self.source / 'kernel/Kbuild'
        original = path.read_text(encoding='utf-8')
        for text in (original.replace('VERSION_BASE := 40000', 'VERSION_BASE := main'),
                     original + 'VERSION_OFFSET := 1234\n', original.replace(' + $(LOCAL_COUNT)', ' + 42')):
            path.write_text(text, encoding='utf-8')
            with self.subTest(text=text), self.assertRaisesRegex(lkm.LkmError, 'formula changed'):
                lkm.native_version_code(self.source, 3872)

    def test_official_eight_vermagics_include_generic_releases_and_match_image_locks(self):
        provenance = json.loads((ROOT / 'tests/fixtures/official-ddk-vermagic/provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(len(provenance['modules']), 8)
        for entry in provenance['modules']:
            kmi, vermagic = entry['kmi'], entry['vermagic']
            lock = lkm.locked_ddk(kmi, entry['image'])
            self.assertEqual(lock['kernel_release'], vermagic.split(' ', 1)[0])
            # A synthetic nonloadable ELF container carries the exact public
            # official .modinfo value; real provenance is in the fixture JSON.
            module = self.module(kmi, modinfo=('name=kernelsu\0vermagic=' + vermagic + '\0').encode())
            with self.subTest(kmi=kmi):
                self.assertEqual(lkm.inspect_module(module, kmi, self.identity, self.reference)['vermagic'], vermagic)

    def test_same_family_cross_kmi_or_unpinned_image_cannot_substitute_a_module(self):
        for left, right in (('android12-5.10', 'android13-5.10'), ('android13-5.15', 'android14-5.15')):
            with self.subTest(left=left, right=right):
                for actual, declared in ((left, right), (right, left)):
                    with self.assertRaisesRegex(lkm.LkmError, 'pinned DDK kernel release'):
                        lkm.inspect_module(self.module(actual), declared, self.identity, self.reference)
                    with self.assertRaisesRegex(lkm.LkmError, 'pinned image'):
                        lkm.locked_ddk(actual, lkm.locked_ddk(declared)['image'])
        module = self.module()
        for image in (None, 'ghcr.io/ylarod/ddk-min:android13-5.15-20260828',
                      'ghcr.io/ylarod/ddk-min@sha256:' + 'f' * 64):
            with self.subTest(image=image), self.assertRaisesRegex(lkm.LkmError, 'pinned image'):
                lkm.record(self.source, module, 'android13-5.15', self.identity_file, self.reference,
                           self.artifacts / 'wrong-image', image)
        self.assertFalse(list(self.artifacts.iterdir()))

    def test_existing_v2_cache_fields_remain_usable_with_exact_ddk_binding(self):
        metadata = self.record()
        self.assertNotIn('kernel_release', metadata)  # Keep the successful v2 artifact contract.
        self.assertEqual(metadata['ddk_image'], lkm.locked_ddk('android13-5.15')['image'])
        self.assertEqual(self.stage()['modules'], [metadata])


if __name__ == '__main__':
    unittest.main()
