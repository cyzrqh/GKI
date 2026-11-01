#!/usr/bin/env python3
"""Record release configuration from the built kernel, rather than menu inputs."""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from validate_susfs_hooks import inspect_c_source
from validate_arm64_image import validate_image


def parse_config(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'(CONFIG_[A-Z0-9_]+)=(.*)', line)
        if match:
            result[match[1]] = match[2]
        else:
            match = re.fullmatch(r'# (CONFIG_[A-Z0-9_]+) is not set', line)
            if match:
                result[match[1]] = 'n'
    if result.get('CONFIG_ARM64') != 'y':
        raise ValueError('The built kernel configuration is not ARM64')
    return result


def kernel_config(root: Path, image: bytes) -> tuple[dict[str, str], str, str]:
    start = image.find(b'IKCFG_ST')
    if start >= 0:
        end = image.find(b'IKCFG_ED', start + 8)
        if end >= 0:
            try:
                text = gzip.decompress(image[start + 8:end]).decode('utf-8')
                return parse_config(text), 'Image / IKCONFIG', hashlib.sha256(text.encode()).hexdigest()
            except (OSError, EOFError, UnicodeError, ValueError) as error:
                raise ValueError('The Image contains invalid IKCONFIG data') from error
    raise ValueError('Image is missing IKCONFIG; refusing to use an unbound or requested configuration')


def state(config: dict[str, str], *names: str):
    values = [config.get('CONFIG_' + name, 'n') for name in names]
    if 'y' in values:
        return True
    if 'm' in values:
        return '模块'
    return False


def collect(kernel_root: Path, image_path: Path, context: dict[str, str]) -> dict:
    if image_path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError('Unexpectedly large kernel Image')
    image = image_path.read_bytes()
    config, origin, config_sha = kernel_config(kernel_root, image)
    from verify_requested_features import requests_from_environment, verify_requested
    verify_requested(config, requests_from_environment())
    entry = validate_image(image)
    susfs = config.get('CONFIG_KSU_SUSFS') == 'y'
    variant = context.get('ksu_variant', 'None')
    ksu = config.get('CONFIG_KSU') == 'y'
    if ksu != (variant != 'None'):
        raise ValueError('The compiled KernelSU state differs from the selected variant')
    if ksu:
        result = subprocess.run(['git', '-C', str(kernel_root / 'KernelSU'), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=15)
        if result.returncode or result.stdout.strip().lower() != context.get('ksu_ref', '').lower():
            raise ValueError('The actual KernelSU checkout differs from the shared manager/kernel source')
    if context.get('enable_susfs') == 'true' and variant != 'None' and not susfs:
        raise ValueError('SUSFS was requested but is not enabled in the built kernel')
    version = None
    if susfs:
        header = kernel_root / 'common/include/linux/susfs.h'
        text = header.read_text(encoding='utf-8')
        found = re.findall(r'^\s*#define\s+SUSFS_VERSION\s+"(v[0-9]+\.[0-9]+\.[0-9]+)"', text, re.M)
        if len(found) != 1 or found[0].encode() not in image:
            raise ValueError('Cannot verify the compiled SUSFS version in the Image')
        version = found[0]
        expected = context.get('expected_susfs_version')
        if expected and expected != version:
            raise ValueError('The compiled SUSFS version differs from the selected compatibility profile')
    lto = ('thin' if state(config, 'LTO_CLANG_THIN') is True else
           'full' if state(config, 'LTO_CLANG_FULL') is True else
           'none' if state(config, 'LTO_NONE') is True else None)
    inline_hooks = False
    if susfs:
        for relative in ('common/fs/exec.c', 'common/security/selinux/selinuxfs.c'):
            source = kernel_root / relative
            if source.is_file():
                calls, _ = inspect_c_source(source, source.read_text(encoding='utf-8'))
                inline_hooks |= bool({symbol for symbol, _ in calls} &
                                     {'ksu_setup_preinit_file', 'ksu_preinit_handle_post_execveat',
                                      'my_setprocattr', 'my_write_context', 'my_write_access'})
    has_backend_symbols = any(key.startswith('CONFIG_ZRAM_BACKEND_') for key in config)
    algorithms = ', '.join(name for name in ('LZO', 'LZ4', 'LZ4HC', 'LZ4KD', 'ZSTD', 'ZSTDP', 'DEFLATE', '842')
                           if state(config, ('ZRAM_BACKEND_' if has_backend_symbols else 'CRYPTO_') + name) is True)
    features = {
        'susfs': susfs,
        'hook': 'SUSFS Inline Hooks' if inline_hooks else ('无' if variant == 'None' else None),
        'mount': '由管理器/元模块在设备上设置' if variant == 'SukiSU' else None,
        'lto': lto, 'bbg': state(config, 'BBG'), 'zram': state(config, 'ZRAM'),
        'lz4kd': bool(state(config, 'ZRAM')) and state(config, 'ZRAM_BACKEND_LZ4KD' if has_backend_symbols else 'CRYPTO_LZ4KD'),
        'kpm': state(config, 'KPM'), 'ntsync': state(config, 'NTSYNC'),
        'rekernel': state(config, 'REKERNEL'), 'virtualization': state(config, 'KVM'),
        'ddk': state(config, 'XINGGUANG_DDK', 'DDK'),
        'TCP 拥塞算法': ', '.join(name for name in ('BBR', 'CUBIC', 'BIC', 'WESTWOOD', 'HTCP')
                             if state(config, 'TCP_CONG_' + name) is True) or '无',
        'ZRAM 压缩算法': (algorithms or '无') if state(config, 'ZRAM') else '无',
    }
    filename = context['file']
    if Path(filename).name != filename or not re.fullmatch(r'[A-Za-z0-9_.+-]+\.zip', filename):
        raise ValueError('Invalid release kernel filename')
    return {key: context.get(key) for key in ('android_version', 'kernel_version', 'sub_level', 'os_patch_level',
                                             'ksu_variant', 'ksu_branch', 'ksu_ref', 'ksu_version_code', 'ksu_version_name')} | {
        'file': filename, 'susfs_version': version, 'features': features,
        'built_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'image_sha256': hashlib.sha256(image).hexdigest(), 'config_sha256': config_sha,
        'config_source': origin,
        'image_entry': entry,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path, required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    names = ('file', 'android_version', 'kernel_version', 'sub_level', 'os_patch_level',
             'ksu_variant', 'ksu_branch', 'ksu_ref', 'ksu_version_code', 'ksu_version_name', 'enable_susfs', 'expected_susfs_version')
    context = {key: os.environ.get('META_' + key.upper(), '') for key in names}
    try:
        metadata = collect(args.kernel_root, args.image, context)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except (OSError, ValueError) as error:
        print(f'Build metadata capture failed: {error}', file=sys.stderr)
        return 1
    print(f'Actual kernel configuration recorded: {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
