#!/usr/bin/env python3
"""Apply the reviewed, complete SUSFS 2.3 port to the pinned SukiSU source.

The patch is a three-way port of simonpunk/susfs4ksu's official
10_enable_susfs_for_ksu.patch at b57ed1998b82c4e3a2a81cdbf11172a560979c40
to SukiSU-Ultra 17ff94d9da24d589a9705227fdda39a1c9045698. It contains the
real preinit runner and SELinux implementations and preserves SukiSU's KPM,
extensions and manager support. See the patch header for upstream attribution.
Run after kernel/setup.sh, before other tools modify the KernelSU source.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

from validate_susfs_hooks import HOOKS, inspect_c_source


SUKISU_COMMIT = "17ff94d9da24d589a9705227fdda39a1c9045698"
SUSFS_VERSION = "v2.3.0"
PATCH_PATH = Path(__file__).resolve().parent / "patches" / "sukisu-susfs23.patch"


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def find_repository(kernel_root: Path) -> Path:
    root = kernel_root.resolve(strict=True)
    for candidate in (root / "KernelSU", root, root / "common/drivers/kernelsu", root / "drivers/kernelsu"):
        candidate = candidate.resolve()
        if not ((candidate / "kernel/Kbuild").is_file() or (candidate / "Kbuild").is_file()):
            continue
        result = git(candidate, "rev-parse", "--show-toplevel")
        if result.returncode == 0:
            repository = Path(result.stdout.strip()).resolve()
            if (repository / "kernel/Kbuild").is_file():
                return repository
    raise ValueError(f"SukiSU Git checkout not found below {root}; run kernel/setup.sh first")


def check_versions(repository: Path, susfs_dir: Path) -> None:
    result = git(repository, "rev-parse", "HEAD")
    if result.returncode or result.stdout.strip() != SUKISU_COMMIT:
        actual = result.stdout.strip() or "unavailable"
        raise ValueError(f"SUSFS 2.3 port requires SukiSU {SUKISU_COMMIT}; found {actual}")
    directory = susfs_dir.resolve(strict=True)
    header = next((path for path in (
        directory / "kernel_patches/include/linux/susfs.h",
        directory / "include/linux/susfs.h",
        directory / "susfs.h",
    ) if path.is_file()), None)
    if header is None:
        raise ValueError(f"SUSFS source header not found below {directory}")
    versions = re.findall(r'^\s*#define\s+SUSFS_VERSION\s+"([^"\r\n]+)"\s*$',
                          header.read_text(encoding="utf-8"), re.M)
    if versions != [SUSFS_VERSION]:
        raise ValueError(f"SUSFS 2.3 port requires {SUSFS_VERSION}; header reports {versions!r}")


def patch_files(patch: Path) -> tuple[str, ...]:
    paths = tuple(re.findall(r'^diff --git a/(kernel/\S+) b/\1$',
                             patch.read_text(encoding="utf-8"), re.M))
    if not paths or any(".." in Path(path).parts for path in paths):
        raise ValueError("Reviewed SUSFS integration patch has an invalid file list")
    return paths


def verify_integration(repository: Path) -> None:
    source = repository / "kernel"
    definitions = []
    for relative in ("preinit/preinit.c", "feature/selinux_hide.c"):
        path = source / relative
        _, found = inspect_c_source(path, path.read_text(encoding="utf-8"))
        definitions.extend(found)
    for symbol in HOOKS:
        found = [item for item in definitions if item.symbol == symbol and not item.is_static]
        if len(found) != 1:
            raise ValueError(f"SUSFS 2.3 requires one real global implementation of {symbol}")
    required = {
        "Kconfig": ("config KSU_SUSFS\n", "config KSU_SUSFS_SUS_MAP\n", "config KPM\n"),
        "Kbuild": ("kernelsu-objs += preinit/preinit.o", "kernelsu-objs += preinit/blob.o",
                   "kernelsu-objs += feature/selinux_hide.o", "kernelsu-objs += selinux/selinux.o",
                   "kernelsu-objs += extension/extension.o",
                   "kernelsu-objs += extension/kpm.o", "CONFIG_KPM", "preinit/runner.elf"),
        "core/init.c": ("susfs_init();", "ksu_preinit_init();", "ksu_extension_init();",
                        "ksu_extension_register_all();", "ksu_extension_apply_forced();"),
        "preinit/preinit.c": ("shmem_file_setup(", "kernel_write(", "fd_install(", "ksu_close_fd("),
        "runtime/ksud_integration.c": ("ksu_module_blacklist_apply();", "struct user_arg_ptr *argv"),
        "preinit/blob.S": ("KSU_PREINIT_RUNNER_BIN",),
        "preinit/runner/runner.c": ("int main(", "execve(INIT_PATH, argv, envp);"),
    }
    for relative, markers in required.items():
        content = (source / relative).read_text(encoding="utf-8")
        for marker in markers:
            if marker not in content:
                raise ValueError(f"Incomplete SUSFS 2.3 integration: {relative} lacks {marker!r}")


def apply_integration(kernel_root: Path, susfs_dir: Path, patch: Path = PATCH_PATH) -> bool:
    repository = find_repository(kernel_root)
    check_versions(repository, susfs_dir)
    patch = patch.resolve(strict=True)
    paths = patch_files(patch)
    # A complete reverse dry run confirms every hunk is present, not just a marker.
    reverse = git(repository, "apply", "--reverse", "--check", str(patch))
    if reverse.returncode == 0:
        verify_integration(repository)
        return False
    # Refuse partial application or other local edits, including staged changes.
    clean = git(repository, "diff", "--quiet", "HEAD", "--", *paths)
    if clean.returncode:
        raise ValueError("SukiSU integration files differ from the pinned source; refusing a partial or conflicting patch")
    checked = git(repository, "apply", "--check", str(patch))
    if checked.returncode:
        raise ValueError("Complete SUSFS 2.3 patch does not apply; no source was changed. " + checked.stderr.strip())
    applied = git(repository, "apply", str(patch))
    if applied.returncode:
        raise ValueError("Git could not apply the complete SUSFS 2.3 patch: " + applied.stderr.strip())
    verified = git(repository, "apply", "--reverse", "--check", str(patch))
    if verified.returncode:
        raise ValueError("SUSFS 2.3 patch failed its complete reverse verification")
    verify_integration(repository)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-root", required=True, type=Path)
    parser.add_argument("--susfs-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        changed = apply_integration(arguments.kernel_root, arguments.susfs_dir)
    except (OSError, ValueError) as error:
        print(f"::error::SukiSU / SUSFS 2.3 integration failed: {error}", file=sys.stderr)
        return 1
    print("SukiSU / SUSFS 2.3: complete integration " + ("applied." if changed else "already present and verified."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
