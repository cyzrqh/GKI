#!/usr/bin/env python3
"""Render a Chinese release description from public APK and kernel metadata."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import re
import sys

from kernel_order import kernel_key


UNKNOWN = "未知"
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._()+@ -]{0,249}\Z")
FEATURES = (
    ("SUSFS", ("susfs", "enable_susfs")),
    ("Hook 方式", ("hook", "hook_method", "hook_mode")),
    ("挂载方式", ("mount", "mount_method", "mount_mode")),
    ("LTO", ("lto", "lto_mode")),
    ("BBG 安全补丁", ("bbg", "use_bbg", "enable_bbg")),
    ("ZRAM", ("zram", "use_zram")),
    ("LZ4KD", ("lz4kd", "use_lz4kd", "enable_lz4kd")),
    ("KPM", ("kpm", "use_kpm")),
    ("Re-Kernel", ("re_kernel", "use_re_kernel", "rekernel")),
    ("DDK", ("ddk", "use_ddk", "enable_ddk")),
    ("NTSync", ("ntsync", "enable_ntsync", "use_ntsync")),
    ("网络增强", ("network", "network_enhancement", "network_enhance")),
    ("虚拟化", ("virtualization", "virtualization_support")),
    ("自定义模块", ("custom_modules", "custom_external_modules")),
    ("附加内核配置", ("kernel_config", "extra_kernel_config", "custom_kernel_config")),
)
PRIVATE_FEATURE = re.compile(r"(?:password|secret|token|keystore|private_key)", re.I)
# Keep full configuration evidence in the packaged metadata, while the public
# description omits details the repository owner does not want listed.
HIDDEN_FEATURES = frozenset(("TCP 拥塞算法",))


class ReleaseNotesError(ValueError):
    """Metadata cannot be represented as a safe public release description."""


def text(value) -> str:
    """Accept only simple JSON values, with explicit unknowns."""
    if value is None or value == "":
        return UNKNOWN
    if isinstance(value, bool):
        return "启用" if value else "关闭"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ReleaseNotesError("Metadata numbers must be finite")
        return str(value)
    if not isinstance(value, str):
        raise ReleaseNotesError("Metadata fields must use simple JSON values")
    normalized = value.strip().lower()
    if normalized in ("true", "on", "enabled"):
        return "启用"
    if normalized in ("false", "off", "disabled"):
        return "关闭"
    if normalized in ("", "unknown", "null"):
        return UNKNOWN
    return value


def escape(value) -> str:
    value = text(value)
    # Never let metadata introduce new Markdown rows, blocks or raw HTML.
    value = re.sub(r"[\r\n\x00-\x1f\x7f-\x9f\u2028\u2029]+", " ⏎ ", value)
    value = html.escape(value, quote=True)
    return re.sub(r"([\\`*_\[\]{}()#!|~])", r"\\\1", value)


def first(document: dict, *keys):
    for key in keys:
        if key in document and document[key] is not None and document[key] != "":
            return document[key]
    return None


def asset_names(document: dict, *, manager: bool = False) -> list[str]:
    names = document.get("files")
    if first(document, "file") is not None and first(document, "filename") is not None and document["file"] != document["filename"]:
        raise ReleaseNotesError("Metadata file and filename disagree")
    single = first(document, "file", "filename")
    if names is None:
        names = [] if single is None else [single]
    elif not isinstance(names, list):
        raise ReleaseNotesError("Metadata files must be an array of filenames")
    elif single is not None and single not in names:
        raise ReleaseNotesError("Metadata file and files disagree")
    allowed = {".apk"} if manager else {".zip", ".img"}
    for name in names:
        if (not isinstance(name, str) or not FILENAME.fullmatch(name) or ".." in name
                or name.endswith((" ", ".")) or Path(name).suffix.lower() not in allowed):
            raise ReleaseNotesError("Metadata contains an invalid release asset filename")
    if len(names) != len(set(names)):
        raise ReleaseNotesError("Metadata contains duplicate release asset filenames")
    return names


def feature_values(document: dict) -> dict:
    features = document.get("features", {})
    if not isinstance(features, dict):
        raise ReleaseNotesError("Metadata features must be an object of simple values")
    for key, value in features.items():
        if not isinstance(key, str):
            raise ReleaseNotesError("Metadata feature names must be strings")
        text(value)
    return features


def feature(document: dict, values: dict, aliases: tuple[str, ...]):
    selected = first(values, *aliases)
    return selected if selected is not None else first(document, *aliases)


def kernel_target(document: dict) -> str:
    explicit = first(document, "target", "kernel_target")
    if explicit is not None:
        return text(explicit)
    android = text(first(document, "android_version", "android"))
    kernel = text(first(document, "kernel_version", "kernel"))
    sublevel = first(document, "sub_level", "sublevel")
    patch = text(first(document, "os_patch_level", "patch_level"))
    if sublevel is not None and kernel != UNKNOWN:
        kernel += "." + text(sublevel)
    return f"{android} / {kernel} / {patch}"


def version(document: dict) -> str:
    name = text(first(document, "ksu_version_name", "manager_version_name", "version_name"))
    code = text(first(document, "ksu_version_code", "manager_version_code", "version_code"))
    return f"{name}（版本号 {code}）"


def kernel_series(document: dict) -> str:
    android = text(first(document, "android_version", "android"))
    kernel = text(first(document, "kernel_version", "kernel"))
    if android == UNKNOWN or kernel == UNKNOWN:
        return kernel_target(document)
    match = re.fullmatch(r"android\s*(\d+)", android, re.I)
    if match:
        android = "Android " + match.group(1)
    match = re.fullmatch(r"(\d+\.\d+)(?:\.\d+)?", kernel)
    if match:
        kernel = match.group(1)
    return f"{android} / {kernel}"


def grouped_value(kernels: list[dict], values: list) -> str:
    """Show a shared value once, refining conflicting series to exact targets."""
    normalized = [text(value) for value in values]
    if not normalized:
        return UNKNOWN
    if len(set(normalized)) == 1:
        return escape(normalized[0])
    series = [kernel_series(document) for document in kernels]
    targets = [kernel_target(document) for document in kernels]
    series_values = {}
    groups = {}
    for label, value in zip(series, normalized):
        series_values.setdefault(label, set()).add(value)
    for index, (label, target, value) in enumerate(zip(series, targets, normalized), 1):
        # A series label is accurate only when every target in that series has
        # the same value for this row. Conflicting builds keep their full target.
        if len(series_values[label]) > 1:
            label = target
            if targets.count(target) > 1:
                label += f"（目标 {index}）"
        labels = groups.setdefault(value, [])
        if label not in labels:
            labels.append(label)
    return "；".join(f"{escape(value)}（{'、'.join(escape(label) for label in labels)}）"
                    for value, labels in groups.items())


def render_notes(manager: dict | None, kernels: list[dict], *, generated_at: datetime | None = None,
                 report_kind: str = 'release') -> str:
    if report_kind not in ('release', 'actions'):
        raise ReleaseNotesError("Report kind must be 'release' or 'actions'")
    if (manager is not None and not isinstance(manager, dict)) or any(not isinstance(item, dict) for item in kernels):
        raise ReleaseNotesError("Each metadata file must contain one JSON object")
    kernels = sorted(kernels, key=kernel_key)
    manager = {} if manager is None else manager
    manager_assets = asset_names(manager, manager=True)
    for document in kernels:
        asset_names(document)
    features = [feature_values(document) for document in kernels]
    instant = generated_at or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ReleaseNotesError("Release time must include a timezone")
    timestamp = instant.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    scope = "、".join(dict.fromkeys(kernel_series(document) for document in kernels))
    if not scope:
        scope = "无内核（仅管理器 APK）" if manager_assets else "未记录"
    if report_kind == 'actions':
        heading = ["# GKI 构建结果", "", "| 构建信息 | 实际值 |", "| --- | --- |",
                   "| 输出位置 | Actions 构建产物 |", f"| 记录时间 | {timestamp} |"]
    else:
        heading = ["# GKI Kernel Release", "", "| 发布信息 | 实际值 |", "| --- | --- |",
                   "| 发布类型 | Release |", f"| 发布时间 | {timestamp} |"]
    lines = [*heading,
             f"| 构建范围 | {escape(scope)} |",
             f"| 实际内核目标数 | {len(kernels)} |",
             f"| 管理器包名 | {escape(first(manager, 'package', 'manager_package'))} |",
             f"| 管理器 APK | {escape('、'.join(manager_assets) or None)} |",
             f"| 管理器版本 | {escape(version(manager))} |",
             f"| 管理器源码提交 | {escape(first(manager, 'ksu_ref', 'source_ref'))} |",
             f"| 管理器构建时间（UTC） | {escape(first(manager, 'built_at_utc'))} |", ""]
    if kernels:
        lines.extend(("## 构建配置", "", "| 项目 | 内容 |", "| --- | --- |"))
        for label, aliases in (("KernelSU 变体", ("ksu_variant", "variant")),
                               ("KernelSU 分支", ("ksu_branch", "tier", "branch")),
                               ("SUSFS 版本", ("susfs_version",)),
                               ("源码提交", ("ksu_ref", "source_ref")),
                               ("配置来源", ("config_source",))):
            values = [first(document, *aliases) for document in kernels]
            lines.append(f"| {label} | {grouped_value(kernels, values)} |")
        lines.append(f"| KernelSU 版本 | {grouped_value(kernels, [version(document) for document in kernels])} |")
        for number, (label, aliases) in enumerate(FEATURES):
            values = []
            for document, actual in zip(kernels, features):
                selected = feature(document, actual, aliases)
                if label == "SUSFS" and text(selected) == "启用":
                    selected = "启用（" + text(first(document, "susfs_version")) + "）"
                values.append(selected)
            # Core rows keep explicit unknowns; omit optional rows only when no
            # target recorded a value. In particular, known 'off' is retained.
            if number < 8 or any(text(value) != UNKNOWN for value in values):
                lines.append(f"| {label} | {grouped_value(kernels, values)} |")
        represented = {key for _, aliases in FEATURES for key in aliases}
        extra = sorted({key for actual in features for key in actual
                        if key not in represented and key not in HIDDEN_FEATURES
                        and not PRIVATE_FEATURE.search(key)})
        for key in extra:
            values = [actual.get(key) for actual in features]
            if any(text(value) != UNKNOWN for value in values):
                lines.append(f"| {escape(key)} | {grouped_value(kernels, values)} |")
    else:
        only_manager = ("本次仅构建管理器安装包，没有内核构建。" if report_kind == 'actions'
                        else "本次仅发布管理器安装包，没有内核构建。")
        lines.append(only_manager if manager_assets else "未记录内核构建目标或管理器安装包。")
        if manager:
            lines.extend(("", f"管理器：{escape(first(manager, 'ksu_variant', 'variant'))}，"
                          f"分支 {escape(first(manager, 'ksu_branch', 'tier', 'branch'))}。"
                          + ("内核功能配置不适用于本次 APK 构建。" if report_kind == 'actions'
                             else "内核功能配置不适用于本次 APK 发布。")))
    lines.append("")
    return "\n".join(lines)


def load_metadata(path: Path) -> dict:
    def reject_constant(_):
        raise ValueError("Non-finite JSON number")
    try:
        document = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, UnicodeError, ValueError) as error:
        raise ReleaseNotesError("Cannot read valid release metadata JSON") from error
    if not isinstance(document, dict):
        raise ReleaseNotesError("Each metadata file must contain one JSON object")
    return document


def generate_notes(manager: Path | None, kernels: list[Path], output: Path, *, report_kind: str = 'release') -> None:
    body = render_notes(load_metadata(manager) if manager else None, [load_metadata(path) for path in kernels],
                        report_kind=report_kind)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8", newline="\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager", type=Path)
    parser.add_argument("--kernels", nargs="*", default=[], type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        generate_notes(args.manager, args.kernels, args.output)
    except (ReleaseNotesError, OSError, UnicodeError) as error:
        print(f"Release notes failed: {error}", file=sys.stderr)
        return 1
    print("Generated release notes from the selected artifact metadata.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
