"""Exercise real LLVM cache reuse and the two existing Make build entries."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import thinlto_cache as link_cache
import persistent_kernel_cache as storage


class ThinLTOCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gki-thinlto-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'common'
        self.source.mkdir()
        self.directory = self.root / 'persistent-link-cache'
        self.text = ('VERSION = 5\nifdef CONFIG_LTO_CLANG_THIN\n'
                     'KBUILD_LDFLAGS += --thinlto-cache-dir=$(extmod-prefix).thinlto-cache\n'
                     'endif\n')
        (self.source / 'Makefile').write_text(self.text, encoding='utf-8', newline='\n')

    def test_only_native_cache_location_changes_and_configuration_is_idempotent(self):
        self.assertTrue(link_cache.configure(self.source, self.directory))
        expected = self.text.replace('$(extmod-prefix).thinlto-cache', '$(GKI_THINLTO_CACHE_DIR)')
        self.assertEqual(expected, (self.source / 'Makefile').read_text(encoding='utf-8'))
        self.assertTrue(link_cache.configure(self.source, self.directory))
        self.assertEqual(expected, (self.source / 'Makefile').read_text(encoding='utf-8'))
        for value in ('.thinlto-cache', '$(extmod_prefix).thinlto-cache'):
            (self.source / 'Makefile').write_text(self.text.replace('$(extmod-prefix).thinlto-cache', value), encoding='utf-8')
            self.assertTrue(link_cache.configure(self.source, self.directory))

    def test_unknown_or_ambiguous_kbuild_is_left_unchanged(self):
        for text in ('VERSION=6\n', self.text + self.text,
                     self.text.replace('$(extmod-prefix).thinlto-cache', 'unknown-layout/cache')):
            (self.source / 'Makefile').write_text(text, encoding='utf-8', newline='\n')
            self.assertFalse(link_cache.configure(self.source, self.directory))
            self.assertEqual(text, (self.source / 'Makefile').read_text(encoding='utf-8'))

    def test_cache_cannot_be_placed_inside_mrproper_output(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            link_cache.configure(self.source, self.source / 'out/.thinlto-cache')

    def test_trim_is_bounded_and_preserves_recent_objects(self):
        self.directory.mkdir()
        for i in range(3):
            path = self.directory / ('llvmcache-' + str(i) * 40)
            path.write_bytes(bytes([i]) * 100)
            os.utime(path, (1000 + i, 1000 + i))
        (self.directory / 'llvmcache.timestamp').write_bytes(b'')
        self.assertEqual((2, 200, 1), link_cache.measure(self.directory, 200))
        self.assertFalse((self.directory / ('llvmcache-' + '0' * 40)).exists())
        (self.directory / 'unexpected.conf').write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            link_cache.measure(self.directory, 200)

    def test_private_cache_roundtrip_uses_a_separate_namespace(self):
        from test_persistent_kernel_cache import MemoryApi, PRODUCER
        self.directory.mkdir()
        name = 'llvmcache-' + 'a' * 40
        (self.directory / name).write_bytes(b'LLVM native object\0' * 100)
        (self.directory / 'llvmcache.timestamp').write_bytes(b'')
        api = MemoryApi()
        key = 'gki-thinlto-v1-android12-5.10.236-KernelSU-Next'
        self.assertTrue(storage.save(api, self.directory, key, 'thinlto', PRODUCER))
        target = self.root / 'restored'
        self.assertTrue(storage.restore(api, target, key, 'thinlto', PRODUCER))
        self.assertEqual((self.directory / name).read_bytes(), (target / name).read_bytes())
        self.assertFalse((target / 'llvmcache.timestamp').exists())
        self.assertNotEqual(storage.key_identity(key, 'ccache'), storage.key_identity(key, 'thinlto'))

    def test_real_llvm_thinlto_cache_survives_a_fresh_directory_and_relinks_identically(self):
        clang, lld, make = shutil.which('clang'), shutil.which('ld.lld'), shutil.which('make')
        if os.name == 'nt' or not all((clang, lld, make)):
            self.skipTest('Linux Clang/LLD/Make required; Ubuntu preflight runs real bitcode links')
        self.assertTrue(link_cache.configure(self.source, self.directory))
        with (self.source / 'Makefile').open('a', encoding='utf-8') as output:
            output.write('all:; @echo $(KBUILD_LDFLAGS)\n')
        environment = {**os.environ, 'GKI_THINLTO_CACHE_DIR': str(self.directory)}
        flags = subprocess.check_output([make, '--no-print-directory', '-s', '-C', str(self.source),
                                         'CONFIG_LTO_CLANG_THIN=y'], env=environment, text=True).strip().split()
        self.assertEqual(['--thinlto-cache-dir=' + str(self.directory)], flags)
        (self.root / 'unit.c').write_text('int square(int x) { return x*x; }\n', encoding='utf-8')
        (self.root / 'main.c').write_text('extern int square(int); int main(void) { return square(5)!=25; }\n', encoding='utf-8')
        objects = []
        for name in ('unit', 'main'):
            path = self.root / (name + '.o')
            result = subprocess.run([clang, '-O2', '-flto=thin', '-fsplit-lto-unit', '-c',
                                     str(self.root / (name + '.c')), '-o', str(path)], capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
            objects.append(str(path))
        cold = self.root / 'cold'
        result = subprocess.run([clang, '-fuse-ld=lld', '-flto=thin', *objects,
                                 '-Wl,' + flags[0], '-o', str(cold)], capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
        self.assertEqual(0, subprocess.run([str(cold)]).returncode)
        self.assertGreater(link_cache.measure(self.directory)[0], 0)
        original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p, _ in link_cache.entries(self.directory)}
        archive = self.root / 'link-cache.tar.gz'
        descriptor = storage.pack(self.directory, archive)
        restored = self.root / 'fresh-runner-cache'
        storage.unpack(archive, restored, descriptor)
        shutil.rmtree(self.directory)
        warm = self.root / 'warm'
        result = subprocess.run([clang, '-fuse-ld=lld', '-flto=thin', *objects,
                                 '-Wl,--thinlto-cache-dir=' + str(restored), '-o', str(warm)], capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
        self.assertEqual(0, subprocess.run([str(warm)]).returncode)
        self.assertEqual(cold.read_bytes(), warm.read_bytes())
        self.assertEqual(original, {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p, _ in link_cache.entries(restored)})


class ThinLTOWorkflowTests(unittest.TestCase):
    def test_both_existing_make_entries_restore_before_link_and_save_only_after_success(self):
        from generate_workflow import create_workflow
        from oplus_workflow import create_workflow as create_oplus
        gki = create_workflow(ROOT)
        oplus = create_oplus(ROOT, gki)
        for job, compile_id in ((gki['jobs']['build-kernel'], 'compile-kernel'),
                                (oplus['jobs']['build-oplus-kernel'], 'oplus-build')):
            steps = job['steps']
            by_id = {s['id']: s for s in steps if 'id' in s}
            self.assertLess(steps.index(by_id['thinlto-config']), steps.index(by_id['persistent-thinlto-restore']))
            self.assertLess(steps.index(by_id['persistent-thinlto-restore']), steps.index(by_id[compile_id]))
            self.assertLess(steps.index(by_id[compile_id]), steps.index(by_id['thinlto-trim']))
            self.assertLess(steps.index(by_id['thinlto-trim']), steps.index(by_id['persistent-thinlto-save']))
            self.assertIn(compile_id + ".outcome == 'success'", by_id['persistent-thinlto-save']['if'])
            self.assertEqual(by_id['persistent-thinlto-restore']['env']['OBJECT_CACHE_KEY'],
                             by_id['persistent-thinlto-save']['env']['OBJECT_CACHE_KEY'])
            self.assertIn('inputs.kernelsu_variant', by_id['persistent-thinlto-save']['env']['OBJECT_CACHE_KEY'])
            for name in ('persistent-thinlto-restore', 'persistent-thinlto-save'):
                self.assertIs(by_id[name]['continue-on-error'], True)
                self.assertEqual(3, by_id[name]['timeout-minutes'])
            self.assertTrue(job['env']['GKI_THINLTO_CACHE_DIR'].endswith('/.gki-thinlto-cache'))


if __name__ == '__main__':
    unittest.main()
