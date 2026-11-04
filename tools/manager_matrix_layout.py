"""Expand only selected managers; bound each GitHub matrix to 256 jobs."""
from __future__ import annotations

import argparse
import json
import os

from manager_variants import selected_managers


def expand(matrix: dict, selection: str, resolution: dict | None = None) -> list[dict]:
    if resolution is None:
        variants = selected_managers(selection)
    else:
        from resolve_manager_selection import active_managers
        variants = active_managers(selection, resolution)
    if not isinstance(matrix, dict) or set(matrix) != {'include'} or not isinstance(matrix['include'], list):
        raise ValueError('Expected a verified include-only build matrix')
    rows = matrix['include']
    if any(not isinstance(row, dict) or 'variant' in row for row in rows):
        raise ValueError('A native matrix must not supply a manager routing field')
    return [{**row, 'variant': variant} for variant in variants for row in rows]


def layout(kernel: dict, ddk: dict, selection: str, resolution: dict | None = None) -> dict:
    targets = expand(kernel, selection, resolution)
    modules = expand(ddk, selection, resolution)
    if not modules or len(modules) > 256 or len(targets) > 512:
        raise ValueError('Selected build matrix exceeds the reviewed job limits')
    # Empty fallback matrices are never executed: their job-level gates run
    # before matrix evaluation. They still must be syntactically valid.
    return {
        'kernel_primary': {'include': targets[:256] or [{}]},
        'kernel_overflow': {'include': targets[256:] or [{}]},
        'overflow_count': str(max(0, len(targets) - 256)),
        'ddk_build_matrix': {'include': modules},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = layout(json.loads(os.environ['KERNEL_MATRIX'] or '{"include":[]}'),
                    json.loads(os.environ['DDK_MATRIX']), os.environ['MANAGER_SELECTION'],
                    json.loads(os.environ['MANAGER_RESOLUTION']) if os.environ.get('MANAGER_RESOLUTION') else None)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        for key, value in result.items():
            output.write(key + '=' + (value if isinstance(value, str) else json.dumps(value, separators=(',', ':'))) + '\n')


if __name__ == '__main__':
    main()
