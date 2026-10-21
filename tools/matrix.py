#!/usr/bin/env python3
"""Build the GKI matrix without collapsing distinct security-patch months."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
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

# Certified Android 12 boot-image revisions from the reference's fixed matrix.
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


def _abk_rows(root: Path) -> list[tuple[dict[str, str], str]]:
    """Include new ABK combinations even if the secondary catalog lags behind."""
    source = root / ".upstream" / "abk-build.yml"
    if not source.exists():
        return []
    text = source.read_text(encoding="utf-8-sig")
    match = re.search(r"KNOWN_KERNEL_PAIRS=\$\(cat <<'EOF'\r?\n(.*?)\r?\n\s*EOF\r?\n\s*\)", text, re.S)
    if not match:
        raise ValueError("ABK source no longer contains KNOWN_KERNEL_PAIRS; review the upstream schema before updating")
    rows = []
    for number, line in enumerate(match.group(1).splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split()
        context = f"ABK KNOWN_KERNEL_PAIRS line {number}"
        if len(parts) != 4:
            raise ValueError(f"{context}: expected android, kernel, sublevel and patch month")
        android, family, sublevel, date = parts
        if family not in KERNELS or KERNELS[family] != android:
            raise ValueError(f"{context}: unsupported Android/kernel pair {android}/{family}")
        if sublevel == "X" and date == "lts":
            continue
        full = _full_version(f"{family}.{sublevel}", family, context)
        rows.append((_row(android, family, full, _month(date, context)), full))
    if not rows:
        raise ValueError("ABK KNOWN_KERNEL_PAIRS is empty")
    return rows


def build_matrix(
    root: Path,
    kernel_versions: str = "all",
    targets: str = "",
    include_lts: bool = False,
) -> list[dict[str, str]]:
    """Return exact version/month combinations from both pinned upstreams.

    Catalog refs take precedence over ABK rows with the same exact identity.
    A full-version target selects every available month for that version; add
    ``@YYYY-MM`` to request one exact combination.
    """
    if not isinstance(include_lts, bool):
        raise ValueError("include_lts must be a boolean")
    root = Path(root)
    selected, requested = _selectors(kernel_versions, targets)
    candidates: list[tuple[dict[str, str], str]] = []
    for family, android in KERNELS.items():
        source = root / "data" / android / f"{family}.json"
        try:
            document = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read catalog {source}: {exc}") from exc
        if not isinstance(document, dict) or document.get("android_version") != android or document.get("kernel_version") != family:
            raise ValueError(f"{source}: catalog Android/kernel identity does not match its path")
        entries = document.get("entries")
        if not isinstance(entries, list):
            raise ValueError(f"{source}: entries must be an array")
        for index, entry in enumerate(entries):
            context = f"{source}: entries[{index}]"
            if not isinstance(entry, dict):
                raise ValueError(f"{context}: entry must be an object")
            full = _full_version(entry.get("kernel"), family, context)
            date = _month(entry.get("date"), context)
            ref = _kernel_ref(entry.get("ref", ""), context)
            candidates.append((_row(android, family, full, date, ref), full))
        if include_lts:
            full = _full_version(document.get("lts"), family, f"{source}: lts")
            row = _row(android, family, full, "lts", f"refs/heads/{android}-{family}-lts")
            row["sub_level"] = "X"
            candidates.append((row, full))
    candidates.extend(_abk_rows(root))

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
    return sorted(
        output.values(),
        key=lambda row: (
            int(row["android_version"].removeprefix("android")),
            tuple(map(int, row["kernel_version"].split("."))),
            row["sub_level"] == "X",
            int(row["sub_level"]) if row["sub_level"] != "X" else 0,
            row["os_patch_level"],
        ),
    )


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
                stream.write("- 保留每个内核小版本的所有安全补丁月份。\n\n")
                for kernel in KERNELS:
                    if counts[kernel]:
                        stream.write(f"- {kernel}：{counts[kernel]}\n")
        print(encoded)
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
