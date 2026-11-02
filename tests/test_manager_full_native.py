import json
from pathlib import Path
import struct
import sys
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import test_manager_lkm as fixtures
import manager_full_native as native
from prepare_manager_lkm import locked_ddk


class FullNativeTests(unittest.TestCase):
    def test_container_drivers_use_linux_line_endings(self):
        for path in (ROOT / 'tools').glob('build_manager_*.sh'):
            self.assertNotIn(b'\r', path.read_bytes(), str(path))

    def setUp(self):
        self.fixture = fixtures.ManagerLkmTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.cleanup)
        self.f = self.fixture
        self.source = self.f.source
        (self.source / 'manager/app').mkdir(parents=True)
        (self.source / 'manager/build.gradle.kts').write_text('// Official root project\n')
        (self.source / 'manager/app/build.gradle.kts').write_text(
            'android { defaultConfig { ndk { abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64", "riscv64") } } }\n')
        (self.source / 'kernel/susfs').mkdir()
        (self.source / 'kernel/susfs/Makefile').write_text('obj-m += susfs_guard_lkm.o\n')
        self.f.git('add', 'manager', 'kernel/susfs')
        self.f.git('commit', '--amend', '--no-edit', '--quiet')
        self.f.reference = self.f.git('rev-parse', 'HEAD')
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict('os.environ', {'KSU_VARIANT': 'SukiSU'}).start()
        self.kmi = 'android13-5.15'

    def extras(self):
        directory = self.f.artifacts / self.kmi / 'extras'
        directory.mkdir(parents=True)
        for arch, kind, name in native.extra_components(self.source, self.kmi):
            info = ('name=' + kind + '\0vermagic=' + locked_ddk(self.kmi)['kernel_release']
                    + ' SMP\0gki_extra_source_revision=' + self.f.reference + '\0').encode()
            fixture = self.f.module(self.kmi, machine=62 if arch == 'x86_64' else 183, modinfo=info)
            target = directory / arch / name
            target.parent.mkdir()
            target.write_bytes(fixture.read_bytes())
        native.record_extras(self.source, directory, self.kmi, self.f.identity_file,
                             self.f.reference, locked_ddk(self.kmi)['image'])
        return directory

    def test_frozen_official_abis_cannot_be_replaced_by_worktree_trimming(self):
        (self.source / 'manager/app/build.gradle.kts').write_text('abiFilters("arm64-v8a")')
        self.assertEqual(tuple(native.ABIS), native.official_abis(self.source))

    def test_all_extra_components_are_checked_before_embedding(self):
        self.extras()
        native.stage_extras(self.source, self.f.artifacts, self.f.identity_file, self.f.reference, (self.kmi,))
        for arch, _, name in native.extra_components(self.source, self.kmi):
            self.assertTrue((self.source / 'userspace/ksud/bin' / arch / name).is_file())
        native.check_original_assets(self.source)

    def test_wrong_certificate_rejects_x86_module_before_any_asset_copy(self):
        directory = self.extras()
        identity = dict(self.f.identity, manager_cert_sha256='ab' * 32)
        self.f.identity_file.write_text(json.dumps(identity))
        with self.assertRaises(ValueError):
            native.stage_extras(self.source, self.f.artifacts, self.f.identity_file, self.f.reference, (self.kmi,))
        self.assertFalse((self.source / 'userspace/ksud/bin/x86_64').exists())

    def test_missing_guard_rejects_partial_native_output(self):
        directory = self.extras()
        (directory / 'aarch64' / (self.kmi + '_susfs_guard_lkm.ko')).unlink()
        with self.assertRaisesRegex(ValueError, 'Missing'):
            native.stage_extras(self.source, self.f.artifacts, self.f.identity_file, self.f.reference, (self.kmi,))
        self.assertFalse((self.source / 'userspace/ksud/bin/x86_64').exists())

    def apk(self, missing=None):
        # Small structural ELF fixtures; these are never exported as binaries.
        path = self.f.root / 'fixture.apk'
        with zipfile.ZipFile(path, 'w') as archive:
            for abi, (arch, _, machine) in native.ABIS.items():
                if abi == missing:
                    continue
                raw = bytearray(b'\x7fELF\x02\x01\x01' + b'\0' * 57)
                if arch == 'arm':
                    raw[4] = 1
                struct.pack_into('<H', raw, 16, 3)
                struct.pack_into('<H', raw, 18, machine)
                archive.writestr('lib/' + abi + '/libkernelsu.so', raw)
                archive.writestr('lib/' + abi + '/libksud.so', raw + b'busybox\0')
        return path

    def test_full_apk_keeps_every_original_architecture(self):
        self.assertEqual(list(native.ABIS), native.verify_apk(self.source, self.apk())['abis'])
        with self.assertRaisesRegex(ValueError, 'architecture'):
            native.verify_apk(self.source, self.apk(missing='riscv64'))

    def test_replacing_official_busybox_is_rejected(self):
        (self.source / 'userspace/ksud/bin/aarch64/busybox').write_bytes(b'trimmed')
        with self.assertRaisesRegex(ValueError, 'removed or changed'):
            native.verify_apk(self.source, self.apk())


if __name__ == '__main__':
    unittest.main()
