"""Keep selected managers behind one complete verification/publication gate."""
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from combined_manager_workflow import add_combined
from generate_workflow import create_workflow
from manager_variants import COMBINED_GROUPS, MANAGERS
from oplus_workflow import create_workflow as create_oplus_workflow


def gate_allows(condition, *, results, overflow_count='0', operation='编译内核', cancelled=False):
    """Evaluate only the status/input expressions used by the publication job."""
    condition = re.sub(r'needs\.([A-Za-z0-9-]+)\.result',
                       lambda match: repr(results[match[1]]), condition)
    for before, after in (
            ('needs.prepare-matrix.outputs.overflow_count', repr(overflow_count)),
            ('inputs.operation', repr(operation)),
            ('github.event_name', repr('workflow_dispatch')),
            ('always()', 'True'), ('cancelled()', repr(cancelled))):
        condition = condition.replace(before, after)
    condition = condition.replace('&&', ' and ').replace('||', ' or ')
    condition = re.sub(r'!(?!=)', 'not ', condition)
    return bool(eval(condition, {'__builtins__': {}}, {}))


def strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings_in(child)


def selection_value(expression, selection):
    expression = expression.removeprefix('${{').removesuffix('}}').strip()
    expression = expression.replace('inputs.kernelsu_variant', repr(selection))
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    return eval(expression, {'__builtins__': {}, 'fromJSON': json.loads,
        'format': lambda template, *values: template.format(*values)}, {})


class CombinedReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gki = create_workflow(ROOT)
        cls.release_concurrency = gki['jobs']['publish-release']['concurrency']
        cls.workflows = [(False, add_combined(gki, oplus=False)[0]),
                         (True, add_combined(create_oplus_workflow(ROOT, gki), oplus=True)[0])]

    def publishers(self):
        for oplus, workflow in self.workflows:
            publisher_id = 'verify-oplus-assets' if oplus else 'publish-release'
            yield oplus, workflow, workflow['jobs'][publisher_id]

    def test_every_needed_kernel_batch_must_succeed_before_any_artifact_verification(self):
        for oplus, _, publisher in self.publishers():
            kernel = 'build-oplus-kernel' if oplus else 'build-kernel'
            overflow = kernel + '-overflow'
            self.assertEqual({'prepare-matrix', 'build-sukisu-manager', kernel, overflow},
                             set(publisher['needs']))
            ready = {name: 'success' for name in publisher['needs']}
            self.assertTrue(gate_allows(publisher['if'], results=ready, overflow_count='1'))
            for name in ready:
                for result in ('failure', 'cancelled', 'skipped'):
                    with self.subTest(oplus=oplus, job=name, result=result):
                        self.assertFalse(gate_allows(publisher['if'],
                            results={**ready, name: result}, overflow_count='1'))
            self.assertFalse(gate_allows(publisher['if'], results=ready,
                                        overflow_count='1', cancelled=True))
            # An absent overflow batch is the only expected skipped kernel job.
            self.assertTrue(gate_allows(publisher['if'],
                                       results={**ready, overflow: 'skipped'}, overflow_count='0'))
            for result in ('failure', 'cancelled'):
                self.assertFalse(gate_allows(publisher['if'],
                    results={**ready, overflow: result}, overflow_count='0'))

    def test_gki_apk_only_verifies_selected_managers_without_requiring_kernel_jobs(self):
        _, _, publisher = next(self.publishers())
        results = {'prepare-matrix': 'success', 'build-sukisu-manager': 'success',
                   'build-kernel': 'skipped', 'build-kernel-overflow': 'skipped'}
        self.assertTrue(gate_allows(publisher['if'], results=results, operation='仅编译管理器安装包'))
        for dependency in ('prepare-matrix', 'build-sukisu-manager'):
            self.assertFalse(gate_allows(publisher['if'], results={**results, dependency: 'failure'},
                                        operation='仅编译管理器安装包'))

    def test_prepare_and_manager_matrices_schedule_exactly_the_selected_group(self):
        for oplus, workflow, _ in self.publishers():
            for selection in (*MANAGERS, *COMBINED_GROUPS):
                expected = list(COMBINED_GROUPS.get(selection, (selection,)))
                for name in ('prepare-matrix', 'build-sukisu-manager'):
                    with self.subTest(oplus=oplus, selection=selection, job=name):
                        job = workflow['jobs'][name]
                        self.assertEqual(expected,
                            selection_value(job['strategy']['matrix']['variant'], selection))
                        self.assertEqual('true' if selection in COMBINED_GROUPS else 'false',
                            selection_value(job['env']['GKI_COMBINED_WORKER'], selection))

    def test_one_raw_download_and_one_validation_receive_every_selected_producer_identity(self):
        for oplus, _, publisher in self.publishers():
            with self.subTest(oplus=oplus):
                self.assertNotIn('strategy', tuple(publisher))
                self.assertEqual(self.release_concurrency, publisher['concurrency'])
                self.assertIs(publisher['concurrency']['cancel-in-progress'], False)
                self.assertEqual({'contents': 'write', 'actions': 'write'}, publisher['permissions'])
                self.assertFalse('matrix.variant' in json.dumps(publisher),
                                 'The shared publisher must not depend on a manager matrix')
                downloads = [step for step in publisher['steps']
                             if step.get('uses') == 'actions/download-artifact@v8']
                self.assertEqual(1, len(downloads))
                self.assertEqual('{*.apk,*-AnyKernel3.zip,*-boot*.img}', downloads[0]['with']['pattern'])
                self.assertEqual('release-input', downloads[0]['with']['path'])
                self.assertIs(downloads[0]['with']['skip-decompress'], True)
                verifiers = [step for step in publisher['steps']
                             if 'tools/prepare_selected_release.py' in step.get('run', '')]
                self.assertEqual(1, len(verifiers))
                verifier = verifiers[0]
                self.assertTrue(verifier.get('id'))
                self.assertIn('python3 tools/prepare_selected_release.py --input release-input '
                              '--output release-publish', verifier['run'])
                self.assertNotIn('continue-on-error', verifier)
                environment = {**publisher.get('env', {}), **verifier.get('env', {})}
                # Passing the menu selection unchanged keeps single-manager builds compatible.
                self.assertEqual('${{ inputs.kernelsu_variant }}', environment['MANAGER_SELECTION'])
                self.assertEqual('oplus' if oplus else 'gki', environment['RELEASE_PLATFORM'])
                self.assertEqual('编译内核' if oplus else '${{ inputs.operation }}', environment['OPERATION'])
                for variant in MANAGERS:
                    prefix = 'MANAGER_' + variant.upper().replace('-', '_') + '_'
                    for suffix, job, output in (
                            ('APK_FILENAME', 'build-sukisu-manager', 'apk_filename'),
                            ('METADATA', 'build-sukisu-manager', 'manager_metadata'),
                            ('EXPECTED_COUNT', 'prepare-matrix', 'count'),
                            ('FULL_CATALOG', 'prepare-matrix', 'full_catalog')):
                        value = environment[prefix + suffix]
                        self.assertIn('needs.' + job + '.outputs', value)
                        self.assertIn(variant.lower() + '_' + output, value)
                if oplus:
                    self.assertIn('needs.prepare-matrix.outputs', environment['OPLUS_MATRIX'])
                    self.assertNotIn('matrix.variant', environment['OPLUS_MATRIX'])
                else:
                    self.assertNotIn('OPLUS_MATRIX', environment)

    def test_publisher_static_output_references_match_declared_dependencies(self):
        reference = re.compile(r'needs\.([A-Za-z0-9_-]+)\.outputs(?:\.([A-Za-z0-9_-]+)'
                               r'|\[(?P<quote>[\'"])([A-Za-z0-9_-]+)(?P=quote)\])')
        for oplus, workflow, publisher in self.publishers():
            matches = [match for value in strings_in(publisher) for match in reference.finditer(value)]
            self.assertGreaterEqual(len(matches), 4 * len(MANAGERS))
            for match in matches:
                dependency, output = match[1], match[2] or match[4]
                with self.subTest(oplus=oplus, dependency=dependency, output=output):
                    self.assertIn(dependency, publisher['needs'])
                    # Actions' output object uses normalized property names;
                    # bracketed string keys must match that lowercase spelling.
                    self.assertEqual(output.lower(), output)
                    declared = {key.lower() for key in workflow['jobs'][dependency].get('outputs', {})}
                    self.assertTrue(output in declared, 'Missing dependency output: ' + dependency + '.' + output)

    def test_oplus_matrix_selection_uses_the_selected_declared_lowercase_output(self):
        _, workflow, publisher = next(item for item in self.publishers() if item[0])
        verifier = next(step for step in publisher['steps']
                        if 'tools/prepare_selected_release.py' in step.get('run', ''))
        value = verifier['env']['OPLUS_MATRIX']
        match = re.fullmatch(r'\$\{\{\s*needs\.prepare-matrix\.outputs\[format\('
                             r'(?P<quote>[\'"])(?P<template>.*?)(?P=quote),\s*(?P<selector>.*)\)\]\s*\}\}', value)
        self.assertIsNotNone(match, 'OPlus must select one prepared manager matrix output')
        self.assertEqual('{0}_matrix', match['template'])
        declared = {key.lower() for key in workflow['jobs']['prepare-matrix']['outputs']}
        for selection in (*MANAGERS, *COMBINED_GROUPS):
            with self.subTest(selection=selection):
                variant = selection_value(match['selector'], selection)
                output = match['template'].format(variant)
                expected = COMBINED_GROUPS.get(selection, (selection,))[0].lower()
                self.assertEqual(expected + '_matrix', output)
                self.assertTrue(output in declared, 'Missing prepared OPlus matrix: ' + output)

    def test_unified_checksums_upload_before_the_only_tag_release_and_cleanup(self):
        for oplus, workflow, publisher in self.publishers():
            with self.subTest(oplus=oplus):
                self.assertNotIn('build-checksums', tuple(workflow['jobs']))
                steps = publisher['steps']
                assets_index = next(index for index, step in enumerate(steps)
                    if 'tools/prepare_selected_release.py' in step.get('run', ''))
                verifier_id = steps[assets_index]['id']
                uploads = [(index, step) for index, step in enumerate(steps)
                           if step.get('uses') == 'actions/upload-artifact@v7']
                self.assertEqual(1, len(uploads))
                upload_index, upload = uploads[0]
                self.assertEqual('release-publish/assets/SHA256SUMS.txt', upload['with']['path'])
                self.assertIs(upload['with']['archive'], False)
                self.assertEqual('error', upload['with']['if-no-files-found'])
                self.assertNotIn('continue-on-error', upload)
                self.assertLess(assets_index, upload_index)
                for step_id in ('release-tag', 'release'):
                    matches = [(index, step) for index, step in enumerate(steps)
                               if step.get('id') == step_id]
                    self.assertEqual(1, len(matches))
                    index, step = matches[0]
                    self.assertLess(upload_index, index)
                    self.assertIn("steps." + verifier_id + ".outputs.publish == 'true'", step['if'])
                    # With no failure-status function, Actions also requires the
                    # preceding checksum upload to succeed before publication.
                    self.assertNotRegex(step['if'], r'\b(always|failure|cancelled)\(')
                cleanups = [(index, step) for index, step in enumerate(steps)
                            if 'tools/cleanup_build_artifacts.py' in step.get('run', '')]
                self.assertEqual(1, len(cleanups))
                cleanup_index, cleanup = cleanups[0]
                release_index = next(index for index, step in enumerate(steps)
                                     if step.get('id') == 'release')
                self.assertLess(release_index, cleanup_index)
                self.assertEqual("steps.release.outcome == 'success'", cleanup['if'])
                self.assertEqual(1, sum(step.get('id') == 'release'
                    for job in workflow['jobs'].values() for step in job.get('steps', [])))
                release = steps[release_index]['run']
                self.assertIn('assets=(release-publish/assets/*)', release)
                self.assertIn('release-publish/title.txt', release)
                self.assertIn('release-publish/release.md', release)
                self.assertNotIn('oplus-report/', release)


if __name__ == '__main__':
    unittest.main()
