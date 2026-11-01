#!/usr/bin/env python3
"""Run the unchanged official command with resource/activity heartbeats."""
from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import subprocess
import time


def activity(group: int, proc_root: Path = Path('/proc')) -> str:
    counts = Counter()
    try:
        for directory in proc_root.iterdir():
            if not directory.name.isdigit():
                continue
            try:
                fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) != group:
                    continue
                arguments = (directory / 'cmdline').read_bytes().split(b'\0')
                program = Path(os.fsdecode(arguments[0])).name
                if program.startswith(('clang', 'gcc', 'cc1')):
                    counts['编译'] += 1
                elif program in ('ld.lld', 'ld', 'lld'):
                    counts['LTO/链接'] += 1
                elif program == 'pahole':
                    counts['BTF'] += 1
                elif program in ('git', 'git-remote-https', 'curl'):
                    counts['源码下载/检出'] += 1
                elif program.startswith('ccache'):
                    counts['编译缓存'] += 1
            except (OSError, ValueError, IndexError):
                continue
        memory = {}
        for line in (proc_root / 'meminfo').read_text().splitlines():
            fields = line.split()
            if fields[0] in ('MemAvailable:', 'SwapFree:'):
                memory[fields[0]] = int(fields[1])
    except (OSError, ValueError):
        memory = {}
    description = '、'.join(name + ' ' + str(count) + ' 个进程' for name, count in counts.items())
    if not description:
        description = '官方构建命令仍在运行'
    if 'MemAvailable:' in memory:
        description += f"；可用内存 {memory['MemAvailable:'] / (1024 * 1024):.1f} GiB"
    return description


def run(command: list[str], label: str, *, interval: float = 30) -> int:
    started = time.monotonic()
    # Keep the runner's process group so a cancelled/timed-out Actions step
    # also stops Make, its compiler children and the foreground Bazel client.
    process = subprocess.Popen(command)
    group = os.getpgid(process.pid) if os.name != 'nt' else process.pid
    try:
        while True:
            try:
                result = process.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                print(f'构建进度：{label}已运行 {time.monotonic() - started:.0f} 秒；{activity(group)}。', flush=True)
    except BaseException:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    seconds = time.monotonic() - started
    print(f'构建计时：{label} {seconds:.1f} 秒，退出码 {result}。', flush=True)
    if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(summary).open('a', encoding='utf-8') as stream:
            stream.write(f'\n{label}：**{seconds:.1f} 秒**（退出码 {result}）。\n')
    return result if result >= 0 else 128 - result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', choices=('官方源码同步', 'build.sh 内核编译', 'Bazel 内核编译'), required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('An official build command is required')
    return run(command, args.label)


if __name__ == '__main__':
    raise SystemExit(main())
