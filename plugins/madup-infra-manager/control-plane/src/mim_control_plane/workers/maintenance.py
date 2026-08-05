"""Deterministic maintenance orchestration over persisted users and jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from mim_control_plane.domain.models import LifecycleAction, User, UserId, WorkloadId
from mim_control_plane.domain.states import UserState
from mim_control_plane.ports.store import Store
from mim_control_plane.services.lifecycle import (
    PlannedLifecycleAction,
    plan_user_lifecycle,
)
from mim_control_plane.services.schedules import require_utc_datetime
from mim_control_plane.workers.lifecycle import (
    LifecycleActionExecutionResult,
    LifecycleReconcileResult,
)


class HoldResolver(Protocol):
    def resolve_holds(
        self,
        *,
        user_id: UserId,
        now: datetime,
    ) -> frozenset[WorkloadId]: ...


class LifecycleMaintenanceWorker(Protocol):
    def reconcile_user(
        self,
        *,
        user_id: UserId,
        account_locked_at: datetime | None,
        holds: frozenset[WorkloadId],
        now: datetime,
    ) -> LifecycleReconcileResult: ...

    def execute_planned_action(
        self,
        planned: PlannedLifecycleAction,
        *,
        user_id: UserId,
        holds: frozenset[WorkloadId],
        account_locked_at: datetime | None,
        now: datetime,
    ) -> LifecycleActionExecutionResult: ...


@dataclass(frozen=True, slots=True)
class MaintenanceSweepResult:
    processed_users: int
    failed_users: int
    replayed_users: int
    replayed_actions: int
    executed_actions: int
    noop_actions: int
    cancelled_actions: int


@dataclass(frozen=True, slots=True)
class MaintenanceSweep:
    store: Store
    lifecycle: LifecycleMaintenanceWorker
    hold_resolver: HoldResolver

    def run(self, *, now: datetime) -> MaintenanceSweepResult:
        current_now = require_utc_datetime(now, label="maintenance sweep")
        processed_users = 0
        failed_users = 0
        replayed_users = 0
        replayed_actions = 0
        executed_actions = 0
        noop_actions = 0
        cancelled_actions = 0

        for user in sorted(self.store.list_users(), key=lambda item: str(item.id)):
            try:
                holds = self._resolve_holds(user_id=user.id, now=current_now)
                account_locked_at = self._account_locked_at(user)
                planned_actions, replayed = self._reconcile_user(
                    user=user,
                    holds=holds,
                    account_locked_at=account_locked_at,
                    now=current_now,
                )
                processed_users += 1
                if replayed:
                    replayed_users += 1
                    replayed_actions += len(planned_actions)
                for planned in planned_actions:
                    execution = self.lifecycle.execute_planned_action(
                        planned,
                        user_id=user.id,
                        holds=holds,
                        account_locked_at=account_locked_at,
                        now=current_now,
                    )
                    if execution.kind == "executed":
                        executed_actions += 1
                    elif execution.kind == "noop":
                        noop_actions += 1
                    elif execution.kind == "cancelled":
                        cancelled_actions += 1
            except Exception:
                failed_users += 1

        return MaintenanceSweepResult(
            processed_users=processed_users,
            failed_users=failed_users,
            replayed_users=replayed_users,
            replayed_actions=replayed_actions,
            executed_actions=executed_actions,
            noop_actions=noop_actions,
            cancelled_actions=cancelled_actions,
        )

    def _reconcile_user(
        self,
        *,
        user: User,
        holds: frozenset[WorkloadId],
        account_locked_at: datetime | None,
        now: datetime,
    ) -> tuple[tuple[PlannedLifecycleAction, ...], bool]:
        try:
            reconcile = self.lifecycle.reconcile_user(
                user_id=user.id,
                account_locked_at=account_locked_at,
                holds=holds,
                now=now,
            )
            return reconcile.planned_actions, False
        except ValueError:
            recovered = self._recover_replayed_actions(
                user=user,
                holds=holds,
                account_locked_at=account_locked_at,
                now=now,
            )
            if recovered is None:
                raise
            return recovered, True

    def _recover_replayed_actions(
        self,
        *,
        user: User,
        holds: frozenset[WorkloadId],
        account_locked_at: datetime | None,
        now: datetime,
    ) -> tuple[PlannedLifecycleAction, ...] | None:
        current_user = self.store.get_user(user.id)
        workloads = self.store.list_workloads(owner_id=user.id)
        schedules = self.store.list_schedules(owner_id=user.id)
        planned_actions = plan_user_lifecycle(
            user=current_user,
            workloads=workloads,
            schedules=schedules,
            holds=holds,
            account_locked_at=account_locked_at,
            now=now,
        ).planned_actions
        if not planned_actions:
            return None
        for planned in planned_actions:
            existing = self.store.get_lifecycle_action(planned.action.id)
            if not self._same_action_material(existing, planned.action):
                return None
        return planned_actions

    def _resolve_holds(
        self,
        *,
        user_id: UserId,
        now: datetime,
    ) -> frozenset[WorkloadId]:
        holds = self.hold_resolver.resolve_holds(user_id=user_id, now=now)
        if not isinstance(holds, frozenset):
            raise ValueError("hold resolver returned invalid holds.")
        for item in holds:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("hold resolver returned invalid holds.")
        return holds

    @staticmethod
    def _account_locked_at(user: User) -> datetime | None:
        if user.state in {UserState.SUSPENDED, UserState.OFFBOARDED}:
            return user.updated_at
        return None

    @staticmethod
    def _same_action_material(
        existing: LifecycleAction,
        planned: LifecycleAction,
    ) -> bool:
        return (
            existing.id == planned.id
            and existing.workload_id == planned.workload_id
            and existing.kind == planned.kind
            and existing.reason == planned.reason
            and existing.eligible_at == planned.eligible_at
            and existing.observed_workload_version
            == planned.observed_workload_version
        )


__all__ = [
    "HoldResolver",
    "LifecycleMaintenanceWorker",
    "MaintenanceSweep",
    "MaintenanceSweepResult",
]
