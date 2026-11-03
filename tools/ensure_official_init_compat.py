#!/usr/bin/env python3
"""Restore the real KernelSU resolver header omitted by the SUSFS hook port.

The port preserves upstream's resolver initialization call and its provider.
Only the removed include needs restoring: never replace initialization with
an extern, a no-op, or a different symbol lookup algorithm.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re

from ensure_susfs_headers import insert_after_includes, without_comments
from validate_susfs_hooks import inspect_c_source, strip_comments_and_literals


SYMBOL = 'ksu_init_symbol_resolver'
HEADER = '#include "infra/symbol_resolver.h"'
SIGNATURE = r'void\s+(?:__init\s+)?' + SYMBOL + r'\s*\(\s*(?:void\s*)?\)'


def _read(root: Path, relative: str) -> str:
    path = root / relative
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 262144
            or not path.resolve().is_relative_to(root)):
        raise ValueError('Missing or unsupported native KernelSU resolver input: ' + str(path))
    return path.read_text(encoding='utf-8')


def _unconditional_include(text: str) -> bool:
    depth = 0
    for line in without_comments(text).splitlines():
        directive = re.match(r'^\s*#\s*(\w+)\b', line)
        if not directive:
            continue
        name = directive[1]
        if name in ('if', 'ifdef', 'ifndef'):
            depth += 1
        elif name == 'endif':
            depth -= 1
        elif (name == 'include' and depth == 0
              and re.fullmatch(r'\s*#\s*include\s*"infra/symbol_resolver\.h"\s*', line)):
            return True
        if depth < 0:
            raise ValueError('Unbalanced KernelSU init preprocessor conditionals')
    return False


def _validate_provider(header: str, provider: str, build: str) -> None:
    declaration = re.compile(r'(?m)^\s*(?:extern\s+)?' + SIGNATURE + r'\s*;')
    if len(declaration.findall(strip_comments_and_literals(header))) != 1:
        raise ValueError('Native KernelSU resolver header must declare the real void initializer')
    code = strip_comments_and_literals(provider)
    definitions = list(re.finditer(r'(?m)^[ \t]*' + SIGNATURE + r'\s*\{', code))
    if len(definitions) != 1:
        raise ValueError('Native KernelSU resolver requires one global initializer implementation')
    start = definitions[0].end()
    depth = 1
    end = start
    for end in range(start, len(code)):
        depth += (code[end] == '{') - (code[end] == '}')
        if depth == 0:
            break
    if depth:
        raise ValueError('Native KernelSU resolver initializer body is incomplete')
    body = code[start:end]
    # Actual upstream initializers populate the version-specific kallsyms
    # callbacks. A declaration or empty substitute cannot satisfy this check.
    if not re.search(r'\bkallsyms_on_each_(?:match_)?symbol_fn\s*=\s*find_kernel_symbol_exact\s*\(', body):
        raise ValueError('Native KernelSU resolver initializer must retain its real callback setup')
    depth = 0
    rules = []
    for raw in build.splitlines():
        line = raw.split('#', 1)[0].strip()
        if re.match(r'(?:ifeq|ifneq|ifdef|ifndef)\b', line):
            depth += 1
        elif re.match(r'endif\b', line):
            depth -= 1
        elif re.fullmatch(r'kernelsu-objs\s*\+=\s*infra/symbol_resolver\.o', line):
            rules.append(depth)
        if depth < 0:
            raise ValueError('Unbalanced KernelSU Kbuild conditionals')
    if depth or rules != [0]:
        raise ValueError('Native KernelSU resolver must remain linked once in Kbuild')


def ensure_official_init_compat(ksu_source: Path) -> bool:
    """Add only the missing real include when the native initializer is used."""
    selected = ksu_source.resolve(strict=True)
    root = selected / 'kernel' if (selected / 'kernel/core/init.c').is_file() else selected
    original = _read(root, 'core/init.c')
    calls, _ = inspect_c_source(root / 'core/init.c', original, symbols=(SYMBOL,))
    if not calls:
        return False
    _validate_provider(_read(root, 'infra/symbol_resolver.h'),
                       _read(root, 'infra/symbol_resolver.c'), _read(root, 'Kbuild'))
    if _unconditional_include(original):
        return False
    path = root / 'core/init.c'
    updated = insert_after_includes(original, HEADER + '\n', path)
    path.write_text(updated, encoding='utf-8', newline='\n')
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ksu-source', type=Path, required=True)
    args = parser.parse_args()
    if ensure_official_init_compat(args.ksu_source):
        print('Restored the native KernelSU symbol resolver header')


if __name__ == '__main__':
    main()
