#!/usr/bin/env python3
"""Match a frozen manager kernel's real KPM ABI to audited official assets.

The 0.14.0 tag's source initially declared ABI 1. Its public release assets
were replaced after official build 40388681873 at d958d258 (ABI 2). We pin
the updated *bytes* and source commit, not the mutable tag or its title.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
import struct


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'SukiSU-Ultra/SukiSU_KernelPatch_patch'
BRIDGE_COMMIT = 'd958d2582fe3b7eaaeaf238b3bf9af4507a81174'
BRIDGE_TAG = '0.14.0'
BRIDGE_ASSETS = {
    'kpimg': (294384, 'ab020f31ca1c058603917e46d8a6691fbce0dd5cdc135a9cd8dc780e89472716'),
    'kptools-linux': (1672840, 'a6b912ca964d35427d7fbcd87273abac604970e9746fa0613362be971f07259d'),
}
BRIDGE_CONSTANTS = {
    'SUKISU_KPM_SUPERCALL_MAGIC': 0x53554b504d000002,
    'SUKISU_KPM_SUPERCALL_MAGIC2': 0x4b504d4252494447,
    'SUKISU_KPM_SUPERCALL_MAGIC3': 0x53554b494e544552,
    'SUKISU_KPM_ABI_VERSION': 2,
    'SUKISU_KPM_PROBE': 0,
    'SUKISU_KPM_OUTPUT_MAX': 4096,
}
LEGACY_SYMBOLS = (
    'sukisu_kpm_load_module_path', 'sukisu_kpm_unload_module',
    'sukisu_kpm_num', 'sukisu_kpm_info', 'sukisu_kpm_list',
    'sukisu_kpm_control', 'sukisu_kpm_version',
)

# Actual GCC ARM64 instructions in the independently hash-pinned official
# payload, corresponding to before_supercall() in patch/sukisu/sukisu.c.
# These check EL1h, all three framing tags, ABI 2 and reserved registers.
# The whole payload hash is still mandatory; these public code words do not
# replace provenance verification or claim that device-specific boot succeeded.
BRIDGE_INSTRUCTIONS = {
    0x1f1c8: (0xf9408401, 0x92400c21, 0xf100143f, 0x54000080),
    0x1f1e4: (0xf9400002, 0xd2800041, 0xf2a9a001, 0xf2c96a01,
              0xf2ea6aa1, 0xeb01005f, 0x54fffee1),
    0x1f208: (0xf9401802, 0xf9002a61, 0xd28888e1, 0xb9000a63,
              0xf2aa4921, 0xf2c9a841, 0xf2e96a01, 0xeb01005f, 0x54fffd81),
    0x1f22c: (0xf9401c02, 0xd288aa41, 0xf2a9ca81, 0xf2c96921,
              0xf2ea6aa1, 0xeb01005f, 0x54fffca1),
    0x1f248: (0xf9402004, 0xf100089f, 0x54fffc41),
    0x1f254: (0x91012001, 0x9103e003, 0x14000003, 0xeb03003f,
              0x540000a0, 0xf9400022, 0x91002021, 0xb4ffff82, 0x17ffffd9),
}


class KPMProfileError(ValueError):
    """Only locally constructed, non-secret diagnostic text is exposed."""


def _read_source(path: Path, limit: int = 524288) -> str:
    try:
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise KPMProfileError('KPM source must be a regular, non-symlink file')
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise KPMProfileError('KPM source exceeds its bounded size')
        return data.decode('utf-8')
    except (OSError, UnicodeError):
        raise KPMProfileError('Cannot read the frozen KPM source contract') from None


def load_bridge_release() -> dict:
    """Return the immutable ABI-2 lock in the existing release-doc schema."""
    try:
        document = json.loads(_read_source(ROOT / 'config/kpm-bridge-release.json', 8192))
    except (ValueError, TypeError):
        raise KPMProfileError('Invalid fixed official ABI-2 KPM release lock') from None
    expected = {
        'schema_version': 1, 'repository': REPOSITORY, 'tag': BRIDGE_TAG,
        'commit': BRIDGE_COMMIT,
        'assets': {name: {'size': size, 'sha256': sha,
                         'url': f'https://github.com/{REPOSITORY}/releases/download/{BRIDGE_TAG}/{name}'}
                   for name, (size, sha) in BRIDGE_ASSETS.items()},
    }
    if document != expected or type(document.get('schema_version')) is not int:
        raise KPMProfileError('Invalid fixed official ABI-2 KPM release lock')
    if any(type(item['size']) is not int for item in document['assets'].values()):
        raise KPMProfileError('Invalid fixed official ABI-2 KPM asset lock')
    return document


def _without_comments(text: str) -> str:
    # Preserve string literals so a comment marker inside one cannot hide a
    # later real definition. Matching a function requires a body, not a string.
    expression = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*'
    return re.sub(expression, lambda match: (' ' if match[0].startswith(('/', '/*'))
                                            else match[0]), text, flags=re.S)


def _constant(text: str, name: str) -> int:
    definitions = re.findall(r'^\s*#\s*define\s+' + re.escape(name) + r'\b([^\n]*)$', text, re.M)
    if len(definitions) != 1:
        raise KPMProfileError('KPM bridge constants are missing or duplicated')
    value = definitions[0].strip()
    if not re.fullmatch(r'(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:ULL|LLU|UL|LU|U|L)?', value):
        raise KPMProfileError('KPM bridge constant is not an audited literal')
    return int(re.sub(r'[uUlL]+$', '', value), 0)


def _check_bridge(code: str, header: str) -> None:
    for name, expected in BRIDGE_CONSTANTS.items():
        if _constant(header, name) != expected:
            raise KPMProfileError('Unsupported KPM bridge ABI or framing tags; matching official payload is required')
    patterns = [
        r'\blong\s+(?:__nocfi\s+)?sukisu_kpm_bridge\s*\([^;{}]*\)\s*\{',
        r'\bfind_kernel_symbol_exact\s*\(\s*"__arm64_sys_truncate"\s*\)',
        r'\bregs\.pstate\s*=\s*PSR_MODE_EL1h\s*;',
        r'\bregs\.regs\[\s*0\s*\]\s*=\s*SUKISU_KPM_SUPERCALL_MAGIC\s*;',
        r'\bregs\.regs\[\s*6\s*\]\s*=\s*SUKISU_KPM_SUPERCALL_MAGIC2\s*;',
        r'\bregs\.regs\[\s*7\s*\]\s*=\s*SUKISU_KPM_SUPERCALL_MAGIC3\s*;',
        r'\bregs\.regs\[\s*8\s*\]\s*=\s*SUKISU_KPM_ABI_VERSION\s*;',
        r'\bregs\.regs\[\s*1\s*\]\s*=\s*SUKISU_KPM_PROBE\s*;',
        r'\bif\s*\(\s*fn\s*\(\s*&regs\s*\)\s*!=\s*SUKISU_KPM_ABI_VERSION\s*\)\s*return\s+-EOPNOTSUPP\s*;',
        r'\breturn\s+fn\s*\(\s*&regs\s*\)\s*;',
    ]
    if not all(re.search(pattern, code, re.S) for pattern in patterns):
        raise KPMProfileError('Frozen KPM source has an unknown bridge implementation')


def _legacy_release() -> dict:
    # Lazy import avoids a dependency cycle with the patcher's validator.
    from apply_kpm_patch import load_release
    return load_release()


def resolve_profile(ksu_source: Path | None = None) -> dict:
    """Select from the actual frozen C interface, never a manager version label.

    None preserves legacy callers. A supplied but unknown source must fail;
    silently selecting the legacy payload recreated the original failure.
    Accept a KernelSU checkout root or its kernel/ directory.
    """
    if ksu_source is None:
        return _legacy_release()
    source = Path(ksu_source).absolute()
    kernel = source / 'kernel' if (source / 'kernel').is_dir() else source
    if (not kernel.is_dir() or any(p.is_symlink() for p in (source, kernel))):
        raise KPMProfileError('Frozen KPM source root must be a real directory')
    candidates = [kernel / path for path in ('extension/kpm/kpm.c', 'kpm/kpm.c', 'kpm.c')]
    files = [path for path in candidates if path.exists() or path.is_symlink()]
    if len(files) != 1:
        raise KPMProfileError('Frozen manager source does not expose one known KPM implementation')
    path = files[0]
    if any(p.is_symlink() for p in path.parents if p != kernel.parent and p.is_relative_to(kernel)):
        raise KPMProfileError('KPM source directories must not be symlinks')
    code = _without_comments(_read_source(path))
    header = path.with_name('bridge.h')
    if re.search(r'\bsukisu_kpm_bridge\b', code) or header.exists() or header.is_symlink():
        if not header.is_file():
            raise KPMProfileError('Frozen KPM bridge header is missing')
        _check_bridge(code, _without_comments(_read_source(header)))
        if any(re.search(r'\bEXPORT_SYMBOL\s*\(\s*' + symbol + r'\s*\)', code)
               for symbol in LEGACY_SYMBOLS):
            raise KPMProfileError('Frozen KPM source ambiguously mixes legacy and bridge interfaces')
        return load_bridge_release()
    for symbol in LEGACY_SYMBOLS:
        if (not re.search(r'\bvoid\s+' + symbol + r'\s*\([^;{}]*\)\s*\{', code, re.S)
                or not re.search(r'\bEXPORT_SYMBOL\s*\(\s*' + symbol + r'\s*\)\s*;', code)):
            raise KPMProfileError('Unsupported legacy KPM hooks; no matching official payload is audited')
    return _legacy_release()


def release_for_provenance(provenance: dict) -> dict:
    """Resolve exact public report provenance to a strict, current local lock."""
    if not isinstance(provenance, dict) or set(provenance) != {'repository', 'tag', 'commit'}:
        raise KPMProfileError('KPM report has an invalid payload provenance')
    for release in (_legacy_release(), load_bridge_release()):
        expected = {key: release[key] for key in ('repository', 'tag', 'commit')}
        if provenance == expected:
            return release
    raise KPMProfileError('KPM report references an unknown payload contract')


def validate_bridge_payload(data: bytes) -> dict:
    """Check actual ABI-2 ARM64 handshake instructions and mandatory full hash."""
    size, sha = BRIDGE_ASSETS['kpimg']
    if (len(data) != size or data[:12] != b'KP1158\0\0\0\0\x0e\0'
            or struct.unpack_from('<Q', data, 16)[0] != 2
            or not data[24:48].split(b'\0', 1)[0]
            or struct.unpack_from('<I', data, 4096)[0] in (0, 0xffffffff)):
        raise KPMProfileError('Expected the audited Android ARM64 ABI-2 KPM payload')
    for offset, words in BRIDGE_INSTRUCTIONS.items():
        if struct.unpack_from('<' + 'I' * len(words), data, offset) != words:
            raise KPMProfileError('KPM payload does not implement the audited ABI-2 handshake')
    if hashlib.sha256(data).hexdigest() != sha:
        raise KPMProfileError('Official ABI-2 KPM payload failed complete SHA256 verification')
    return {'version': '0.14.0', 'flags': 2, 'bridge_abi': 2}
