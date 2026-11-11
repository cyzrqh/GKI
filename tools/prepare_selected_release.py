#!/usr/bin/env python3
"""Verify selected managers separately and prepare one release of original files."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import zipfile

from embedded_build_metadata import (KERNEL_MEMBER, metadata_json, read_manager_apk,
                                     read_member, official_receipt_from_environment)
from kernel_order import natural_key
from manager_variants import valid_manager_apk_filename
from prepare_release import prepare as prepare_gki, digest, regular, release_title
from release_notes import asset_names
from resolve_manager_selection import active_managers, validate_receipt


SUFFIXES = {'SukiSU': 'SUKISU', 'BakaSU': 'BAKASU', 'KernelSU-Next': 'KERNELSU_NEXT',
            'KernelSU': 'KERNELSU', 'KowSU': 'KOWSU', 'FinaSU': 'FINASU'}
OPERATIONS = ('编译内核', '仅编译管理器安装包')


def combined_release_title(managers: list[dict], *, platform: str) -> str:
    """Keep each manager on its own line in GitHub's release directory."""
    prefix = platform + ' · '
    rows = [release_title(manager, platform=platform).removeprefix(prefix)
            for manager in managers]
    # Ordinary newlines collapse in GitHub's title links. A Unicode line
    # separator also preserves one manager per line in the release sidebar.
    # Figure spaces follow numeral widths in both normal and selected links.
    indent = '\u2007' * 8 + ('\u200a\u2060' if platform == 'OnePlus' else '')
    return prefix + ('\u2028' + indent).join(rows)


@contextmanager
def manager_environment(variant: str, metadata: dict | None):
    values = {'KSU_VARIANT': variant, 'GKI_MANAGER_METADATA':
              json.dumps(metadata, separators=(',', ':')) if metadata is not None else None}
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def manager_options(selection: str, inputs: dict, resolution=None) -> dict:
    variants = active_managers(selection, resolution)
    if not variants:
        raise ValueError('A release requires at least one active manager')
    if not isinstance(inputs, dict) or any(variant not in inputs for variant in variants):
        raise ValueError('Missing selected manager producer outputs')
    if resolution is not None and set(inputs) != set(variants):
        raise ValueError('Producer outputs must match the active managers exactly')
    result = {}
    for variant in variants:
        row = inputs[variant]
        if (not isinstance(row, dict)
                or set(row) != {'apk_filename', 'metadata', 'expected_count', 'full_catalog'}
                or not valid_manager_apk_filename(row['apk_filename'], variant)
                or row['metadata'] is not None and not isinstance(row['metadata'], dict)
                or type(row['expected_count']) is not int or row['expected_count'] < 0
                or type(row['full_catalog']) is not bool):
            raise ValueError('Invalid selected manager producer outputs: ' + variant)
        result[variant] = dict(row)
    return result


def original_files(source: Path) -> dict[str, Path]:
    if source.is_symlink() or not source.is_dir():
        raise ValueError('Expected an unlinked original artifact input directory')
    paths = list(source.rglob('*'))
    if len(paths) > 10000 or any(path.is_symlink() for path in paths):
        raise ValueError('Linked or oversized release input trees are refused')
    files = {}
    for path in paths:
        if not path.is_file():
            if not path.is_dir():
                raise ValueError('Unexpected release input path')
            continue
        regular(path)
        name = path.name
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+@()-]{0,249}', name) or '..' in name
                or not (name.endswith(('.apk', '-AnyKernel3.zip'))
                        or re.fullmatch(r'.+-boot[^/]*\.img', name))):
            raise ValueError('Expected only original APK, AnyKernel3 and boot files: ' + name)
        if name in files:
            raise ValueError('Duplicate original artifact filename: ' + name)
        files[name] = path
    if not files:
        raise ValueError('Original release artifacts are missing')
    return files


def verify_producer(document: dict) -> None:
    for field, variable in (('producer_repository', 'GITHUB_REPOSITORY'),
                            ('producer_head_sha', 'GITHUB_SHA'), ('producer_run_id', 'GITHUB_RUN_ID')):
        current = os.environ.get(variable, '')
        if not current or str(document.get(field, '')).casefold() != current.casefold():
            raise ValueError('Selected artifact belongs to another run or producer commit')
    try:
        attempt, current = int(document['producer_run_attempt']), int(os.environ.get('GITHUB_RUN_ATTEMPT', '0'))
    except (ValueError, TypeError, KeyError):
        raise ValueError('Selected artifact has an invalid producer attempt') from None
    if not 1 <= attempt <= current:
        raise ValueError('Selected artifact belongs to an invalid producer attempt')


