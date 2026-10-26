#!/usr/bin/env python3
"""Save public, verified APK metadata for release notes."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re

from wait_manager_identity import Expected, positive_integer, validate_metadata
from pin_build_dependencies import load_profile, source_policy
from manager_variants import selected_variant


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    fields = {key: os.environ.get('META_' + key.upper(), '') for key in
              ('file', 'package', 'ksu_version_code', 'ksu_version_name', 'ksu_ref', 'ksu_branch')}
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*\.apk', fields['file'])
            or not re.fullmatch(r'[0-9a-f]{40}', fields['ksu_ref'])
            or not re.fullmatch(r'[0-9]+', fields['ksu_version_code'])
            or any('\n' in value or '\r' in value for value in fields.values())):
        raise SystemExit('Invalid verified manager release metadata')
    variant = selected_variant()
    # Prepare has already proved the selected source and full SUSFS port before
    # any manager job runs. Checked-patch sources need not be a fixed commit.
    susfs_enabled = os.environ.get('META_ENABLE_SUSFS') == 'true'
    susfs_version = None
    if variant == 'SukiSU':
        profile = load_profile(Path(__file__).resolve().parents[1])
        compatible_source = (source_policy(profile) == 'checked-patch'
                             or fields['ksu_ref'].lower() in {commit.lower() for commit in profile['ksu_commits']})
        susfs_version = profile['version'] if susfs_enabled and compatible_source else None
    fields.update(ksu_variant=variant, built_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  susfs_version=susfs_version)
    # Signer outputs supply these public certificate/APK hashes. No private key
    # or signing password is ever put in this identity artifact.
    fields.update(
        manager_cert_size=os.environ.get('META_MANAGER_CERT_SIZE', ''),
        manager_cert_sha256=os.environ.get('META_MANAGER_CERT_SHA256', ''),
        apk_sha256=os.environ.get('META_APK_SHA256', ''),
        producer_repository=os.environ.get('GITHUB_REPOSITORY', ''),
        producer_run_id=os.environ.get('GITHUB_RUN_ID', ''),
        producer_run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT', ''),
        producer_head_sha=os.environ.get('GITHUB_SHA', ''),
    )
    expected = Expected(fields['producer_repository'],
                        positive_integer(fields['producer_run_id'], 'producer run ID'),
                        positive_integer(fields['producer_run_attempt'], 'producer run attempt'),
                        fields['producer_head_sha'], fields['ksu_ref'],
                        fields['file'], variant=variant)
    expected.validate()
    validate_metadata(fields, expected)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fields, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
