"""Exercise fixed vendor provenance and safe replacement without compiling a kernel.

Filesystem lifecycle fixtures are deliberately synthetic. Their separate test
lock never replaces the production lock or claims kernel compatibility.
"""
from contextlib import ExitStack
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_vendor_sources as vendor


class VendorSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT.parent / '.gki-upstream-cache' / ('vendor-source-test-' + uuid.uuid4().hex)
        self.common = self.directory / 'common'
        self.common.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.target = self.common / vendor.SYMLINK_PATH
        self.target.parent.mkdir()
        self.profile = {'id': vendor.PROFILE, 'platform': 'sm8650', 'kernel': '6.1.141',
                        'kernel_repo': vendor.COMMON_REPOSITORY, 'kernel_ref': vendor.COMMON_COMMIT}
        self.config = self.common / 'arch/arm64/configs/gki_defconfig'
        self.config.parent.mkdir(parents=True)
        self.config.write_text('CONFIG_CGROUPS=y\nCONFIG_OPLUS_SCHED_TUNE=y\n')
        for relative in vendor.COMMON_HEADERS:
            path = self.common / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('/* Synthetic filesystem fixture, not compilable kernel headers. */\n')
        self.link_marker = b'SYNTHETIC_DANGLING_LINK_FOR_WINDOWS_TESTS'
        self.target.write_bytes(self.link_marker)
        stanza = b'config OPLUS_SCHED_TUNE\n\tbool "Native scheduler test definition"\n\tdefault n\n\n'
        self.sources = {
            'sched/Kconfig': b'config BEFORE\n\tbool\n\n' + stanza + b'config AFTER\n\tbool\n',
            'sched/sched_tune/Makefile': b'obj-$(CONFIG_OPLUS_SCHED_TUNE) += oplus_bsp_schedtune.o\n'
                                       b'oplus_bsp_schedtune-y += tune.o\n',
            'sched/sched_tune/tune.c': b''.join(
                b'#include <' + (p if p.startswith('kernel/') else p[len('include/'):]).encode() + b'>\n'
                for p in sorted(vendor.COMMON_HEADERS)) + b'#include "tune.h"\n',
            'sched/sched_tune/tune.h': b'#ifndef TEST_TUNE_H\n#define TEST_TUNE_H\n#endif\n',
        }
        self.lock = vendor.load_vendor_lock()
        self.lock['files'] = {path: {'size': len(data), 'sha256': vendor._sha256(data),
                                     'git_blob': vendor._git_blob(data)}
                              for path, data in self.sources.items()}
        fragment = vendor.COPYRIGHT + stanza
        self.lock['kconfig_fragment'].update(size=len(fragment), sha256=vendor._sha256(fragment))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.lock_patch = self.stack.enter_context(mock.patch.object(vendor, 'load_vendor_lock', return_value=self.lock))
        self.head = self.stack.enter_context(mock.patch.object(vendor, 'native_head', return_value=vendor.COMMON_COMMIT))
        self.download = self.stack.enter_context(mock.patch.object(vendor, 'download_source',
                                                                 side_effect=lambda path, size: self.sources[path]))
        # Emulate only the dangling-link filesystem primitives on Windows,
        # where creating real symlinks requires privileges. Stage writes,
        # publication, checksums and all other filesystem checks remain real.
        original_link, original_exists = Path.is_symlink, Path.exists
        original_readlink = os.readlink
        def marker(path):
            try:
                return path == self.target and path.is_file() and path.read_bytes() == self.link_marker
            except OSError:
                return False
        self.stack.enter_context(mock.patch.object(Path, 'is_symlink',
            lambda path: marker(path) or original_link(path)))
        self.stack.enter_context(mock.patch.object(Path, 'exists',
            lambda path: False if marker(path) else original_exists(path)))
        self.stack.enter_context(mock.patch.object(vendor.os, 'readlink',
            side_effect=lambda path: vendor.SYMLINK_TARGET if marker(Path(path)) else original_readlink(path)))

    def restore(self, profile=None):
        return vendor.restore_vendor_sources(self.common, self.profile if profile is None else profile)

    def test_restoration_preserves_enabled_scheduler_and_is_idempotent_without_network(self):
        original_config = self.config.read_bytes()
        report = self.restore()
        self.assertEqual(4, self.download.call_count)
        self.assertTrue(self.target.is_dir())
        self.assertEqual(original_config, self.config.read_bytes())
        for relative in self.sources:
            if relative != 'sched/Kconfig':
                self.assertEqual(self.sources[relative], (self.target / relative).read_bytes())
        kconfig = (self.target / 'sched/Kconfig').read_text()
        self.assertIn('config OPLUS_SCHED_TUNE\n', kconfig)
        self.assertNotIn('config BEFORE', kconfig)
        self.assertNotIn('config AFTER', kconfig)
        self.assertEqual(vendor.VENDOR_COMMIT, report['commit'])
        self.assertEqual(vendor.COMMON_COMMIT, report['common_commit'])
        self.assertEqual(4, len(report['files']))
        self.download.reset_mock()
        self.assertEqual(report, self.restore())
        self.download.assert_not_called()
        self.assertEqual(report, vendor.verify_vendor_report(self.common, self.profile, report))

    def test_hash_failure_keeps_original_link_and_never_touches_external_vendor_tree(self):
        external = self.directory / 'vendor/oplus/kernel/cpu'
        external.mkdir(parents=True)
        sentinel = external / 'DO_NOT_TOUCH'
        sentinel.write_bytes(b'outside common')
        self.sources['sched/sched_tune/tune.c'] += b'corrupt'
        with self.assertRaisesRegex(ValueError, 'SHA256 or Git blob'):
            self.restore()
        self.assertEqual(self.link_marker, self.target.read_bytes())
        self.assertEqual(b'outside common', sentinel.read_bytes())
        self.assertEqual([], list(self.target.parent.glob('.oplus_cpu-restore-*')))

    def test_wrong_git_blob_is_rejected_even_with_matching_size_and_sha256(self):
        self.lock['files']['sched/sched_tune/tune.c']['git_blob'] = 'a' * 40
        with self.assertRaisesRegex(ValueError, 'Git blob'):
            self.restore()
        self.assertEqual(self.link_marker, self.target.read_bytes())

    def test_unknown_common_revision_profile_pairing_or_disabled_native_option_rejects_before_download(self):
        for key, value in (('platform', 'sm8750'), ('kernel', '6.1.118'),
                           ('kernel_repo', 'cctv18/android_kernel_common_oneplus_sm8650'),
                           ('kernel_ref', 'a' * 40)):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'pairing'):
                self.restore({**self.profile, key: value})
        self.config.write_text('# CONFIG_OPLUS_SCHED_TUNE is not set\n')
        with self.assertRaisesRegex(ValueError, 'unchanged enabled native'):
            self.restore()
        self.download.assert_not_called()
        self.config.write_text('CONFIG_OPLUS_SCHED_TUNE=y\n')
        self.head.side_effect = ValueError('Exact reviewed common required')
        with self.assertRaisesRegex(ValueError, 'reviewed common'):
            self.restore()
        self.download.assert_not_called()

    def test_missing_common_header_is_rejected_without_erasing_the_enabled_feature(self):
        before = self.config.read_bytes()
        (self.common / 'kernel/sched/sched.h').unlink()
        with self.assertRaisesRegex(ValueError, 'common-header dependencies'):
            self.restore()
        self.download.assert_not_called()
        self.assertEqual(before, self.config.read_bytes())

    def test_only_exact_dangling_link_is_eligible_for_replacement(self):
        with mock.patch.object(vendor.os, 'readlink', return_value='../../../somewhere/else'), \
                self.assertRaisesRegex(ValueError, 'exact reviewed dangling'):
            self.restore()
        self.download.assert_not_called()
        self.target.write_bytes(b'ordinary existing file')
        with self.assertRaisesRegex(ValueError, 'exact reviewed dangling'):
            self.restore()
        self.download.assert_not_called()
        self.assertEqual(b'ordinary existing file', self.target.read_bytes())

    def test_existing_directory_different_file_and_unrelated_extra_file_are_not_overwritten(self):
        report = self.restore()
        tune = self.target / 'sched/sched_tune/tune.c'
        tune.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'differs from its audited bytes'):
            self.restore()
        self.assertEqual(b'changed', tune.read_bytes())
        with self.assertRaises(ValueError):
            vendor.verify_vendor_report(self.common, self.profile, report)
        tune.write_bytes(self.sources['sched/sched_tune/tune.c'])
        extra = self.target / 'unrelated'
        extra.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'incomplete, different or symlinked'):
            self.restore()
        self.assertEqual(b'keep', extra.read_bytes())

    def test_atomic_publish_failure_restores_only_original_link_and_cleans_known_stage(self):
        def restore_marker(source, target, **kwargs):
            self.assertEqual(vendor.SYMLINK_TARGET, source)
            self.assertEqual(self.target, target)
            self.target.write_bytes(self.link_marker)
        with mock.patch.object(vendor.os, 'replace', side_effect=OSError('no credentials in diagnostics')), \
                mock.patch.object(vendor.os, 'symlink', side_effect=restore_marker), \
                self.assertRaisesRegex(ValueError, 'Cannot publish'):
            self.restore()
        self.assertEqual(self.link_marker, self.target.read_bytes())
        self.assertEqual([], list(self.target.parent.glob('.oplus_cpu-restore-*')))

    def test_changed_kconfig_dependencies_or_c_include_closure_cannot_hide_behind_a_test_lock(self):
        for relative, extra in (('sched/Kconfig', b'\tselect SOME_VENDOR_FEATURE\n'),
                                ('sched/sched_tune/tune.c', b'#include "../vendor_missing.h"\n'),
                                ('sched/sched_tune/tune.c', b'extern int unpublished_vendor(void);\n'),
                                ('sched/sched_tune/tune.h', b'#include "vendor_header.h"\n')):
            with self.subTest(relative=relative, extra=extra):
                source = copy.deepcopy(self.sources)
                lock = copy.deepcopy(self.lock)
                if relative == 'sched/Kconfig':
                    source[relative] = source[relative].replace(b'\tdefault n\n', b'\tdefault n\n' + extra)
                else:
                    source[relative] += extra
                data = source[relative]
                lock['files'][relative] = {'size': len(data), 'sha256': vendor._sha256(data),
                                            'git_blob': vendor._git_blob(data)}
                with self.assertRaises(ValueError):
                    vendor.derive_sources(source, lock)

    def test_pure_provenance_validator_needs_no_source_files_and_rejects_missing_or_tampered_report(self):
        report = vendor._provenance(self.lock)
        self.assertEqual(report, vendor.validate_vendor_provenance(self.profile, report))
        self.download.assert_not_called()
        self.head.assert_not_called()
        for changed in ({}, {**report, 'schema_version': True}, {**report, 'commit': 'a' * 40},
                        {**report, 'files': {}}, {**report, 'extra': 'unreviewed'}):
            with self.subTest(report=changed), self.assertRaisesRegex(ValueError, 'immutable provenance'):
                vendor.validate_vendor_provenance(self.profile, changed)
        with self.assertRaisesRegex(ValueError, 'pairing'):
            vendor.validate_vendor_provenance({**self.profile, 'kernel_ref': 'b' * 40}, report)

    def test_other_profiles_are_noops_and_cannot_claim_this_scheduler_provenance(self):
        other = {**self.profile, 'id': 'sm8650-6.1.118'}
        self.assertEqual({}, self.restore(other))
        self.assertEqual({}, vendor.validate_vendor_provenance(other, {}))
        self.download.assert_not_called()
        self.head.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'Unexpected vendor source report'):
            vendor.validate_vendor_provenance(other, vendor._provenance(self.lock))


