import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import optimize_manifest_fetch as optimize
from set_kernel_ref import _load_manifests


def checkout_semantics(directory):
    manifests = _load_manifests(directory)
    defaults = [node for _, tree in manifests for node in tree.getroot().findall('default')]
    default = defaults[0] if defaults else ET.Element('default')
    states = {}
    for _, tree in manifests:
        for project in tree.getroot().findall('project'):
            states[project.get('name')] = {
                'revision': project.get('revision') or default.get('revision'),
                'upstream': project.get('upstream') or default.get('upstream'),
                'other_attributes': {key: value for key, value in project.attrib.items() if key != 'upstream'},
                'children': tuple(ET.tostring(child) for child in project),
            }
    return states


class ManifestFetchTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.manifest-fetch-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def bytes(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*.xml')}

    def assert_unchanged_checkouts(self, before, after):
        self.assertEqual(set(before), set(after))
        for name, state in before.items():
            self.assertEqual(state['revision'], after[name]['revision'], name)
            self.assertEqual(state['other_attributes'], after[name]['other_attributes'], name)
            self.assertEqual(state['children'], after[name]['children'], name)

    def test_real_june_and_august_manifests_fetch_only_pinned_sha_and_keep_checkouts(self):
        for month, upstream in (('06', 'master'), ('08', 'master-kernel-build-2022')):
            with self.subTest(month=month):
                fixture = ROOT / 'tests/fixtures/manifest-fetch' / ('android13-5.15-2022-' + month + '.xml')
                self.write('default.xml', fixture.read_text(encoding='utf-8'))
                before = checkout_semantics(self.root)
                clang = before['platform/prebuilts/clang/host/linux-x86']
                self.assertEqual(clang['upstream'], upstream)
                self.assertRegex(clang['revision'], r'^[0-9a-f]{40}$')
                # repo 2.16 _RemoteFetch's SHA+depth path appends revision, then
                # upstream if present. This is the exact redundant fetch input.
                self.assertEqual([clang['revision'], clang['upstream']], [clang['revision'], upstream])
                result = optimize.optimize_manifest_fetch(self.root)
                self.assertEqual(result['status'], 'optimized')
                self.assertGreaterEqual(result['optimized_projects'], 15)
                self.assertEqual(result['changed_files'], 1)
                after = checkout_semantics(self.root)
                self.assert_unchanged_checkouts(before, after)
                restored_clang = after['platform/prebuilts/clang/host/linux-x86']
                actual_fetch = [restored_clang['revision']] + ([restored_clang['upstream']] if restored_clang['upstream'] else [])
                self.assertEqual(actual_fetch, [clang['revision']])
                self.assertEqual(before['kernel/common'], after['kernel/common'])
                self.assertIn('Official source:', (self.root / 'default.xml').read_text(encoding='utf-8'))

    def test_branch_tag_and_inherited_revisions_retain_effective_upstream(self):
        self.write('default.xml', '<manifest><remote name="aosp" fetch=".."/>'
                   '<default revision="inherited-branch" upstream="master" remote="aosp" sync-j="4"/>'
                   '<project name="fixed" revision="' + 'A' * 40 + '" groups="g" upstream="old"/>'
                   '<project name="branch" revision="refs/heads/main"/>'
                   '<project name="tag" revision="refs/tags/v1" upstream="release"/>'
                   '<project name="inherited"/></manifest>')
        before = checkout_semantics(self.root)
        result = optimize.optimize_manifest_fetch(self.root)
        self.assertEqual(result['optimized_projects'], 1)
        self.assertEqual(result['preserved_upstreams'], 2)
        after = checkout_semantics(self.root)
        self.assert_unchanged_checkouts(before, after)
        self.assertIsNone(after['fixed']['upstream'])
        for name in ('branch', 'tag', 'inherited'):
            self.assertEqual(before[name], after[name])

    def test_only_explicit_complete_40_or_64_character_commits_are_optimized(self):
        self.write('default.xml', '<manifest><default upstream="master"/>' + ''.join(
            '<project name="p' + str(index) + '" revision="' + value + '"/>'
            for index, value in enumerate(('a' * 40, 'B' * 64, 'c' * 39, 'd' * 41,
                                           'e' * 63, 'f' * 65, 'main', 'refs/tags/release'))) + '</manifest>')
        before = checkout_semantics(self.root)
        result = optimize.optimize_manifest_fetch(self.root)
        self.assertEqual(result['optimized_projects'], 2)
        after = checkout_semantics(self.root)
        self.assert_unchanged_checkouts(before, after)
        for index in range(8):
            self.assertEqual(after['p' + str(index)]['upstream'], None if index < 2 else 'master')

    def test_default_sha_is_preserved_when_only_explicit_fixed_projects_are_changed(self):
        self.write('default.xml', '<manifest><default revision="' + 'b' * 40 + '" upstream="master"/>'
                   '<project name="explicit" revision="' + 'a' * 40 + '"/><project name="inherited"/></manifest>')
        before = checkout_semantics(self.root)
        result = optimize.optimize_manifest_fetch(self.root)
        self.assertEqual(result['optimized_projects'], 1)
        self.assertEqual(checkout_semantics(self.root)['inherited'], before['inherited'])

    def test_includes_keep_attributes_children_comments_and_unreachable_files(self):
        self.write('default.xml', '<manifest><!-- keep --><default upstream="main"/>'
                   '<include name="parts/projects.xml" groups="group-a"/></manifest>')
        self.write('parts/projects.xml', '<manifest><project name="fixed" path="compiler" revision="' + 'a' * 40 + '">'
                   '<linkfile src="a" dest="b"/><copyfile src="c" dest="d"/>'
                   '<annotation name="test" value="kept"/></project><project name="branch" revision="stable"/></manifest>')
        ignored = self.write('unreachable.xml', '<manifest><project name="unused" revision="' + 'a' * 40 + '" upstream="unchanged"/></manifest>')
        ignored_before = ignored.read_bytes()
        before = checkout_semantics(self.root)
        result = optimize.optimize_manifest_fetch(self.root)
        self.assertEqual(result['changed_files'], 2)
        after = checkout_semantics(self.root)
        self.assert_unchanged_checkouts(before, after)
        self.assertEqual(before['branch'], after['branch'])
        self.assertEqual(ignored.read_bytes(), ignored_before)
        self.assertIn('<!-- keep -->', (self.root / 'default.xml').read_text())
        self.assertEqual(ET.parse(self.root / 'default.xml').find('include').attrib,
                         {'name': 'parts/projects.xml', 'groups': 'group-a'})

    def test_unsupported_layouts_skip_the_entire_graph_without_writing(self):
        variants = ('<extend-project name="fixed" revision="other"/>', '<remove-project name="fixed"/>',
                    '<submanifest name="extra"/>', '<future-project name="extra"/>',
                    '<remote name="aosp" fetch=".." revision="remote-default"/>',
                    '<remote name="aosp" fetch=".."><project name="nested" revision="main"/></remote>',
                    '<default upstream="another"/>',
                    '<project name="outer"><project name="inner" revision="' + 'b' * 40 + '"/></project>',
                    '<include name="parts.xml" revision="override"/>')
        self.write('parts.xml', '<manifest/>')
        for extra in variants:
            with self.subTest(extra=extra):
                self.write('default.xml', '<manifest><default upstream="main"/>'
                           '<project name="fixed" revision="' + 'a' * 40 + '"/>' + extra + '</manifest>')
                before = self.bytes()
                result = optimize.optimize_manifest_fetch(self.root)
                self.assertEqual(result['status'], 'skipped')
                self.assertEqual(result['changed_files'], 0)
                self.assertEqual(self.bytes(), before)

    def test_unsafe_missing_cyclic_or_malformed_includes_skip_without_mutation(self):
        for target, included in (('../escape.xml', '<manifest/>'), ('missing.xml', None),
                                 ('parts.xml', '<manifest><include name="default.xml"/></manifest>'),
                                 ('parts.xml', 'invalid xml')):
            if included is not None:
                self.write('parts.xml', included)
            self.write('default.xml', '<manifest><default upstream="main"/><project name="fixed" revision="' +
                       'a' * 40 + '"/><include name="' + target + '"/></manifest>')
            before = self.bytes()
            with self.subTest(target=target):
                self.assertEqual(optimize.optimize_manifest_fetch(self.root)['status'], 'skipped')
                self.assertEqual(self.bytes(), before)

    def test_repeated_optimization_is_safe_and_unchanged_files_are_not_rewritten(self):
        path = self.write('default.xml', '<manifest><default upstream="main"/>'
                          '<project name="fixed" revision="' + 'a' * 40 + '"/><project name="branch" revision="main"/></manifest>')
        self.assertEqual(optimize.optimize_manifest_fetch(self.root)['status'], 'optimized')
        before, timestamp = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(optimize.optimize_manifest_fetch(self.root)['status'], 'unchanged')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, timestamp)
        self.assertFalse(list(self.root.rglob('.gki-fetch-*')))

    def test_symlinked_manifest_or_root_is_skipped_without_modification(self):
        original = self.write('source.xml', '<manifest><default upstream="main"/>'
                              '<project name="fixed" revision="' + 'a' * 40 + '"/></manifest>')
        link = self.root / 'default.xml'
        try:
            link.symlink_to(original)
        except (OSError, NotImplementedError):
            self.skipTest('Creating symlinks is unavailable on this host')
        before = original.read_bytes()
        self.assertEqual(optimize.optimize_manifest_fetch(self.root)['status'], 'skipped')
        self.assertEqual(original.read_bytes(), before)
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(optimize.optimize_manifest_fetch(alias)['status'], 'skipped')
        self.assertEqual(original.read_bytes(), before)

    def test_nonfixed_only_manifest_retains_default_upstream_without_rewrite(self):
        path = self.write('default.xml', '<manifest><default upstream="main"/>'
                          '<project name="branch" revision="main"/></manifest>')
        before = path.read_bytes()
        self.assertEqual(optimize.optimize_manifest_fetch(self.root)['status'], 'unchanged')
        self.assertEqual(path.read_bytes(), before)

    def test_late_replace_failure_rolls_back_all_previously_changed_files(self):
        self.write('default.xml', '<manifest><default upstream="main"/><include name="part.xml"/></manifest>')
        self.write('part.xml', '<manifest><project name="fixed" revision="' + 'a' * 40 + '" upstream="old"/></manifest>')
        before = self.bytes()
        replace = os.replace
        calls = []
        def fail_second(source, destination):
            calls.append(destination)
            if len(calls) == 2:
                raise OSError('fixture: second file cannot be replaced')
            return replace(source, destination)
        with mock.patch.object(optimize.os, 'replace', side_effect=fail_second):
            self.assertEqual(optimize.optimize_manifest_fetch(self.root)['status'], 'skipped')
        self.assertEqual(self.bytes(), before)
        self.assertEqual(len(calls), 3)  # Two writes and one rollback.
        self.assertFalse(list(self.root.rglob('.gki-fetch-*')))

    def test_cli_skip_is_successful_and_reports_counts_without_blocking_build(self):
        self.write('default.xml', '<manifest><future-layout/></manifest>')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(optimize.main(['--manifest-root', str(self.root)]), 0)
        report = json.loads(output.getvalue().split('Manifest fetch optimization: ', 1)[1])
        self.assertEqual(report['status'], 'skipped')
        self.assertEqual(report['optimized_projects'], 0)

    def test_failed_rollback_is_fatal_and_keeps_recoverable_original_backup(self):
        self.write('default.xml', '<manifest><default upstream="main"/><include name="part.xml"/></manifest>')
        self.write('part.xml', '<manifest><project name="fixed" revision="' + 'a' * 40 + '" upstream="old"/></manifest>')
        before = self.bytes()
        replace = os.replace
        calls = []
        def failure(source, destination):
            calls.append(destination)
            if len(calls) >= 2:
                raise OSError('fixture: write and rollback both unavailable')
            return replace(source, destination)
        with mock.patch.object(optimize.os, 'replace', side_effect=failure):
            with self.assertRaisesRegex(optimize.ManifestWriteError, 'original backups retained'):
                optimize.optimize_manifest_fetch(self.root)
        backups = list(self.root.rglob('.gki-fetch-original-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before['part.xml'])
        self.assertEqual((self.root / 'default.xml').read_bytes(), before['default.xml'])
        with mock.patch.object(optimize, 'optimize_manifest_fetch', side_effect=optimize.ManifestWriteError('recover from backup')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(optimize.main(['--manifest-root', str(self.root)]), 1)

    def test_cleanup_error_cannot_hide_failed_rollback_or_its_retained_original(self):
        self.write('default.xml', '<manifest><default upstream="main"/><include name="part.xml"/></manifest>')
        self.write('part.xml', '<manifest><project name="fixed" revision="' + 'a' * 40 + '" upstream="old"/></manifest>')
        original = (self.root / 'part.xml').read_bytes()
        replace = os.replace
        calls = []
        def failure(source, destination):
            calls.append(destination)
            if len(calls) >= 2:
                raise OSError('fixture: write and rollback both unavailable')
            return replace(source, destination)
        output = io.StringIO()
        with mock.patch.object(optimize.os, 'replace', side_effect=failure), \
                mock.patch.object(Path, 'unlink', side_effect=OSError('fixture: cleanup also unavailable')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(optimize.main(['--manifest-root', str(self.root)]), 1)
        self.assertIn('original backups retained', output.getvalue())
        self.assertNotIn('"status": "skipped"', output.getvalue())
        backups = list(self.root.rglob('.gki-fetch-original-*'))
        self.assertIn(original, [path.read_bytes() for path in backups])


if __name__ == '__main__':
    unittest.main()
