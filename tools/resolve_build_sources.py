#!/usr/bin/env python3
"""Resolve one immutable KernelSU source for every job in a build."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
REPOSITORIES = {
    "Official": "tiann/KernelSU",
    "SukiSU": "SukiSU-Ultra/SukiSU-Ultra",
    "BakaSU": "Baka-SU/BakaSU",
}
TIERS = frozenset(("Stable(标准)", "Dev(开发)", "Latest(最新)", "Custom(自定义)"))


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


class NoRedirect(HTTPRedirectHandler):
    """Keep Authorization on the single explicitly selected GitHub API URL."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise SourceResolutionError("GitHub source lookup redirects are not permitted")


def github_json(repository: str, resource: str = "", *, missing: bool = False) -> dict | None:
    """Read an allowlisted official repository API without following redirects."""
    if repository not in REPOSITORIES.values():
        raise SourceResolutionError("KernelSU repository is not allowlisted")
    if resource and (not resource.startswith('/') or any(char in resource for char in ('\r', '\n', '#'))):
        raise SourceResolutionError("Invalid official GitHub API resource")
    url = f"https://api.github.com/repos/{repository}{resource}"
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
    except HTTPError as error:
        if missing and error.code == 404:
            return None
        raise SourceResolutionError("Official GitHub source lookup failed") from None
    except (URLError, OSError, ValueError) as error:
        raise SourceResolutionError("GitHub source lookup failed") from error
    if not isinstance(document, dict):
        raise SourceResolutionError("Official GitHub source lookup did not return an object")
    return document


def github_commit(repository: str, ref: str) -> str:
    """Resolve a public ref to a full SHA without following HTTP redirects."""
    validate_ref(ref)
    document = github_json(repository, f"/commits/{quote(ref, safe='')}")
    commit = document.get("sha") if isinstance(document, dict) else None
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise SourceResolutionError("GitHub source lookup did not return a full commit SHA")
    return commit.lower()


def default_branch(repository: str) -> str:
    document = github_json(repository)
    branch = document.get('default_branch')
    if not isinstance(branch, str):
        raise SourceResolutionError('Official repository did not return a default branch')
    return validate_ref(branch)


def official_selection(repository: str, tier: str, custom_ref: str) -> tuple[str, str]:
    if tier == 'Custom(自定义)':
        if not custom_ref:
            raise SourceResolutionError('Custom tier requires an explicit commit, tag, branch, or branch:N selector')
        if ':' not in custom_ref:
            commit = github_commit(repository, custom_ref)
            return commit, '' if SHA.fullmatch(custom_ref) else custom_ref
        branch, _, number = custom_ref.rpartition(':')
        index = int(number) - 1
        query = urlencode({'branch': branch, 'status': 'success', 'per_page': 100, 'page': index // 100 + 1})
        document = github_json(repository, '/actions/workflows/build-manager.yml/runs?' + query, missing=True)
        runs = document.get('workflow_runs') if document else None
        selected = runs[index % 100] if isinstance(runs, list) and len(runs) > index % 100 else None
        if (not isinstance(selected, dict) or selected.get('conclusion') != 'success'
                or selected.get('head_branch') != branch
                or not isinstance(selected.get('head_sha'), str) or not SHA.fullmatch(selected['head_sha'])):
            raise SourceResolutionError('Custom branch:N requires the Nth successful official build-manager.yml run; none was found')
        head_repository = selected.get('head_repository')
        if head_repository is not None:
            name = head_repository.get('full_name') if isinstance(head_repository, dict) else None
            if not isinstance(name, str) or name.casefold() != repository.casefold():
                raise SourceResolutionError('Custom workflow run did not originate in the selected official repository')
        return selected['head_sha'].lower(), branch
    if tier == 'Stable(标准)':
        release = github_json(repository, '/releases/latest', missing=True)
        tag = release.get('tag_name') if release else None
        if not isinstance(tag, str):
            raise SourceResolutionError('Official Stable has no published release; select Dev, Latest, or an explicit Custom ref')
        validate_ref(tag)
        return github_commit(repository, tag), tag
    branch = default_branch(repository)
    if tier == 'Dev(开发)':
        development = github_json(repository, '/branches/dev', missing=True)
        if development is not None:
            commit = development.get('commit', {}).get('sha') if isinstance(development.get('commit'), dict) else None
            if not isinstance(commit, str) or not SHA.fullmatch(commit):
                raise SourceResolutionError('Official dev branch did not return a full commit SHA')
            return commit.lower(), 'dev'
    return github_commit(repository, branch), branch


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
    repository = REPOSITORIES[variant]
    commit, branch = official_selection(repository, tier, custom_ref)
    if not SHA.fullmatch(commit):
        raise SourceResolutionError("KernelSU source must resolve to a full commit SHA")
    return source_result(commit.lower(), repository, branch, tier)


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
    except (SourceResolutionError, OSError, UnicodeError) as error:
        print(f"KernelSU source selection failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
