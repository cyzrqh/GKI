#!/usr/bin/env python3
"""Wait for this workflow run's signed SukiSU APK public identity."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Callable
import zipfile
from urllib.parse import quote, unquote

from embedded_build_metadata import MAX_APK_BYTES, read_manager_apk, validate_members
from manager_variants import MANAGERS, manager_profile


MAX_ARCHIVE_BYTES = MAX_APK_BYTES
MAX_METADATA_BYTES = 16 * 1024
MAX_API_BYTES = 4 * 1024 * 1024
JOBS_PAGE_SIZE = 20
ARTIFACTS_PAGE_SIZE = 100
READ_ATTEMPTS = 3
TRANSIENT_HTTP = frozenset((408, 429, 500, 502, 503, 504))
NETWORK_FAILURES = (
    b'tls handshake timeout', b'connection reset', b'connection refused',
    b'connection timed out', b'i/o timeout', b'temporary failure in name resolution',
    b'no such host', b'network is unreachable', b'failed to connect',
    b'unexpected eof', b'client connection lost',
)
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
APK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.apk\Z")
DEFAULT_MANAGER_JOB = "编译配套 SukiSU 管理器 APK"


class IdentityError(RuntimeError):
    """Fail before compiling a kernel with an unverified manager identity."""


class StaleIdentity(IdentityError):
    """An earlier attempt's artifact is waiting to be replaced by the manager."""


def positive_integer(value, label: str, maximum: int = 2**63 - 1) -> int:
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not re.fullmatch(r"[0-9]+", str(value))
            or not 1 <= int(value) <= maximum):
        raise IdentityError(f"Invalid {label}")
    return int(value)


def validate_repository(value: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value)
            or value.split("/")[1] in (".", "..")):
        raise IdentityError("Invalid workflow repository")
    return value


@dataclass(frozen=True)
class Expected:
    repository: str
    run_id: int
    run_attempt: int
    head_sha: str
    ksu_ref: str
    artifact_name: str = ''
    manager_job: str = DEFAULT_MANAGER_JOB
    variant: str = 'SukiSU'
    manager_job_id: int | None = None

    def validate(self):
        try:
            manager_profile(self.variant)
        except ValueError as error:
            raise IdentityError(str(error)) from None
        validate_repository(self.repository)
        positive_integer(self.run_id, "workflow run ID")
        positive_integer(self.run_attempt, "workflow run attempt")
        if not SHA40.fullmatch(self.head_sha) or not SHA40.fullmatch(self.ksu_ref):
            raise IdentityError("Workflow and KernelSU references must be full commit hashes")
        if (self.artifact_name and not APK_NAME.fullmatch(self.artifact_name)
                or not self.manager_job or any(ord(c) < 32 for c in self.manager_job)):
            raise IdentityError("Invalid manager identity artifact or job name")
        if self.manager_job_id is not None:
            positive_integer(self.manager_job_id, "manager producer job ID")


@dataclass(frozen=True)
class ProducerContext:
    repository: str
    run_id: int
    run_attempt: int
    head_sha: str
    manager_job: str
    variant: str

    def validate(self):
        validate_repository(self.repository)
        positive_integer(self.run_id, 'workflow run ID')
        positive_integer(self.run_attempt, 'workflow run attempt')
        manager_profile(self.variant)
        if (not SHA40.fullmatch(self.head_sha) or not self.manager_job
                or any(ord(c) < 32 for c in self.manager_job)):
            raise IdentityError('Invalid manager producer workflow context')


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IdentityError("Duplicate manager metadata key")
        result[key] = value
    return result


def _check_strings(value):
    if isinstance(value, str) and any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise IdentityError("Manager metadata contains a control character")
    if isinstance(value, dict):
        for key, child in value.items():
            _check_strings(key)
            _check_strings(child)
    elif isinstance(value, list):
        for child in value:
            _check_strings(child)


