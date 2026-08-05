"""Production MIM cost-enforcement adapter for usage ingestion."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from mim_control_plane.domain.models import (
    AuditEvent,
    AuditEventId,
    Schedule,
    ScheduleId,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import ScheduleState, WorkloadState
from mim_control_plane.ports.store import AlreadyExists, Store
from mim_control_plane.services.schedules import require_utc_datetime

_CENTRAL_PROJECT_ID = "mim-prod-123456"
_FAILED = "cost enforcement was denied."
_AUDIT_OUTCOME = "recorded"
_ORG_TARGET = "org:platform"
_USER_TARGET_PREFIX = "user:"
_WORKLOAD_TARGET_PREFIX = "workload:"
_SCHEDULE_TARGET_PREFIX = "schedule:"


class WorkloadAccessEffects(Protocol):
    def remove_owner_access(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        reason: str,
    ) -> None: ...


class ScheduleEffects(Protocol):
    def apply_schedule_state(
        self,
        *,
        schedule_id: ScheduleId,
        workload_id: WorkloadId,
        target_state: ScheduleState,
        expected_schedule_version: int,
        reason: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CostEnforcementAdapter:
    store: Store
    workload_access: WorkloadAccessEffects
    schedule_effects: ScheduleEffects
    project_id: str
    clock: Callable[[], datetime]

    def __post_init__(self) -> None:
        if self.project_id != _CENTRAL_PROJECT_ID:
            raise ValueError("cost enforcement project is invalid.")
        if not callable(getattr(self.store, "list_workloads", None)):
            raise ValueError("cost enforcement store is invalid.")
        if not callable(getattr(self.workload_access, "remove_owner_access", None)):
            raise ValueError("cost enforcement workload access is invalid.")
        if not callable(getattr(self.schedule_effects, "apply_schedule_state", None)):
            raise ValueError("cost enforcement schedule effects are invalid.")
        if not callable(self.clock):
            raise ValueError("cost enforcement clock is invalid.")

    def enforce_user_policy(
        self,
        *,
        user_id: UserId,
        user_percent: int,
        warn: bool,
        block_new: bool,
        pause: bool,
        basis_entry_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> None:
        normalized_user_id = _require_text(str(user_id), "user_id")
        normalized_basis = _normalize_basis_ids(basis_entry_ids)
        normalized_key = _require_text(idempotency_key, "idempotency_key")
        _require_user_flags(
            user_percent=user_percent,
            warn=warn,
            block_new=block_new,
            pause=pause,
        )
        if pause:
            self._append_exact_audit(
                action="cost_policy_pause",
                target_ref=f"{_USER_TARGET_PREFIX}{normalized_user_id}",
                policy_decision=_policy_decision(
                    scope="user",
                    level="pause",
                    percent=user_percent,
                    basis_entry_ids=normalized_basis,
                    idempotency_key=normalized_key,
                ),
                before_ref=None,
                after_ref=None,
            )
            self._pause_owner_resources(
                owner_id=UserId(normalized_user_id),
                reason="user_cost_pause",
                policy_scope="user",
                policy_level="pause",
                user_percent=user_percent,
                basis_entry_ids=normalized_basis,
                idempotency_key=normalized_key,
            )
            return
        if block_new:
            self._append_exact_audit(
                action="cost_policy_block_new",
                target_ref=f"{_USER_TARGET_PREFIX}{normalized_user_id}",
                policy_decision=_policy_decision(
                    scope="user",
                    level="block_new",
                    percent=user_percent,
                    basis_entry_ids=normalized_basis,
                    idempotency_key=normalized_key,
                ),
                before_ref=None,
                after_ref=None,
            )
            return
        self._append_exact_audit(
            action="cost_policy_warn",
            target_ref=f"{_USER_TARGET_PREFIX}{normalized_user_id}",
            policy_decision=_policy_decision(
                scope="user",
                level="warn",
                percent=user_percent,
                basis_entry_ids=normalized_basis,
                idempotency_key=normalized_key,
            ),
            before_ref=None,
            after_ref=None,
        )

    def enforce_org_policy(
        self,
        *,
        emergency_stop: bool,
        basis_entry_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> None:
        if type(emergency_stop) is not bool:
            raise ValueError("emergency_stop is invalid.")
        if not emergency_stop:
            return
        normalized_basis = _normalize_basis_ids(basis_entry_ids)
        normalized_key = _require_text(idempotency_key, "idempotency_key")
        self._append_exact_audit(
            action="cost_policy_org_emergency_stop",
            target_ref=_ORG_TARGET,
            policy_decision=_policy_decision(
                scope="org",
                level="emergency_stop",
                percent=None,
                basis_entry_ids=normalized_basis,
                idempotency_key=normalized_key,
            ),
            before_ref=None,
            after_ref=None,
        )
        self._pause_all_resources(
            reason="org_emergency_stop",
            policy_scope="org",
            policy_level="emergency_stop",
            user_percent=None,
            basis_entry_ids=normalized_basis,
            idempotency_key=normalized_key,
        )

    def _pause_owner_resources(
        self,
        *,
        owner_id: UserId,
        reason: str,
        policy_scope: str,
        policy_level: str,
        user_percent: int | None,
        basis_entry_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> None:
        workloads = tuple(
            sorted(
                self.store.list_workloads(owner_id=owner_id),
                key=lambda item: str(item.id),
            )
        )
        workload_ids = {item.id for item in workloads}
        schedules = tuple(
            sorted(
                self.store.list_schedules(owner_id=owner_id),
                key=lambda item: str(item.id),
            )
        )
        for workload in workloads:
            self._pause_workload_if_eligible(
                workload=workload,
                reason=reason,
                policy_scope=policy_scope,
                policy_level=policy_level,
                user_percent=user_percent,
                basis_entry_ids=basis_entry_ids,
                idempotency_key=idempotency_key,
            )
        for schedule in schedules:
            if schedule.workload_id not in workload_ids:
                raise RuntimeError(_FAILED)
            self._pause_schedule_if_eligible(
                schedule=schedule,
                workload_map={item.id: item for item in workloads},
                reason=reason,
                policy_scope=policy_scope,
                policy_level=policy_level,
                user_percent=user_percent,
                basis_entry_ids=basis_entry_ids,
                idempotency_key=idempotency_key,
            )

    def _pause_all_resources(
        self,
        *,
        reason: str,
        policy_scope: str,
        policy_level: str,
        user_percent: int | None,
        basis_entry_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> None:
        workloads = tuple(
            sorted(self.store.list_workloads(), key=lambda item: str(item.id))
        )
        workload_map = {item.id: item for item in workloads}
        schedules = tuple(
            sorted(self.store.list_schedules(), key=lambda item: str(item.id))
        )
        for workload in workloads:
            self._pause_workload_if_eligible(
                workload=workload,
                reason=reason,
                policy_scope=policy_scope,
                policy_level=policy_level,
                user_percent=user_percent,
                basis_entry_ids=basis_entry_ids,
                idempotency_key=idempotency_key,
            )
        for schedule in schedules:
            self._pause_schedule_if_eligible(
                schedule=schedule,
                workload_map=workload_map,
                reason=reason,
                policy_scope=policy_scope,
                policy_level=policy_level,
                user_percent=user_percent,
                basis_entry_ids=basis_entry_ids,
                idempotency_key=idempotency_key,
            )

    def _pause_workload_if_eligible(
        self,
        *,
        workload: Workload,
        reason: str,
        policy_scope: str,
        policy_level: str,
        user_percent: int | None,
        basis_entry_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> None:
        if workload.state is WorkloadState.ARCHIVED:
            return
        if workload.state is WorkloadState.PAUSED:
            return
        if workload.state is not WorkloadState.ACTIVE:
            return
        try:
            self.workload_access.remove_owner_access(
                workload_id=workload.id,
                expected_workload_version=workload.version,
                reason=reason,
            )
            paused = workload.transition_state(WorkloadState.PAUSED, at=self._now())
            self.store.save_workload(paused, expected_version=workload.version)
        except Exception:
            raise RuntimeError(_FAILED) from None
        self._append_exact_audit(
            action="cost_policy_pause_workload",
            target_ref=f"{_WORKLOAD_TARGET_PREFIX}{workload.id}",
            policy_decision=_policy_decision(
                scope=policy_scope,
                level=policy_level,
                percent=user_percent,
                basis_entry_ids=basis_entry_ids,
                idempotency_key=idempotency_key,
            ),
            before_ref=_state_ref(workload.state, workload.version),
            after_ref=_state_ref(WorkloadState.PAUSED, paused.version),
        )

    def _pause_schedule_if_eligible(
        self,
        *,
        schedule: Schedule,
        workload_map: dict[WorkloadId, Workload],
        reason: str,
        policy_scope: str,
        policy_level: str,
        user_percent: int | None,
        basis_entry_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> None:
        workload = workload_map.get(schedule.workload_id)
        if workload is None:
            raise RuntimeError(_FAILED)
        if schedule.owner_id != workload.owner_id:
            raise RuntimeError(_FAILED)
        if workload.state is WorkloadState.ARCHIVED:
            return
        if schedule.state is ScheduleState.ARCHIVED:
            return
        if schedule.state is ScheduleState.PAUSED:
            return
        if schedule.state is not ScheduleState.ENABLED:
            return
        try:
            self.schedule_effects.apply_schedule_state(
                schedule_id=schedule.id,
                workload_id=schedule.workload_id,
                target_state=ScheduleState.PAUSED,
                expected_schedule_version=schedule.version,
                reason=reason,
            )
            paused = schedule.transition_state(ScheduleState.PAUSED, at=self._now())
            self.store.save_schedule(paused, expected_version=schedule.version)
        except Exception:
            raise RuntimeError(_FAILED) from None
        self._append_exact_audit(
            action="cost_policy_pause_schedule",
            target_ref=f"{_SCHEDULE_TARGET_PREFIX}{schedule.id}",
            policy_decision=_policy_decision(
                scope=policy_scope,
                level=policy_level,
                percent=user_percent,
                basis_entry_ids=basis_entry_ids,
                idempotency_key=idempotency_key,
            ),
            before_ref=_state_ref(schedule.state, schedule.version),
            after_ref=_state_ref(ScheduleState.PAUSED, paused.version),
        )

    def _append_exact_audit(
        self,
        *,
        action: str,
        target_ref: str,
        policy_decision: str,
        before_ref: str | None,
        after_ref: str | None,
    ) -> None:
        occurred_at = self._now()
        event = AuditEvent(
            id=_audit_event_id(
                action,
                target_ref,
                policy_decision,
                before_ref,
                after_ref,
            ),
            actor_id=None,
            action=action,
            target_ref=target_ref,
            policy_decision=policy_decision,
            before_ref=before_ref,
            after_ref=after_ref,
            correlation_id=_audit_correlation_id(
                action=action,
                target_ref=target_ref,
                policy_decision=policy_decision,
            ),
            outcome=_AUDIT_OUTCOME,
            occurred_at=occurred_at,
        )
        try:
            self.store.append_audit_event(event)
            return
        except AlreadyExists:
            for existing in self.store.list_audit_events():
                if existing.id == event.id:
                    if not _same_audit_material(existing, event):
                        raise RuntimeError(_FAILED) from None
                    return
            raise RuntimeError(_FAILED) from None
        except Exception:
            raise RuntimeError(_FAILED) from None

    def _now(self) -> datetime:
        try:
            return require_utc_datetime(self.clock(), label="cost enforcement")
        except Exception:
            raise RuntimeError(_FAILED) from None


def _require_user_flags(
    *,
    user_percent: int,
    warn: bool,
    block_new: bool,
    pause: bool,
) -> None:
    if type(user_percent) is not int or user_percent < 0:
        raise ValueError("user_percent is invalid.")
    for field_name, value in (
        ("warn", warn),
        ("block_new", block_new),
        ("pause", pause),
    ):
        if type(value) is not bool:
            raise ValueError(f"{field_name} is invalid.")
    if pause and user_percent < 100:
        raise ValueError("pause policy is invalid.")
    if block_new and user_percent < 90:
        raise ValueError("block_new policy is invalid.")
    if warn and user_percent < 70:
        raise ValueError("warn policy is invalid.")
    if pause and not block_new:
        raise ValueError("pause policy is invalid.")
    if block_new and not warn:
        raise ValueError("block_new policy is invalid.")
    if not warn and not block_new and not pause:
        raise ValueError("user policy is invalid.")


def _same_audit_material(left: AuditEvent, right: AuditEvent) -> bool:
    return (
        left.id == right.id
        and left.actor_id == right.actor_id
        and left.action == right.action
        and left.target_ref == right.target_ref
        and left.policy_decision == right.policy_decision
        and left.before_ref == right.before_ref
        and left.after_ref == right.after_ref
        and left.correlation_id == right.correlation_id
        and left.outcome == right.outcome
    )


def _normalize_basis_ids(value: tuple[str, ...]) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise ValueError("basis_entry_ids is invalid.")
    normalized = tuple(sorted(_require_text(item, "basis_entry_id") for item in value))
    if len(set(normalized)) != len(normalized):
        raise ValueError("basis_entry_ids is invalid.")
    return normalized


def _policy_decision(
    *,
    scope: str,
    level: str,
    percent: int | None,
    basis_entry_ids: tuple[str, ...],
    idempotency_key: str,
) -> str:
    percent_part = "na" if percent is None else str(percent)
    return (
        f"scope={scope};level={level};percent={percent_part};"
        f"basis={_material_hash(*basis_entry_ids)};count={len(basis_entry_ids)};"
        f"idempotency={_material_hash(idempotency_key)}"
    )


def _state_ref(state: WorkloadState | ScheduleState, version: int) -> str:
    return f"{state}:{version}"


def _audit_event_id(
    action: str,
    target_ref: str,
    policy_decision: str,
    before_ref: str | None,
    after_ref: str | None,
) -> AuditEventId:
    return AuditEventId(
        "audit-cost-"
        + _material_hash(
            action,
            target_ref,
            policy_decision,
            before_ref or "",
            after_ref or "",
        )[:24]
    )


def _audit_correlation_id(
    *,
    action: str,
    target_ref: str,
    policy_decision: str,
) -> str:
    return "cost-corr-" + _material_hash(action, target_ref, policy_decision)[:24]


def _material_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"mim:cost-enforcement:v1\x00")
    for part in parts:
        if type(part) is not str:
            raise ValueError("material is invalid.")
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _require_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} is invalid.")
    return value


__all__ = ["CostEnforcementAdapter"]
