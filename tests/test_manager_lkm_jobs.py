"""Validate pinned DDK selection and the exact-cache producer/consumer contract."""
import copy
import contextlib
import hashlib
import io
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


def github_exact_file_hash(root: Path, paths: list[str]) -> str:
    """The hashFiles algorithm for this key's two exact paths, in argument order.

    @actions/glob hashes each matched file, then SHA256s those binary digests:
    https://github.com/actions/toolkit/blob/main/packages/glob/src/internal-hash-files.ts
    No wildcard/ancestor traversal is involved in these two production inputs.
    """
    combined, matches = hashlib.sha256(), 0
    for relative in dict.fromkeys(paths):
        path = root / relative
        if path.is_file():
            combined.update(hashlib.sha256(path.read_bytes()).digest())
            matches += 1
    return combined.hexdigest() if matches else ''


def render_key(template, *, root=ROOT, **changes):
    values = {'runner.os': 'Linux', 'runner.arch': 'X64',
              'needs.prepare-matrix.outputs.ksu_ref': 'a' * 40,
              'needs.prepare-matrix.outputs.manager_package': 'com.sukisu.ultra',
              'needs.prepare-matrix.outputs.manager_cert_size': '730',
              'needs.prepare-matrix.outputs.manager_cert_sha256': 'b' * 64,
              'matrix.kmi': 'android12-5.10', 'matrix.ddk_short': 'c' * 16,
              'inputs.kernelsu_variant': 'SukiSU'}
    values.update(changes)
    def replace(match):
        expression = match[1].strip()
        port = re.fullmatch(r"inputs\.kernelsu_variant == 'BakaSU' && "
                            r"format\('-kpm-\{0\}', hashFiles\((.*?)\)\) \|\| ''", expression)
        if port:
            paths = re.findall(r"'([^']+)'", port[1])
            if ', '.join(repr(path) for path in paths) != port[1]:
                raise ValueError('Unsupported hashFiles path expression in the actual cache key')
            return ('-kpm-' + github_exact_file_hash(Path(root), paths)
                    if values['inputs.kernelsu_variant'] == 'BakaSU' else '')
        extra = re.fullmatch(r"inputs\.kernelsu_variant != 'SukiSU' && inputs\.kernelsu_variant != 'BakaSU' && "
                             r"format\('-kpm-\{0\}', hashFiles\((.*?)\)\) \|\| ''", expression)
        if extra:
            paths = re.findall(r"'([^']+)'", extra[1])
            if ', '.join(repr(path) for path in paths) != extra[1]:
                raise ValueError('Unsupported additional port hash expression')
            return ('-kpm-' + github_exact_file_hash(Path(root), paths)
                    if values['inputs.kernelsu_variant'] not in ('SukiSU', 'BakaSU') else '')
        return expression[1:-1] if expression.startswith("'") and expression.endswith("'") else values[expression]
    return re.sub(r'\$\{\{(.*?)\}\}', replace, template)


