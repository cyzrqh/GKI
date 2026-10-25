"""Consistent numeric ordering for kernel targets and their public filenames."""
from __future__ import annotations

import re


def natural_key(value: object) -> tuple:
    """Compare version components as numbers, including 6.6 before 6.12."""
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r'(\d+)', str(value)) if part)


def kernel_key(document: dict) -> tuple:
    """Keep each numeric series ascending, with its unversioned LTS last."""
    sublevel = document.get('sub_level', document.get('sublevel', ''))
    return (natural_key(document.get('android_version', document.get('android', ''))),
            natural_key(document.get('kernel_version', document.get('kernel', ''))),
            str(sublevel).upper() == 'X', natural_key(sublevel),
            natural_key(document.get('os_patch_level', document.get('patch_level', ''))),
            natural_key(document.get('file', document.get('filename', ''))))
