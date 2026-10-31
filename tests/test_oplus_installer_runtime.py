"""Exercise the pinned upstream AK3 flash_boot, with no Android/block access.

The real function is included unmodified under its upstream license. Only the
Android tools and device I/O are replaced by bounded spies. This verifies
replacement-Image integrity, not device bootability or AVB acceptance.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import oplus_assets as assets
from test_oplus_assets import SHELL

FIXTURE = ROOT / 'tests/fixtures/oplus-ak3-flash'
EXCERPT_SHA256 = 'c6f1cbc1b6868e346d853f579d472900db6dd0f547f157a9af49ce5030708acb'

CORE = r'''
AKHOME="$PWD";
BIN="$PWD/tools";
SPLITIMG="$PWD/split_img";
BOOTIMG="$PWD/boot.img";
BLOCK="$PWD/partitions/boot_a";
unset CUSTOMDD;
abort() { printf 'abort %s\n' "$*" >> "$AKHOME/trace"; exit 1; }
ui_print() { :; }
getprop() { case "$1" in ro.soc.model) printf 'SM8650\n';; esac; }
uname() { printf '6.1.141-android14-11-stock\n'; }
split_boot() { printf 'split\n' >> "$AKHOME/trace"; }
blockdev() {
  [ "$1" = --setrw ] && [ "$2" = "$AKHOME/partitions/boot_a" ] && [ -f "$2" ] || abort 'unexpected block access';
  printf 'blockdev\n' >> "$AKHOME/trace";
}
cat() {
  # Upstream zero-fills a partition after the image. The test target is a
  # small regular file: copy only the image, never read an infinite stream.
  if [ "$#" = 2 ] && [ "$1" = boot-new.img ] && [ "$2" = /dev/zero ]; then
    command cat boot-new.img;
  else
    command cat "$@";
  fi;
}
magiskboot() {
  printf '%s\n' "$1" >> "$AKHOME/magiskboot-calls";
  case "$1" in
    repack)
      [ "$PATCHVBMETAFLAG" = false ] || abort 'AVB flags changed';
      [ "$2" = "$BOOTIMG" ] && [ "$3" = "$AKHOME/boot-new.img" ] || abort 'unexpected repack path';
      cp kernel "$3";
    ;;
    cpio)
      # A Magisk-marked ramdisk is a valid upstream test result. Its extraction
      # also supplies a harmless config so a legacy `export $(cat ...)` never
      # turns into a no-argument export of the host environment.
      if [ "$3" = test ]; then return 1; fi;
      printf 'KEEPVERITY=true\n' > .magisk;
    ;;
    split|decompress) return 1;;
    hexpatch)
      printf 'unrequested legacy kernel modification' > kernel;
    ;;
    dtb) abort 'unexpected DTB modification';;
    *) abort 'unexpected Android tool command';;
  esac;
}
. "$AK3_RUNTIME_FIXTURE";
'''


@unittest.skipUnless(SHELL, 'A POSIX shell is required for the upstream runtime fixture')
class OplusUpstreamInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        doc = json.loads((FIXTURE / 'provenance.json').read_text())
        if (doc['repository'] != 'osm0sis/AnyKernel3'
                or doc['excerpt_sha256'] != EXCERPT_SHA256
                or hashlib.sha256((FIXTURE / 'flash_boot.sh').read_bytes()).hexdigest() != EXCERPT_SHA256
                or hashlib.sha256((FIXTURE / 'LICENSE').read_bytes()).hexdigest() != doc['license_sha256']):
            raise AssertionError('Pinned upstream runtime fixture or license changed')

    def setUp(self):
        self.directory = ROOT.parent / '.gki-upstream-cache' / ('oplus-real-runtime-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)

    def run_case(self, *, variant='SukiSU', ramdisk=False, legacy=False):
        directory = self.directory / uuid.uuid4().hex
        directory.mkdir()
        (directory / 'tools').mkdir()
        (directory / 'split_img').mkdir()
        (directory / 'partitions').mkdir()
        (directory / 'tools/ak3-core.sh').write_text(CORE, encoding='utf-8', newline='\n')
        image = b'non-installable paired kernel fixture\0' * 8
        (directory / 'Image').write_bytes(image)
        (directory / 'boot.img').write_bytes(b'original boot fixture\0' * 256)
        (directory / 'partitions/boot_a').write_bytes(b'original partition fixture')
        if ramdisk:
            (directory / 'split_img/ramdisk.cpio').write_bytes(b'Magisk-marked original ramdisk fixture')
        script = assets.anykernel_script({'platform': 'sm8650', 'kernel': '6.1.141'},
                                        variant, '6.1.141-android14-11')
        if legacy:
            script = script.replace(b'NO_MAGISK_CHECK=1;\n', b'').replace(b'unset magisk_patched;\n', b'')
        (directory / 'anykernel.sh').write_bytes(script)
        env = {**os.environ, 'AK3_RUNTIME_FIXTURE': (FIXTURE / 'flash_boot.sh').as_posix(),
               'PATCHVBMETAFLAG': 'true', 'magisk_patched': '1',
               'PATH': str(Path(SHELL).parent) + os.pathsep + os.environ.get('PATH', '')}
        result = subprocess.run([SHELL, './anykernel.sh'], cwd=directory, env=env,
                                text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = (directory / 'magiskboot-calls').read_text().splitlines()
        return directory, image, calls

    def test_both_managers_preserve_image_and_existing_ramdisk_with_upstream_runtime(self):
        for variant in ('SukiSU', 'BakaSU'):
            for ramdisk in (False, True):
                with self.subTest(variant=variant, ramdisk=ramdisk):
                    directory, image, calls = self.run_case(variant=variant, ramdisk=ramdisk)
                    self.assertEqual(['repack'], calls)
                    self.assertEqual(image, (directory / 'partitions/boot_a').read_bytes())
                    self.assertEqual('split\nblockdev\n', (directory / 'trace').read_text())
                    if ramdisk:
                        self.assertEqual(b'Magisk-marked original ramdisk fixture',
                                         (directory / 'split_img/ramdisk.cpio').read_bytes())

    def test_old_entry_allows_legacy_runtime_to_modify_verified_image(self):
        directory, image, calls = self.run_case(ramdisk=True, legacy=True)
        self.assertIn('hexpatch', calls)
        self.assertNotEqual(image, (directory / 'partitions/boot_a').read_bytes())


if __name__ == '__main__':
    unittest.main()
