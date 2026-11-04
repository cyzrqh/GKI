"""One selected release, using signed APKs and verified inert kernel fixtures."""
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

from apk_fixture import manager_metadata, signed_apk
from embedded_build_metadata import read_manager_apk
from kernel_fixture import ak3_bytes, kernel_fixture
from manager_variants import COMBINED_MANAGERS, COMBINED_SELECTION, LOCAL_MANAGERS, manager_profile
from test_additional_managers import manifest
import test_oplus_assets as oplus_fixture
import oplus_assets
import prepare_release as legacy_gki
import prepare_selected_release as selected


TIERS = {'SukiSU': ('40922', 'Stable(标准)', '正式版'),
         'BakaSU': ('35252', 'Latest(最新)', '最新测试版'),
         'KernelSU-Next': ('45003', 'Dev(开发)', '开发版'),
         'KowSU': ('35888', 'Stable(标准)', '正式版'),
         'KernelSU': ('33001', 'Latest(最新)', '最新测试版'),
         'FinaSU': ('44551', 'Dev(开发)', '开发版')}
SECOND_SELECTION = 'KowSU + KernelSU + FinaSU'
SECOND_MANAGERS = ('KowSU', 'KernelSU', 'FinaSU')
CURRENT = {'GITHUB_REPOSITORY': 'cyzrqh/gki', 'GITHUB_RUN_ID': '123',
           'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40}


def manager_fixture(variant, *, reference='b' * 40):
    profile = manager_profile(variant)
    code, branch, _ = TIERS[variant]
    metadata = manager_metadata(file=f"{profile['apk_prefixes'][0]}_v4.2.0_{code}-release.apk",
                                package=profile['package'], ksu_variant=variant, ksu_ref=reference,
                                ksu_version_code=code, ksu_version_name='4.2.0', ksu_branch=branch)
    receipt = None
    if variant not in LOCAL_MANAGERS:
        # Model an unmodified upstream APK: it has no embedded GKI receipt.
        raw = signed_apk(metadata, member='assets/upstream-info.json',
                         manifest_bytes=manifest(profile['package'], code=int(code), version='4.2.0'))
        receipt = {**metadata, 'producer_run_attempt': 1,
                   'apk_sha256': hashlib.sha256(raw).hexdigest(),
                   'upstream_apk': {'repository': profile['repository'], 'head_sha': reference}}
    else:
        raw = signed_apk(metadata)
    verified = read_manager_apk(raw, filename=metadata['file'], external_metadata=receipt)
    return raw, verified, receipt


def checksums(path):
    rows = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        checksum, name = line.split('  ', 1)
        if name in rows:
            raise AssertionError('Duplicate global checksum filename')
        rows[name] = checksum
    return rows


class SelectedGkiReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='selected-release-', dir=ROOT.parent / '.gki-upstream-cache')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'downloaded'
        self.source.mkdir()
        self.output = self.root / 'release-publish'
        patcher = mock.patch.dict(os.environ, {**CURRENT, 'GKI_MANAGER_METADATA': 'preexisting value'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.inputs, self.managers, self.kernels = {}, {}, {}
        self.originals = {}

    def populate(self, variants=COMBINED_MANAGERS, *, kernels=True, boot=False):
        for variant in variants:
            directory = self.source / ('artifact-' + variant)
            directory.mkdir()
            raw, manager, receipt = manager_fixture(variant)
            apk = directory / manager['file']
            apk.write_bytes(raw)
            self.originals[apk.name] = raw
            self.managers[variant] = manager
            self.inputs[variant] = {'apk_filename': manager['file'], 'metadata': receipt,
                                    'expected_count': int(kernels), 'full_catalog': True}
            if not kernels:
                continue
            native = 'android14-6.1.145-2025-08-AnyKernel3.zip'
            document, image = kernel_fixture(manager, file=native)
            filename = native.replace('-AnyKernel3.zip', '-' + variant + '-AnyKernel3.zip')
            document.update(file=filename, files=[filename])
            if boot:
                name = filename.removesuffix('-AnyKernel3.zip') + '-boot.img'
                payload = b'ANDROID! inert, non-installable boot fixture ' + variant.encode()
                (directory / name).write_bytes(payload)
                self.originals[name] = payload
                document['files'].append(name)
                document['boot_sha256'][name] = hashlib.sha256(payload).hexdigest()
            path = directory / filename
            path.write_bytes(ak3_bytes(document, image))
            self.originals[filename] = path.read_bytes()
            self.kernels[variant] = path, document, image

    def prepare(self, *, selection=COMBINED_SELECTION, operation='编译内核', inputs=None, output=None):
        return selected.prepare(self.source, output or self.output, selection=selection, platform='gki',
                                operation=operation, manager_inputs=self.inputs if inputs is None else inputs)

    def assert_original_assets(self, expected=None):
        expected = self.originals if expected is None else expected
        actual = {path.name: path.read_bytes() for path in (self.output / 'assets').iterdir()}
        self.assertEqual(set(expected) | {'SHA256SUMS.txt'}, set(actual))
        for name, content in expected.items():
            self.assertEqual(content, actual[name], name)
        self.assertEqual({name: hashlib.sha256(content).hexdigest() for name, content in expected.items()},
                         checksums(self.output / 'assets/SHA256SUMS.txt'))
        self.assertEqual({'assets', 'release.md', 'title.txt'}, {path.name for path in self.output.iterdir()})

    def test_three_signed_managers_and_each_paired_kernel_form_one_native_release(self):
        self.populate(boot=True)
        result = self.prepare()
        self.assertTrue(result['publish'])
        self.assertEqual(3, result['kernels'])
        self.assertEqual(9, result['assets'])
        self.assertEqual(set(COMBINED_MANAGERS), set(result['managers']))
        self.assert_original_assets()
        title = (self.output / 'title.txt').read_text(encoding='utf-8')
        self.assertTrue(title.startswith('Android'), title)
        body = (self.output / 'release.md').read_text(encoding='utf-8')
        for variant in COMBINED_MANAGERS:
            code, _, label = TIERS[variant]
            self.assertIn(variant, title)
            self.assertIn(code, title)
            self.assertIn(label, title)
            self.assertIn(variant, body)
            self.assertIn(manager_profile(variant)['package'], body)
        self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])
        self.assertEqual(self.originals, {path.name: path.read_bytes() for path in self.source.rglob('*') if path.is_file()})

    def test_combined_manager_only_produces_actions_assets_and_never_a_public_release(self):
        self.populate(kernels=False)
        result = self.prepare(operation='仅编译管理器安装包')
        self.assertFalse(result['publish'])
        self.assertEqual(0, result['kernels'])
        self.assertEqual(3, result['assets'])
        self.assert_original_assets()

    def test_one_partial_catalog_disables_publication_for_the_entire_selection(self):
        self.populate()
        self.inputs['BakaSU']['full_catalog'] = False
        self.assertFalse(self.prepare()['publish'])
        self.assert_original_assets()

    def test_single_manager_preserves_the_existing_verified_title_and_notes(self):
        self.populate(('SukiSU',))
        moment = datetime(2026, 11, 4, tzinfo=timezone.utc)
        renderer = legacy_gki.render_notes
        def render(manager, kernels, **options):
            return renderer(manager, kernels, generated_at=moment, **options)
        with mock.patch.object(legacy_gki, 'render_notes', side_effect=render):
            self.assertTrue(self.prepare(selection='SukiSU')['publish'])
            with mock.patch.dict(os.environ, {'GKI_MANAGER_METADATA': ''}):
                legacy_gki.prepare(self.source, self.root / 'legacy', expected_kernel_count=1)
        self.assertEqual((self.root / 'legacy/title.txt').read_bytes(), (self.output / 'title.txt').read_bytes())
        self.assertEqual((self.root / 'legacy/release.md').read_bytes(), (self.output / 'release.md').read_bytes())
        self.assert_original_assets()

    def test_any_missing_selected_apk_or_kernel_fails_without_final_output(self):
        for missing in ('apk', 'kernel'):
            with self.subTest(missing=missing):
                self.source.mkdir(exist_ok=True)
                self.populate()
                path = (next(self.source.rglob(self.managers['BakaSU']['file'])) if missing == 'apk'
                        else self.kernels['BakaSU'][0])
                path.unlink()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.output.exists())
                self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])
                shutil.rmtree(self.source)
                self.source.mkdir()

    def test_expected_count_failure_in_any_manager_keeps_all_results_unpublished(self):
        self.populate()
        self.inputs['KernelSU-Next']['expected_count'] = 2
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])

    def test_kernel_source_or_certificate_mismatch_is_not_ignored_while_combining(self):
        self.populate()
        path, document, image = self.kernels['BakaSU']
        for changed in ({'ksu_ref': 'f' * 40}, {'manager_cert_sha256': 'f' * 64}):
            with self.subTest(changed=changed):
                path.write_bytes(ak3_bytes({**document, **changed}, image))
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.output.exists())
                self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])

    def test_actual_signed_apk_content_and_official_receipt_are_required(self):
        self.populate()
        apk = next(self.source.rglob(self.managers['KernelSU-Next']['file']))
        original = apk.read_bytes()
        changed = bytearray(original)
        changed[original.index(b'APK dex fixture')] ^= 1
        apk.write_bytes(changed)
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())
        apk.write_bytes(original)
        self.inputs['KernelSU-Next']['metadata']['producer_head_sha'] = 'f' * 40
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])

    def test_extra_unknown_or_duplicate_artifacts_are_never_silently_discarded(self):
        self.populate()
        duplicate = self.source / 'duplicate'
        duplicate.mkdir()
        apk = next(self.source.rglob(self.managers['SukiSU']['file']))
        (duplicate / apk.name).write_bytes(apk.read_bytes())
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())
        shutil.rmtree(duplicate)
        for name in ('manager.json', 'unselected-manager.apk'):
            with self.subTest(extra=name):
                (self.source / name).write_bytes(b'untrusted extra artifact')
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.output.exists())
                (self.source / name).unlink()
        path, document, image = self.kernels['BakaSU']
        path.write_bytes(ak3_bytes({**document, 'ksu_variant': 'UnknownSU'}, image))
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_wrong_current_producer_or_future_attempt_cannot_route_a_kernel(self):
        self.populate()
        path, document, image = self.kernels['BakaSU']
        for changed in ({'producer_run_id': '124'}, {'producer_head_sha': 'f' * 40},
                        {'producer_repository': 'another/gki'}, {'producer_run_attempt': '2'}):
            with self.subTest(changed=changed):
                path.write_bytes(ak3_bytes({**document, **changed}, image))
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.output.exists())
        self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])

    def test_missing_claimed_boot_image_fails_without_any_final_assets(self):
        self.populate(boot=True)
        next(self.source.rglob('*-BakaSU-boot.img')).unlink()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_cli_reads_small_selected_output_fields_and_emits_the_publication_gate(self):
        self.populate()
        common = {'MANAGER_SELECTION': COMBINED_SELECTION, 'RELEASE_PLATFORM': 'gki', 'OPERATION': '编译内核'}
        for variant, row in self.inputs.items():
            prefix = 'MANAGER_' + selected.SUFFIXES[variant] + '_'
            common.update({prefix + 'APK_FILENAME': row['apk_filename'],
                           prefix + 'METADATA': json.dumps(row['metadata']) if row['metadata'] else '',
                           prefix + 'EXPECTED_COUNT': str(row['expected_count']),
                           prefix + 'FULL_CATALOG': 'true'})
        for full in (True, False):
            output = self.root / ('full' if full else 'partial')
            github_output = self.root / ('outputs-' + str(full))
            environment = {**common, 'GITHUB_OUTPUT': str(github_output),
                           'MANAGER_BAKASU_FULL_CATALOG': str(full).lower()}
            with (self.subTest(full=full), mock.patch.dict(os.environ, environment),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(0, selected.main(['--input', str(self.source), '--output', str(output)]))
            self.assertEqual({'publish': str(full).lower(), 'assets': '6', 'kernels': '3'},
                             dict(line.split('=', 1) for line in github_output.read_text().splitlines()))
            self.assertTrue((output / 'assets/SHA256SUMS.txt').is_file())
        self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])

    def test_empty_output_can_be_used_but_existing_public_files_are_preserved(self):
        self.populate()
        self.output.mkdir()
        self.assertTrue(self.prepare()['publish'])
        original = {path.name: path.read_bytes() for path in (self.output / 'assets').iterdir()}
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(original, {path.name: path.read_bytes() for path in (self.output / 'assets').iterdir()})


