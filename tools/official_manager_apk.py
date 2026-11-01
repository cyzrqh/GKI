"""Obtain an unchanged official APK built from the exact selected source SHA."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import zipfile

from apk_manifest import manifest_identity
from embedded_build_metadata import MAX_APK_BYTES, read_official_manager, validate_members, verify_apk_v2
from manager_variants import ADDITIONAL_MANAGERS, manager_profile, valid_manager_apk_filename
from resolve_build_sources import github_json, github_commit, TIER_ALIASES
from snapshot_ksu_source import git, source_identity
from prepare_manager_lkm import native_version_code

WORKFLOWS = {'KernelSU-Next': 'build-manager-ci.yml', 'KernelSU': 'build-manager.yml',
             'KowSU': 'build-manager.yml', 'FinaSU': 'build-manager.yml'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download(url: str) -> bytes:
    """Follow asset redirects without forwarding GitHub credentials to storage."""
    original = urlsplit(url)
    if original.scheme != 'https' or original.hostname != 'api.github.com' or original.username:
        raise ValueError('Official APK downloads must start at the GitHub API')
    opener = build_opener(NoRedirect())
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    for attempt in range(3):
        target = url
        try:
            for redirect in range(6):
                parsed = urlsplit(target)
                if (parsed.scheme != 'https' or parsed.username or parsed.password
                        or not (parsed.hostname == 'api.github.com'
                                or parsed.hostname and (parsed.hostname.endswith('.githubusercontent.com')
                                                         or parsed.hostname.endswith('.blob.core.windows.net')))):
                    raise ValueError('Official APK storage redirect is not an approved HTTPS host')
                accept = ('application/vnd.github+json' if '/actions/artifacts/' in parsed.path
                          else 'application/octet-stream')
                headers = {'User-Agent': 'gki-official-manager', 'Accept': accept}
                if token and parsed.hostname == 'api.github.com':
                    headers['Authorization'] = 'Bearer ' + token
                try:
                    with opener.open(Request(target, headers=headers), timeout=60) as response:
                        raw = response.read(MAX_APK_BYTES + 1)
                    if not 0 < len(raw) <= MAX_APK_BYTES:
                        raise ValueError('Official manager download exceeds its size bound')
                    return raw
                except HTTPError as error:
                    if error.code not in (301, 302, 303, 307, 308):
                        raise
                    target = error.headers.get('Location', '')
            raise ValueError('Official manager download exceeded its redirect bound')
        except (OSError, HTTPError):
            if attempt == 2:
                raise ValueError('Official manager asset download failed after bounded retries') from None
            time.sleep(attempt + 1)
    raise ValueError('Official manager asset download did not complete')


def select_asset(variant: str, reference: str, tier: str, branch: str, *, api=github_json,
                 commit=github_commit) -> dict | None:
    profile = manager_profile(variant)
    repository = profile['repository']
    tier = TIER_ALIASES.get(tier, tier)
    if tier == 'Stable(标准)':
        release = api(repository, '/releases/tags/' + quote(branch, safe=''))
        if (release.get('tag_name') != branch or release.get('draft') is not False
                or release.get('prerelease') is not False or commit(repository, branch) != reference):
            raise ValueError('Official release APK does not match the selected stable source')
        candidates = [asset for asset in release.get('assets', [])
                      if valid_manager_apk_filename(asset.get('name'), variant)
                      and 'spoof' not in asset['name'].lower()]
        if len(candidates) != 1:
            raise ValueError('The official stable release does not provide one canonical manager APK')
        asset = candidates[0]
        if type(asset.get('id')) is not int or asset['id'] < 1:
            raise ValueError('Official APK asset identity is invalid')
        return {'url': f'https://api.github.com/repos/{repository}/releases/assets/{asset["id"]}',
                'filename': asset['name'], 'digest': asset.get('digest'), 'release_tag': branch}
    workflow = WORKFLOWS[variant]
    response = api(repository, '/actions/workflows/' + workflow + '/runs?head_sha=' + reference + '&per_page=20')
    for run in response.get('workflow_runs', []):
        if (run.get('head_sha') != reference or run.get('status') != 'completed'
                or run.get('conclusion') != 'success' or run.get('event') == 'pull_request'):
            continue
        origin = run.get('head_repository') or {}
        if origin.get('full_name', '').casefold() != repository.casefold():
            raise ValueError('Official manager CI came from another repository')
        run_id = run.get('id')
        if type(run_id) is not int or run_id < 1:
            raise ValueError('Official manager CI run identity is invalid')
        artifacts = api(repository, f'/actions/runs/{run_id}/artifacts?per_page=100')
        matches = [a for a in artifacts.get('artifacts', [])
                   if a.get('name') in ('manager', 'Manager', 'Manager-release') and a.get('expired') is False]
        if len(matches) != 1:
            continue
        artifact = matches[0]
        if type(artifact.get('id')) is not int or artifact['id'] < 1:
            raise ValueError('Official manager CI artifact identity is invalid')
        return {'url': f'https://api.github.com/repos/{repository}/actions/artifacts/{artifact["id"]}/zip',
                'digest': artifact.get('digest'), 'workflow_run_id': run_id}
    return None


def unpack_apk(raw: bytes, variant: str, *, filename: str | None = None) -> tuple[str, bytes]:
    profile = manager_profile(variant)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = validate_members(archive)
        if 'AndroidManifest.xml' in members:
            if not valid_manager_apk_filename(filename, variant):
                raise ValueError('Raw official APK lacks its original published filename')
            return filename, raw
        matches = []
        for name, member in members.items():
            base = Path(name).name
            if not valid_manager_apk_filename(base, variant) or not 0 < member.file_size <= MAX_APK_BYTES:
                continue
            candidate = archive.read(member)
            with zipfile.ZipFile(io.BytesIO(candidate)) as apk:
                identity = manifest_identity(apk.read('AndroidManifest.xml'))
            if identity['package'] == profile['package']:
                matches.append((base, candidate))
        if len(matches) != 1:
            raise ValueError('Official artifact does not contain one APK with the native application ID')
        return matches[0]


def obtain(source: Path, output: Path, variant: str, reference: str, tier: str, branch: str,
           *, api=github_json, fetch=download, wait=600, sleep=time.sleep) -> dict:
    if variant not in ADDITIONAL_MANAGERS or not re.fullmatch(r'[0-9a-f]{40}', reference):
        raise ValueError('Official APK preparation requires one supported manager and a fixed full source SHA')
    identity = source_identity(source, reference)
    code = native_version_code(source, identity['git_count'], variant=variant)
    deadline = time.monotonic() + wait
    while True:
        asset = select_asset(variant, reference, tier, branch, api=api)
        if asset is not None:
            break
        if time.monotonic() >= deadline:
            raise ValueError('该官方源码尚无可用的成功 APK 构建；未混用旧版 APK，请待官方 CI 完成后重试')
        print('Waiting for the official APK from the exact selected source commit.', flush=True)
        sleep(min(20, max(0, deadline - time.monotonic())))
    raw = fetch(asset['url'])
    expected_digest = asset.get('digest')
    if expected_digest is not None and expected_digest != 'sha256:' + hashlib.sha256(raw).hexdigest():
        raise ValueError('Official manager asset digest differs from GitHub metadata')
    name, apk = unpack_apk(raw, variant, filename=asset.get('filename'))
    with zipfile.ZipFile(io.BytesIO(apk)) as archive:
        native = manifest_identity(archive.read('AndroidManifest.xml'))
    if native['version_code'] != code:
        raise ValueError('Official manager APK numeric version differs from the selected kernel source history')
    certificate = verify_apk_v2(apk)
    metadata = {'schema_version': 1, 'file': name, 'package': native['package'],
                'ksu_variant': variant, 'ksu_ref': reference, 'ksu_branch': tier,
                'ksu_version_code': code, 'ksu_version_name': native['version_name'],
                'manager_cert_size': len(certificate), 'manager_cert_sha256': hashlib.sha256(certificate).hexdigest(),
                'apk_sha256': hashlib.sha256(apk).hexdigest(),
                'producer_repository': os.environ.get('GITHUB_REPOSITORY'),
                'producer_run_id': int(os.environ.get('GITHUB_RUN_ID', '0')),
                'producer_run_attempt': int(os.environ.get('GITHUB_RUN_ATTEMPT', '0')),
                'producer_head_sha': os.environ.get('GITHUB_SHA'),
                'upstream_apk': {'repository': manager_profile(variant)['repository'], 'head_sha': reference,
                                 **{key: value for key, value in asset.items() if key in ('release_tag', 'workflow_run_id')}}}
    read_official_manager(apk, metadata, filename=name)
    output.mkdir(parents=True, exist_ok=True)
    target = output / name
    target.write_bytes(apk)
    android = os.environ.get('ANDROID_HOME', '')
    signers = sorted(Path(android, 'build-tools').glob('*/apksigner')) if android else []
    if not signers:
        raise ValueError('Android apksigner is required to verify the official APK signing schemes')
    subprocess.run([str(signers[-1]), 'verify', '--verbose', str(target)], check=True, timeout=90)
    (output / 'manager.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', choices=ADDITIONAL_MANAGERS, required=True)
    parser.add_argument('--expected-ref', required=True)
    parser.add_argument('--tier', required=True)
    parser.add_argument('--source-branch', required=True)
    args = parser.parse_args(argv)
    try:
        metadata = obtain(args.source, args.output, args.variant, args.expected_ref, args.tier, args.source_branch)
        values = {'apk_filename': metadata['file'], 'manager_package': metadata['package'],
                  'manager_version_code': metadata['ksu_version_code'], 'manager_version_name': metadata['ksu_version_name'],
                  **{key: metadata[key] for key in ('manager_cert_size', 'manager_cert_sha256', 'apk_sha256')},
                  'manager_metadata': json.dumps(metadata, ensure_ascii=True, separators=(',', ':'))}
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as stream:
            for key, value in values.items():
                stream.write(f'{key}={value}\n')
        print('Official paired manager verified: ' + metadata['file'])
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('::error::Official manager APK preparation failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
