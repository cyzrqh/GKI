"""Retain objects across one reviewed additive compiler-guard change.

Existing successful compiler identities are unchanged; missing compiler aliases
now have an explicit missing-target identity. Kleaf additionally binds its proven
native configuration, changing the inner content key when needed. Only this
exact reviewed pair may retain the preceding outer namespace;
any future tool or binary-lock change automatically gets its own namespace.
"""
from __future__ import annotations

OLD_PROFILE = '217f51aaeb82d358b33c728205e4575476c9d41b984cf64f20641376e6e59b41'
DIAGNOSTIC_PROFILE = '5306047305f470ea63e721213c646469476f9e333cebeb2a7352a62dd4b5a1e3'
HASH = "hashFiles('config/kernel-cache-tools.json', 'tools/kernel_compile_cache.py')"
ORIGINAL = '${{ ' + HASH + ' }}'


def profile_expression() -> str:
    return '${{ ' + HASH + " == '" + DIAGNOSTIC_PROFILE + "' && '" + OLD_PROFILE + "' || " + HASH + ' }}'


def wire_profile_alias(job: dict) -> None:
    def rewrite(value):
        if isinstance(value, str):
            return value.replace(ORIGINAL, profile_expression())
        if isinstance(value, list):
            return [rewrite(child) for child in value]
        if isinstance(value, dict):
            return {key: rewrite(child) for key, child in value.items()}
        return value
    updated = rewrite(job)
    job.clear()
    job.update(updated)
