from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from apk_fixture import manager_metadata, signed_apk
from manager_lkm_jobs import KMIS
from restore_prebuilt_manager_apk import restore
from reusable_manager_apk import manager_selection
from test_additional_managers import manifest


class RetainedManagerApkTests(unittest.TestCase):
    def test_recover_original_after_eviction_and_reject_same_version_different_source(self):
        source = {'ref': 'b' * 40, 'git_tree': '1' * 40, 'git_count': 512, 'git_describe': 'v4.2.0'}
        identity = manager_metadata(ksu_branch='Stable(标准)', manager_recipe_sha256='c' * 64,
                                    manager_source_identity=source, manager_native_proof={
                                        'schema_version': 1, 'supported_kmis': sorted(KMIS),
                                        'lkm_report_sha256': 'd' * 64, 'boot_report_sha256': 'e' * 64})
        package = manifest('com.sukisu.ultra', code=40922, version='4.2.0')
        wrong = {**identity, 'ksu_ref': '2' * 40, 'manager_source_identity': {**source, 'ref': '2' * 40}}
        apk = signed_apk(identity, manifest_bytes=package)
        wrapped = BytesIO()
        with ZipFile(wrapped, 'w') as archive:
            archive.writestr(identity['file'], apk)
        selection = manager_selection('SukiSU', 'Latest(最新)', source['ref'], identity['manager_cert_size'],
                                      identity['manager_cert_sha256'], identity['manager_recipe_sha256'], source)
        consumer = {'repository': 'cyzrqh/GKI', 'run_id': 456, 'run_attempt': 1, 'head_sha': 'f' * 40}
        artifacts = [{'name': 'SukiSU_v4.2.0_40921-release.apk'}, {'name': identity['file']}, {'name': identity['file']}]
        with (tempfile.TemporaryDirectory() as directory,
              patch('restore_prebuilt_manager_apk.successful_runs', return_value=iter([{'id': 789}])),
              patch('restore_prebuilt_manager_apk.run_artifacts', return_value=iter(artifacts)),
              patch('restore_prebuilt_manager_apk.verified_artifact',
                    side_effect=(signed_apk(wrong, manifest_bytes=package), wrapped.getvalue())) as download):
            root = Path(directory).resolve()
            result = restore(object(), cache=root / 'evicted', output=root / 'output', consumer=consumer,
                             selection=selection, branch='main', version=40922)
            self.assertEqual(2, download.call_count)
            self.assertEqual('true', result['manager_apk_reused'])
            self.assertEqual(apk, (root / 'output' / identity['file']).read_bytes())


if __name__ == '__main__':
    unittest.main()
