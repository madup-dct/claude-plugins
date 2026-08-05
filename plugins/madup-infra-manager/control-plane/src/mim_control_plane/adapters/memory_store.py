"""Thread-safe reference store used by unit and adapter contract tests."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from threading import RLock
from typing import Callable, Protocol, TypeVar, cast

from mim_control_plane.config import PILOT_MAX_IDENTITIES
from mim_control_plane.domain.directory_sync import (
    DirectoryUserReconciliation,
)
from mim_control_plane.domain.models import (
    ActivityEvent,
    ActivityEventId,
    AppHostnameBinding,
    AuditEvent,
    AuditEventId,
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
    OriginRequestId,
    RepositoryAdmission,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    SecretId,
    SecretMetadata,
    UsageEntry,
    UsageEntryId,
    User,
    UserId,
    Workload,
    WorkloadId,
    require_app_hostname_binding_transition,
)
from mim_control_plane.domain.states import (
    LifecycleActionState,
    OperationState,
    PlanState,
    RepositoryAdmissionState,
    ScheduleState,
    SecretMutationState,
    SecretRotationState,
    UsageConfidence,
    UserState,
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
from mim_control_plane.ports.directory import DirectoryIdentityRepositoryResult
from mim_control_plane.ports.execution import (
    QueuedDeployTask,
    TaskConflictError,
    TaskNotFoundError,
)
from mim_control_plane.ports.store import (
    AUTO_DEPLOY_ACTOR_ID,
    AlreadyExists,
    GitHubAutoDeployResult,
    IdempotencyConflict,
    InvariantViolation,
    NotFound,
    ReplayDetected,
    Store,
    VersionConflict,
)
from mim_control_plane.services.directory_repository import (
    require_directory_material_hash,
    require_directory_snapshot_id,
    validate_directory_snapshot_write,
)
from mim_control_plane.services.schedules import (
    require_expected_version,
    require_schedule_lease_token,
    require_utc_datetime,
)


class _Versioned(Protocol):
    @property
    def version(self) -> int: ...


_RecordT = TypeVar("_RecordT")
_VersionedT = TypeVar("_VersionedT", bound=_Versioned)
_KeyT = TypeVar("_KeyT")
_StateT = TypeVar("_StateT")

_DELIVERY_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_LOWER_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class MemoryStore(Store):
    """In-process implementation whose behavior mirrors transactional storage."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._users: dict[UserId, User] = {}
        self._repository_admissions: dict[
            RepositoryAdmissionId, RepositoryAdmission
        ] = {}
        self._workloads: dict[WorkloadId, Workload] = {}
        self._app_hostname_bindings: dict[str, AppHostnameBinding] = {}
        self._deployment_plans: dict[DeploymentPlanId, DeploymentPlan] = {}
        self._operations: dict[OperationId, Operation] = {}
        self._operation_idempotency: dict[
            tuple[UserId, str],
            tuple[str, str, WorkloadId | None, OperationId],
        ] = {}
        self._schedules: dict[ScheduleId, Schedule] = {}
        self._secrets: dict[SecretId, SecretMetadata] = {}
        self._usage_entries: dict[UsageEntryId, UsageEntry] = {}
        self._org_cost_guards: dict[str, OrgCostGuard] = {}
        self._activity_events: dict[ActivityEventId, ActivityEvent] = {}
        self._daily_aggregates: dict[
            tuple[date, UserId | None], DailyUsageAggregate
        ] = {}
        self._audit_events: dict[AuditEventId, AuditEvent] = {}
        self._lifecycle_actions: dict[LifecycleActionId, LifecycleAction] = {}
        self._maintenance_job_statuses: dict[str, MaintenanceJobStatus] = {}
        self._origin_claims: dict[OriginRequestId, OriginRequestClaim] = {}
        self._github_delivery_claims: dict[
            str,
            tuple[
                str,
                str,
                RepositoryAdmissionId,
                WorkloadId,
                DeploymentPlanId,
                OperationId,
                str,
            ],
        ] = {}
        self._deploy_tasks: dict[OperationId, QueuedDeployTask] = {}
        self._deploy_task_idempotency: dict[str, OperationId] = {}
        self._directory_snapshot_ledger: dict[
            str, tuple[str, DirectoryIdentityRepositoryResult]
        ] = {}
        self.directory_result_override: (
            DirectoryIdentityRepositoryResult | object | None
        ) = None

    def create_user(self, user: User) -> User:
        return self._create(self._users, user.id, user, "user")

    def get_user(self, user_id: UserId) -> User:
        return self._get(self._users, user_id, "user")

    def save_user(self, user: User, *, expected_version: int) -> User:
        return self._save_stateful(
            self._users,
            user.id,
            user,
            expected_version,
            "user",
            state_of=lambda item: item.state,
            validate=require_user_transition,
            immutable_fields=(
                "id",
                "email",
                "role",
                "groups",
                "identity_synced_at",
                "created_at",
            ),
        )

    def list_users(self) -> tuple[User, ...]:
        with self._lock:
            return tuple(
                self._copy(user)
                for user in sorted(self._users.values(), key=lambda item: str(item.id))
            )

    def apply_snapshot_once(
        self,
        *,
        snapshot_id: str,
        material_hash: str,
        reconciliations: tuple[DirectoryUserReconciliation, ...],
        audit_events: tuple[AuditEvent, ...],
    ) -> DirectoryIdentityRepositoryResult:
        with self._lock:
            require_directory_snapshot_id(snapshot_id)
            require_directory_material_hash(material_hash)
            if not isinstance(reconciliations, tuple):
                raise InvariantViolation("directory reconciliations must be immutable.")
            if not isinstance(audit_events, tuple):
                raise InvariantViolation("directory audit events must be immutable.")

            recorded = self._directory_snapshot_ledger.get(snapshot_id)
            if recorded is not None:
                recorded_hash, _recorded_result = recorded
                if recorded_hash != material_hash:
                    raise IdempotencyConflict("directory snapshot material conflicts.")
                replayed = DirectoryIdentityRepositoryResult(
                    snapshot_id=snapshot_id,
                    material_hash=material_hash,
                    replayed=True,
                    applied_user_ids=(),
                    locked_user_ids=(),
                    audit_event_ids=(),
                )
                return self._copy(replayed)

            override = self.directory_result_override
            if override is not None and not isinstance(
                override,
                DirectoryIdentityRepositoryResult,
            ):
                raise InvariantViolation("directory snapshot result is invalid.")

            expected_result = validate_directory_snapshot_write(
                snapshot_id=snapshot_id,
                material_hash=material_hash,
                reconciliations=reconciliations,
                audit_events=audit_events,
                current_users=self._users,
                existing_audit_event_ids=frozenset(self._audit_events),
                max_identities=PILOT_MAX_IDENTITIES,
            )
            if override is not None and override != expected_result:
                raise InvariantViolation("directory snapshot result is invalid.")
            validated_result = self._copy(expected_result)

            for reconciliation in reconciliations:
                self._users[reconciliation.user.id] = self._copy(reconciliation.user)
            for event in audit_events:
                self._audit_events[event.id] = self._copy(event)

            self._directory_snapshot_ledger[snapshot_id] = (
                material_hash,
                validated_result,
            )
            return self._copy(validated_result)

    def create_repository_admission(
        self,
        admission: RepositoryAdmission,
    ) -> RepositoryAdmission:
        return self._create(
            self._repository_admissions,
            admission.id,
            admission,
            "repository admission",
        )

    def get_repository_admission(
        self,
        admission_id: RepositoryAdmissionId,
    ) -> RepositoryAdmission:
        return self._get(
            self._repository_admissions,
            admission_id,
            "repository admission",
        )

    def save_repository_admission(
        self,
        admission: RepositoryAdmission,
        *,
        expected_version: int,
    ) -> RepositoryAdmission:
        return self._save_stateful(
            self._repository_admissions,
            admission.id,
            admission,
            expected_version,
            "repository admission",
            state_of=lambda item: item.state,
            validate=require_repository_admission_transition,
            immutable_fields=(
                "id",
                "repository_numeric_id",
                "owner",
                "name",
                "installation_id",
                "admitted_sha",
                "created_at",
            ),
        )

    def create_workload(self, workload: Workload) -> Workload:
        return self._create(self._workloads, workload.id, workload, "workload")

    def get_workload(self, workload_id: WorkloadId) -> Workload:
        return self._get(self._workloads, workload_id, "workload")

    def save_workload(
        self,
        workload: Workload,
        *,
        expected_version: int,
    ) -> Workload:
        return self._save_stateful(
            self._workloads,
            workload.id,
            workload,
            expected_version,
            "workload",
            state_of=lambda item: item.state,
            validate=require_workload_transition,
            immutable_fields=(
                "id",
                "owner_id",
                "repository_admission_id",
                "name",
                "kind",
                "source_sha",
                "desired_manifest_hash",
                "auto_deploy_enabled",
                "auto_deploy_ref",
                "created_at",
            ),
        )

    def list_workloads(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[Workload, ...]:
        with self._lock:
            workloads = (
                workload
                for workload in self._workloads.values()
                if owner_id is None or workload.owner_id == owner_id
            )
            return tuple(
                self._copy(workload)
                for workload in sorted(
                    workloads,
                    key=lambda item: (item.created_at, str(item.id)),
                )
            )

    def create_app_hostname_binding(
        self,
        binding: AppHostnameBinding,
    ) -> AppHostnameBinding:
        with self._lock:
            existing = self._app_hostname_bindings.get(binding.public_host)
            if existing is not None:
                if existing == binding:
                    return self._copy(existing)
                raise IdempotencyConflict(
                    "app hostname binding material conflicts."
                )
            if binding.version != 1:
                raise VersionConflict(
                    "new app hostname binding must start at version 1."
                )
            self._app_hostname_bindings[binding.public_host] = self._copy(binding)
            return self._copy(binding)

    def get_app_hostname_binding(self, public_host: str) -> AppHostnameBinding:
        return self._get(
            self._app_hostname_bindings,
            public_host,
            "app hostname binding",
        )

    def save_app_hostname_binding(
        self,
        binding: AppHostnameBinding,
        *,
        expected_version: int,
    ) -> AppHostnameBinding:
        return self._save_stateful(
            self._app_hostname_bindings,
            binding.public_host,
            binding,
            expected_version,
            "app hostname binding",
            state_of=lambda item: item.state,
            validate=require_app_hostname_binding_transition,
            immutable_fields=(
                "public_host",
                "workload_id",
                "owner_id",
                "workload_kind",
                "service_resource",
                "upstream_url",
                "upstream_audience",
                "created_at",
            ),
        )

    def create_deployment_plan(self, plan: DeploymentPlan) -> DeploymentPlan:
        return self._create(self._deployment_plans, plan.id, plan, "deployment plan")

    def get_deployment_plan(self, plan_id: DeploymentPlanId) -> DeploymentPlan:
        return self._get(self._deployment_plans, plan_id, "deployment plan")

    def save_deployment_plan(
        self,
        plan: DeploymentPlan,
        *,
        expected_version: int,
    ) -> DeploymentPlan:
        return self._save_stateful(
            self._deployment_plans,
            plan.id,
            plan,
            expected_version,
            "deployment plan",
            state_of=lambda item: item.state,
            validate=require_plan_transition,
            immutable_fields=(
                "id",
                "actor_id",
                "workload_id",
                "action",
                "material_hash",
                "policy_version",
                "expires_at",
                "sanitized_summary",
                "created_at",
            ),
        )

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
    ) -> tuple[DeploymentPlan, Operation]:
        with self._lock:
            plan = self._deployment_plans.get(plan_id)
            if plan is None:
                raise NotFound("deployment plan was not found.")
            if plan.actor_id != actor_id:
                raise InvariantViolation("deployment plan actor is immutable.")
            if operation.actor_id != actor_id:
                raise InvariantViolation(
                    "operation actor does not match reviewed plan."
                )
            if plan.policy_version != policy_version:
                raise InvariantViolation("deployment plan policy version changed.")
            if plan.action != expected_action:
                raise InvariantViolation("deployment plan action changed.")
            if operation.action != expected_action:
                raise InvariantViolation(
                    "operation action does not match reviewed plan."
                )
            if plan.material_hash != expected_material_hash:
                raise InvariantViolation("deployment plan material hash changed.")
            if operation.workload_id != plan.workload_id:
                raise InvariantViolation(
                    "operation workload does not match reviewed plan."
                )

            claim_key = (operation.actor_id, operation.idempotency_key)
            material = (
                operation.request_hash,
                operation.action,
                operation.workload_id,
            )
            existing_claim = self._operation_idempotency.get(claim_key)
            if existing_claim is not None:
                existing_material = existing_claim[:3]
                if existing_material != material:
                    raise IdempotencyConflict(
                        "Idempotency key was already used for different material."
                    )
                if plan.state is not PlanState.CONSUMED:
                    raise InvariantViolation(
                        "deployment plan must already be consumed before replaying "
                        "the same operation."
                    )
                return self._copy(plan), self._copy(self._operations[existing_claim[3]])

            if plan.state is not PlanState.ISSUED:
                raise VersionConflict(
                    "deployment plan is not available for first consumption."
                )
            if consumed_at >= plan.expires_at:
                raise InvariantViolation("deployment plan expired before consumption.")
            if operation.id in self._operations:
                raise AlreadyExists("operation already exists.")
            if operation.version != 1:
                raise VersionConflict("new operation must start at version 1.")

            consumed_plan = plan.transition_state(PlanState.CONSUMED, at=consumed_at)
            self._deployment_plans[plan_id] = self._copy(consumed_plan)
            self._operations[operation.id] = self._copy(operation)
            self._operation_idempotency[claim_key] = (*material, operation.id)
            return self._copy(consumed_plan), self._copy(operation)

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
    ) -> GitHubAutoDeployResult:
        """Commit one verified delivery, source advance, plan, op, and task."""

        with self._lock:
            _require_github_delivery_material(
                delivery_id=delivery_id,
                delivery_hash=delivery_hash,
                source_ref=source_ref,
            )
            existing = self._github_delivery_claims.get(delivery_id)
            expected_claim = (
                delivery_hash,
                source_ref,
                admission.id,
                workload.id,
                plan.id,
                operation.id,
                task.material_hash,
            )
            if existing is not None:
                if existing != expected_claim:
                    raise ReplayDetected("GitHub delivery material changed.")
                try:
                    persisted_admission = self._repository_admissions[admission.id]
                    persisted_workload = self._workloads[workload.id]
                    persisted_plan = self._deployment_plans[plan.id]
                    persisted_operation = self._operations[operation.id]
                    persisted_task = self._deploy_tasks[operation.id]
                except KeyError:
                    raise InvariantViolation(
                        "GitHub delivery outcome is incomplete."
                    ) from None
                return GitHubAutoDeployResult(
                    admission=self._copy(persisted_admission),
                    workload=self._copy(persisted_workload),
                    plan=self._copy(persisted_plan),
                    operation=self._copy(persisted_operation),
                    task=self._copy(persisted_task),
                    replayed=True,
                )

            current = self._workloads.get(workload.id)
            if current is None:
                raise NotFound("workload was not found.")
            if current.version != expected_workload_version:
                raise VersionConflict("stale workload version.")
            owner = self._users.get(current.owner_id)
            if owner is None or owner.state is not UserState.ACTIVE:
                raise InvariantViolation("auto deploy owner is not active.")
            if current.state not in {WorkloadState.ACTIVE, WorkloadState.FAILED}:
                raise InvariantViolation("workload is not deployable.")
            if (
                current.auto_deploy_enabled is not True
                or current.auto_deploy_ref != source_ref
            ):
                raise InvariantViolation("auto deploy policy does not match.")
            old_admission = self._repository_admissions.get(
                current.repository_admission_id
            )
            if old_admission is None:
                raise NotFound("repository admission was not found.")
            if old_admission.state is not RepositoryAdmissionState.ADMITTED:
                raise InvariantViolation("repository admission is not active.")
            if admission.id in self._repository_admissions:
                raise AlreadyExists("repository admission already exists.")
            if (
                admission.version != 1
                or admission.state is not RepositoryAdmissionState.ADMITTED
                or admission.repository_numeric_id
                != old_admission.repository_numeric_id
                or admission.owner != old_admission.owner
                or admission.name != old_admission.name
                or admission.installation_id != old_admission.installation_id
                or admission.admitted_sha == old_admission.admitted_sha
            ):
                raise InvariantViolation("repository source advancement is invalid.")
            expected_workload = current.advance_source(
                repository_admission_id=admission.id,
                source_sha=admission.admitted_sha,
                desired_manifest_hash=workload.desired_manifest_hash,
                at=workload.updated_at,
            )
            if workload != expected_workload:
                raise InvariantViolation("workload source advancement is invalid.")
            if (
                plan.id in self._deployment_plans
                or operation.id in self._operations
                or operation.id in self._deploy_tasks
            ):
                raise AlreadyExists("GitHub delivery outcome already exists.")
            if (
                plan.version != 1
                or plan.state is not PlanState.ISSUED
                or plan.actor_id != AUTO_DEPLOY_ACTOR_ID
                or plan.workload_id != workload.id
                or plan.action != "deploy"
                or consumed_at >= plan.expires_at
            ):
                raise InvariantViolation("automatic deployment plan is invalid.")
            if (
                operation.version != 1
                or operation.state is not OperationState.QUEUED
                or operation.actor_id != AUTO_DEPLOY_ACTOR_ID
                or operation.workload_id != workload.id
                or operation.action != "deploy"
                or operation.idempotency_key != f"github:{delivery_id}"
                or operation.request_hash != plan.material_hash
            ):
                raise InvariantViolation("automatic deployment operation is invalid.")
            operation_claim_key = (
                operation.actor_id,
                operation.idempotency_key,
            )
            if operation_claim_key in self._operation_idempotency:
                raise IdempotencyConflict("automatic deployment key already exists.")
            _require_task_matches_auto_deploy(
                task=task,
                workload=workload,
                admission=admission,
                operation=operation,
            )
            existing_task_operation = self._deploy_task_idempotency.get(
                task.idempotency_key
            )
            if existing_task_operation is not None:
                raise TaskConflictError("queued deploy task key already exists.")

            consumed_plan = plan.transition_state(
                PlanState.CONSUMED,
                at=consumed_at,
            )
            self._repository_admissions[admission.id] = self._copy(admission)
            self._workloads[workload.id] = self._copy(workload)
            self._deployment_plans[plan.id] = self._copy(consumed_plan)
            self._operations[operation.id] = self._copy(operation)
            self._operation_idempotency[operation_claim_key] = (
                operation.request_hash,
                operation.action,
                operation.workload_id,
                operation.id,
            )
            self._deploy_tasks[operation.id] = self._copy(task)
            self._deploy_task_idempotency[task.idempotency_key] = operation.id
            self._github_delivery_claims[delivery_id] = expected_claim
            return GitHubAutoDeployResult(
                admission=self._copy(admission),
                workload=self._copy(workload),
                plan=self._copy(consumed_plan),
                operation=self._copy(operation),
                task=self._copy(task),
                replayed=False,
            )

    def create_operation_once(self, operation: Operation) -> Operation:
        """Create once, or return the original for identical replay material."""

        with self._lock:
            claim_key = (operation.actor_id, operation.idempotency_key)
            material = (
                operation.request_hash,
                operation.action,
                operation.workload_id,
            )
            existing_claim = self._operation_idempotency.get(claim_key)
            if existing_claim is not None:
                existing_material = existing_claim[:3]
                if existing_material != material:
                    raise IdempotencyConflict(
                        "Idempotency key was already used for different material."
                    )
                return self._copy(self._operations[existing_claim[3]])
            if operation.id in self._operations:
                raise AlreadyExists("operation already exists.")
            if operation.version != 1:
                raise VersionConflict("new operation must start at version 1.")
            self._operations[operation.id] = self._copy(operation)
            self._operation_idempotency[claim_key] = (*material, operation.id)
            return self._copy(operation)

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
    ) -> tuple[DeploymentPlan, Schedule, Operation]:
        with self._lock:
            plan = self._deployment_plans.get(plan_id)
            if plan is None:
                raise NotFound("deployment plan was not found.")
            if (
                plan.actor_id != actor_id
                or plan.action != expected_action
                or plan.policy_version != policy_version
                or plan.material_hash != expected_material_hash
            ):
                raise InvariantViolation("schedule plan material changed.")
            if schedule.workload_id != plan.workload_id:
                raise InvariantViolation("schedule workload does not match plan.")

            claim_key = (operation.actor_id, operation.idempotency_key)
            material = (
                operation.request_hash,
                operation.action,
                operation.workload_id,
            )
            existing_claim = self._operation_idempotency.get(claim_key)
            if existing_claim is not None:
                if existing_claim[:3] != material:
                    raise IdempotencyConflict(
                        "Idempotency key was already used for different material."
                    )
                if plan.state is not PlanState.CONSUMED:
                    raise InvariantViolation("schedule replay outcome is incomplete.")
                try:
                    persisted_schedule = self._schedules[schedule.id]
                    persisted_operation = self._operations[existing_claim[3]]
                except KeyError:
                    raise InvariantViolation(
                        "schedule replay outcome is incomplete."
                    ) from None
                if (
                    persisted_schedule.id != schedule.id
                    or persisted_schedule.owner_id != schedule.owner_id
                    or persisted_schedule.workload_id != schedule.workload_id
                    or persisted_schedule.cron != schedule.cron
                    or persisted_schedule.timezone != schedule.timezone
                ):
                    raise InvariantViolation("schedule replay material changed.")
                return (
                    self._copy(plan),
                    self._copy(persisted_schedule),
                    self._copy(persisted_operation),
                )

            if plan.state is not PlanState.ISSUED:
                raise VersionConflict(
                    "deployment plan is not available for first consumption."
                )
            if consumed_at >= plan.expires_at:
                raise InvariantViolation("deployment plan expired before consumption.")
            if schedule.id in self._schedules:
                raise AlreadyExists("schedule already exists.")
            if (
                schedule.version != 1
                or schedule.state is not ScheduleState.ENABLED
                or schedule.consecutive_failures != 0
                or schedule.lease_token is not None
                or schedule.lease_expires_at is not None
                or schedule.last_attempt_at is not None
                or schedule.last_success_at is not None
            ):
                raise InvariantViolation("new schedule material is invalid.")
            if operation.id in self._operations:
                raise AlreadyExists("operation already exists.")
            if (
                operation.version != 1
                or operation.actor_id != actor_id
                or operation.action != expected_action
                or operation.request_hash != expected_material_hash
                or operation.state is not OperationState.QUEUED
            ):
                raise InvariantViolation("schedule operation is invalid.")

            consumed_plan = plan.transition_state(PlanState.CONSUMED, at=consumed_at)
            self._deployment_plans[plan_id] = self._copy(consumed_plan)
            self._schedules[schedule.id] = self._copy(schedule)
            self._operations[operation.id] = self._copy(operation)
            self._operation_idempotency[claim_key] = (*material, operation.id)
            return (
                self._copy(consumed_plan),
                self._copy(schedule),
                self._copy(operation),
            )

    def get_operation(self, operation_id: OperationId) -> Operation:
        return self._get(self._operations, operation_id, "operation")

    def get_latest_workload_operation(
        self,
        *,
        owner_id: UserId,
        workload_id: WorkloadId,
    ) -> Operation | None:
        with self._lock:
            workload = self._workloads.get(workload_id)
            if workload is None or workload.owner_id != owner_id:
                return None
            matches = tuple(
                operation
                for operation in self._operations.values()
                if operation.workload_id == workload_id
            )
            if not matches:
                return None
            latest = max(
                matches,
                key=lambda item: (
                    item.updated_at,
                    item.created_at,
                    str(item.id),
                ),
            )
            return self._copy(latest)

    def save_operation(
        self,
        operation: Operation,
        *,
        expected_version: int,
    ) -> Operation:
        with self._lock:
            current = self._operations.get(operation.id)
            if current is None:
                raise NotFound("operation was not found.")
            immutable_current = (
                current.actor_id,
                current.workload_id,
                current.action,
                current.idempotency_key,
                current.request_hash,
                current.created_at,
            )
            immutable_next = (
                operation.actor_id,
                operation.workload_id,
                operation.action,
                operation.idempotency_key,
                operation.request_hash,
                operation.created_at,
            )
            if immutable_next != immutable_current:
                raise InvariantViolation(
                    "operation identity and idempotency material are immutable."
                )
            self._validate_next_version_locked(
                current,
                operation,
                expected_version,
                "operation",
            )
            if (
                current.result_summary
                and operation.result_summary != current.result_summary
            ):
                raise InvariantViolation("operation result summary is immutable.")
            if current.state != operation.state:
                require_operation_transition(current.state, operation.state)
            return self._save_versioned_locked(
                self._operations,
                operation.id,
                operation,
                expected_version,
                "operation",
            )

    def create_schedule(self, schedule: Schedule) -> Schedule:
        self._require_schedule_lease_state(
            schedule,
            now=schedule.updated_at,
            allow_expired=True,
        )
        return self._create(self._schedules, schedule.id, schedule, "schedule")

    def get_schedule(self, schedule_id: ScheduleId) -> Schedule:
        return self._get(self._schedules, schedule_id, "schedule")

    def save_schedule(
        self,
        schedule: Schedule,
        *,
        expected_version: int,
    ) -> Schedule:
        return self._save_stateful(
            self._schedules,
            schedule.id,
            schedule,
            expected_version,
            "schedule",
            state_of=lambda item: item.state,
            validate=require_schedule_transition,
            immutable_fields=(
                "id",
                "owner_id",
                "workload_id",
                "cron",
                "timezone",
                "created_at",
                "consecutive_failures",
                "lease_token",
                "lease_expires_at",
                "last_attempt_at",
                "last_success_at",
            ),
        )

    def list_schedules(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[Schedule, ...]:
        with self._lock:
            schedules = (
                schedule
                for schedule in self._schedules.values()
                if owner_id is None or schedule.owner_id == owner_id
            )
            return tuple(
                self._copy(schedule)
                for schedule in sorted(
                    schedules,
                    key=lambda item: (item.created_at, str(item.id)),
                )
            )

    def acquire_schedule_lease(
        self,
        schedule_id: ScheduleId,
        *,
        expected_version: int,
        lease_token: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> Schedule:
        expected_version = require_expected_version(
            expected_version,
            label="schedule lease",
        )
        lease_token = require_schedule_lease_token(lease_token)
        lease_expires_at = require_utc_datetime(
            lease_expires_at,
            label="schedule lease",
        )
        now = require_utc_datetime(now, label="schedule lease")
        if lease_expires_at <= now:
            raise ValueError("schedule lease is invalid.")

        with self._lock:
            current = self._schedules.get(schedule_id)
            if current is None:
                raise NotFound("schedule was not found.")
            if current.version != expected_version:
                raise VersionConflict("stale schedule version.")
            if current.state is not ScheduleState.ENABLED:
                raise InvariantViolation("schedule cannot be leased.")
            if now < current.updated_at:
                raise InvariantViolation("schedule timestamp is invalid.")
            self._require_schedule_lease_state(
                current,
                now=now,
                allow_expired=True,
            )
            if current.lease_token is not None and current.lease_expires_at is not None:
                if current.lease_expires_at > now:
                    raise InvariantViolation("schedule lease is already active.")
                if current.lease_token == lease_token:
                    raise InvariantViolation("schedule lease is invalid.")

            leased = replace(
                current,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                updated_at=now,
                version=current.version + 1,
            )
            self._schedules[schedule_id] = self._copy(leased)
            return self._copy(leased)

    def complete_schedule_run(
        self,
        schedule_id: ScheduleId,
        *,
        expected_version: int,
        lease_token: str,
        succeeded: bool,
        completed_at: datetime,
    ) -> Schedule:
        expected_version = require_expected_version(
            expected_version,
            label="schedule completion",
        )
        lease_token = require_schedule_lease_token(lease_token)
        if type(succeeded) is not bool:
            raise ValueError("schedule completion is invalid.")
        completed_at = require_utc_datetime(
            completed_at,
            label="schedule completion",
        )

        with self._lock:
            current = self._schedules.get(schedule_id)
            if current is None:
                raise NotFound("schedule was not found.")
            if current.version != expected_version:
                raise VersionConflict("stale schedule version.")
            if current.state is not ScheduleState.ENABLED:
                raise InvariantViolation("schedule completion is invalid.")
            if completed_at < current.updated_at:
                raise InvariantViolation("schedule timestamp is invalid.")
            self._require_schedule_lease_state(
                current,
                now=completed_at,
                allow_expired=False,
            )
            if current.lease_token != lease_token:
                raise InvariantViolation("schedule lease is invalid.")
            if (
                current.lease_expires_at is None
                or current.lease_expires_at <= completed_at
            ):
                raise InvariantViolation("schedule lease is invalid.")

            next_failures = 0 if succeeded else current.consecutive_failures + 1
            next_state: ScheduleState = current.state
            if not succeeded and next_failures >= 3:
                require_schedule_transition(current.state, ScheduleState.DISABLED)
                next_state = ScheduleState.DISABLED

            completed = replace(
                current,
                state=next_state,
                consecutive_failures=next_failures,
                lease_token=None,
                lease_expires_at=None,
                last_attempt_at=completed_at,
                last_success_at=completed_at if succeeded else current.last_success_at,
                updated_at=completed_at,
                version=current.version + 1,
            )
            self._schedules[schedule_id] = self._copy(completed)
            return self._copy(completed)

    def create_secret_metadata(self, secret: SecretMetadata) -> SecretMetadata:
        return self._create(self._secrets, secret.id, secret, "secret metadata")

    def get_secret_metadata(self, secret_id: SecretId) -> SecretMetadata:
        return self._get(self._secrets, secret_id, "secret metadata")

    def save_secret_metadata(
        self,
        secret: SecretMetadata,
        *,
        expected_version: int,
    ) -> SecretMetadata:
        with self._lock:
            current = self._secrets.get(secret.id)
            if current is None:
                raise NotFound("secret metadata was not found.")
            self._validate_next_version_locked(
                current,
                secret,
                expected_version,
                "secret metadata",
            )
            self._require_immutable_fields(
                current,
                secret,
                "secret metadata",
                (
                    "id",
                    "owner_id",
                    "name",
                    "integration_type",
                    "created_at",
                ),
            )
            if current.lifecycle_state != secret.lifecycle_state:
                require_secret_lifecycle_transition(
                    current.lifecycle_state,
                    secret.lifecycle_state,
                )
            if current.rotation_state != secret.rotation_state:
                require_secret_rotation_transition(
                    current.rotation_state,
                    secret.rotation_state,
                )
            self._require_legal_secret_update(current=current, secret=secret)
            return self._save_versioned_locked(
                self._secrets,
                secret.id,
                secret,
                expected_version,
                "secret metadata",
            )

    def list_secret_metadata(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[SecretMetadata, ...]:
        with self._lock:
            secrets = (
                secret
                for secret in self._secrets.values()
                if owner_id is None or secret.owner_id == owner_id
            )
            return tuple(
                self._copy(secret)
                for secret in sorted(
                    secrets,
                    key=lambda item: (item.created_at, str(item.id)),
                )
            )

    @staticmethod
    def _require_legal_secret_update(
        *,
        current: SecretMetadata,
        secret: SecretMetadata,
    ) -> None:
        if secret.active_version < current.active_version:
            raise InvariantViolation("secret metadata active version regressed.")
        if (
            secret.retiring_version is not None
            and secret.retiring_version >= secret.active_version
        ):
            raise InvariantViolation("secret retirement metadata is invalid.")
        if current.mutation_state is SecretMutationState.IDLE:
            MemoryStore._require_idle_secret_update(current=current, secret=secret)
            return
        if current.mutation_state is SecretMutationState.CREATING:
            MemoryStore._require_create_secret_update(current=current, secret=secret)
            return
        if current.mutation_state is SecretMutationState.ATTACHING:
            MemoryStore._require_attach_secret_update(current=current, secret=secret)
            return
        if current.mutation_state is SecretMutationState.ROTATING:
            MemoryStore._require_rotate_secret_update(current=current, secret=secret)
            return
        raise InvariantViolation("secret mutation state is invalid.")

    @staticmethod
    def _require_idle_secret_update(
        *,
        current: SecretMetadata,
        secret: SecretMetadata,
    ) -> None:
        if secret.mutation_state is SecretMutationState.IDLE:
            if current.rotation_state is secret.rotation_state:
                if (
                    current.attached_workload_ids != secret.attached_workload_ids
                    or current.active_version != secret.active_version
                ):
                    raise InvariantViolation(
                        "secret metadata changed outside an active mutation."
                    )
            else:
                if (
                    current.attached_workload_ids != secret.attached_workload_ids
                    or current.active_version != secret.active_version
                ):
                    raise InvariantViolation(
                        "secret transition changed immutable runtime bindings."
                    )
            return
        if secret.mutation_state is SecretMutationState.ATTACHING:
            if (
                current.rotation_state is not secret.rotation_state
                or current.active_version != secret.active_version
                or current.attached_workload_ids != secret.attached_workload_ids
            ):
                raise InvariantViolation("secret attachment draft shape is invalid.")
            if secret.pending_workload_ids is None or not set(
                current.attached_workload_ids
            ).issubset(set(secret.pending_workload_ids)):
                raise InvariantViolation("secret attachment cannot detach workloads.")
            return
        if secret.mutation_state is SecretMutationState.ROTATING:
            if (
                current.active_version != secret.active_version
                or current.attached_workload_ids != secret.attached_workload_ids
                or secret.rotation_state.value != "rotating"
            ):
                raise InvariantViolation("secret rotation draft shape is invalid.")
            if secret.pending_workload_ids is None or not set(
                current.attached_workload_ids
            ).issubset(set(secret.pending_workload_ids)):
                raise InvariantViolation("secret rotation cannot detach workloads.")
            return
        raise InvariantViolation("secret create draft must be create-only.")

    @staticmethod
    def _require_create_secret_update(
        *,
        current: SecretMetadata,
        secret: SecretMetadata,
    ) -> None:
        if secret.mutation_state is SecretMutationState.CREATING:
            if (
                secret.attached_workload_ids != current.attached_workload_ids
                or secret.active_version != current.active_version
                or secret.pending_workload_ids != current.pending_workload_ids
                or secret.pending_payload_sha256 != current.pending_payload_sha256
                or secret.mutation_idempotency_key != current.mutation_idempotency_key
                or secret.rotation_state is not current.rotation_state
                or secret.lifecycle_state is not current.lifecycle_state
            ):
                raise InvariantViolation("secret create draft progress is invalid.")
            return
        if secret.mutation_state is not SecretMutationState.IDLE:
            raise InvariantViolation("secret create draft must finalize to idle.")
        if (
            current.pending_workload_ids is None
            or secret.attached_workload_ids != current.pending_workload_ids
            or secret.active_version != current.active_version
            or secret.rotation_state is not SecretRotationState.STABLE
            or secret.lifecycle_state is not current.lifecycle_state
        ):
            raise InvariantViolation("secret create finalization shape is invalid.")

    @staticmethod
    def _require_attach_secret_update(
        *,
        current: SecretMetadata,
        secret: SecretMetadata,
    ) -> None:
        if secret.mutation_state is SecretMutationState.ATTACHING:
            if (
                secret.attached_workload_ids != current.attached_workload_ids
                or secret.active_version != current.active_version
                or secret.pending_workload_ids != current.pending_workload_ids
                or secret.pending_payload_sha256 is not None
                or secret.mutation_idempotency_key != current.mutation_idempotency_key
                or secret.rotation_state is not current.rotation_state
                or secret.lifecycle_state is not current.lifecycle_state
            ):
                raise InvariantViolation("secret attach draft progress is invalid.")
            return
        if secret.mutation_state is not SecretMutationState.IDLE:
            raise InvariantViolation("secret attach draft must finalize to idle.")
        if (
            current.pending_workload_ids is None
            or secret.attached_workload_ids != current.pending_workload_ids
            or secret.active_version != current.active_version
            or secret.rotation_state is not current.rotation_state
            or secret.lifecycle_state is not current.lifecycle_state
        ):
            raise InvariantViolation("secret attach finalization shape is invalid.")

    @staticmethod
    def _require_rotate_secret_update(
        *,
        current: SecretMetadata,
        secret: SecretMetadata,
    ) -> None:
        if secret.mutation_state is SecretMutationState.ROTATING:
            if (
                secret.attached_workload_ids != current.attached_workload_ids
                or secret.active_version != current.active_version
                or secret.pending_workload_ids != current.pending_workload_ids
                or secret.pending_payload_sha256 != current.pending_payload_sha256
                or secret.mutation_idempotency_key != current.mutation_idempotency_key
                or secret.rotation_state is not current.rotation_state
                or secret.lifecycle_state is not current.lifecycle_state
            ):
                raise InvariantViolation("secret rotation draft progress is invalid.")
            return
        if secret.mutation_state is not SecretMutationState.IDLE:
            raise InvariantViolation("secret rotation draft must finalize to idle.")
        if (
            current.pending_workload_ids is None
            or secret.attached_workload_ids != current.pending_workload_ids
            or secret.rotation_state.value != "retiring_old_version"
            or current.rotation_state.value != "rotating"
            or secret.retiring_version != current.active_version
            or secret.active_version <= current.active_version
            or secret.lifecycle_state is not current.lifecycle_state
        ):
            raise InvariantViolation("secret rotation finalization shape is invalid.")

    def append_usage_entry(self, entry: UsageEntry) -> UsageEntry:
        return self._create(self._usage_entries, entry.id, entry, "usage entry")

    def upsert_usage_entry_monotonic(
        self,
        *,
        current: UsageEntry,
        updated: UsageEntry,
    ) -> UsageEntry:
        with self._lock:
            persisted = self._usage_entries.get(current.id)
            if persisted is None:
                raise NotFound("usage entry was not found.")
            if persisted == updated:
                return self._copy(persisted)
            if persisted != current:
                raise VersionConflict("stale usage entry material.")
            self._require_monotonic_usage_update(current=current, updated=updated)
            self._usage_entries[current.id] = self._copy(updated)
            return self._copy(updated)

    def list_usage_entries(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[UsageEntry, ...]:
        with self._lock:
            entries = (
                entry
                for entry in self._usage_entries.values()
                if owner_id is None or entry.owner_id == owner_id
            )
            return tuple(
                self._copy(entry)
                for entry in sorted(
                    entries,
                    key=lambda item: (item.collected_at, str(item.id)),
                )
            )

    def create_org_cost_guard(self, guard: OrgCostGuard) -> OrgCostGuard:
        return self._create(
            self._org_cost_guards,
            "organization",
            guard,
            "org cost guard",
        )

    def get_org_cost_guard(self) -> OrgCostGuard:
        return self._get(self._org_cost_guards, "organization", "org cost guard")

    def save_org_cost_guard(
        self,
        guard: OrgCostGuard,
        *,
        expected_version: int,
    ) -> OrgCostGuard:
        with self._lock:
            return self._save_versioned_locked(
                self._org_cost_guards,
                "organization",
                guard,
                expected_version,
                "org cost guard",
            )

    def append_activity_event(self, event: ActivityEvent) -> ActivityEvent:
        return self._create(self._activity_events, event.id, event, "activity event")

    def list_activity_events(
        self,
        *,
        user_id: UserId | None = None,
    ) -> tuple[ActivityEvent, ...]:
        with self._lock:
            events = (
                event
                for event in self._activity_events.values()
                if user_id is None or event.user_id == user_id
            )
            return tuple(
                self._copy(event)
                for event in sorted(
                    events,
                    key=lambda item: (item.occurred_at, str(item.id)),
                )
            )

    def expire_activity_events(
        self,
        *,
        event_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not isinstance(event_ids, tuple):
            raise InvariantViolation("activity event IDs must be immutable.")
        removed: list[str] = []
        seen: set[str] = set()
        with self._lock:
            for event_id in event_ids:
                if (
                    not isinstance(event_id, str)
                    or not event_id.strip()
                    or event_id != event_id.strip()
                ):
                    raise InvariantViolation("activity event ID is invalid.")
                if event_id in seen:
                    raise InvariantViolation("activity event IDs must be unique.")
                seen.add(event_id)
                key = ActivityEventId(event_id)
                if key in self._activity_events:
                    del self._activity_events[key]
                    removed.append(event_id)
        return tuple(removed)

    def create_daily_usage_aggregate(
        self,
        aggregate: DailyUsageAggregate,
    ) -> DailyUsageAggregate:
        key = (aggregate.day, aggregate.user_id)
        return self._create(
            self._daily_aggregates,
            key,
            aggregate,
            "daily usage aggregate",
        )

    def get_daily_usage_aggregate(
        self,
        day: date,
        user_id: UserId | None,
    ) -> DailyUsageAggregate:
        return self._get(
            self._daily_aggregates,
            (day, user_id),
            "daily usage aggregate",
        )

    def save_daily_usage_aggregate(
        self,
        aggregate: DailyUsageAggregate,
        *,
        expected_version: int,
    ) -> DailyUsageAggregate:
        key = (aggregate.day, aggregate.user_id)
        return self._save_versioned(
            self._daily_aggregates,
            key,
            aggregate,
            expected_version,
            "daily usage aggregate",
        )

    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        return self._create(self._audit_events, event.id, event, "audit event")

    def list_audit_events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(
                self._copy(event)
                for event in sorted(
                    self._audit_events.values(),
                    key=lambda item: (item.occurred_at, str(item.id)),
                )
            )

    def create_lifecycle_action(self, action: LifecycleAction) -> LifecycleAction:
        self._require_lifecycle_execution_consistency(action)
        return self._create(
            self._lifecycle_actions, action.id, action, "lifecycle action"
        )

    def get_lifecycle_action(
        self,
        action_id: LifecycleActionId,
    ) -> LifecycleAction:
        return self._get(self._lifecycle_actions, action_id, "lifecycle action")

    def save_lifecycle_action(
        self,
        action: LifecycleAction,
        *,
        expected_version: int,
    ) -> LifecycleAction:
        self._require_lifecycle_execution_consistency(action)
        return self._save_stateful(
            self._lifecycle_actions,
            action.id,
            action,
            expected_version,
            "lifecycle action",
            state_of=lambda item: item.state,
            validate=require_lifecycle_action_transition,
            immutable_fields=(
                "id",
                "workload_id",
                "kind",
                "reason",
                "eligible_at",
                "observed_workload_version",
                "created_at",
            ),
        )

    def get_maintenance_job_status(self, job_name: str) -> MaintenanceJobStatus:
        return self._get(
            self._maintenance_job_statuses,
            job_name,
            "maintenance job status",
        )

    def list_maintenance_job_statuses(self) -> tuple[MaintenanceJobStatus, ...]:
        with self._lock:
            return tuple(
                self._copy(status)
                for status in sorted(
                    self._maintenance_job_statuses.values(),
                    key=lambda item: item.job_name,
                )
            )

    def record_maintenance_job_started(
        self,
        *,
        job_name: str,
        run_id: str,
        started_at: datetime,
    ) -> MaintenanceJobStatus:
        with self._lock:
            current = self._maintenance_job_statuses.get(job_name)
            status = MaintenanceJobStatus(
                job_name=job_name,
                run_id=run_id,
                started_at=started_at,
                finished_at=None,
                succeeded_at=None,
                failed_at=None,
                outcome="started",
                summary=(),
                failure_code=None,
                failure_class=None,
                version=1 if current is None else current.version + 1,
            )
            self._maintenance_job_statuses[job_name] = self._copy(status)
            return self._copy(status)

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
    ) -> MaintenanceJobStatus:
        with self._lock:
            current = self._maintenance_job_statuses.get(job_name)
            if current is None:
                raise NotFound("maintenance job status was not found.")
            if current.version != expected_version:
                raise VersionConflict("stale maintenance job status version.")
            if current.run_id != run_id:
                raise VersionConflict("stale maintenance job run ID.")
            status = MaintenanceJobStatus(
                job_name=job_name,
                run_id=run_id,
                started_at=current.started_at,
                finished_at=finished_at,
                succeeded_at=finished_at if outcome == "completed" else None,
                failed_at=finished_at if outcome == "failed" else None,
                outcome=outcome,
                summary=summary,
                failure_code=failure_code,
                failure_class=failure_class,
                version=current.version + 1,
            )
            self._maintenance_job_statuses[job_name] = self._copy(status)
            return self._copy(status)

    def claim_origin_request(self, claim: OriginRequestClaim) -> None:
        with self._lock:
            if claim.request_id in self._origin_claims:
                raise ReplayDetected("origin request ID was already claimed.")
            self._origin_claims[claim.request_id] = self._copy(claim)

    def create_deploy_task_once(
        self,
        task: QueuedDeployTask,
    ) -> tuple[QueuedDeployTask, bool]:
        with self._lock:
            claimed_operation = self._deploy_task_idempotency.get(task.idempotency_key)
            if claimed_operation is not None:
                existing = self._deploy_tasks.get(claimed_operation)
                if existing is None:
                    raise InvariantViolation("queued deploy task claim is incomplete.")
                if existing.material_hash != task.material_hash:
                    raise TaskConflictError("queued deploy task material changed.")
                return self._copy(existing), False
            existing = self._deploy_tasks.get(task.operation_id)
            if existing is not None:
                if existing.material_hash != task.material_hash:
                    raise TaskConflictError("operation has different queued material.")
                return self._copy(existing), False
            self._deploy_tasks[task.operation_id] = self._copy(task)
            self._deploy_task_idempotency[task.idempotency_key] = task.operation_id
            return self._copy(task), True

    def get_deploy_task(self, operation_id: OperationId) -> QueuedDeployTask:
        with self._lock:
            task = self._deploy_tasks.get(operation_id)
            if task is None:
                raise TaskNotFoundError("queued deploy task was not found.")
            return self._copy(task)

    def _create(
        self,
        collection: dict[_KeyT, _RecordT],
        key: _KeyT,
        record: _RecordT,
        label: str,
    ) -> _RecordT:
        with self._lock:
            if key in collection:
                raise AlreadyExists(f"{label} already exists.")
            version = getattr(record, "version", None)
            if version is not None and version != 1:
                raise VersionConflict(f"new {label} must start at version 1.")
            collection[key] = self._copy(record)
            return self._copy(record)

    def _get(
        self,
        collection: dict[_KeyT, _RecordT],
        key: _KeyT,
        label: str,
    ) -> _RecordT:
        with self._lock:
            record = collection.get(key)
            if record is None:
                raise NotFound(f"{label} was not found.")
            return self._copy(record)

    def _save_versioned(
        self,
        collection: dict[_KeyT, _VersionedT],
        key: _KeyT,
        record: _VersionedT,
        expected_version: int,
        label: str,
    ) -> _VersionedT:
        with self._lock:
            return self._save_versioned_locked(
                collection,
                key,
                record,
                expected_version,
                label,
            )

    def _save_stateful(
        self,
        collection: dict[_KeyT, _VersionedT],
        key: _KeyT,
        record: _VersionedT,
        expected_version: int,
        label: str,
        *,
        state_of: Callable[[_VersionedT], _StateT],
        validate: Callable[[_StateT, _StateT], None],
        immutable_fields: tuple[str, ...] = (),
    ) -> _VersionedT:
        with self._lock:
            current = collection.get(key)
            if current is None:
                raise NotFound(f"{label} was not found.")
            self._validate_next_version_locked(
                current,
                record,
                expected_version,
                label,
            )
            self._require_immutable_fields(
                current,
                record,
                label,
                immutable_fields,
            )
            current_state = state_of(current)
            next_state = state_of(record)
            if current_state != next_state:
                validate(current_state, next_state)
            return self._save_versioned_locked(
                collection,
                key,
                record,
                expected_version,
                label,
            )

    def _save_versioned_locked(
        self,
        collection: dict[_KeyT, _VersionedT],
        key: _KeyT,
        record: _VersionedT,
        expected_version: int,
        label: str,
    ) -> _VersionedT:
        current = collection.get(key)
        if current is None:
            raise NotFound(f"{label} was not found.")
        self._validate_next_version_locked(
            current,
            record,
            expected_version,
            label,
        )
        collection[key] = self._copy(record)
        return self._copy(record)

    @staticmethod
    def _validate_next_version_locked(
        current: _Versioned,
        record: _Versioned,
        expected_version: int,
        label: str,
    ) -> None:
        if current.version != expected_version:
            raise VersionConflict(f"stale {label} version.")
        if record.version != expected_version + 1:
            raise VersionConflict(f"next {label} version must increment exactly once.")
        current_created_at = getattr(current, "created_at", None)
        next_created_at = getattr(record, "created_at", None)
        if current_created_at != next_created_at:
            raise InvariantViolation(f"{label} created_at is immutable.")
        current_updated_at = getattr(current, "updated_at", None)
        next_updated_at = getattr(record, "updated_at", None)
        if (
            current_updated_at is not None
            and next_updated_at is not None
            and next_updated_at < current_updated_at
        ):
            raise InvariantViolation(f"{label} updated_at cannot move backward.")

    @staticmethod
    def _require_immutable_fields(
        current: object,
        record: object,
        label: str,
        field_names: tuple[str, ...],
    ) -> None:
        if any(
            getattr(current, field_name) != getattr(record, field_name)
            for field_name in field_names
        ):
            raise InvariantViolation(f"{label} immutable policy material changed.")

    @staticmethod
    def _require_schedule_lease_state(
        schedule: Schedule,
        *,
        now: datetime,
        allow_expired: bool,
    ) -> None:
        has_token = schedule.lease_token is not None
        has_expiry = schedule.lease_expires_at is not None
        if has_token != has_expiry:
            raise InvariantViolation("schedule lease is invalid.")
        if not has_token:
            return
        if schedule.lease_expires_at is None:
            raise InvariantViolation("schedule lease is invalid.")
        require_schedule_lease_token(schedule.lease_token)
        require_utc_datetime(schedule.lease_expires_at, label="schedule lease")
        if not allow_expired and schedule.lease_expires_at <= now:
            raise InvariantViolation("schedule lease is invalid.")

    @staticmethod
    def _require_lifecycle_execution_consistency(action: LifecycleAction) -> None:
        is_executed = action.state is LifecycleActionState.EXECUTED
        if is_executed != (action.executed_at is not None):
            raise InvariantViolation(
                "lifecycle action execution timestamp is inconsistent with state."
            )

    @staticmethod
    def _copy(record: _RecordT) -> _RecordT:
        return cast(_RecordT, deepcopy(record))

    @staticmethod
    def _require_monotonic_usage_update(
        *,
        current: UsageEntry,
        updated: UsageEntry,
    ) -> None:
        immutable_fields = ("id", "owner_id", "workload_id", "service_category")
        if any(
            getattr(current, field_name) != getattr(updated, field_name)
            for field_name in immutable_fields
        ):
            raise InvariantViolation("usage entry material is immutable.")
        if updated.estimated_cost_krw < current.estimated_cost_krw:
            raise InvariantViolation("usage estimate must not decrease.")
        if current.finalized_cost_krw is not None and (
            updated.finalized_cost_krw is None
            or updated.finalized_cost_krw < current.finalized_cost_krw
        ):
            raise InvariantViolation("finalized usage cost must not decrease.")
        confidence_rank = {
            UsageConfidence.ESTIMATED: 1,
            UsageConfidence.MEASURED: 2,
            UsageConfidence.FINALIZED: 3,
        }
        if confidence_rank[updated.confidence] < confidence_rank[current.confidence]:
            raise InvariantViolation("usage confidence must not weaken.")
        if updated.collected_at < current.collected_at:
            raise InvariantViolation("usage collection time must not decrease.")


def _require_github_delivery_material(
    *,
    delivery_id: str,
    delivery_hash: str,
    source_ref: str,
) -> None:
    if (
        type(delivery_id) is not str
        or _DELIVERY_ID_PATTERN.fullmatch(delivery_id) is None
        or type(delivery_hash) is not str
        or _LOWER_SHA256_PATTERN.fullmatch(delivery_hash) is None
        or type(source_ref) is not str
        or not source_ref.startswith("refs/heads/")
        or ".." in source_ref
    ):
        raise InvariantViolation("GitHub delivery material is invalid.")


def _require_task_matches_auto_deploy(
    *,
    task: QueuedDeployTask,
    workload: Workload,
    admission: RepositoryAdmission,
    operation: Operation,
) -> None:
    if (
        type(task) is not QueuedDeployTask
        or task.operation_id != operation.id
        or task.expected_operation_version != operation.version
        or task.workload_id != workload.id
        or task.expected_workload_version != workload.version
        or task.admission_id != admission.id
        or task.expected_admission_version != admission.version
        or task.expected_source_sha != admission.admitted_sha
        or task.expected_source_sha != workload.source_sha
        or task.idempotency_key != operation.idempotency_key
        or task.queued_at != operation.created_at
    ):
        raise InvariantViolation("automatic deploy task material is invalid.")
