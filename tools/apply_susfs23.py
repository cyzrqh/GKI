#!/usr/bin/env python3
"""Apply the complete SUSFS 2.3 port to SukiSU's reviewed kernel and UAPI trees.

The patch is a three-way port of simonpunk/susfs4ksu's official
10_enable_susfs_for_ksu.patch at b57ed1998b82c4e3a2a81cdbf11172a560979c40
to SukiSU-Ultra 17ff94d9da24d589a9705227fdda39a1c9045698. New official
revisions can use this port when their complete kernel/UAPI pair is reviewed,
or checked-patch policy proves the complete port and native interfaces again.
It contains the
real preinit runner and SELinux implementations and preserves SukiSU's KPM,
extensions and manager support. See the patch header for upstream attribution.
Run after kernel/setup.sh, before other tools modify the KernelSU source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from validate_susfs_hooks import HOOKS, inspect_c_source, strip_comments_and_literals
from pin_build_dependencies import CompatibilityError, load_profile, reviewed_tree_pairs, source_policy


PROFILE_ROOT = Path(__file__).resolve().parents[1]
SUSFS_VERSION = "v2.3.0"
PATCH_PATH = Path(__file__).resolve().parent / "patches" / "sukisu-susfs23.patch"
PATCH_MEMORY_ARCHES = ("arm64", "x86_64", "riscv64")


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), "-c", "core.longpaths=true", *arguments],
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
        reviewed = reviewed_tree_pairs(profile)
        policy = source_policy(profile)
    except CompatibilityError as error:
        raise ValueError(str(error)) from error
    result = git(repository, "rev-parse", "HEAD")
    actual = result.stdout.strip().lower()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40}", actual):
        raise ValueError("Cannot verify the SukiSU checkout commit")
    if reviewed or policy == "checked-patch":
        # Comparing complete Git trees covers additions, deletions, modes and
        # symlinks as well as file contents. The root UAPI tree is included via
        # kernel/include/uapi -> ../../uapi, and is part of the same ABI review.
        found = []
        for path in ("kernel", "uapi"):
            tree = git(repository, "rev-parse", "--verify", f"HEAD:{path}")
            value = tree.stdout.strip().lower()
            if tree.returncode or not re.fullmatch(r"[0-9a-f]{40}", value):
                raise ValueError(f"Cannot verify the SukiSU {path} tree at {actual}")
            found.append(value)
        if tuple(found) not in reviewed and policy != "checked-patch":
            path = "kernel" if all(pair[0] != found[0] for pair in reviewed) else "uapi"
            raise ValueError(
                f"SUSFS 2.3 port requires the reviewed SukiSU {path} tree in its complete kernel/UAPI pair; "
                f"commit {actual} has kernel {found[0]} and UAPI {found[1]}. "
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
    content = patch.read_text(encoding="utf-8")
    paths = tuple(re.findall(r'^diff --git a/(kernel/\S+) b/\1$', content, re.M))
    if (not paths or len(paths) != len(re.findall(r'^diff --git ', content, re.M))
            or any(".." in Path(path).parts for path in paths)):
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


def verify_avc_lifecycle(core: str, boot_event: str) -> None:
    """Keep the selected upstream's AVC feature connected to its native lifecycle."""
    for name, content in (("core init", core), ("boot event", boot_event)):
        includes = re.sub(r"/\*.*?\*/|//[^\n]*", "", content, flags=re.S)
        if not re.search(r'(?m)^\s*#\s*include\s+"feature/avc_spoof\.h"', includes):
            raise ValueError(f"SukiSU AVC {name} must include its native feature header")
    for symbol, content in (("ksu_avc_spoof_init", core), ("ksu_avc_spoof_exit", core),
                            ("ksu_avc_spoof_late_init", boot_event)):
        calls = re.findall(r"\b" + symbol + r"\s*\(\s*\)\s*;", strip_comments_and_literals(content))
        if len(calls) != 1:
            raise ValueError(f"SukiSU AVC lifecycle requires exactly one {symbol} call")


def verify_native_avc(repository: Path) -> None:
    if git(repository, "cat-file", "-e", "HEAD:kernel/feature/avc_spoof.c").returncode:
        return
    source = repository / "kernel"
    paths = ("kernel/feature/avc_spoof.c", "kernel/feature/avc_spoof.h")
    if (any(not (repository / path).is_file() for path in paths)
            or git(repository, "diff", "--quiet", "HEAD", "--", *paths).returncode):
        raise ValueError("SukiSU AVC provider must retain its selected, real implementation")
    kbuild = re.sub(r"#[^\n]*", "", (source / "Kbuild").read_text(encoding="utf-8"))
    if not re.search(r"(?m)^\s*kernelsu-objs\s*\+=\s*feature/avc_spoof\.o\s*$", kbuild):
        raise ValueError("SukiSU AVC provider must remain linked in Kbuild")
    verify_avc_lifecycle((source / "core/init.c").read_text(encoding="utf-8"),
                         (source / "runtime/boot_event.c").read_text(encoding="utf-8"))


