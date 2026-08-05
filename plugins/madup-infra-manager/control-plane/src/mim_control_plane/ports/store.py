"""Behavioral persistence contract shared by production and fake adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from mim_control_plane.domain.models import (
    ActivityEvent,
    AppHostnameBinding,
    AuditEvent,
    DailyUsageAggregate,
    DeploymentPlan,
    DeploymentPlanId,
    LifecycleAction,
    LifecycleActionId,
    MaintenanceJobStatus,
    Operation,
    OperationId,
    OrgCostGuard,
    OriginRequestClaim,
    RepositoryAdmission,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    SecretId,
    SecretMetadata,
    UsageEntry,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.ports.execution import QueuedDeployTask

AUTO_DEPLOY_ACTOR_ID = UserId("system:github-auto-deploy")


class StoreError(RuntimeError):
    """Base class for deterministic persistence contract failures."""


class AlreadyExists(StoreError):
    """Raised when a create-only record already exists."""


class NotFound(StoreError):
    """Raised when a requested record does not exist."""


class VersionConflict(StoreError):
    """Raised on stale or malformed optimistic writes."""


class IdempotencyConflict(StoreError):
    """Raised when an idempotency key is reused for different material."""


class ReplayDetected(StoreError):
    """Raised when a create-only origin request ID is reused."""


class InvariantViolation(StoreError):
    """Raised when an update changes immutable persisted material."""


@dataclass(frozen=True, slots=True)
class GitHubAutoDeployResult:
    """Atomic durable outcome for one verified GitHub delivery."""

    admission: RepositoryAdmission
    workload: Workload
    plan: DeploymentPlan
    operation: Operation
    task: QueuedDeployTask
    replayed: bool


class Store(Protocol):
    """Domain-oriented storage behavior required by control-plane services."""

    def create_user(self, user: User) -> User: ...
    def get_user(self, user_id: UserId) -> User: ...
    def save_user(self, user: User, *, expected_version: int) -> User: ...
    def list_users(self) -> tuple[User, ...]: ...

    def create_repository_admission(
        self, admission: RepositoryAdmission
    ) -> RepositoryAdmission: ...
    def get_repository_admission(
        self, admission_id: RepositoryAdmissionId
    ) -> RepositoryAdmission: ...
    def save_repository_admission(
        self, admission: RepositoryAdmission, *, expected_version: int
    ) -> RepositoryAdmission: ...

    def create_workload(self, workload: Workload) -> Workload: ...
    def get_workload(self, workload_id: WorkloadId) -> Workload: ...
    def save_workload(
        self, workload: Workload, *, expected_version: int
    ) -> Workload: ...
    def list_workloads(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[Workload, ...]: ...
    def create_app_hostname_binding(
        self, binding: AppHostnameBinding
    ) -> AppHostnameBinding: ...
    def get_app_hostname_binding(self, public_host: str) -> AppHostnameBinding: ...
    def save_app_hostname_binding(
        self, binding: AppHostnameBinding, *, expected_version: int
    ) -> AppHostnameBinding: ...

    def create_deployment_plan(self, plan: DeploymentPlan) -> DeploymentPlan: ...
    def get_deployment_plan(self, plan_id: DeploymentPlanId) -> DeploymentPlan: ...
    def save_deployment_plan(
        self, plan: DeploymentPlan, *, expected_version: int
    ) -> DeploymentPlan: ...
    def consume_deployment_plan_with_operation(
        self,
        *,
        plan_id: DeploymentPlanId,
        actor_id: UserId,
        expected_material_hash: str,
        expected_action: str,
        policy_version: str,
        consumed_at: datetime,
        operation: Operation,
    ) -> tuple[DeploymentPlan, Operation]: ...
    def consume_schedule_plan_with_operation(
        self,
        *,
        plan_id: DeploymentPlanId,
        actor_id: UserId,
        expected_material_hash: str,
        expected_action: str,
        policy_version: str,
        consumed_at: datetime,
        schedule: Schedule,
        operation: Operation,
    ) -> tuple[DeploymentPlan, Schedule, Operation]: ...

    def apply_github_auto_deploy_once(
        self,
        *,
        delivery_id: str,
        delivery_hash: str,
        source_ref: str,
        expected_workload_version: int,
        admission: RepositoryAdmission,
        workload: Workload,
        plan: DeploymentPlan,
        operation: Operation,
        task: QueuedDeployTask,
        consumed_at: datetime,
    ) -> GitHubAutoDeployResult: ...

    def create_operation_once(self, operation: Operation) -> Operation: ...
    def get_operation(self, operation_id: OperationId) -> Operation: ...
    def get_latest_workload_operation(
        self,
        *,
        owner_id: UserId,
        workload_id: WorkloadId,
    ) -> Operation | None:
        """Return the exact-scoped max by updated_at, created_at, then ID."""
        ...
    def save_operation(
        self, operation: Operation, *, expected_version: int
    ) -> Operation: ...

    def create_schedule(self, schedule: Schedule) -> Schedule: ...
    def get_schedule(self, schedule_id: ScheduleId) -> Schedule: ...
    def save_schedule(
        self, schedule: Schedule, *, expected_version: int
    ) -> Schedule: ...
    def list_schedules(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[Schedule, ...]: ...
    def acquire_schedule_lease(
        self,
        schedule_id: ScheduleId,
        *,
        expected_version: int,
        lease_token: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> Schedule: ...
    def complete_schedule_run(
        self,
        schedule_id: ScheduleId,
        *,
        expected_version: int,
        lease_token: str,
        succeeded: bool,
        completed_at: datetime,
    ) -> Schedule: ...

    def create_secret_metadata(self, secret: SecretMetadata) -> SecretMetadata: ...
    def get_secret_metadata(self, secret_id: SecretId) -> SecretMetadata: ...
    def save_secret_metadata(
        self, secret: SecretMetadata, *, expected_version: int
    ) -> SecretMetadata: ...
    def list_secret_metadata(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[SecretMetadata, ...]: ...

    def append_usage_entry(self, entry: UsageEntry) -> UsageEntry: ...
    def upsert_usage_entry_monotonic(
        self,
        *,
        current: UsageEntry,
        updated: UsageEntry,
    ) -> UsageEntry: ...
    def list_usage_entries(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[UsageEntry, ...]: ...
    def create_org_cost_guard(self, guard: OrgCostGuard) -> OrgCostGuard: ...
    def get_org_cost_guard(self) -> OrgCostGuard: ...
    def save_org_cost_guard(
        self,
        guard: OrgCostGuard,
        *,
        expected_version: int,
    ) -> OrgCostGuard: ...

    def append_activity_event(self, event: ActivityEvent) -> ActivityEvent: ...
    def list_activity_events(
        self, *, user_id: UserId | None = None
    ) -> tuple[ActivityEvent, ...]: ...
    def expire_activity_events(
        self,
        *,
        event_ids: tuple[str, ...],
    ) -> tuple[str, ...]: ...

    def create_daily_usage_aggregate(
        self, aggregate: DailyUsageAggregate
    ) -> DailyUsageAggregate: ...
    def get_daily_usage_aggregate(
        self, day: date, user_id: UserId | None
    ) -> DailyUsageAggregate: ...
    def save_daily_usage_aggregate(
        self, aggregate: DailyUsageAggregate, *, expected_version: int
    ) -> DailyUsageAggregate: ...

    def append_audit_event(self, event: AuditEvent) -> AuditEvent: ...
    def list_audit_events(self) -> tuple[AuditEvent, ...]: ...

    def create_lifecycle_action(self, action: LifecycleAction) -> LifecycleAction: ...
    def get_lifecycle_action(self, action_id: LifecycleActionId) -> LifecycleAction: ...
    def save_lifecycle_action(
        self, action: LifecycleAction, *, expected_version: int
    ) -> LifecycleAction: ...
    def get_maintenance_job_status(self, job_name: str) -> MaintenanceJobStatus: ...
    def list_maintenance_job_statuses(self) -> tuple[MaintenanceJobStatus, ...]: ...
    def record_maintenance_job_started(
        self,
        *,
        job_name: str,
        run_id: str,
        started_at: datetime,
    ) -> MaintenanceJobStatus: ...
    def record_maintenance_job_terminal(
        self,
        *,
        job_name: str,
        run_id: str,
        expected_version: int,
        finished_at: datetime,
        outcome: str,
        summary: tuple[tuple[str, int], ...],
        failure_code: str | None = None,
        failure_class: str | None = None,
    ) -> MaintenanceJobStatus: ...

    def claim_origin_request(self, claim: OriginRequestClaim) -> None: ...

    def create_deploy_task_once(
        self,
        task: QueuedDeployTask,
    ) -> tuple[QueuedDeployTask, bool]: ...
    def get_deploy_task(self, operation_id: OperationId) -> QueuedDeployTask: ...
