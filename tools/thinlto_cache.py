#!/usr/bin/env python3
"""Retain LLVM's own content-addressed link cache outside disposable kernel output."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import time

from persistent_kernel_cache import checked_root, is_link

LIMIT = 1536 * 1024 * 1024
CACHE_FILE = re.compile(r'llvmcache-[A-Za-z0-9_-]{16,128}\Z')
FLAG = re.compile(r'(?m)^([ \t]*KBUILD_LDFLAGS[ \t]*\+=[^\r\n]*?)--thinlto-cache-dir=([^ \t\r\n]+)')
PATHS = {'.thinlto-cache', '$(extmod-prefix).thinlto-cache', '$(extmod_prefix).thinlto-cache',
         '$(GKI_THINLTO_CACHE_DIR)'}


def configure(source: Path, directory: Path) -> bool:
    source = checked_root(source)
    directory = checked_root(directory, create=True)
    if directory == source or directory.is_relative_to(source):
        raise ValueError('The persistent link cache must stay outside the cleaned kernel source/output')
    makefile = source / 'Makefile'
    if is_link(makefile) or not makefile.is_file():
        raise ValueError('Expected the real kernel Makefile')
    text = makefile.read_text(encoding='utf-8')
    matches = list(FLAG.finditer(text))
    if len(matches) != 1 or matches[0][2] not in PATHS:
        return False  # An unfamiliar Kbuild keeps its original linker arguments.
    replaced = FLAG.sub(lambda m: m[1] + '--thinlto-cache-dir=$(GKI_THINLTO_CACHE_DIR)', text)
    if replaced != text:
        makefile.write_text(replaced, encoding='utf-8', newline='\n')
    return True


def entries(directory: Path):
    directory = checked_root(directory, create=True)
    result = []
    for item in directory.iterdir():
        if is_link(item) or not item.is_file():
            raise ValueError('The link cache must contain only regular LLVM cache files')
        if item.name == 'llvmcache.timestamp':
            continue
        if not CACHE_FILE.fullmatch(item.name):
            raise ValueError('Unexpected file in the dedicated LLVM link cache')
        result.append((item, item.stat()))
    return result


def measure(directory: Path, maximum: int | None = None) -> tuple[int, int, int]:
    files = entries(directory)
    size = sum(info.st_size for _, info in files)
    removed = 0
    if maximum is not None:
        if maximum < 1:
            raise ValueError('Invalid link-cache size limit')
        for path, info in sorted(files, key=lambda row: (row[1].st_atime_ns, row[1].st_mtime_ns, row[0].name)):
            if size <= maximum:
                break
            path.unlink()
            size -= info.st_size
            removed += 1
    return len(files) - removed, size, removed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('configure', 'report', 'trim'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        if args.operation == 'configure':
            if args.source is None:
                parser.error('configure requires --source')
            enabled = configure(args.source, args.directory)
            if output := os.environ.get('GITHUB_OUTPUT'):
                with Path(output).open('a', encoding='utf-8') as stream:
                    stream.write('enabled=' + str(enabled).lower() + '\n')
            print('ThinLTO link-cache binding: ' + ('enabled' if enabled else 'unsupported Kbuild; unchanged'))
            if not enabled:
                print('当前官方 Kbuild 没有可绑定的原生链接缓存参数；保留官方链接方式，不强行重新启用已移除的缓存功能。')
            return 0
        count, size, removed = measure(args.directory, LIMIT if args.operation == 'trim' else None)
        message = f'ThinLTO 链接缓存（{args.operation}）：{count} 个对象，{size / (1024 * 1024):.1f} MiB，清理 {removed} 个对象。'
        print(message)
        if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
            with Path(summary).open('a', encoding='utf-8') as stream:
                stream.write('\n' + message + f' 耗时 {time.monotonic() - started:.1f} 秒。\n')
        return 0
    except (OSError, ValueError):
        print('::warning::ThinLTO cache unavailable; keeping the normal kernel build.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
