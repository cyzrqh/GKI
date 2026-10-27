#!/usr/bin/env python3
"""Narrow source adaptations for reviewed official OPlus 6.12 layouts."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from validate_susfs_hooks import strip_comments_and_literals

RTMUTEX_PATCH_SHA256 = '2dc6572a936f1acba139ccdb6daff6cacf709fcfc36f465f9e2b59f56bd40cfc'
BITCODE_COMMAND = '\t\t$(CC) $(CLANG_FLAGS) $(KBUILD_CFLAGS) -c $(patsubst %.o,%.m.bc,$@) -o $@)'
BITCODE_FIXED = BITCODE_COMMAND.replace('$(KBUILD_CFLAGS)',
    '$(filter-out -fmacro-prefix-map=% -ffile-prefix-map=%,$(KBUILD_CFLAGS))')


def native_git(common: Path, *arguments: str) -> str:
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(common), *arguments],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    if result.returncode:
        raise ValueError('Cannot verify the immutable official kernel naming inputs')
    return result.stdout


def config_suffix(text: str) -> str:
    lines = re.findall(r'(?m)^CONFIG_LOCALVERSION=(.*)$', text)
    if len(lines) > 1:
        raise ValueError('Duplicate native CONFIG_LOCALVERSION settings')
    if not lines:
        return ''
    value = lines[0]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if not re.fullmatch(r'[A-Za-z0-9_.+-]{0,200}', value):
        raise ValueError('Unsupported official CONFIG_LOCALVERSION value')
    return value


def official_release_prefix(common: Path, profile: dict) -> str:
    """Read static official constants, never source executable shell config."""
    values = {'BRANCH': set(), 'KMI_GENERATION': set()}
    for relative in ('build.config.common', 'build.config.constants', 'build.config.gki'):
        path = common / relative
        if not path.exists():
            continue
        content = read_source(path)
        # Feature configuration may remove check_defconfig from gki only.
        if relative != 'build.config.gki' and content != native_git(common, 'show', 'HEAD:' + relative):
            raise ValueError('Official Android/KMI naming constants were modified')
        for line in content.splitlines():
            if not re.match(r'^[ \t]*(?:export[ \t]+)?(?:BRANCH|KMI_GENERATION)[ \t]*=', line):
                continue
            match = re.fullmatch(r'[ \t]*(?:export[ \t]+)?(BRANCH|KMI_GENERATION)[ \t]*=[ \t]*'
                r'(?:([A-Za-z0-9_.-]+)|"([A-Za-z0-9_.-]+)"|\'([A-Za-z0-9_.-]+)\')[ \t]*(?:#.*)?', line)
            if match is None:
                raise ValueError('Android/KMI naming requires static official constants')
            values[match[1]].add(next(value for value in match.groups()[1:] if value is not None))
    if any(len(items) != 1 for items in values.values()):
        raise ValueError('Official Android/KMI generation is missing or contradictory')
    branch, generation = next(iter(values['BRANCH'])), next(iter(values['KMI_GENERATION']))
    if (branch != profile['android_version'] + '-' + profile['kernel_version']
            or not re.fullmatch(r'(?:0|[1-9][0-9]{0,3})', generation)):
        raise ValueError('Official Android/KMI constants disagree with the selected source profile')
    return profile['kernel'] + '-' + profile['android_version'] + '-' + generation


def prepare_native_release(common: Path, profile: dict) -> str:
    prefix = official_release_prefix(common, profile)
    script = common / 'scripts/setlocalversion'
    if read_source(script) != native_git(common, 'show', 'HEAD:scripts/setlocalversion'):
        raise ValueError('Official setlocalversion must remain byte-for-byte unchanged')
    defconfig = 'arch/arm64/configs/gki_defconfig'
    suffix = config_suffix(read_source(common / defconfig))
    if suffix != config_suffix(native_git(common, 'show', 'HEAD:' + defconfig)):
        raise ValueError('Preserve the official vendor/page-size CONFIG_LOCALVERSION suffix')
    output = common / 'out'
    if output.is_symlink() or output.exists() and not output.is_dir():
        raise ValueError('Native version output must be an independent build directory')
    output.mkdir(exist_ok=True)
    target = output / 'localversion.gki'
    local = []
    for directory in (output, common):
        for path in sorted(directory.glob('localversion*')):
            if '~' in path.name:
                continue
            content = read_source(path).rstrip('\n')
            if not re.fullmatch(r'[A-Za-z0-9_.+-]{0,200}', content):
                raise ValueError('Unsupported native localversion file')
            local.append(content)
    existing = ''.join(local) + suffix
    desired = prefix.removeprefix(profile['kernel'])
    if existing == desired or existing.startswith(desired + '-'):
        return prefix
    if re.search(r'android[0-9]+-[0-9]+', existing) or target.exists():
        raise ValueError('Native localversion has a conflicting Android/KMI prefix')
    # Official setlocalversion reads build-dir localversion* before the source
    # and CONFIG_LOCALVERSION. Thus -4k and native SCM suffixes stay intact.
    if any(path != target for path in output.glob('localversion*')):
        raise ValueError('Review existing build localversion files before adding a KMI prefix')
    target.write_text(desired, encoding='ascii', newline='\n')
    return prefix


def read_source(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Expected a bounded regular official compatibility source')
    return path.read_text(encoding='utf-8')


def function_text(source: str, name: str) -> str:
    code = strip_comments_and_literals(source)
    pattern = re.compile(r'(?m)^[A-Za-z_][^;{}\n]*\b' + re.escape(name) + r'\s*\([^;{}]*\)\s*\{')
    matches = list(pattern.finditer(code))
    if len(matches) != 1:
        raise ValueError('Expected one real native compatibility function: ' + name)
    opening = code.index('{', matches[0].start(), matches[0].end())
    depth = 0
    for index in range(opening, len(code)):
        if code[index] == '{':
            depth += 1
        elif code[index] == '}':
            depth -= 1
            if depth == 0:
                return source[matches[0].start():index + 1]
    raise ValueError('Unterminated native compatibility function: ' + name)


def adapt_susfs_patch(common: Path, patch_bytes: bytes) -> bytes:
    """Bind the show_smap hunk to its actual function before strict patching.

    The 6.12 official trees use vma_pages in show_smap, while the SUSFS patch
    was authored against vma_data_pages. Fuzz must never relocate this hunk to
    an unrelated function with a similar declaration.
    """
    text = patch_bytes.decode('utf-8')
    sections = re.split(r'(?=^diff --git )', text, flags=re.M)
    selected = [index for index, section in enumerate(sections)
                if section.startswith('diff --git a/fs/proc/task_mmu.c b/fs/proc/task_mmu.c\n')]
    if len(selected) != 1:
        raise ValueError('Official SUSFS patch requires one task_mmu source section')
    index = selected[0]
    hunks = re.split(r'(?=^@@ )', sections[index], flags=re.M)
    targets = [position for position, hunk in enumerate(hunks)
               if re.match(r'@@ [^\n]+ @@ static int show_smap\(', hunk)]
    if len(targets) != 1:
        raise ValueError('Official SUSFS patch requires one show_smap hunk')
    original_file = read_source(common / 'fs/proc/task_mmu.c')
    after = original_file
    rollup = function_text(original_file, 'show_smaps_rollup')
    # The vendor's newer mmap-lock contention loop gathers both whole VMAs
    # and partial tails. Retain that implementation and exclude hidden file
    # mappings independently at each of its three real gather calls.
    calls = re.findall(r'(?m)^(\s*)smap_gather_stats\(vma, &mss, (0|last_vma_end)\);$', rollup)
    changed_rollup = None
    if calls:
        if [argument for _indent, argument in calls] != ['0', '0', 'last_vma_end']:
            raise ValueError('Unknown native show_smaps_rollup gather lifecycle')
        def guard(match):
            indent, argument = match[1], match[2]
            return ('#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n' + indent
                    + 'if (!vma->vm_file || !SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
                    + '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n' + indent + '\t'
                    + 'smap_gather_stats(vma, &mss, ' + argument + ');')
        changed_rollup = re.sub(r'(?m)^([ \t]*)smap_gather_stats\(vma, &mss, (0|last_vma_end)\);$', guard, rollup)
    skipped_rollup = 0
    for hunk in hunks[1:]:
        if 'static int show_smaps_rollup(' in hunk.splitlines()[0] and changed_rollup is not None:
            added = ''.join(line[1:] for line in hunk.splitlines(keepends=True)[1:] if line.startswith('+'))
            expected_additions = (
                '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
                '\t\tif (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
                '\t\t\tgoto bypass_orig_flow;\n'
                '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n\n',
                '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\nbypass_orig_flow:\n'
                '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n')
            if skipped_rollup >= 2 or added != expected_additions[skipped_rollup]:
                raise ValueError('Unknown official SUSFS rollup feature additions')
            skipped_rollup += 1
            continue
        old = ''.join(line[1:] for line in hunk.splitlines(keepends=True)[1:] if line.startswith((' ', '-')))
        new = ''.join(line[1:] for line in hunk.splitlines(keepends=True)[1:] if line.startswith((' ', '+')))
        if 'static int show_smap(' in hunk.splitlines()[0]:
            function = function_text(after, 'show_smap')
            if old not in function:
                context = '\tif (!vma_data_pages(vma))\n'
                if old.count(context) != 1 or new.count(context) != 1:
                    raise ValueError('Unknown official SUSFS show_smap pre-context')
                old = old.replace(context, '\tif (!vma_pages(vma))\n', 1)
                new = new.replace(context, '\tif (!vma_pages(vma))\n', 1)
            if function.count(old) != 1:
                raise ValueError('Official source differs from both reviewed show_smap contexts')
        if after.count(old) != 1:
            raise ValueError('Official task_mmu context is not unique; refusing fuzzy hook relocation')
        after = after.replace(old, new, 1)
    if changed_rollup is not None:
        if skipped_rollup != 2 or after.count(rollup) != 1:
            raise ValueError('Unknown official SUSFS rollup hook layout')
        after = after.replace(rollup, changed_rollup, 1)
    sections[index] = 'diff --git a/fs/proc/task_mmu.c b/fs/proc/task_mmu.c\n' + ''.join(difflib.unified_diff(
        original_file.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile='a/fs/proc/task_mmu.c', tofile='b/fs/proc/task_mmu.c'))
    return ''.join(sections).encode('utf-8')


def apply_checked_updates(common: Path, updates: dict[str, tuple[str, str]]) -> bool:
    """Validate every file and exact diff before changing any source file."""
    changes = []
    for relative, (before, after) in updates.items():
        if relative not in ('kernel/locking/rtmutex.c', 'kernel/locking/rtmutex_api.c', 'rust/Makefile'):
            raise ValueError('Unreviewed compatibility output path')
        if read_source(common / relative) != before:
            raise ValueError('Official source changed while preparing compatibility updates')
        if before != after:
            changes.extend(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile='a/' + relative, tofile='b/' + relative))
    if not changes:
        return False
    descriptor, name = tempfile.mkstemp(prefix='.oplus-compat-', suffix='.patch', dir=common)
    patch = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output:
            output.write(''.join(changes))
        for arguments in (['--check'], ['--whitespace=nowarn']):
            result = subprocess.run(['git', '--no-replace-objects', '-C', str(common), 'apply',
                                     *arguments, str(patch.resolve())], capture_output=True, timeout=30)
            if result.returncode:
                raise ValueError('Exact compatibility patch ' + ('dry-run' if '--check' in arguments else 'application') + ' failed')
        if any(read_source(common / relative) != after for relative, (_, after) in updates.items()):
            raise ValueError('Compatibility patch result differs from its checked source plan')
    finally:
        patch.unlink(missing_ok=True)
    return True


def rtmutex_updates(sources: dict[str, str], patch_bytes: bytes) -> dict[str, tuple[str, str]]:
    if hashlib.sha256(patch_bytes).hexdigest() != RTMUTEX_PATCH_SHA256:
        raise ValueError('The reviewed native rtmutex patch changed')
    patch = patch_bytes.decode('utf-8')
    chunks = re.split(r'(?=^diff )', patch, flags=re.M)
    updates = {}
    for section in chunks:
        if not section:
            continue
        paths = re.findall(r'^\+\+\+ b/([^\t\n]+)', section, flags=re.M)
        if len(paths) != 1 or paths[0] not in ('kernel/locking/rtmutex.c', 'kernel/locking/rtmutex_api.c'):
            raise ValueError('Reviewed rtmutex patch file layout changed')
        relative = paths[0]
        before = after = sources[relative]
        for hunk in re.split(r'(?=^@@ )', section, flags=re.M)[1:]:
            lines = hunk.splitlines(keepends=True)[1:]
            old = ''.join(line[1:] for line in lines if line.startswith((' ', '-')))
            new = ''.join(line[1:] for line in lines if line.startswith((' ', '+')))
            if after.count(old) == 1:
                after = after.replace(old, new, 1)
            elif after.count(new) == 1:
                pass  # The complete exact reviewed fix is already upstream.
            else:
                # SM8845 already has the waiter_task fix and scoped lock but
                # lacks its early null-waiter guard. Require the entire exact
                # fixed hunk minus only those three added lines.
                guard = '\tif (!waiter_task) /* never enqueued */\n\t\treturn;\n\n'
                partial = new.replace(guard, '', 1)
                if (relative != 'kernel/locking/rtmutex.c' or new.count(guard) != 1
                        or after.count(partial) != 1):
                    raise ValueError('Native rtmutex source differs from the reviewed full or partial fix')
                after = after.replace(partial, new, 1)
        updates[relative] = (before, after)
    if set(updates) != {'kernel/locking/rtmutex.c', 'kernel/locking/rtmutex_api.c'}:
        raise ValueError('Reviewed rtmutex patch requires both native source files')
    return updates


def apply_rtmutex_patch(common: Path, patch: Path) -> bool:
    raw = read_source(patch).encode('utf-8')
    sources = {relative: read_source(common / relative) for relative in
               ('kernel/locking/rtmutex.c', 'kernel/locking/rtmutex_api.c')}
    return apply_checked_updates(common, rtmutex_updates(sources, raw))


def prepare_rust_bitcode(common: Path) -> bool:
    path = common / 'rust/Makefile'
    before = read_source(path)
    if '$(if $(link_helper)' not in before:
        return False
    if before.count(BITCODE_FIXED) == 1 and BITCODE_COMMAND not in before:
        return False
    if before.count(BITCODE_COMMAND) != 1:
        raise ValueError('Review the official Rust inline-helper bitcode command before compiling')
    # Macro/file prefix maps apply to source preprocessing, not precompiled
    # LLVM bitcode. Preserve debug prefix maps and every source C ABI flag.
    after = before.replace(BITCODE_COMMAND, BITCODE_FIXED, 1)
    return apply_checked_updates(common, {'rust/Makefile': (before, after)})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('rtmutex', 'prepare-build'))
    parser.add_argument('--common', required=True, type=Path)
    parser.add_argument('--patch', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'rtmutex':
            if args.patch is None:
                raise ValueError('rtmutex requires its reviewed fixed patch file')
            changed = apply_rtmutex_patch(args.common, args.patch)
        else:
            changed = prepare_rust_bitcode(args.common)
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as error:
        print('OPlus exact compatibility check stopped: ' + str(error), file=sys.stderr)
        return 1
    print('OPlus exact compatibility: ' + ('applied' if changed else 'already satisfied'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
