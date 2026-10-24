#!/usr/bin/env python3
"""Apply a verified official KPM payload without its unsafe embedded wrapper."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from validate_arm64_image import MAX_IMAGE_SIZE, validate_image


ROOT = Path(__file__).resolve().parents[1]
MAGIC = b'KP1158\0\0'
REPOSITORY = 'SukiSU-Ultra/SukiSU_KernelPatch_patch'
COMMIT = '72bf62025e9c72e07b64c77db0801891f1088429'
TAG = '0.13.9'
PAGE = 4096
EXTRA_SIZE = 128


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_release() -> dict:
    raw = _read_regular(ROOT / 'config/kpm-release.json', 8192)
    doc = json.loads(raw)
    if (not isinstance(doc, dict) or set(doc) != {'schema_version', 'repository', 'tag', 'commit', 'assets'}
            or type(doc['schema_version']) is not int or doc['schema_version'] != 1
            or doc['repository'] != REPOSITORY or doc['tag'] != TAG or doc['commit'] != COMMIT
            or not isinstance(doc['assets'], dict) or set(doc['assets']) != {'kpimg', 'kptools-linux'}):
        raise ValueError('Invalid fixed official KPM release lock')
    for name, item in doc['assets'].items():
        if (not isinstance(item, dict) or set(item) != {'size', 'sha256', 'url'}
                or type(item['size']) is not int or not 4096 < item['size'] <= 16 * 1024 * 1024
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
                or item['url'] != f'https://github.com/{REPOSITORY}/releases/download/{TAG}/{name}'):
            raise ValueError('Invalid fixed official KPM asset lock')
    return doc


def _read_regular(path: Path, limit: int) -> bytes:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.is_symlink() or path.stat().st_size > limit:
        raise ValueError('Expected a bounded regular file')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('File exceeds its size limit')
    return data


def _directory(path: Path) -> Path:
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('KPM working paths must not contain symlinks')
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError('KPM working path must be a directory')
    return path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(url: str, size: int) -> bytes:
    """Public assets only: no credentials, bounded body, explicit HTTPS redirects."""
    opener = urllib.request.build_opener(_NoRedirect())
    for _ in range(4):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment
                or parsed.hostname not in {'github.com', 'release-assets.githubusercontent.com',
                                           'objects.githubusercontent.com'}):
            raise ValueError('KPM asset redirected outside its public HTTPS hosts')
        request = urllib.request.Request(url, headers={'User-Agent': 'gki-official-kpm',
                                                       'Accept': 'application/octet-stream'})
        try:
            response = opener.open(request, timeout=45)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308) and error.headers.get('Location'):
                next_url = urllib.parse.urljoin(url, error.headers['Location'])
                error.close()
                url = next_url
                continue
            error.close()
            raise ValueError('Unable to download the fixed official KPM asset') from None
        with response:
            if response.status != 200:
                raise ValueError('Unexpected official KPM download response')
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) != size):
                raise ValueError('Official KPM download has an unexpected size')
            data = response.read(size + 1)
        if len(data) != size:
            raise ValueError('Official KPM download is truncated or oversized')
        return data
    raise ValueError('Too many official KPM asset redirects')


def validate_payload(data: bytes) -> dict:
    if (len(data) < PAGE + 4 or data[:8] != MAGIC or data[8:12] != bytes((0, 9, 13, 0))
            or struct.unpack_from('<Q', data, 16)[0] != 2
            or not data[24:48].split(b'\0', 1)[0]
            or struct.unpack_from('<I', data, PAGE)[0] in (0, 0xffffffff)):
        raise ValueError('KPM payload must be a real Android ARM64 0.13.9 payload, not a placeholder')
    return {'version': '0.13.9', 'flags': 2}


def validate_tool(data: bytes) -> None:
    if len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01':
        raise ValueError('KPM tool is not a little-endian ELF64 executable')
    typ, machine, version = struct.unpack_from('<HHI', data, 16)
    entry, phoff = struct.unpack_from('<QQ', data, 24)
    ehsize, phsize, phnum = struct.unpack_from('<HHH', data, 52)
    if (typ not in (2, 3) or machine != 62 or version != 1 or ehsize != 64
            or phsize != 56 or not 0 < phnum <= 128 or phoff + phnum * phsize > len(data)):
        raise ValueError('KPM tool has an invalid x86-64 executable header')
    executable = False
    for offset in range(phoff, phoff + phnum * phsize, phsize):
        kind, flags, pos, address, _, filesz, memsz, _ = struct.unpack_from('<IIQQQQQQ', data, offset)
        if kind == 1:
            if pos + filesz > len(data) or filesz > memsz:
                raise ValueError('KPM tool has an invalid load segment')
            executable |= bool(flags & 1 and address <= entry < address + filesz)
    if not executable:
        raise ValueError('KPM tool has no executable entry segment')


def _verified(data: bytes, name: str, release: dict) -> bytes:
    expected = release['assets'][name]
    if len(data) != expected['size'] or digest(data) != expected['sha256']:
        raise ValueError(f'Fixed official KPM asset failed size/SHA256 verification: {name}')
    (validate_payload if name == 'kpimg' else validate_tool)(data)
    return data


def prepare(cache_dir: Path, github_env: Path | None = None) -> Path:
    release = load_release()
    cache = _directory(cache_dir)
    for name, spec in release['assets'].items():
        destination = cache / name
        if destination.exists() or destination.is_symlink():
            _verified(_read_regular(destination, spec['size']), name, release)
        else:
            data = _verified(download(spec['url'], spec['size']), name, release)
            temporary = cache / ('.' + name + '.tmp-' + os.urandom(8).hex())
            try:
                with temporary.open('xb') as stream:
                    stream.write(data)
                # Never replace a pre-existing cache asset, even during a race.
                os.link(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        if name == 'kptools-linux':
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
    if github_env is not None:
        if '\n' in str(cache) or '\r' in str(cache):
            raise ValueError('Invalid KPM cache path for GitHub environment')
        with Path(github_env).open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(f'KPM_CACHE_DIR={cache}\n')
    return cache


def verify_patched(before: bytes, after: bytes, payload: bytes) -> dict:
    original, patched = validate_image(before), validate_image(after)
    offset = (len(before) + PAGE - 1) // PAGE * PAGE
    end = offset + len(payload)
    if len(after) != end + EXTRA_SIZE:
        raise ValueError('Patched KPM Image has an unexpected payload/extra extent')
    if (after[offset:offset + 64] != payload[:64]
            or after[offset + PAGE:end] != payload[PAGE:]
            or after[len(before):offset] != bytes(offset - len(before))
            or after[end:] != bytes(EXTRA_SIZE)):
        raise ValueError('The complete fixed KPM payload was not embedded intact')
    if patched['branch_target'] != offset + PAGE or after[8:64] != before[8:64]:
        raise ValueError('Patched KPM entry/header does not match its real payload')
    setup = offset + 64
    (kimg_size, kpimg_size, kernel_size, page_shift, setup_offset,
     start_offset, extra_size, map_offset, map_max_size) = struct.unpack_from('<9Q', after, setup + 8)
    expected_start = (max(original['image_size'], len(after)) + PAGE - 1) // PAGE * PAGE
    if (kimg_size != len(before) or kpimg_size != len(payload) or kernel_size != original['image_size']
            or setup_offset != offset or start_offset != expected_start or extra_size != EXTRA_SIZE
            or page_shift not in (12, 14, 16) or map_offset % 16 or not 0 < map_max_size <= 0xa00
            or map_offset < 64 or map_offset + map_max_size * 2 > len(before)
            or after[setup + 160:setup + 168] != before[:8]):
        raise ValueError('Patched KPM preset does not describe the original Image and valid layout')
    return {'payload_offset': offset, 'payload_size': kpimg_size, 'extra_offset': end,
            'extra_size': extra_size, 'start_offset': start_offset,
            'map_offset': map_offset, 'map_max_size': map_max_size}


def patch(image: Path, cache_dir: Path, report: Path | None = None) -> dict:
    release = load_release()
    # Bazel exposes its real build outputs through bazel-bin symlinks. Resolve
    # once, then validate and atomically replace that real output, never the link.
    image = Path(image).resolve(strict=True)
    before = _read_regular(image, MAX_IMAGE_SIZE)
    pre_entry = validate_image(before)
    if MAGIC in before:
        raise ValueError('Image already contains KPM; use the original unpatched compilation output')
    cache = _directory(cache_dir)
    assets = {name: _verified(_read_regular(cache / name, spec['size']), name, release)
              for name, spec in release['assets'].items()}
    key = os.environ.get('KPM_PASSWORD') or '123'
    encoded_key = key.encode('utf-8')
    if not 0 < len(encoded_key) < 64 or any(value in encoded_key for value in (0, 10, 13)):
        raise ValueError('KPM password must be 1..63 UTF-8 bytes without NUL/newlines')
    # mkdir inherits the runner ACL on Windows, unlike tempfile's 0700 directory.
    temporary = image.parent / ('.kpm-patch-' + os.urandom(8).hex())
    temporary.mkdir()
    try:
        source = temporary / 'Image'
        source.write_bytes(before)
        payload = temporary / 'kpimg'
        payload.write_bytes(assets['kpimg'])
        executable = temporary / 'kptools-linux'
        executable.write_bytes(assets['kptools-linux'])
        executable.chmod(0o700)
        output = temporary / 'oImage'
        try:
            completed = subprocess.run([str(executable), '-p', '-i', str(source), '-k', str(payload),
                                        '-o', str(output), '--skey-fd', '0'],
                                       input=encoded_key + b'\n', stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=180, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ValueError('Official KPM tool could not finish; original Image is unchanged') from None
        # Tool output can contain a caller's password. Never print it or propagate it in errors.
        if completed.returncode != 0:
            raise ValueError('Official KPM tool failed; original Image is unchanged')
        after = _read_regular(output, MAX_IMAGE_SIZE)
        layout = verify_patched(before, after, assets['kpimg'])
        result = {'schema_version': 1, 'pre_image_sha256': digest(before), 'post_image_sha256': digest(after),
                  'pre_image_size': len(before), 'post_image_size': len(after),
                  'release': {key: release[key] for key in ('repository', 'tag', 'commit')},
                  'tool': {key: release['assets']['kptools-linux'][key] for key in ('size', 'sha256')},
                  'payload': {**{key: release['assets']['kpimg'][key] for key in ('size', 'sha256')},
                              **validate_payload(assets['kpimg'])},
                  'pre_entry': pre_entry, 'post_entry': validate_image(after), 'layout': layout}
        report_bytes = (json.dumps(result, indent=2, sort_keys=True) + '\n').encode()
        if report is not None:
            report = Path(report).absolute()
            if report == image or report.exists() or report.is_symlink():
                raise ValueError('KPM report must be a new separate file')
            _directory(report.parent)
        if _read_regular(image, MAX_IMAGE_SIZE) != before:
            raise ValueError('Original Image changed during KPM validation; refusing replacement')
        output.chmod(stat.S_IMODE(image.stat().st_mode))
        # Publish a complete report first. If replacement fails, remove that report.
        written_report = False
        try:
            if report is not None:
                with report.open('xb') as stream:
                    written_report = True
                    stream.write(report_bytes)
            os.replace(output, image)
        except OSError:
            if written_report:
                report.unlink(missing_ok=True)
            raise ValueError('Unable to publish verified KPM Image; original Image is unchanged') from None
        return result
    finally:
        # Cleanup failure must not turn a successfully replaced Image into a
        # reported patch failure, or mask an earlier validation exception.
        shutil.rmtree(temporary, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    ready = sub.add_parser('prepare')
    ready.add_argument('--cache-dir', required=True, type=Path)
    ready.add_argument('--github-env', type=Path)
    apply = sub.add_parser('patch')
    apply.add_argument('--image', required=True, type=Path)
    apply.add_argument('--cache-dir', required=True, type=Path)
    apply.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            prepare(args.cache_dir, args.github_env)
            print('Verified fixed official KPM tool and Android ARM64 payload are ready.')
        else:
            result = patch(args.image, args.cache_dir, args.report)
            print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, struct.error, urllib.error.URLError):
        # No subprocess output, URLs, environment values or password-derived fields.
        print('KPM validation/patch failed; refusing to publish an unverified Image.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