def validate_metadata(metadata: dict, expected: Expected, *,
                      reusable_attempt: int | None = None,
                      producer_attempt: int | None = None) -> dict[str, str]:
    """Return only verified public values that are safe for GITHUB_ENV."""
    if not isinstance(metadata, dict):
        raise IdentityError("Manager metadata must be an object")
    _check_strings(metadata)
    required = ("package", "file", "ksu_variant", "ksu_ref", "ksu_version_code",
                "ksu_version_name", "manager_cert_size", "manager_cert_sha256", "apk_sha256",
                "producer_repository", "producer_run_id", "producer_run_attempt", "producer_head_sha")
    if any(key not in metadata for key in required):
        raise IdentityError("Signed manager metadata is incomplete")
    if (not isinstance(metadata["producer_repository"], str)
            or metadata["producer_repository"].lower() != expected.repository.lower()
            or positive_integer(metadata["producer_run_id"], "producer run ID") != expected.run_id
            or metadata["producer_head_sha"] != expected.head_sha):
        raise IdentityError("Manager identity belongs to a different repository, run or workflow commit")
    if metadata["ksu_variant"] != expected.variant or metadata["ksu_ref"] != expected.ksu_ref:
        raise IdentityError("Manager and kernel KernelSU source commits differ")
    attempt = positive_integer(metadata["producer_run_attempt"], "producer run attempt")
    if attempt < expected.run_attempt and attempt != reusable_attempt:
        raise StaleIdentity("Waiting for this attempt's signed manager identity")
    if attempt > expected.run_attempt:
        raise IdentityError("Manager identity belongs to a different workflow attempt")
    if producer_attempt is not None and attempt != producer_attempt:
        if attempt < producer_attempt:
            raise StaleIdentity("Waiting for the selected producer's signed manager identity")
        raise IdentityError("Manager identity differs from the selected producer's exact attempt")
    package, filename = metadata["package"], metadata["file"]
    if not isinstance(package, str) or len(package) > 255 or not PACKAGE.fullmatch(package):
        raise IdentityError("Invalid signed manager Android package ID")
    if package != manager_profile(expected.variant)['package']:
        raise IdentityError("Signed manager package differs from the selected upstream application ID")
    if not isinstance(filename, str) or len(filename) > 240 or not APK_NAME.fullmatch(filename):
        raise IdentityError("Invalid signed manager APK filename")
    size = positive_integer(metadata["manager_cert_size"], "manager certificate size", 4096)
    if size < 64:
        raise IdentityError("Invalid manager certificate size")
    for key in ("manager_cert_sha256", "apk_sha256"):
        if not isinstance(metadata[key], str) or not SHA256.fullmatch(metadata[key]):
            raise IdentityError("Invalid public manager SHA256 metadata")
    code = positive_integer(metadata["ksu_version_code"], "manager version code", 2_100_000_000)
    version = metadata["ksu_version_name"]
    if not isinstance(version, str) or not version.strip() or len(version) > 128:
        raise IdentityError("Invalid manager version name")
    return {"MANAGER_PACKAGE": package, "MANAGER_CERT_SIZE": str(size),
            "MANAGER_CERT_SHA256": metadata["manager_cert_sha256"],
            "MANAGER_VERSION_CODE": str(code), "MANAGER_VERSION_NAME": version,
            "MANAGER_APK_FILENAME": filename, "MANAGER_APK_SHA256": metadata["apk_sha256"],
            "GKI_MANAGER_PACKAGE": package}


def read_identity_archive(archive: bytes, expected: Expected, *,
                          reusable_attempt: int | None = None, filename: str | None = None,
                          producer_attempt: int | None = None) -> dict[str, str]:
    if not archive or len(archive) > MAX_ARCHIVE_BYTES:
        raise IdentityError("Manager APK exceeds the size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            members = validate_members(bundle)
            if 'AndroidManifest.xml' in members:
                apk = archive
            else:
                # Some GitHub API versions still transport a raw artifact as
                # one ZIP member. This is not another published artifact.
                entries = list(members.values())
                if (len(entries) != 1 or entries[0].is_dir()
                        or not APK_NAME.fullmatch(entries[0].filename)
                        or not 0 < entries[0].file_size <= MAX_APK_BYTES):
                    raise IdentityError('Manager artifact must contain only its final APK')
                if filename is not None and entries[0].filename != filename:
                    raise IdentityError('Manager artifact filename does not match its APK')
                filename = entries[0].filename
                apk = bundle.read(entries[0])
        metadata = read_manager_apk(apk, filename=filename or expected.artifact_name or None)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, UnicodeError,
            ValueError, OSError, NotImplementedError) as error:
        if isinstance(error, IdentityError):
            raise
        raise IdentityError('Could not verify the final signed manager APK: ' + str(error)) from None
    return validate_metadata(metadata, expected, reusable_attempt=reusable_attempt,
                             producer_attempt=producer_attempt)


