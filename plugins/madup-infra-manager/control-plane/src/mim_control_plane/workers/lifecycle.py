"""Private lifecycle worker orchestrating offboarding and inactivity effects."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from mim_control_plane.config import FINAL_IMAGE_RETENTION_DAYS
from mim_control_plane.domain.models import (
    LifecycleAction,
    LifecycleActionId,
    ScheduleId,
    SecretId,
    SecretMetadata,
    User,
    UserId,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    LifecycleActionKind,
    LifecycleActionState,
    ScheduleState,
    SecretLifecycleState,
    UserState,
    WorkloadState,
)
from mim_control_plane.ports.store import AlreadyExists, Store
from mim_control_plane.services.lifecycle import (
    AdminDecision,
    AdminDecisionKind,
    CleanupExecutionDecisionKind,
    PlannedLifecycleAction,
    plan_user_lifecycle,
    revalidate_cleanup_action,
)
from mim_control_plane.services.schedules import require_utc_datetime


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class SessionGate(Protocol):
    def deny_user_sessions(self, *, user_id: UserId, reason: str) -> None: ...


class AccessManager(Protocol):
    def remove_owner_access(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        reason: str,
    ) -> None: ...


class SecretBindingManager(Protocol):
    def remove_workload_bindings(
        self,
        *,
        workload_id: WorkloadId,
        secret_ids: tuple[SecretId, ...],
        expected_workload_version: int,
    ) -> None: ...


class SlackGrantManager(Protocol):
    def revoke_user_grant(self, *, user_id: UserId, reason: str) -> None: ...


class LifecycleNotifier(Protocol):
    def notify_admin(self, *, user_id: UserId, kind: str, reason: str) -> None: ...

    def notify_owner(
        self,
        *,
        user_id: UserId,
        workload_id: WorkloadId,
        reason: str,
    ) -> None: ...


class TransferManager(Protocol):
    def open_transfer_window(
        self,
        *,
        user_id: UserId,
        workload_ids: tuple[WorkloadId, ...],
        reason: str,
    ) -> None: ...


class ScheduleManager(Protocol):
    def apply_schedule_state(
        self,
        *,
        schedule_id: ScheduleId,
        workload_id: WorkloadId,
        target_state: ScheduleState,
        expected_schedule_version: int,
        reason: str,
    ) -> None: ...


class ComputeManager(Protocol):
    def delete_compute(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        target_kinds: tuple[str, ...],
        retain_image_until: datetime | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LifecycleReconcileResult:
    user: User
    planned_actions: tuple[PlannedLifecycleAction, ...]
    persisted_action_ids: tuple[LifecycleActionId, ...]


@dataclass(frozen=True, slots=True)
class LifecycleActionExecutionResult:
    kind: str
    action: LifecycleAction


@dataclass(frozen=True, slots=True)
class LifecycleWorker:
    store: Store
    sessions: SessionGate
    access: AccessManager
    secret_bindings: SecretBindingManager
    slack_grants: SlackGrantManager
    notifier: LifecycleNotifier
    transfer: TransferManager
    schedules: ScheduleManager
    compute: ComputeManager

    def reconcile_user(
        self,
        *,
        user_id: UserId,
        account_locked_at: datetime | None,
        holds: frozenset[WorkloadId],
        now: datetime,
    ) -> LifecycleReconcileResult:
        current_now = require_utc_datetime(now, label="lifecycle reconcile")
        user = self.store.get_user(user_id)
        workloads = self.store.list_workloads(owner_id=user_id)
        schedules = self.store.list_schedules(owner_id=user_id)
        decision = plan_user_lifecycle(
            user=user,
            workloads=workloads,
            schedules=schedules,
            holds=holds,
            account_locked_at=account_locked_at,
            now=current_now,
        )

        if user.state in {UserState.SUSPENDED, UserState.OFFBOARDED}:
            self.sessions.deny_user_sessions(
                user_id=user.id,
                reason=user.state.value,
            )

        for proposal in decision.workload_transitions:
            current_workload = self.store.get_workload(proposal.workload_id)
            if current_workload.state is proposal.target_state:
                continue
            self.access.remove_owner_access(
                workload_id=current_workload.id,
                expected_workload_version=current_workload.version,
                reason=user.state.value,
            )
            attached = self._attached_secrets(
                owner_id=user.id,
                workload_id=current_workload.id,
            )
            if attached:
                self.secret_bindings.remove_workload_bindings(
                    workload_id=current_workload.id,
                    secret_ids=tuple(secret.id for secret in attached),
                    expected_workload_version=current_workload.version,
                )
                for secret in attached:
                    self._lock_secret(secret=secret, at=current_now)
            updated_workload = current_workload.transition_state(
                proposal.target_state,
                at=current_now,
            )
            self.store.save_workload(
                updated_workload,
                expected_version=current_workload.version,
            )

        if user.state in {UserState.SUSPENDED, UserState.OFFBOARDED}:
            self.slack_grants.revoke_user_grant(
                user_id=user.id,
                reason=user.state.value,
            )

        for schedule_proposal in decision.schedule_transitions:
            current_schedule = self.store.get_schedule(schedule_proposal.schedule_id)
            if current_schedule.state is schedule_proposal.target_state:
                continue
            self.schedules.apply_schedule_state(
                schedule_id=current_schedule.id,
                workload_id=current_schedule.workload_id,
                target_state=schedule_proposal.target_state,
                expected_schedule_version=current_schedule.version,
                reason=schedule_proposal.reason,
            )
            self.store.save_schedule(
                current_schedule.transition_state(
                    schedule_proposal.target_state,
                    at=current_now,
                ),
                expected_version=current_schedule.version,
            )

        workload_ids = tuple(
            sorted((workload.id for workload in workloads), key=str)
        )
        for admin_decision in decision.admin_decisions:
            self._apply_admin_decision(
                user=user,
                decision=admin_decision,
                workload_ids=workload_ids,
            )

        persisted_ids: list[LifecycleActionId] = []
        planned_actions: list[PlannedLifecycleAction] = []
        for planned in decision.planned_actions:
            persisted = self._persist_action(planned)
            persisted_ids.append(persisted.action.id)
            planned_actions.append(persisted)

        refreshed = self.store.get_user(user.id)
        return LifecycleReconcileResult(
            user=refreshed,
            planned_actions=tuple(planned_actions),
            persisted_action_ids=tuple(persisted_ids),
        )

    def execute_planned_action(
        self,
        planned: PlannedLifecycleAction,
        *,
        user_id: UserId,
        holds: frozenset[WorkloadId],
        account_locked_at: datetime | None,
        now: datetime,
    ) -> LifecycleActionExecutionResult:
        current_now = require_utc_datetime(now, label="lifecycle execution")
        current_action = self.store.get_lifecycle_action(planned.action.id)
        if current_action.state is not LifecycleActionState.PLANNED:
            return LifecycleActionExecutionResult(
                kind=current_action.state.value,
                action=current_action,
            )
        self._validate_wrapper_if_current(
            planned=planned,
            user_id=user_id,
            holds=holds,
            account_locked_at=account_locked_at,
            now=current_now,
        )

        if planned.action.kind is LifecycleActionKind.INACTIVITY_WARNING:
            if current_now < planned.action.eligible_at:
                return LifecycleActionExecutionResult(
                    kind="noop",
                    action=current_action,
                )
            self.notifier.notify_owner(
                user_id=user_id,
                workload_id=planned.action.workload_id,
                reason=planned.action.reason,
            )
            executed = current_action.transition_state(
                LifecycleActionState.EXECUTED,
                at=current_now,
            )
            saved = self.store.save_lifecycle_action(
                executed,
                expected_version=current_action.version,
            )
            return LifecycleActionExecutionResult(kind="executed", action=saved)

        user = self.store.get_user(user_id)
        workload = self.store.get_workload(planned.action.workload_id)
        revalidated = revalidate_cleanup_action(
            planned,
            user=user,
            workload=workload,
            holds=holds,
            account_locked_at=account_locked_at,
            now=current_now,
        )
        if revalidated.kind is CleanupExecutionDecisionKind.NOOP:
            return LifecycleActionExecutionResult(kind="noop", action=current_action)
        if revalidated.kind is CleanupExecutionDecisionKind.CANCEL:
            cancelled = current_action.transition_state(
                LifecycleActionState.CANCELLED,
                at=current_now,
            )
            saved = self.store.save_lifecycle_action(
                cancelled,
                expected_version=current_action.version,
            )
            return LifecycleActionExecutionResult(kind="cancelled", action=saved)

        current_workload = self.store.get_workload(workload.id)
        self._archive_schedules(
            owner_id=user.id,
            workload_id=current_workload.id,
            at=current_now,
            reason=planned.action.reason,
        )
        self.compute.delete_compute(
            workload_id=current_workload.id,
            expected_workload_version=current_workload.version,
            target_kinds=tuple(
                target.kind.value for target in revalidated.compute_targets
            ),
            retain_image_until=current_now + timedelta(days=FINAL_IMAGE_RETENTION_DAYS),
        )
        if current_workload.state is not WorkloadState.ARCHIVED:
            archived = current_workload.transition_state(
                WorkloadState.ARCHIVED,
                at=current_now,
            )
            self.store.save_workload(
                archived,
                expected_version=current_workload.version,
            )
        executed = current_action.transition_state(
            LifecycleActionState.EXECUTED,
            at=current_now,
        )
        saved_action = self.store.save_lifecycle_action(
            executed,
            expected_version=current_action.version,
        )
        return LifecycleActionExecutionResult(kind="executed", action=saved_action)

    def _attached_secrets(
        self,
        *,
        owner_id: UserId,
        workload_id: WorkloadId,
    ) -> tuple[SecretMetadata, ...]:
        return tuple(
            secret
            for secret in self.store.list_secret_metadata(owner_id=owner_id)
            if workload_id in secret.attached_workload_ids
        )

    def _lock_secret(self, *, secret: SecretMetadata, at: datetime) -> None:
        if secret.lifecycle_state is not SecretLifecycleState.ACTIVE:
            return
        locked = secret.transition_lifecycle(SecretLifecycleState.LOCKED, at=at)
        self.store.save_secret_metadata(locked, expected_version=secret.version)

    def _apply_admin_decision(
        self,
        *,
        user: User,
        decision: AdminDecision,
        workload_ids: tuple[WorkloadId, ...],
    ) -> None:
        if decision.kind is AdminDecisionKind.TRANSFER_WINDOW:
            self.transfer.open_transfer_window(
                user_id=user.id,
                workload_ids=workload_ids,
                reason=decision.reason,
            )
            return
        self.notifier.notify_admin(
            user_id=user.id,
            kind=decision.kind.value,
            reason=decision.reason,
        )

    def _persist_action(
        self,
        planned: PlannedLifecycleAction,
    ) -> PlannedLifecycleAction:
        try:
            saved = self.store.create_lifecycle_action(planned.action)
        except AlreadyExists:
            saved = self.store.get_lifecycle_action(planned.action.id)
            if saved != planned.action:
                raise ValueError(
                    "stored lifecycle action conflicts with planned action."
                )
        return replace(planned, action=saved)

    def _archive_schedules(
        self,
        *,
        owner_id: UserId,
        workload_id: WorkloadId,
        at: datetime,
        reason: str,
    ) -> None:
        for current_schedule in self.store.list_schedules(owner_id=owner_id):
            if current_schedule.workload_id != workload_id:
                continue
            if current_schedule.state is ScheduleState.ARCHIVED:
                continue
            self.schedules.apply_schedule_state(
                schedule_id=current_schedule.id,
                workload_id=current_schedule.workload_id,
                target_state=ScheduleState.ARCHIVED,
                expected_schedule_version=current_schedule.version,
                reason=reason,
            )
            archived = current_schedule.transition_state(
                ScheduleState.ARCHIVED,
                at=at,
            )
            self.store.save_schedule(
                archived,
                expected_version=current_schedule.version,
            )

    def _validate_wrapper_if_current(
        self,
        *,
        planned: PlannedLifecycleAction,
        user_id: UserId,
        holds: frozenset[WorkloadId],
        account_locked_at: datetime | None,
        now: datetime,
    ) -> None:
        current_user = self.store.get_user(user_id)
        current_workloads = self.store.list_workloads(owner_id=user_id)
        current_schedules = self.store.list_schedules(owner_id=user_id)
        decision = plan_user_lifecycle(
            user=current_user,
            workloads=current_workloads,
            schedules=current_schedules,
            holds=holds,
            account_locked_at=account_locked_at,
            now=now,
        )
        current_match = next(
            (
                candidate
                for candidate in decision.planned_actions
                if candidate.action.id == planned.action.id
            ),
            None,
        )
        if current_match is not None and not self._same_planned_material(
            current_match,
            planned,
        ):
            raise ValueError("planned lifecycle wrapper is invalid.")

    @staticmethod
    def _same_planned_material(
        left: PlannedLifecycleAction,
        right: PlannedLifecycleAction,
    ) -> bool:
        return (
            left.action.id == right.action.id
            and left.action.workload_id == right.action.workload_id
            and left.action.kind == right.action.kind
            and left.action.reason == right.action.reason
            and left.action.eligible_at == right.action.eligible_at
            and left.action.observed_workload_version
            == right.action.observed_workload_version
            and left.compute_targets == right.compute_targets
            and left.cleanup_guard == right.cleanup_guard
        )


__all__ = [
    "LifecycleActionExecutionResult",
    "LifecycleReconcileResult",
    "LifecycleWorker",
]
