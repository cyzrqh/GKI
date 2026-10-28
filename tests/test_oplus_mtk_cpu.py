"""Use original official bytes; simulate only transport and the local Git/link boundary."""
from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_mtk_cpu as cpu

FIXTURE = ROOT / 'tests/fixtures/oplus-mtk-cpu'


def fixture(spec):
    return bytes.fromhex((FIXTURE / (spec['sha256'] + '.hex')).read_text(encoding='ascii'))


class MtkCpuSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT.parent / '.gki-upstream-cache' / ('mtk-cpu-test-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.lock = cpu.load_lock()
        self.common = self.directory / 'common'
        self.common.mkdir()
        self.use_profile('mt6897-6.1.75')
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.head = self.patches.enter_context(mock.patch.object(cpu, 'native_head'))
        self.download = self.patches.enter_context(mock.patch.object(cpu, 'download_source',
            side_effect=lambda selected, relative, spec: fixture(spec)))
        # Windows often cannot create symlinks. The lifecycle fixture's regular
        # marker is an explicit simulation; production dangling-link checks
        # are separately exercised without this mock.
        self.link_check = self.patches.enter_context(mock.patch.object(cpu, 'dangling', side_effect=self.marker))

    def use_profile(self, identity):
        self.selected = self.lock['profiles'][identity]
        self.profile = {'id': identity, 'platform': self.selected['platform'], 'kernel': self.selected['kernel'],
                        'kernel_repo': self.selected['common_repository'], 'kernel_ref': self.selected['common_commit']}
        for relative, spec in self.selected['native'].items():
            path = self.common / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fixture(spec))
        for relative in self.lock['common_headers']:
            path = self.common / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'/* real common header is a boundary in lifecycle tests */\n')
        self.target = self.common / cpu.TARGET
        self.target.write_text(cpu.LINK, encoding='utf-8')

    def marker(self, target):
        self.assertEqual(self.target, target)
        if not target.is_file() or target.read_text(encoding='utf-8') != cpu.LINK:
            raise ValueError('Fixture link identity changed')

    def restore(self):
        return cpu.restore_mtk_cpu(self.common, self.profile)

    def test_four_real_official_closures_and_all_exact_file_hashes(self):
        for identity, selected in self.lock['profiles'].items():
            self.assertIn(identity, cpu.PROFILES)
            self.assertEqual(13, len(selected['files']))
            self.assertEqual(selected['symbols'], sorted(set(selected['symbols'])))
            for relative, spec in selected['files'].items():
                with self.subTest(profile=identity, relative=relative): cpu.checked_bytes(fixture(spec), spec)
            root = fixture(selected['files']['Kconfig']).decode('utf-8')
            for line in root.splitlines():
                if line.startswith('source '):
                    relative = line.split('kernel/oplus_cpu/', 1)[1].rstrip('"')
                    self.assertIn(relative, selected['files'])
            # Root/common Kbuild and all config defaults are original bytes,
            # including the differing native 134 defaults; no dummy Kconfig.
            sched = fixture(selected['files']['sched/Kconfig']).decode('utf-8')
            self.assertIn('config OPLUS_FEATURE_PERFORMANCE_SCHED', sched)
            self.assertIn('config OPLUS_SCHED_TUNE', sched)
            self.assertIn('obj-$(CONFIG_OPLUS_SCHED_TUNE) += oplus_cpu/sched/sched_tune/',
                          fixture(selected['native']['kernel/Makefile']).decode('utf-8'))

    def test_restore_preserves_configs_and_installs_complete_same_source_provenance(self):
        original = {relative: (self.common / relative).read_bytes() for relative in self.selected['native']}
        report = self.restore()
        self.assertEqual(13, len(report['files']))
        self.assertEqual(self.selected['vendor_commit'], report['commit'])
        self.assertEqual(report, cpu.verify_mtk_cpu_report(self.common, self.profile, report))
        self.assertEqual(original, {relative: (self.common / relative).read_bytes() for relative in original})
        self.assertEqual(13, self.download.call_count)
        self.assertEqual(report, self.restore())
        self.assertEqual(13, self.download.call_count)

    def test_all_four_profiles_restore_their_own_original_component(self):
        self.restore()
        for identity in sorted(cpu.PROFILES - {self.profile['id']}):
            shutil.rmtree(self.target)
            self.use_profile(identity)
            report = self.restore()
            self.assertEqual(self.selected['vendor_commit'], report['commit'])
            cpu.verify_mtk_cpu_report(self.common, self.profile, report)

    def test_other_profiles_do_not_touch_source_or_network(self):
        self.assertEqual({}, cpu.restore_mtk_cpu(self.common, {'id': 'sm8650-6.1.141'}))
        self.download.assert_not_called()
        self.head.assert_not_called()

    def test_profile_revision_and_foreign_origin_pairing_fail_before_download(self):
        for key, value in (('kernel_ref', 'a' * 40), ('kernel_repo', 'cctv18/unreviewed'),
                           ('platform', 'sm8650'), ('kernel', '6.1.141')):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'pairing'):
                cpu.restore_mtk_cpu(self.common, {**self.profile, key: value})
        self.download.assert_not_called()
        self.head.side_effect = ValueError('Wrong common commit')
        with self.assertRaisesRegex(ValueError, 'common commit'): self.restore()
        self.download.assert_not_called()

    def test_additional_selected_cpu_features_require_their_real_full_source(self):
        for relative, symbol, value in (('arch/arm64/configs/gki_defconfig', 'OPLUS_FEATURE_SCHED_ASSIST', 'm'),
                                        ('out/.config', 'UA_KERNEL_CPU_IOCTL', 'y')):
            path = self.common / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            old = path.read_bytes() if path.exists() else None
            path.write_bytes((old or b'') + f'\nCONFIG_{symbol}={value}\n'.encode())
            with self.subTest(symbol=symbol), self.assertRaisesRegex(ValueError, 'additional complete official sources'):
                self.restore()
            self.assertEqual(cpu.LINK, self.target.read_text())
            if old is None: path.unlink()
            else: path.write_bytes(old)
        self.download.assert_not_called()

    def test_native_default_flags_in_resolved_config_do_not_cause_a_false_postbuild_failure(self):
        self.use_profile('mt6989-6.1.134')
        report = self.restore()
        path = self.common / 'out/.config'
        path.parent.mkdir()
        path.write_text('CONFIG_OPLUS_FEATURE_PERFORMANCE_SCHED=y\nCONFIG_OPLUS_FEATURE_SCHED_DDL=y\n'
                        'CONFIG_OPLUS_SCHED_GROUP_OPT=y\n# CONFIG_OPLUS_SCHED_TUNE is not set\n')
        self.assertEqual(report, cpu.verify_mtk_cpu_report(self.common, self.profile, report))

    def test_missing_common_header_or_changed_extension_kbuild_is_rejected(self):
        for relative in ('include/linux/cgroup.h', 'Kconfig.ext', 'kernel/Makefile'):
            path = self.common / relative
            original = path.read_bytes()
            path.unlink()
            if relative != 'include/linux/cgroup.h': path.write_bytes(original + b'changed\n')
            with self.subTest(relative=relative), self.assertRaises(ValueError): self.restore()
            path.write_bytes(original)
        self.download.assert_not_called()

    def test_download_hash_or_blob_tampering_preserves_link_and_config(self):
        for field, value in (('sha256', 'a' * 64), ('git_blob', 'b' * 40), ('size', 1)):
            selected = copy.deepcopy(self.selected)
            selected['files']['sched/sched_tune/tune.c'][field] = value
            with self.subTest(field=field), mock.patch.object(cpu, 'selected_lock', return_value=(selected, self.lock['common_headers'])):
                # Download authentic bytes rather than adapting the fixture to
                # the deliberately corrupted lock being checked.
                self.download.side_effect = lambda selected, relative, spec: fixture(self.selected['files'][relative])
                with self.assertRaisesRegex(ValueError, 'verification'): self.restore()
            self.assertEqual(cpu.LINK, self.target.read_text())
            self.assertFalse(list(self.target.parent.glob('.oplus-mtk-cpu-*')))

    def test_installed_tamper_extra_files_and_provenance_changes_are_rejected(self):
        report = self.restore()
        for key, value in (('repository', 'attacker/repo'), ('commit', 'f' * 40), ('schema_version', True)):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'provenance'):
                cpu.validate_mtk_cpu_provenance(self.profile, {**report, key: value})
        extra = self.target / 'unknown.c'; extra.write_text('extra source\n')
        with self.assertRaisesRegex(ValueError, 'extra files'): self.restore()
        extra.unlink()
        (self.target / 'sched/sched_tune/tune.c').write_bytes(b'not the original source')
        with self.assertRaises(ValueError): cpu.verify_mtk_cpu_report(self.common, self.profile, report)

    def test_failed_publication_restores_link_and_removes_only_owned_stage(self):
        def recreate(target, destination, **kwargs):
            self.assertEqual(cpu.LINK, target)
            self.assertEqual(self.target, destination)
            destination.write_text(target)
        with mock.patch.object(cpu.os, 'replace', side_effect=OSError('fixture publish failure')), \
                mock.patch.object(cpu.os, 'symlink', side_effect=recreate), self.assertRaisesRegex(ValueError, 'publish'):
            self.restore()
        self.assertEqual(cpu.LINK, self.target.read_text())
        self.assertFalse(list(self.target.parent.glob('.oplus-mtk-cpu-*')))

    def test_actual_link_validator_rejects_regular_or_unreviewed_sources(self):
        # Test the original guard, outside the simulated Windows link boundary.
        self.patches.close()
        with self.assertRaisesRegex(ValueError, 'exact dangling'):
            cpu.dangling(self.target)

    def test_lock_tampering_is_not_accepted(self):
        original = cpu.read(ROOT / 'config/oplus-mtk-cpu.json')
        with mock.patch.object(cpu, 'read', return_value=original + b' '), self.assertRaisesRegex(ValueError, 'fixed SHA256'):
            cpu.load_lock()


if __name__ == '__main__':
    unittest.main()
