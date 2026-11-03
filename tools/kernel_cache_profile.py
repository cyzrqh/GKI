"""Retain objects across one reviewed additive compiler-guard change.

Existing 5.x compiler/configuration inputs are unchanged. Kleaf can additionally
bind its proven native configuration, which changes the inner content key when
needed. Only this exact reviewed pair may retain the preceding outer namespace;
any future tool or binary-lock change automatically gets its own namespace.
"""
from __future__ import annotations

OLD_PROFILE = '217f51aaeb82d358b33c728205e4575476c9d41b984cf64f20641376e6e59b41'
DIAGNOSTIC_PROFILE = 'eac002baa24f8d66b9a9bb41238ce2325743a3c68828d4a14306b3cdc410ea8c'
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
