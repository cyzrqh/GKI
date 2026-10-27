"""Reject a manager/payload ABI mismatch before expensive kernel compilation."""
import hashlib
import json
from pathlib import Path
import shutil
import struct
import sys
import unittest
from unittest import mock
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import kpm_payload_profiles as profiles


BRIDGE_SOURCE = '''
#include "bridge.h"
long __nocfi sukisu_kpm_bridge(unsigned long op, unsigned long a1,
                              unsigned long a2, unsigned long a3, unsigned long a4)
{
    handler_t fn = (handler_t)find_kernel_symbol_exact("__arm64_sys_truncate");
    struct pt_regs regs = { 0 };
    if (!fn) return -EOPNOTSUPP;
    regs.pstate = PSR_MODE_EL1h;
    regs.regs[0] = SUKISU_KPM_SUPERCALL_MAGIC;
    regs.regs[6] = SUKISU_KPM_SUPERCALL_MAGIC2;
    regs.regs[7] = SUKISU_KPM_SUPERCALL_MAGIC3;
    regs.regs[8] = SUKISU_KPM_ABI_VERSION;
    regs.regs[1] = SUKISU_KPM_PROBE;
    if (fn(&regs) != SUKISU_KPM_ABI_VERSION) return -EOPNOTSUPP;
    regs.regs[1] = op;
    return fn(&regs);
}
'''
BRIDGE_HEADER = '''
#define SUKISU_KPM_SUPERCALL_MAGIC 0x53554b504d000002UL
#define SUKISU_KPM_SUPERCALL_MAGIC2 0x4b504d4252494447UL
#define SUKISU_KPM_SUPERCALL_MAGIC3 0x53554b494e544552UL
#define SUKISU_KPM_ABI_VERSION 2
#define SUKISU_KPM_PROBE 0
#define SUKISU_KPM_OUTPUT_MAX 4096
'''
LEGACY_SOURCE = '''
noinline NO_OPTIMIZE void sukisu_kpm_load_module_path(const char *path,
                                                     const char *args, void *ptr, int *result)
{ __asm__ volatile("nop"); }
EXPORT_SYMBOL(sukisu_kpm_load_module_path);
noinline NO_OPTIMIZE void sukisu_kpm_unload_module(const char *name, void *ptr, int *result)
{ __asm__ volatile("nop"); }
EXPORT_SYMBOL(sukisu_kpm_unload_module);
noinline NO_OPTIMIZE void sukisu_kpm_num(int *result)
{ __asm__ volatile("nop"); }
EXPORT_SYMBOL(sukisu_kpm_num);
noinline NO_OPTIMIZE void sukisu_kpm_info(const char *name, char *buf, int n, int *size)
{ __asm__ volatile("nop"); }
EXPORT_SYMBOL(sukisu_kpm_info);
noinline NO_OPTIMIZE void sukisu_kpm_list(void *buf, int n, int *result)
{ __asm__ volatile("nop"); }
EXPORT_SYMBOL(sukisu_kpm_list);
noinline NO_OPTIMIZE void sukisu_kpm_control(const char *name, const char *args, long n, int *result)
{ __asm__ volatile("nop"); }
EXPORT_SYMBOL(sukisu_kpm_control);
noinline NO_OPTIMIZE void sukisu_kpm_version(char *buf, int n)
{ __asm__ volatile("nop"); }
EXPORT_SYMBOL(sukisu_kpm_version);
'''


class KpmPayloadProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT.parent / '.gki-upstream-cache' / ('kpm-profile-test-' + uuid.uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.source = self.directory / 'KernelSU'
        self.kernel = self.source / 'kernel'
        self.kpm = self.kernel / 'extension/kpm/kpm.c'
        self.kpm.parent.mkdir(parents=True)
        self.header = self.kpm.with_name('bridge.h')

    def bridge(self, code=BRIDGE_SOURCE, header=BRIDGE_HEADER):
        self.kpm.write_text(code, encoding='utf-8')
        self.header.write_text(header, encoding='utf-8')

    def test_none_preserves_legacy_but_an_unknown_supplied_source_does_not(self):
        self.assertEqual(profiles.resolve_profile()['tag'], '0.13.9')
        with self.assertRaisesRegex(ValueError, 'known KPM implementation'):
            profiles.resolve_profile(self.source)

    def test_native_bridge_selects_updated_official_bytes_not_the_stale_tag_commit(self):
        self.bridge()
        release = profiles.resolve_profile(self.source)
        self.assertEqual(release, profiles.load_bridge_release())
        self.assertEqual(release['commit'], 'd958d2582fe3b7eaaeaf238b3bf9af4507a81174')
        self.assertEqual(release['tag'], '0.14.0')
        self.assertEqual(release['assets']['kpimg']['sha256'],
                         'ab020f31ca1c058603917e46d8a6691fbce0dd5cdc135a9cd8dc780e89472716')
        self.assertEqual(profiles.resolve_profile(self.kernel), release)

    @unittest.skipUnless(shutil.which('git'), 'Git is required to apply the production BakaSU port')
    def test_real_bakasu_port_selects_abi2_assets_and_rejects_changed_bridge(self):
        import apply_kpm_patch as patcher
        from test_bakasu_kpm_backend import extract_backend

        # Apply the committed production patch with Git, rather than copying
        # BRIDGE_SOURCE or mocking the source-contract resolver.
        with extract_backend(ROOT / 'tools/patches/bakasu-kpm.patch') as source:
            release = profiles.resolve_profile(source)
            self.assertEqual(release, profiles.load_bridge_release())
            self.assertEqual(profiles.resolve_profile(source / 'kernel'), release)
            cache = self.directory / 'real-bakasu-assets'
            spec = release['assets']['kpimg']
            # Stop at the network boundary: preparation must request the
            # ABI-2 pinned payload, without downloading or executing a tool.
            with mock.patch.object(patcher, 'download', side_effect=RuntimeError('download boundary')) as download:
                with self.assertRaisesRegex(RuntimeError, 'download boundary'):
                    patcher.prepare(cache, ksu_source=source)
            download.assert_called_once_with(spec['url'], spec['size'])
            self.assertIn('/0.14.0/kpimg', spec['url'])
            self.assertFalse((cache / patcher.PROFILE_MANIFEST).exists())

            header = source / 'kernel/kpm/bridge.h'
            original = header.read_text(encoding='utf-8')
            changed = original.replace('SUKISU_KPM_ABI_VERSION 2', 'SUKISU_KPM_ABI_VERSION 1')
            self.assertNotEqual(original, changed)
            header.write_text(changed, encoding='utf-8')
            with mock.patch.object(patcher, 'download') as download:
                with self.assertRaisesRegex(ValueError, 'Unsupported KPM bridge'):
                    patcher.prepare(self.directory / 'changed-bakasu-assets', ksu_source=source)
            download.assert_not_called()
            self.assertFalse((self.directory / 'changed-bakasu-assets').exists())

    def test_whitespace_comments_and_literal_unsigned_suffix_are_accepted(self):
        self.bridge('/* unrelated KPM comment */\n' + BRIDGE_SOURCE.replace(' = ', '\t=\t'),
                    BRIDGE_HEADER.replace('UL', 'ULL') + '// comment\n')
        self.assertEqual(profiles.resolve_profile(self.source)['tag'], '0.14.0')

    def test_legacy_source_with_all_real_exported_hooks_keeps_original_payload(self):
        self.kpm.write_text(LEGACY_SOURCE, encoding='utf-8')
        self.assertEqual(profiles.resolve_profile(self.source)['tag'], '0.13.9')

    def test_pre_extension_directory_layout_is_still_supported(self):
        self.kpm.unlink(missing_ok=True)
        legacy = self.kernel / 'kpm/kpm.c'
        legacy.parent.mkdir()
        legacy.write_text(LEGACY_SOURCE, encoding='utf-8')
        self.assertEqual(profiles.resolve_profile(self.source)['tag'], '0.13.9')

    def test_changed_abi_or_any_framing_tag_rejects_instead_of_falling_back(self):
        for old, new in (('ABI_VERSION 2', 'ABI_VERSION 1'),
                         ('0x53554b504d000002', '0x53554b504d000001'),
                         ('0x4b504d4252494447', '0x4b504d4252494448'),
                         ('0x53554b494e544552', '0x53554b494e544553'),
                         ('PROBE 0', 'PROBE 1'), ('OUTPUT_MAX 4096', 'OUTPUT_MAX 8192')):
            with self.subTest(constant=old):
                self.bridge(header=BRIDGE_HEADER.replace(old, new))
                with self.assertRaisesRegex(ValueError, 'Unsupported KPM bridge'):
                    profiles.resolve_profile(self.source)

    def test_duplicate_missing_and_expression_constants_are_not_guessed(self):
        for header in (BRIDGE_HEADER + '#define SUKISU_KPM_ABI_VERSION 2\n',
                       BRIDGE_HEADER.replace('#define SUKISU_KPM_PROBE 0', ''),
                       BRIDGE_HEADER.replace('ABI_VERSION 2', 'ABI_VERSION (1 + 1)')):
            with self.subTest(header=header):
                self.bridge(header=header)
                with self.assertRaises(ValueError):
                    profiles.resolve_profile(self.source)

    def test_missing_header_and_changed_calling_convention_do_not_select_legacy(self):
        self.bridge()
        self.header.unlink()
        with self.assertRaisesRegex(ValueError, 'header is missing'):
            profiles.resolve_profile(self.source)
        for code in (BRIDGE_SOURCE.replace('regs.regs[6]', 'regs.regs[5]'),
                     BRIDGE_SOURCE.replace('PSR_MODE_EL1h', 'PSR_MODE_EL0t'),
                     BRIDGE_SOURCE.replace('!= SUKISU_KPM_ABI_VERSION', '== SUKISU_KPM_ABI_VERSION'),
                     BRIDGE_SOURCE.replace('"__arm64_sys_truncate"', '"unreviewed_call"')):
            with self.subTest(code=code):
                self.bridge(code=code)
                with self.assertRaisesRegex(ValueError, 'unknown bridge implementation'):
                    profiles.resolve_profile(self.source)

    def test_legacy_requires_definitions_and_exports_instead_of_names_in_comments(self):
        for code in ('/* ' + LEGACY_SOURCE + ' */',
                     LEGACY_SOURCE.replace('EXPORT_SYMBOL(sukisu_kpm_num);', ''),
                     LEGACY_SOURCE.replace('void sukisu_kpm_version(char *buf, int n)\n{ __asm__ volatile("nop"); }',
                                           'void sukisu_kpm_version(char *buf, int n);')):
            with self.subTest(code=code):
                self.kpm.write_text(code, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Unsupported legacy'):
                    profiles.resolve_profile(self.source)

    def test_multiple_implementations_and_mixed_interfaces_reject(self):
        self.bridge(code=BRIDGE_SOURCE + LEGACY_SOURCE)
        with self.assertRaisesRegex(ValueError, 'ambiguously mixes'):
            profiles.resolve_profile(self.source)
        other = self.kernel / 'kpm/kpm.c'
        other.parent.mkdir()
        other.write_text(LEGACY_SOURCE)
        with self.assertRaisesRegex(ValueError, 'one known KPM implementation'):
            profiles.resolve_profile(self.source)

    def test_source_cannot_be_oversized_or_symlinked(self):
        self.kpm.write_bytes(b'x' * 524289)
        with self.assertRaisesRegex(ValueError, 'bounded size'):
            profiles.resolve_profile(self.source)
        self.kpm.unlink()
        target = self.directory / 'other.c'
        target.write_text(LEGACY_SOURCE)
        try:
            self.kpm.symlink_to(target)
        except OSError:
            self.skipTest('This Windows user cannot create symlinks')
        with self.assertRaisesRegex(ValueError, 'non-symlink'):
            profiles.resolve_profile(self.source)

    def test_report_provenance_requires_exact_known_repository_tag_and_commit(self):
        for release in (profiles.resolve_profile(), profiles.load_bridge_release()):
            provenance = {key: release[key] for key in ('repository', 'tag', 'commit')}
            self.assertEqual(profiles.release_for_provenance(provenance), release)
            for key, value in (('repository', 'other/project'), ('tag', 'latest'), ('commit', 'a' * 40)):
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'unknown payload'):
                    profiles.release_for_provenance({**provenance, key: value})
            with self.assertRaisesRegex(ValueError, 'invalid payload provenance'):
                profiles.release_for_provenance({**provenance, 'extra': True})

    def test_release_lock_rejects_changed_bytes_commit_url_and_boolean_sizes(self):
        release = profiles.load_bridge_release()
        variants = []
        for key, value in (('commit', 'a' * 40), ('repository', 'other/project'), ('tag', 'main')):
            changed = json.loads(json.dumps(release))
            changed[key] = value
            variants.append(changed)
        for key, value in (('size', True), ('sha256', 'a' * 64), ('url', 'https://example.com/payload')):
            changed = json.loads(json.dumps(release))
            changed['assets']['kpimg'][key] = value
            variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed), mock.patch.object(
                    profiles, '_read_source', return_value=json.dumps(changed)), self.assertRaises(ValueError):
                profiles.load_bridge_release()

    def test_instruction_fingerprint_never_replaces_full_payload_hash(self):
        # Deliberately non-loadable synthetic bytes copy only the public
        # fingerprint. They must still fail the complete official-asset hash.
        data = bytearray(294384)
        data[:12] = b'KP1158\0\0\0\0\x0e\0'
        struct.pack_into('<Q', data, 16, 2)
        data[24:30] = b'NOBOOT'
        struct.pack_into('<I', data, 4096, 0xd503201f)
        for offset, words in profiles.BRIDGE_INSTRUCTIONS.items():
            struct.pack_into('<' + 'I' * len(words), data, offset, *words)
        with self.assertRaisesRegex(ValueError, 'complete SHA256'):
            profiles.validate_bridge_payload(bytes(data))
        # ABI 1 in otherwise identical public fingerprints is diagnosed before
        # the hash error; accepting the old tag source would miss this.
        struct.pack_into('<I', data, 0x1f24c, 0xf100049f)
        with self.assertRaisesRegex(ValueError, 'ABI-2 handshake'):
            profiles.validate_bridge_payload(bytes(data))

    def test_audited_official_artifact_payload_is_really_abi2_when_available(self):
        artifact = ROOT.parent / '.gki-upstream-cache/kpm-bridge2-d958-build-artifacts.zip'
        if not artifact.is_file():
            self.skipTest('Optional pinned official artifact from the repair audit is not cached')
        raw = artifact.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         'b570fedae955f8bbb5f891dd5328220f31763a5ea41d82fdafef89cd9dc81db9')
        with zipfile.ZipFile(artifact) as archive:
            payload = archive.read('kernel/kpimg')
        self.assertEqual(profiles.validate_bridge_payload(payload),
                         {'version': '0.14.0', 'flags': 2, 'bridge_abi': 2})
        for offset in (0x1f1f4, 0x1f220, 0x1f23c, 0x1f24c, 0x1f26c):
            modified = bytearray(payload)
            modified[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, 'ABI-2 handshake'):
                profiles.validate_bridge_payload(bytes(modified))
        modified = bytearray(payload)
        modified[-1] ^= 1
        with self.assertRaisesRegex(ValueError, 'complete SHA256'):
            profiles.validate_bridge_payload(bytes(modified))


if __name__ == '__main__':
    unittest.main()