class GitHubReader:
    """Use runner-provided gh credentials only for this repository's read APIs."""
    def __init__(self, repository: str):
        self.repository = validate_repository(repository)

    @staticmethod
    def _stage(suffix: str) -> str:
        if suffix.startswith('actions/artifacts/'):
            return 'signed APK download'
        if '/artifacts' in suffix:
            return 'APK artifact listing'
        if '/jobs' in suffix:
            return 'manager producer lookup'
        return 'workflow provenance lookup'

    @staticmethod
    def _failure(stderr: bytes) -> tuple[str, bool]:
        # Inspect diagnostics locally, but return only a fixed category or a
        # numeric HTTP status. CLI output can contain tokens and signed URLs.
        status = re.search(rb'\bHTTP\s+([1-5][0-9]{2})\b', stderr, re.I)
        if status:
            code = int(status[1])
            if code == 403 and any(marker in stderr.lower() for marker in (
                    b'rate limit', b'rate-limit', b'secondary limit')):
                return 'HTTP 403 (rate limited)', False
            return f'HTTP {code}', code in TRANSIENT_HTTP
        lower = stderr.lower()
        if any(marker in lower for marker in NETWORK_FAILURES):
            return 'temporary network failure', True
        return 'GitHub CLI request failed', False

    def get(self, suffix: str, *, binary: bool = False):
        if (not re.fullmatch(r"actions/[A-Za-z0-9_/?=&.%+-]+", suffix)
                or ".." in unquote(suffix) or '%' in suffix.partition('?')[0]
                or re.search(r'%(?![0-9a-fA-F]{2})', suffix)):
            raise IdentityError("Invalid workflow API path")
        endpoint = f"repos/{self.repository}/{suffix}"
        # No token is put in command arguments, and gh errors are not echoed.
        # Only public metadata leaves this process; signing secrets are absent.
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("SUKISU_APK_KEYSTORE", "SUKISU_APK_PASSWORD")}
        stage = self._stage(suffix)
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                response = subprocess.run(
                    ["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint],
                    capture_output=True, timeout=60, env=environment,
                )
            except subprocess.TimeoutExpired:
                reason, retry = 'request timeout', True
            except OSError:
                raise IdentityError(f'Manager identity {stage} failed: GitHub CLI could not execute') from None
            else:
                if not response.returncode:
                    break
                reason, retry = self._failure(response.stderr)
            if not retry or attempt == READ_ATTEMPTS:
                raise IdentityError(f'Manager identity {stage} failed: {reason} '
                                    f'(attempt {attempt}/{READ_ATTEMPTS})') from None
            print(f'::warning::Manager identity {stage}: {reason}; '
                  f'retrying ({attempt + 1}/{READ_ATTEMPTS}).', file=sys.stderr)
            time.sleep(attempt)
        if binary:
            if len(response.stdout) > MAX_ARCHIVE_BYTES:
                raise IdentityError("Manager APK exceeds the size limit")
            return response.stdout
        if len(response.stdout) > MAX_API_BYTES:
            raise IdentityError(f'Manager identity {stage} response exceeds the size limit')
        try:
            return json.loads(response.stdout)
        except (ValueError, UnicodeError):
            raise IdentityError(f'Invalid manager identity {stage} API response') from None


def _pages(reader, suffix: str, key: str):
    # Avoid silently missing the identity in a large matrix's artifact list.
    # Job records contain every step and become very large in a full matrix.
    # GitHub can repeatedly return 502 for per_page=100 even when every job is
    # healthy; use bounded job pages while still checking the complete listing.
    page_size = JOBS_PAGE_SIZE if key == 'jobs' else ARTIFACTS_PAGE_SIZE
    for page in range(1, 101):
        separator = "&" if "?" in suffix else "?"
        data = reader.get(f"{suffix}{separator}per_page={page_size}&page={page}")
        if not isinstance(data, dict) or not isinstance(data.get(key), list):
            raise IdentityError("Invalid workflow API listing")
        values = data[key]
        if len(values) > page_size:
            raise IdentityError("Workflow API listing exceeds the requested page size")
        yield from values
        if len(values) < page_size:
            return
    raise IdentityError("Workflow API listing is unexpectedly large")


def _check_run(reader, expected: Expected):
    run = reader.get(f"actions/runs/{expected.run_id}/attempts/{expected.run_attempt}")
    if (not isinstance(run, dict) or run.get("id") != expected.run_id
            or run.get("head_sha") != expected.head_sha
            or run.get("run_attempt") != expected.run_attempt
            or not isinstance(run.get("repository"), dict)
            or str(run["repository"].get("full_name", "")).lower() != expected.repository.lower()):
        raise IdentityError("Workflow API returned a different repository, run, attempt or commit")
    return run


def _validate_producer(job, expected: Expected | ProducerContext, *, job_id: int | None = None,
                       current_attempt: bool = False):
    if (not isinstance(job, dict) or job.get('name') != expected.manager_job
            or job.get('run_id') != expected.run_id or job.get('head_sha') != expected.head_sha):
        raise IdentityError('Manager producer belongs to a different job, workflow run or commit')
    actual_id = positive_integer(job.get('id'), 'manager producer job ID')
    attempt = positive_integer(job.get('run_attempt'), 'manager job attempt')
    if ((job_id is not None and actual_id != job_id) or attempt > expected.run_attempt
            or (current_attempt and attempt != expected.run_attempt)):
        raise IdentityError('Manager producer job ID or attempt differs from its trusted workflow output')
    if job.get('status') not in ('queued', 'in_progress', 'completed'):
        raise IdentityError('Invalid manager producer status')
    if job['status'] == 'completed' and job.get('conclusion') != 'success':
        raise IdentityError('The ' + expected.variant + ' APK job failed or was cancelled; no identity can be accepted')
    return {**job, 'id': actual_id, 'run_attempt': attempt}


def _check_manager_job(reader, expected: Expected):
    if expected.manager_job_id is not None:
        job = reader.get(f'actions/jobs/{expected.manager_job_id}')
        return _validate_producer(job, expected, job_id=expected.manager_job_id)
    jobs = list(_pages(reader, f"actions/runs/{expected.run_id}/attempts/{expected.run_attempt}/jobs", "jobs"))
    matches = [job for job in jobs if isinstance(job, dict) and job.get("name") == expected.manager_job]
    if not matches:
        # Rerun failed jobs retains a successful producer from the previous
        # attempt. Only explicit success from the latest manager job permits
        # reusing that exact attempt's already signed identity.
        jobs = list(_pages(reader, f"actions/runs/{expected.run_id}/jobs?filter=latest", "jobs"))
        matches = [job for job in jobs if isinstance(job, dict) and job.get("name") == expected.manager_job]
    if len(matches) > 1:
        raise IdentityError("More than one manager producer job was found")
    if matches and matches[0].get("status") == "completed" and matches[0].get("conclusion") != "success":
        raise IdentityError("The " + expected.variant + " APK job failed or was cancelled; no identity can be accepted")
    return matches[0] if matches else None


def record_producer_job(context: ProducerContext, reader) -> int:
    """Record the unique current producer once, before kernel matrix expansion."""
    context.validate()
    _check_run(reader, context)
    jobs = list(_pages(reader, f'actions/runs/{context.run_id}/attempts/{context.run_attempt}/jobs', 'jobs'))
    matches = [job for job in jobs if isinstance(job, dict) and job.get('name') == context.manager_job]
    if len(matches) != 1:
        raise IdentityError('Expected exactly one current manager producer before expanding the kernel matrix')
    job = _validate_producer(matches[0], context, current_attempt=True)
    return positive_integer(job['id'], 'manager producer job ID')


def wait_for_identity(expected: Expected, reader, *, timeout: float = 1200, interval: float = 15,
                      monotonic: Callable[[], float] = time.monotonic,
                      sleep: Callable[[float], None] = time.sleep) -> dict[str, str]:
    expected.validate()
    if not 0 < timeout <= 1200 or not 0 < interval <= 60:
        raise IdentityError("Manager wait must be at most 20 minutes with a positive poll interval")
    deadline = monotonic() + timeout
    stale_archives = {}
    _check_run(reader, expected)
    while True:
        manager = _check_manager_job(reader, expected)
        reusable_attempt = None
        if manager and manager.get("status") == "completed" and manager.get("conclusion") == "success":
            attempt = positive_integer(manager.get("run_attempt"), "manager job attempt")
            if attempt < expected.run_attempt:
                reusable_attempt = attempt
        # A successful manager job passes its numeric ID as a trusted needs
        # output. Consumers validate that one exact producer instead of each
        # downloading every step of 108 matrix jobs. If API completion lags,
        # keep waiting; never accept an unfinished exact producer.
        pending_producer = (expected.manager_job_id is not None and manager.get('status') != 'completed')
        artifact_listing = f"actions/runs/{expected.run_id}/artifacts"
        if expected.artifact_name:
            artifact_listing += '?name=' + quote(expected.artifact_name, safe='')
        artifacts = [] if pending_producer else list(_pages(reader, artifact_listing, "artifacts"))
        matches = [item for item in artifacts if isinstance(item, dict)
                   and isinstance(item.get('name'), str) and APK_NAME.fullmatch(item['name'])
                   and (not expected.artifact_name or item['name'] == expected.artifact_name)
                   and not item.get("expired")]
        if len(matches) > 1:
            raise IdentityError("More than one manager identity artifact was found")
        if matches:
            artifact = matches[0]
            producer = artifact.get("workflow_run")
            if (not isinstance(producer, dict) or producer.get("id") != expected.run_id
                    or producer.get("head_sha") != expected.head_sha):
                raise IdentityError("Identity artifact belongs to a different workflow run or commit")
            positive_integer(artifact.get("size_in_bytes"), "identity artifact size", MAX_ARCHIVE_BYTES)
            artifact_id = positive_integer(artifact.get("id"), "identity artifact ID")
            raw = stale_archives.get(artifact_id)
            if raw is None:
                raw = reader.get(f"actions/artifacts/{artifact_id}/zip", binary=True)
            digest = artifact.get('digest')
            if (not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest)
                    or hashlib.sha256(raw).hexdigest() != digest.removeprefix('sha256:')):
                raise IdentityError('Manager artifact SHA256 does not match the GitHub digest')
            try:
                return read_identity_archive(raw, expected, reusable_attempt=reusable_attempt, filename=artifact['name'],
                                             producer_attempt=manager['run_attempt']
                                             if expected.manager_job_id is not None else None)
            except StaleIdentity:
                stale_archives[artifact_id] = raw
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise IdentityError("Timed out waiting for this run's signed " + expected.variant + " manager identity")
        sleep(min(interval, remaining))


