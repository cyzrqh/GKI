"""Set bounded Gradle resources for the actual runner without changing APK features."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re


def memory_limit() -> int:
    limits = []
    memory = Path('/proc/meminfo')
    if memory.is_file():
        match = re.search(r'^MemTotal:\s+(\d+) kB$', memory.read_text(), re.M)
        if match:
            limits.append(int(match[1]) * 1024)
    for name in ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
        path = Path(name)
        if path.is_file():
            value = path.read_text().strip()
            if value.isdecimal() and int(value) > 0:
                limits.append(int(value))
    return min(limits) if limits else 4 * 1024 ** 3


def configure(source: Path, memory: int, cpus: int) -> None:
    # Leave at least half the runner for Kotlin/native tools, the OS and file IO.
    heap = min(6144, max(512, memory // (2 * 1024 ** 2)))
    workers = max(1, min(8, cpus, memory // (1024 ** 3)))
    for relative in ('manager/gradle.properties', 'userspace/sui/gradle.properties'):
        path = source / relative
        if not path.is_file():
            continue
        if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (path, *path.parents)):
            raise ValueError('Gradle settings must not traverse links')
        text = path.read_text(encoding='utf-8')
        original = re.findall(r'^org\.gradle\.jvmargs=(.*)$', text, re.M)
        if len(original) > 1:
            raise ValueError('Review duplicate Gradle JVM settings')
        arguments = original[0] if original else ''
        if re.search(r'(?<!\S)-Xmx\S+', arguments):
            arguments = re.sub(r'(?<!\S)-Xmx\S+', '-Xmx' + str(heap) + 'm', arguments, count=1)
        else:
            arguments = '-Xmx' + str(heap) + 'm ' + arguments
        values = {'org.gradle.jvmargs': arguments.strip(), 'org.gradle.caching': 'true',
                  'org.gradle.workers.max': str(workers), 'org.gradle.daemon': 'false'}
        for key, value in values.items():
            pattern = r'^' + re.escape(key) + r'=.*$'
            if len(re.findall(pattern, text, re.M)) > 1:
                raise ValueError('Review duplicate Gradle resource settings')
            if re.search(pattern, text, re.M):
                text = re.sub(pattern, lambda _: key + '=' + value, text, flags=re.M)
            else:
                text = text.rstrip() + '\n' + key + '=' + value + '\n'
        path.write_text(text, encoding='utf-8', newline='\n')
        print(f'Gradle resources ({relative}): heap {heap} MiB, workers {workers}, single-use daemon, task cache enabled.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    configure(args.source.resolve(strict=True), memory_limit(), os.cpu_count() or 1)
