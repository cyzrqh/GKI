#!/usr/bin/env python3
"""Build the GKI matrix without collapsing distinct security-patch months."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import sys


KERNELS = {
    "5.10": "android12",
    "5.15": "android13",
    "6.1": "android14",
    "6.6": "android15",
    "6.12": "android16",
}
FULL_VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
TARGET = re.compile(r"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(?:@(\d{4}-\d{2}))?\Z")
MONTH = re.compile(r"\d{4}-\d{2}\Z")

# Preserved Android 12 boot-image revisions for historical release months.
# Revision only affects optional boot.img packaging; AK3 uses the compiled Image.
ANDROID12_REVISIONS = {
    "2022-01": "r11",
    "2022-03": "r11",
    "2022-04": "r28",
    "2022-11": "r15",
    "2023-04": "r9",
    "2023-07": "r3",
    "2024-01": "r17",
    "2024-05": "r13",
    "2024-08": "r14",
    "2024-11": "r8",
}


def _month(value: object, context: str) -> str:
    if not isinstance(value, str) or not MONTH.fullmatch(value):
        raise ValueError(f"{context}: expected a security-patch month in YYYY-MM format")
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise ValueError(f"{context}: invalid security-patch month {value!r}") from exc
    return value


def _full_version(value: object, family: str, context: str) -> str:
    if not isinstance(value, str) or not FULL_VERSION.fullmatch(value):
        raise ValueError(f"{context}: invalid full kernel version {value!r}")
    if value.rsplit(".", 1)[0] != family:
        raise ValueError(f"{context}: kernel {value!r} does not belong to {family}")
    return value


def _kernel_ref(value: object, context: str) -> str:
    # An empty ref lets the source resolver query the upstream manifest.
    if value == "":
        return ""
    if isinstance(value, str) and re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value):
        return value.lower()
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"refs/(heads|tags)/[A-Za-z0-9._/-]+", value)
        or ".." in value
        or "//" in value
        or value.endswith(("/", "."))
    ):
        raise ValueError(f"{context}: invalid kernel ref {value!r}")
    return value


def _selectors(kernel_versions: str, targets: str) -> tuple[set[str], list[tuple[str, str | None]]]:
    if not isinstance(kernel_versions, str) or not isinstance(targets, str):
        raise ValueError("kernel_versions and targets must be strings")
    if kernel_versions.strip() == "all":
        selected = set(KERNELS)
    else:
        values = [item.strip() for item in kernel_versions.split(",")]
        if not all(values) or any(item not in KERNELS for item in values):
            raise ValueError("kernel versions must be 'all' or a comma-separated subset of " + ", ".join(KERNELS))
        selected = set(values)
    parsed: list[tuple[str, str | None]] = []
    if targets.strip():
        for raw in targets.split(","):
            value = raw.strip()
            match = TARGET.fullmatch(value)
            if not match:
                raise ValueError(f"invalid target {value!r}; use 6.1.145 or 6.1.145@2025-08")
            full, date = match.groups()
            if full.rsplit(".", 1)[0] not in KERNELS:
                raise ValueError(f"unsupported target kernel {full!r}")
            if date is not None:
                _month(date, f"target {value}")
            pair = (full, date)
            if pair not in parsed:
                parsed.append(pair)
    return selected, parsed


def _row(android: str, family: str, full: str, month: str, ref: str = "") -> dict[str, str]:
    return {
        "android_version": android,
        "kernel_version": family,
        "sub_level": full.rsplit(".", 1)[1],
        "os_patch_level": month,
        "revision": ANDROID12_REVISIONS.get(month, "r1") if family == "5.10" else "r1",
        "kernel_ref": ref,
    }


def load_catalog(root: Path) -> dict:
    """Load the self-contained catalog, independently of workflow templates."""
    source = Path(root) / "data/kernels.json"
    try:
        document = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read kernel catalog {source}") from exc
    return validate_catalog(document, str(source))


def validate_catalog(document: object, source: str = "kernel catalog") -> dict:
    """Validate local and offline-probed rows with the same schema rules."""
    if (not isinstance(document, dict) or document.get("schema_version") != 1
            or isinstance(document.get("schema_version"), bool)):
        raise ValueError(f"{source}: unsupported kernel catalog schema")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{source}: entries must be an array")
    for index, entry in enumerate(entries):
        context = f"{source}: entries[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{context}: entry must be an object")
        family, android = entry.get("kernel_version"), entry.get("android_version")
        if (not isinstance(family, str) or not isinstance(android, str)
                or family not in KERNELS or KERNELS[family] != android):
            raise ValueError(f"{context}: unsupported Android/kernel pair")
        _full_version(entry.get("kernel"), family, context)
        if entry.get("date") != "lts":
            _month(entry.get("date"), context)
        _kernel_ref(entry.get("ref", ""), context)
        if "revision" in entry and (not isinstance(entry["revision"], str)
                                    or not re.fullmatch(r"r[0-9]+", entry["revision"])):
            raise ValueError(f"{context}: invalid boot revision")
    return document


def build_matrix(
    root: Path,
    kernel_versions: str = "all",
    targets: str = "",
    include_lts: bool = False,
) -> list[dict[str, str]]:
    """Return exact version/month combinations from the local kernel catalog.
    A full-version target selects every available month for that version; add
    ``@YYYY-MM`` to request one exact combination.
    """
    if not isinstance(include_lts, bool):
        raise ValueError("include_lts must be a boolean")
    root = Path(root)
    selected, requested = _selectors(kernel_versions, targets)
    candidates: list[tuple[dict[str, str], str]] = []
    for entry in load_catalog(root)["entries"]:
        date = entry["date"]
        if date == "lts" and not include_lts:
            continue
        full, family, android = entry["kernel"], entry["kernel_version"], entry["android_version"]
        row = _row(android, family, full, date, _kernel_ref(entry.get("ref", ""), "kernel catalog"))
        row["revision"] = entry.get("revision", row["revision"])
        if date == "lts":
            row["sub_level"] = "X"
        candidates.append((row, full))

    output: dict[tuple[str, str, str, str], dict[str, str]] = {}
    matched: set[tuple[str, str | None]] = set()
    for row, full in candidates:
        if row["kernel_version"] not in selected:
            continue
        matches = [pair for pair in requested if pair[0] == full and (pair[1] is None or pair[1] == row["os_patch_level"])]
        if requested and not matches:
            continue
        matched.update(matches)
        key = tuple(row[name] for name in ("android_version", "kernel_version", "sub_level", "os_patch_level"))
        output.setdefault(key, row)
    missing = [full + (f"@{date}" if date else "") for full, date in requested if (full, date) not in matched]
    if missing:
        raise ValueError("targets not found in the selected kernels/catalogs: " + ", ".join(missing))
    if not output:
        raise ValueError("the selected kernels and targets produce an empty build matrix")
    if len(output) > 256:
        raise ValueError(f"matrix has {len(output)} jobs; GitHub Actions allows at most 256 per matrix; narrow kernel versions or targets")
    # Offer the newest target from every selected family before its older
    # history. This only changes scheduling order: all exact version/month
    # identities above remain present, including explicitly requested LTS.
    families = [sorted(
        (row for row in output.values() if row["kernel_version"] == family),
        key=lambda row: (
            row["sub_level"] != "X",
            int(row["sub_level"]) if row["sub_level"] != "X" else 0,
            row["os_patch_level"],
        ),
        reverse=True,
    ) for family in KERNELS]
    return [row for turn in zip_longest(*families) for row in turn if row is not None]


def _boolean(value: str) -> bool:
    if value.lower() in {"true", "1", "yes", "on"}:
        return True
    if value.lower() in {"false", "0", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _append_path(value: str | None, env_name: str) -> Path | None:
    if value is None:
        return None
    actual = value or os.environ.get(env_name, "")
    if not actual:
        raise ValueError(f"{env_name} is unset; provide an explicit output path")
    return Path(actual)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--kernel-versions", default="all")
    parser.add_argument("--targets", default="")
    parser.add_argument("--include-lts", nargs="?", const=True, default=False, type=_boolean)
    parser.add_argument("--github-output", nargs="?", const="")
    parser.add_argument("--summary", nargs="?", const="")
    args = parser.parse_args(argv)
    try:
        matrix = build_matrix(args.root, args.kernel_versions, args.targets, args.include_lts)
        encoded = json.dumps({"include": matrix}, ensure_ascii=False, separators=(",", ":"))
        output = _append_path(args.github_output, "GITHUB_OUTPUT")
        summary = _append_path(args.summary, "GITHUB_STEP_SUMMARY")
        if output is not None:
            with output.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f"matrix={encoded}\ncount={len(matrix)}\n")
        if summary is not None:
            counts = Counter(row["kernel_version"] for row in matrix)
            with summary.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("## GKI 全属性内核构建矩阵\n\n")
                stream.write(f"- 构建任务：{len(matrix)}\n")
                stream.write(f"- 包含 LTS：{'是' if args.include_lts else '否'}\n")
                if args.targets.strip():
                    stream.write(f"- 选择范围：指定版本（{args.targets}）。\n")
                else:
                    stream.write("- 选择范围：所选系列的全部历史版本与补丁月份。\n")
                stream.write("- 调度顺序：按所选内核系列轮转，每系列优先较新版本和补丁月份；目标数量不变。\n\n")
                for kernel in KERNELS:
                    if counts[kernel]:
                        stream.write(f"- {kernel}：{counts[kernel]}\n")
        print(encoded)
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
