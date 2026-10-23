#!/usr/bin/env python3
"""Bounded directory cleanup for disposable GitHub-hosted Linux kernel runners."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


GIB = 1024 ** 3
MINIMUM_FREE = 40 * GIB
TARGET_FREE = 50 * GIB
TOTAL_SECONDS = 7 * 60  # The workflow step has an eight-minute outer limit.
DIRECTORY_SECONDS = 90
# These tools are not used by the kernel job. Keep the runner, system packages,
# Python/Node tool caches, workspace, downloaded kernel prebuilts and build caches.
CLEANUP_DIRECTORIES = tuple(Path(path) for path in (
    "/usr/local/lib/android", "/opt/android", "/usr/local/android-sdk", "/home/runner/Android",
    "/usr/share/dotnet", "/opt/ghc", "/usr/local/.ghcup", "/opt/cabal",
    "/home/runner/.ghcup", "/home/runner/.cabal", "/usr/share/swift",
    "/opt/hostedtoolcache/CodeQL", "/opt/hostedtoolcache/Ruby", "/opt/hostedtoolcache/go",
    "/usr/share/miniconda", "/usr/local/julia", "/usr/local/aws-cli",
    "/usr/local/aws-sam-cli", "/usr/share/gradle", "/usr/local/share/powershell",
    "/usr/local/share/chromium",
))


def environment_paths(environ: dict[str, str] | None = None) -> tuple[Path, tuple[Path, ...]]:
    environ = os.environ if environ is None else environ
    if (sys.platform != "linux" or environ.get("GITHUB_ACTIONS") != "true"
            or environ.get("GKI_RUNNER_ENVIRONMENT") != "github-hosted"):
        raise ValueError("Cleanup requires a GitHub-hosted Linux Actions runner")
    workspace_value = environ.get("GITHUB_WORKSPACE")
    if not workspace_value:
        raise ValueError("GITHUB_WORKSPACE is required for cleanup safety and disk checks")
    workspace = Path(workspace_value).resolve(strict=True)
    if not workspace.is_dir() or workspace == Path(workspace.anchor):
        raise ValueError("GITHUB_WORKSPACE must name a real non-root directory")
    protected = [workspace, Path(sys.executable).resolve(),
                 Path("/opt/hostedtoolcache/Python"), Path("/opt/hostedtoolcache/Node"),
                 Path("/home/runner/runners"), Path("/home/runner/actions-runner")]
    if environ.get("RUNNER_TEMP"):
        protected.append(Path(environ["RUNNER_TEMP"]).resolve())
    if environ.get("HOME"):
        home = Path(environ["HOME"]).resolve()
        protected.extend(home / path for path in (".cache", ".ccache", ".cargo", ".gradle"))
    return workspace, tuple(protected)


def safe_directory(path: Path, protected: tuple[Path, ...]) -> Path:
    if path not in CLEANUP_DIRECTORIES or not path.is_absolute() or ".." in path.parts:
        raise ValueError("Directory is not an explicitly allowed absolute cleanup path")
    # Reject a symlink at any component rather than deleting through a changed
    # parent directory. rm itself also does not follow symlinks inside the tree.
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Cleanup directory or one of its parents is a symlink")
    for reserved in protected:
        if path == reserved or path in reserved.parents or reserved in path.parents:
            raise ValueError("Cleanup directory overlaps the workspace, runner, interpreter or a build cache")
    return path


def print_disk_space(workspace: Path) -> None:
    # This is diagnostic only; the protective threshold uses disk_usage bytes.
    try:
        subprocess.run(["df", "-h", "--", str(workspace)], check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        print("::warning::Could not print df output; checking free bytes directly.", flush=True)
    print(f"Available space: {shutil.disk_usage(workspace).free / GIB:.1f} GiB", flush=True)


def clean(workspace: Path, protected: tuple[Path, ...]) -> None:
    deadline = time.monotonic() + TOTAL_SECONDS
    print_disk_space(workspace)
    for candidate in CLEANUP_DIRECTORIES:
        if shutil.disk_usage(workspace).free >= TARGET_FREE:
            print("50 GiB cleanup target reached; keeping remaining preinstalled directories.", flush=True)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 6:
            print("::warning::Cleanup time budget exhausted; checking remaining space.", flush=True)
            break
        try:
            path = safe_directory(candidate, protected)
        except (OSError, ValueError) as error:
            print(f"::warning::Skipping {candidate}: {error}", flush=True)
            continue
        if not path.exists():
            continue
        if not path.is_dir():
            print(f"::warning::Skipping non-directory {path}", flush=True)
            continue
        print(f"Removing unused preinstalled directory: {path}", flush=True)
        started = time.monotonic()
        limit = min(DIRECTORY_SECONDS, remaining - 6)
        try:
            result = subprocess.run(
                # timeout runs inside sudo so the real deletion also stops if
                # the Python-side wait expires; allow five seconds for SIGKILL.
                ["sudo", "-n", "/usr/bin/timeout", "--kill-after=5s", f"{limit:.1f}s",
                 "/usr/bin/rm", "-rf", "--", str(path)],
                check=False, timeout=limit + 6,
            )
            if result.returncode:
                print(f"::warning::Directory cleanup exited {result.returncode}: {path}", flush=True)
        except (OSError, subprocess.TimeoutExpired):
            print(f"::warning::Directory cleanup could not finish within its limit: {path}", flush=True)
        print(f"Directory cleanup elapsed: {time.monotonic() - started:.1f}s", flush=True)
    print_disk_space(workspace)
    free = shutil.disk_usage(workspace).free
    if free < MINIMUM_FREE:
        raise ValueError(f"Only {free / GIB:.1f} GiB is available; at least 40 GiB is required before source sync. "
                         "This protective minimum does not guarantee enough space for every kernel configuration.")
    print("Disk-space protection passed (40 GiB minimum, 50 GiB cleanup target).", flush=True)


def main() -> int:
    try:
        workspace, protected = environment_paths()
        clean(workspace, protected)
    except (OSError, ValueError) as error:
        print(f"::error::Kernel runner cleanup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
