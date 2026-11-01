import copy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from apk_manifest import manifest_identity
from apk_fixture import signed_apk, manager_metadata
from embedded_build_metadata import read_manager_apk, official_receipt_from_environment
from manager_variants import (MANAGERS, LOCAL_MANAGERS, ADDITIONAL_MANAGERS, COMBINED_SELECTION,
                              COMBINED_MANAGERS, manager_profile, selected_managers)
from official_manager_apk import select_asset, unpack_apk
from additional_manager_integration import effective_susfs
from combined_manager_workflow import add_combined
from generate_workflow import create_workflow, load_yaml
from oplus_workflow import create_workflow as oplus_workflow


def manifest(package='me.weishu.kernelsu', code=32715, version='v3.3.1', *, utf8=True):
    values = ['manifest', 'package', 'versionCode', 'versionName', package, version]
    strings, offsets = b'', []
    for value in values:
        offsets.append(len(strings))
        raw = value.encode('utf-8' if utf8 else 'utf-16le')
        strings += ((bytes([len(value), len(raw)]) + raw + b'\0') if utf8 else
                    struct.pack('<H', len(value)) + raw + b'\0\0')
    strings += b'\0' * (-len(strings) % 4)
    start = 28 + 4 * len(values)
    pool = (struct.pack('<HHIIIIII', 1, 28, start + len(strings), len(values), 0,
                        0x100 if utf8 else 0, start, 0)
            + struct.pack('<' + 'I' * len(offsets), *offsets) + strings)
    attrs = b''.join(struct.pack('<IIIHBBI', 0xffffffff, name, 0xffffffff, 8, 0, kind, value)
                     for name, kind, value in ((1, 3, 4), (2, 0x10, code), (3, 3, 5)))
    node = (struct.pack('<HHIII', 0x102, 16, 36 + len(attrs), 1, 0xffffffff)
            + struct.pack('<IIHHHHHH', 0xffffffff, 0, 20, 20, 3, 0, 0, 0) + attrs)
    return struct.pack('<HHI', 3, 8, 8 + len(pool) + len(node)) + pool + node


class OfficialApkTests(unittest.TestCase):
    def native(self, variant='KernelSU', **changes):
        profile = manager_profile(variant)
        filename = profile['apk_prefixes'][0] + '_v3.3.1_32715-release.apk'
        public = manager_metadata(file=filename, package=profile['package'], ksu_variant=variant,
                                  ksu_version_code=32715, ksu_version_name='v3.3.1')
        raw = signed_apk(public, manifest_bytes=manifest(profile['package']))
        # Receipt is a producer output, separate from any APK's embedded file.
        receipt = {**public, 'apk_sha256': hashlib.sha256(raw).hexdigest(),
                   'producer_run_attempt': int(public['producer_run_attempt']),
                   'upstream_apk': {'repository': profile['repository'], 'head_sha': public['ksu_ref']}}
        receipt.update(changes)
        return raw, receipt

    def test_real_binary_manifest_identity_utf8_and_utf16(self):
        for utf8 in (True, False):
            self.assertEqual({'package': 'me.weishu.kernelsu', 'version_code': 32715, 'version_name': 'v3.3.1'},
                             manifest_identity(manifest(utf8=utf8)))
        for raw in (b'', b'<manifest/>', manifest()[:-1], manifest() + b'changed'):
            with self.assertRaises(ValueError):
                manifest_identity(raw)

    def test_official_receipt_verifies_actual_apk_manifest_signer_and_hash(self):
        for variant in ADDITIONAL_MANAGERS:
            raw, receipt = self.native(variant)
            result = read_manager_apk(raw, filename=receipt['file'], external_metadata=receipt)
            self.assertEqual(variant, result['ksu_variant'])
            self.assertEqual(receipt['apk_sha256'], result['apk_sha256'])

    def test_wrong_apk_source_package_version_and_certificate_are_rejected(self):
        raw, receipt = self.native()
        for field, value in (('package', 'com.other.manager'), ('ksu_version_code', 99999),
                             ('ksu_version_name', 'fabricated'), ('manager_cert_sha256', '0' * 64),
                             ('manager_cert_size', 4096), ('apk_sha256', '0' * 64),
                             ('file', 'SukiSU_v3_32715-release.apk'),
                             ('upstream_apk', {'repository': 'attacker/repo', 'head_sha': 'b' * 40})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                read_manager_apk(raw, external_metadata={**receipt, field: value})
        with self.assertRaises(ValueError):
            read_manager_apk(raw[:-1] + b'x', external_metadata=receipt)

    def test_only_explicit_same_run_job_output_is_accepted_as_receipt(self):
        _, receipt = self.native()
        env = {'GKI_MANAGER_METADATA': json.dumps(receipt), 'GITHUB_REPOSITORY': receipt['producer_repository'],
               'GITHUB_RUN_ID': str(receipt['producer_run_id']), 'GITHUB_RUN_ATTEMPT': '1',
               'GITHUB_SHA': receipt['producer_head_sha']}
        with patch.dict(os.environ, env):
            self.assertEqual(receipt, official_receipt_from_environment())
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '999'}), self.assertRaises(ValueError):
                official_receipt_from_environment()

    def test_stable_asset_requires_exact_tag_source_and_excludes_spoofed(self):
        release = {'tag_name': 'v3.4.2', 'draft': False, 'prerelease': False,
                   'assets': [{'id': 1, 'name': 'KernelSU_Next_v3.4.2_33355-release.apk'},
                              {'id': 2, 'name': 'KernelSU_Next_v3.4.2-spoofed_33355-release.apk'}]}
        asset = select_asset('KernelSU-Next', 'b' * 40, '正式版', 'v3.4.2',
                             api=lambda *a: release, commit=lambda *a: 'b' * 40)
        self.assertEqual(1, int(asset['url'].rsplit('/', 1)[1]))
        with self.assertRaises(ValueError):
            select_asset('KernelSU-Next', 'b' * 40, '正式版', 'v3.4.2',
                         api=lambda *a: release, commit=lambda *a: 'c' * 40)

    def test_latest_never_substitutes_an_old_or_foreign_workflow_apk(self):
        for changes in ({'head_sha': 'c' * 40}, {'conclusion': 'failure'}, {'event': 'pull_request'}):
            run = {'id': 2, 'head_sha': 'b' * 40, 'status': 'completed', 'conclusion': 'success',
                   'event': 'push', 'head_repository': {'full_name': 'tiann/KernelSU'}, **changes}
            self.assertIsNone(select_asset('KernelSU', 'b' * 40, '最新版', 'main',
                                          api=lambda *a: {'workflow_runs': [run]}))
        run = {'id': 2, 'head_sha': 'b' * 40, 'status': 'completed', 'conclusion': 'success',
               'event': 'push', 'head_repository': {'full_name': 'attacker/fork'}}
        with self.assertRaises(ValueError):
            select_asset('KernelSU', 'b' * 40, '最新版', 'main', api=lambda *a: {'workflow_runs': [run]})


class CombinedWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.single = create_workflow(ROOT)

    def test_existing_menu_defaults_and_version_choices_are_retained(self):
        current = self.single['on']['workflow_dispatch']['inputs']
        self.assertEqual(['正式版', '最新版'], current['kernelsu_branch']['options'])
        self.assertEqual('SukiSU', current['kernelsu_variant']['default'])
        self.assertEqual(tuple(MANAGERS), selected_managers('SukiSU') + tuple(MANAGERS[1:]))
        self.assertEqual(COMBINED_MANAGERS, selected_managers(COMBINED_SELECTION))
        for key in ('use_bbg', 'use_ddk', 'use_ntsync', 'use_networking'):
            self.assertFalse(current[key]['default'])

    def test_combo_uses_three_isolated_matrices_and_all_success_release_gate(self):
        for oplus, single in ((False, self.single), (True, oplus_workflow(ROOT, self.single))):
            document, (_, worker) = add_combined(single, oplus=oplus)
            calls = [job for job in document['jobs'].values() if 'uses' in job]
            self.assertEqual(3, len(calls))
            self.assertEqual(set(COMBINED_MANAGERS), {job['with']['kernelsu_variant'] for job in calls})
            self.assertEqual({'workflow_call'}, set(worker['on']))
            self.assertNotIn('publish-release', worker['jobs'])
            self.assertNotIn('verify-oplus-assets', worker['jobs'])
            self.assertEqual('true', worker['env']['GKI_COMBINED_WORKER'])
            for key, job in document['jobs'].items():
                if key.startswith('publish-combo-'):
                    self.assertEqual(3, len(job['needs']))
                    self.assertEqual(3, job['if'].count(".result == 'success'"))
                    download = next(s for s in job['steps'] if s.get('uses') == 'actions/download-artifact@v8')
                    self.assertIn('-' + job['name'].split()[-1] + '-AnyKernel3.zip', download['with']['pattern'])

    def test_kow_and_fina_capabilities_do_not_change_suki_and_baka(self):
        self.assertFalse(effective_susfs('KowSU', True))
        self.assertTrue(effective_susfs('FinaSU', True))
        with self.assertRaises(ValueError):
            effective_susfs('FinaSU', False)
        for variant in LOCAL_MANAGERS:
            self.assertFalse(effective_susfs(variant, False))
            self.assertTrue(effective_susfs(variant, True))

    def test_skipped_lkm_steps_always_have_parseable_environment_inputs(self):
        from manager_lkm_jobs import KMIS
        step = next(s for s in self.single['jobs']['prepare-matrix']['steps'] if s.get('id') == 'lkm-matrix')
        for variant in MANAGERS:
            for operation in ('编译内核', '仅编译管理器安装包'):
                expression = step['if']
                for key, value in (('github.event_name', 'workflow_dispatch'),
                                   ('inputs.operation', operation), ('inputs.kernelsu_variant', variant)):
                    expression = expression.replace(key, repr(value))
                self.assertTrue(eval(expression.replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}}))
        body = step['run'].split("python3 - <<'PY'\n", 1)[1].split('\nPY', 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'outputs'
            result = subprocess.run([sys.executable, '-c', body], cwd=ROOT,
                                    env={**os.environ, 'GITHUB_OUTPUT': str(output)}, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
            matrix = json.loads(output.read_text(encoding='utf-8').strip().split('=', 1)[1])
            self.assertEqual(len(KMIS), len(matrix['include']))
            for index in range(len(KMIS)):
                self.assertTrue(matrix['include'][index]['ddk_short'])

    def test_all_embedded_python_steps_compile(self):
        for oplus, single in ((False, self.single), (True, oplus_workflow(ROOT, self.single))):
            document, (_, worker) = add_combined(single, oplus=oplus)
            for workflow in (document, worker):
                for job in workflow['jobs'].values():
                    for step in job.get('steps', []):
                        script = step.get('run', '')
                        if "python3 - <<'PY'\n" in script:
                            for block in script.split("python3 - <<'PY'\n")[1:]:
                                body = block.split('\nPY', 1)[0]
                                if '${{' not in body:
                                    compile(body, step.get('name', 'workflow'), 'exec')


class AdditionalUpstreamTests(unittest.TestCase):
    def snapshot(self):
        from check_official_updates import validate_snapshot
        document = json.loads((ROOT / 'data/upstream-candidates.json').read_text(encoding='utf-8'))['sources']
        document['sukisu'].pop('development', None)
        for name in ADDITIONAL_MANAGERS:
            document[name.lower()] = {'branch': 'main', 'head': 'a' * 40,
                                      'release': {'tag': 'v1.0', 'commit': 'b' * 40}}
        return validate_snapshot(document)

    def candidate(self, snapshot):
        from check_official_updates import head_state
        return {'schema_version': 1, 'sources': snapshot, 'acknowledged_heads': head_state(snapshot)}

    def test_only_changed_new_manager_channel_is_reported(self):
        from official_sync_report import source_delta
        before = self.snapshot()
        after = copy.deepcopy(before)
        after['kernelsu-next']['head'] = 'c' * 40
        changes, _ = source_delta(self.candidate(before), self.candidate(after))
        self.assertEqual(1, len(changes))
        self.assertEqual(('KernelSU-Next', '默认分支'), (changes[0]['source'], changes[0]['channel']))

    def test_new_manager_source_and_version_numbers_are_bound_in_manual_sync_report(self):
        from official_sync_report import manager_versions
        before = self.snapshot()
        after = copy.deepcopy(before)
        after['kernelsu-next']['head'] = 'c' * 40
        from official_sync_report import unobserved_manager_versions
        observed = unobserved_manager_versions(before, after)
        observed.pop('reason', None)
        observed['status'] = 'observed'
        for side in ('before', 'after'):
            for row in observed[side]:
                if row['status'] != 'unavailable':
                    row['status'] = 'observed'
                    row['version_code'] = (33452 if row['commit'] == 'a' * 40 else
                                           33453 if row['commit'] == 'c' * 40 else 33355)
        result = manager_versions(observed, before, after)
        new = next(row for row in result['after'] if row['source'] == 'KernelSU-Next' and row['channel'] == '测试版')
        self.assertEqual(33453, new['version_code'])
        new['commit'] = 'd' * 40
        with self.assertRaises(ValueError):
            manager_versions(result, before, after)

    def test_unrecognized_source_or_release_shape_cannot_be_synchronized(self):
        from check_official_updates import validate_snapshot, UpdateError
        for changes in ({'head': 'not-a-sha'}, {'release': {'tag': 'v1.0', 'commit': 'bad'}}):
            snapshot = self.snapshot()
            snapshot['finasu'].update(changes)
            with self.assertRaises(UpdateError):
                validate_snapshot(snapshot)

    def test_automatic_check_and_manual_sync_discover_all_six_official_sources(self):
        import check_official_updates as updates
        def api(repository, resource='', **kwargs):
            if not resource:
                return {'default_branch': 'main'}
            if resource in ('/releases/latest', '/branches/dev'):
                return None
            raise AssertionError('Unexpected source lookup ' + resource)
        current = {'sukisu': 'a' * 40}
        baka = {'branch': 'master', 'head': 'b' * 40, 'release': None, 'prerelease': None}
        with patch.object(updates, 'github_json', api), patch.object(updates, 'github_commit', return_value='b' * 40), \
                patch.object(updates, 'fetch_bakasu_snapshot', return_value=baka), \
                patch.object(updates, 'gitlab_head', return_value='d' * 40):
            snapshot = updates.fetch_snapshot(current)
        for variant in MANAGERS:
            self.assertEqual('b' * 40, snapshot[variant.lower()]['head'])


if __name__ == '__main__':
    unittest.main()
