#!/usr/bin/env python3
"""Apply the complete SUSFS 2.3 port to SukiSU's reviewed kernel and UAPI trees.

The patch is a three-way port of simonpunk/susfs4ksu's official
10_enable_susfs_for_ksu.patch at b57ed1998b82c4e3a2a81cdbf11172a560979c40
to SukiSU-Ultra 17ff94d9da24d589a9705227fdda39a1c9045698. New official
manager/ksud revisions can use this port when both source trees are identical.
It contains the
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

from validate_susfs_hooks import HOOKS, inspect_c_source, strip_comments_and_literals
from pin_build_dependencies import CompatibilityError, load_profile


PROFILE_ROOT = Path(__file__).resolve().parents[1]
SUSFS_VERSION = "v2.3.0"
PATCH_PATH = Path(__file__).resolve().parent / "patches" / "sukisu-susfs23.patch"
PATCH_MEMORY_ARCHES = ("arm64", "x86_64", "riscv64")


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
    try:
        profile = load_profile(PROFILE_ROOT)
    except CompatibilityError as error:
        raise ValueError(str(error)) from error
    result = git(repository, "rev-parse", "HEAD")
    actual = result.stdout.strip().lower()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40}", actual):
        raise ValueError("Cannot verify the SukiSU checkout commit")
    if "reviewed_kernel_tree" in profile:
        # Comparing complete Git trees covers additions, deletions, modes and
        # symlinks as well as file contents. The root UAPI tree is included via
        # kernel/include/uapi -> ../../uapi, and is part of the same ABI review.
        for path, field in (("kernel", "reviewed_kernel_tree"), ("uapi", "reviewed_uapi_tree")):
            expected = profile.get(field)
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", expected):
                raise ValueError(f"Reviewed SukiSU source profile is missing a valid {field}")
            tree = git(repository, "rev-parse", "--verify", f"HEAD:{path}")
            found = tree.stdout.strip().lower()
            if tree.returncode or found != expected.lower():
                raise ValueError(
                    f"SUSFS 2.3 port requires the reviewed SukiSU {path} tree {expected}; "
                    f"commit {actual} has {found or 'unavailable'}. "
                    "Review and adapt the port before building this upstream revision."
                )
        unchanged = git(repository, "diff", "--quiet", "HEAD", "--", "uapi")
        if unchanged.returncode:
            raise ValueError("SukiSU UAPI files differ from the reviewed source")
    elif actual not in {commit.lower() for commit in profile["ksu_commits"]}:
        raise ValueError("SUSFS 2.3 port requires SukiSU reviewed by build-compatibility.json; "
                         f"found {actual}")
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


def verify_patch_memory_sources(repository: Path) -> None:
    """Keep mount_hide's real text patcher from the pinned source, never a shim."""
    paths = tuple(f"kernel/hook/{arch}/patch_memory.c" for arch in PATCH_MEMORY_ARCHES)
    unchanged = git(repository, "diff", "--quiet", "HEAD", "--", *paths)
    if unchanged.returncode:
        raise ValueError("SukiSU text patch providers differ from the pinned source")
    for relative in paths:
        code = strip_comments_and_literals((repository / relative).read_text(encoding="utf-8"))
        if not re.search(r"(?m)^int\s+ksu_patch_text\s*\([^;{}]+\)\s*\{", code):
            raise ValueError(f"Missing real global ksu_patch_text definition in {relative}")
        if not re.search(r"\bstop_machine(?:_cpuslocked)?\s*\(", code):
            raise ValueError(f"Incomplete SukiSU text patch implementation in {relative}")


def verify_patch_memory_build(kbuild: str) -> None:
    """The direct hook port still needs SukiSU mount hiding's arch provider."""
    rules = (
        "ifeq ($(CONFIG_ARM64),y)\nkernelsu-objs += hook/arm64/patch_memory.o",
        "else ifeq ($(CONFIG_X86_64),y)\nkernelsu-objs += hook/x86_64/patch_memory.o",
        "else ifeq ($(CONFIG_RISCV),y)\nkernelsu-objs += hook/riscv64/patch_memory.o\nendif",
    )
    if any(rule not in kbuild for rule in rules):
        raise ValueError("SukiSU mount_hide requires the architecture's real patch_memory object in Kbuild")
    if any(f"kernelsu-objs += {object_name}" in kbuild for object_name in (
        "hook/lsm_hook.o", "hook/syscall_hook_manager.o", "hook/syscall_event_bridge.o",
        "hook/arm64/syscall_hook.o", "hook/x86_64/syscall_hook.o", "hook/riscv64/syscall_hook.o",
    )):
        raise ValueError("Legacy syscall hook registration must not replace SUSFS 2.3 direct hooks")


