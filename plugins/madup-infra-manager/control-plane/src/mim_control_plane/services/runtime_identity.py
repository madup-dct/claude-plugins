"""Canonical deterministic identity material for one managed workload."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from mim_control_plane.config import _validate_project_id


@dataclass(frozen=True, slots=True)
class RuntimeIdentitySpec:
    account_id: str
    email: str
    name: str
    display_name: str
    description: str


def runtime_identity_spec(*, project_id: str, workload_id: str) -> RuntimeIdentitySpec:
    project = _validate_project_id(_require_exact_text(project_id, "project_id"))
    workload = _require_exact_text(workload_id, "workload_id")
    suffix = hashlib.sha256(workload.encode("utf-8")).hexdigest()[:12]
    account_id = f"mim-wrk-{suffix}"
    email = f"{account_id}@{project}.iam.gserviceaccount.com"
    return RuntimeIdentitySpec(
        account_id=account_id,
        email=email,
        name=f"projects/{project}/serviceAccounts/{email}",
        display_name=f"MIM workload {suffix}",
        description=(
            f"MIM managed runtime identity for workload {suffix}; "
            "no project roles."
        ),
    )


def _require_exact_text(value: str, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} must be exact non-empty text.")
    return value
