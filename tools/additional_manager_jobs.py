"""Keep every modified manager on the same-source APK/LKM signing pipeline."""
from __future__ import annotations

import copy

from manager_variants import LOCAL_MANAGERS, ADDITIONAL_MANAGERS


def variant_condition(variants):
    return '(' + ' || '.join("inputs.kernelsu_variant == '" + value + "'" for value in variants) + ')'


LOCAL_IF = variant_condition(LOCAL_MANAGERS)
ADDITIONAL_IF = variant_condition(ADDITIONAL_MANAGERS)
ALL_IF = '(' + LOCAL_IF + ' || ' + ADDITIONAL_IF + ')'


def extend_manager_job(original: dict) -> dict:
    job = copy.deepcopy(original)
    # All six now build the modified manager, daemon and paired LKMs. An
    # unchanged upstream APK can never contain the added native KPM page.
    job['if'] = ("always() && !cancelled() && needs.prepare-matrix.result == 'success' && "
                 + ALL_IF + " && needs.build-sukisu-lkm.result == 'success'")
    job['outputs']['manager_metadata'] = ''
    return job
