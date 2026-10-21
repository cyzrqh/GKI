#!/usr/bin/env python3
"""Prepare a validated upstream-update PR without publishing or merging it.

The caller owns branch and pull-request creation. --prepare only changes local
imported files after staged validation succeeds. --check-only is read-only.
GITHUB_OUTPUT receives has_updates, workflow_changed, current_json, latest_json,
branch, title, and body_path. An unchanged or irrelevant upstream commit produces
has_updates=false and no report. No kernel compilation is performed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid


_SPEC = importlib.util.spec_from_file_location("gki_update_sync", Path(__file__).with_name("sync_upstream.py"))
sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync)

BRANCH = "automation/upstream-sync"
PR_TITLE = "chore: update GKI upstream dependencies"
PR_MARKER = "<!-- gki-upstream-sync:v1 -->"
SNAPSHOT_PATHS = frozenset(sync.ABK_SNAPSHOTS.values())
PROVENANCE_LINE = re.compile(r"^# (?:ABK|Version catalog) [0-9a-fA-F]{40}\r?\n?$")


class UpdateError(RuntimeError):
    """A preparation failure that must prevent pull-request publication."""


def run_command(arguments: list[str], root: Path, *, timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            arguments, cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"Could not run {arguments[0]}: {exc}") from exc
    if completed.returncode:
        output = (completed.stdout + "\n" + completed.stderr).strip()[-6000:]
        raise UpdateError(f"Validation command failed: {' '.join(arguments[:4])}\n{output}")
    return completed


def find_latest(lock: dict) -> dict[str, str]:
    return {
        source: sync.resolve_commit(lock[source]["repository"], lock[source]["branch"])
        for source in ("abk", "catalog")
    }


def source_changed(lock: dict, latest: dict[str, str]) -> bool:
    return any(lock[source]["commit"].lower() != latest[source].lower() for source in ("abk", "catalog"))


def heads_marker(latest: dict[str, str]) -> str:
    for source in ("abk", "catalog"):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", latest[source]):
            raise UpdateError(f"Invalid candidate SHA for {source}")
    return f"<!-- gki-upstream-heads:abk={latest['abk'].lower()};catalog={latest['catalog'].lower()} -->"


def _content(root: Path, name: str) -> bytes | None:
    path = sync._safe_path(root, name)
    return path.read_bytes() if path.is_file() else None


def workflow_content(content: bytes | None) -> bytes:
    """Ignore generated commit comments when deciding whether behavior changed."""
    if content is None:
        return b""
    text = content.decode("utf-8")
    return "".join(line for line in text.splitlines(keepends=True) if not PROVENANCE_LINE.fullmatch(line)).encode("utf-8")


def meaningful_changes(root: Path, staging: Path, before: dict[str, str], after: dict[str, str]) -> list[str]:
    names = sorted(set(before) | set(after))
    changed = [name for name in names if name not in SNAPSHOT_PATHS and _content(root, name) != _content(staging, name)]
    if workflow_content(_content(root, sync.WORKFLOW_PATH)) != workflow_content(_content(staging, sync.WORKFLOW_PATH)):
        changed.append(sync.WORKFLOW_PATH)
    return changed


def _validate(staging: Path) -> None:
    run_command([sys.executable, "tools/generate_workflow.py", "--root", str(staging)], staging)
    parse_code = (
        "import sys; from pathlib import Path; "
        "sys.path.insert(0, str(Path('tools').resolve())); "
        "import generate_workflow as g; "
        f"document = g.load_yaml(Path({sync.WORKFLOW_PATH!r})); "
        "assert isinstance(document, dict) and document.get('jobs') and document.get('on'), "
        "'Generated workflow has an invalid shape'"
    )
    run_command([sys.executable, "-c", parse_code], staging)
    run_command([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"], staging)


def prepare_updates(root: Path, lock: dict, latest: dict[str, str]) -> dict:
    """Validate in isolation and apply only a meaningful, fully checked import."""
    root = root.resolve()
    unchanged = {"has_updates": False, "workflow_changed": False, "changed_files": [], "meaningful_files": []}
    if not source_changed(lock, latest):
        return unchanged
    status = run_command(["git", "status", "--porcelain", "--untracked-files=normal"], root, timeout=60)
    if status.stdout.strip():
        raise UpdateError("Upstream PR preparation requires a clean checkout. Commit or move local changes first.")
    before = sync.read_manifest(root)
    sync._verify_live_files(root, before, {})
    protected = (sync.LOCK_PATH, sync.MANIFEST_PATH, sync.WORKFLOW_PATH)
    baseline = {name: _content(root, name) for name in protected}
    with sync._staging_tree(root) as staging:
        shutil.copytree(
            root, staging, symlinks=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", ".pytest_cache", ".sync-stage-*"),
        )
        run_command([
            sys.executable, "tools/sync_upstream.py", "--root", str(staging), "--update",
            "--abk-ref", latest["abk"], "--catalog-ref", latest["catalog"],
        ], staging)
        staged_lock = sync.read_lock(staging)
        for source in ("abk", "catalog"):
            if staged_lock[source]["commit"].lower() != latest[source].lower():
                raise UpdateError(f"Staged {source} commit does not match the resolved upstream commit")
            for key in ("repository", "branch"):
                if staged_lock[source][key] != lock[source][key]:
                    raise UpdateError(f"Staged update unexpectedly changed {source}.{key}")
        after = sync.read_manifest(staging)
        meaningful = meaningful_changes(root, staging, before, after)
        if not meaningful:
            print("Upstream commits changed only unused snapshots or metadata; no update PR is needed.")
            return unchanged
        _validate(staging)
        # Full tests must not change upstream import bytes after their hashes
        # were recorded by the sync tool.
        imports = {name: sync._safe_path(staging, name).read_bytes() for name in after}
        sync._verify_live_files(staging, after, imports)
        sync._verify_live_files(root, before, imports)
        if any(_content(root, name) != value for name, value in baseline.items()):
            raise UpdateError("The checkout changed during staged validation; rerun preparation")
        writes = {**imports, **{name: sync._safe_path(staging, name).read_bytes() for name in protected}}
        deletes = set(before) - after.keys()
        changed_files = sorted(name for name in writes if _content(root, name) != writes[name]) + sorted(deletes)
        workflow_changed = _content(root, sync.WORKFLOW_PATH) != writes[sync.WORKFLOW_PATH]
        sync._commit_changes(root, writes, deletes)
    return {
        "has_updates": True, "workflow_changed": workflow_changed,
        "changed_files": sorted(set(changed_files)), "meaningful_files": meaningful,
    }


def report_text(lock: dict, latest: dict[str, str], result: dict) -> str:
    lines = [
        PR_MARKER,
        heads_marker(latest),
        "更新影响本仓库构建流程、支持文件或内核版本目录的上游变更。",
        "",
        "| 上游 | 当前提交 | 更新提交 | 差异 |",
        "| --- | --- | --- | --- |",
    ]
    for source, label in (("abk", "ABK 构建流程"), ("catalog", "GKI 版本目录")):
        item = lock[source]
        old, new = item["commit"], latest[source]
        base = f"https://github.com/{item['repository']}"
        lines.append(f"| {label} | [`{old[:12]}`]({base}/commit/{old}) | [`{new[:12]}`]({base}/commit/{new}) | [查看]({base}/compare/{old}...{new}) |")
    lines.extend(["", "影响构建的文件：", ""])
    lines.extend(f"- `{name}`" for name in result["meaningful_files"])
    lines.extend([
        "", "验证：工作流重新生成、生成的 YAML 解析、完整 Python 单元测试均通过。",
        "此次检查没有编译内核；合并需由仓库所有者审核决定。", "",
    ])
    return "\n".join(lines)


def write_report(content: str, directory: Path | None = None) -> Path:
    parent = directory or Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    parent = parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / ("gki-upstream-update-" + uuid.uuid4().hex + ".md")
    target.write_text(content, encoding="utf-8", newline="\n")
    return target


def write_outputs(values: dict[str, str]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                delimiter = "gki_output_" + uuid.uuid4().hex
                stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                stream.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check-only", action="store_true", help="Only resolve upstream commits; do not import or generate files")
    action.add_argument("--prepare", action="store_true", help="Prepare validated local changes for the caller's update PR")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--report-dir", type=Path, help="Directory for the PR body; defaults to RUNNER_TEMP or the system temporary directory")
    parser.add_argument("--abk-commit", help="Exact ABK commit from the probe; requires --catalog-commit and --prepare")
    parser.add_argument("--catalog-commit", help="Exact catalog commit from the probe; requires --abk-commit and --prepare")
    args = parser.parse_args(argv)
    supplied = (args.abk_commit is not None, args.catalog_commit is not None)
    if any(supplied):
        if not args.prepare:
            parser.error("Pinned commit options require --prepare")
        if not all(supplied):
            parser.error("--abk-commit and --catalog-commit must be supplied together")
        for option, value in (("--abk-commit", args.abk_commit), ("--catalog-commit", args.catalog_commit)):
            if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
                parser.error(f"{option} must be a full 40-character hexadecimal commit SHA")
    # A failed step must never leave an affirmative publication output.
    write_outputs({"has_updates": "false", "workflow_changed": "false", "body_path": ""})
    try:
        root = args.root.resolve()
        lock = sync.read_lock(root)
        latest = (
            {"abk": args.abk_commit.lower(), "catalog": args.catalog_commit.lower()}
            if all(supplied) else find_latest(lock)
        )
        values = {
            "current_json": json.dumps({source: lock[source]["commit"] for source in ("abk", "catalog")}, sort_keys=True),
            "latest_json": json.dumps(latest, sort_keys=True),
            "heads_marker": heads_marker(latest),
            "branch": BRANCH, "title": PR_TITLE,
        }
        if args.check_only:
            values["has_updates"] = str(source_changed(lock, latest)).lower()
            write_outputs(values)
            print("Upstream update available." if source_changed(lock, latest) else "Upstreams unchanged.")
            return 0
        result = prepare_updates(root, lock, latest)
        if not result["has_updates"]:
            write_outputs({**values, "has_updates": "false", "workflow_changed": "false"})
            print("No relevant upstream update.")
            return 0
        body_path = write_report(report_text(lock, latest, result), args.report_dir)
        write_outputs({
            **values, "has_updates": "true", "workflow_changed": str(result["workflow_changed"]).lower(),
            "body_path": str(body_path), "changed_files_json": json.dumps(result["changed_files"], ensure_ascii=False),
        })
        print(f"Prepared {len(result['changed_files'])} validated changed files for {BRANCH}.")
        return 0
    except (UpdateError, sync.SyncError, OSError, UnicodeError) as exc:
        print(f"Upstream PR preparation stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
