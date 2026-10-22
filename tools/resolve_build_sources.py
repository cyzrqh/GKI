#!/usr/bin/env python3
"""Resolve one immutable KernelSU source for every job in a build."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pin_build_dependencies import CompatibilityError, TIERS as PROFILE_TIERS, custom_configuration, load_profile


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
REPOSITORIES = {
    "Official": "tiann/KernelSU",
    "SukiSU": "SukiSU-Ultra/SukiSU-Ultra",
    "BakaSU": "Baka-SU/BakaSU",
}
TIERS = frozenset(("Stable(标准)", "Dev(开发)", "Latest(最新)", "Custom(自定义)"))
ENV_KEYS = frozenset((
    "BRANCH", "RESOLVED_KSU_SHA", "RESOLVED_KSU_REPO", "RESOLVED_KSU_SOURCE_BRANCH",
    "EFFECTIVE_KSU_BRANCH", "KSU_LATEST_SOURCE",
))


class SourceResolutionError(RuntimeError):
    """A source could not be selected safely and reproducibly."""


def validate_ref(value: str, *, empty: bool = False) -> str:
    """Accept conventional public branch/tag names and full commit hashes."""
    if empty and value == "":
        return value
    if (not isinstance(value, str) or len(value) > 255
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
            or ".." in value or "//" in value or value.endswith(("/", "."))
            or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise SourceResolutionError("Invalid KernelSU source ref")
    return value


def validate_custom_ref(value: str) -> None:
    if not isinstance(value, str):
        raise SourceResolutionError("Invalid custom KernelSU ref")
    if ":" in value:
        branch, separator, number = value.rpartition(":")
        if not separator or not re.fullmatch(r"[1-9][0-9]{0,5}", number):
            raise SourceResolutionError("Invalid custom KernelSU workflow selector")
        validate_ref(branch)
    else:
        validate_ref(value, empty=True)


def parse_environment(text: str) -> dict[str, str]:
    """Read only the upstream resolver's simple, allowlisted assignments."""
    values = {}
    lines = text.split("\n")
    for number, line in enumerate(lines):
        if line.endswith("\r") and number < len(lines) - 1:
            line = line[:-1]
        if not line:
            continue
        key, separator, value = line.partition("=")
        if (not separator or key not in ENV_KEYS or key in values
                or any(ord(character) < 32 or ord(character) == 127 for character in value)):
            raise SourceResolutionError("Invalid environment output from the KernelSU resolver")
        values[key] = value
    if not values.get("BRANCH"):
        raise SourceResolutionError("KernelSU resolver did not emit a source ref")
    for key in ("BRANCH", "RESOLVED_KSU_SHA", "RESOLVED_KSU_SOURCE_BRANCH", "KSU_LATEST_SOURCE"):
        if key in values:
            validate_ref(values[key], empty=key in ("RESOLVED_KSU_SOURCE_BRANCH", "KSU_LATEST_SOURCE"))
    if values.get("RESOLVED_KSU_SHA", values["BRANCH"]) != values["BRANCH"]:
        raise SourceResolutionError("KernelSU resolver emitted inconsistent source refs")
    return values


