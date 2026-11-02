import json
import unittest

import test_manager_full_native as native_fixtures
from fina_native_components import record, stage, verify_staged
from manager_lkm_jobs import KMIS
from prepare_manager_lkm import locked_ddk


class FinaComponentsTests(unittest.TestCase):
    def setUp(self):
        native_fixtures.FullNativeTests.setUp(self)

    def nomount(self):
        for kmi in KMIS:
            directory = self.f.artifacts / kmi / 'nomount'
            directory.mkdir(parents=True)
            info = ('name=nomount\0vermagic=' + locked_ddk(kmi)['kernel_release']
                    + ' SMP\0gki_extra_source_revision=' + self.f.reference + '\0').encode()
            module = self.f.module(kmi, machine=183, modinfo=info)
            (directory / (kmi + '_nomount.ko')).write_bytes(module.read_bytes())
            record(self.source, directory, self.f.identity_file, self.f.reference,
                   kmi, locked_ddk(kmi)['image'])

    def test_all_eight_components_are_validated_before_the_native_driver_reuses_them(self):
        self.nomount()
        self.assertFalse(verify_staged(self.source, self.f.identity_file, self.f.reference))
        stage(self.source, self.f.artifacts, self.f.identity_file, self.f.reference)
        self.assertTrue(verify_staged(self.source, self.f.identity_file, self.f.reference))
        module = self.source / 'userspace/ksud/bin/aarch64' / (KMIS[0] + '_nomount.ko')
        module.write_bytes(module.read_bytes() + b'tampered')
        with self.assertRaises(ValueError):
            verify_staged(self.source, self.f.identity_file, self.f.reference)

    def test_a_corrupt_last_component_cannot_partially_stage_the_first_seven(self):
        self.nomount()
        path = self.f.artifacts / KMIS[-1] / 'nomount/nomount.json'
        receipt = json.loads(path.read_text())
        receipt['component']['sha256'] = '00' * 32
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'changed'):
            stage(self.source, self.f.artifacts, self.f.identity_file, self.f.reference)
        self.assertFalse((self.source / 'userspace/ksud/bin/aarch64' / (KMIS[0] + '_nomount.ko')).exists())
