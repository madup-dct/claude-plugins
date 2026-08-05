"""Pure lifecycle policy decisions for offboarding and inactivity cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from mim_control_plane.config import (
    CLEANUP_DAYS,
    INACTIVITY_WARNING_DAYS,
    TRANSFER_GRACE_DAYS,
)
from mim_control_plane.domain.models import (
    LifecycleAction,
    LifecycleActionId,
    Schedule,
    ScheduleId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    LifecycleActionKind,
    LifecycleActionState,
    ScheduleState,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.services.schedules import require_utc_datetime

_OFFBOARD_CLEANUP_DELAY = timedelta(days=TRANSFER_GRACE_DAYS)
_INACTIVITY_WARNING_DELAY = timedelta(days=INACTIVITY_WARNING_DAYS)
_INACTIVITY_CLEANUP_DELAY = timedelta(days=CLEANUP_DAYS)


class ComputeTargetKind(StrEnum):
    CLOUD_RUN_SERVICE = "cloud_run_service"
    CLOUD_RUN_JOB = "cloud_run_job"
    CLOUD_SCHEDULER_JOB = "cloud_scheduler_job"


class AdminDecisionKind(StrEnum):
    NOTIFY_ADMIN = "notify_admin"
    TRANSFER_WINDOW = "transfer_window"


class CleanupExecutionDecisionKind(StrEnum):
    NOOP = "noop"
    EXECUTE = "execute"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class ComputeTarget:
    workload_id: WorkloadId
    kind: ComputeTargetKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ComputeTargetKind):
            raise ValueError("compute target kind is invalid.")


@dataclass(frozen=True, slots=True)
class WorkloadTransitionProposal:
    workload_id: WorkloadId
    target_state: WorkloadState
    reason: str


@dataclass(frozen=True, slots=True)
class ScheduleTransitionProposal:
    schedule_id: ScheduleId
    workload_id: WorkloadId
    target_state: ScheduleState
    reason: str


@dataclass(frozen=True, slots=True)
class AdminDecision:
    user_id: UserId
    kind: AdminDecisionKind
    eligible_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AdminDecisionKind):
            raise ValueError("admin decision kind is invalid.")
        require_utc_datetime(self.eligible_at, label="admin decision")


@dataclass(frozen=True, slots=True)
class CleanupGuard:
    planned_user_id: UserId
    expected_workload_owner_id: UserId
    user_state: UserState
    user_version: int
    user_updated_at: datetime
    workload_state: WorkloadState
    workload_version: int
    workload_updated_at: datetime
    observed_last_activity_at: datetime | None
    status_anchor_at: datetime | None = None
    inactivity_anchor: datetime | None = None

    def __post_init__(self) -> None:
        require_utc_datetime(self.user_updated_at, label="cleanup guard")
        require_utc_datetime(self.workload_updated_at, label="cleanup guard")
        if self.observed_last_activity_at is not None:
            require_utc_datetime(
                self.observed_last_activity_at,
                label="cleanup guard",
            )
        if self.status_anchor_at is not None:
            require_utc_datetime(self.status_anchor_at, label="cleanup guard")
        if self.inactivity_anchor is not None:
            require_utc_datetime(self.inactivity_anchor, label="cleanup guard")


@dataclass(frozen=True, slots=True)
class PlannedLifecycleAction:
    action: LifecycleAction
    compute_targets: tuple[ComputeTarget, ...] = ()
    cleanup_guard: CleanupGuard | None = None

    def __post_init__(self) -> None:
        if self.action.kind is LifecycleActionKind.DELETE_COMPUTE:
            if not self.compute_targets:
                raise ValueError("cleanup targets are required.")
            if self.cleanup_guard is None:
                raise ValueError("cleanup guard is required.")
        elif self.compute_targets:
            raise ValueError("non-cleanup actions cannot carry cleanup targets.")


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    workload_transitions: tuple[WorkloadTransitionProposal, ...]
    schedule_transitions: tuple[ScheduleTransitionProposal, ...]
    admin_decisions: tuple[AdminDecision, ...]
    planned_actions: tuple[PlannedLifecycleAction, ...]


@dataclass(frozen=True, slots=True)
class CleanupExecutionDecision:
    kind: CleanupExecutionDecisionKind
    action: LifecycleAction
    compute_targets: tuple[ComputeTarget, ...]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CleanupExecutionDecisionKind):
            raise ValueError("cleanup execution decision is invalid.")


def plan_user_lifecycle(
    *,
    user: User,
    workloads: tuple[Workload, ...],
    schedules: tuple[Schedule, ...],
    holds: frozenset[WorkloadId],
    account_locked_at: datetime | None = None,
    now: datetime,
) -> LifecycleDecision:
    now = require_utc_datetime(now, label="lifecycle planning")
    holds_valid = _holds_are_valid(holds)
    account_locked = user.state in {UserState.SUSPENDED, UserState.OFFBOARDED}
    locked_anchor = _valid_status_anchor(account_locked_at, now=now)
    workload_transitions: list[WorkloadTransitionProposal] = []
    schedule_transitions: list[ScheduleTransitionProposal] = []
    planned_actions: list[PlannedLifecycleAction] = []

    if account_locked and workloads:
        admin_decisions: tuple[AdminDecision, ...] = (
            AdminDecision(
                user_id=user.id,
                kind=AdminDecisionKind.NOTIFY_ADMIN,
                eligible_at=now,
                reason=f"{user.state.value}_notification",
            ),
            AdminDecision(
                user_id=user.id,
                kind=AdminDecisionKind.TRANSFER_WINDOW,
                eligible_at=now,
                reason=f"{user.state.value}_transfer_window",
            ),
        )
    else:
        admin_decisions = ()

    for workload in workloads:
        workload_schedules = tuple(
            schedule for schedule in schedules if schedule.workload_id == workload.id
        )
        if account_locked:
            _append_account_lock_decisions(
                workload_transitions=workload_transitions,
                schedule_transitions=schedule_transitions,
                planned_actions=planned_actions,
                user=user,
                workload=workload,
                schedules=workload_schedules,
                holds=holds,
                holds_valid=holds_valid,
                account_locked_at=locked_anchor,
                now=now,
            )
            continue
        planned_actions.extend(
            _plan_inactivity_actions(
                user=user,
                workload=workload,
                holds=holds,
                holds_valid=holds_valid,
                now=now,
            )
        )

    return LifecycleDecision(
        workload_transitions=tuple(workload_transitions),
        schedule_transitions=tuple(schedule_transitions),
        admin_decisions=admin_decisions,
        planned_actions=tuple(planned_actions),
    )


def revalidate_cleanup_action(
    planned: PlannedLifecycleAction,
    *,
    user: User,
    workload: Workload,
    holds: frozenset[WorkloadId],
    account_locked_at: datetime | None = None,
    now: datetime,
) -> CleanupExecutionDecision:
    now = require_utc_datetime(now, label="cleanup revalidation")
    holds_valid = _holds_are_valid(holds)
    if planned.action.kind is not LifecycleActionKind.DELETE_COMPUTE:
        raise ValueError("cleanup revalidation requires a cleanup action.")
    if planned.cleanup_guard is None:
        raise ValueError("cleanup action is missing its guard.")
    if now < planned.action.eligible_at:
        return CleanupExecutionDecision(
            kind=CleanupExecutionDecisionKind.NOOP,
            action=planned.action,
            compute_targets=(),
            reason="cleanup_not_yet_eligible",
        )

    invalid_reason = _invalid_cleanup_reason(
        planned=planned,
        user=user,
        workload=workload,
        holds=holds,
        holds_valid=holds_valid,
        account_locked_at=_valid_status_anchor(account_locked_at, now=now),
    )
    if invalid_reason is not None:
        cancelled = planned.action
        if planned.action.state is LifecycleActionState.PLANNED:
            cancelled = planned.action.transition_state(
                LifecycleActionState.CANCELLED,
                at=now,
            )
        return CleanupExecutionDecision(
            kind=CleanupExecutionDecisionKind.CANCEL,
            action=cancelled,
            compute_targets=(),
            reason=invalid_reason,
        )
    return CleanupExecutionDecision(
        kind=CleanupExecutionDecisionKind.EXECUTE,
        action=planned.action,
        compute_targets=planned.compute_targets,
        reason="cleanup_execute",
    )


def _append_account_lock_decisions(
    *,
    workload_transitions: list[WorkloadTransitionProposal],
    schedule_transitions: list[ScheduleTransitionProposal],
    planned_actions: list[PlannedLifecycleAction],
    user: User,
    workload: Workload,
    schedules: tuple[Schedule, ...],
    holds: frozenset[WorkloadId],
    holds_valid: bool,
    account_locked_at: datetime | None,
    now: datetime,
) -> None:
    if workload.state not in {WorkloadState.QUARANTINED, WorkloadState.ARCHIVED}:
        workload_transitions.append(
            WorkloadTransitionProposal(
                workload_id=workload.id,
                target_state=WorkloadState.QUARANTINED,
                reason=f"{user.state.value}_quarantine",
            )
        )
    for schedule in schedules:
        if schedule.state not in {
            ScheduleState.DISABLED,
            ScheduleState.ARCHIVED,
            ScheduleState.QUARANTINED,
        }:
            schedule_transitions.append(
                ScheduleTransitionProposal(
                    schedule_id=schedule.id,
                    workload_id=workload.id,
                    target_state=ScheduleState.DISABLED,
                    reason=f"{user.state.value}_disable_schedule",
                )
            )

    if workload.state is WorkloadState.ARCHIVED:
        return
    if not holds_valid or account_locked_at is None:
        return
    eligible_at = account_locked_at + _OFFBOARD_CLEANUP_DELAY
    if (
        workload.id in holds
        or now < eligible_at
        or workload.state is not WorkloadState.QUARANTINED
    ):
        return
    planned_actions.append(
        PlannedLifecycleAction(
            action=LifecycleAction(
                id=_cleanup_action_id(
                    workload=workload,
                    kind=LifecycleActionKind.DELETE_COMPUTE,
                    eligible_at=eligible_at,
                    discriminator=f"locked-{_anchor_stamp(account_locked_at)}",
                ),
                workload_id=workload.id,
                kind=LifecycleActionKind.DELETE_COMPUTE,
                state=LifecycleActionState.PLANNED,
                reason=f"{user.state.value}_7d_quarantined",
                eligible_at=eligible_at,
                observed_workload_version=workload.version,
                created_at=now,
                updated_at=now,
            ),
            compute_targets=_compute_targets_for(workload),
            cleanup_guard=CleanupGuard(
                planned_user_id=user.id,
                expected_workload_owner_id=workload.owner_id,
                user_state=user.state,
                user_version=user.version,
                user_updated_at=user.updated_at,
                workload_state=workload.state,
                workload_version=workload.version,
                workload_updated_at=workload.updated_at,
                observed_last_activity_at=workload.last_activity_at,
                status_anchor_at=account_locked_at,
            ),
        )
    )


def _plan_inactivity_actions(
    *,
    user: User,
    workload: Workload,
    holds: frozenset[WorkloadId],
    holds_valid: bool,
    now: datetime,
) -> tuple[PlannedLifecycleAction, ...]:
    if workload.state is WorkloadState.ARCHIVED:
        return ()
    anchor = workload.last_activity_at or workload.created_at
    warning_at = anchor + _INACTIVITY_WARNING_DELAY
    cleanup_at = anchor + _INACTIVITY_CLEANUP_DELAY

    if now >= cleanup_at:
        if not holds_valid or workload.id in holds:
            return ()
        return (
            PlannedLifecycleAction(
                action=LifecycleAction(
                    id=_cleanup_action_id(
                        workload=workload,
                        kind=LifecycleActionKind.DELETE_COMPUTE,
                        eligible_at=cleanup_at,
                        discriminator=f"inactivity-v{workload.version}",
                    ),
                    workload_id=workload.id,
                    kind=LifecycleActionKind.DELETE_COMPUTE,
                    state=LifecycleActionState.PLANNED,
                    reason="30_days_inactive",
                    eligible_at=cleanup_at,
                    observed_workload_version=workload.version,
                    created_at=now,
                    updated_at=now,
                ),
                compute_targets=_compute_targets_for(workload),
                cleanup_guard=CleanupGuard(
                    planned_user_id=user.id,
                    expected_workload_owner_id=workload.owner_id,
                    user_state=user.state,
                    user_version=user.version,
                    user_updated_at=user.updated_at,
                    workload_state=workload.state,
                    workload_version=workload.version,
                    workload_updated_at=workload.updated_at,
                    observed_last_activity_at=workload.last_activity_at,
                    inactivity_anchor=anchor,
                ),
            ),
        )
    if now >= warning_at:
        return (
            PlannedLifecycleAction(
                action=LifecycleAction(
                    id=_cleanup_action_id(
                        workload=workload,
                        kind=LifecycleActionKind.INACTIVITY_WARNING,
                        eligible_at=warning_at,
                        discriminator="warning",
                    ),
                    workload_id=workload.id,
                    kind=LifecycleActionKind.INACTIVITY_WARNING,
                    state=LifecycleActionState.PLANNED,
                    reason="23_days_inactive",
                    eligible_at=warning_at,
                    observed_workload_version=workload.version,
                    created_at=now,
                    updated_at=now,
                ),
            ),
        )
    return ()


def _invalid_cleanup_reason(
    *,
    planned: PlannedLifecycleAction,
    user: User,
    workload: Workload,
    holds: frozenset[WorkloadId],
    holds_valid: bool,
    account_locked_at: datetime | None,
) -> str | None:
    guard = planned.cleanup_guard
    if guard is None:
        return "cleanup_missing_guard"
    if not holds_valid:
        return "cleanup_invalid_hold_input"
    if workload.id in holds:
        return "cleanup_hold_present"
    if planned.action.workload_id != workload.id:
        return "cleanup_wrong_workload"
    if user.id != guard.planned_user_id:
        return "cleanup_wrong_user"
    if workload.owner_id != guard.expected_workload_owner_id:
        return "cleanup_owner_changed"
    if user.id != workload.owner_id:
        return "cleanup_owner_relation_changed"
    if user.version != guard.user_version or user.updated_at != guard.user_updated_at:
        return "cleanup_stale_user"
    if user.state != guard.user_state:
        return "cleanup_user_state_changed"
    if workload.version != guard.workload_version:
        return "cleanup_stale_workload"
    if workload.updated_at != guard.workload_updated_at:
        return "cleanup_workload_timestamp_changed"
    if workload.state != guard.workload_state:
        return "cleanup_workload_state_changed"
    if workload.last_activity_at != guard.observed_last_activity_at:
        return "cleanup_new_activity"
    if guard.status_anchor_at is not None:
        if account_locked_at != guard.status_anchor_at:
            return "cleanup_status_anchor_changed"
        if user.state not in {UserState.SUSPENDED, UserState.OFFBOARDED}:
            return "cleanup_user_reactivated"
    if guard.inactivity_anchor is not None:
        if (
            workload.last_activity_at or workload.created_at
        ) != guard.inactivity_anchor:
            return "cleanup_new_activity"
    return None


def _compute_targets_for(workload: Workload) -> tuple[ComputeTarget, ...]:
    if workload.kind is WorkloadKind.SCHEDULED_SCRIPT:
        return (
            ComputeTarget(
                workload_id=workload.id,
                kind=ComputeTargetKind.CLOUD_RUN_JOB,
            ),
            ComputeTarget(
                workload_id=workload.id,
                kind=ComputeTargetKind.CLOUD_SCHEDULER_JOB,
            ),
        )
    return (
        ComputeTarget(
            workload_id=workload.id,
            kind=ComputeTargetKind.CLOUD_RUN_SERVICE,
        ),
    )


def _cleanup_action_id(
    *,
    workload: Workload,
    kind: LifecycleActionKind,
    eligible_at: datetime,
    discriminator: str,
) -> LifecycleActionId:
    stamp = eligible_at.strftime("%Y%m%dT%H%M%SZ")
    return LifecycleActionId(
        f"life-{workload.id}-{kind.value}-{stamp}-w{workload.version}-{discriminator}"
    )


def _holds_are_valid(holds: object) -> bool:
    if not isinstance(holds, frozenset):
        return False
    return all(_valid_workload_id_text(item) for item in holds)


def _valid_workload_id_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_status_anchor(value: object, *, now: datetime) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() != now.utcoffset():
        return None
    if value > now:
        return None
    return value


def _anchor_stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


__all__ = [
    "AdminDecision",
    "AdminDecisionKind",
    "CleanupExecutionDecision",
    "CleanupExecutionDecisionKind",
    "CleanupGuard",
    "ComputeTarget",
    "ComputeTargetKind",
    "LifecycleDecision",
    "PlannedLifecycleAction",
    "ScheduleTransitionProposal",
    "WorkloadTransitionProposal",
    "plan_user_lifecycle",
    "revalidate_cleanup_action",
]
