#!/usr/bin/env python3
"""Install a pinned ECS cache with full configuration and compiler guards.

ECS's historical configuration scan is an optimization, never the authority
for whether two kernel compilations are equivalent. Every cached invocation
also hashes the complete generated autoconf.h and a toolchain inventory.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
GUARD = 'gki-kernel-cache-v1'
EPOCH = '1748178000'
MAX_BINARY = 16 * 1024 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def regular(path: Path, maximum: int) -> bytes:
    if linked(path) or not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > maximum:
        raise ValueError('Expected a bounded regular cache-tool input')
    with path.open('rb') as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError('Cache-tool input exceeds its size limit')
    return value


def real_directory(path: Path) -> Path:
    path = path.absolute()
    if any(linked(parent) for parent in (path, *path.parents)):
        raise ValueError('Cache-tool output must not traverse symlinks or junctions')
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError('Cache-tool output must be a directory')
    return path


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    if linked(path):
        raise ValueError('Cache-tool destination must not be a symlink')
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_lock() -> dict:
    document = json.loads(regular(ROOT / 'config/kernel-cache-tools.json', 16384))
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'ecs'}
            or type(document['schema_version']) is not int or document['schema_version'] != 1):
        raise ValueError('Invalid kernel cache-tool lock')
    item = document['ecs']
    if (not isinstance(item, dict) or set(item) != {'repository', 'commit', 'path', 'url', 'size', 'sha256'}
            or item['repository'] != 'cctv18/oppo_oplus_realme_sm8650'
            or not isinstance(item['commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', item['commit'])
            or item['path'] != 'lib/ccache-x86-64'
            or item['url'] != f"https://raw.githubusercontent.com/{item['repository']}/{item['commit']}/{item['path']}"
            or type(item['size']) is not int or not 64 <= item['size'] <= MAX_BINARY
            or not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])):
        raise ValueError('Invalid immutable ECS binary identity')
    return item


def download(item: dict) -> bytes:
    request = urllib.request.Request(item['url'], headers={'User-Agent': GUARD})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.geturl() != item['url'] or response.status != 200:
            raise ValueError('Unexpected cache-tool HTTP destination or status')
        length = response.headers.get('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) != item['size']):
            raise ValueError('Unexpected cache-tool HTTP length')
        return response.read(item['size'] + 1)


def validate_binary(value: bytes, item: dict) -> None:
    if len(value) != item['size'] or sha(value) != item['sha256']:
        raise ValueError('ECS binary size or SHA256 differs from its lock')
    if (len(value) < 64 or value[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<H', value, 16)[0] not in (2, 3)
            or struct.unpack_from('<H', value, 18)[0] != 62
            or struct.unpack_from('<I', value, 20)[0] != 1):
        raise ValueError('ECS binary must be an x86-64 little-endian Linux ELF')
    offset = struct.unpack_from('<Q', value, 32)[0]
    size, count = struct.unpack_from('<HH', value, 54)
    if offset < 64 or size != 56 or not 0 < count <= 1024 or offset + size * count > len(value):
        raise ValueError('ECS binary has an invalid ELF program table')


def probe_binary(path: Path) -> None:
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('LD_PRELOAD', 'FAKESTAT', 'FAKETIME')}
    result = subprocess.run([str(path), '--version'], capture_output=True, timeout=5, env=environment)
    if result.returncode or len(result.stdout) > 16384 or not result.stdout.lower().startswith(b'ccache'):
        raise ValueError('The validated ECS tool cannot run on this Linux runner')


def compiler_identity(compiler: Path, engine: Path) -> dict:
    """Hash every binary/library/resource in the selected standalone toolchain.

    This runs once per canonical compiler in a fresh per-job installation,
    outside any fake-time preload. The shell holds a flock across generation.
    Internal symlinks are included by link text and target content; directory
    cycles and unsupported special files stop caching instead of weakening it.
    """
    compiler = compiler.resolve(strict=True)
    engine = engine.resolve(strict=True)
    if not compiler.is_file() or not engine.is_file() or compiler.parent.name != 'bin':
        raise ValueError('Cannot identify the complete standalone compiler toolchain')
    root = compiler.parent.parent
    if root.as_posix() in ('/', '/usr', '/usr/local'):
        raise ValueError('A system-wide compiler is not a standalone kernel toolchain')
    inventory = []
    digests = {}

    def visit(path: Path, name: str, ancestors: frozenset[Path]) -> None:
        if len(inventory) > 100000:
            raise ValueError('Kernel compiler inventory exceeds its file limit')
        canonical = path.resolve(strict=True)
        link = os.readlink(path) if path.is_symlink() else None
        mode = canonical.stat().st_mode
        if stat.S_ISDIR(mode):
            if canonical in ancestors:
                raise ValueError('Kernel compiler inventory contains a directory cycle')
            inventory.append({'path': name, 'kind': 'directory', 'link': link})
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child, name + '/' + child.name, ancestors | {canonical})
        elif stat.S_ISREG(mode):
            if canonical not in digests:
                digests[canonical] = file_digest(canonical)
            inventory.append({'path': name, 'kind': 'file', 'link': link,
                              'size': canonical.stat().st_size, 'sha256': digests[canonical]})
        else:
            raise ValueError('Kernel compiler inventory contains an unsupported file')

    for name in ('bin', 'lib', 'lib64'):
        path = root / name
        if path.exists() or linked(path):
            visit(path, name, frozenset())
    if not inventory:
        raise ValueError('Kernel compiler inventory is empty')
    inventory_bytes = json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode()
    return {'schema_version': 1, 'guard': GUARD, 'source_date_epoch': EPOCH,
            'compiler': compiler.relative_to(root).as_posix(),
            'engine_sha256': file_digest(engine), 'file_count': len(inventory),
            'toolchain_sha256': sha(inventory_bytes),
            'ecs_kernel_mode': modern_clang(compiler)}


def modern_clang(compiler: Path) -> bool:
    """Older Clang ignores SOURCE_DATE_EPOCH for __TIMESTAMP__.

    Keep ordinary ccache's temporal macro handling for old or unidentified
    compilers. This bounded check runs once while constructing the identity.
    """
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('LD_PRELOAD', 'FAKESTAT', 'FAKETIME')}
    try:
        result = subprocess.run([str(compiler), '--version'], capture_output=True,
                                timeout=5, env=environment)
        if result.returncode or len(result.stdout) > 16384:
            return False
        match = re.search(rb'\bclang version\s+([0-9]+)\.', result.stdout)
        return bool(match and int(match[1]) >= 18)
    except (OSError, subprocess.SubprocessError):
        return False


def bound_tools() -> dict[str, str]:
    return {name: str(Path(value).resolve()) if (value := shutil.which(name)) else ''
            for name in ('env', 'readlink', 'flock', 'sha256sum')}


def shell_wrapper(engine: str, helper: Path, identities: Path, python: str,
                  tool_paths: dict[str, str] | None = None) -> str:
    # All paths are inserted as shell literals. No eval or shell-built commands.
    tools = bound_tools() if tool_paths is None else tool_paths
    header = ('#!/bin/bash\nset -euo pipefail\n'
              f'engine={shlex.quote(engine)}\nhelper={shlex.quote(helper.as_posix())}\n'
              f'identities={shlex.quote(identities.as_posix())}\npython={shlex.quote(python)}\n')
    header += ''.join('cache_' + name + '=' + shlex.quote(tools.get(name, '')) + '\n'
                      for name in ('env', 'readlink', 'flock', 'sha256sum'))
    return header + r'''
export CCACHE_COMPILERCHECK=content
export CCACHE_SLOPPINESS=include_file_ctime,include_file_mtime
unset CCACHE_INODECACHE
export CCACHE_NOINODECACHE=1
export SOURCE_DATE_EPOCH=1748178000
unset CCACHE_IS_KERNEL_COMPILING
# KEY=VALUE command-line overrides take priority over the environment in
# ccache. Skip caching for this uncommon syntax rather than accepting overrides.
overrides=false
while [ "$#" -gt 0 ] && [[ "$1" == *=* ]] && [[ "$1" != -* ]]; do
  overrides=true
  shift
done
if [ "$#" -eq 0 ] || [[ "$1" == -* ]]; then
  if [ -n "$engine" ]; then exec "$engine" "$@"; fi
  printf '%s\n' 'Kernel compiler cache unavailable; compiling without cache.'
  exit 0
fi
compiler=$(command -v -- "$1") || exit 127
shift
if [ ! -x "$compiler" ]; then exit 127; fi
if [ -z "$engine" ] || $overrides; then exec "$compiler" "$@"; fi
# The kernel's hermetic build may replace PATH. Use only host utilities bound
# before that transition; never prepend the whole original host PATH.
for utility in "$cache_env" "$cache_readlink" "$cache_flock" "$cache_sha256sum"; do
  if [ -z "$utility" ] || [ ! -x "$utility" ]; then exec "$compiler" "$@"; fi
done
# Keep all plausible generated autoconf candidates. Hashing an extra one is
# conservative; choosing a wrong single include-search candidate is unsafe.
configs=()
add_config() {
  local path="$1"
  if [[ "$path" == *generated/autoconf.h ]] && [ -f "$path" ]; then
    path=$("$cache_readlink" -f -- "$path") || return 0
    if [[ "$path" != *:* && "$path" != *$'\n'* && "$path" != *$'\r'* ]]; then
      local found
      for found in "${configs[@]}"; do [ "$found" = "$path" ] && return 0; done
      configs+=("$path")
    fi
  fi
  return 0
}
arguments=("$@")
for ((i=0; i<${#arguments[@]}; i++)); do
  argument="${arguments[$i]}"
  # Hidden include paths cannot be proved by this shell-only guard.
  if [[ "$argument" == @* ]]; then exec "$compiler" "$@"; fi
  case "$argument" in
    -include|-I|-isystem|-iquote)
      if ((i+1<${#arguments[@]})); then
        ((i+=1))
        directory="${arguments[$i]}"
        if [ "$argument" = -include ]; then add_config "$directory";
        else add_config "$directory/generated/autoconf.h"; fi
      fi;;
    -include*) add_config "${argument#-include}";;
    -I*) add_config "${argument#-I}/generated/autoconf.h";;
    -isystem*) add_config "${argument#-isystem}/generated/autoconf.h";;
    -iquote*) add_config "${argument#-iquote}/generated/autoconf.h";;
  esac
done
add_config "$PWD/include/generated/autoconf.h"
add_config "$PWD/out/include/generated/autoconf.h"
if [ "${#configs[@]}" -eq 0 ]; then exec "$compiler" "$@"; fi
canonical=$("$cache_readlink" -f -- "$compiler") || exec "$compiler" "$@"
key=$(printf '%s' "$canonical" | "$cache_sha256sum")
key=${key%% *}
if [[ ! "$key" =~ ^[0-9a-f]{64}$ ]]; then exec "$compiler" "$@"; fi
descriptor="$identities/$key.json"
disabled="$identities/$key.disabled"
if [ -f "$disabled" ]; then exec "$compiler" "$@"; fi
if [ ! -f "$descriptor" ]; then
  (
    "$cache_flock" -x 9
    if [ ! -f "$descriptor" ] && [ ! -f "$disabled" ]; then
      # Hash real bytes outside recipe-provided LD_PRELOAD/fake stat hooks.
      if ! "$cache_env" -u LD_PRELOAD -u FAKESTAT -u FAKETIME "$python" "$helper" identity \
          --compiler "$canonical" --engine "$engine" --output "$descriptor"; then
        printf '%s\n' '::warning::Compiler identity unavailable; using the compiler directly.' >&2
        : > "$disabled"
      fi
    fi
  ) 9>"$identities/$key.lock"
fi
if [ ! -f "$descriptor" ] || [ -L "$descriptor" ]; then exec "$compiler" "$@"; fi
extra="$descriptor"
for config in "${configs[@]}"; do extra+=":$config"; done
if [ -n "${CCACHE_EXTRAFILES:-}" ]; then extra+=":$CCACHE_EXTRAFILES"; fi
export CCACHE_EXTRAFILES="$extra"
identity=$(<"$descriptor")
if [[ "$identity" == *'"ecs_kernel_mode":true'* ]]; then export CCACHE_IS_KERNEL_COMPILING=true; fi
exec "$engine" "$compiler" "$@"
'''


def install(output: Path, *, stock: Path = Path('/usr/bin/ccache')) -> dict:
    output = real_directory(output)
    if any(c in str(output) for c in '\r\n:') and os.name != 'nt':
        raise ValueError('Unsupported cache-tool output path')
    engine = ''
    kind = 'disabled'
    try:
        item = load_lock()
        value = download(item)
        validate_binary(value, item)
        binary = output / 'ccache-real'
        write_atomic(binary, value, 0o700)
        probe_binary(binary)
        engine, kind = str(binary), 'ccache-ECS'
    except (OSError, ValueError, UnicodeError, http.client.HTTPException, subprocess.SubprocessError):
        print('::warning::Pinned ECS cache unavailable or invalid; using guarded ordinary ccache.', file=sys.stderr)
        if stock.is_file() and os.access(stock, os.X_OK):
            engine, kind = str(stock.resolve()), 'stock-ccache'
    identities = output / ('identities-' + uuid.uuid4().hex)
    # Python 3.13 gives mode 0700 special DACL semantics on Windows. Keep
    # inherited test/workspace permissions there; production runs on Linux.
    identities.mkdir()
    if os.name != 'nt':
        identities.chmod(0o700)
    helper = Path(__file__).resolve()
    wrapper = output / 'ccache'
    write_atomic(wrapper, shell_wrapper(engine, helper, identities, sys.executable).encode('utf-8'), 0o700)
    result = {'schema_version': 1, 'guard': GUARD, 'engine': kind,
              'engine_path': engine, 'program': str(wrapper), 'helper': str(helper)}
    write_atomic(output / 'cache-tool.json', (json.dumps(result, sort_keys=True) + '\n').encode())
    return result


def append_environment(path: Path, values: dict[str, str]) -> None:
    if linked(path) or any(c in value for value in values.values() for c in '\r\n'):
        raise ValueError('Invalid GitHub environment destination or value')
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        for key, value in values.items():
            stream.write(key + '=' + value + '\n')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('install')
    setup.add_argument('--output', required=True, type=Path)
    setup.add_argument('--github-env', required=True, type=Path)
    setup.add_argument('--github-path', type=Path)
    identity = commands.add_parser('identity')
    identity.add_argument('--compiler', required=True, type=Path)
    identity.add_argument('--engine', required=True, type=Path)
    identity.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'install':
            result = install(args.output)
            append_environment(args.github_env, {'CCACHE_PROGRAM': result['program'],
                'GKI_CACHE_TOOLS': result['helper'], 'GKI_CACHE_ENGINE': result['engine']})
            if args.github_path is not None:
                append_environment(args.github_path, {})
                with args.github_path.open('a', encoding='utf-8', newline='\n') as stream:
                    stream.write(str(args.output.absolute()) + '\n')
            print('Kernel cache engine: ' + result['engine'] + '; complete compiler/configuration guards enabled.')
        else:
            value = compiler_identity(args.compiler, args.engine)
            write_atomic(args.output, (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode())
    except (OSError, ValueError, UnicodeError) as error:
        print('Kernel compiler cache stopped: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