def group_files(files: dict[str, Path], options: dict, platform: str) -> dict[str, list[Path]]:
    groups = {variant: [] for variant in options}
    claimed = set()
    for variant, row in options.items():
        name = row['apk_filename']
        if name not in files or name in claimed:
            raise ValueError('Missing or duplicate selected manager APK: ' + name)
        groups[variant].append(files[name])
        claimed.add(name)
    member = KERNEL_MEMBER
    if platform == 'oplus':
        from oplus_assets import MEMBER
        member = MEMBER
    for name, path in files.items():
        if not name.endswith('-AnyKernel3.zip'):
            continue
        # This bounded metadata read only routes a file. The original platform
        # verifier below still checks its complete Image, identity and hashes.
        with zipfile.ZipFile(path) as archive:
            document = read_member(archive, member)
        verify_producer(document)
        variant = document.get('ksu_variant')
        if variant not in groups or document.get('file') != name:
            raise ValueError('Unexpected manager or filename in kernel artifact: ' + name)
        names = asset_names(document)
        if name not in names:
            raise ValueError('Kernel metadata does not claim its original filename')
        for selected in names:
            if (selected not in files or selected in claimed
                    or selected != name and not re.fullmatch(r'.+-boot[^/]*\.img', selected)):
                raise ValueError('Missing, duplicate or unexpected kernel output: ' + selected)
            groups[variant].append(files[selected])
            claimed.add(selected)
    if claimed != set(files):
        raise ValueError('Unexpected or unclaimed original release artifacts: '
                         + ', '.join(sorted(set(files) - claimed)))
    return groups


def link_inputs(paths: list[Path], destination: Path) -> None:
    destination.mkdir()
    for path in paths:
        try:
            os.link(path, destination / path.name)
        except OSError:
            shutil.copyfile(path, destination / path.name)


def checksums(directory: Path) -> dict[str, str]:
    result = {}
    for line in (directory / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+@()-]{0,249})', line)
        if not match or match[2] in result or match[2] == 'SHA256SUMS.txt':
            raise ValueError('Invalid verified checksum list')
        result[match[2]] = match[1]
    if not result:
        raise ValueError('Verified checksum list is empty')
    return result


