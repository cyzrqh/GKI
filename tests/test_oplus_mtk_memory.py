"""Check the actual official memory header and bounded link restoration."""
from contextlib import ExitStack
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import oplus_mtk_memory as memory


class MtkMemoryTests(unittest.TestCase):
    def setUp(self):
        self.profile = next(p for p in json.loads((ROOT / 'config/oplus-kernel-profiles.json').read_text('utf-8'))['profiles']
                            if p['id'] == memory.PROFILE)
        self.pair = memory.selected(self.profile)
        self.source = bytes.fromhex((ROOT / 'tests/fixtures/oplus-mtk-memory/mm-trace.hex').read_text('ascii'))
        self.root = ROOT / 'tests' / ('.mtk-memory-' + uuid.uuid4().hex)
        self.common = self.root / 'common'
        (self.common / 'mm').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)

    def simulation(self):
        stack = ExitStack()
        stack.enter_context(patch.object(memory, 'check_common'))
        stack.enter_context(patch.object(memory, 'check_link'))
        stack.enter_context(patch.object(memory, 'download_source', return_value=self.source))
        return stack

    def test_official_bytes_and_profile_pairing(self):
        memory.checked_bytes(self.source, memory.SPEC)
        self.assertTrue(memory.selected(self.profile))
        for field, value in [('kernel_repo', 'unreviewed/repo'), ('kernel_ref', 'a' * 40),
                             ('verified_kernel_ref', 'b' * 40), ('platform', 'sm8650'), ('kernel', '6.1.128')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                memory.selected({**self.profile, field: value})

    def test_all_four_profiles_bind_their_own_reviewed_official_common_and_vendor(self):
        profiles = json.loads((ROOT / 'config/oplus-kernel-profiles.json').read_text('utf-8'))['profiles']
        for profile in profiles:
            if profile['id'] not in memory.VMSCAN_SHA256:
                continue
            with self.subTest(profile=profile['id']):
                pair = memory.selected(profile)
                self.assertEqual(profile['kernel_repo'], pair['common_repository'])
                self.assertEqual(profile['verified_kernel_ref'], pair['common_commit'])
                report = memory.provenance(pair)
                self.assertEqual(pair['vendor_repository'], report['repository'])
                self.assertEqual(pair['vendor_commit'], report['commit'])
                self.assertEqual(report, memory.validate_mtk_memory_provenance(profile, report))
                response = MagicMock()
                url = ('https://raw.githubusercontent.com/' + pair['vendor_repository']
                       + '/' + pair['vendor_commit'] + '/' + memory.SOURCE)
                response.status = 200
                response.geturl.return_value = url
                response.read.return_value = self.source
                response.__enter__.return_value = response
                opener = MagicMock()
                opener.open.return_value = response
                with patch.object(memory, 'build_opener', return_value=opener):
                    self.assertEqual(self.source, memory.download_source(pair))
                request = opener.open.call_args.args[0]
                self.assertEqual(url, request.full_url)
                self.assertNotIn('Authorization', dict(request.header_items()))
                if profile['id'] != self.profile['id']:
                    with self.assertRaises(ValueError):
                        memory.validate_mtk_memory_provenance(profile, memory.provenance(self.pair))

    def test_other_platforms_noop_without_transport(self):
        with patch.object(memory, 'download_source') as fetch:
            self.assertEqual({}, memory.restore_mtk_memory(self.common, {'id': 'mt6897-6.6.89'}))
            self.assertEqual({}, memory.verify_mtk_memory_report(self.common, {'id': 'sm8650-6.1.57'}, {}))
            fetch.assert_not_called()
        with self.assertRaises(ValueError):
            memory.validate_mtk_memory_provenance({'id': 'mt6897-6.6.89'}, memory.provenance(self.pair))

    def test_publication_is_byte_exact_idempotent_and_report_is_required(self):
        with self.simulation():
            report = memory.restore_mtk_memory(self.common, self.profile)
            self.assertEqual(self.source, (self.common / memory.TARGET).read_bytes())
            self.assertEqual(report, memory.restore_mtk_memory(self.common, self.profile))
            self.assertEqual(report, memory.verify_mtk_memory_report(self.common, self.profile, report))
            with self.assertRaises(ValueError):
                memory.verify_mtk_memory_report(self.common, self.profile, {})
        self.assertFalse(list((self.common / 'mm').glob('.oplus-*')))
        for field in ('commit', 'files', 'common_commit'):
            wrong = copy.deepcopy(report)
            wrong[field] = {}
            with self.subTest(field=field), self.assertRaises(ValueError):
                memory.validate_mtk_memory_provenance(self.profile, wrong)

    def test_tampered_download_never_publishes(self):
        with self.simulation(), patch.object(memory, 'download_source', return_value=self.source + b'\n'):
            with self.assertRaises(ValueError):
                memory.restore_mtk_memory(self.common, self.profile)
        self.assertFalse((self.common / memory.TARGET).exists())
        self.assertFalse(list((self.common / 'mm').glob('.oplus-*')))

    def test_real_dangling_link_is_replaced_without_touching_external_vendor_path(self):
        target = self.common / memory.TARGET
        try:
            target.symlink_to(memory.LINK)
        except OSError:
            self.skipTest('Requires permission to create a real source symlink')
        with patch.object(memory, 'check_common'), patch.object(memory, 'download_source', return_value=self.source):
            report = memory.restore_mtk_memory(self.common, self.profile)
        self.assertFalse(target.is_symlink())
        self.assertEqual(self.source, target.read_bytes())
        self.assertEqual(memory.provenance(self.pair), report)
        self.assertFalse((self.root / 'vendor').exists())

    def test_all_transport_attempts_fail_without_unbounded_wait(self):
        opener = MagicMock()
        opener.open.side_effect = URLError('offline')
        with patch.object(memory, 'build_opener', return_value=opener), patch.object(memory.time, 'sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'transport'):
                memory.download_source(self.pair)
        self.assertEqual(3, opener.open.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])

    def test_replace_failure_keeps_original_and_removes_owned_stage(self):
        target = self.common / memory.TARGET
        target.write_bytes(b'original test link observation')
        with self.simulation(), patch.object(memory, 'is_link', side_effect=lambda p: p == target), \
                patch.object(memory.os, 'replace', side_effect=OSError('publication failed')):
            with self.assertRaises(OSError):
                memory.restore_mtk_memory(self.common, self.profile)
        self.assertEqual(b'original test link observation', target.read_bytes())
        self.assertFalse(list((self.common / 'mm').glob('.oplus-*')))

    def test_changed_consumer_or_native_link_is_rejected(self):
        link = '120000 blob ' + memory.LINK_BLOB + '\t' + memory.TARGET + '\n'
        with patch.object(memory, 'native_git', side_effect=[self.pair['common_commit'], link]), \
                patch.object(memory, 'read', return_value=b'changed native vmscan'):
            with self.assertRaisesRegex(ValueError, 'consumer'):
                memory.check_common(self.common, self.pair)
        for head, native in [('a' * 40, link), (self.pair['common_commit'], link.replace('120000', '100644'))]:
            with patch.object(memory, 'native_git', side_effect=[head, native]), self.assertRaises(ValueError):
                memory.check_common(self.common, self.pair)
        with self.assertRaisesRegex(ValueError, 'exact dangling'):
            memory.check_link(self.common / memory.TARGET)

    def test_transient_downloads_are_bounded_and_exact(self):
        response = MagicMock()
        response.status = 200
        response.geturl.return_value = ('https://raw.githubusercontent.com/' + self.pair['vendor_repository']
                                       + '/' + self.pair['vendor_commit'] + '/' + memory.SOURCE)
        response.read.return_value = self.source
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.side_effect = [URLError('transient'), response]
        with patch.object(memory, 'build_opener', return_value=opener), patch.object(memory.time, 'sleep') as sleep:
            self.assertEqual(self.source, memory.download_source(self.pair))
            self.assertEqual(2, opener.open.call_count)
            sleep.assert_called_once_with(1)
            response.read.assert_called_once_with(memory.SPEC['size'] + 1)
        opener.open.side_effect = HTTPError('https://redacted.invalid', 404, 'missing', {}, io.BytesIO())
        with patch.object(memory, 'build_opener', return_value=opener), patch.object(memory.time, 'sleep') as sleep:
            with self.assertRaises(ValueError):
                memory.download_source(self.pair)
            sleep.assert_not_called()

    def test_exact_official_header_compiles_and_emits_native_trace_messages(self):
        compiler = os.environ.get('OPLUS_MEMORY_CC') or shutil.which('cc')
        if not compiler:
            self.skipTest('Requires a native C compiler')
        (self.root / 'mm-trace.h').write_bytes(self.source)
        harness = self.root / 'test.c'
        harness.write_text(r'''
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include <assert.h>
typedef int int32_t;
typedef long long int64_t;
#define noinline __attribute__((noinline))
static struct { int tgid; } task = {42};
#define current (&task)
static char recorded[1024];
static int events;
static void trace_puts(const char *s) { snprintf(recorded, sizeof(recorded), "%s", s); events++; }
#include "mm-trace.h"
int main(void) {
    mm_trace_int64("kswapd_reclaimed", 123);
    assert(!strcmp(recorded, "C|42|kswapd_reclaimed|123"));
    mm_trace_fmt_int64(456, "reclaim-%d", 9);
    assert(!strcmp(recorded, "C|42|reclaim-9|456"));
    mm_trace_begin("reclaim"); assert(!strcmp(recorded, "B|42|reclaim"));
    mm_trace_end(); assert(!strcmp(recorded, "E|42"));
    mm_trace_async_begin("page", 7); assert(!strcmp(recorded, "S|42|page|7"));
    mm_trace_async_end("page", 7); assert(!strcmp(recorded, "F|42|page|7"));
    assert(events == 6);
    return 0;
}
''', encoding='utf-8')
        executable = self.root / ('trace.exe' if os.name == 'nt' else 'trace')
        command = [compiler] + (['cc'] if Path(compiler).stem == 'zig' else [])
        result = subprocess.run(command + ['-std=gnu11', '-Werror=implicit-function-declaration', str(harness), '-o', str(executable)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stderr)
        subprocess.run([str(executable)], check=True, capture_output=True, timeout=15)


if __name__ == '__main__':
    unittest.main()
