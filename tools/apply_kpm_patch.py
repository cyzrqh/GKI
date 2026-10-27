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
PI_MAP_BEFORE = bytes.fromhex('E60316AAE7031F2A3411889A')
PI_MAP_AFTER = bytes.fromhex('E60316AAE7031F2AF40309AA')


class KPMValidationError(ValueError):
    """A public validation message constructed locally, never tool output."""


class KPMError(ValueError):
    """A fixed processing stage and a safe, locally constructed reason."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        self.public_message = message
        super().__init__(f'{stage}: {message}')


def checked(stage: str, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except KPMError:
        raise
    except KPMValidationError as error:
        raise KPMError(stage, str(error)) from None
    except OSError:
        raise KPMError(stage, 'File or network operation failed') from None
    except (ValueError, KeyError, TypeError, struct.error):
        raise KPMError(stage, 'Invalid input or metadata') from None


def image_entry(data: bytes) -> dict:
    try:
        return validate_image(data)
    except ValueError as error:
        # ARM64 validation only reports public sizes, offsets and instruction
        # words. Match the complete known formats before allowing that detail.
        formats = (
            r'ARM64 Image header is truncated: [0-9]+ bytes \(need 64\)',
            r'ARM64 Image exceeds the [0-9]+-byte size limit',
            r'Image has no ARM64 magic at header offset 56; expected a raw ARM64 Image',
            r'Big-endian ARM64 Image is unsupported; this build requires little endian',
            r'Unsupported ARM64 boot entry: code0=0x[0-9a-f]+, code1=0x[0-9a-f]+; expected B or EFI MZ ADD/CCMP/NOP followed by B',
            r'ARM64 entry branch target 0x-?[0-9a-f]+ is unaligned',
            r'ARM64 entry branch at 0x[0-9a-f]+ targets 0x-?[0-9a-f]+, before executable payload \(64-byte header\)',
            r'ARM64 entry branch at 0x[0-9a-f]+ targets 0x-?[0-9a-f]+ outside Image file \(size 0x[0-9a-f]+\); target needs four bytes',
            r'ARM64 entry branch targets empty/erased instruction 0x[0-9a-f]+ at 0x[0-9a-f]+',
        )
        message = str(error)
        if not any(re.fullmatch(pattern, message) for pattern in formats):
            message = 'ARM64 header or boot entry validation failed'
        raise KPMValidationError(message) from None


def tool_failure_class(*streams: bytes) -> str:
    # Never return any part of stdout/stderr. The fixed official tool can log
    # passwords; only locally written category names may leave this function.
    markers = (
        (b'kernel primary entry', 'kernel-entry'),
        (b'kernel image magic error', 'kernel-header'),
        (b"can't find linux banner", 'linux-banner'),
        (b'analyze_kallsym_info error', 'kernel-symbols'),
        (b'no usable symbol scan anchor', 'symbol-anchor'),
        (b'kpimg length mismatch', 'payload-size'),
        (b'get_kernel_info error', 'kernel-header'),
        (b'parse kernel image error', 'kernel-header'),
    )
    for stream in streams:
        if isinstance(stream, bytes):
            lowered = stream.lower()
            for marker, category in markers:
                if marker in lowered:
                    return category
    return 'unclassified'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_release() -> dict:
    raw = _read_regular(ROOT / 'config/kpm-release.json', 8192)
    doc = json.loads(raw)
    if (not isinstance(doc, dict) or set(doc) != {'schema_version', 'repository', 'tag', 'commit', 'assets'}
            or type(doc['schema_version']) is not int or doc['schema_version'] != 1
            or doc['repository'] != REPOSITORY or doc['tag'] != TAG or doc['commit'] != COMMIT
            or not isinstance(doc['assets'], dict) or set(doc['assets']) != {'kpimg', 'kptools-linux'}):
        raise KPMValidationError('Invalid fixed official KPM release lock')
    for name, item in doc['assets'].items():
        if (not isinstance(item, dict) or set(item) != {'size', 'sha256', 'url'}
                or type(item['size']) is not int or not 4096 < item['size'] <= 16 * 1024 * 1024
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
                or item['url'] != f'https://github.com/{REPOSITORY}/releases/download/{TAG}/{name}'):
            raise KPMValidationError('Invalid fixed official KPM asset lock')
    return doc


def _read_regular(path: Path, limit: int) -> bytes:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.is_symlink() or path.stat().st_size > limit:
        raise KPMValidationError('Expected a bounded regular file')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise KPMValidationError('File exceeds its size limit')
    return data


def _directory(path: Path) -> Path:
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise KPMValidationError('KPM working paths must not contain symlinks')
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise KPMValidationError('KPM working path must be a directory')
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
            raise KPMValidationError('KPM asset redirected outside its public HTTPS hosts')
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
            raise KPMValidationError('Unable to download the fixed official KPM asset') from None
        with response:
            if response.status != 200:
                raise KPMValidationError('Unexpected official KPM download response')
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) != size):
                raise KPMValidationError('Official KPM download has an unexpected size')
            data = response.read(size + 1)
        if len(data) != size:
            raise KPMValidationError('Official KPM download is truncated or oversized')
        return data
    raise KPMValidationError('Too many official KPM asset redirects')


def validate_payload(data: bytes) -> dict:
    if (len(data) < PAGE + 4 or data[:8] != MAGIC or data[8:12] != bytes((0, 9, 13, 0))
            or struct.unpack_from('<Q', data, 16)[0] != 2
            or not data[24:48].split(b'\0', 1)[0]
            or struct.unpack_from('<I', data, PAGE)[0] in (0, 0xffffffff)):
        raise KPMValidationError('KPM payload must be a real Android ARM64 0.13.9 payload, not a placeholder')
    return {'version': '0.13.9', 'flags': 2}


def validate_tool(data: bytes) -> None:
    if len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01':
        raise KPMValidationError('KPM tool is not a little-endian ELF64 executable')
    typ, machine, version = struct.unpack_from('<HHI', data, 16)
    entry, phoff = struct.unpack_from('<QQ', data, 24)
    ehsize, phsize, phnum = struct.unpack_from('<HHH', data, 52)
    if (typ not in (2, 3) or machine != 62 or version != 1 or ehsize != 64
            or phsize != 56 or not 0 < phnum <= 128 or phoff + phnum * phsize > len(data)):
        raise KPMValidationError('KPM tool has an invalid x86-64 executable header')
    executable = False
    for offset in range(phoff, phoff + phnum * phsize, phsize):
        kind, flags, pos, address, _, filesz, memsz, _ = struct.unpack_from('<IIQQQQQQ', data, offset)
        if kind == 1:
            if pos + filesz > len(data) or filesz > memsz:
                raise KPMValidationError('KPM tool has an invalid load segment')
            executable |= bool(flags & 1 and address <= entry < address + filesz)
    if not executable:
        raise KPMValidationError('KPM tool has no executable entry segment')


def _verified(data: bytes, name: str, release: dict) -> bytes:
    expected = release['assets'][name]
    if len(data) != expected['size'] or digest(data) != expected['sha256']:
        raise KPMValidationError(f'Fixed official KPM asset failed size/SHA256 verification: {name}')
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
            raise KPMValidationError('Invalid KPM cache path for GitHub environment')
        with Path(github_env).open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(f'KPM_CACHE_DIR={cache}\n')
    return cache


def verify_patched(before: bytes, after: bytes, payload: bytes) -> dict:
    original, patched = image_entry(before), image_entry(after)
    offset = (len(before) + PAGE - 1) // PAGE * PAGE
    end = offset + len(payload)
    if len(after) != end + EXTRA_SIZE:
        raise KPMValidationError('Patched KPM Image has an unexpected payload/extra extent')
    if (after[offset:offset + 64] != payload[:64]
            or after[offset + PAGE:end] != payload[PAGE:]
            or after[len(before):offset] != bytes(offset - len(before))
            or after[end:] != bytes(EXTRA_SIZE)):
        raise KPMValidationError('The complete fixed KPM payload was not embedded intact')
    if patched['branch_target'] != offset + PAGE or after[8:64] != before[8:64]:
        raise KPMValidationError('Patched KPM entry/header does not match its real payload')
    setup = offset + 64
    (kimg_size, kpimg_size, kernel_size, page_shift, setup_offset,
     start_offset, extra_size, map_offset, map_max_size) = struct.unpack_from('<9Q', after, setup + 8)
    expected_start = (max(original['image_size'], len(after)) + PAGE - 1) // PAGE * PAGE
    if (kimg_size != len(before) or kpimg_size != len(payload) or kernel_size != original['image_size']
            or setup_offset != offset or start_offset != expected_start or extra_size != EXTRA_SIZE
            or page_shift not in (12, 14, 16) or map_offset % 16 or not 0 < map_max_size <= 0xa00
            # Official symbol.c requires a page in the original file after
            # the usable anchor; floor-aligning that anchor cannot reduce it.
            # patch.c clamps its optional doubled sync window at EOF.
            or map_offset < 64 or map_offset + PAGE > len(before)
            or after[setup + 160:setup + 168] != before[:8]):
        raise KPMValidationError('Patched KPM preset does not describe the original Image and valid layout')
    return {'payload_offset': offset, 'payload_size': kpimg_size, 'extra_offset': end,
            'extra_size': extra_size, 'start_offset': start_offset,
            'map_offset': map_offset, 'map_max_size': map_max_size}


def _original_map_extent(size: int, layout: dict) -> tuple[int, int]:
    if not isinstance(layout, dict):
        raise KPMValidationError('KPM report has no verified map layout')
    offset, maximum = layout.get('map_offset'), layout.get('map_max_size')
    if (type(offset) is not int or type(maximum) is not int or offset < 64 or offset % 16
            or not 0 < maximum <= 0xa00 or offset + PAGE > size):
        raise KPMValidationError('KPM report has an invalid bounded map window')
    return offset, min(2 * maximum, size - offset)


def _check_map_changes(before: bytes, after: bytes) -> None:
    # Official 72bf620 tools/symbol.c changes PAC/AUT instructions to NOP in
    # the bounded map scratch window and tools/patch.c syncs that window.
    for offset in range(0, len(before), 4):
        old, new = before[offset:offset + 4], after[offset:offset + 4]
        if old == new:
            continue
        if (len(old) != 4 or len(new) != 4 or struct.unpack('<I', new)[0] != 0xd503201f
                or struct.unpack('<I', old)[0] & 0xfffffd1f != 0xd503211f):
            raise KPMValidationError('KPM changed an instruction outside its official PAC/NOP map transformation')


def capture_original_windows(before: bytes, after: bytes, layout: dict) -> list[dict]:
    """Retain public pre-patch bytes needed for independent full-image proof."""
    offset, length = _original_map_extent(len(before), layout)
    windows = []
    if before[offset:offset + length] != after[offset:offset + length]:
        _check_map_changes(before[offset:offset + length], after[offset:offset + length])
        windows.append({'offset': offset, 'before_hex': before[offset:offset + length].hex()})
    pi = before.find(PI_MAP_BEFORE)
    if pi >= 0 and after[pi:pi + len(PI_MAP_AFTER)] == PI_MAP_AFTER:
        windows.append({'offset': pi, 'before_hex': PI_MAP_BEFORE.hex()})
    windows.sort(key=lambda item: item['offset'])
    report = {'pre_image_size': len(before), 'pre_entry': image_entry(before),
              'layout': layout, 'original_windows': windows}
    restored = reconstruct_original_image(after, report)
    if restored != before:
        raise KPMValidationError('Official KPM tool changed kernel bytes outside its verified transformation windows')
    return windows


def reconstruct_original_image(image: bytes, report: dict) -> bytes:
    """Undo only measured official transformations; never skip the pre-image hash."""
    size, entry = report.get('pre_image_size'), report.get('pre_entry')
    if type(size) is not int or not 64 <= size < len(image) or not isinstance(entry, dict):
        raise KPMValidationError('KPM report lacks the bounded original Image extent and entry')
    map_offset, map_length = _original_map_extent(size, report.get('layout'))
    windows = report.get('original_windows', [])
    if not isinstance(windows, list) or len(windows) > 2:
        raise KPMValidationError('KPM original byte windows are missing or oversized')
    before = bytearray(image[:size])
    try:
        struct.pack_into('<II', before, 0, entry['code0'], entry['code1'])
    except (KeyError, TypeError, struct.error):
        raise KPMValidationError('KPM report has an invalid original ARM64 entry') from None
    previous_end, seen = 64, set()
    for window in windows:
        if (not isinstance(window, dict) or set(window) != {'offset', 'before_hex'}
                or type(window['offset']) is not int or not isinstance(window['before_hex'], str)
                or not re.fullmatch(r'(?:[0-9a-f]{2}){1,5120}', window['before_hex'])):
            raise KPMValidationError('Invalid original KPM byte window')
        start = window['offset']
        original = bytes.fromhex(window['before_hex'])
        end = start + len(original)
        if start < previous_end or end > size:
            raise KPMValidationError('Original KPM byte windows overlap or escape the Image')
        if start == map_offset and len(original) == map_length:
            if 'map' in seen:
                raise KPMValidationError('Duplicate original KPM map window')
            _check_map_changes(original, image[start:end])
            seen.add('map')
        elif original == PI_MAP_BEFORE and start % 4 == 0 and image[start:end] == PI_MAP_AFTER:
            if 'pi' in seen:
                raise KPMValidationError('Duplicate original KPM PI map window')
            seen.add('pi')
        else:
            raise KPMValidationError('Original KPM bytes do not describe an official transformation')
        before[start:end] = original
        previous_end = end
    if 'pi' in seen:
        banner = re.search(rb'Linux version ([0-9]+)\.([0-9]+)\.([0-9]+)', before)
        if not banner or tuple(map(int, banner.groups())) <= (6, 7, 0):
            raise KPMValidationError('PI map transformation is not applicable to this original kernel')
        if before.find(PI_MAP_BEFORE) != next(w['offset'] for w in windows if w['before_hex'] == PI_MAP_BEFORE.hex()):
            raise KPMValidationError('PI map transformation differs from the official first-match patch')
    return bytes(before)


def patch(image: Path, cache_dir: Path, report: Path | None = None) -> dict:
    release = checked('release-lock', load_release)
    # Bazel exposes its real build outputs through bazel-bin symlinks. Resolve
    # once, then validate and atomically replace that real output, never the link.
    image = checked('read-image', Path(image).resolve, strict=True)
    before = checked('read-image', _read_regular, image, MAX_IMAGE_SIZE)
    pre_entry = checked('image-entry', image_entry, before)
    if MAGIC in before:
        raise KPMError('image-entry', 'Image already contains KPM; use the original unpatched compilation output')
    cache = checked('verify-assets', _directory, cache_dir)
    assets = {name: checked('verify-assets', _verified,
                           checked('verify-assets', _read_regular, cache / name, spec['size']), name, release)
              for name, spec in release['assets'].items()}
    key = os.environ.get('KPM_PASSWORD') or '123'
    encoded_key = key.encode('utf-8')
    if not 0 < len(encoded_key) < 64 or any(value in encoded_key for value in (0, 10, 13)):
        raise KPMValidationError('KPM password must be 1..63 UTF-8 bytes without NUL/newlines')
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
        except subprocess.TimeoutExpired:
            raise KPMError('run-tool', 'Official KPM tool timed out after 180 seconds; original Image is unchanged') from None
        except (OSError, subprocess.SubprocessError):
            raise KPMError('run-tool', 'Official KPM tool could not finish; original Image is unchanged') from None
        # Tool output can contain a caller's password. Never print it or propagate it in errors.
        if completed.returncode != 0:
            code = completed.returncode if type(completed.returncode) is int else 'unknown'
            category = tool_failure_class(completed.stderr, completed.stdout)
            raise KPMError('run-tool', f'Official KPM tool failed (exit={code}, class={category}); original Image is unchanged')
        after = checked('read-output', _read_regular, output, MAX_IMAGE_SIZE)
        layout = checked('verify-layout', verify_patched, before, after, assets['kpimg'])
        result = {'schema_version': 1, 'pre_image_sha256': digest(before), 'post_image_sha256': digest(after),
                  'pre_image_size': len(before), 'post_image_size': len(after),
                  'release': {key: release[key] for key in ('repository', 'tag', 'commit')},
                  'tool': {key: release['assets']['kptools-linux'][key] for key in ('size', 'sha256')},
                  'payload': {**{key: release['assets']['kpimg'][key] for key in ('size', 'sha256')},
                              **validate_payload(assets['kpimg'])},
                  'pre_entry': pre_entry, 'post_entry': checked('output-entry', image_entry, after), 'layout': layout,
                  'original_windows': checked('verify-original-kernel', capture_original_windows, before, after, layout)}
        report_bytes = (json.dumps(result, indent=2, sort_keys=True) + '\n').encode()
        if report is not None:
            report = Path(report).absolute()
            if report == image or report.exists() or report.is_symlink():
                raise KPMValidationError('KPM report must be a new separate file')
            _directory(report.parent)
        if _read_regular(image, MAX_IMAGE_SIZE) != before:
            raise KPMValidationError('Original Image changed during KPM validation; refusing replacement')
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
            raise KPMError('publish-image', 'Unable to publish verified KPM Image; original Image is unchanged') from None
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
            checked('prepare-assets', prepare, args.cache_dir, args.github_env)
            print('Verified fixed official KPM tool and Android ARM64 payload are ready.')
        else:
            result = checked('patch-setup', patch, args.image, args.cache_dir, args.report)
            print(json.dumps(result, sort_keys=True))
        return 0
    except KPMError as error:
        print(f'KPM validation/patch failed [{error.stage}]: {error.public_message}. Refusing to publish an unverified Image.')
        return 1
    except (OSError, ValueError, KeyError, TypeError, struct.error, urllib.error.URLError):
        # Never expose external exception messages, URLs or subprocess output.
        print('KPM validation/patch failed [input]: invalid local data or file operation. Refusing to publish an unverified Image.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
