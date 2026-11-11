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
from manager_variants import (COMBINED_MANAGERS, COMBINED_SELECTION, LOCAL_MANAGERS,
                              manager_profile, selected_managers)
from test_additional_managers import manifest
import test_oplus_assets as oplus_fixture
import oplus_assets
import oplus_cctv
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


def manager_fixture(variant, *, reference='b' * 40, branch=None):
    profile = manager_profile(variant)
    code, default_branch, _ = TIERS[variant]
    branch = default_branch if branch is None else branch
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

    def populate(self, variants=COMBINED_MANAGERS, *, kernels=True, boot=False, branch=None):
        for variant in variants:
            directory = self.source / ('artifact-' + variant)
            directory.mkdir()
            raw, manager, receipt = manager_fixture(variant, branch=branch)
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

    def prepare(self, *, selection=COMBINED_SELECTION, operation='编译内核', inputs=None, output=None,
                resolution=None):
        return selected.prepare(self.source, output or self.output, selection=selection, platform='gki',
                                operation=operation, manager_inputs=self.inputs if inputs is None else inputs,
                                resolution=resolution)

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
        self.assertEqual('Android · SukiSU 40922 正式版\u2028'
            + '\u2007' * 8 + 'BakaSU 35252 最新测试版\u2028'
            + '\u2007' * 8 + 'KernelSU-Next 45003 开发版', title)
        body = (self.output / 'release.md').read_text(encoding='utf-8')
        self.assertTrue(body.startswith('# Android 联合构建结果\n'))
        for variant in COMBINED_MANAGERS:
            code, _, label = TIERS[variant]
            self.assertIn(variant + ' ' + code + ' ' + label, title)
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
    branch = None

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
            raw, manager, receipt = manager_fixture(variant, reference=self.fixture.ksu_ref, branch=self.branch)
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

    def prepare(self, *, selection=None, inputs=None, resolution=None):
        return selected.prepare(self.source, self.output, selection=selection or self.selection, platform='oplus', operation='编译内核',
                                manager_inputs=self.inputs if inputs is None else inputs,
                                oplus_matrix={'include': [self.fixture.profile]}, resolution=resolution)

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
        self.assertTrue(title.startswith('OnePlus · '), title)
        body = (self.output / 'release.md').read_text(encoding='utf-8')
        self.assertTrue(body.startswith('# OnePlus 联合构建结果\n'))
        self.assertEqual(len(self.variants) - 1, title.count('\u2028'))
        self.assertTrue(all(line.startswith('\u2007' * 8 + '\u200a\u2060')
                            for line in title.split('\u2028')[1:]))
        for variant in self.variants:
            code, _, label = TIERS[variant]
            self.assertIn(variant + ' ' + code + ' ' + label, title)
        self.assertNotIn('适用构建范围', body)
        self.assertNotIn('仅证明产物及编译配置配套', body)
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

    def test_producer_cli_count_drives_environment_and_verified_oplus_publication(self):
        native_matrix = {'include': [self.fixture.profile]}
        for label, selection, variants in (('combined', self.selection, self.variants),
                                            ('single', self.variants[0], self.variants[:1])):
            with self.subTest(selection=selection):
                environment = {'MANAGER_SELECTION': selection, 'RELEASE_PLATFORM': 'oplus',
                               'OPERATION': '编译内核'}
                matrices = []
                for variant in variants:
                    producer_output = self.fixture.directory / (label + '-' + variant + '-producer-outputs')
                    with (mock.patch.object(oplus_cctv, 'discover_matrix', return_value=native_matrix) as discover,
                          mock.patch.object(sys, 'argv', ['oplus_cctv.py', '--github-output']),
                          mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(producer_output)})):
                        self.assertEqual(0, oplus_cctv.main())
                    discover.assert_called_once_with(target=oplus_cctv.catalog.ALL_TARGETS)
                    outputs = dict(line.split('=', 1) for line in
                                   producer_output.read_text(encoding='utf-8').splitlines())
                    matrix = json.loads(outputs['matrix'])
                    matrices.append(matrix)
                    # Read the real producer's output: the old CLI omitted count,
                    # and the workflow fallback then passed zero to every manager.
                    self.assertEqual(len(matrix['include']), int(outputs['count']))
                    if len(variants) > 1:
                        self.assertNotEqual(len(matrix['include']) * len(variants), int(outputs['count']))
                    row = self.inputs[variant]
                    prefix = 'MANAGER_' + selected.SUFFIXES[variant] + '_'
                    environment.update({prefix + 'APK_FILENAME': row['apk_filename'],
                        prefix + 'METADATA': json.dumps(row['metadata']) if row['metadata'] else '',
                        prefix + 'EXPECTED_COUNT': outputs['count'],
                        prefix + 'FULL_CATALOG': outputs['full_catalog']})
                self.assertTrue(all(matrix == native_matrix for matrix in matrices))
                environment['OPLUS_MATRIX'] = json.dumps(matrices[0])
                source = self.source
                if len(variants) == 1:
                    source = self.fixture.directory / 'single-contract-input'
                    source.mkdir()
                    variant = variants[0]
                    for name in (self.inputs[variant]['apk_filename'], self.kernels[variant].name):
                        shutil.copyfile(self.source / name, source / name)
                original = {path.name: path.read_bytes() for path in source.iterdir()}
                output = self.fixture.directory / (label + '-contract-publish')
                github_output = self.fixture.directory / (label + '-wrapper-outputs')
                environment['GITHUB_OUTPUT'] = str(github_output)
                stdout, stderr = io.StringIO(), io.StringIO()
                with (mock.patch.dict(os.environ, environment), redirect_stdout(stdout), redirect_stderr(stderr)):
                    prepared = selected.environment_inputs(selection)
                    self.assertEqual(set(variants), set(prepared))
                    self.assertEqual({len(native_matrix['include'])},
                                     {row['expected_count'] for row in prepared.values()})
                    status = selected.main(['--input', str(source), '--output', str(output)])
                self.assertEqual(0, status, stderr.getvalue() + stdout.getvalue())
                total_kernels = len(native_matrix['include']) * len(variants)
                self.assertEqual({'publish': 'true', 'assets': str(total_kernels + len(variants)),
                                  'kernels': str(total_kernels)}, dict(line.split('=', 1) for line in
                                    github_output.read_text(encoding='utf-8').splitlines()))
                self.assertEqual(list(variants), json.loads(stdout.getvalue())['managers'])
                self.assertEqual(original, {path.name: path.read_bytes() for path in
                    (output / 'assets').iterdir() if path.name != 'SHA256SUMS.txt'})
                self.assertEqual({name: hashlib.sha256(raw).hexdigest() for name, raw in original.items()},
                                 checksums(output / 'assets/SHA256SUMS.txt'))
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
        self.assertTrue(title.startswith('Android · '), title)
        self.assertEqual(2, title.count('\u2028'))
        self.assertTrue((fixture.output / 'release.md').read_text(encoding='utf-8')
                        .startswith('# Android 联合构建结果\n'))
        for variant in SECOND_MANAGERS:
            code, _, tier = TIERS[variant]
            self.assertIn(variant + ' ' + code + ' ' + tier, title)
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


class SkippedStableReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        oplus_fixture.OplusAssetsTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        oplus_fixture.OplusAssetsTests.tearDownClass()

    def gki(self, variants, *, kernels=True):
        fixture = SelectedGkiReleaseTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.populate(variants, kernels=kernels, branch='Stable(标准)')
        return fixture

    def oplus(self, selection, variants):
        fixture = SelectedOplusReleaseTests()
        fixture.selection, fixture.variants, fixture.branch = selection, variants, 'Stable(标准)'
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def resolution(self, selection, variants, *, reference='b' * 40):
        from resolve_build_sources import source_result
        return {'schema_version': 1, 'selection': selection, 'tier': '正式版',
                'active_managers': list(variants),
                'skipped_managers': [{'variant': variant, 'reason': '官方尚未发布正式版'}
                    for variant in selected_managers(selection) if variant not in variants],
                'sources': {variant: source_result(reference, manager_profile(variant)['repository'],
                                                   'v4.2.0', 'Stable(标准)') for variant in variants}}

    def test_each_group_publishes_only_active_managers_and_explains_missing_stable_releases(self):
        for selection, variants in ((COMBINED_SELECTION, ('SukiSU', 'KernelSU-Next')),
                                    (SECOND_SELECTION, ('KernelSU', 'FinaSU'))):
            with self.subTest(selection=selection):
                fixture = self.gki(variants)
                receipt = self.resolution(selection, variants)
                result = fixture.prepare(selection=selection, resolution=receipt)
                self.assertTrue(result['publish'])
                self.assertEqual(list(variants), result['managers'])
                self.assertEqual(2, result['kernels'])
                fixture.assert_original_assets()
                title = (fixture.output / 'title.txt').read_text(encoding='utf-8')
                notes = (fixture.output / 'release.md').read_text(encoding='utf-8')
                for row in receipt['skipped_managers']:
                    self.assertNotIn(row['variant'], title)
                    self.assertIn('- ' + row['variant'] + '：官方尚未发布正式版。', notes)

    def test_latest_groups_publish_only_distinct_sources_on_gki_and_oplus(self):
        from resolve_build_sources import source_result
        from resolve_manager_selection import LATEST_STABLE_REASON
        for platform in ('gki', 'oplus'):
            for group in (COMBINED_SELECTION, SECOND_SELECTION):
                requested = selected_managers(group)
                variants = requested[1:]
                with self.subTest(platform=platform, selection=group):
                    if platform == 'gki':
                        fixture = SelectedGkiReleaseTests()
                        fixture.setUp()
                        self.addCleanup(fixture.doCleanups)
                        fixture.populate(variants, branch='Latest(最新)')
                        reference = 'b' * 40
                    else:
                        fixture = SelectedOplusReleaseTests()
                        fixture.selection, fixture.variants, fixture.branch = group, variants, 'Latest(最新)'
                        fixture.setUp()
                        self.addCleanup(fixture.doCleanups)
                        reference = fixture.fixture.ksu_ref
                    skipped = requested[0]
                    receipt = {'schema_version': 1, 'selection': group, 'tier': '最新版',
                        'active_managers': list(variants),
                        'skipped_managers': [{'variant': skipped, 'reason': LATEST_STABLE_REASON,
                            'latest_source': source_result('d' * 40, manager_profile(skipped)['repository'],
                                                           'main', 'Latest(最新)'),
                            'stable_source': source_result('d' * 40, manager_profile(skipped)['repository'],
                                                           'v4.2.0', 'Stable(标准)')}],
                        'sources': {variant: source_result(reference, manager_profile(variant)['repository'],
                                                           'main', 'Latest(最新)') for variant in variants}}
                    result = fixture.prepare(selection=group, resolution=receipt)
                    self.assertTrue(result['publish'])
                    self.assertEqual(list(variants), result['managers'])
                    self.assertEqual(len(variants), result['kernels'])
                    self.assertNotIn(skipped, (fixture.output / 'title.txt').read_text(encoding='utf-8'))
                    self.assertIn('- ' + skipped + '：' + LATEST_STABLE_REASON + '。',
                                  (fixture.output / 'release.md').read_text(encoding='utf-8'))
                    self.assertEqual({name: hashlib.sha256(raw).hexdigest() for name, raw in fixture.originals.items()},
                                     checksums(fixture.output / 'assets/SHA256SUMS.txt'))

    def test_only_one_available_manager_keeps_its_normal_report_title_and_checksums(self):
        variants = ('KernelSU-Next',)
        fixture = self.gki(variants)
        receipt = self.resolution(COMBINED_SELECTION, variants)
        moment = datetime(2026, 11, 5, tzinfo=timezone.utc)
        renderer = legacy_gki.render_notes
        def render(manager, kernels, **options):
            return renderer(manager, kernels, generated_at=moment, **options)
        with mock.patch.object(legacy_gki, 'render_notes', side_effect=render):
            fixture.prepare(resolution=receipt)
            with mock.patch.dict(os.environ, {'GKI_MANAGER_METADATA': json.dumps(fixture.inputs[variants[0]]['metadata'])}):
                legacy_gki.prepare(fixture.source, fixture.root / 'legacy', expected_kernel_count=1)
        self.assertEqual((fixture.root / 'legacy/title.txt').read_bytes(),
                         (fixture.output / 'title.txt').read_bytes())
        self.assertTrue((fixture.output / 'release.md').read_bytes().startswith(
            (fixture.root / 'legacy/release.md').read_bytes()))
        self.assertEqual((fixture.root / 'legacy/assets/SHA256SUMS.txt').read_bytes(),
                         (fixture.output / 'assets/SHA256SUMS.txt').read_bytes())
        fixture.assert_original_assets()

    def test_skipping_a_manager_does_not_hide_missing_outputs_or_partial_catalogs(self):
        variants = ('SukiSU', 'KernelSU-Next')
        fixture = self.gki(variants)
        receipt = self.resolution(COMBINED_SELECTION, variants)
        fixture.inputs[variants[0]]['full_catalog'] = False
        self.assertFalse(fixture.prepare(resolution=receipt)['publish'])
        path = fixture.kernels[variants[1]][0]
        path.unlink()
        with self.assertRaises(ValueError):
            fixture.prepare(resolution=receipt, output=fixture.root / 'missing-output')
        self.assertFalse((fixture.root / 'missing-output').exists())

    def test_skipped_manager_artifacts_and_producer_outputs_are_rejected(self):
        fixture = self.gki(COMBINED_MANAGERS)
        variants = ('SukiSU', 'KernelSU-Next')
        receipt = self.resolution(COMBINED_SELECTION, variants)
        with self.assertRaisesRegex(ValueError, 'active managers'):
            fixture.prepare(resolution=receipt)
        inputs = {variant: fixture.inputs[variant] for variant in variants}
        with self.assertRaises(ValueError):
            fixture.prepare(inputs=inputs, resolution=receipt)
        self.assertFalse(fixture.output.exists())

    def test_available_apk_must_match_the_fixed_source_and_formal_channel(self):
        variants = ('SukiSU', 'KernelSU-Next')
        fixture = self.gki(variants)
        receipt = self.resolution(COMBINED_SELECTION, variants, reference='c' * 40)
        with self.assertRaisesRegex(ValueError, 'fixed official source or channel'):
            fixture.prepare(resolution=receipt)
        self.assertFalse(fixture.output.exists())
        latest = self.gki(variants)
        # Rebuild one signed fixture as a genuine latest-channel artifact. It
        # must not be accepted by the formal selection merely because its APK
        # and AK3 are mutually paired and come from the current producer.
        path, document, image = latest.kernels['KernelSU-Next']
        raw, manager, metadata = manager_fixture('KernelSU-Next', branch='Latest(最新)')
        next(latest.source.rglob(manager['file'])).write_bytes(raw)
        latest.inputs['KernelSU-Next']['metadata'] = metadata
        changed, changed_image = kernel_fixture(manager, file=document['file'])
        path.write_bytes(ak3_bytes(changed, changed_image))
        with self.assertRaisesRegex(ValueError, 'fixed official source or channel'):
            latest.prepare(resolution=self.resolution(COMBINED_SELECTION, variants))
        self.assertFalse(latest.output.exists())

    def test_all_skipped_cannot_create_an_empty_or_falsely_successful_release(self):
        fixture = self.gki(())
        receipt = self.resolution(COMBINED_SELECTION, ())
        with self.assertRaisesRegex(ValueError, 'at least one active manager'):
            fixture.prepare(resolution=receipt)
        self.assertFalse(fixture.output.exists())

    def test_apk_only_subset_stays_in_actions_and_cli_ignores_absent_manager_outputs(self):
        variants = ('SukiSU', 'KernelSU-Next')
        fixture = self.gki(variants, kernels=False)
        receipt = self.resolution(COMBINED_SELECTION, variants)
        environment = {'MANAGER_SELECTION': COMBINED_SELECTION, 'MANAGER_RESOLUTION': json.dumps(receipt),
                       'RELEASE_PLATFORM': 'gki', 'OPERATION': '仅编译管理器安装包',
                       'MANAGER_BAKASU_EXPECTED_COUNT': '', 'MANAGER_BAKASU_FULL_CATALOG': ''}
        for variant, row in fixture.inputs.items():
            prefix = 'MANAGER_' + selected.SUFFIXES[variant] + '_'
            environment.update({prefix + 'APK_FILENAME': row['apk_filename'],
                prefix + 'METADATA': json.dumps(row['metadata']) if row['metadata'] else '',
                prefix + 'EXPECTED_COUNT': '0', prefix + 'FULL_CATALOG': 'true'})
        stdout, stderr = io.StringIO(), io.StringIO()
        with (mock.patch.dict(os.environ, environment), redirect_stdout(stdout), redirect_stderr(stderr)):
            self.assertEqual(set(variants), set(selected.environment_inputs(COMBINED_SELECTION, receipt)))
            status = selected.main(['--input', str(fixture.source), '--output', str(fixture.output)])
        self.assertEqual(0, status, stderr.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertFalse(result['publish'])
        self.assertEqual(list(variants), result['managers'])
        fixture.assert_original_assets()

    def test_oplus_can_publish_when_the_first_requested_manager_has_no_stable_release(self):
        for selection, variants in ((COMBINED_SELECTION, ('BakaSU', 'KernelSU-Next')),
                                    (SECOND_SELECTION, ('KernelSU',))):
            with self.subTest(selection=selection):
                fixture = self.oplus(selection, variants)
                receipt = self.resolution(selection, variants, reference=fixture.fixture.ksu_ref)
                result = fixture.prepare(resolution=receipt)
                self.assertTrue(result['publish'])
                self.assertEqual(list(variants), result['managers'])
                self.assertEqual(len(variants), result['kernels'])
                self.assertEqual({name: hashlib.sha256(raw).hexdigest() for name, raw in fixture.originals.items()},
                                 checksums(fixture.output / 'assets/SHA256SUMS.txt'))
                for row in receipt['skipped_managers']:
                    self.assertIn('- ' + row['variant'] + '：官方尚未发布正式版。',
                                  (fixture.output / 'release.md').read_text(encoding='utf-8'))

    def test_oplus_subset_still_requires_all_prepared_kernels(self):
        variants = ('BakaSU', 'KernelSU-Next')
        fixture = self.oplus(COMBINED_SELECTION, variants)
        receipt = self.resolution(COMBINED_SELECTION, variants, reference=fixture.fixture.ksu_ref)
        fixture.inputs[variants[0]]['expected_count'] = 2
        with self.assertRaises(ValueError):
            fixture.prepare(resolution=receipt)
        self.assertFalse(fixture.output.exists())


if __name__ == '__main__':
    unittest.main()
