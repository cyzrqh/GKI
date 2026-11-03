"""Retain objects across the exact reviewed compiler-guard correction.

The outer namespace remains compatible with existing objects. Dangling aliases
have explicit identities, native Kleaf configuration binds the content key,
and pinned modern ECS also hashes the live compiler through extra inputs so
learning an anchor cannot change the compiler's contribution to that key.
Only this exact helper and binary lock may retain the preceding namespace;
future changes automatically receive their own namespace.
"""
from __future__ import annotations

OLD_PROFILE = '217f51aaeb82d358b33c728205e4575476c9d41b984cf64f20641376e6e59b41'
DIAGNOSTIC_PROFILE = '11bef0ef2fd71957605f5fe2268979d0309489a140c5b30b0c1287e9c1a65afc'
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
