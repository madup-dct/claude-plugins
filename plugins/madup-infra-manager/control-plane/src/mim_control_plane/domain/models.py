"""Immutable records persisted by the MIM control plane."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import NewType

from .states import (
    ActivityOutcome,
    ActivitySurface,
    LifecycleActionKind,
    LifecycleActionState,
    OperationState,
    PlanState,
    RepositoryAdmissionState,
    ScheduleState,
    SecretLifecycleState,
    SecretMutationState,
    SecretRotationState,
    UsageConfidence,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
    require_lifecycle_action_transition,
    require_operation_transition,
    require_plan_transition,
    require_repository_admission_transition,
    require_schedule_transition,
    require_secret_lifecycle_transition,
    require_secret_rotation_transition,
    require_user_transition,
    require_workload_transition,
)

UserId = NewType("UserId", str)
RepositoryAdmissionId = NewType("RepositoryAdmissionId", str)
WorkloadId = NewType("WorkloadId", str)
DeploymentPlanId = NewType("DeploymentPlanId", str)
OperationId = NewType("OperationId", str)
ScheduleId = NewType("ScheduleId", str)
SecretId = NewType("SecretId", str)
UsageEntryId = NewType("UsageEntryId", str)
ActivityEventId = NewType("ActivityEventId", str)
AuditEventId = NewType("AuditEventId", str)
LifecycleActionId = NewType("LifecycleActionId", str)
OriginRequestId = NewType("OriginRequestId", str)

_SOURCE_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_AUTO_DEPLOY_REF_PATTERN = re.compile(r"^refs/heads/[A-Za-z0-9._/-]{1,200}$")
_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAINTENANCE_JOB_NAMES = frozenset({"identity-sync", "lifecycle", "usage-ingest"})
_MAINTENANCE_JOB_OUTCOMES = frozenset(
    {"started", "completed", "failed", "skipped_overlap"}
)
_MAINTENANCE_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")


def _require_utc(value: datetime | None, field_name: str) -> None:
    if value is None:
        return
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware.")


def _require_timestamp_order(
    created_at: datetime,
    updated_at: datetime,
) -> None:
    if updated_at < created_at:
        raise ValueError("updated_at must not precede created_at.")


def _require_version(version: int) -> None:
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("version must be a positive integer.")


def _require_non_negative(value: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


def _require_transition_time(at: datetime, updated_at: datetime) -> None:
    _require_utc(at, "at")
    if at < updated_at:
        raise ValueError("transition time must not precede updated_at.")


@dataclass(frozen=True, slots=True)
class User:
    id: UserId
    email: str
    role: UserRole
    state: UserState
    groups: frozenset[str]
    identity_synced_at: datetime
    created_at: datetime
    updated_at: datetime
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.email, "email")
        if not isinstance(self.groups, frozenset):
            raise ValueError("groups must be an immutable frozenset.")
        for group in self.groups:
            _require_text(group, "group")
        _require_utc(self.identity_synced_at, "identity_synced_at")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_timestamp_order(self.created_at, self.updated_at)
        _require_version(self.version)

    def transition_state(self, target: UserState, *, at: datetime) -> User:
        _require_transition_time(at, self.updated_at)
        require_user_transition(self.state, target)
        return replace(
            self,
            state=target,
            updated_at=at,
            version=self.version + 1,
        )

    def reconcile_identity(
        self,
        *,
        target_state: UserState,
        required_group: str,
        in_required_group: bool,
        synced_at: datetime,
    ) -> User:
        if not isinstance(target_state, UserState):
            raise ValueError("target_state must be a UserState.")
        _require_text(required_group, "required_group")
        if type(in_required_group) is not bool:
            raise ValueError("in_required_group must be an exact bool.")
        _require_utc(synced_at, "synced_at")
        if synced_at < self.identity_synced_at:
            raise ValueError("synced_at must not precede identity_synced_at.")

        next_groups = set(self.groups)
        if in_required_group:
            next_groups.add(required_group)
        else:
            next_groups.discard(required_group)
        updated_groups = frozenset(next_groups)
        state_changed = target_state is not self.state
        if state_changed:
            require_user_transition(self.state, target_state)
        material_changed = (
            state_changed
            or updated_groups != self.groups
            or synced_at != self.identity_synced_at
        )
        if not material_changed:
            return self
        if state_changed or updated_groups != self.groups:
            if synced_at < self.updated_at:
                raise ValueError(
                    "synced_at must not precede updated_at for identity changes."
                )
            updated_at = synced_at
        else:
            updated_at = self.updated_at
        return replace(
            self,
            state=target_state,
            groups=updated_groups,
            identity_synced_at=synced_at,
            updated_at=updated_at,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class RepositoryAdmission:
    id: RepositoryAdmissionId
    repository_numeric_id: int
    owner: str
    name: str
    installation_id: int
    state: RepositoryAdmissionState
    admitted_sha: str
    created_at: datetime
    updated_at: datetime
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_non_negative(self.repository_numeric_id, "repository_numeric_id")
        _require_non_negative(self.installation_id, "installation_id")
        _require_text(self.owner, "owner")
        _require_text(self.name, "name")
        _require_text(self.admitted_sha, "admitted_sha")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_timestamp_order(self.created_at, self.updated_at)
        _require_version(self.version)

    def transition_state(
        self,
        target: RepositoryAdmissionState,
        *,
        at: datetime,
    ) -> RepositoryAdmission:
        _require_transition_time(at, self.updated_at)
        require_repository_admission_transition(self.state, target)
        return replace(
            self,
            state=target,
            updated_at=at,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class Workload:
    id: WorkloadId
    owner_id: UserId
    repository_admission_id: RepositoryAdmissionId
    name: str
    kind: WorkloadKind
    state: WorkloadState
    source_sha: str
    desired_manifest_hash: str
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime | None = None
    last_healthy_image_digest: str | None = None
    auto_deploy_enabled: bool = False
    auto_deploy_ref: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.owner_id, "owner_id")
        _require_text(self.repository_admission_id, "repository_admission_id")
        _require_text(self.name, "name")
        _require_text(self.source_sha, "source_sha")
        _require_text(self.desired_manifest_hash, "desired_manifest_hash")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_utc(self.last_activity_at, "last_activity_at")
        _require_timestamp_order(self.created_at, self.updated_at)
        if type(self.auto_deploy_enabled) is not bool:
            raise ValueError("auto_deploy_enabled must be an exact bool.")
        if self.auto_deploy_enabled != (self.auto_deploy_ref is not None):
            raise ValueError(
                "auto_deploy_ref must be set exactly when auto deploy is enabled."
            )
        if self.auto_deploy_ref is not None and (
            _AUTO_DEPLOY_REF_PATTERN.fullmatch(self.auto_deploy_ref) is None
            or ".." in self.auto_deploy_ref
        ):
            raise ValueError("auto_deploy_ref must be an exact safe branch ref.")
        _require_version(self.version)

    def transition_state(
        self,
        target: WorkloadState,
        *,
        at: datetime,
    ) -> Workload:
        _require_transition_time(at, self.updated_at)
        require_workload_transition(self.state, target)
        return replace(
            self,
            state=target,
            updated_at=at,
            version=self.version + 1,
        )

    def record_activity(self, *, at: datetime) -> Workload:
        _require_transition_time(at, self.updated_at)
        return replace(
            self,
            updated_at=at,
            last_activity_at=at,
            version=self.version + 1,
        )

    def advance_source(
        self,
        *,
        repository_admission_id: RepositoryAdmissionId,
        source_sha: str,
        desired_manifest_hash: str,
        at: datetime,
    ) -> Workload:
        """Advance immutable source identity through the dedicated webhook path."""

        _require_transition_time(at, self.updated_at)
        _require_text(repository_admission_id, "repository_admission_id")
        _require_text(desired_manifest_hash, "desired_manifest_hash")
        if repository_admission_id == self.repository_admission_id:
            raise ValueError("source advancement requires a new admission.")
        if (
            _SOURCE_SHA_PATTERN.fullmatch(source_sha) is None
            or source_sha == "0" * 40
            or source_sha == self.source_sha
        ):
            raise ValueError("source advancement requires a new exact SHA.")
        return replace(
            self,
            repository_admission_id=repository_admission_id,
            source_sha=source_sha,
            desired_manifest_hash=desired_manifest_hash,
            updated_at=at,
            last_activity_at=at,
            version=self.version + 1,
        )


class AppHostnameBindingState(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


_APP_HOST_BINDING_TRANSITIONS = {
    AppHostnameBindingState.ACTIVE: frozenset(
        {AppHostnameBindingState.DISABLED, AppHostnameBindingState.RETIRED}
    ),
    AppHostnameBindingState.DISABLED: frozenset(
        {AppHostnameBindingState.ACTIVE, AppHostnameBindingState.RETIRED}
    ),
    AppHostnameBindingState.RETIRED: frozenset(),
}


def require_app_hostname_binding_transition(
    current: AppHostnameBindingState,
    target: AppHostnameBindingState,
) -> None:
    if target not in _APP_HOST_BINDING_TRANSITIONS[current]:
        raise ValueError(
            "app hostname binding cannot transition from "
            f"{current.value} to {target.value}."
        )


@dataclass(frozen=True, slots=True)
class AppHostnameBinding:
    public_host: str
    workload_id: WorkloadId
    owner_id: UserId
    workload_kind: WorkloadKind
    service_resource: str
    upstream_url: str
    upstream_audience: str
    state: AppHostnameBindingState
    created_at: datetime
    updated_at: datetime
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.public_host, "public_host")
        _require_text(self.workload_id, "workload_id")
        _require_text(self.owner_id, "owner_id")
        _require_text(self.service_resource, "service_resource")
        _require_text(self.upstream_url, "upstream_url")
        _require_text(self.upstream_audience, "upstream_audience")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_timestamp_order(self.created_at, self.updated_at)
        _require_version(self.version)
        if self.public_host != self.public_host.strip().casefold():
            raise ValueError("public_host must be exact lower-case text.")
        if self.upstream_url != self.upstream_audience:
            raise ValueError("upstream_audience must exactly equal upstream_url.")

    def transition_state(
        self,
        target: AppHostnameBindingState,
        *,
        at: datetime,
    ) -> AppHostnameBinding:
        _require_transition_time(at, self.updated_at)
        require_app_hostname_binding_transition(self.state, target)
        return replace(
            self,
            state=target,
            updated_at=at,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    id: DeploymentPlanId
    actor_id: UserId
    workload_id: WorkloadId | None
    action: str
    material_hash: str
    policy_version: str
    state: PlanState
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    sanitized_summary: tuple[tuple[str, str], ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.actor_id, "actor_id")
        if self.workload_id is not None:
            _require_text(self.workload_id, "workload_id")
        _require_text(self.action, "action")
        _require_text(self.material_hash, "material_hash")
        _require_text(self.policy_version, "policy_version")
        if not isinstance(self.sanitized_summary, tuple):
            raise ValueError("sanitized_summary must be an immutable tuple.")
        for item in self.sanitized_summary:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError(
                    "sanitized_summary entries must be immutable key/value tuples."
                )
            key, value = item
            _require_text(key, "summary key")
            _require_text(value, "summary value")
        _require_utc(self.expires_at, "expires_at")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_timestamp_order(self.created_at, self.updated_at)
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must follow created_at.")
        _require_version(self.version)

    def transition_state(
        self,
        target: PlanState,
        *,
        at: datetime,
    ) -> DeploymentPlan:
        _require_transition_time(at, self.updated_at)
        require_plan_transition(self.state, target)
        return replace(
            self,
            state=target,
            updated_at=at,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class Operation:
    id: OperationId
    actor_id: UserId
    workload_id: WorkloadId | None
    action: str
    idempotency_key: str
    request_hash: str
    state: OperationState
    created_at: datetime
    updated_at: datetime
    sanitized_failure: str | None = None
    result_summary: tuple[tuple[str, str], ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.actor_id, "actor_id")
        if self.workload_id is not None:
            _require_text(self.workload_id, "workload_id")
        _require_text(self.action, "action")
        _require_text(self.idempotency_key, "idempotency_key")
        _require_text(self.request_hash, "request_hash")
        if self.sanitized_failure is not None:
            _require_text(self.sanitized_failure, "sanitized_failure")
        if not isinstance(self.result_summary, tuple):
            raise ValueError("result_summary must be an immutable tuple.")
        for item in self.result_summary:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError(
                    "result_summary entries must be immutable key/value tuples."
                )
            key, value = item
            _require_text(key, "result key")
            _require_text(value, "result value")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_timestamp_order(self.created_at, self.updated_at)
        _require_version(self.version)

    def transition(
        self,
        target: OperationState,
        *,
        at: datetime,
        sanitized_failure: str | None = None,
    ) -> Operation:
        """Return the next immutable operation version after policy validation."""

        _require_transition_time(at, self.updated_at)
        require_operation_transition(self.state, target)
        return replace(
            self,
            state=target,
            updated_at=at,
            sanitized_failure=sanitized_failure,
            version=self.version + 1,
        )

    def record_result(
        self,
        *,
        result_summary: tuple[tuple[str, str], ...],
        at: datetime,
    ) -> Operation:
        _require_transition_time(at, self.updated_at)
        if self.state is not OperationState.SUCCEEDED:
            raise ValueError("operation must be succeeded before recording result.")
        return replace(
            self,
            result_summary=result_summary,
            updated_at=at,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class Schedule:
    id: ScheduleId
    owner_id: UserId
    workload_id: WorkloadId
    cron: str
    timezone: str
    state: ScheduleState
    created_at: datetime
    updated_at: datetime
    consecutive_failures: int = 0
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.owner_id, "owner_id")
        _require_text(self.workload_id, "workload_id")
        _require_text(self.cron, "cron")
        _require_text(self.timezone, "timezone")
        _require_non_negative(self.consecutive_failures, "consecutive_failures")
        if self.lease_token is not None:
            _require_text(self.lease_token, "lease_token")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_utc(self.lease_expires_at, "lease_expires_at")
        _require_utc(self.last_attempt_at, "last_attempt_at")
        _require_utc(self.last_success_at, "last_success_at")
        _require_timestamp_order(self.created_at, self.updated_at)
        _require_version(self.version)

    def transition_state(
        self,
        target: ScheduleState,
        *,
        at: datetime,
    ) -> Schedule:
        _require_transition_time(at, self.updated_at)
        require_schedule_transition(self.state, target)
        return replace(
            self,
            state=target,
            updated_at=at,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    id: SecretId
    owner_id: UserId
    name: str
    integration_type: str
    attached_workload_ids: tuple[WorkloadId, ...]
    active_version: int
    rotation_state: SecretRotationState
    lifecycle_state: SecretLifecycleState
    created_at: datetime
    updated_at: datetime
    retiring_version: int | None = None
    retirement_not_before: datetime | None = None
    mutation_state: SecretMutationState = SecretMutationState.IDLE
    mutation_idempotency_key: str | None = None
    pending_workload_ids: tuple[WorkloadId, ...] | None = None
    pending_payload_sha256: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.owner_id, "owner_id")
        _require_text(self.name, "name")
        _require_text(self.integration_type, "integration_type")
        if not isinstance(self.attached_workload_ids, tuple):
            raise ValueError("attached_workload_ids must be an immutable tuple.")
        for workload_id in self.attached_workload_ids:
            _require_text(workload_id, "attached workload ID")
        if isinstance(self.active_version, bool) or self.active_version < 1:
            raise ValueError("active_version must be a positive integer.")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_utc(self.retirement_not_before, "retirement_not_before")
        _require_timestamp_order(self.created_at, self.updated_at)
        if (self.retiring_version is None) != (self.retirement_not_before is None):
            raise ValueError(
                "retiring_version and retirement_not_before must be set together."
            )
        if self.retiring_version is not None:
            if (
                isinstance(self.retiring_version, bool)
                or self.retiring_version < 1
                or self.retiring_version == self.active_version
            ):
                raise ValueError("retiring_version must be a distinct version.")
            if self.rotation_state is not SecretRotationState.RETIRING_OLD_VERSION:
                raise ValueError(
                    "retiring version metadata requires retiring_old_version state."
                )
        elif self.rotation_state is SecretRotationState.RETIRING_OLD_VERSION:
            raise ValueError(
                "retiring_old_version state requires retirement metadata."
            )
        if self.pending_workload_ids is not None:
            if type(self.pending_workload_ids) is not tuple:
                raise ValueError("pending_workload_ids must be an immutable tuple.")
            for workload_id in self.pending_workload_ids:
                _require_text(workload_id, "pending workload ID")
        if self.mutation_state is SecretMutationState.IDLE:
            if (
                self.mutation_idempotency_key is not None
                or self.pending_workload_ids is not None
                or self.pending_payload_sha256 is not None
            ):
                raise ValueError("idle secret metadata cannot carry pending mutation.")
        else:
            _require_text(
                self.mutation_idempotency_key or "",
                "mutation_idempotency_key",
            )
            if not self.pending_workload_ids:
                raise ValueError(
                    "pending_workload_ids are required for inflight secret mutation."
                )
            if (
                self.mutation_state is SecretMutationState.CREATING
                and self.attached_workload_ids
            ):
                raise ValueError(
                    "creating secret draft cannot expose attached workloads yet."
                )
            if self.mutation_state in {
                SecretMutationState.ATTACHING,
                SecretMutationState.ROTATING,
            } and not set(self.attached_workload_ids).issubset(
                set(self.pending_workload_ids)
            ):
                raise ValueError(
                    "pending_workload_ids must preserve existing attachments."
                )
            if self.mutation_state is SecretMutationState.ATTACHING:
                if self.pending_payload_sha256 is not None:
                    raise ValueError(
                        "attaching secret mutation cannot persist payload material."
                    )
            else:
                if (
                    type(self.pending_payload_sha256) is not str
                    or _SHA256_HEX_PATTERN.fullmatch(self.pending_payload_sha256)
                    is None
                ):
                    raise ValueError(
                        "secret mutation payload hash must be exact sha256 hex."
                    )
        _require_version(self.version)

    def transition_lifecycle(
        self,
        target: SecretLifecycleState,
        *,
        at: datetime,
    ) -> SecretMetadata:
        _require_transition_time(at, self.updated_at)
        require_secret_lifecycle_transition(self.lifecycle_state, target)
        return replace(
            self,
            lifecycle_state=target,
            updated_at=at,
            version=self.version + 1,
        )

    def transition_rotation(
        self,
        target: SecretRotationState,
        *,
        at: datetime,
    ) -> SecretMetadata:
        _require_transition_time(at, self.updated_at)
        require_secret_rotation_transition(self.rotation_state, target)
        return replace(
            self,
            rotation_state=target,
            updated_at=at,
            version=self.version + 1,
        )

    def bind_workloads(
        self,
        attached_workload_ids: tuple[WorkloadId, ...],
        *,
        at: datetime,
    ) -> SecretMetadata:
        _require_transition_time(at, self.updated_at)
        if type(attached_workload_ids) is not tuple:
            raise ValueError("attached_workload_ids must be an immutable tuple.")
        for workload_id in attached_workload_ids:
            _require_text(workload_id, "attached workload ID")
        return replace(
            self,
            attached_workload_ids=attached_workload_ids,
            updated_at=at,
            version=self.version + 1,
        )

    def begin_attachment(
        self,
        *,
        attached_workload_ids: tuple[WorkloadId, ...],
        mutation_idempotency_key: str,
        at: datetime,
    ) -> SecretMetadata:
        _require_transition_time(at, self.updated_at)
        if type(attached_workload_ids) is not tuple:
            raise ValueError("attached_workload_ids must be an immutable tuple.")
        for workload_id in attached_workload_ids:
            _require_text(workload_id, "attached workload ID")
        _require_text(mutation_idempotency_key, "mutation_idempotency_key")
        if not set(self.attached_workload_ids).issubset(set(attached_workload_ids)):
            raise ValueError("secret attachment cannot remove existing workloads.")
        return replace(
            self,
            mutation_state=SecretMutationState.ATTACHING,
            mutation_idempotency_key=mutation_idempotency_key,
            pending_workload_ids=attached_workload_ids,
            pending_payload_sha256=None,
            updated_at=at,
            version=self.version + 1,
        )

    def begin_rotation(
        self,
        *,
        attached_workload_ids: tuple[WorkloadId, ...],
        mutation_idempotency_key: str,
        pending_payload_sha256: str,
        at: datetime,
    ) -> SecretMetadata:
        staged = self.transition_rotation(SecretRotationState.ROTATING, at=at)
        if not set(self.attached_workload_ids).issubset(set(attached_workload_ids)):
            raise ValueError("secret rotation cannot remove existing workloads.")
        _require_text(mutation_idempotency_key, "mutation_idempotency_key")
        if _SHA256_HEX_PATTERN.fullmatch(pending_payload_sha256) is None:
            raise ValueError("pending_payload_sha256 is invalid.")
        return replace(
            staged,
            mutation_state=SecretMutationState.ROTATING,
            mutation_idempotency_key=mutation_idempotency_key,
            pending_workload_ids=attached_workload_ids,
            pending_payload_sha256=pending_payload_sha256,
        )

    def advance_mutation_progress(self, *, at: datetime) -> SecretMetadata:
        _require_transition_time(at, self.updated_at)
        if self.mutation_state is SecretMutationState.IDLE:
            raise ValueError("idle secret metadata cannot advance mutation progress.")
        return replace(
            self,
            updated_at=at,
            version=self.version + 1,
        )

    def finalize_attachment(self, *, at: datetime) -> SecretMetadata:
        _require_transition_time(at, self.updated_at)
        if self.mutation_state is not SecretMutationState.ATTACHING:
            raise ValueError("secret attachment is not inflight.")
        if self.pending_workload_ids is None:
            raise ValueError("pending_workload_ids are missing.")
        return replace(
            self,
            attached_workload_ids=self.pending_workload_ids,
            mutation_state=SecretMutationState.IDLE,
            mutation_idempotency_key=None,
            pending_workload_ids=None,
            pending_payload_sha256=None,
            updated_at=at,
            version=self.version + 1,
        )

    def record_rotation(
        self,
        *,
        active_version: int,
        retiring_version: int,
        retirement_not_before: datetime,
        attached_workload_ids: tuple[WorkloadId, ...] | None = None,
        at: datetime,
    ) -> SecretMetadata:
        _require_transition_time(at, self.updated_at)
        _require_utc(retirement_not_before, "retirement_not_before")
        if retirement_not_before < at:
            raise ValueError("retirement_not_before must not precede updated_at.")
        if attached_workload_ids is not None:
            if type(attached_workload_ids) is not tuple:
                raise ValueError("attached_workload_ids must be an immutable tuple.")
            for workload_id in attached_workload_ids:
                _require_text(workload_id, "attached workload ID")
        if (
            isinstance(active_version, bool)
            or isinstance(retiring_version, bool)
            or active_version < 1
            or retiring_version < 1
            or active_version <= self.active_version
            or retiring_version != self.active_version
        ):
            raise ValueError("secret rotation metadata is invalid.")
        require_secret_rotation_transition(
            self.rotation_state,
            SecretRotationState.RETIRING_OLD_VERSION,
        )
        return replace(
            self,
            active_version=active_version,
            attached_workload_ids=(
                self.attached_workload_ids
                if attached_workload_ids is None
                else attached_workload_ids
            ),
            rotation_state=SecretRotationState.RETIRING_OLD_VERSION,
            retiring_version=retiring_version,
            retirement_not_before=retirement_not_before,
            mutation_state=SecretMutationState.IDLE,
            mutation_idempotency_key=None,
            pending_workload_ids=None,
            pending_payload_sha256=None,
            updated_at=at,
            version=self.version + 1,
        )

    def complete_retirement(self, *, at: datetime) -> SecretMetadata:
        _require_transition_time(at, self.updated_at)
        if self.rotation_state is not SecretRotationState.RETIRING_OLD_VERSION:
            raise ValueError("secret is not retiring an old version.")
        return replace(
            self,
            rotation_state=SecretRotationState.STABLE,
            retiring_version=None,
            retirement_not_before=None,
            updated_at=at,
            version=self.version + 1,
        )

    @classmethod
    def create_draft(
        cls,
        *,
        id: SecretId,
        owner_id: UserId,
        name: str,
        integration_type: str,
        attached_workload_ids: tuple[WorkloadId, ...],
        mutation_idempotency_key: str,
        pending_payload_sha256: str,
        created_at: datetime,
    ) -> SecretMetadata:
        if attached_workload_ids and type(attached_workload_ids) is not tuple:
            raise ValueError("attached_workload_ids must be an immutable tuple.")
        return cls(
            id=id,
            owner_id=owner_id,
            name=name,
            integration_type=integration_type,
            attached_workload_ids=(),
            active_version=1,
            rotation_state=SecretRotationState.STABLE,
            lifecycle_state=SecretLifecycleState.ACTIVE,
            created_at=created_at,
            updated_at=created_at,
            mutation_state=SecretMutationState.CREATING,
            mutation_idempotency_key=mutation_idempotency_key,
            pending_workload_ids=attached_workload_ids,
            pending_payload_sha256=pending_payload_sha256,
            version=1,
        )

    def finalize_creation(
        self,
        *,
        active_version: int,
        at: datetime,
    ) -> SecretMetadata:
        _require_transition_time(at, self.updated_at)
        if self.mutation_state is not SecretMutationState.CREATING:
            raise ValueError("secret creation is not inflight.")
        if self.pending_workload_ids is None:
            raise ValueError("pending_workload_ids are missing.")
        if active_version != 1:
            raise ValueError("managed secret create must resolve to version 1.")
        return replace(
            self,
            attached_workload_ids=self.pending_workload_ids,
            active_version=active_version,
            mutation_state=SecretMutationState.IDLE,
            mutation_idempotency_key=None,
            pending_workload_ids=None,
            pending_payload_sha256=None,
            updated_at=at,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class UsageEntry:
    id: UsageEntryId
    owner_id: UserId | None
    workload_id: WorkloadId | None
    service_category: str
    estimated_cost_krw: int
    finalized_cost_krw: int | None
    confidence: UsageConfidence
    collected_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        if self.owner_id is not None:
            _require_text(self.owner_id, "owner_id")
        if self.workload_id is not None:
            _require_text(self.workload_id, "workload_id")
        _require_text(self.service_category, "service_category")
        _require_non_negative(self.estimated_cost_krw, "estimated_cost_krw")
        if self.finalized_cost_krw is not None:
            _require_non_negative(self.finalized_cost_krw, "finalized_cost_krw")
        _require_utc(self.collected_at, "collected_at")


@dataclass(frozen=True, slots=True)
class OrgCostGuard:
    evaluated_at: datetime
    latest_usage_collected_at: datetime | None
    emergency_stop: bool
    org_policy_cost_krw: int
    version: int = 1

    def __post_init__(self) -> None:
        _require_utc(self.evaluated_at, "evaluated_at")
        _require_utc(
            self.latest_usage_collected_at,
            "latest_usage_collected_at",
        )
        if (
            self.latest_usage_collected_at is not None
            and self.latest_usage_collected_at > self.evaluated_at
        ):
            raise ValueError(
                "latest_usage_collected_at must not exceed evaluated_at."
            )
        if type(self.emergency_stop) is not bool:
            raise ValueError("emergency_stop must be an exact bool.")
        _require_non_negative(self.org_policy_cost_krw, "org_policy_cost_krw")
        _require_version(self.version)


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    id: ActivityEventId
    user_id: UserId
    surface: ActivitySurface
    action: str
    target_ref: str | None
    outcome: ActivityOutcome
    latency_bucket: str
    correlation_id: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.user_id, "user_id")
        _require_text(self.action, "action")
        if self.target_ref is not None:
            _require_text(self.target_ref, "target_ref")
        _require_text(self.latency_bucket, "latency_bucket")
        _require_text(self.correlation_id, "correlation_id")
        _require_utc(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class MaintenanceJobStatus:
    job_name: str
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    succeeded_at: datetime | None
    failed_at: datetime | None
    outcome: str
    summary: tuple[tuple[str, int], ...]
    failure_code: str | None
    failure_class: str | None
    version: int

    def __post_init__(self) -> None:
        _require_exact_maintenance_field(
            self.job_name,
            "job_name",
            allowed_values=_MAINTENANCE_JOB_NAMES,
        )
        _require_exact_maintenance_field(self.run_id, "run_id")
        _require_utc(self.started_at, "started_at")
        _require_utc(self.finished_at, "finished_at")
        _require_utc(self.succeeded_at, "succeeded_at")
        _require_utc(self.failed_at, "failed_at")
        _require_exact_maintenance_field(
            self.outcome,
            "outcome",
            allowed_values=_MAINTENANCE_JOB_OUTCOMES,
        )
        if not isinstance(self.summary, tuple):
            raise ValueError("summary must be immutable.")
        if len(self.summary) > 12:
            raise ValueError("summary must stay within the bounded status contract.")
        for key, value in self.summary:
            _require_exact_maintenance_field(key, "summary key")
            _require_non_negative(value, "summary value")
        if self.failure_code is not None:
            _require_exact_maintenance_field(self.failure_code, "failure_code")
        if self.failure_class is not None:
            _require_exact_maintenance_field(self.failure_class, "failure_class")
        _require_version(self.version)
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at.")
        if self.outcome == "started":
            if any(
                value is not None
                for value in (
                    self.finished_at,
                    self.succeeded_at,
                    self.failed_at,
                    self.failure_code,
                    self.failure_class,
                )
            ):
                raise ValueError("started status cannot include terminal fields.")
            return
        if self.finished_at is None:
            raise ValueError("terminal maintenance status must include finished_at.")
        if self.outcome == "completed":
            if self.succeeded_at != self.finished_at or self.failed_at is not None:
                raise ValueError("completed status timestamps are invalid.")
            if self.failure_code is not None or self.failure_class is not None:
                raise ValueError("completed status cannot include failure metadata.")
            return
        if self.outcome == "failed":
            if self.failed_at != self.finished_at or self.succeeded_at is not None:
                raise ValueError("failed status timestamps are invalid.")
            if self.failure_code is None or self.failure_class is None:
                raise ValueError("failed status must include failure metadata.")
            return
        if self.succeeded_at is not None or self.failed_at is not None:
            raise ValueError(
                "skipped status cannot include success or failure timestamps."
            )
        if self.failure_code is not None or self.failure_class is not None:
            raise ValueError("skipped status cannot include failure metadata.")


@dataclass(frozen=True, slots=True)
class DailyUsageAggregate:
    day: date
    user_id: UserId | None
    active_users: int
    dashboard_visits: int
    mcp_actions: int
    deployments: int
    schedule_executions: int
    successes: int
    failures: int
    policy_denials: int
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.user_id is not None:
            _require_text(self.user_id, "user_id")
        for field_name in (
            "active_users",
            "dashboard_visits",
            "mcp_actions",
            "deployments",
            "schedule_executions",
            "successes",
            "failures",
            "policy_denials",
        ):
            _require_non_negative(getattr(self, field_name), field_name)
        _require_version(self.version)
        _require_utc(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: AuditEventId
    actor_id: UserId | None
    action: str
    target_ref: str
    policy_decision: str
    before_ref: str | None
    after_ref: str | None
    correlation_id: str
    outcome: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        if self.actor_id is not None:
            _require_text(self.actor_id, "actor_id")
        _require_text(self.action, "action")
        _require_text(self.target_ref, "target_ref")
        _require_text(self.policy_decision, "policy_decision")
        if self.before_ref is not None:
            _require_text(self.before_ref, "before_ref")
        if self.after_ref is not None:
            _require_text(self.after_ref, "after_ref")
        _require_text(self.correlation_id, "correlation_id")
        _require_text(self.outcome, "outcome")
        _require_utc(self.occurred_at, "occurred_at")


def _require_exact_maintenance_field(
    value: str,
    field_name: str,
    *,
    allowed_values: frozenset[str] | None = None,
) -> None:
    _require_text(value, field_name)
    if (
        _MAINTENANCE_FIELD_PATTERN.fullmatch(value) is None
        or value != value.strip()
    ):
        raise ValueError(f"{field_name} is invalid.")
    if allowed_values is not None and value not in allowed_values:
        raise ValueError(f"{field_name} is invalid.")


@dataclass(frozen=True, slots=True)
class LifecycleAction:
    id: LifecycleActionId
    workload_id: WorkloadId
    kind: LifecycleActionKind
    state: LifecycleActionState
    reason: str
    eligible_at: datetime
    observed_workload_version: int
    created_at: datetime
    updated_at: datetime
    executed_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.workload_id, "workload_id")
        _require_text(self.reason, "reason")
        _require_utc(self.eligible_at, "eligible_at")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_utc(self.executed_at, "executed_at")
        _require_timestamp_order(self.created_at, self.updated_at)
        _require_version(self.observed_workload_version)
        _require_version(self.version)
        is_executed = self.state is LifecycleActionState.EXECUTED
        if is_executed != (self.executed_at is not None):
            raise ValueError(
                "executed_at must be set exactly when lifecycle state is executed."
            )

    def transition_state(
        self,
        target: LifecycleActionState,
        *,
        at: datetime,
    ) -> LifecycleAction:
        _require_transition_time(at, self.updated_at)
        require_lifecycle_action_transition(self.state, target)
        return replace(
            self,
            state=target,
            updated_at=at,
            executed_at=at if target is LifecycleActionState.EXECUTED else None,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class OriginRequestClaim:
    request_id: OriginRequestId
    body_hash: str
    claimed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.request_id, "request_id")
        _require_text(self.body_hash, "body_hash")
        _require_utc(self.claimed_at, "claimed_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.claimed_at:
            raise ValueError("expires_at must follow claimed_at.")
