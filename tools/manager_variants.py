"""Allowlisted manager identities shared by source, signing and packaging tools."""
from __future__ import annotations

import os
import re


MANAGERS = ('SukiSU', 'BakaSU')
_PROFILES = {
    'SukiSU': {'repository': 'SukiSU-Ultra/SukiSU-Ultra',
               'package': 'com.cyzrqh.gki.sukisu', 'apk_prefixes': ('SukiSU',)},
    'BakaSU': {'repository': 'Baka-SU/BakaSU',
               'package': 'com.cyzrqh.gki.bakasu', 'apk_prefixes': ('BakaSU', 'ReSukiSU')},
}


def manager_profile(variant: str = 'SukiSU') -> dict:
    if not isinstance(variant, str) or variant not in MANAGERS:
        raise ValueError('Unsupported paired manager variant')
    return dict(_PROFILES[variant])


def selected_variant(variant: str | None = None) -> str:
    selected = os.environ.get('KSU_VARIANT', 'SukiSU') if variant is None else variant
    manager_profile(selected)
    return selected


def valid_manager_apk_filename(name: str, variant: str = 'SukiSU', *,
                               allow_unsigned: bool = False) -> bool:
    profile = manager_profile(variant)
    prefixes = '|'.join(re.escape(prefix) for prefix in profile['apk_prefixes'])
    unsigned = r'(?:-unsigned)?' if allow_unsigned else ''
    return (isinstance(name, str) and len(name) <= 250 and '..' not in name
            and re.fullmatch(r'(?:' + prefixes + r')_[A-Za-z0-9][A-Za-z0-9._+-]*-release'
                             + unsigned + r'\.apk', name) is not None)
