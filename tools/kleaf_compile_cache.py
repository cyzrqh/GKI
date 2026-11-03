#!/usr/bin/env python3
"""Prefix Kleaf's existing kernel compiler with the guarded object cache.

Keep the official toolchain, Make arguments, configuration and install steps.
Unknown Kleaf layouts retain the unmodified official build.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shlex

from persistent_kernel_cache import checked_root, is_link

MARKER = '# gki-guarded-kleaf-compiler-cache-v1'
MAKE = '{interceptor_command_prefix} make -C ${{KERNEL_DIR}} ${{TOOL_ARGS}} O=${{OUT_DIR}} '
MAKE_FRAGMENT = 'make -C ${{KERNEL_DIR}} ${{TOOL_ARGS}} O=${{OUT_DIR}} '
MAKE_COMMAND = re.compile(r'(?m)^(?P<indent>[ \t]*)(?P<interceptor>\{interceptor_command_prefix\} )?'
                          + re.escape(MAKE_FRAGMENT))
START = '# Actual kernel build\n'
END = '# Install modules\n'


def shell_path(path: Path) -> str:
    value = path.absolute().as_posix()
    if not re.fullmatch(r'[A-Za-z0-9_./:\-]+', value):
        raise ValueError('Unsupported kernel compiler cache path')
    return shlex.quote(value)


def launcher(program: Path, directory: Path) -> str:
    return ('#!/bin/bash\nset -euo pipefail\ncache_program=' + shell_path(program)
            + '\nexport CCACHE_DIR=' + shell_path(directory) + '\n' + r'''
# Preserve the exact compiler chosen by the official resolved toolchain.
# Make's final command-line assignment wins over earlier CC= arguments.
compiler="${CC:-clang}"
for argument in "$@"; do
  case "$argument" in CC=*) compiler="${argument#CC=}";; esac
done
if (set -C; : > "${cache_program}.kleaf-entered") 2>/dev/null; then
  cache_ready=false; compiler_ready=false; config_ready=false
  if [ -x "$cache_program" ]; then cache_ready=true; fi
  if command -v -- "${compiler%% *}" >/dev/null 2>&1; then compiler_ready=true; fi
  if [ -n "${OUT_DIR:-}" ] && [ -f "$OUT_DIR/include/generated/autoconf.h" ]; then config_ready=true; fi
  printf 'Kleaf cache launcher executed: cache_ready=%s compiler_ready=%s config_ready=%s\n' \
    "$cache_ready" "$compiler_ready" "$config_ready"
fi
export CCACHE_BASEDIR="${ROOT_DIR:-$PWD}"
exec make "$@" "CC=$cache_program $compiler"
''')


def configure(source: Path, program: Path, output: Path, directory: Path) -> bool:
    source = checked_root(source)
    target = source / 'build/kernel/kleaf/impl/kernel_build.bzl'
    if is_link(target) or not target.is_file() or target.stat().st_size > 512 * 1024:
        return False
    text = target.read_text(encoding='utf-8')
    if text.count(START) != 1 or END not in text.split(START, 1)[1]:
        return False
    prefix, rest = text.split(START, 1)
    block, suffix = rest.split(END, 1)
    command = shell_path(output) + ' -C ${{KERNEL_DIR}} ${{TOOL_ARGS}} O=${{OUT_DIR}} '
    if MARKER in text:
        if (text.count(MARKER) != 1 or MAKE_COMMAND.search(block)
                or not 1 <= block.count(command) <= 4
                or block.count(MAKE_FRAGMENT) != block.count(command)):
            return False
    else:
        matches = list(MAKE_COMMAND.finditer(block))
        if not 1 <= len(matches) <= 4 or block.count(MAKE_FRAGMENT) != len(matches):
            return False
        # New Kleaf removed the interceptor prefix. Preserve either reviewed
        # native command's exact arguments and optional interceptor unchanged.
        block = '         ' + MARKER + '\n' + MAKE_COMMAND.sub(
            lambda match: match['indent'] + (match['interceptor'] or '') + command, block)
    checked_root(output.absolute().parent, create=True)
    checked_root(directory, create=True)
    if is_link(output) or output.exists() and not output.is_file():
        raise ValueError('Kernel compiler launcher must be a regular file')
    output.write_text(launcher(program, directory), encoding='utf-8', newline='\n')
    if os.name != 'nt':
        output.chmod(0o700)
    replaced = prefix + START + block + END + suffix
    if replaced != text:
        target.write_text(replaced, encoding='utf-8', newline='\n')
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--program', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    enabled = False
    try:
        enabled = configure(args.source, args.program, args.output, args.directory)
    except (OSError, ValueError):
        pass
    if destination := os.environ.get('GITHUB_OUTPUT'):
        with Path(destination).open('a', encoding='utf-8') as stream:
            stream.write('enabled=' + str(enabled).lower() + '\n')
    print('Kleaf 逐文件编译缓存：' + ('已接入，保留官方编译器与配置。' if enabled
          else '当前布局未接入，继续使用官方编译及 Bazel 磁盘缓存。'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