class SelectedOplusReleaseTests(unittest.TestCase):
    variants = COMBINED_MANAGERS
    selection = COMBINED_SELECTION

    @classmethod
    def setUpClass(cls):
        oplus_fixture.OplusAssetsTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        oplus_fixture.OplusAssetsTests.tearDownClass()

    def setUp(self):
        self.fixture = oplus_fixture.OplusAssetsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source = self.fixture.inputs
        next(self.source.glob('*.apk')).unlink()
        self.output = self.fixture.directory / 'selected-release'
        self.inputs, self.originals, self.kernels = {}, {}, {}
        for variant in self.variants:
            raw, manager, receipt = manager_fixture(variant, reference=self.fixture.ksu_ref)
            (self.source / manager['file']).write_bytes(raw)
            environment = {'KSU_VARIANT': variant, 'KSU_REF': manager['ksu_ref'], 'KSU_BRANCH': manager['ksu_branch'],
                           'MANAGER_VERSION_CODE': manager['ksu_version_code'], 'MANAGER_VERSION_NAME': manager['ksu_version_name'],
                           'MANAGER_PACKAGE': manager['package'], 'MANAGER_CERT_SIZE': manager['manager_cert_size'],
                           'MANAGER_CERT_SHA256': manager['manager_cert_sha256'], 'MANAGER_APK_FILENAME': manager['file'],
                           'MANAGER_APK_SHA256': manager['apk_sha256']}
            self.fixture.image.write_bytes(oplus_fixture.image_fixture(manager, self.fixture.native))
            with mock.patch.dict(os.environ, {key: str(value) for key, value in environment.items()}):
                path = Path(oplus_assets.package(self.fixture.profile, self.fixture.workspace, self.fixture.image,
                                                self.fixture.template, self.source)['path'])
            self.kernels[variant] = path
            self.inputs[variant] = {'apk_filename': manager['file'], 'metadata': receipt,
                                    'expected_count': 1, 'full_catalog': True}
        self.originals = {path.name: path.read_bytes() for path in self.source.iterdir()}
        os.environ['GKI_MANAGER_METADATA'] = 'preexisting value'

    def prepare(self, *, selection=None, inputs=None):
        return selected.prepare(self.source, self.output, selection=selection or self.selection, platform='oplus', operation='编译内核',
                                manager_inputs=self.inputs if inputs is None else inputs,
                                oplus_matrix={'include': [self.fixture.profile]})

    def test_three_oplus_source_profiles_and_receipts_are_verified_then_combined(self):
        result = self.prepare()
        self.assertTrue(result['publish'])
        self.assertEqual(3, result['kernels'])
        self.assertEqual(6, result['assets'])
        self.assertEqual(set(self.originals) | {'SHA256SUMS.txt'},
                         {path.name for path in (self.output / 'assets').iterdir()})
        for name, content in self.originals.items():
            self.assertEqual(content, (self.output / 'assets' / name).read_bytes())
        self.assertEqual({name: hashlib.sha256(raw).hexdigest() for name, raw in self.originals.items()},
                         checksums(self.output / 'assets/SHA256SUMS.txt'))
        title = (self.output / 'title.txt').read_text(encoding='utf-8')
        self.assertTrue(title.startswith('OnePlus'), title)
        for variant in self.variants:
            code, _, label = TIERS[variant]
            self.assertIn(variant, title)
            self.assertIn(code, title)
            self.assertIn(label, title)
        body = (self.output / 'release.md').read_text(encoding='utf-8')
        self.assertIn('6.1.57-android14-11-test', body)
        self.assertIn('SUSFS', body)
        self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])

    def test_wrong_oplus_apk_binding_fails_before_a_combined_release_is_created(self):
        path = self.kernels[self.variants[1]]
        oplus_fixture.rewrite_zip(path, change=lambda data: oplus_fixture.change_metadata(
            data, lambda document: document.update(manager_apk_sha256='f' * 64)))
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])

    def test_single_oplus_report_matches_the_legacy_collector(self):
        variant = self.variants[0]
        for path in list(self.source.iterdir()):
            if path.name not in (self.inputs[variant]['apk_filename'], self.kernels[variant].name):
                path.unlink()
        self.assertTrue(self.prepare(selection=variant, inputs={variant: self.inputs[variant]})['publish'])
        legacy = self.fixture.directory / 'legacy-report'
        metadata = self.inputs[variant]['metadata']
        with mock.patch.dict(os.environ, {'GKI_MANAGER_METADATA': json.dumps(metadata) if metadata else ''}):
            oplus_assets.collect(self.source, legacy, {'include': [self.fixture.profile]}, variant)
        self.assertEqual((legacy / 'title.txt').read_bytes(), (self.output / 'title.txt').read_bytes())
        self.assertEqual((legacy / 'summary.md').read_bytes(), (self.output / 'release.md').read_bytes())
        self.assertEqual((legacy / 'SHA256SUMS.txt').read_bytes(),
                         (self.output / 'assets/SHA256SUMS.txt').read_bytes())

    def test_oplus_matrix_and_each_prepared_count_must_match_all_selected_outputs(self):
        for matrix, count in (({'include': []}, 1),
                              ({'include': [self.fixture.profile]}, 2),
                              ({'include': [{**self.fixture.profile, 'kernel_ref': 'f' * 40}]}, 1),
                              ({'include': [self.fixture.profile, self.fixture.profile]}, 2)):
            with self.subTest(matrix=matrix, count=count):
                options = copy.deepcopy(self.inputs)
                for row in options.values():
                    row['expected_count'] = count
                with self.assertRaises(ValueError):
                    selected.prepare(self.source, self.output, selection=self.selection, platform='oplus',
                                     operation='编译内核', manager_inputs=options, oplus_matrix=matrix)
                self.assertFalse(self.output.exists())
                self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])