def allowlist_umount_body(content: str) -> str:
    code = strip_comments_and_literals(content)
    signature = re.search(r"\bbool\s+ksu_uid_should_umount\s*\(\s*uid_t\s+uid\s*\)\s*\{", code)
    if signature is None:
        raise ValueError("Selected SukiSU allowlist umount function is missing")
    depth = 1
    for offset in range(signature.end(), len(code)):
        depth += (code[offset] == "{") - (code[offset] == "}")
        if depth == 0:
            return code[signature.end():offset]
    raise ValueError("Selected SukiSU allowlist umount function is incomplete")


def verify_native_rcu_allowlist(repository: Path) -> None:
    selected = git(repository, "show", "HEAD:kernel/policy/allowlist.c")
    if selected.returncode or "hash_for_each_possible_rcu" not in selected.stdout:
        return
    original = allowlist_umount_body(selected.stdout)
    if "hash_for_each_possible_rcu" not in original:
        return
    # The SUSFS direct-hook port removes the manager exemption; the selected
    # upstream's complete RCU lookup and decision logic must otherwise survive.
    guard = (r"if\s*\(\s*likely\s*\(\s*ksu_is_manager_appid_valid\s*\(\s*\)\s*\)\s*&&\s*"
             r"unlikely\s*\(\s*ksu_get_manager_appid\s*\(\s*\)\s*==\s*uid\s*%\s*PER_USER_RANGE\s*\)\s*\)"
             r"\s*\{\s*return\s+false\s*;\s*\}")
    expected, count = re.subn(guard, "", original)
    if count > 1:
        raise ValueError("Selected SukiSU allowlist has an ambiguous manager exemption")
    actual = allowlist_umount_body((repository / "kernel/policy/allowlist.c").read_text(encoding="utf-8"))
    if re.sub(r"\s+", "", expected) != re.sub(r"\s+", "", actual):
        raise ValueError("SukiSU native RCU allowlist lookup and decisions must retain the selected implementation")


def verify_integration(repository: Path) -> None:
    source = repository / "kernel"
    verify_patch_memory_sources(repository)
    verify_patch_memory_build((source / "Kbuild").read_text(encoding="utf-8"))
    verify_native_avc(repository)
    verify_native_rcu_allowlist(repository)
    # An upstream logging cleanup must survive the SUSFS hook replacement.
    original_setuid = git(repository, 'show', 'HEAD:kernel/hook/setuid_hook.c')
    if original_setuid.returncode == 0:
        original = strip_comments_and_literals(original_setuid.stdout)
        current = strip_comments_and_literals((source / 'hook/setuid_hook.c').read_text(encoding='utf-8'))
        if len(re.findall(r'\bpr_info\s*\(', current)) > len(re.findall(r'\bpr_info\s*\(', original)):
            raise ValueError('SUSFS hook port must preserve upstream setuid logging cleanup')
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


def seed_port_bases(repository: Path, candidates: list[Path]) -> None:
    """Keep reviewed merge preimages available after an upstream history rewrite."""
    root = PROFILE_ROOT / 'tools/patches/sukisu-susfs23-base'
    path = root / 'objects.json'
    if not path.is_file():
        return
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError('Invalid reviewed SUSFS merge-base inventory')
    document = json.loads(path.read_text('utf-8'))
    if (not isinstance(document, dict) or document.get('schema_version') != 1
            or not isinstance(document.get('objects'), dict) or not 1 <= len(document['objects']) <= 128):
        raise ValueError('Invalid reviewed SUSFS merge-base inventory')
    required = {value for candidate in candidates for value in re.findall(
        r'^index ([0-9a-f]{40})\.\.[0-9a-f]{40}', candidate.read_text('utf-8'), re.M)}
    for identity in required & document['objects'].keys():
        digest = document['objects'][identity]
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid reviewed SUSFS merge-base digest')
        source = root / (digest + '.source')
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Missing reviewed SUSFS merge-base source')
        raw = source.read_bytes()
        if (hashlib.sha256(raw).hexdigest() != digest
                or hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != identity):
            raise ValueError('Reviewed SUSFS merge base differs from its locked identity')
        stored = git(repository, 'hash-object', '-w', '--no-filters', '--', str(source))
        if stored.returncode or stored.stdout.strip() != identity:
            raise ValueError('Could not restore the reviewed SUSFS Git merge base')