def verify_blacklist_lifecycle(core: str, runtime: str) -> None:
    """SukiSU configures static params, then merges the list in second-stage init."""
    core_code = strip_comments_and_literals(core)
    runtime_code = strip_comments_and_literals(runtime)
    if re.search(r"\bksu_module_blacklist_(?:init|apply)\s*\(", core_code):
        raise ValueError("SukiSU module blacklist must not use a foreign init hook or apply before second-stage init")
    if len(re.findall(r"\bksu_module_blacklist_exit\s*\(", core_code)) != 1:
        raise ValueError("SukiSU module blacklist teardown hook is missing")
    guard = re.search(r'if\s*\(check_argv\(\*argv,\s*1,\s*"second_stage",[^{};]+\)\)\s*\{', runtime)
    if guard is None:
        raise ValueError("SukiSU module blacklist second-stage init guard is missing")
    end = runtime.find("static_branch_disable(&is_init_second_stage_not_executed)", guard.end())
    body = strip_comments_and_literals(runtime[guard.end():end]) if end >= 0 else ""
    apply = body.find("ksu_module_blacklist_apply();")
    ready = body.find("ksu_feature_apply_forced();")
    if (len(re.findall(r"\bksu_module_blacklist_apply\s*\(", runtime_code)) != 1
            or ready < 0 or apply <= ready):
        raise ValueError("SukiSU module blacklist must apply exactly once after second-stage setup")


def verify_preinit_fd_cleanup(preinit: str, util: str) -> None:
    """Use SukiSU's real close syscall wrapper on every supported GKI series."""
    code = strip_comments_and_literals(preinit)
    if re.search(r"\bclose_fd\s*\(", code):
        raise ValueError("SukiSU preinit must use ksu_close_fd; close_fd is unavailable on GKI 5.10")
    for argument in (r"fd", r"\*\s*preinit_fd"):
        if not re.search(r"\bksu_close_fd\s*\(\s*" + argument + r"\s*\)\s*;", code):
            raise ValueError("SukiSU preinit must retain both real descriptor cleanup paths")
    if not re.search(r'(?m)^\s*#\s*include\s+"util\.h"', preinit):
        raise ValueError("SukiSU preinit must include its native descriptor cleanup helper")
    # util.h is also checked against HEAD below. Confirm that this helper invokes
    # the real close syscall rather than accepting a stub with the same name.
    util_code = re.sub(r"/\*.*?\*/|//[^\n]*", "", util, flags=re.S)
    if not re.search(r"(?m)^\s*#\s*define\s+ksu_close_fd\s*\(fd\)\s*"
                     r"\(\{\s*ksyscall\s*\(close,\s*fd\)\s*;\s*\}\)\s*$", util_code):
        raise ValueError("SukiSU ksu_close_fd must retain its native close syscall implementation")


def verify_integration(repository: Path) -> None:
    source = repository / "kernel"
    verify_patch_memory_sources(repository)
    verify_patch_memory_build((source / "Kbuild").read_text(encoding="utf-8"))
    verify_blacklist_lifecycle((source / "core/init.c").read_text(encoding="utf-8"),
                               (source / "runtime/ksud_integration.c").read_text(encoding="utf-8"))
    original_blacklist = git(repository, "diff", "--quiet", "HEAD", "--",
                             "kernel/feature/module_blacklist.c", "kernel/feature/module_blacklist.h")
    if original_blacklist.returncode:
        raise ValueError("SukiSU module blacklist provider must retain its pinned, real implementation")
    original_util = git(repository, "diff", "--quiet", "HEAD", "--", "kernel/include/util.h")
    if original_util.returncode:
        raise ValueError("SukiSU descriptor cleanup helper differs from the pinned implementation")
    verify_preinit_fd_cleanup((source / "preinit/preinit.c").read_text(encoding="utf-8"),
                              (source / "include/util.h").read_text(encoding="utf-8"))
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
                   "kernelsu-objs += feature/module_blacklist.o",
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
    verify_patch_memory_sources(repository)
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
