"""Pure repair policy decisions for bounded safe drift handling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from mim_control_plane.domain.models import RepositoryAdmission, Workload
from mim_control_plane.domain.states import (
    RepositoryAdmissionState,
    WorkloadKind,
    WorkloadState,
)

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPAIR_INPUT_ERROR = "repair input is invalid."
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class DriftComponent(StrEnum):
    RUNTIME_ENV = "runtime_env"
    LABELS = "labels"
    SCHEDULE_POLICY = "schedule_policy"
    RUNTIME_HEALTH = "runtime_health"
    IAM_POLICY = "iam_policy"
    AUTH_INGRESS = "auth_ingress"
    SERVICE_ACCOUNT = "service_account"
    SECRET_ATTACHMENT = "secret_attachment"
    BILLING_BOUNDARY = "billing_boundary"
    PROJECT_BOUNDARY = "project_boundary"
    REGION_BOUNDARY = "region_boundary"
    DATA_BOUNDARY = "data_boundary"
    BIGQUERY_BOUNDARY = "bigquery_boundary"
    VPC_BOUNDARY = "vpc_boundary"
    UNKNOWN = "unknown"


class SafeReconcileField(StrEnum):
    RUNTIME_ENV = "runtime_env"
    LABELS = "labels"
    SCHEDULE_POLICY = "schedule_policy"
    RUNTIME_HEALTH = "runtime_health"


class RepairActionKind(StrEnum):
    NOOP = "noop"
    DENY = "deny"
    RECONCILE_RUNTIME = "reconcile_runtime"
    RESTORE_SCHEDULE = "restore_schedule"
    ROLLBACK = "rollback"
    QUARANTINE_ESCALATE = "quarantine_escalate"


@dataclass(frozen=True, slots=True)
class DriftObservation:
    components: tuple[DriftComponent, ...]

    def __post_init__(self) -> None:
        if type(self.components) is not tuple:
            raise ValueError(_REPAIR_INPUT_ERROR)
        for component in self.components:
            if type(component) is not DriftComponent:
                raise ValueError(_REPAIR_INPUT_ERROR)


@dataclass(frozen=True, slots=True)
class RepairGateSnapshot:
    holds_clear: bool
    quota_clear: bool
    emergency_stop_clear: bool
    policy_clear: bool
    admission_current: bool
    workload_version_current: bool

    def __post_init__(self) -> None:
        for value in (
            self.holds_clear,
            self.quota_clear,
            self.emergency_stop_clear,
            self.policy_clear,
            self.admission_current,
            self.workload_version_current,
        ):
            if type(value) is not bool:
                raise ValueError(_REPAIR_INPUT_ERROR)


@dataclass(frozen=True, slots=True)
class RepairDecision:
    kind: RepairActionKind
    reconcile_fields: tuple[SafeReconcileField, ...] = ()
    rollback_digest: str | None = None
    reason_code: str = ""

    def __post_init__(self) -> None:
        if type(self.kind) is not RepairActionKind:
            raise ValueError(_REPAIR_INPUT_ERROR)
        if type(self.reconcile_fields) is not tuple:
            raise ValueError(_REPAIR_INPUT_ERROR)
        for field in self.reconcile_fields:
            if type(field) is not SafeReconcileField:
                raise ValueError(_REPAIR_INPUT_ERROR)
        if (
            type(self.reason_code) is not str
            or _REASON_CODE_PATTERN.fullmatch(self.reason_code) is None
        ):
            raise ValueError(_REPAIR_INPUT_ERROR)
        if self.kind in {
            RepairActionKind.NOOP,
            RepairActionKind.DENY,
            RepairActionKind.QUARANTINE_ESCALATE,
        }:
            if self.reconcile_fields or self.rollback_digest is not None:
                raise ValueError(_REPAIR_INPUT_ERROR)
            return
        if self.kind is RepairActionKind.RECONCILE_RUNTIME:
            if (
                not self.reconcile_fields
                or self.rollback_digest is not None
                or SafeReconcileField.SCHEDULE_POLICY in self.reconcile_fields
            ):
                raise ValueError(_REPAIR_INPUT_ERROR)
            return
        if self.kind is RepairActionKind.RESTORE_SCHEDULE:
            if (
                self.reconcile_fields != (SafeReconcileField.SCHEDULE_POLICY,)
                or self.rollback_digest is not None
            ):
                raise ValueError(_REPAIR_INPUT_ERROR)
            return
        if self.kind is RepairActionKind.ROLLBACK:
            if self.reconcile_fields or not _is_immutable_digest(self.rollback_digest):
                raise ValueError(_REPAIR_INPUT_ERROR)
            return
        raise ValueError(_REPAIR_INPUT_ERROR)


def plan_drift_repair(
    *,
    workload: Workload,
    admission: RepositoryAdmission,
    drift: DriftObservation,
    gates: RepairGateSnapshot,
) -> RepairDecision:
    _require_policy_inputs(
        workload=workload,
        admission=admission,
        gates=gates,
        drift=drift,
    )
    components = drift.components
    if not components:
        return RepairDecision(
            kind=RepairActionKind.NOOP,
            reason_code="no_drift_components",
        )
    if any(component in _UNSAFE_COMPONENTS for component in components):
        return RepairDecision(
            kind=RepairActionKind.QUARANTINE_ESCALATE,
            reason_code="unsafe_drift_detected",
        )
    if DriftComponent.UNKNOWN in components:
        return RepairDecision(
            kind=RepairActionKind.DENY,
            reason_code="unknown_drift_component",
        )
    if (
        DriftComponent.SCHEDULE_POLICY in components
        and workload.kind is not WorkloadKind.SCHEDULED_SCRIPT
    ):
        return RepairDecision(
            kind=RepairActionKind.QUARANTINE_ESCALATE,
            reason_code="schedule_drift_on_non_scheduled_workload",
        )
    if (
        workload.kind is WorkloadKind.SCHEDULED_SCRIPT
        and DriftComponent.SCHEDULE_POLICY in components
        and len(components) > 1
    ):
        return RepairDecision(
            kind=RepairActionKind.DENY,
            reason_code="mixed_schedule_and_runtime_drift",
        )
    if not _repair_gates_allow(
        workload=workload,
        admission=admission,
        gates=gates,
    ):
        return RepairDecision(
            kind=RepairActionKind.DENY,
            reason_code="repair_gate_blocked",
        )

    reconcile_fields = _reconcile_fields_for(components)
    if reconcile_fields == (SafeReconcileField.SCHEDULE_POLICY,):
        return RepairDecision(
            kind=RepairActionKind.RESTORE_SCHEDULE,
            reconcile_fields=reconcile_fields,
            reason_code="safe_schedule_restore",
        )
    return RepairDecision(
        kind=RepairActionKind.RECONCILE_RUNTIME,
        reconcile_fields=reconcile_fields,
        reason_code="safe_runtime_reconcile",
    )


def plan_redeploy(
    *,
    workload: Workload,
    admission: RepositoryAdmission,
    gates: RepairGateSnapshot,
) -> RepairDecision:
    _require_policy_inputs(
        workload=workload,
        admission=admission,
        gates=gates,
    )
    if not _repair_gates_allow(
        workload=workload,
        admission=admission,
        gates=gates,
    ):
        return RepairDecision(
            kind=RepairActionKind.DENY,
            reason_code="repair_gate_blocked",
        )
    if workload.source_sha != admission.admitted_sha:
        return RepairDecision(
            kind=RepairActionKind.DENY,
            reason_code="admitted_sha_mismatch",
        )
    return RepairDecision(
        kind=RepairActionKind.DENY,
        reason_code="verified_desired_state_required",
    )


def plan_rollback(
    *,
    workload: Workload,
    rollback_digest: str,
    gates: RepairGateSnapshot,
    admission: RepositoryAdmission,
) -> RepairDecision:
    _require_policy_inputs(
        workload=workload,
        admission=admission,
        gates=gates,
    )
    if not _repair_gates_allow(
        workload=workload,
        admission=admission,
        gates=gates,
    ):
        return RepairDecision(
            kind=RepairActionKind.DENY,
            reason_code="repair_gate_blocked",
        )
    recorded_digest = workload.last_healthy_image_digest
    if (
        not _is_immutable_digest(rollback_digest)
        or recorded_digest is None
        or not _is_immutable_digest(recorded_digest)
        or rollback_digest != recorded_digest
    ):
        return RepairDecision(
            kind=RepairActionKind.QUARANTINE_ESCALATE,
            reason_code="healthy_digest_invalid",
        )
    return RepairDecision(
        kind=RepairActionKind.ROLLBACK,
        rollback_digest=recorded_digest,
        reason_code="healthy_digest_rollback",
    )


_SAFE_COMPONENT_FIELDS: dict[DriftComponent, SafeReconcileField] = {
    DriftComponent.RUNTIME_ENV: SafeReconcileField.RUNTIME_ENV,
    DriftComponent.LABELS: SafeReconcileField.LABELS,
    DriftComponent.SCHEDULE_POLICY: SafeReconcileField.SCHEDULE_POLICY,
    DriftComponent.RUNTIME_HEALTH: SafeReconcileField.RUNTIME_HEALTH,
}

_UNSAFE_COMPONENTS = frozenset(
    {
        DriftComponent.IAM_POLICY,
        DriftComponent.AUTH_INGRESS,
        DriftComponent.SERVICE_ACCOUNT,
        DriftComponent.SECRET_ATTACHMENT,
        DriftComponent.BILLING_BOUNDARY,
        DriftComponent.PROJECT_BOUNDARY,
        DriftComponent.REGION_BOUNDARY,
        DriftComponent.DATA_BOUNDARY,
        DriftComponent.BIGQUERY_BOUNDARY,
        DriftComponent.VPC_BOUNDARY,
    }
)


def _reconcile_fields_for(
    components: tuple[DriftComponent, ...],
) -> tuple[SafeReconcileField, ...]:
    ordered_fields: list[SafeReconcileField] = []
    for component in (
        DriftComponent.RUNTIME_ENV,
        DriftComponent.LABELS,
        DriftComponent.SCHEDULE_POLICY,
        DriftComponent.RUNTIME_HEALTH,
    ):
        if component in components:
            ordered_fields.append(_SAFE_COMPONENT_FIELDS[component])
    return tuple(ordered_fields)


def _repair_gates_allow(
    *,
    workload: Workload,
    admission: RepositoryAdmission,
    gates: RepairGateSnapshot,
) -> bool:
    if not (
        gates.holds_clear
        and gates.quota_clear
        and gates.emergency_stop_clear
        and gates.policy_clear
        and gates.admission_current
        and gates.workload_version_current
    ):
        return False
    if workload.state not in {WorkloadState.ACTIVE, WorkloadState.FAILED}:
        return False
    return admission.state is RepositoryAdmissionState.ADMITTED


def _require_policy_inputs(
    *,
    workload: Workload,
    admission: RepositoryAdmission,
    gates: RepairGateSnapshot,
    drift: DriftObservation | None = None,
) -> None:
    if type(workload) is not Workload:
        raise ValueError(_REPAIR_INPUT_ERROR)
    if type(admission) is not RepositoryAdmission:
        raise ValueError(_REPAIR_INPUT_ERROR)
    if type(gates) is not RepairGateSnapshot:
        raise ValueError(_REPAIR_INPUT_ERROR)
    if drift is not None and type(drift) is not DriftObservation:
        raise ValueError(_REPAIR_INPUT_ERROR)


def _is_immutable_digest(value: object) -> bool:
    return type(value) is str and _DIGEST_PATTERN.fullmatch(value) is not None


__all__ = [
    "DriftComponent",
    "DriftObservation",
    "RepairActionKind",
    "RepairDecision",
    "RepairGateSnapshot",
    "SafeReconcileField",
    "plan_drift_repair",
    "plan_redeploy",
    "plan_rollback",
]