def write_environment(path: Path, values: dict[str, str]):
    # These values have already passed strict single-line validation. Do not
    # append any requested (as opposed to signed and verified) identity values.
    with path.open("a", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-ref")
    parser.add_argument('--record-producer-job', action='store_true',
                        help='Record the unique current manager job ID before the matrix expands')
    parser.add_argument('--manager-job-id', help='Trusted numeric output from the successful manager producer')
    parser.add_argument("--artifact-name", default='', help='Optional exact final APK filename; otherwise require one APK')
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    parser.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    parser.add_argument("--head-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--manager-job-name", default=None)
    parser.add_argument('--variant', choices=MANAGERS, default=os.environ.get('KSU_VARIANT', 'SukiSU'))
    parser.add_argument("--github-env", type=Path, default=os.environ.get("GITHUB_ENV"))
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args()
    try:
        manager_job = args.manager_job_name or f'编译配套 {args.variant} 管理器 APK'
        if args.record_producer_job:
            if args.manager_job_id is not None:
                raise IdentityError('Producer discovery cannot be combined with an existing producer job ID')
            context = ProducerContext(args.repo, positive_integer(args.run_id, 'run ID'),
                                      positive_integer(args.run_attempt, 'run attempt'),
                                      args.head_sha, manager_job, args.variant)
            job_id = record_producer_job(context, GitHubReader(context.repository))
            output = os.environ.get('GITHUB_OUTPUT', '')
            if not output:
                raise IdentityError('GITHUB_OUTPUT is required to publish the current manager producer ID')
            with Path(output).open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(f'manager_job_id={job_id}\n')
            print(f'Verified current {args.variant} manager producer job {job_id}.')
            return 0
        if args.expected_ref is None:
            raise IdentityError('--expected-ref is required when consuming a signed manager identity')
        if args.github_env is None:
            raise IdentityError("GITHUB_ENV or --github-env is required")
        expected = Expected(args.repo, positive_integer(args.run_id, "run ID"),
                            positive_integer(args.run_attempt, "run attempt"), args.head_sha,
                            args.expected_ref, args.artifact_name,
                            manager_job, args.variant,
                            positive_integer(args.manager_job_id, 'manager producer job ID')
                            if args.manager_job_id is not None else None)
        values = wait_for_identity(expected, GitHubReader(expected.repository),
                                   timeout=args.timeout, interval=args.interval)
        write_environment(args.github_env, values)
    except (IdentityError, OSError, TypeError, AttributeError, KeyError, ValueError) as error:
        # A malformed remote value must not echo arbitrary payloads or credentials.
        message = str(error) if isinstance(error, IdentityError) else "Invalid signed manager identity data"
        print(f"Manager identity verification failed: {message}", file=sys.stderr)
        return 1
    print("Verified this run's signed " + args.variant + " APK identity; kernel compilation can proceed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
