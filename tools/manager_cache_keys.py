"""Borrow verified dependency/task caches without changing manager outputs.

Each manager still saves into its own namespace. Cargo and Gradle retain
authority over the inputs/fingerprints of every restored compilation task.
The old namespaces label NDK 30 BakaSU caches as ``ndk29``; retain those names
for migration, but never offer them to the NDK 29 / stable-Rust managers.
"""
from __future__ import annotations

import re

from manager_variants import MANAGERS


VARIANT = '${{ inputs.kernelsu_variant }}'
OS = '${{ runner.os }}'
ARCH = '${{ runner.arch }}'
RUST = '${{ steps.manager-rust-version.outputs.version }}'
HASH_SUFFIX = re.compile(r'\$\{\{ hashFiles\([^{}]+\) \}\}-$')


def restore_keys(prefix: str, *, kind: str, compiler_prefix: str | None = None,
                 legacy: tuple[str, ...] = ()) -> str:
    """Order own snapshots before compatible sibling dependency snapshots.

    ``prefix`` is the existing save-key prefix, including its source-input
    hash. For Cargo only, ``compiler_prefix`` may be that exact prefix minus
    the final hash expression, retaining the full Rust/NDK/runner identity.
    Legacy migration keys remain last; they are the caller's existing keys.
    """
    if kind not in ('cargo', 'gradle') or not isinstance(prefix, str):
        raise ValueError('Only the reviewed Cargo and Gradle namespaces may be shared')
    namespace = 'gki-manager-cargo-v3-' if kind == 'cargo' else 'gki-manager-gradle-build-v2-'
    expected = namespace + VARIANT + '-' + OS + '-' + ARCH + '-'
    if not prefix.startswith(expected) or prefix.count(VARIANT) != 1 or '\n' in prefix or '\r' in prefix:
        raise ValueError('Manager dependency cache must retain its selected manager and runner identity')
    suffix = HASH_SUFFIX.search(prefix)
    if suffix is None:
        raise ValueError('Manager dependency cache must retain its source/tool input hash')
    tools = prefix[len(expected):suffix.start()]
    if kind == 'cargo':
        if re.fullmatch(r'ndk[0-9]+-' + re.escape(RUST) + '-', tools) is None:
            raise ValueError('Cargo dependencies must retain the full NDK and Rust compiler identity')
        if compiler_prefix is not None and compiler_prefix != prefix[:suffix.start()]:
            raise ValueError('Cargo fallback cannot omit compiler, NDK, OS or architecture guards')
    elif compiler_prefix is not None or re.fullmatch(r'jdk[0-9]+-sdk[0-9]+-ndk[0-9]+-', tools) is None:
        raise ValueError('Gradle task fallback must retain JDK, SDK, NDK and wrapper/catalog identity')
    if not isinstance(legacy, tuple) or any(not isinstance(key, str) or not key or
                                          '\n' in key or '\r' in key for key in legacy):
        raise ValueError('Legacy cache migration keys must be individual nonempty prefixes')

    own = [prefix] + ([compiler_prefix] if compiler_prefix else [])
    siblings = []
    for variant in MANAGERS:
        if variant == 'BakaSU':
            continue
        # Baka's historical NDK label is inaccurate. On that variant, these
        # expressions collapse to its own namespace rather than crossing NDKs.
        routed = "${{ inputs.kernelsu_variant == 'BakaSU' && 'BakaSU' || '" + variant + "' }}"
        siblings.append(prefix.replace(VARIANT, routed))
    # Actions permits ten keys in total, including its primary save key.
    # Prefer exact-input sibling objects over broad cross-source snapshots;
    # the latter would exceed that limit and disable restoration altogether.
    ordered = list(dict.fromkeys([*own, *siblings, *legacy]))
    if len(ordered) > 9:
        raise ValueError('Actions accepts at most nine restore prefixes plus its primary key')
    return '\n'.join(ordered)
