#!/usr/bin/env python3
"""Check the executable boot entry of a raw little-endian ARM64 Linux Image.

This is a bounded structural check, not a proof that the kernel can boot.
Header layout: https://docs.kernel.org/arch/arm64/booting.html
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct


HEADER_SIZE = 64
MAX_IMAGE_SIZE = 256 * 1024 * 1024
ARM64_MAGIC = b'ARM\x64'
EFI_MZ_ADD = 0x91005A4D  # add x13, x18, #0x16; its first bytes spell MZ.
EFI_SIGNATURE_NOP = 0xFA405A4D  # ccmp x18, #0, #0xd, pl; Linux's current MZ signature.
NOP = 0xD503201F


def _is_branch(instruction: int) -> bool:
    # B imm26, excluding BL (which changes the link register).
    return instruction & 0xFC000000 == 0x14000000


def _branch_displacement(instruction: int) -> int:
    """Decode B's signed imm26, scaled by four, relative to its own address."""
    immediate = instruction & 0x03FFFFFF
    if immediate & 0x02000000:
        immediate -= 0x04000000
    return immediate * 4


def validate_image(data: bytes) -> dict:
    """Return public header/entry details, or raise ValueError on an invalid Image.

    Linux normally starts with B, or its exact EFI MZ ADD/CCMP followed by B. A NOP
    followed by B also has unambiguous fall-through semantics. Do not scan for
    an arbitrary branch after an unknown or control-flow-changing code0.
    image_size describes memory requirements and need not equal the file size;
    in particular, post-link payloads may extend beyond it.
    """
    file_size = len(data)
    if file_size < HEADER_SIZE:
        raise ValueError(f'ARM64 Image header is truncated: {file_size} bytes (need 64)')
    if file_size > MAX_IMAGE_SIZE:
        raise ValueError(f'ARM64 Image exceeds the {MAX_IMAGE_SIZE}-byte size limit')
    if data[56:60] != ARM64_MAGIC:
        raise ValueError('Image has no ARM64 magic at header offset 56; expected a raw ARM64 Image')

    (code0, code1, text_offset, image_size, flags, reserved2, reserved3,
     reserved4, magic, pe_offset) = struct.unpack_from('<IIQQQQQQII', data)
    if flags & 1:
        raise ValueError('Big-endian ARM64 Image is unsupported; this build requires little endian')

    if _is_branch(code0):
        branch_offset, branch_instruction = 0, code0
    elif code0 in (EFI_MZ_ADD, EFI_SIGNATURE_NOP, NOP) and _is_branch(code1):
        branch_offset, branch_instruction = 4, code1
    else:
        raise ValueError(
            f'Unsupported ARM64 boot entry: code0=0x{code0:08x}, code1=0x{code1:08x}; '
            'expected B or EFI MZ ADD/CCMP/NOP followed by B'
        )

    branch_displacement = _branch_displacement(branch_instruction)
    branch_target = branch_offset + branch_displacement
    if branch_target % 4:
        raise ValueError(f'ARM64 entry branch target 0x{branch_target:x} is unaligned')
    if branch_target < HEADER_SIZE:
        raise ValueError(
            f'ARM64 entry branch at 0x{branch_offset:x} targets 0x{branch_target:x}, '
            'before executable payload (64-byte header)'
        )
    if branch_target + 4 > file_size:
        raise ValueError(
            f'ARM64 entry branch at 0x{branch_offset:x} targets 0x{branch_target:x} '
            f'outside Image file (size 0x{file_size:x}); target needs four bytes'
        )
    target_instruction, = struct.unpack_from('<I', data, branch_target)
    if target_instruction in (0, 0xFFFFFFFF):
        raise ValueError(
            f'ARM64 entry branch targets empty/erased instruction 0x{target_instruction:08x} '
            f'at 0x{branch_target:x}'
        )

    return {
        'file_size': file_size,
        'code0': code0,
        'code1': code1,
        'text_offset': text_offset,
        'image_size': image_size,
        'flags': flags,
        'magic': magic,
        'reserved2': reserved2,
        'reserved3': reserved3,
        'reserved4': reserved4,
        'pe_offset': pe_offset,
        'branch_offset': branch_offset,
        'branch_instruction': branch_instruction,
        'branch_displacement': branch_displacement,
        'branch_target': branch_target,
        'target_instruction': target_instruction,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path, help='raw ARM64 Image file')
    args = parser.parse_args(argv)
    try:
        with args.image.open('rb') as stream:
            # Bound the read even if a supplied file is too large or changes size.
            details = validate_image(stream.read(MAX_IMAGE_SIZE + 1))
    except (OSError, ValueError) as error:
        print(json.dumps({'valid': False, 'error': str(error)}))
        return 1
    print(json.dumps({'valid': True, **details}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