def replay_cache_steps(steps, responses):
    """Replay generated descriptors and execute their actual final Python gate.

    Only the GitHub cache service and wait clock are simulated. No kernel,
    module compiler, provenance verifier or final acceptance gate is mocked.
    """
    outputs, requests, delays = {}, [], []
    result = {'requests': requests, 'delays': delays, 'exit': 0}
    for step in steps:
        if 'if' in step:
            checks = []
            for part in step['if'].split(' && '):
                match = re.fullmatch(r"steps\.([a-z0-9-]+)\.outputs\.cache-hit != 'true'", part)
                if match is None:
                    raise AssertionError('Unexpected generated retry condition: ' + part)
                checks.append(outputs.get(match[1], {}).get('cache-hit', '') != 'true')
            if not all(checks):
                continue
        if step.get('uses') == 'actions/cache/restore@v5':
            requests.append(step['with'])
            outputs[step['id']] = responses[len(requests) - 1] if len(requests) <= len(responses) else {}
        elif step['run'].startswith('sleep '):
            delays.append(int(step['run'].split()[1]))
        else:
            environment = {}
            for field, value in step['env'].items():
                match = re.fullmatch(r'\$\{\{ steps\.([a-z0-9-]+)\.outputs\.([a-z-]+) \}\}', value)
                environment[field] = outputs.get(match[1], {}).get(match[2], '') if match else value
            command = step['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
            try:
                with patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
                    exec(compile(command, '<lkm-exact-cache-gate>', 'exec'), {})
            except SystemExit as error:
                result['exit'] = error.code
    return result


class ManagerLkmJobTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / 'tests' / ('.lkm-jobs-' + uuid.uuid4().hex)
        (self.directory / 'config').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.lock = {'schema_version': 1, 'images': [
            {'kmi': kmi, 'tag': f'ghcr.io/ylarod/ddk-min:{kmi}-20260828',
             'image': 'ghcr.io/ylarod/ddk-min@sha256:' + str(index) * 64,
             'kernel_release': kmi.split('-')[1] + '.123-dirty'}
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
            self.assertEqual({'kmi', 'image', 'ddk_short', 'kernel_release'}, set(row))
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
            ('kernel_release', ''), ('kernel_release', None), ('kernel_release', '5.15.123'),
            ('kernel_release', '5.10.123 SMP'), ('kernel_release', '5.10.123\n'),
            ('kernel_release', '5.10.123/unsafe'), ('kernel_release', '5.10.123-' + 'a' * 64),
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

    def test_sukisu_keys_remain_byte_for_byte_identical_to_the_existing_v2_identity(self):
        template = jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short')
        expected = ('gki-sukisu-lkm-v2-Linux-X64-' + 'a' * 40 + '-com.sukisu.ultra-730-'
                    + 'b' * 64 + '-android12-5.10-' + 'c' * 16)
        with patch(__name__ + '.github_exact_file_hash', side_effect=AssertionError('Suki must not hash a Baka port')):
            self.assertEqual(expected, render_key(template))

    def test_bakasu_port_bytes_invalidate_its_old_cache_without_changing_source_or_cert(self):
        patch_file = self.directory / 'tools/patches/bakasu-kpm.patch'
        patch_file.parent.mkdir(parents=True)
        port_lock = self.directory / 'config/bakasu-kpm-port.json'
        patch_file.write_bytes(b'audited-port-patch-A\n')
        port_lock.write_bytes(b'{"reviewed_port": 1}\n')
        identity = {'inputs.kernelsu_variant': 'BakaSU',
                    'needs.prepare-matrix.outputs.manager_package': 'com.resukisu.resukisu'}
        template = jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short')
        fixed = jobs.lkm_cache_key("'android12-5.10'", "'" + 'c' * 16 + "'")
        old = ('gki-sukisu-lkm-v2-Linux-X64-' + 'a' * 40 + '-com.resukisu.resukisu-730-'
               + 'b' * 64 + '-android12-5.10-' + 'c' * 16)
        digest = hashlib.sha256(hashlib.sha256(patch_file.read_bytes()).digest()
                                + hashlib.sha256(port_lock.read_bytes()).digest()).hexdigest()
        baseline = render_key(template, root=self.directory, **identity)
        self.assertEqual(old + '-kpm-' + digest, baseline)
        self.assertEqual(baseline, render_key(fixed, root=self.directory, **identity))
        self.assertNotEqual(old, baseline)
        for path in (patch_file, port_lock):
            before = path.read_bytes()
            path.write_bytes(before + b'changed')
            with self.subTest(path=path.name):
                self.assertNotEqual(baseline, render_key(template, root=self.directory, **identity))
                self.assertEqual(render_key(template), render_key(template, root=self.directory))
            path.write_bytes(before)

    def test_port_key_retains_all_native_identities_and_stays_within_cache_byte_limit(self):
        template = jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short')
        for variant, package in (('SukiSU', 'com.sukisu.ultra'), ('BakaSU', 'com.resukisu.resukisu')):
            identity = {'inputs.kernelsu_variant': variant,
                        'needs.prepare-matrix.outputs.manager_package': package,
                        'needs.prepare-matrix.outputs.manager_cert_size': '4096',
                        'runner.os': 'Windows', 'runner.arch': 'ARM64'}
            baseline = render_key(template, **identity)
            for field, value in (('needs.prepare-matrix.outputs.ksu_ref', 'd' * 40),
                                 ('needs.prepare-matrix.outputs.manager_cert_sha256', 'e' * 64),
                                 ('needs.prepare-matrix.outputs.manager_cert_size', '730'),
                                 ('matrix.ddk_short', 'f' * 16)):
                with self.subTest(variant=variant, field=field):
                    self.assertNotEqual(baseline, render_key(template, **{**identity, field: value}))
            for kmi in jobs.KMIS:
                value = render_key(template, **{**identity, 'matrix.kmi': kmi})
                self.assertLessEqual(len(value.encode('utf-8')), 512)

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
        caches = [step for step in steps if step.get('uses', '').startswith('actions/cache/')
                  and step.get('with', {}).get('path', '').startswith('manager-lkm/')]
        self.assertEqual(['actions/cache/restore@v5', 'actions/cache/save@v5',
                          *['actions/cache/restore@v5'] * 3],
                         [step['uses'] for step in caches])
        for step in caches:
            self.assertNotIn('restore-keys', step['with'])
            self.assertEqual('manager-lkm/${{ matrix.kmi }}', step['with']['path'])
            self.assertEqual(jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short'), step['with']['key'])
        for step in caches[:2]:
            self.assertNotIn('continue-on-error', step)
        for step in caches[2:]:
            self.assertTrue(step['with']['lookup-only'])
            self.assertFalse(step['with']['fail-on-cache-miss'])
            self.assertTrue(step['continue-on-error'])
        self.assertEqual('lkm-published-ready', steps[-1]['id'])
        self.assertNotIn('continue-on-error', steps[-1])
        self.assertIn('raise SystemExit', steps[-1]['run'])
        self.assertFalse(any('upload-artifact' in step.get('uses', '') for step in steps))
        self.assertFalse(steps[0]['with']['persist-credentials'])
        hit = next(step for step in steps if '--expected-kmis "$KMI"' in step.get('run', ''))
        build = next(step for step in steps if 'docker run' in step.get('run', ''))
        self.assertIn("cache-hit == 'true'", hit['if'])
        self.assertIn("cache-hit != 'true'", build['if'])
        self.assertIn("cache-hit != 'true'", caches[1]['if'])
        self.assertLess(steps.index(hit), steps.index(build))
        self.assertLess(steps.index(build), steps.index(caches[1]))

    def test_cache_visibility_lag_retries_exact_key_and_stops_on_later_hit(self):
        key = render_key(jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short'))
        for lookup_only in (True, False):
            steps = jobs.lkm_cache_restore_steps(key, 'manager-lkm/android12-5.10',
                id_prefix='retry-kmi', name='strict cache', lookup_only=lookup_only)
            for responses in ([{}, {'cache-hit': 'true', 'cache-matched-key': key}],
                              [{}, {}, {'cache-hit': 'true', 'cache-matched-key': key}]):
                replay = replay_cache_steps(steps, responses)
                with self.subTest(lookup_only=lookup_only, responses=responses):
                    self.assertEqual(0, replay['exit'])
                    self.assertEqual(len(responses), len(replay['requests']))
                    self.assertEqual([10] if len(responses) == 2 else [10, 20], replay['delays'])
                    self.assertTrue(all(request['key'] == key and request['lookup-only'] == lookup_only
                                        and not request['fail-on-cache-miss'] for request in replay['requests']))
            immediate = replay_cache_steps(steps, [{'cache-hit': 'true', 'cache-matched-key': key}])
            self.assertEqual(0, immediate['exit'])
            self.assertEqual(1, len(immediate['requests']))
            self.assertEqual([], immediate['delays'])

    def test_all_cache_misses_fail_after_bounded_retries_without_another_build_or_transport(self):
        key = render_key(jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short'))
        steps = jobs.lkm_cache_restore_steps(key, 'manager-lkm/android12-5.10',
            id_prefix='bounded', name='strict cache', lookup_only=True)
        replay = replay_cache_steps(steps, [{}, {}, {}])
        self.assertIn('three bounded attempts', replay['exit'])
        self.assertEqual(3, len(replay['requests']))
        self.assertEqual([10, 20], replay['delays'])
        self.assertEqual(6, len(steps))
        for step in steps:
            if 'uses' in step:
                self.assertEqual('actions/cache/restore@v5', step['uses'])
                self.assertEqual(3, step['timeout-minutes'])
                self.assertNotIn('restore-keys', step['with'])
            else:
                self.assertTrue(step['run'].startswith(('sleep ', 'python3 -')))
        self.assertNotIn('continue-on-error', steps[-1])

    def test_a_cache_hit_for_another_source_signature_or_kmi_never_passes_the_gate(self):
        key = render_key(jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short'))
        steps = jobs.lkm_cache_restore_steps(key, 'manager-lkm/android12-5.10',
            id_prefix='identity', name='strict cache')
        wrong = (render_key(jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short'),
                            **{'needs.prepare-matrix.outputs.ksu_ref': 'f' * 40}),
                 render_key(jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short'),
                            **{'needs.prepare-matrix.outputs.manager_cert_sha256': 'e' * 64}),
                 render_key(jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short'),
                            **{'matrix.kmi': 'android13-5.15'}))
        for matched in wrong:
            with self.subTest(matched=matched):
                # A partial service match is never enough. Even an incorrectly
                # reported cache-hit=true cannot bypass the exact-key gate.
                for hit in ('false', 'true'):
                    replay = replay_cache_steps(steps, [{'cache-hit': hit, 'cache-matched-key': matched}] * 3)
                    self.assertIn('no other source or signature cache', replay['exit'])
                    self.assertTrue(all(request['key'] == key for request in replay['requests']))

    def test_fixed_consumers_and_matrix_producer_retry_the_identical_native_identity(self):
        for variant in ('SukiSU', 'BakaSU'):
            for row in jobs.load_ddk_matrix(ROOT):
                dynamic = jobs.lkm_cache_key('matrix.kmi', 'matrix.ddk_short')
                fixed = jobs.lkm_cache_key(repr(row['kmi']), repr(row['ddk_short']))
                fields = {'inputs.kernelsu_variant': variant, 'matrix.kmi': row['kmi'],
                          'matrix.ddk_short': row['ddk_short']}
                producer = jobs.lkm_cache_restore_steps(dynamic, 'manager-lkm/' + row['kmi'],
                    id_prefix='producer', name='publish', lookup_only=True)
                consumer = jobs.lkm_cache_restore_steps(fixed, 'manager-lkm/' + row['kmi'],
                    id_prefix='consumer', name='restore')
                with self.subTest(variant=variant, kmi=row['kmi']):
                    expected = render_key(dynamic, **fields)
                    for step in producer + consumer:
                        if 'uses' in step:
                            self.assertEqual(expected, render_key(step['with']['key'], **fields))
                            self.assertNotIn('restore-keys', step['with'])
                    self.assertEqual(expected, render_key(consumer[-1]['env']['EXPECTED_CACHE_KEY'], **fields))
        optional = jobs.lkm_cache_restore_steps('exact-key', 'manager-lkm/kmi',
            id_prefix='optional', name='restore', condition='inputs.build_manager')
        self.assertEqual('inputs.build_manager', optional[-1]['if'])
        self.assertTrue(all('inputs.build_manager' in step['if'] for step in optional))

    def test_source_uses_exact_prepared_cache_key_and_never_downloads_an_artifact(self):
        steps = jobs.manager_lkm_job('true')['steps']
        source = next(step for step in steps if step.get('with', {}).get('path') == jobs.SOURCE_BUNDLE_PATH)
        self.assertEqual('actions/cache/restore@v5', source['uses'])
        self.assertEqual('${{ needs.prepare-matrix.outputs.source_cache_key }}', source['with']['key'])
        self.assertFalse(source['with']['fail-on-cache-miss'])
        self.assertNotIn('restore-keys', source['with'])
        self.assertTrue(source['continue-on-error'])
        self.assertNotIn('actions/download-artifact', json.dumps(steps))
        verify = next(step for step in steps if 'transfer_ksu_snapshot.py verify-cache' in step.get('run', ''))
        checkout = next(step for step in steps if 'checkout_ksu_snapshot.py' in step.get('run', ''))
        self.assertLess(steps.index(source), steps.index(verify))
        self.assertLess(steps.index(verify), steps.index(checkout))
        self.assertNotIn('if', verify)
        self.assertNotIn('continue-on-error', verify)
        self.assertEqual('${{ needs.prepare-matrix.outputs.source_identity }}', verify['env']['PREPARED_SOURCE_IDENTITY'])
        self.assertIn('--source "$GITHUB_WORKSPACE/ksu-source-bundle"', verify['run'])
        self.assertIn('--prepared-identity "$PREPARED_SOURCE_IDENTITY"', verify['run'])

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
        self.assertEqual({'HOME', 'KSU_PACKAGE_NAME', 'KSU_MANAGER_PACKAGE', 'KSU_VARIANT',
                          'KSU_EXPECTED_SIZE', 'KSU_EXPECTED_HASH', 'KSU_VERSION', 'MODULE_OUTPUT'}, passed)
        self.assertIn('if [ "$KSU_VARIANT" = BakaSU ]; then', arguments[-1])
        self.assertIn('CONFIG_KSU_MULTI_MANAGER_SUPPORT=y', arguments[-1])
        self.assertIn('CONFIG_KSU_TRACEPOINT_HOOK=y', arguments[-1])
        baka = arguments[-1].split('if [ "$KSU_VARIANT" = BakaSU ]; then', 1)[1].split('    fi', 1)[0]
        self.assertIn('CONFIG_KPM=y', baka)
        self.assertEqual(3, arguments[-1].count('CONFIG_KPM=y'))
        self.assertIn('KSU_NEXT_MANAGER_SIZE="$KSU_EXPECTED_SIZE"', arguments[-1])
        self.assertIn('KSU_NEXT_MANAGER_HASH="$KSU_EXPECTED_HASH"', arguments[-1])
        self.assertIn('KSU_MANAGER_PACKAGE="$KSU_MANAGER_PACKAGE"', arguments[-1])
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
        serialized = json.dumps(build)
        for private in ('secrets.', 'GITHUB_TOKEN', 'GH_TOKEN', 'SUKISU_APK_KEYSTORE', 'SUKISU_APK_PASSWORD'):
            self.assertNotIn(private, serialized)
        for private in ('SUKISU_APK_KEYSTORE', 'SUKISU_APK_PASSWORD'):
            self.assertNotIn(private, json.dumps(job))
        fallback = next(step for step in job['steps'] if 'transfer_ksu_snapshot.py restore-official' in step.get('run', ''))
        self.assertEqual(fallback['env']['GH_TOKEN'], '${{ secrets.UPSTREAM_SYNC_TOKEN || github.token }}')
        self.assertNotIn('SOURCE_BACKUP_RELEASE_ID', fallback['env'])
        self.assertNotIn('--release-id', fallback['run'])
        self.assertIn('--expected-ref "$KSU_REF"', fallback['run'])
        self.assertIn('--prepared-identity "$PREPARED_SOURCE_IDENTITY"', fallback['run'])
        self.assertEqual(job['permissions'], {'contents': 'read'})
        self.assertIn('--expected-ref "$KSU_REF"', script)
        self.assertIn('--ddk-image "$DDK_IMAGE"', script)

    def test_public_identity_step_executes_with_exact_canonical_fields_and_rejects_bad_cert(self):
        step = next(step for step in jobs.manager_lkm_job('true')['steps']
                    if step['name'] == '核实管理器公开签名身份')
        script = step['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        environment = {'MANAGER_PACKAGE': 'com.sukisu.ultra',
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
