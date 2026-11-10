#!/usr/bin/env python3
"""Recover a signed manager from retained Actions artifacts when its cache is gone."""
from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import zipfile
import zlib

from embedded_build_metadata import MAX_APK_BYTES, metadata_json
from manager_variants import MANAGERS, valid_manager_apk_filename
from prebuilt_manager_history import run_artifacts, successful_runs, verified_artifact
from prepare_manager_lkm import native_version_code
from reusable_manager_apk import CHANNELS, compact, manager_selection, recipe_hash, stage, workflow_consumer
from wait_manager_identity import GitHubReader


INVALID_APK = (OSError, ValueError, RuntimeError, zipfile.BadZipFile, struct.error, zlib.error)
MAX_DOWNLOADS = 4


def artifact_apk(data: bytes, filename: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if 'AndroidManifest.xml' in archive.namelist():
            return data
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != filename or not 0 < entries[0].file_size <= MAX_APK_BYTES:
            raise ValueError('Expected one original manager APK')
        return archive.read(entries[0])


def restore(reader, *, cache: Path, output: Path, consumer: dict, selection: dict, branch: str,
            version: int | None) -> dict:
    try:
        return stage(cache, output, consumer=consumer, selection=selection)
    except INVALID_APK:
        pass
    if version is None:
        return {}
    version_suffix = re.compile(rf'_{version}(?:-(?:arm64-v8a|universal))?-release\.apk\Z')
    downloads = 0
    for run in successful_runs(reader, consumer['repository'], branch, current_run=consumer['run_id']):
        for item in run_artifacts(reader, run):
            name = item['name']
            if not valid_manager_apk_filename(name, selection['ksu_variant']) or not version_suffix.search(name):
                continue
            if downloads >= MAX_DOWNLOADS:
                return {}
            downloads += 1
            try:
                payload = verified_artifact(reader, run, item, limit=MAX_APK_BYTES)
                apk = artifact_apk(payload, name)
                with tempfile.TemporaryDirectory(prefix='gki-apk-restore-') as temporary:
                    directory = Path(temporary)
                    (directory / name).write_bytes(apk)
                    result = stage(directory, output, consumer=consumer, selection=selection)
                print(f"Recovered signed manager APK from run {run['id']}: {name}", file=sys.stderr)
                return result
            except INVALID_APK:
                continue
    return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', required=True, choices=MANAGERS)
    parser.add_argument('--channel', required=True, choices=CHANNELS)
    parser.add_argument('--ksu-ref', required=True)
    parser.add_argument('--cert-size', required=True)
    parser.add_argument('--cert-sha256', required=True)
    parser.add_argument('--source-identity', required=True)
    parser.add_argument('--cache-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--source-dir', required=True, type=Path)
    parser.add_argument('--branch', required=True)
    args = parser.parse_args(argv)
    try:
        recipe = recipe_hash(Path('.'))
        identity = metadata_json(args.source_identity.encode('utf-8'))
        selection = manager_selection(args.variant, args.channel, args.ksu_ref, args.cert_size,
                                      args.cert_sha256, recipe, identity)
        consumer = workflow_consumer()
        try:
            version = native_version_code(args.source_dir, identity['git_count'], variant=args.variant)
        except (OSError, ValueError):
            version = None
        result = restore(GitHubReader(consumer['repository']), cache=args.cache_dir, output=args.output_dir,
                         consumer=consumer, selection=selection, branch=args.branch, version=version)
    except INVALID_APK as error:
        print('Manager APK recovery unavailable: ' + str(error), file=sys.stderr)
        result = {}
    if not result:
        print('No matching signed manager APK was found; building the selected source.', file=sys.stderr)
    if output := os.environ.get('GITHUB_OUTPUT'):
        with Path(output).open('a', encoding='utf-8', newline='\n') as stream:
            for key, value in result.items():
                stream.write(f'{key}={value}\n')
    else:
        print(compact(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
