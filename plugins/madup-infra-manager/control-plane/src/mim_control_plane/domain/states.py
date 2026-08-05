"""Closed state machines for persisted MIM domain records."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping, TypeVar


class InvalidTransition(ValueError):
    """Raised when a domain record attempts a transition outside policy."""


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class UserState(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    OFFBOARDED = "offboarded"


class RepositoryAdmissionState(StrEnum):
    PENDING = "pending"
    ADMITTED = "admitted"
    REJECTED = "rejected"
    REVOKED = "revoked"


class WorkloadKind(StrEnum):
    STREAMLIT = "streamlit"
    NEXTJS = "nextjs"
    SCHEDULED_SCRIPT = "scheduled_script"


class WorkloadState(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    ARCHIVED = "archived"


class PlanState(StrEnum):
    ISSUED = "issued"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OperationState(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    BUILDING = "building"
    DEPLOYING = "deploying"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"
    QUARANTINED = "quarantined"


class ScheduleState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    PAUSED = "paused"
    QUARANTINED = "quarantined"
    ARCHIVED = "archived"


class SecretRotationState(StrEnum):
    STABLE = "stable"
    ROTATING = "rotating"
    RETIRING_OLD_VERSION = "retiring_old_version"


class SecretLifecycleState(StrEnum):
    ACTIVE = "active"
    LOCKED = "locked"
    RETIRING = "retiring"
    DESTROYED = "destroyed"


class SecretMutationState(StrEnum):
    IDLE = "idle"
    CREATING = "creating"
    ATTACHING = "attaching"
    ROTATING = "rotating"


class UsageConfidence(StrEnum):
    ESTIMATED = "estimated"
    MEASURED = "measured"
    FINALIZED = "finalized"


class ActivitySurface(StrEnum):
    DASHBOARD = "dashboard"
    MCP = "mcp"
    OPERATOR = "operator"
    WORKER = "worker"


class ActivityOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"


class LifecycleActionKind(StrEnum):
    INACTIVITY_WARNING = "inactivity_warning"
    QUARANTINE = "quarantine"
    TRANSFER = "transfer"
    DELETE_COMPUTE = "delete_compute"
    RESTORE = "restore"
    DELETE_IMAGE = "delete_image"


class LifecycleActionState(StrEnum):
    PLANNED = "planned"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    FAILED = "failed"


_StateT = TypeVar("_StateT", bound=StrEnum)


USER_TRANSITIONS: Final[Mapping[UserState, frozenset[UserState]]] = MappingProxyType(
    {
        UserState.ACTIVE: frozenset({UserState.SUSPENDED, UserState.OFFBOARDED}),
        UserState.SUSPENDED: frozenset({UserState.ACTIVE, UserState.OFFBOARDED}),
        UserState.OFFBOARDED: frozenset(),
    }
)

REPOSITORY_ADMISSION_TRANSITIONS: Final[
    Mapping[RepositoryAdmissionState, frozenset[RepositoryAdmissionState]]
] = MappingProxyType(
    {
        RepositoryAdmissionState.PENDING: frozenset(
            {RepositoryAdmissionState.ADMITTED, RepositoryAdmissionState.REJECTED}
        ),
        RepositoryAdmissionState.ADMITTED: frozenset(
            {RepositoryAdmissionState.REVOKED}
        ),
        RepositoryAdmissionState.REJECTED: frozenset(),
        RepositoryAdmissionState.REVOKED: frozenset(),
    }
)

WORKLOAD_TRANSITIONS: Final[
    Mapping[WorkloadState, frozenset[WorkloadState]]
] = MappingProxyType(
    {
        WorkloadState.PLANNED: frozenset(
            {
                WorkloadState.ACTIVE,
                WorkloadState.FAILED,
                WorkloadState.QUARANTINED,
                WorkloadState.ARCHIVED,
            }
        ),
        WorkloadState.ACTIVE: frozenset(
            {
                WorkloadState.PAUSED,
                WorkloadState.FAILED,
                WorkloadState.QUARANTINED,
                WorkloadState.ARCHIVED,
            }
        ),
        WorkloadState.PAUSED: frozenset(
            {
                WorkloadState.ACTIVE,
                WorkloadState.QUARANTINED,
                WorkloadState.ARCHIVED,
            }
        ),
        WorkloadState.FAILED: frozenset(
            {
                WorkloadState.ACTIVE,
                WorkloadState.QUARANTINED,
                WorkloadState.ARCHIVED,
            }
        ),
        WorkloadState.QUARANTINED: frozenset(
            {WorkloadState.ACTIVE, WorkloadState.PAUSED, WorkloadState.ARCHIVED}
        ),
        WorkloadState.ARCHIVED: frozenset({WorkloadState.PLANNED}),
    }
)

PLAN_TRANSITIONS: Final[Mapping[PlanState, frozenset[PlanState]]] = MappingProxyType(
    {
        PlanState.ISSUED: frozenset(
            {PlanState.CONSUMED, PlanState.EXPIRED, PlanState.CANCELLED}
        ),
        PlanState.CONSUMED: frozenset(),
        PlanState.EXPIRED: frozenset(),
        PlanState.CANCELLED: frozenset(),
    }
)

OPERATION_TRANSITIONS: Final[
    Mapping[OperationState, frozenset[OperationState]]
] = MappingProxyType(
    {
        OperationState.PLANNED: frozenset(
            {OperationState.QUEUED, OperationState.CANCELLED}
        ),
        OperationState.QUEUED: frozenset(
            {
                OperationState.BUILDING,
                OperationState.FAILED,
                OperationState.CANCELLED,
                OperationState.QUARANTINED,
            }
        ),
        OperationState.BUILDING: frozenset(
            {
                OperationState.DEPLOYING,
                OperationState.FAILED,
                OperationState.CANCELLED,
                OperationState.QUARANTINED,
            }
        ),
        OperationState.DEPLOYING: frozenset(
            {
                OperationState.VERIFYING,
                OperationState.FAILED,
                OperationState.ROLLED_BACK,
                OperationState.QUARANTINED,
            }
        ),
        OperationState.VERIFYING: frozenset(
            {
                OperationState.SUCCEEDED,
                OperationState.FAILED,
                OperationState.ROLLED_BACK,
                OperationState.QUARANTINED,
            }
        ),
        OperationState.SUCCEEDED: frozenset(),
        OperationState.FAILED: frozenset(),
        OperationState.ROLLED_BACK: frozenset(),
        OperationState.CANCELLED: frozenset(),
        OperationState.QUARANTINED: frozenset(),
    }
)

SCHEDULE_TRANSITIONS: Final[
    Mapping[ScheduleState, frozenset[ScheduleState]]
] = MappingProxyType(
    {
        ScheduleState.ENABLED: frozenset(
            {
                ScheduleState.DISABLED,
                ScheduleState.PAUSED,
                ScheduleState.QUARANTINED,
                ScheduleState.ARCHIVED,
            }
        ),
        ScheduleState.DISABLED: frozenset(
            {
                ScheduleState.ENABLED,
                ScheduleState.QUARANTINED,
                ScheduleState.ARCHIVED,
            }
        ),
        ScheduleState.PAUSED: frozenset(
            {
                ScheduleState.ENABLED,
                ScheduleState.DISABLED,
                ScheduleState.QUARANTINED,
                ScheduleState.ARCHIVED,
            }
        ),
        ScheduleState.QUARANTINED: frozenset(
            {ScheduleState.DISABLED, ScheduleState.ARCHIVED}
        ),
        ScheduleState.ARCHIVED: frozenset(),
    }
)

SECRET_ROTATION_TRANSITIONS: Final[
    Mapping[SecretRotationState, frozenset[SecretRotationState]]
] = MappingProxyType(
    {
        SecretRotationState.STABLE: frozenset({SecretRotationState.ROTATING}),
        SecretRotationState.ROTATING: frozenset(
            {
                SecretRotationState.STABLE,
                SecretRotationState.RETIRING_OLD_VERSION,
            }
        ),
        SecretRotationState.RETIRING_OLD_VERSION: frozenset(
            {SecretRotationState.STABLE}
        ),
    }
)

SECRET_LIFECYCLE_TRANSITIONS: Final[
    Mapping[SecretLifecycleState, frozenset[SecretLifecycleState]]
] = MappingProxyType(
    {
        SecretLifecycleState.ACTIVE: frozenset(
            {SecretLifecycleState.LOCKED, SecretLifecycleState.RETIRING}
        ),
        SecretLifecycleState.LOCKED: frozenset(
            {SecretLifecycleState.ACTIVE, SecretLifecycleState.RETIRING}
        ),
        SecretLifecycleState.RETIRING: frozenset(
            {SecretLifecycleState.LOCKED, SecretLifecycleState.DESTROYED}
        ),
        SecretLifecycleState.DESTROYED: frozenset(),
    }
)

LIFECYCLE_ACTION_TRANSITIONS: Final[
    Mapping[LifecycleActionState, frozenset[LifecycleActionState]]
] = MappingProxyType(
    {
        LifecycleActionState.PLANNED: frozenset(
            {
                LifecycleActionState.EXECUTED,
                LifecycleActionState.CANCELLED,
                LifecycleActionState.FAILED,
            }
        ),
        LifecycleActionState.EXECUTED: frozenset(),
        LifecycleActionState.CANCELLED: frozenset(),
        LifecycleActionState.FAILED: frozenset(),
    }
)


def require_transition(
    current: _StateT,
    target: _StateT,
    allowed: Mapping[_StateT, frozenset[_StateT]],
    *,
    record_type: str,
) -> None:
    """Validate a transition against an exhaustive positive adjacency map."""

    if target not in allowed[current]:
        raise InvalidTransition(
            f"{record_type} cannot transition from {current.value} to {target.value}."
        )


def require_operation_transition(
    current: OperationState,
    target: OperationState,
) -> None:
    require_transition(
        current,
        target,
        OPERATION_TRANSITIONS,
        record_type="operation",
    )


def require_user_transition(current: UserState, target: UserState) -> None:
    require_transition(current, target, USER_TRANSITIONS, record_type="user")


def require_repository_admission_transition(
    current: RepositoryAdmissionState,
    target: RepositoryAdmissionState,
) -> None:
    require_transition(
        current,
        target,
        REPOSITORY_ADMISSION_TRANSITIONS,
        record_type="repository admission",
    )


def require_workload_transition(
    current: WorkloadState,
    target: WorkloadState,
) -> None:
    require_transition(current, target, WORKLOAD_TRANSITIONS, record_type="workload")


def require_plan_transition(current: PlanState, target: PlanState) -> None:
    require_transition(current, target, PLAN_TRANSITIONS, record_type="plan")


def require_schedule_transition(
    current: ScheduleState,
    target: ScheduleState,
) -> None:
    require_transition(current, target, SCHEDULE_TRANSITIONS, record_type="schedule")


def require_secret_rotation_transition(
    current: SecretRotationState,
    target: SecretRotationState,
) -> None:
    require_transition(
        current,
        target,
        SECRET_ROTATION_TRANSITIONS,
        record_type="secret rotation",
    )


def require_secret_lifecycle_transition(
    current: SecretLifecycleState,
    target: SecretLifecycleState,
) -> None:
    require_transition(
        current,
        target,
        SECRET_LIFECYCLE_TRANSITIONS,
        record_type="secret lifecycle",
    )


def require_lifecycle_action_transition(
    current: LifecycleActionState,
    target: LifecycleActionState,
) -> None:
    require_transition(
        current,
        target,
        LIFECYCLE_ACTION_TRANSITIONS,
        record_type="lifecycle action",
    )
