"""Binary format/CLI contract tests, NOT execution of ksud or real phone boot."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import verify_manager_boot_patch as boot


class ManagerBootPatchTests(unittest.TestCase):
    def setUp(self):
        # Opaque test tokens only exercise the byte comparison. They are not
        # ELF modules, KMI assets, usable init programs, or build artifacts.
        self.module = b"opaque module comparison test token"
        self.init = b"opaque init comparison test token"
        self.entries = {"init": boot.Entry(stat.S_IFREG | 0o755, self.init),
                        "init.real": boot.Entry(stat.S_IFREG | 0o755, boot.FIXTURE_INIT),
                        "kernelsu.ko": boot.Entry(stat.S_IFREG | 0o755, self.module),
                        "ksu_config": boot.Entry(stat.S_IFREG | 0o644, b"bundled=1")}

    def image(self, entries=None):
        return boot.boot_v4(boot.synthetic_kernel(), boot.newc(self.entries if entries is None else entries))

    def test_fixture_is_explicit_non_bootable_v4_with_newc_init(self):
        data = self.image({"init": boot.Entry(stat.S_IFREG | 0o755, boot.FIXTURE_INIT)})
        self.assertEqual(data[:8], b"ANDROID!")
        self.assertEqual(struct.unpack_from("<I", data, 20)[0], 1584)
        self.assertEqual(struct.unpack_from("<I", data, 40)[0], 4)
        self.assertEqual(struct.unpack_from("<I", data, 1580)[0], 0)
        kernel, entries = boot.parse_boot_v4(data)
        self.assertEqual(kernel[56:60], b"ARMd")
        self.assertIn(b"SYNTHETIC FORMAT TEST ONLY", kernel)
        self.assertIn(b"DO NOT FLASH", entries["init"].data)
        self.assertEqual(entries["init"].mode, stat.S_IFREG | 0o755)
        self.assertEqual(len(data) % 4096, 0)

    def test_patched_checks_exact_three_payloads_and_modes(self):
        report = boot.check_patched(self.image(), self.module, self.init)
        self.assertEqual(report["module_sha256"], boot.digest(self.module))
        self.assertEqual(report["ksuinit_sha256"], boot.digest(self.init))
        for name in ("init", "init.real", "kernelsu.ko"):
            with self.subTest(name=name):
                entries = {**self.entries, name: boot.Entry(stat.S_IFREG | 0o755, b"other")}
                with self.assertRaisesRegex(boot.VerificationError, name.replace(".", r"\.")):
                    boot.check_patched(self.image(entries), self.module, self.init)
                entries = {**self.entries, name: boot.Entry(stat.S_IFREG | 0o644, self.entries[name].data)}
                with self.assertRaises(boot.VerificationError):
                    boot.check_patched(self.image(entries), self.module, self.init)

    def test_patch_rejects_missing_or_unexpected_files_and_changed_kernel(self):
        for name in ("init", "init.real", "kernelsu.ko"):
            entries = dict(self.entries)
            del entries[name]
            with self.assertRaises(boot.VerificationError):
                boot.check_patched(self.image(entries), self.module, self.init)
        entries = {**self.entries, "other": boot.Entry(stat.S_IFREG | 0o644, b"other")}
        with self.assertRaisesRegex(boot.VerificationError, "Unexpected files"):
            boot.check_patched(self.image(entries), self.module, self.init)
        data = bytearray(self.image())
        data[4096 + 100] ^= 1
        with self.assertRaisesRegex(boot.VerificationError, "changed.*kernel"):
            boot.check_patched(bytes(data), self.module, self.init)

    def test_restore_checks_original_init_and_removes_installed_payloads(self):
        entries = {"init": boot.Entry(stat.S_IFREG | 0o755, boot.FIXTURE_INIT),
                   "ksu_config": self.entries["ksu_config"]}
        boot.check_restored(self.image(entries))
        for name in ("init.real", "kernelsu.ko"):
            with self.subTest(name=name), self.assertRaises(boot.VerificationError):
                boot.check_restored(self.image({**entries, name: self.entries[name]}))
        with self.assertRaises(boot.VerificationError):
            boot.check_restored(self.image({**entries, "init": self.entries["init"]}))

    def test_newc_rejects_unsafe_duplicate_and_non_regular_entries(self):
        for name in ("../init", "/init", "a//b", "./init", "a\\b", "a/../b"):
            with self.subTest(name=name), self.assertRaises(boot.VerificationError):
                boot.parse_newc(boot.newc({name: boot.Entry(stat.S_IFREG | 0o755, b"x")}))
        single = boot.newc({"init": boot.Entry(stat.S_IFREG | 0o755, b"x")})
        self.assertEqual(single[-124:-118], b"070701")
        with self.assertRaisesRegex(boot.VerificationError, "duplicate"):
            boot.parse_newc(single[:-124] + single)
        with self.assertRaisesRegex(boot.VerificationError, "non-regular"):
            boot.parse_newc(boot.newc({"init": boot.Entry(stat.S_IFLNK | 0o777, b"target")}))

    def test_newc_rejects_malformed_bounds_trailer_and_tail(self):
        data = boot.newc({"init": boot.Entry(stat.S_IFREG | 0o755, b"x")})
        bad_size = bytearray(data)
        bad_size[54:62] = b"ffffffff"
        missing_name = bytearray(data)
        missing_name[94:102] = b"00000000"
        for damaged in (data[:100], data[:-125], data + b"junk", bytes(bad_size), bytes(missing_name), b"070702" + data[6:]):
            with self.subTest(size=len(damaged)), self.assertRaises(boot.VerificationError):
                boot.parse_newc(damaged)

    def test_boot_rejects_truncation_wrong_version_signature_and_payload_tail(self):
        data = self.image()
        for offset, value in ((40, 3), (20, 1580), (1580, 16), (24, 1), (8, 0xFFFFFFFF), (12, 0xFFFFFFFF)):
            damaged = bytearray(data)
            struct.pack_into("<I", damaged, offset, value)
            with self.subTest(offset=offset), self.assertRaises(boot.VerificationError):
                boot.parse_boot_v4(bytes(damaged))
        for damaged in (data[:4095], data[:8192], data + b"unexpected tail"):
            with self.assertRaises(boot.VerificationError):
                boot.parse_boot_v4(damaged)

    def test_native_cli_must_report_all_eight_exact_unique_kmis(self):
        values = ["aarch64/" + kmi for kmi in boot.KMIS]
        self.assertEqual(boot.check_supported_kmis("\n".join(reversed(values)) + "\n"), sorted(values))
        for values in (values[:-1], values + values[:1], values + ["x86_64/android13-5.15"], [kmi for kmi in boot.KMIS]):
            with self.assertRaises(boot.VerificationError):
                boot.check_supported_kmis("\n".join(values))

    def test_host_environment_removes_android_global_flags_and_uses_same_target_cache(self):
        settings = {"RUSTFLAGS": "-C link-arg=arm64.a", "CARGO_ENCODED_RUSTFLAGS": "wrong",
                    "CARGO_BUILD_TARGET": "aarch64-linux-android", "CC": "android-clang",
                    "CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER": "keep-for-the-android-build",
                    "ANDROID_NDK_HOME": "/sdk/ndk", "CARGO_TARGET_DIR": "/other"}
        with mock.patch.dict(os.environ, settings, clear=True):
            environment = boot.host_environment(Path("/source"))
        self.assertNotIn("RUSTFLAGS", environment)
        self.assertNotIn("CARGO_ENCODED_RUSTFLAGS", environment)
        self.assertNotIn("CARGO_BUILD_TARGET", environment)
        self.assertNotIn("CC", environment)
        self.assertEqual(environment["CARGO_TARGET_DIR"], str(Path("/source/target")))
        self.assertEqual(environment["ANDROID_NDK_HOME"], "/sdk/ndk")
        self.assertEqual(environment["CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER"], "keep-for-the-android-build")

    def test_commands_cannot_flash_and_nonzero_status_is_failure(self):
        with mock.patch.object(boot.subprocess, "run") as run:
            with self.assertRaisesRegex(boot.VerificationError, "Flashing"):
                boot.command(["ksud", "boot-patch", "--flash"], ROOT, {})
            run.assert_not_called()
            run.return_value = subprocess.CompletedProcess(["ksud"], 1)
            with self.assertRaisesRegex(boot.VerificationError, "exit 1"):
                boot.command(["ksud", "supported-kmis"], ROOT, {})
            run.side_effect = subprocess.TimeoutExpired("ksud", 180)
            with self.assertRaisesRegex(boot.VerificationError, "could not complete"):
                boot.command(["ksud", "supported-kmis"], ROOT, {})

    def test_wrong_runner_and_invalid_assets_fail_before_compilation(self):
        with mock.patch.object(boot.sys, "platform", "win32"), mock.patch.object(boot, "command") as run:
            with self.assertRaisesRegex(boot.VerificationError, "Linux"):
                boot.verify(ROOT, ROOT / "absent-report.json", "a" * 40, ROOT / "outside")
            run.assert_not_called()
        with mock.patch.object(boot.sys, "platform", "linux"), mock.patch.object(boot.platform, "machine", return_value="x86_64"), \
                mock.patch.object(boot, "validate_assets", side_effect=boot.VerificationError("staged bytes changed")), \
                mock.patch.object(boot, "command") as run:
            with self.assertRaisesRegex(boot.VerificationError, "staged bytes changed"):
                boot.verify(ROOT, ROOT / "absent-report.json", "a" * 40, ROOT / "outside")
            run.assert_not_called()

    def cfg_source(self, content):
        root = ROOT / "tests" / (".manager-host-cfg-" + uuid.uuid4().hex)
        path = root / "userspace/ksud/src/main.rs"
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        self.addCleanup(shutil.rmtree, root)
        return root, path

    def test_host_only_cfg_adjustment_preserves_all_other_rust_bytes(self):
        original = b'mod defs;\nmod feature;\nmod feature_id;\nfn main() {}\n'
        source, main = self.cfg_source(original)
        shared = main.parent / "boot_patch.rs"
        shared.write_bytes(b"opaque shared Rust code, must remain byte-identical\n")
        shared_before = shared.read_bytes()
        patches = boot.apply_host_cfg(source)
        self.assertEqual(main.read_bytes(), original.replace(b"mod feature;", b'#[cfg(target_os = "android")]\nmod feature;'))
        self.assertEqual(shared.read_bytes(), shared_before)
        self.assertEqual(patches[0]["scope"], "isolated-host-verification-copy-only")
        self.assertEqual(patches[0]["before_sha256"], boot.digest(original))
        self.assertEqual(boot.apply_host_cfg(source), [])

    def test_unknown_cfg_or_module_layout_fails_without_writing(self):
        for content in (b"mod feature_id;\n", b"mod feature;\nmod feature;\n",
                        b'#[cfg(target_os = "linux")]\nmod feature;\n'):
            source, path = self.cfg_source(content)
            with self.assertRaises(boot.VerificationError):
                boot.apply_host_cfg(source)
            self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