def upstream_selection(root: Path, variant: str, tier: str, custom_ref: str) -> dict[str, str]:
    # A private environment file prevents upstream from mutating later prepare steps.
    environment_path = root / f".gki-ksu-source-{uuid.uuid4().hex}.env"
    with environment_path.open("x", encoding="utf-8"):
        pass
    try:
        environment = {**os.environ, "KSU_VARIANT": variant, "KSU_BRANCH": tier,
                       "CUSTOM_REF": custom_ref, "GITHUB_ENV": str(environment_path)}
        # Do not let an upstream helper write unvalidated job outputs.
        environment.pop("GITHUB_OUTPUT", None)
        try:
            result = subprocess.run(
                ["bash", str(root / ".github/scripts/resolve-ksu-ref.sh")],
                cwd=root, env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SourceResolutionError("Upstream KernelSU source resolution could not complete") from error
        if result.returncode:
            # stdout/stderr may contain credentials or unsafe workflow commands.
            raise SourceResolutionError(f"Upstream KernelSU source resolution failed (exit {result.returncode})")
        try:
            emitted = environment_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise SourceResolutionError("Cannot read the upstream KernelSU source result") from error
        return parse_environment(emitted)
    finally:
        environment_path.unlink(missing_ok=True)


class NoRedirect(HTTPRedirectHandler):
    """Keep Authorization on the single explicitly selected GitHub API URL."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise SourceResolutionError("GitHub source lookup redirects are not permitted")


def github_commit(repository: str, ref: str) -> str:
    """Resolve a public ref to a full SHA without following HTTP redirects."""
    if repository not in REPOSITORIES.values():
        raise SourceResolutionError("KernelSU repository is not allowlisted")
    validate_ref(ref)
    url = f"https://api.github.com/repos/{repository}/commits/{quote(ref, safe='')}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "gki-source-resolver"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        if any(ord(character) < 32 or ord(character) == 127 for character in token):
            raise SourceResolutionError("Invalid GitHub authentication token")
        headers["Authorization"] = f"Bearer {token}"
    try:
        with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=60) as response:
            final_url = response.geturl()
            parsed = urlsplit(final_url)
            if (parsed.scheme != "https" or parsed.netloc != "api.github.com" or final_url != url):
                raise SourceResolutionError("GitHub source lookup returned an unexpected URL")
            if response.status != 200:
                raise SourceResolutionError("GitHub source lookup returned an unsuccessful status")
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise SourceResolutionError("GitHub source lookup response is too large")
            document = json.loads(raw)
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise SourceResolutionError("GitHub source lookup failed") from error
    commit = document.get("sha") if isinstance(document, dict) else None
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise SourceResolutionError("GitHub source lookup did not return a full commit SHA")
    return commit.lower()


def source_result(commit: str, repository: str, source_branch: str, tier: str) -> dict[str, str]:
    return {"ksu_ref": commit, "ksu_repo": repository, "ksu_short": commit[:7],
            "ksu_source_branch": source_branch, "effective_ksu_branch": tier}


def resolve_sources(root: Path, variant: str, tier: str, custom_ref: str = "", susfs: bool = False) -> dict[str, str]:
    if variant not in (*REPOSITORIES, "None"):
        raise SourceResolutionError("Unknown KernelSU variant")
    if tier not in TIERS:
        raise SourceResolutionError("Unknown KernelSU tier")
    validate_custom_ref(custom_ref)
    if variant == "None":
        return source_result("", "", "", tier)
    root = root.resolve()
    repository = REPOSITORIES[variant]
    if variant == "SukiSU" and susfs and tier in PROFILE_TIERS and not custom_configuration(root):
        profile = load_profile(root)
        if tier in profile["tiers"]:
            commit = profile.get("source_ref", profile["ksu_commits"][0])
            if not isinstance(commit, str) or not SHA.fullmatch(commit):
                raise SourceResolutionError("Compatibility source_ref must be a full commit SHA")
            return source_result(commit.lower(), repository, "", tier)
    emitted = upstream_selection(root, variant, tier, custom_ref)
    if emitted.get("RESOLVED_KSU_REPO", repository) != repository:
        raise SourceResolutionError("KernelSU resolver returned an unexpected repository")
    effective_tier = emitted.get("EFFECTIVE_KSU_BRANCH", tier)
    if effective_tier not in TIERS:
        raise SourceResolutionError("KernelSU resolver returned an invalid tier")
    ref = emitted["BRANCH"]
    commit = ref.lower() if SHA.fullmatch(ref) else github_commit(repository, ref)
    if not SHA.fullmatch(commit):
        raise SourceResolutionError("KernelSU source must resolve to a full commit SHA")
    return source_result(commit.lower(), repository, emitted.get("RESOLVED_KSU_SOURCE_BRANCH", ""), effective_tier)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--variant", required=True, choices=(*REPOSITORIES, "None"))
    parser.add_argument("--tier", required=True, choices=sorted(TIERS))
    parser.add_argument("--custom-ref", default="")
    parser.add_argument("--susfs", required=True, choices=("true", "false"))
    args = parser.parse_args(argv)
    try:
        result = resolve_sources(args.root, args.variant, args.tier, args.custom_ref, args.susfs == "true")
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
                for key, value in result.items():
                    stream.write(f"{key}={value}\n")
        print(f"KernelSU source: {result['ksu_repo']}@{result['ksu_ref']}" if result["ksu_ref"] else "KernelSU source: None")
    except (SourceResolutionError, CompatibilityError, OSError, UnicodeError) as error:
        print(f"KernelSU source selection failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
