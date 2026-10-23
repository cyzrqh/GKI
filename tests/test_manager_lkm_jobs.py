"""Validate pinned DDK selection and the exact-cache producer/consumer contract."""
import copy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import manager_lkm_jobs as jobs


def render_key(template, **changes):
    values = {'runner.os': 'Linux', 'runner.arch': 'X64',
              'needs.prepare-matrix.outputs.ksu_ref': 'a' * 40,
              'needs.prepare-matrix.outputs.manager_package': 'com.cyzrqh.gki.sukisu',
              'needs.prepare-matrix.outputs.manager_cert_size': '730',
              'needs.prepare-matrix.outputs.manager_cert_sha256': 'b' * 64,
              'matrix.kmi': 'android12-5.10', 'matrix.ddk_short': 'c' * 16}
    values.update(changes)
    def replace(match):
        expression = match[1].strip()
        return expression[1:-1] if expression.startswith("'") and expression.endswith("'") else values[expression]
    return re.sub(r'\$\{\{(.*?)\}\}', replace, template)


class ManagerLkmJobTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / 'tests' / ('.lkm-jobs-' + uuid.uuid4().hex)
        (self.directory / 'config').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.lock = {'schema_version': 1, 'images': [
            {'kmi': kmi, 'tag': f'ghcr.io/ylarod/ddk-min:{kmi}-20260828',
             'image': 'ghcr.io/ylarod/ddk-min@sha256:' + str(index) * 64}
            for index, kmi in enumerate(jobs.KMIS, 1)]}
        self.path = self.directory / 'config' / 'manager-ddk-images.json'

    def write_lock(self, document=None):
        self.path.write_text(json.dumps(self.lock if document is None else document), encoding='utf-8')

    def test_loader_preserves_all_eight_official_kmis_and_derives_digest_from_image(self):
        self.lock['images'].reverse()
        self.write_lock()
        matrix = jobs.load_ddk_matrix(self.directory)
        self.assertEqual(jobs.KMIS, tuple(row['kmi'] for row in matrix))
        for row in matrix:
            self.assertEqual({'kmi', 'image', 'ddk_short'}, set(row))
            self.assertEqual(row['image'].split('@sha256:')[1][:16], row['ddk_short'])
        # The actual checked-in lock is also usable without unpinned tag lookups.
        self.assertEqual(8, len(jobs.load_ddk_matrix(ROOT)))

    def test_loader_refuses_incomplete_duplicate_unknown_or_mutable_images(self):
        examples = []
        incomplete = copy.deepcopy(self.lock)
        incomplete['images'].pop()
        examples.append(incomplete)
        duplicate = copy.deepcopy(self.lock)
        duplicate['images'][1] = duplicate['images'][0]
        examples.append(duplicate)
        for field, value in (
            ('kmi', 'android99-99.99'), ('image', 'ghcr.io/ylarod/ddk-min:latest'),
            ('image', 'evil.example/ddk-min@sha256:' + 'a' * 64),
            ('image', 'ghcr.io/ylarod/ddk-min@sha256:' + 'a' * 63),
            ('image', 'ghcr.io/ylarod/ddk-min@sha256:' + 'A' * 64),
            ('image', 'ghcr.io/ylarod/ddk-min@sha256:' + 'a' * 64 + '\n'),
            ('tag', 'ghcr.io/ylarod/ddk-min:android15-6.6-20260828'),
            ('ddk_short', 'wrong-image-key'), ('unexpected', 'ignored configuration'),
        ):
            document = copy.deepcopy(self.lock)
            document['images'][0][field] = value
            examples.append(document)
        for document in examples:
            with self.subTest(document=document):
                self.write_lock(document)
                with self.assertRaises(ValueError):
                    jobs.load_ddk_matrix(self.directory)

    def test_loader_refuses_unknown_schema_missing_or_unbounded_lock(self):
        with self.assertRaises(ValueError):
            jobs.load_ddk_matrix(self.directory)
        for document in ([], {'schema_version': True, 'images': []},
                         {'schema_version': 2, 'images': []}, {'schema_version': 1, 'images': {}}):
            self.write_lock(document)
            with self.assertRaises(ValueError):
                jobs.load_ddk_matrix(self.directory)
        self.path.write_text('x' * 32769)
        with self.assertRaises(ValueError):
            jobs.load_ddk_matrix(self.directory)

    def test_cache_key_matches_fixed_consumer_and_changes_with_every_native_identity(self):
        template = jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short')
        baseline = render_key(template)
        self.assertTrue(baseline.startswith('gki-sukisu-lkm-v2-'))
        fixed = jobs.lkm_cache_key("'android12-5.10'", "'" + 'c' * 16 + "'")
        self.assertEqual(baseline, render_key(fixed))
        self.assertLess(len(baseline), 512)
        for field, value in (
            ('needs.prepare-matrix.outputs.ksu_ref', 'd' * 40),
            ('needs.prepare-matrix.outputs.manager_package', 'com.example.changed'),
            ('needs.prepare-matrix.outputs.manager_cert_size', '731'),
            ('needs.prepare-matrix.outputs.manager_cert_sha256', 'e' * 64),
            ('matrix.kmi', 'android13-5.15'), ('matrix.ddk_short', 'f' * 16),
            ('runner.arch', 'ARM64'), ('runner.os', 'Windows'),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(baseline, render_key(template, **{field: value}))
        for bad in ('', '${{ matrix.kmi }}', 'matrix.kmi\nignored'):
            with self.assertRaises(ValueError):
                jobs.lkm_cache_key(bad, 'matrix.ddk_short')

    def test_exact_cache_is_strict_and_transfers_no_module_artifacts(self):
        job = jobs.manager_lkm_job('supplied-condition')
        self.assertEqual('supplied-condition', job['if'])
        self.assertEqual('prepare-matrix', job['needs'])
        self.assertEqual({'contents': 'read'}, job['permissions'])
        self.assertEqual('ubuntu-24.04', job['runs-on'])
        self.assertNotIn('container', job)
        self.assertEqual(8, job['strategy']['max-parallel'])
        self.assertFalse(job['strategy']['fail-fast'])
        self.assertEqual('${{ fromJSON(needs.prepare-matrix.outputs.ddk_matrix) }}', job['strategy']['matrix'])
        steps = job['steps']
        caches = [step for step in steps if step.get('uses', '').startswith('actions/cache/')]
        self.assertEqual(['actions/cache/restore@v5', 'actions/cache/save@v5', 'actions/cache/restore@v5'],
                         [step['uses'] for step in caches])
        for step in caches:
            self.assertNotIn('continue-on-error', step)
            self.assertNotIn('restore-keys', step['with'])
            self.assertEqual('manager-lkm/${{ matrix.kmi }}', step['with']['path'])
            self.assertEqual(jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short'), step['with']['key'])
        self.assertTrue(caches[-1]['with']['lookup-only'])
        self.assertTrue(caches[-1]['with']['fail-on-cache-miss'])
        self.assertFalse(any('upload-artifact' in step.get('uses', '') for step in steps))
        self.assertFalse(steps[0]['with']['persist-credentials'])
        hit = next(step for step in steps if '--expected-kmis "$KMI"' in step.get('run', ''))
        build = next(step for step in steps if 'docker run' in step.get('run', ''))
        self.assertIn("cache-hit == 'true'", hit['if'])
        self.assertIn("cache-hit != 'true'", build['if'])
        self.assertIn("cache-hit != 'true'", caches[1]['if'])
        self.assertLess(steps.index(hit), steps.index(build))
        self.assertLess(steps.index(build), steps.index(caches[1]))

    def test_docker_runs_offline_in_native_ddk_context_and_receives_only_public_build_values(self):
        job = jobs.manager_lkm_job('true')
        build = next(step for step in job['steps'] if 'docker run' in step.get('run', ''))
        script = build['run']
        self.assertLess(script.index('docker pull "$DDK_IMAGE"'), script.index('docker run'))
        command = script[script.index('docker run'):script.index('python3 tools/prepare_manager_lkm.py record')]
        arguments = shlex.split(command)
        self.assertEqual('none', arguments[arguments.index('--network') + 1])
        self.assertNotIn('--user', arguments)
        self.assertNotIn('--privileged', arguments)
        self.assertEqual('/bin/bash', arguments[arguments.index('--entrypoint') + 1])
        self.assertEqual('$DDK_IMAGE', arguments[arguments.index('--entrypoint') + 2])
        passed = {arguments[index + 1].split('=', 1)[0] for index, arg in enumerate(arguments) if arg == '--env'}
        self.assertEqual({'HOME', 'KSU_PACKAGE_NAME', 'KSU_EXPECTED_SIZE', 'KSU_EXPECTED_HASH', 'KSU_VERSION', 'MODULE_OUTPUT'}, passed)
        self.assertIn('CONFIG_KSU=m CC=clang make -j "$(nproc)"', arguments[-1])
        self.assertIn('KSU_PACKAGE_NAME="$KSU_PACKAGE_NAME"', arguments[-1])
        self.assertIn('KSU_EXPECTED_SIZE="$KSU_EXPECTED_SIZE"', arguments[-1])
        self.assertIn('KSU_EXPECTED_HASH="$KSU_EXPECTED_HASH"', arguments[-1])
        self.assertIn('KSU_VERSION="$KSU_VERSION"', arguments[-1])
        self.assertIn('git config --global --add safe.directory "$(dirname "$PWD")"', arguments[-1])
        self.assertIn('umask 022', arguments[-1])
        self.assertIn('chmod a+r "$MODULE_OUTPUT"', arguments[-1])
        self.assertIn('python3 tools/prepare_manager_lkm.py version-code', script)
        self.assertLess(script.index('prepare_manager_lkm.py version-code'), script.index('docker run'))
        self.assertLess(arguments[-1].index('make -j'), arguments[-1].index('cp kernelsu.ko'))
        self.assertLess(arguments[-1].index('cp kernelsu.ko'), arguments[-1].index('llvm-strip -d'))
        serialized = json.dumps(job)
        for private in ('secrets.', 'GITHUB_TOKEN', 'GH_TOKEN', 'SUKISU_APK_KEYSTORE', 'SUKISU_APK_PASSWORD'):
            self.assertNotIn(private, serialized)
        self.assertIn('--expected-ref "$KSU_REF"', script)
        self.assertIn('--ddk-image "$DDK_IMAGE"', script)

    def test_public_identity_step_executes_with_exact_canonical_fields_and_rejects_bad_cert(self):
        step = next(step for step in jobs.manager_lkm_job('true')['steps']
                    if step['name'] == '核实管理器公开签名身份')
        script = step['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        environment = {'MANAGER_PACKAGE': 'com.cyzrqh.gki.sukisu',
                       'MANAGER_CERT_SIZE': '730', 'MANAGER_CERT_SHA256': 'b' * 64}
        previous = Path.cwd()
        try:
            os.chdir(self.directory)
            with patch.dict(os.environ, environment):
                exec(compile(script, '<public-identity-step>', 'exec'), {})
            identity = json.loads(Path('manager-public-certificate.json').read_text())
            self.assertEqual({'manager_package': environment['MANAGER_PACKAGE'],
                              'manager_cert_size': '730', 'manager_cert_sha256': 'b' * 64}, identity)
            with patch.dict(os.environ, {**environment, 'MANAGER_CERT_SHA256': 'not-a-certificate-hash'}):
                with self.assertRaisesRegex(ValueError, 'actual manager'):
                    exec(compile(script, '<public-identity-step>', 'exec'), {})
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
