"""Allowlisted manager identities shared by source, signing and packaging tools."""
from __future__ import annotations

import os
import re


LOCAL_MANAGERS = ('SukiSU', 'BakaSU')
ADDITIONAL_MANAGERS = ('KernelSU-Next', 'KernelSU', 'KowSU', 'FinaSU')
MANAGERS = LOCAL_MANAGERS + ADDITIONAL_MANAGERS
COMBINED_SELECTION = 'SukiSU + BakaSU + KernelSU-Next'
COMBINED_MANAGERS = ('SukiSU', 'BakaSU', 'KernelSU-Next')
MANAGER_SELECTIONS = MANAGERS + (COMBINED_SELECTION,)
# Actual Gradle application IDs, not Kotlin namespaces. BakaSU retains its
# upstream ReSukiSU ID; its current Kotlin namespace is org.bakasu.bakasu.
_PROFILES = {
    'SukiSU': {'repository': 'SukiSU-Ultra/SukiSU-Ultra',
               'package': 'com.sukisu.ultra', 'apk_prefixes': ('SukiSU',)},
    'BakaSU': {'repository': 'Baka-SU/BakaSU',
               'package': 'com.resukisu.resukisu', 'apk_prefixes': ('BakaSU', 'ReSukiSU')},
    'KernelSU-Next': {'repository': 'KernelSU-Next/KernelSU-Next',
                     'package': 'com.rifsxd.ksunext', 'apk_prefixes': ('KernelSU_Next',)},
    'KernelSU': {'repository': 'tiann/KernelSU',
                 'package': 'me.weishu.kernelsu', 'apk_prefixes': ('KernelSU',)},
    'KowSU': {'repository': 'KOWX712/KernelSU',
              'package': 'com.kowx712.supermanager', 'apk_prefixes': ('KernelSU', 'KowSU', 'SuperManager')},
    'FinaSU': {'repository': 'LyraVoid/FinaSU',
               'package': 'me.yuki.fina', 'apk_prefixes': ('FinaSU',)},
}


def manager_profile(variant: str = 'SukiSU') -> dict:
    if not isinstance(variant, str) or variant not in MANAGERS:
        raise ValueError('Unsupported paired manager variant')
    return dict(_PROFILES[variant])


def selected_variant(variant: str | None = None) -> str:
    selected = os.environ.get('KSU_VARIANT', 'SukiSU') if variant is None else variant
    manager_profile(selected)
    return selected


def selected_managers(selection: str) -> tuple[str, ...]:
    """Expand the menu once; every build still receives exactly one manager."""
    if selection == COMBINED_SELECTION:
        return COMBINED_MANAGERS
    manager_profile(selection)
    return (selection,)


def valid_manager_apk_filename(name: str, variant: str = 'SukiSU', *,
                               allow_unsigned: bool = False) -> bool:
    profile = manager_profile(variant)
    if variant != 'KernelSU-Next' and isinstance(name, str) and name.startswith('KernelSU_Next_'):
        return False
    prefixes = '|'.join(re.escape(prefix) for prefix in profile['apk_prefixes'])
    unsigned = r'(?:-unsigned)?' if allow_unsigned else ''
    return (isinstance(name, str) and len(name) <= 250 and '..' not in name
            and re.fullmatch(r'(?:' + prefixes + r')_[A-Za-z0-9][A-Za-z0-9._+-]*-release'
                             + unsigned + r'\.apk', name) is not None)
