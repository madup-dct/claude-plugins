#!/usr/bin/env python3
"""Deterministic stratified sample selector for stress cases."""

from __future__ import annotations

import hashlib
from collections import defaultdict


DEFAULT_SAMPLE_SIZE = 96
SUBMODE_QUOTAS = {
    "slack": 28,
    "email": 14,
    "report": 24,
    "proposal": 20,
    "presentation": 10,
}


def _sort_key(case: dict[str, object]) -> tuple[str, str]:
    digest = hashlib.sha256(f"sample-v1:{case['id']}".encode("utf-8")).hexdigest()
    return digest, str(case["id"])


def select_cases(cases: list[dict[str, object]], size: int = DEFAULT_SAMPLE_SIZE) -> list[dict[str, object]]:
    if size != DEFAULT_SAMPLE_SIZE:
        raise ValueError("Only the tracked 96-case release sample is supported.")

    by_submode: dict[str, list[dict[str, object]]] = defaultdict(list)
    for case in cases:
        by_submode[str(case["submode"])].append(case)

    selected: list[dict[str, object]] = []
    for submode, quota in SUBMODE_QUOTAS.items():
        pool = sorted(by_submode[submode], key=_sort_key)
        by_stratum: dict[str, list[dict[str, object]]] = defaultdict(list)
        for case in pool:
            by_stratum[str(case["stratum"])].append(case)
        strata = sorted(by_stratum)
        stratum_index = 0
        while len([item for item in selected if item["submode"] == submode]) < quota:
            stratum = strata[stratum_index % len(strata)]
            bucket = by_stratum[stratum]
            if bucket:
                selected.append(bucket.pop(0))
            stratum_index += 1

    selected = sorted(selected, key=lambda case: str(case["id"]))
    return selected