def prepare(source: Path, output: Path, *, selection: str, platform: str, operation: str,
            manager_inputs: dict, oplus_matrix: dict | None = None, resolution=None) -> dict:
    if platform not in ('gki', 'oplus') or operation not in OPERATIONS:
        raise ValueError('Expected a supported release platform and operation')
    if resolution is not None:
        resolution = validate_receipt(resolution, selection=selection)
    options = manager_options(selection, manager_inputs, resolution)
    source, output = Path(source), Path(output)
    if output.is_symlink() or output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Release output must be new or empty; existing files are preserved')
    source, output = source.absolute(), output.absolute()
    if source.resolve() == output.resolve() or source.resolve() in output.resolve().parents:
        raise ValueError('Release output must be independent of its input')
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError('Release output requires an existing unlinked parent directory')
    files = original_files(source)
    groups = group_files(files, options, platform)
    publish = operation == '编译内核' and all(row['full_catalog'] for row in options.values())
    if operation == '编译内核' and any(row['expected_count'] == 0 for row in options.values()):
        raise ValueError('Kernel release verification requires a positive prepared kernel count')
    if platform == 'oplus':
        if (operation != '编译内核' or not isinstance(oplus_matrix, dict)
                or set(oplus_matrix) != {'include'} or not isinstance(oplus_matrix['include'], list)
                or any(row['expected_count'] != len(oplus_matrix['include']) for row in options.values())):
            raise ValueError('OPlus requires its complete prepared source matrix and matching counts')
    records, merged, total_kernels, single_report = [], {}, 0, None
    with tempfile.TemporaryDirectory(prefix='.selected-release-', dir=output.parent) as temporary:
        stage = Path(temporary)
        for variant, row in options.items():
            inputs, report = stage / (SUFFIXES[variant] + '-input'), stage / (SUFFIXES[variant] + '-report')
            link_inputs(groups[variant], inputs)
            with manager_environment(variant, row['metadata']):
                manager = read_manager_apk(inputs / row['apk_filename'],
                                           external_metadata=official_receipt_from_environment())
                verify_producer(manager)
                if manager['ksu_variant'] != variant:
                    raise ValueError('Selected APK belongs to another manager: ' + variant)
                if resolution is not None:
                    fixed = resolution['sources'][variant]
                    if (manager['ksu_ref'] != fixed['ksu_ref']
                            or manager['ksu_branch'] != fixed['effective_ksu_branch']):
                        raise ValueError('Selected APK differs from the fixed official source or channel: ' + variant)
                if platform == 'gki':
                    result = prepare_gki(inputs, report,
                        expected_kernel_count=row['expected_count'] if operation == '编译内核' else 0,
                        report_kind='release' if publish else 'actions')
                    assets, notes = report / 'assets', report / 'release.md'
                    count = result['kernels']
                else:
                    from oplus_assets import collect
                    result = collect(inputs, report, oplus_matrix, variant)
                    count, notes = result['count'], report / 'summary.md'
                    assets = stage / (SUFFIXES[variant] + '-assets')
                    assets.mkdir()
                    for path in groups[variant]:
                        shutil.copyfile(path, assets / path.name)
                    shutil.copyfile(report / 'SHA256SUMS.txt', assets / 'SHA256SUMS.txt')
            expected = checksums(assets)
            if set(expected) != {path.name for path in groups[variant]}:
                raise ValueError('Verified output does not cover its complete original input group')
            for name, checksum in expected.items():
                if name in merged or digest(assets / name) != checksum:
                    raise ValueError('Duplicate or changed verified release asset: ' + name)
                merged[name] = (assets / name, checksum)
            records.append((variant, manager, notes.read_text(encoding='utf-8'),
                            (report / 'title.txt').read_text(encoding='utf-8')))
            if len(options) == 1:
                single_report = (notes, report / 'title.txt', assets / 'SHA256SUMS.txt')
            total_kernels += count
        ready = stage / 'publish'
        assets = ready / 'assets'
        assets.mkdir(parents=True)
        for name in sorted(merged, key=natural_key):
            path, checksum = merged[name]
            path.replace(assets / name)
            if digest(assets / name) != checksum:
                raise ValueError('Release asset changed during final preparation: ' + name)
        if single_report is not None:
            # Keep the established single-manager report and checksum bytes,
            # including its platform's ordering and line endings.
            for original, target in zip(single_report,
                    (ready / 'release.md', ready / 'title.txt', assets / 'SHA256SUMS.txt')):
                shutil.copyfile(original, target)
        else:
            (assets / 'SHA256SUMS.txt').write_text(''.join(merged[name][1] + '  ' + name + '\n'
                for name in sorted(merged, key=natural_key)), encoding='utf-8', newline='\n')
            label = 'Android' if platform == 'gki' else 'OnePlus'
            title = combined_release_title([manager for _, manager, _, _ in records], platform=label)
            sections = ['# ' + label + ' 联合构建结果', '']
            for variant, _, notes, _ in records:
                sections += ['## ' + variant, '', '\n'.join('##' + line if line.startswith('#') else line
                            for line in notes.splitlines()), '']
            (ready / 'release.md').write_text('\n'.join(sections), encoding='utf-8', newline='\n')
            (ready / 'title.txt').write_text(title, encoding='utf-8', newline='\n')
        if resolution is not None and resolution['skipped_managers']:
            # Preserve the normal report/title and original checksums; add only
            # the explicit selection result to the human-readable notes.
            with (ready / 'release.md').open('a', encoding='utf-8', newline='\n') as stream:
                stream.write('\n\n## 本次跳过的管理器\n\n')
                for row in resolution['skipped_managers']:
                    stream.write('- ' + row['variant'] + '：' + row['reason'] + '。\n')
        if output.is_symlink() or output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError('Release output changed during verification; existing files are preserved')
        if output.exists():
            output.rmdir()  # Only a still-empty caller-provided output directory.
        ready.replace(output)
    return {'publish': publish, 'assets': len(merged), 'kernels': total_kernels, 'managers': list(options)}


def environment_inputs(selection: str, resolution=None) -> dict:
    result = {}
    for variant in active_managers(selection, resolution):
        prefix = 'MANAGER_' + SUFFIXES[variant] + '_'
        count, full = os.environ.get(prefix + 'EXPECTED_COUNT', ''), os.environ.get(prefix + 'FULL_CATALOG', '')
        if not re.fullmatch(r'0|[1-9][0-9]{0,5}', count) or full not in ('true', 'false'):
            raise ValueError('Missing or invalid prepared manager count/catalog: ' + variant)
        metadata = os.environ.get(prefix + 'METADATA', '')
        result[variant] = {'apk_filename': os.environ.get(prefix + 'APK_FILENAME', ''),
                           'metadata': metadata_json(metadata.encode()) if metadata else None,
                           'expected_count': int(count), 'full_catalog': full == 'true'}
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        selection = os.environ.get('MANAGER_SELECTION', '')
        platform = os.environ.get('RELEASE_PLATFORM', '')
        matrix = os.environ.get('OPLUS_MATRIX', '')
        resolution = os.environ.get('MANAGER_RESOLUTION') or None
        if resolution is not None:
            resolution = validate_receipt(resolution, selection=selection)
        if platform == 'oplus':
            from oplus_catalog import strict_json
            matrix = strict_json(matrix.encode('utf-8'))
        result = prepare(args.input, args.output, selection=selection, platform=platform,
                         operation=os.environ.get('OPERATION', ''), manager_inputs=environment_inputs(selection, resolution),
                         oplus_matrix=matrix if platform == 'oplus' else None, resolution=resolution)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
                for name in ('publish', 'assets', 'kernels'):
                    stream.write(name + '=' + str(result[name]).lower() + '\n')
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print('Selected release preparation failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
