#!/usr/bin/env python3
"""Exercise official ksud patch/restore against a SYNTHETIC, NON-BOOTABLE fixture.

This checks the real staged LKM/ksuinit assets through ksud's native CLI. It is
not a phone boot test, an AVB/signature test, or evidence of device compatibility.
The generated image has an Android boot v4 container and an ARM64 Image header,
but its kernel payload and original init are explicitly synthetic test data.

Format references:
https://source.android.com/docs/core/architecture/bootloader/boot-image-header
https://github.com/5ec1cff/android_bootimg/tree/150425b027c76ea104c82e408571651f2181b2c2
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import struct
import subprocess
import sys

from manager_lkm_jobs import KMIS
from prepare_manager_lkm import (LkmError, atomic_json, inspect_module, public_identity,
                                 read_json, selected_source)
from snapshot_ksu_source import SnapshotError, git
from manager_variants import manager_profile, selected_variant


HOST = "x86_64-unknown-linux-gnu"
PAGE = 4096
HEADER_SIZE = 1584
MAX_IMAGE = 160 * 1024 * 1024
MAX_INIT = 64 * 1024 * 1024
FIXTURE_NOTICE = ("SYNTHETIC FORMAT TEST ONLY. NON-BOOTABLE KERNEL AND INIT. "
                  "DO NOT FLASH TO A DEVICE. THIS IS NOT A DEVICE BOOT TEST.\n")
FIXTURE_INIT = ("#!/system/bin/sh\n# " + FIXTURE_NOTICE + "exit 1\n").encode("ascii")


class VerificationError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    mode: int
    data: bytes


def aligned(value: int, boundary: int) -> int:
    return (value + boundary - 1) // boundary * boundary


def newc(entries: dict[str, Entry]) -> bytes:
    """Serialize only the tiny regular-file fixture; never extract to disk."""
    result = bytearray()
    for inode, (name, entry) in enumerate([*entries.items(), ("TRAILER!!!", Entry(0, b""))], 1):
        encoded = name.encode("ascii") + b"\0"
        fields = (inode, entry.mode, 0, 0, 1, 0, len(entry.data), 0, 0, 0, 0, len(encoded), 0)
        result.extend(b"070701" + b"".join(f"{value:08x}".encode("ascii") for value in fields))
        result.extend(encoded)
        result.extend(b"\0" * (aligned(len(result), 4) - len(result)))
        result.extend(entry.data)
        result.extend(b"\0" * (aligned(len(result), 4) - len(result)))
    return bytes(result)


def parse_newc(data: bytes) -> dict[str, Entry]:
    """Read bounded newc entries without following paths or links."""
    if not data or len(data) > MAX_IMAGE:
        raise VerificationError("Ramdisk is empty or exceeds the fixture size limit")
    entries, position = {}, 0
    while position + 110 <= len(data):
        header = data[position:position + 110]
        if not re.fullmatch(rb"070701[0-9a-fA-F]{104}", header):
            raise VerificationError("Expected an uncompressed newc ramdisk header")
        fields = [int(header[index:index + 8], 16) for index in range(6, 110, 8)]
        mode, size, name_size, checksum = fields[1], fields[6], fields[11], fields[12]
        name_start = position + 110
        name_end = name_start + name_size
        if not 1 <= name_size <= 4096 or name_end > len(data) or checksum:
            raise VerificationError("Invalid newc entry bounds or checksum")
        raw_name = data[name_start:name_end]
        if raw_name[-1:] != b"\0" or b"\0" in raw_name[:-1]:
            raise VerificationError("Invalid newc entry name")
        try:
            name = raw_name[:-1].decode("ascii")
        except UnicodeError as error:
            raise VerificationError("Fixture ramdisk entry name is not ASCII") from error
        start = aligned(name_end, 4)
        end = start + size
        position = aligned(end, 4)
        if end > len(data) or position > len(data):
            raise VerificationError("Truncated newc entry payload")
        if name == "TRAILER!!!":
            if size or any(data[position:]):
                raise VerificationError("Unexpected data after newc trailer")
            return entries
        path = PurePosixPath(name)
        if (not name or "\\" in name or path.is_absolute() or name != str(path)
                or any(part in {".", ".."} for part in path.parts) or name in entries):
            raise VerificationError("Unsafe or duplicate newc entry name")
        if stat.S_IFMT(mode) != stat.S_IFREG or fields[4] != 1:
            raise VerificationError("Synthetic ramdisk contains an unexpected non-regular entry")
        entries[name] = Entry(mode, data[start:end])
        if len(entries) > 64:
            raise VerificationError("Unexpected number of synthetic ramdisk entries")
    raise VerificationError("Missing newc trailer")


def synthetic_kernel() -> bytes:
    # This is a format header, NOT executable ARM64 kernel code.
    payload = FIXTURE_NOTICE.encode("ascii")
    size = PAGE
    header = struct.pack("<IIQQQQQQII", 0, 0, 0x80000, size, 0, 0, 0, 0, 0x644D5241, 0)
    return (header + payload).ljust(size, b"\0")


def boot_v4(kernel: bytes, ramdisk: bytes) -> bytes:
    """Create the unsigned v4 format fixture; it deliberately has no AVB data."""
    header = bytearray(PAGE)
    header[:8] = b"ANDROID!"
    struct.pack_into("<IIII", header, 8, len(kernel), len(ramdisk), 0, HEADER_SIZE)
    struct.pack_into("<I", header, 40, 4)
    cmdline = b"gki.synthetic_format_test=1 do_not_flash=1"
    header[44:44 + len(cmdline)] = cmdline
    # signature_size at offset 1580 is zero, like the four reserved u32s.
    return bytes(header) + kernel.ljust(aligned(len(kernel), PAGE), b"\0") + ramdisk.ljust(aligned(len(ramdisk), PAGE), b"\0")


def parse_boot_v4(data: bytes) -> tuple[bytes, dict[str, Entry]]:
    if not PAGE <= len(data) <= MAX_IMAGE or data[:8] != b"ANDROID!":
        raise VerificationError("Expected a bounded Android boot image")
    kernel_size, ramdisk_size, _, header_size = struct.unpack_from("<IIII", data, 8)
    if (header_size != HEADER_SIZE or struct.unpack_from("<I", data, 40)[0] != 4
            or any(data[24:40]) or struct.unpack_from("<I", data, 1580)[0]):
        raise VerificationError("Expected the unsigned synthetic Android boot v4 header")
    kernel_end = PAGE + kernel_size
    ramdisk_start = aligned(kernel_end, PAGE)
    ramdisk_end = ramdisk_start + ramdisk_size
    if (kernel_size < 64 or not ramdisk_size or kernel_end > len(data)
            or ramdisk_end > len(data) or any(data[ramdisk_end:])):
        raise VerificationError("Invalid synthetic boot payload bounds or unexpected tail")
    kernel = data[PAGE:kernel_end]
    if kernel[56:60] != b"ARMd":
        raise VerificationError("Synthetic kernel lost its ARM64 Image header")
    # The fixture uses raw newc, and official patcher preserves that format.
    return kernel, parse_newc(data[ramdisk_start:ramdisk_end])


def regular_bytes(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= limit:
        raise VerificationError("Expected a bounded regular verification input: " + path.name)
    return path.read_bytes()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def protected_host_sources(variant: str | None = None) -> tuple[str, ...]:
    common = ('userspace/ksud/src/boot_patch.rs', 'userspace/ksud/src/assets.rs',
              'userspace/ksud/src/cli_non_android.rs', 'userspace/ksud/build.rs',
              'userspace/ksud/Cargo.toml')
    if selected_variant(variant) == 'BakaSU':
        return (*common, 'userspace/ksud/Cargo.lock', 'userspace/ksuinit/Cargo.toml',
                'userspace/ksuinit/Cargo.lock')
    return (*common, 'Cargo.toml', 'Cargo.lock')


def validate_assets(source: Path, report_path: Path, reference: str) -> tuple[dict, dict[str, bytes], bytes]:
    provenance = selected_source(source, reference)
    git(source, "diff", "--quiet", "HEAD", "--", "userspace/ksud/src", *protected_host_sources())
    report = read_json(report_path)
    identity = public_identity(report_path)
    if (type(report.get("schema_version")) is not int or report["schema_version"] != 1
            or report.get("mode") != "native-lkm" or report.get("supported_kmis") != sorted(KMIS)
            or any(report.get(key) != value for key, value in provenance.items())):
        raise VerificationError("Native LKM report does not match the selected complete source build")
    rows = report.get("modules")
    if not isinstance(rows, list) or len(rows) != len(KMIS):
        raise VerificationError("Expected exactly eight verified native module records")
    assets = source / "userspace/ksud/bin/aarch64"
    if not assets.is_dir() or assets.resolve() != assets:
        raise VerificationError("Staged asset directory is absent or symlinked")
    if {path.name for path in assets.glob("*_kernelsu.ko")} != {kmi + "_kernelsu.ko" for kmi in KMIS}:
        raise VerificationError("Staged native module set does not match all eight KMIs")
    modules = {}
    for row in rows:
        if not isinstance(row, dict):
            raise VerificationError("Invalid native module record")
        kmi = row.get("kmi")
        if (not isinstance(kmi, str) or kmi not in KMIS or kmi in modules or row.get("arch") != "aarch64"
                or row.get("file") != kmi + "_kernelsu.ko"
                or any(row.get(key) != value for key, value in {**identity, **provenance}.items())):
            raise VerificationError("Native module identity does not match this build")
        path = assets / row["file"]
        actual = inspect_module(path, kmi, identity, provenance["ksu_ref"])
        if any(row.get(key) != value for key, value in actual.items()):
            raise VerificationError("Staged native module changed after verification")
        modules[kmi] = regular_bytes(path, MAX_INIT)
    init = regular_bytes(assets / "ksuinit", MAX_INIT)
    if (len(init) < 64 or init[:7] != b"\x7fELF\x02\x01\x01"
            or struct.unpack_from("<H", init, 16)[0] not in {2, 3}
            or struct.unpack_from("<H", init, 18)[0] != 183):
        raise VerificationError("Staged ksuinit must be the built ARM64 ELF executable")
    return provenance, modules, init


def host_environment(source: Path) -> dict[str, str]:
    environment = os.environ.copy()
    # Android-specific global flags can otherwise send ARM64 builtins to the
    # host linker. The explicit host target also overrides Cargo build.target.
    for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_RUSTFLAGS", "CARGO_BUILD_TARGET",
                "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS",
                "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER", "CC", "CXX", "AR",
                "CFLAGS", "CXXFLAGS", "LDFLAGS", "BINDGEN_EXTRA_CLANG_ARGS"):
        environment.pop(key, None)
    environment["CARGO_TARGET_DIR"] = str(source / "target")
    environment["CARGO_TERM_COLOR"] = "never"
    environment['KSU_PACKAGE_NAME'] = manager_profile(selected_variant())['package']
    return environment


def prepare_host_copy(source: Path, destination: Path, reference: str,
                      modules: dict[str, bytes], init: bytes) -> list[dict]:
    """Keep the actual Android build untouched, including its Rust main.rs.

    The current upstream native CLI accidentally declares the Android-only
    feature module unconditionally. Its sulog/ksucalls/utils imports do not
    exist on Linux. Restrict that module only in an isolated local checkout;
    the shared patch/restore code and asset loader remain byte-identical.
    """
    if destination.exists() or destination.is_symlink():
        raise VerificationError("Host verification source copy must not already exist")
    git(destination.parent, "clone", "--quiet", "--shared", "--no-checkout", str(source), str(destination))
    git(destination, "checkout", "--quiet", "--detach", reference)
    if selected_source(destination, reference) != selected_source(source, reference):
        raise VerificationError("Host copy did not preserve the selected official source identity")
    protected = protected_host_sources()
    for relative in protected:
        if regular_bytes(destination / relative, 4 * 1024 * 1024) != regular_bytes(source / relative, 4 * 1024 * 1024):
            raise VerificationError("Host verification must use unmodified official shared Rust code: " + relative)
    patches = apply_host_cfg(destination)
    assets = destination / "userspace/ksud/bin/aarch64"
    if not assets.is_dir() or assets.resolve() != assets:
        raise VerificationError("Host copy asset directory is absent or symlinked")
    for name, payload in [(kmi + "_kernelsu.ko", data) for kmi, data in modules.items()] + [("ksuinit", init)]:
        path = assets / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise VerificationError("Host copy asset path is not a regular file")
        path.write_bytes(payload)
    for relative in protected:
        if (destination / relative).read_bytes() != (source / relative).read_bytes():
            raise VerificationError("Shared Rust patch/restore source changed during host preparation")
    return patches


def apply_host_cfg(source: Path, *, variant: str | None = None) -> list[dict]:
    path = source / "userspace/ksud/src/main.rs"
    original = regular_bytes(path, 1024 * 1024)
    lines = original.splitlines(keepends=True)
    declarations = [index for index, line in enumerate(lines) if line.strip() == b"mod feature;"]
    if selected_variant(variant) == 'BakaSU':
        # BakaSU's Android modules are already gated in the official source.
        # Verify that layout without applying SukiSU's unrelated feature fix.
        guards = {b'mod android;': b'#[cfg(target_os = "android")]',
                  b'mod anykernel3;': b'#[cfg(target_os = "android")]',
                  b'mod cli_non_android;': b'#[cfg(not(target_os = "android"))]'}
        if declarations:
            raise VerificationError('Review the changed BakaSU native module layout before verification')
        for declaration, guard in guards.items():
            found = [index for index, line in enumerate(lines) if line.strip() == declaration]
            if len(found) != 1 or found[0] == 0 or lines[found[0] - 1].strip() != guard:
                raise VerificationError('BakaSU native verification requires its official Android module guards')
        return []
    if len(declarations) != 1:
        raise VerificationError("Review upstream feature module declaration before native verification")
    index = declarations[0]
    previous = lines[index - 1].strip() if index else b""
    if previous == b'#[cfg(target_os = "android")]':
        return []
    if previous.startswith(b"#[") or lines[index].strip(b"\r\n") != b"mod feature;":
        raise VerificationError("Unexpected cfg for the host-only verification source adjustment")
    newline = b"\r\n" if lines[index].endswith(b"\r\n") else b"\n"
    lines.insert(index, b'#[cfg(target_os = "android")]' + newline)
    changed = b"".join(lines)
    path.write_bytes(changed)
    return [{"file": "userspace/ksud/src/main.rs", "scope": "isolated-host-verification-copy-only",
             "change": 'Add #[cfg(target_os = "android")] to Android-only mod feature;',
             "before_sha256": digest(original), "after_sha256": digest(changed)}]


def command(arguments: list[str], source: Path, environment: dict, *, capture: bool = False,
            timeout: int = 180) -> str:
    if "--flash" in arguments:
        raise VerificationError("Flashing is prohibited in the synthetic image test")
    try:
        result = subprocess.run(arguments, cwd=source, env=environment, check=False,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                stdout=subprocess.PIPE if capture else None)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VerificationError("Verification command could not complete: " + Path(arguments[0]).name) from error
    if result.returncode:
        raise VerificationError("Verification command failed: " + Path(arguments[0]).name + " (exit " + str(result.returncode) + ")")
    return result.stdout if capture else ""


def check_supported_kmis(output: str) -> list[str]:
    found = output.splitlines()
    expected = {"aarch64/" + kmi for kmi in KMIS}
    if len(found) != len(expected) or set(found) != expected:
        raise VerificationError("Native ksud supported-kmis does not match the eight staged ARM64 modules")
    return sorted(found)


def check_patched(data: bytes, module: bytes, init: bytes) -> dict:
    kernel, entries = parse_boot_v4(data)
    if kernel != synthetic_kernel():
        raise VerificationError("Patching unexpectedly changed the synthetic kernel payload")
    expected = {"init": init, "init.real": FIXTURE_INIT, "kernelsu.ko": module}
    for name, payload in expected.items():
        entry = entries.get(name)
        if entry is None or entry.data != payload or entry.mode != stat.S_IFREG | 0o755:
            raise VerificationError("Patched ramdisk does not contain the exact expected executable " + name)
    if set(entries) - {*expected, "ksu_config"}:
        raise VerificationError("Unexpected files were added to the synthetic ramdisk")
    return {"image_sha256": digest(data), "module_sha256": digest(module),
            "ksuinit_sha256": digest(init), "original_init_sha256": digest(FIXTURE_INIT)}


def check_restored(data: bytes) -> None:
    kernel, entries = parse_boot_v4(data)
    if (kernel != synthetic_kernel() or entries.get("init") != Entry(stat.S_IFREG | 0o755, FIXTURE_INIT)
            or "init.real" in entries or "kernelsu.ko" in entries
            or set(entries) - {"init", "ksu_config"}):
        raise VerificationError("Official boot-restore did not restore the original synthetic init and remove the module")


def verify(source: Path, report_path: Path, reference: str, output: Path) -> dict:
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise VerificationError("Real native ksud verification requires an x86_64 Linux runner")
    source = source.resolve(strict=True)
    provenance, modules, init = validate_assets(source, report_path, reference)
    for path in (source / relative for relative in protected_host_sources()):
        regular_bytes(path, 4 * 1024 * 1024)
    target = source / "target"
    if target.resolve() != target:
        raise VerificationError("Cargo target directory must not be symlinked")
    output = output.absolute()
    if (output.resolve() != output or output.is_relative_to(source)
            or (output.exists() and (not output.is_dir() or any(output.iterdir())))):
        raise VerificationError("Synthetic image output must be a new or empty directory outside the source checkout")
    output.mkdir(parents=True, exist_ok=True)
    (output / "SYNTHETIC_TEST_ONLY_DO_NOT_FLASH.txt").write_text(FIXTURE_NOTICE, encoding="ascii")
    native_source = output / "native-source"
    host_patches = prepare_host_copy(source, native_source, provenance["ksu_ref"], modules, init)
    manifest = native_source / "userspace/ksud/Cargo.toml"
    fixture = output / "synthetic-format-only-do-not-flash.img"
    fixture_bytes = boot_v4(synthetic_kernel(), newc({"init": Entry(stat.S_IFREG | 0o755, FIXTURE_INIT)}))
    fixture.write_bytes(fixture_bytes)
    environment = host_environment(source)
    cargo = ['cargo', '+nightly'] if selected_variant() == 'BakaSU' else ['cargo']
    command([*cargo, "clean", "--manifest-path", str(manifest), "-p", "ksud", "--target", HOST, "--release"],
            native_source, environment)
    command([*cargo, "build", "--locked", "--target", HOST, "--release", "--manifest-path", str(manifest)],
            native_source, environment, timeout=1800)
    ksud = target / HOST / "release/ksud"
    host_binary = regular_bytes(ksud, MAX_IMAGE)
    if (len(host_binary) < 64 or host_binary[:7] != b"\x7fELF\x02\x01\x01"
            or struct.unpack_from("<H", host_binary, 16)[0] not in {2, 3}
            or struct.unpack_from("<H", host_binary, 18)[0] != 62):
        raise VerificationError("Native ksud build did not produce an x86_64 ELF executable")
    supported = check_supported_kmis(command([str(ksud), "supported-kmis"], native_source, environment, capture=True))
    results = []
    for kmi in KMIS:
        patched = output / (kmi + "-synthetic-patched.img")
        restored = output / (kmi + "-synthetic-restored.img")
        command([str(ksud), "boot-patch", "--boot", str(fixture), "--kmi", kmi, "--arch", "aarch64",
                 "--out", str(output), "--out-name", patched.name], native_source, environment)
        checked = check_patched(regular_bytes(patched, MAX_IMAGE), modules[kmi], init)
        command([str(ksud), "boot-restore", "--boot", str(patched), "--out", str(output),
                 "--out-name", restored.name], native_source, environment)
        check_restored(regular_bytes(restored, MAX_IMAGE))
        results.append({"kmi": kmi, **checked, "restored_sha256": digest(restored.read_bytes())})
        print("Verified official file patch/restore with embedded assets: " + kmi, flush=True)
    if fixture.read_bytes() != fixture_bytes:
        raise VerificationError("Official CLI changed its original synthetic fixture")
    # Catch an input race or asset mutation while Cargo embedded the binaries.
    final_provenance, final_modules, final_init = validate_assets(source, report_path, reference)
    if (final_provenance, final_modules, final_init) != (provenance, modules, init):
        raise VerificationError("Source or embedded asset inputs changed during verification")
    result = {"schema_version": 1, "verification": "official-native-cli-synthetic-image-patch-and-restore",
              "synthetic_fixture": True, "fixture_bootable": False, "device_boot_test": False,
              "notice": FIXTURE_NOTICE.strip(), **provenance, "host_target": HOST,
              "host_source_adjustments": host_patches,
              "execution_scope": "official host CLI with the same staged assets; not the APK ARM64 executable",
              "host_ksud_sha256": digest(host_binary), "supported_kmis": supported,
              "fixture_sha256": digest(fixture_bytes), "results": results}
    atomic_json(output / "verification.json", result)
    print("Synthetic format patch/restore checks passed; device boot was NOT tested.", flush=True)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--module-report", required=True, type=Path)
    parser.add_argument("--expected-ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        verify(args.source, args.module_report, args.expected_ref, args.output)
    except (VerificationError, LkmError, SnapshotError, OSError, UnicodeError, struct.error) as error:
        print("::error::Real manager boot patch verification stopped: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
