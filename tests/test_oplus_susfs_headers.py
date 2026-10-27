"""Use the actual shared OnePlus namespace prefix and official SUSFS hunk."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from oplus_susfs_headers import adapt_namespace_header

FIXTURE = ROOT / 'tests/fixtures/oplus-susfs61'


class OPlusSusfsHeaderTests(unittest.TestCase):
    def setUp(self):
        self.common = ROOT / 'tests' / ('.oplus-susfs-header-' + uuid.uuid4().hex)
        (self.common / 'fs').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.common)
        self.source = self.common / 'fs/namespace.c'
        self.original = (FIXTURE / 'namespace-header.c').read_bytes()
        self.source.write_bytes(self.original)
        self.patch = (FIXTURE / 'namespace-header.patch').read_bytes()

    def test_fixture_provenance_covers_the_five_actual_failed_official_sources(self):
        provenance = json.loads((FIXTURE / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual({'sm8650-6.1.57', 'sm8650-6.1.118', 'sm8650-6.1.141',
                          'mt6897-6.1.128', 'mt6989-6.1.115'},
                         {source['profile'] for source in provenance['sources']})
        for name, digest in provenance['sha256'].items():
            self.assertEqual(digest, hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest())

    def test_vendor_context_adaptation_keeps_every_official_added_line_and_native_source(self):
        adapted = adapt_namespace_header(self.common, self.patch)
        self.assertNotIn(b' #include <trace/hooks/blk.h>\n', adapted)
        self.assertIn(b'@@ -32,10 +32,20 @@\n', adapted)
        additions = lambda data: [line for line in data.splitlines()
                                  if line.startswith(b'+') and not line.startswith(b'+++')]
        self.assertEqual(additions(self.patch), additions(adapted))
        for declaration in (b'extern bool susfs_is_current_ksu_domain(void);',
                            b'extern struct static_key_true susfs_is_sdcard_android_data_not_decrypted;',
                            b'#define CL_COPY_MNT_NS BIT(25)'):
            self.assertIn(declaration, adapted)
        self.assertEqual(self.original, self.source.read_bytes())

    def test_matching_gki_header_context_needs_no_adaptation(self):
        self.source.write_bytes(self.original.replace(b'#include "internal.h"\n',
                                                     b'#include "internal.h"\n#include <trace/hooks/blk.h>\n'))
        self.assertEqual(self.patch, adapt_namespace_header(self.common, self.patch))

    @unittest.skipUnless(shutil.which('patch'), 'GNU patch is not available on this host')
    def test_real_patch_reproduces_the_rejection_and_then_applies_all_header_additions_without_fuzz(self):
        official = self.common / 'official.patch'
        official.write_bytes(self.patch)
        def apply(path, *options):
            return subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(path), *options],
                                  cwd=self.common, capture_output=True, text=True)
        rejected = apply(official, '--forward', '--dry-run')
        self.assertNotEqual(0, rejected.returncode)
        self.assertEqual(self.original, self.source.read_bytes())
        adapted = self.common / 'adapted.patch'
        adapted.write_bytes(adapt_namespace_header(self.common, self.patch))
        for options in (('--forward', '--dry-run'), ('--forward',), ('--reverse', '--dry-run')):
            result = apply(adapted, *options)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        content = self.source.read_bytes()
        self.assertIn(b'#include <linux/susfs_def.h>', content)
        self.assertIn(b'#define CL_COPY_MNT_NS BIT(25)', content)
        self.assertIn(b'extern bool susfs_is_current_ksu_domain(void);', content)
        self.assertNotIn(b'#include <trace/hooks/blk.h>', content)

    def test_unfamiliar_or_ambiguous_vendor_context_is_rejected_before_any_source_write(self):
        for original in (self.original.replace(b'100000', b'200000'), self.original * 2,
                         self.original.replace(b'#include "internal.h"', b'#include "different.h"')):
            with self.subTest(original=original[-40:]):
                self.source.write_bytes(original)
                with self.assertRaisesRegex(ValueError, 'Unknown OKI namespace header context'):
                    adapt_namespace_header(self.common, self.patch)
                self.assertEqual(original, self.source.read_bytes())

    def test_changed_hook_or_multiple_context_lines_cannot_be_silently_trimmed(self):
        for data in (self.patch.replace(b'+extern bool susfs_is_current_ksu_domain(void);', b'+extern bool other(void);'),
                     self.patch.replace(b' #include <trace/hooks/blk.h>\n',
                                        b' #include <trace/hooks/blk.h>\n #include <trace/hooks/blk.h>\n')):
            with self.subTest(data=data[-40:]), self.assertRaisesRegex(ValueError, 'Review the changed SUSFS'):
                adapt_namespace_header(self.common, data)


if __name__ == '__main__':
    unittest.main()