class SelectedAdditionalGkiReleaseTests(unittest.TestCase):
    def fixture(self, variants=SECOND_MANAGERS, *, boot=False):
        fixture = SelectedGkiReleaseTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.populate(variants, boot=boot)
        return fixture

    def test_second_group_merges_three_official_apks_and_their_paired_ak3_once(self):
        fixture = self.fixture(boot=True)
        result = fixture.prepare(selection=SECOND_SELECTION)
        self.assertTrue(result['publish'])
        self.assertEqual(3, result['kernels'])
        self.assertEqual(9, result['assets'])
        self.assertEqual(list(SECOND_MANAGERS), result['managers'])
        fixture.assert_original_assets()
        title = (fixture.output / 'title.txt').read_text(encoding='utf-8')
        self.assertTrue(title.startswith('Android'), title)
        for variant in SECOND_MANAGERS:
            code, _, tier = TIERS[variant]
            self.assertIn(variant, title)
            self.assertIn(code, title)
            self.assertIn(tier, title)
            self.assertIsNotNone(fixture.inputs[variant]['metadata'])
        for variant in COMBINED_MANAGERS:
            self.assertNotIn(variant, title)
        self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])

    def test_second_group_requires_every_catalog_to_be_complete(self):
        fixture = self.fixture()
        fixture.inputs['KernelSU']['full_catalog'] = False
        self.assertFalse(fixture.prepare(selection=SECOND_SELECTION)['publish'])
        fixture.assert_original_assets()

    def test_each_new_group_member_keeps_its_single_manager_report_and_native_files(self):
        for variant in SECOND_MANAGERS:
            with self.subTest(variant=variant):
                fixture = self.fixture((variant,))
                moment = datetime(2026, 11, 4, tzinfo=timezone.utc)
                renderer = legacy_gki.render_notes
                def render(manager, kernels, **options):
                    return renderer(manager, kernels, generated_at=moment, **options)
                with mock.patch.object(legacy_gki, 'render_notes', side_effect=render):
                    self.assertTrue(fixture.prepare(selection=variant)['publish'])
                    with mock.patch.dict(os.environ, {'GKI_MANAGER_METADATA': json.dumps(fixture.inputs[variant]['metadata'])}):
                        legacy_gki.prepare(fixture.source, fixture.root / 'legacy', expected_kernel_count=1)
                for name in ('title.txt', 'release.md'):
                    self.assertEqual((fixture.root / 'legacy' / name).read_bytes(), (fixture.output / name).read_bytes())
                fixture.assert_original_assets()


class SelectedAdditionalOplusReleaseTests(SelectedOplusReleaseTests):
    variants = SECOND_MANAGERS
    selection = SECOND_SELECTION

    def test_one_partial_second_group_catalog_disables_publication(self):
        self.inputs['FinaSU']['full_catalog'] = False
        self.assertFalse(self.prepare()['publish'])
        self.assertEqual({name: hashlib.sha256(raw).hexdigest() for name, raw in self.originals.items()},
                         checksums(self.output / 'assets/SHA256SUMS.txt'))
        self.assertEqual('preexisting value', os.environ['GKI_MANAGER_METADATA'])


if __name__ == '__main__':
    unittest.main()
