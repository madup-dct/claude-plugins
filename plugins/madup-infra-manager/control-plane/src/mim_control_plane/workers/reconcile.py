"""Private reconcile worker for bounded safe drift handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from mim_control_plane.domain.models import WorkloadId
from mim_control_plane.domain.states import WorkloadState
from mim_control_plane.ports.store import Store
from mim_control_plane.services.repair import (
    DriftObservation,
    RepairActionKind,
    RepairGateSnapshot,
    plan_drift_repair,
)
from mim_control_plane.services.schedules import require_utc_datetime


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class RuntimeReconciler(Protocol):
    def reconcile_runtime(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        expected_admission_version: int,
        fields: tuple[str, ...],
    ) -> None: ...


class AccessManager(Protocol):
    def remove_owner_access(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        reason: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ReconcileGateResolution:
    gates: RepairGateSnapshot
    expected_workload_version: int
    expected_admission_version: int


class ReconcileGateResolver(Protocol):
    def resolve(
        self,
        *,
        workload_id: WorkloadId,
        workload_version: int,
        admission_version: int,
        now: datetime,
    ) -> ReconcileGateResolution: ...


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    kind: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class ReconcileWorker:
    store: Store
    runtime: RuntimeReconciler
    access: AccessManager
    gates: ReconcileGateResolver

    def reconcile(
        self,
        *,
        workload_id: WorkloadId,
        drift: DriftObservation,
        now: datetime,
    ) -> ReconcileResult:
        current_now = require_utc_datetime(now, label="reconcile worker")
        workload = self.store.get_workload(workload_id)
        admission = self.store.get_repository_admission(
            workload.repository_admission_id
        )
        resolution = self.gates.resolve(
            workload_id=workload_id,
            workload_version=workload.version,
            admission_version=admission.version,
            now=current_now,
        )
        decision = plan_drift_repair(
            workload=workload,
            admission=admission,
            drift=drift,
            gates=resolution.gates,
        )
        if decision.kind is RepairActionKind.RECONCILE_RUNTIME:
            self.runtime.reconcile_runtime(
                workload_id=workload.id,
                expected_workload_version=resolution.expected_workload_version,
                expected_admission_version=resolution.expected_admission_version,
                fields=tuple(field.value for field in decision.reconcile_fields),
            )
            return ReconcileResult(
                kind=decision.kind.value,
                reason_code=decision.reason_code,
            )
        if decision.kind is RepairActionKind.QUARANTINE_ESCALATE:
            current_workload = self.store.get_workload(workload_id)
            self.access.remove_owner_access(
                workload_id=current_workload.id,
                expected_workload_version=current_workload.version,
                reason=decision.reason_code,
            )
            if current_workload.state is not WorkloadState.QUARANTINED:
                quarantined = current_workload.transition_state(
                    WorkloadState.QUARANTINED,
                    at=current_now,
                )
                self.store.save_workload(
                    quarantined,
                    expected_version=current_workload.version,
                )
            return ReconcileResult(
                kind=decision.kind.value,
                reason_code=decision.reason_code,
            )
        return ReconcileResult(
            kind=decision.kind.value,
            reason_code=decision.reason_code,
        )


__all__ = [
    "ReconcileGateResolution",
    "ReconcileResult",
    "ReconcileWorker",
]
