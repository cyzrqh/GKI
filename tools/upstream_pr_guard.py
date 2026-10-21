#!/usr/bin/env python3
"""Refuse to overwrite an upstream-sync branch not owned by this automation."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_ROOT = "https://api.github.com"
BRANCH = "automation/upstream-sync"
OWNERSHIP_MARKER = "<!-- gki-upstream-sync:v1 -->"
HEADS_MARKER_PATTERN = re.compile(
    r"<!-- gki-upstream-heads:abk=[0-9a-f]{40};catalog=[0-9a-f]{40} -->\Z"
)


class GuardError(RuntimeError):
    """A GitHub read failed or the automation cannot safely own the branch."""


class ApiNotFound(GuardError):
    pass


class NoRedirect(HTTPRedirectHandler):
    # urllib otherwise copies Authorization when following some redirects.
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise GuardError("GitHub API redirects are not accepted; verify the repository name")


def validate_repository(repository: str) -> tuple[str, str]:
    parts = repository.split("/")
    if len(parts) != 2:
        raise GuardError("Repository must have the form owner/name")
    owner, name = parts
    if (not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
            or name in (".", "..")):
        raise GuardError("Invalid GitHub repository name")
    return owner, name


def validate_base(base: str) -> str:
    if (not re.fullmatch(r"[A-Za-z0-9_./-]+", base) or ".." in base or "//" in base
            or base.endswith(("/", "."))
            or any(part.startswith(".") or part.endswith(".lock") for part in base.split("/"))):
        raise GuardError("Invalid base branch name")
    return base


class GitHubReader:
    def __init__(self, token: str, opener=None):
        if not token or "\n" in token or "\r" in token:
            raise GuardError("Set GH_TOKEN or GITHUB_TOKEN for the upstream PR guard")
        self._token = token
        self._opener = opener or build_opener(NoRedirect())

    def get(self, path: str):
        # Callers provide paths only. Never send a token to a response-provided URL.
        if (not path.startswith("/repos/") or path.startswith("//")
                or "\r" in path or "\n" in path or "\\" in path):
            raise GuardError("Invalid GitHub API request path")
        url = API_ROOT + path
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise GuardError("GitHub API host must be api.github.com")
        parts = parsed.path.split("/")
        if len(parts) < 5 or parsed.fragment or "%" in parsed.path:
            raise GuardError("Invalid GitHub API resource path")
        validate_repository("/".join(parts[2:4]))
        suffix = "/".join(parts[4:])
        if suffix not in ("pulls", f"git/ref/heads/{BRANCH}"):
            raise GuardError("GitHub API resource is outside the upstream PR guard scope")
        request = Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gki-upstream-pr-guard",
        })
        try:
            with self._opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404:
                raise ApiNotFound("GitHub API resource was not found") from None
            raise GuardError(f"GitHub API request failed (HTTP {error.code}); stopping") from None
        except (URLError, OSError, ValueError):
            raise GuardError("GitHub API read failed; stopping without changing the branch") from None


@dataclass(frozen=True)
class GuardResult:
    safe_to_update: bool
    already_pending: bool
    branch_exists: bool
    pull_request_url: str = ""


def _owned_pr(pr: dict, repository: str, base: str) -> bool:
    head = pr.get("head") or {}
    base_data = pr.get("base") or {}
    head_repo = head.get("repo") or {}
    base_repo = base_data.get("repo") or {}
    return (
        head.get("ref") == BRANCH
        and str(head_repo.get("full_name", "")).lower() == repository.lower()
        and base_data.get("ref") == base
        and str(base_repo.get("full_name", "")).lower() == repository.lower()
        and OWNERSHIP_MARKER in (pr.get("body") or "")
    )


def _public_pr_url(pr: dict, repository: str) -> str:
    number = pr.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise GuardError("GitHub returned a pull request without a valid number")
    # Construct the public URL; never trust or print an arbitrary html_url value.
    return f"https://github.com/{repository}/pull/{number}"


def check_branch(
    repository: str,
    heads_marker: str,
    base: str = "main",
    fetch: Callable[[str], object] | None = None,
) -> GuardResult:
    owner, _ = validate_repository(repository)
    validate_base(base)
    if not HEADS_MARKER_PATTERN.fullmatch(heads_marker):
        raise GuardError("Invalid upstream heads marker; expected full ABK and catalog commit SHAs")
    if fetch is None:
        fetch = GitHubReader(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")).get
    prefix = "/repos/" + quote(repository, safe="/")
    try:
        branch_data = fetch(prefix + "/git/ref/heads/" + quote(BRANCH, safe="/"))
    except ApiNotFound:
        branch_exists = False
    else:
        if not isinstance(branch_data, dict) or branch_data.get("ref") != f"refs/heads/{BRANCH}":
            raise GuardError("GitHub returned an unexpected sync branch ref; stopping")
        branch_exists = True

    pull_requests = []
    for page in range(1, 101):
        # Inspect every base: this head branch must never belong to another PR.
        query = urlencode({"state": "all", "head": f"{owner}:{BRANCH}",
                           "per_page": 100, "page": page})
        batch = fetch(prefix + "/pulls?" + query)
        if not isinstance(batch, list) or any(not isinstance(pr, dict) for pr in batch):
            raise GuardError("GitHub returned an unexpected pull request response")
        pull_requests.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise GuardError("Too many sync pull requests to inspect safely; stopping")

    if any((pr.get("base") or {}).get("ref") != base for pr in pull_requests):
        raise GuardError("Sync branch has a pull request targeting a different base; refusing to overwrite it")
    open_prs = [pr for pr in pull_requests if pr.get("state") == "open"]
    # With no branch and no open PR there is no existing state to overwrite.
    if not branch_exists and not open_prs:
        return GuardResult(True, False, False)
    relevant = pull_requests if branch_exists else open_prs
    if not relevant:
        raise GuardError("Sync branch already exists without an associated automation pull request")
    if any(not _owned_pr(pr, repository, base) for pr in relevant):
        raise GuardError("Sync branch has an unmarked or foreign pull request; refusing to overwrite it")

    pending = [pr for pr in open_prs if heads_marker in (pr.get("body") or "")]
    selected = pending[0] if pending else (open_prs[0] if open_prs else relevant[0])
    return GuardResult(True, bool(pending), branch_exists, _public_pr_url(selected, repository))


def write_outputs(result: GuardResult, output: Path | None) -> None:
    values = asdict(result)
    if output:
        with output.open("a", encoding="utf-8", newline="\n") as handle:
            for key, value in values.items():
                serialized = str(value).lower() if isinstance(value, bool) else value
                handle.write(f"{key}={serialized}\n")
    print(json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--base", default="main")
    parser.add_argument("--heads-marker", required=True)
    parser.add_argument("--github-output", type=Path,
                        default=Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None)
    args = parser.parse_args(argv)
    try:
        result = check_branch(args.repository, args.heads_marker, args.base)
        write_outputs(result, args.github_output)
    except (GuardError, OSError) as error:
        # Do not dump request objects, tokens, or remote response bodies.
        print(f"Upstream PR guard failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
