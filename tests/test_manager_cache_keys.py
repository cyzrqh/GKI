import re
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from manager_cache_keys import restore_keys
from manager_variants import MANAGERS


VARIANT = '${{ inputs.kernelsu_variant }}'
RUNNER = '${{ runner.os }}-${{ runner.arch }}-'
RUST = '${{ steps.manager-rust-version.outputs.version }}-'
HASH = "${{ hashFiles('manager-source/**/Cargo.lock', 'tools/build_manager_native.sh') }}-"
CARGO_COMPILER = 'gki-manager-cargo-v3-' + VARIANT + '-' + RUNNER + 'ndk29-' + RUST
CARGO = CARGO_COMPILER + HASH
GRADLE = ('gki-manager-gradle-build-v2-' + VARIANT + '-' + RUNNER + 'jdk21-sdk37-ndk29-'
          + "${{ hashFiles('manager-source/manager/gradle/wrapper/gradle-wrapper.properties', "
          "'manager-source/manager/gradle/libs.versions.toml') }}-")


def render(text, variant, *, compiler='rust-A', digest='inputs-A'):
    text = text.replace(VARIANT, variant).replace('${{ runner.os }}', 'Linux')
    text = text.replace('${{ runner.arch }}', 'X64')
    text = text.replace('${{ steps.manager-rust-version.outputs.version }}', compiler)
    text = re.sub(r"\$\{\{ inputs\.kernelsu_variant == 'BakaSU' && 'BakaSU' \|\| '([^']+)' \}\}",
                  lambda m: 'BakaSU' if variant == 'BakaSU' else m[1], text)
    text = re.sub(r'\$\{\{ hashFiles\([^{}]+\) \}\}', digest, text)
    if '${{' in text:
        raise AssertionError('Unreviewed expression in cache key')
    return text


def restored_key(patterns, entries):
    """Actions searches prefixes in order, choosing the newest matching entry."""
    for pattern in patterns.splitlines():
        candidates = [(created, key) for key, created in entries.items() if key.startswith(pattern)]
        if candidates:
            return max(candidates)[1]
    return None


class ManagerCacheKeysTests(unittest.TestCase):
    def test_kow_can_borrow_existing_official_dependencies_with_identical_inputs(self):
        patterns = render(restore_keys(CARGO, kind='cargo', compiler_prefix=CARGO_COMPILER), 'KowSU')
        official = render(CARGO, 'KernelSU') + '41571437064-1'
        self.assertEqual(official, restored_key(patterns, {official: 1}))

    def test_own_exact_and_own_compiler_snapshots_take_priority_over_siblings(self):
        patterns = render(restore_keys(CARGO, kind='cargo', compiler_prefix=CARGO_COMPILER), 'KowSU')
        exact = render(CARGO, 'KowSU') + 'old-run-1'
        own_older_inputs = render(CARGO, 'KowSU', digest='inputs-old') + 'own-run-1'
        sibling = render(CARGO, 'KernelSU') + 'new-run-1'
        self.assertEqual(exact, restored_key(patterns, {exact: 1, sibling: 3, own_older_inputs: 2}))
        self.assertEqual(own_older_inputs, restored_key(patterns, {sibling: 3, own_older_inputs: 2}))

    def test_cargo_sibling_exact_inputs_take_priority_over_newer_broad_snapshot(self):
        patterns = render(restore_keys(CARGO, kind='cargo', compiler_prefix=CARGO_COMPILER), 'KowSU')
        exact = render(CARGO, 'KernelSU') + 'old-run-1'
        changed = render(CARGO, 'SukiSU', digest='inputs-changed') + 'new-run-1'
        self.assertEqual(exact, restored_key(patterns, {exact: 1, changed: 2}))

    def test_cargo_fallback_never_crosses_rust_ndk_os_or_architecture(self):
        patterns = render(restore_keys(CARGO, kind='cargo', compiler_prefix=CARGO_COMPILER), 'KowSU')
        native = render(CARGO, 'KernelSU') + 'run-1'
        entries = {native.replace('rust-A-', 'rust-B-'): 1, native.replace('-ndk29-', '-ndk30-'): 2,
                   native.replace('-Linux-', '-Windows-'): 3, native.replace('-X64-', '-ARM64-'): 4}
        self.assertIsNone(restored_key(patterns, entries))

    def test_baka_ndk30_nightly_cache_cannot_be_borrowed_by_ndk29_variants(self):
        # Baka's existing namespace incorrectly says ndk29. Its isolation must
        # survive even if a test or future toolchain produces the same Rust ID.
        for prefix, kind, compiler in ((CARGO, 'cargo', CARGO_COMPILER), (GRADLE, 'gradle', None)):
            template = restore_keys(prefix, kind=kind, compiler_prefix=compiler)
            for selected in MANAGERS:
                patterns = render(template, selected)
                if selected == 'BakaSU':
                    entries = {render(prefix, other) + 'run-1': 1 for other in MANAGERS if other != 'BakaSU'}
                else:
                    entries = {render(prefix, 'BakaSU') + 'run-1': 1}
                self.assertIsNone(restored_key(patterns, entries), selected)

    def test_gradle_can_borrow_matching_tasks_but_never_omit_wrapper_or_tool_versions(self):
        patterns = render(restore_keys(GRADLE, kind='gradle'), 'KowSU')
        native = render(GRADLE, 'KernelSU') + 'run-1'
        self.assertEqual(native, restored_key(patterns, {native: 1}))
        for changed in (native.replace('inputs-A-', 'inputs-old-'), native.replace('-jdk21-', '-jdk17-'),
                        native.replace('-sdk37-', '-sdk36-'), native.replace('-ndk29-', '-ndk30-')):
            self.assertIsNone(restored_key(patterns, {changed: 1}))

    def test_existing_primary_prefix_and_legacy_migration_order_are_preserved(self):
        legacy = ('legacy-one-', 'legacy-two-')
        patterns = restore_keys(CARGO, kind='cargo', compiler_prefix=CARGO_COMPILER, legacy=legacy).splitlines()
        self.assertEqual([CARGO, CARGO_COMPILER], patterns[:2])
        self.assertEqual(list(legacy), patterns[-2:])
        self.assertEqual(1, patterns.count(CARGO))

    def test_unknown_or_weakened_guards_are_rejected(self):
        invalid = (
            (CARGO, {'kind': 'apk'}),
            (CARGO.replace('-' + RUNNER, '-'), {'kind': 'cargo'}),
            (CARGO.replace(RUST, ''), {'kind': 'cargo'}),
            (CARGO_COMPILER, {'kind': 'cargo'}),
            (CARGO, {'kind': 'cargo', 'compiler_prefix': CARGO_COMPILER.replace(RUST, '')}),
            (GRADLE, {'kind': 'gradle', 'compiler_prefix': GRADLE.split('${{ hashFiles(', 1)[0]}),
            (GRADLE.replace('-ndk29-', '-'), {'kind': 'gradle'}),
            (CARGO, {'kind': 'cargo', 'legacy': ('valid-\ninjected-',)}),
        )
        for prefix, options in invalid:
            with self.subTest(prefix=prefix, options=options), self.assertRaises(ValueError):
                restore_keys(prefix, **options)


if __name__ == '__main__':
    unittest.main()
