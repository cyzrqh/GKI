"""Keep caches across one reviewed diagnostics-only compiler guard change.

Compiler/configuration identity and every eligibility check remain unchanged.
Only this exact reviewed pair of files may retain the preceding namespace;
any future tool or binary-lock change automatically gets its own namespace.
"""
from __future__ import annotations

OLD_PROFILE = '217f51aaeb82d358b33c728205e4575476c9d41b984cf64f20641376e6e59b41'
DIAGNOSTIC_PROFILE = '18a55a68598103729badbf0edab30807fc4adb71088129e4dc5667db1d31ec7e'
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
