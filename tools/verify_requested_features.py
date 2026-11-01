"""Reject requested features missing from the actual Image's bound IKCONFIG."""
from __future__ import annotations

import json
import os

FEATURES = {
    'enable_susfs': ('CONFIG_KSU_SUSFS',),
    'use_kpm': ('CONFIG_KPM',),
    'use_zram': ('CONFIG_ZRAM',),
    'use_bbg': ('CONFIG_BBG',),
    'use_ddk': ('CONFIG_XINGGUANG_DDK', 'CONFIG_DDK'),
    'use_ntsync': ('CONFIG_NTSYNC',),
    'use_rekernel': ('CONFIG_REKERNEL',),
    'use_bbr': ('CONFIG_TCP_CONG_BBR',),
    'use_lz4kd': ('CONFIG_ZRAM_BACKEND_LZ4KD', 'CONFIG_CRYPTO_LZ4KD'),
}


def requests_from_environment() -> dict[str, bool]:
    requested = {}
    for name in (*FEATURES, 'use_networking'):
        value = os.environ.get('GKI_REQUEST_' + name.upper())
        if value is not None:
            if value not in ('true', 'false'):
                raise ValueError('Invalid requested feature flag: ' + name)
            requested[name] = value == 'true'
    # OPlus already carries a validated flags-only menu document. It contains
    # no signing credentials or KPM password; never include it in diagnostics.
    if not requested and (raw := os.environ.get('OPLUS_INPUTS')):
        if len(raw.encode()) > 16384:
            raise ValueError('Oversized OPlus requested-feature document')
        values = json.loads(raw)
        for name in FEATURES:
            if name in values:
                if type(values[name]) is not bool:
                    raise ValueError('Invalid requested feature flag: ' + name)
                requested[name] = values[name]
    return requested


def verify_requested(config: dict[str, str], requested: dict[str, bool]) -> None:
    for name, keys in FEATURES.items():
        if requested.get(name) and not any(config.get(key) == 'y' for key in keys):
            raise ValueError('菜单请求的功能未编入实际内核：' + name + '；需要 ' + ' / '.join(keys))
    if requested.get('use_networking'):
        for key in ('CONFIG_IP_SET', 'CONFIG_TCP_CONG_BBR'):
            if config.get(key) != 'y':
                raise ValueError('菜单请求的网络增强未编入实际内核：' + key)