class ProductionLockTests(unittest.TestCase):
    def test_production_lock_pins_full_official_inputs_and_only_one_derived_kconfig(self):
        lock = vendor.load_vendor_lock()
        self.assertEqual('c951f93ec63fbee49d468ee2f9d93ac0e807da67', lock['vendor_commit'])
        self.assertEqual('ebdd1643cfdbf719fb59c6573ce0abe6e794a5cc', lock['common_commit'])
        self.assertEqual(4, len(lock['files']))
        self.assertEqual(1009, lock['kconfig_fragment']['size'])
        for key in ('vendor_repository', 'vendor_commit', 'symlink_target'):
            changed = copy.deepcopy(lock)
            changed[key] = 'unreviewed'
            with self.subTest(key=key), mock.patch.object(vendor, '_read', return_value=json.dumps(changed).encode()), \
                    self.assertRaisesRegex(ValueError, 'source lock'):
                vendor.load_vendor_lock()
        duplicate = json.dumps(lock).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
        with mock.patch.object(vendor, '_read', return_value=duplicate.encode()), self.assertRaises(ValueError):
            vendor.load_vendor_lock()

    def test_public_source_download_uses_only_fixed_official_sha_and_no_authorization(self):
        relative = 'sched/sched_tune/Makefile'
        size = vendor.FILE_LOCKS[relative][0]
        url = (f'https://raw.githubusercontent.com/{vendor.VENDOR_REPOSITORY}/'
               f'{vendor.VENDOR_COMMIT}/{vendor.SOURCE_PREFIX}{relative}')
        data = b'x' * size
        response = io.BytesIO(data)
        response.status = 200
        response.geturl = lambda: url
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(vendor, 'build_opener', return_value=opener), \
                mock.patch.dict(os.environ, {'GH_TOKEN': 'do-not-forward-token'}):
            self.assertEqual(data, vendor.download_source(relative, size))
        request = opener.open.call_args.args[0]
        self.assertEqual(url, request.full_url)
        self.assertIsNone(request.get_header('Authorization'))
        self.assertIsNone(request.get_header('Cookie'))
        self.assertIsNone(vendor._NoRedirect().redirect_request(None, None, 302, None, None, 'https://other.test'))

    def test_bad_size_redirect_and_http_errors_do_not_leak_remote_detail_or_credentials(self):
        relative = 'sched/sched_tune/Makefile'
        size = vendor.FILE_LOCKS[relative][0]
        for status, count in ((404, 1), (503, 3)):
            opener = mock.Mock()
            opener.open.side_effect = HTTPError('https://api.github.com', status,
                                                'do-not-print-token', {}, io.BytesIO(b'private'))
            with mock.patch.object(vendor, 'build_opener', return_value=opener), \
                    mock.patch.object(vendor.time, 'sleep'), self.assertRaises(ValueError) as found:
                vendor.download_source(relative, size)
            self.assertEqual(count, opener.open.call_count)
            self.assertNotIn('do-not-print-token', str(found.exception))
        with self.assertRaisesRegex(ValueError, 'Unreviewed'):
            vendor.download_source('../escape', size)

    def test_audited_real_official_source_component_when_cached(self):
        directory = ROOT.parent / '.gki-upstream-cache/oplus-vendor141-official-files'
        lock = vendor.load_vendor_lock()
        if not all((directory / relative).is_file() for relative in lock['files']):
            self.skipTest('Optional fixed official source bytes from the repair audit are not cached')
        sources = {relative: (directory / relative).read_bytes() for relative in lock['files']}
        installed = vendor.derive_sources(sources, lock)
        self.assertEqual(17141, sum(map(len, installed.values())))
        self.assertEqual(lock['kconfig_fragment']['sha256'], hashlib.sha256(installed['sched/Kconfig']).hexdigest())
        for relative in ('sched/sched_tune/Makefile', 'sched/sched_tune/tune.c', 'sched/sched_tune/tune.h'):
            self.assertEqual(sources[relative], installed[relative])


if __name__ == '__main__':
    unittest.main()
