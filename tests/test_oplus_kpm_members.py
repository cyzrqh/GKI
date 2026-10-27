"""Check native stable/latest KPM probes against the actual vendor struct."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from oplus_ksu_compat import CB_MEMBER, CB_KBUILD, netlink_has_cb_mutex, prepare_kpm_members
from test_oplus_ksu_compat import remove_temporary

FIXTURE = ROOT / 'tests/fixtures/oplus-kpm-members'


@unittest.skipUnless(shutil.which('git'), 'Git is required for immutable native KPM verification')
class OPlusKpmMemberTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.oplus-ksu-include-' + uuid.uuid4().hex)
        self.repository = self.root / 'KernelSU'
        self.kernel = self.repository / 'kernel'
        (self.kernel / 'extension/kpm').mkdir(parents=True)
        self.header = self.root / 'common/include/linux/netlink.h'
        self.header.parent.mkdir(parents=True)
        self.header.write_bytes((FIXTURE / 'netlink-6.12.h').read_bytes())
        self.source = self.kernel / 'extension/kpm/super_access.c'
        self.kbuild = self.kernel / 'Kbuild'
        self.addCleanup(remove_temporary, self.root)
        self.git('init', '-q')
        self.git('config', 'core.autocrlf', 'false')
        self.select('formal')

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repository), *args], text=True,
                                capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def commit(self):
        self.git('add', '--all')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Native member source')
        self.reference = self.git('rev-parse', 'HEAD')

    def select(self, tier):
        self.source.write_bytes((FIXTURE / f'netlink-members-{tier}.c').read_bytes())
        self.kbuild.write_text((FIXTURE / 'Kbuild-netlink.inc').read_text() if tier == 'latest'
                              else 'ccflags-y += -Werror\n', encoding='utf-8', newline='\n')
        self.commit()

    def test_fixture_records_both_frozen_releases_and_the_real_official_target_struct(self):
        info = json.loads((FIXTURE / 'provenance.json').read_text())
        self.assertEqual('d6b36700f3bd0a4c1585549b470dd30bb8774534', info['formal_commit'])
        self.assertEqual('96ceb4a42c50e27199a094264f62cfbafee77fa0', info['latest_commit'])
        for name, expected in info['sha256'].items():
            self.assertEqual(expected, hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest())

    def test_formal_absent_member_removes_only_one_exact_probe_and_is_idempotent(self):
        original, flags = self.source.read_bytes(), self.kbuild.read_bytes()
        self.assertTrue(prepare_kpm_members(self.root, self.reference))
        self.assertEqual(original.replace(CB_MEMBER.encode(), b'', 1), self.source.read_bytes())
        self.assertEqual(flags, self.kbuild.read_bytes())
        self.assertIn(b'DEFINE_MEMBER(netlink_kernel_cfg, compare)', self.source.read_bytes())
        self.assertFalse(prepare_kpm_members(self.root, self.reference))

    def test_native_present_member_keeps_the_formal_probe(self):
        self.header.write_text(self.header.read_text().replace('\tunsigned int\tgroups;',
                              '\tstruct mutex *cb_mutex;\n\tunsigned int\tgroups;'))
        original = self.source.read_bytes()
        self.assertTrue(netlink_has_cb_mutex(self.header.read_text()))
        self.assertFalse(prepare_kpm_members(self.root, self.reference))
        self.assertEqual(original, self.source.read_bytes())

    def test_latest_keeps_native_guards_and_target_header_detection_completely_unchanged(self):
        self.select('latest')
        original, kbuild = self.source.read_bytes(), self.kbuild.read_bytes()
        self.assertFalse(prepare_kpm_members(self.root, self.reference))
        self.assertEqual(original, self.source.read_bytes())
        self.assertEqual(kbuild, self.kbuild.read_bytes())
        self.assertIn(b'#ifdef KSU_NETLINK_CFG_HAS_CB_MUTEX', original)
        self.assertIn(b'#ifdef KSU_NETLINK_CFG_HAS_COMPARE', original)

    def test_latest_missing_kbuild_detection_cannot_silently_drop_its_guarded_member(self):
        self.select('latest')
        self.kbuild.write_text(self.kbuild.read_text().replace(CB_KBUILD, ''))
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, 'native target-header Kbuild detection'):
            prepare_kpm_members(self.root, self.reference)
        self.assertEqual(original, self.source.read_bytes())

    def test_comments_other_structs_and_callback_arguments_do_not_count_as_real_member(self):
        self.header.write_text('/* cb_mutex */\nstruct unrelated { int cb_mutex; };\n'
                              + self.header.read_text().replace('struct netlink_kernel_cfg {',
                                  'struct netlink_kernel_cfg {\n/* struct mutex *cb_mutex; */\n'
                                  '\tvoid (*unrelated)(struct mutex *cb_mutex);'))
        self.assertFalse(netlink_has_cb_mutex(self.header.read_text()))
        self.assertTrue(prepare_kpm_members(self.root, self.reference))

    def test_unknown_source_change_is_rejected_without_further_writes(self):
        self.source.write_bytes(self.source.read_bytes() + b'\n/* unknown change */\n')
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, 'differs from its frozen implementation'):
            prepare_kpm_members(self.root, self.reference)
        self.assertEqual(original, self.source.read_bytes())

    def test_unknown_struct_and_unreviewed_conditional_probe_stop_before_writes(self):
        original = self.source.read_bytes()
        self.header.write_text(self.header.read_text() * 2)
        with self.assertRaisesRegex(ValueError, 'exactly one real target'):
            prepare_kpm_members(self.root, self.reference)
        self.assertEqual(original, self.source.read_bytes())
        self.header.write_bytes((FIXTURE / 'netlink-6.12.h').read_bytes())
        self.source.write_text(self.source.read_text().replace(CB_MEMBER, '#ifdef UNKNOWN\n' + CB_MEMBER + '#endif\n'),
                               encoding='utf-8', newline='\n')
        self.commit()
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Unreviewed conditional'):
            prepare_kpm_members(self.root, self.reference)
        self.assertEqual(original, self.source.read_bytes())


if __name__ == '__main__':
    unittest.main()