def apply_three_way_port(repository: Path, candidates: list[Path]) -> bool | None:
    """Accept only an actual conflict-free, reversible merge in a separate clone."""
    from snapshot_ksu_source import temporary_directory
    reference = git(repository, "rev-parse", "HEAD")
    if reference.returncode:
        return None
    with temporary_directory(repository.parent, ".gki-susfs-merge-") as temporary:
        checked = temporary / "source"
        bundle = temporary / "source.bundle"
        if git(repository, "bundle", "create", str(bundle), "HEAD", "--tags").returncode:
            return None
        cloned = git(temporary, "clone", "--quiet", "--no-checkout",
                     "--config", "core.autocrlf=false", str(bundle), str(checked))
        if cloned.returncode:
            return None
        seed_port_bases(checked, candidates)
        for candidate in candidates:
            if git(checked, "reset", "--hard", "--quiet", reference.stdout.strip()).returncode:
                return None
            merged = git(checked, "apply", "--3way", str(candidate))
            if merged.returncode or git(checked, "ls-files", "--unmerged").stdout.strip():
                continue
            if git(checked, "diff", "--quiet", "HEAD", "--", "uapi").returncode:
                continue
            try:
                verify_integration(checked)
            except (OSError, ValueError):
                continue
            materialized = git(checked, "diff", "--binary", "--full-index", "HEAD", "--", "kernel")
            if materialized.returncode or not materialized.stdout.strip():
                continue
            # --3way --check can return success while reporting conflicts.
            # Require a real reverse operation and a completely restored source.
            reversed_port = git(checked, "apply", "--reverse", "--3way", str(candidate))
            if (reversed_port.returncode or git(checked, "ls-files", "--unmerged").stdout.strip()
                    or git(checked, "diff", "--quiet", "HEAD", "--", "kernel", "uapi").returncode):
                continue
            prepared = temporary / "verified-merge.patch"
            prepared.write_text(materialized.stdout, encoding="utf-8", newline="\n")
            # The materialized patch is based on this exact HEAD. Applying it
            # uses the ordinary clean-source, reverse and native checks again.
            return apply_reviewed_port(repository, prepared)
    return None


def apply_reviewed_port(repository: Path, patch: Path, *, allow_variants: bool = False) -> bool:
    """Prove and apply the real port, for prepare clones and kernel jobs alike.

    This checks source and patch consistency, not a kernel compile or device boot.
    A matching recorded source tree never bypasses any integration validation.
    """
    verify_patch_memory_sources(repository)
    patch = patch.resolve(strict=True)
    candidates = [patch]
    if allow_variants:
        for name in ("sukisu-susfs23-avc.patch", "sukisu-susfs23-avc-rcu.patch", "sukisu-susfs23-quiet.patch"):
            variant = patch.with_name(name)
            if variant.is_file() and variant.resolve() not in candidates:
                candidates.append(variant.resolve(strict=True))
    paths = tuple(dict.fromkeys(path for candidate in candidates for path in patch_files(candidate)))
    if git(repository, "diff", "--quiet", "HEAD", "--", "uapi").returncode:
        raise ValueError("SukiSU UAPI files differ from the selected source")
    # A complete reverse dry run confirms every hunk is present, not just a marker.
    for candidate in candidates:
        reverse = git(repository, "apply", "--reverse", "--check", str(candidate))
        if reverse.returncode == 0:
            verify_integration(repository)
            return False
    # Refuse partial application or other local edits, including staged changes.
    clean = git(repository, "diff", "--quiet", "HEAD", "--", *paths)
    selected = None
    if clean.returncode == 0:
        for candidate in candidates:
            checked = git(repository, "apply", "--check", str(candidate))
            if checked.returncode == 0:
                selected = candidate
                break
    if selected is None:
        if allow_variants:
            merged = apply_three_way_port(repository, candidates)
            if merged is not None:
                return merged
        if clean.returncode:
            raise ValueError("SukiSU integration files differ from the pinned source; refusing a partial or conflicting patch")
        raise ValueError("Complete SUSFS 2.3 patch does not apply; no source was changed. " + checked.stderr.strip())
    applied = git(repository, "apply", str(selected))
    if applied.returncode:
        raise ValueError("Git could not apply the complete SUSFS 2.3 patch: " + applied.stderr.strip())
    verified = git(repository, "apply", "--reverse", "--check", str(selected))
    if verified.returncode:
        raise ValueError("SUSFS 2.3 patch failed its complete reverse verification")
    if git(repository, "diff", "--quiet", "HEAD", "--", "uapi").returncode:
        raise ValueError("SUSFS 2.3 integration changed the selected SukiSU UAPI")
    verify_integration(repository)
    return True


def apply_integration(kernel_root: Path, susfs_dir: Path, patch: Path = PATCH_PATH) -> bool:
    repository = find_repository(kernel_root)
    check_versions(repository, susfs_dir)
    return apply_reviewed_port(repository, patch, allow_variants=patch.resolve() == PATCH_PATH.resolve())


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
