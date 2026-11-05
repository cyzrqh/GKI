"""Bounded official metadata HTTP and per-run fixed-commit observations.

Only the update checker uses this transport. Kernel source/archive recovery
keeps its independent client. The temporary cache is never restored from an
Actions cache, a PR, a repository artifact or an earlier workflow run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
import uuid

from official_susfs_archive import API, NoRedirect
from snapshot_ksu_source import SnapshotError

SHA = re.compile(r'[0-9a-f]{40}\Z')
POLICY = 'same-build-tree-v1'
MAX_ATTEMPTS = 4
MAX_RETRY_DELAY = 60
MAX_CACHE_BYTES = 4096
MIN_REQUEST_INTERVAL = 0.75
TREE_URL = re.compile(re.escape(API) + r'/repository/tree\?ref=[0-9a-f]{40}&per_page=100&page=(?:[1-9]|10)\Z')
HEADER_URL = re.compile(re.escape(API) + r'/repository/files/kernel_patches%2Finclude%2Flinux%2Fsusfs\.h/raw\?ref=[0-9a-f]{40}\Z')


class RequestGate:
    """Space metadata calls and share a server-requested cooldown across workers."""
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.next_request = 0.0

    def wait(self):
        while True:
            with self.lock:
                delay = self.next_request - self.clock()
                if delay <= 0:
                    self.next_request = self.clock() + MIN_REQUEST_INTERVAL
                    return
            self.sleep(min(delay, 30))

    def cooldown(self, delay):
        with self.lock:
            self.next_request = max(self.next_request, self.clock() + delay)


GATE = RequestGate()


def retry_delay(value, fallback, *, now=None):
    if value is None:
        return fallback
    if not isinstance(value, str) or len(value) > 128:
        raise SnapshotError('Official SUSFS metadata Retry-After is invalid')
    if re.fullmatch(r'[0-9]+', value.strip()):
        seconds = int(value.strip())
    else:
        try:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                raise ValueError('Missing timezone')
            seconds = max(0, (instant - (now or datetime.now(timezone.utc))).total_seconds())
        except (TypeError, ValueError, OverflowError):
            raise SnapshotError('Official SUSFS metadata Retry-After is invalid') from None
    if seconds > MAX_RETRY_DELAY:
        # Never retry before the server's window, nor hang this job indefinitely.
        raise SnapshotError('Official SUSFS rate limit requires a later retry; no unverified update was applied')
    return max(fallback, seconds)


def request_bytes(url, limit, *, opener=None, gate=None):
    if not isinstance(url, str) or not (TREE_URL.fullmatch(url) or HEADER_URL.fullmatch(url)):
        raise SnapshotError('Unexpected official SUSFS metadata destination')
    if type(limit) is not int or not 1 <= limit <= 2 * 1024 * 1024:
        raise SnapshotError('Invalid official SUSFS metadata response bound')
    opener, gate = opener or build_opener(NoRedirect()), gate or GATE
    for attempt in range(MAX_ATTEMPTS):
        gate.wait()
        # Public GitLab metadata: never send any GitHub token or credentials.
        request = Request(url, headers={'User-Agent': 'gki-official-susfs-sync', 'Accept': 'application/json'})
        try:
            with opener.open(request, timeout=45) as response:
                if response.status != 200 or response.geturl() != url:
                    raise SnapshotError('Official SUSFS metadata returned an unexpected status or URL')
                declared = response.headers.get('Content-Length')
                if declared is not None and (not declared.isdigit() or int(declared) > limit):
                    raise SnapshotError('Official SUSFS metadata response exceeds its bound')
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise SnapshotError('Official SUSFS metadata response exceeds its bound')
                return raw
        except HTTPError as error:
            code, retry_after = error.code, error.headers.get('Retry-After') if error.headers else None
            error.close()
            if code not in (408, 429, 500, 502, 503, 504) or attempt + 1 == MAX_ATTEMPTS:
                raise SnapshotError(f'Official SUSFS metadata HTTP request failed (HTTP {code}); no unverified update was applied') from None
            fallback = (10, 20, 40)[attempt] if code == 429 else (2, 5, 10)[attempt]
            delay = retry_delay(retry_after, fallback) if code in (429, 503) else fallback
            if code == 429:
                print(f'Official SUSFS metadata rate limited; respecting shared {delay:g}s cooldown', flush=True)
            gate.cooldown(delay)
        except (OSError, URLError, TimeoutError):
            if attempt + 1 == MAX_ATTEMPTS:
                raise SnapshotError('Official SUSFS metadata transport failed after four attempts') from None
            gate.cooldown((2, 5, 10)[attempt])
    raise AssertionError('Unreachable official SUSFS retry state')


def validate_metadata(value, reference):
    if (not isinstance(value, dict) or set(value) != {'ref', 'version', 'build_tree_sha256'}
            or value.get('ref') != reference or not isinstance(reference, str) or not SHA.fullmatch(reference)
            or not isinstance(value.get('version'), str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', value['version'])
            or not isinstance(value.get('build_tree_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', value['build_tree_sha256'])):
        raise SnapshotError('Invalid verified SUSFS commit observation')
    return dict(value)


def _canonical(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':')).encode('utf-8')


class RunMetadataCache:
    def __init__(self, directory, identity):
        self.directory, self.identity = directory, identity

    @classmethod
    def from_environment(cls):
        directory = os.environ.get('GKI_SUSFS_METADATA_CACHE')
        if not directory:
            return None
        identity = {name: os.environ.get(name, '') for name in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_REPOSITORY')}
        if (not re.fullmatch(r'[1-9][0-9]{0,19}', identity['GITHUB_RUN_ID'])
                or not re.fullmatch(r'[1-9][0-9]{0,5}', identity['GITHUB_RUN_ATTEMPT'])
                or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', identity['GITHUB_REPOSITORY'])):
            raise SnapshotError('SUSFS temporary metadata reuse requires this workflow run identity')
        if not os.environ.get('RUNNER_TEMP'):
            raise SnapshotError('SUSFS temporary metadata requires RUNNER_TEMP')
        temporary = Path(os.environ['RUNNER_TEMP']).absolute()
        path = Path(directory).absolute()
        if (not temporary.is_dir() or path == temporary or not path.resolve().is_relative_to(temporary.resolve())
                or any(item.is_symlink() or getattr(item, 'is_junction', lambda: False)()
                       for item in (path, *path.parents) if item.is_relative_to(temporary))):
            raise SnapshotError('SUSFS metadata reuse must stay in a real per-run temporary directory')
        path.mkdir(parents=True, exist_ok=True)
        return cls(path, identity)

    def path(self, reference):
        if not isinstance(reference, str) or not SHA.fullmatch(reference):
            raise SnapshotError('SUSFS temporary metadata requires a fixed commit')
        path = self.directory / (reference + '.json')
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise SnapshotError('SUSFS temporary observation cannot be a linked file')
        return path

    def read(self, reference):
        path = self.path(reference)
        if not path.exists():
            return None
        try:
            if not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
                raise ValueError('Observation exceeds size bound')
            def unique(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ValueError('Duplicate observation field')
                    value[key] = item
                return value
            document = json.loads(path.read_bytes(), object_pairs_hook=unique)
            if (not isinstance(document, dict) or set(document) != {'schema_version', 'source', 'policy', 'run', 'metadata', 'sha256'}
                    or type(document['schema_version']) is not int or document['schema_version'] != 1
                    or document['source'] != API or document['policy'] != POLICY or document['run'] != self.identity
                    or document['sha256'] != hashlib.sha256(_canonical(document['metadata'])).hexdigest()):
                raise ValueError('Observation identity differs')
            return validate_metadata(document['metadata'], reference)
        except (ValueError, UnicodeError, SnapshotError):
            # Bad/old observations never authorize a change. Fetch this exact
            # commit from the official API and validate it afresh instead.
            return None

    def write(self, reference, metadata):
        path = self.path(reference)
        metadata = validate_metadata(metadata, reference)
        document = {'schema_version': 1, 'source': API, 'policy': POLICY, 'run': self.identity,
                    'metadata': metadata, 'sha256': hashlib.sha256(_canonical(metadata)).hexdigest()}
        staged = path.with_name('.susfs-metadata-' + uuid.uuid4().hex + '.tmp')
        try:
            with staged.open('xb') as stream:
                os.chmod(staged, 0o600)
                stream.write(_canonical(document) + b'\n')
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
