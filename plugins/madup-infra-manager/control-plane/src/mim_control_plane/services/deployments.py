"""Plan-bound deployment orchestration with no direct cloud mutation access."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from mim_control_plane.adapters.github import (
    GitHubSourceError,
    GitHubWebhookError,
    VerifiedGitHubPush,
    verify_github_push,
    verify_github_webhook_signature,
)
from mim_control_plane.config import (
    GITHUB_OWNER,
    PER_USER_SCHEDULE_LIMIT,
    PER_USER_SERVICE_LIMIT,
    PLAN_EXPIRY_MINUTES,
    TARGET_MONTHLY_BUDGET_KRW,
)
from mim_control_plane.domain.models import (
    DeploymentPlan,
    DeploymentPlanId,
    Operation,
    OperationId,
    RepositoryAdmission,
    RepositoryAdmissionId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.plans import (
    PlanValidationError,
    consume_plan_with_operation,
    hash_plan_material,
    validate_consumed_plan_repair,
    validate_plan_request,
)
from mim_control_plane.domain.states import (
    OperationState,
    PlanState,
    RepositoryAdmissionState,
    SecretLifecycleState,
    UserRole,
    UserState,
    WorkloadState,
)
from mim_control_plane.ports.execution import (
    DeploymentQueueReceipt,
    PrivateDeployEnqueuer,
    QueuedDeployTask,
    SnapshotAttestation,
    TaskConflictError,
    TaskNotFoundError,
)
from mim_control_plane.ports.source import SourceSnapshotPort
from mim_control_plane.ports.store import (
    AUTO_DEPLOY_ACTOR_ID,
    AlreadyExists,
    IdempotencyConflict,
    InvariantViolation,
    NotFound,
    ReplayDetected,
    Store,
    StoreError,
    VersionConflict,
)
from mim_control_plane.security.authorization import (
    AccessDenied,
    require_owner_or_admin,
)
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.services.audit import build_audit_record
from mim_control_plane.services.build_template import BuildTemplate, build_template_for
from mim_control_plane.services.classifier import (
    ClassificationQuestion,
    ManifestValidationError,
    SnapshotValidationError,
    WorkloadClassification,
    classify_snapshot,
)
from mim_control_plane.services.org_cost_guard import (
    OrgCostGuardDenied,
    require_current_org_cost_guard,
)
from mim_control_plane.services.quota import (
    ResourceInventory,
    evaluate_cost_policy,
    evaluate_resource_policy,
)
from mim_control_plane.services.render import (
    DesiredStateDenied,
    DesiredStateRenderContext,
    SignedDesiredStateEnvelope,
    render_signed_desired_state,
)
from mim_control_plane.services.repository_admission import SelectedRepositoryPolicy
from mim_control_plane.services.usage import (
    build_cost_snapshot,
    build_usage_ledger,
    usage_entries_for_utc_month,
)

_POLICY_VERSION = "mim-deploy-v1"
_ACTION = "deploy"
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HEX_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_WORKLOAD_STATES = frozenset({WorkloadState.ACTIVE, WorkloadState.FAILED})
_DEFAULT_GITHUB_REF = "refs/heads/main"


class DeploymentDenied(PermissionError):
    """Sanitized fail-closed deployment denial."""

    def __init__(self, reason_code: str = "deployment_denied") -> None:
        super().__init__("Deployment request was denied.")
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class _ReviewedDeployment:
    workload: Workload
    admission: RepositoryAdmission
    snapshot: dict[str, bytes]
    material: dict[str, object]
    summary: tuple[tuple[str, str], ...]


class DeploymentService:
    """Create reviewed plans and hand durable tasks to the private queue only."""

    def __init__(
        self,
        *,
        store: Store,
        source: SourceSnapshotPort,
        enqueuer: PrivateDeployEnqueuer,
        render_context: DesiredStateRenderContext,
        signing_key: bytes,
        clock: Callable[[], datetime],
        id_factory: Callable[[str], str] | None = None,
        github_policy: SelectedRepositoryPolicy | None = None,
        github_webhook_secret: bytes | None = None,
    ) -> None:
        if type(render_context) is not DesiredStateRenderContext:
            raise ValueError("render_context must be exact.")
        if type(signing_key) is not bytes or len(signing_key) < 32:
            raise ValueError("signing_key must contain at least 32 bytes.")
        if (github_policy is None) != (github_webhook_secret is None):
            raise ValueError("GitHub webhook configuration must be complete.")
        if (
            github_policy is not None
            and type(github_policy) is not SelectedRepositoryPolicy
        ):
            raise ValueError("github_policy must be exact.")
        if github_webhook_secret is not None and (
            type(github_webhook_secret) is not bytes
            or len(github_webhook_secret) < 32
        ):
            raise ValueError("github_webhook_secret must contain at least 32 bytes.")
        self._store = store
        self._source = source
        self._enqueuer = enqueuer
        self._render_context = render_context
        self._signing_key = bytes(signing_key)
        self._clock = clock
        self._id_factory = id_factory or _secure_id
        self._github_policy = github_policy
        self._github_webhook_secret = (
            None if github_webhook_secret is None else bytes(github_webhook_secret)
        )

    def plan_deploy(
        self,
        *,
        principal: AuthenticatedPrincipal,
        workload_id: str,
    ) -> dict[str, object]:
        now = self._now()
        try:
            reviewed = self._review(
                principal=principal,
                workload_id=WorkloadId(_require_identifier(workload_id)),
                now=now,
            )
            material_hash = hash_plan_material(
                reviewed.material,
                action=_ACTION,
                policy_version=_POLICY_VERSION,
            )
            plan = self._store.create_deployment_plan(
                DeploymentPlan(
                    id=DeploymentPlanId(self._id_factory("plan")),
                    actor_id=principal.user_id,
                    workload_id=reviewed.workload.id,
                    action=_ACTION,
                    material_hash=material_hash,
                    policy_version=_POLICY_VERSION,
                    state=PlanState.ISSUED,
                    expires_at=now + timedelta(minutes=PLAN_EXPIRY_MINUTES),
                    created_at=now,
                    updated_at=now,
                    sanitized_summary=reviewed.summary,
                )
            )
        except DeploymentDenied:
            raise
        except (
            AccessDenied,
            AlreadyExists,
            DesiredStateDenied,
            GitHubSourceError,
            ManifestValidationError,
            NotFound,
            SnapshotValidationError,
            StoreError,
            ValueError,
        ):
            raise DeploymentDenied() from None
        return {
            "action": "plan_deploy",
            "status": "ready",
            "actor_id": str(plan.actor_id),
            "workload_id": str(reviewed.workload.id),
            "plan_id": str(plan.id),
            "plan_hash": plan.material_hash,
            "expires_at": plan.expires_at.isoformat(),
            "material_summary": dict(plan.sanitized_summary),
        }

    def deploy_from_plan(
        self,
        *,
        principal: AuthenticatedPrincipal,
        plan_id: str,
        plan_hash: str,
        idempotency_key: str,
        correlation_id: str,
    ) -> dict[str, object]:
        now = self._now()
        try:
            normalized_plan_id = DeploymentPlanId(_require_identifier(plan_id))
            normalized_plan_hash = _require_hash(plan_hash)
            normalized_idempotency = _require_idempotency_key(idempotency_key)
            normalized_correlation = _require_correlation_id(correlation_id)
            plan = self._store.get_deployment_plan(normalized_plan_id)
            if not hmac.compare_digest(plan.material_hash, normalized_plan_hash):
                raise DeploymentDenied("plan_hash_mismatch")
            if plan.actor_id != principal.user_id or plan.workload_id is None:
                raise DeploymentDenied("plan_actor_mismatch")
            requested_operation = Operation(
                id=OperationId(self._id_factory("operation")),
                actor_id=principal.user_id,
                workload_id=plan.workload_id,
                action=_ACTION,
                idempotency_key=normalized_idempotency,
                request_hash=plan.material_hash,
                state=OperationState.QUEUED,
                created_at=now,
                updated_at=now,
            )
            was_consumed = plan.state is PlanState.CONSUMED
            repair_missing_task = False
            if was_consumed:
                consumed_plan, operation = (
                    self._store.consume_deployment_plan_with_operation(
                        plan_id=plan.id,
                        actor_id=principal.user_id,
                        expected_material_hash=plan.material_hash,
                        expected_action=_ACTION,
                        policy_version=_POLICY_VERSION,
                        consumed_at=now,
                        operation=requested_operation,
                    )
                )
                try:
                    durable_task = self._store.get_deploy_task(operation.id)
                except TaskNotFoundError:
                    # The plan and operation commit precedes task persistence. A
                    # retry must be able to reconstruct that missing durable task.
                    repair_missing_task = True
                else:
                    self._append_audit_once(
                        plan=consumed_plan,
                        operation=operation,
                        correlation_id=normalized_correlation,
                        occurred_at=operation.created_at,
                    )
                    receipt = (
                        self._enqueue_durable_task(durable_task)
                        if operation.state is OperationState.QUEUED
                        else None
                    )
                    return {
                        "action": "deploy_from_plan",
                        "operation_id": str(operation.id),
                        "state": operation.state.value,
                        "queued": operation.state is OperationState.QUEUED,
                        "replayed": True if receipt is None else not receipt.created,
                    }
            reviewed = self._review(
                principal=principal,
                workload_id=plan.workload_id,
                now=now,
            )
            if repair_missing_task:
                validate_consumed_plan_repair(
                    plan,
                    actor_id=principal.user_id,
                    material=reviewed.material,
                    action=_ACTION,
                    policy_version=_POLICY_VERSION,
                )
                consumed_plan, operation = (
                    self._store.consume_deployment_plan_with_operation(
                        plan_id=plan.id,
                        actor_id=principal.user_id,
                        expected_material_hash=plan.material_hash,
                        expected_action=_ACTION,
                        policy_version=_POLICY_VERSION,
                        consumed_at=now,
                        operation=requested_operation,
                    )
                )
            else:
                validate_plan_request(
                    plan,
                    actor_id=principal.user_id,
                    material=reviewed.material,
                    action=_ACTION,
                    policy_version=_POLICY_VERSION,
                    at=now,
                )
                consumed_plan, operation = consume_plan_with_operation(
                    self._store,
                    plan_id=plan.id,
                    actor_id=principal.user_id,
                    material=reviewed.material,
                    action=_ACTION,
                    policy_version=_POLICY_VERSION,
                    operation=requested_operation,
                    consumed_at=now,
                )
            task = QueuedDeployTask.from_snapshot(
                operation_id=operation.id,
                expected_operation_version=operation.version,
                workload_id=reviewed.workload.id,
                expected_workload_version=reviewed.workload.version,
                admission_id=reviewed.admission.id,
                expected_admission_version=reviewed.admission.version,
                expected_source_sha=reviewed.workload.source_sha,
                idempotency_key=operation.idempotency_key,
                queued_at=operation.created_at,
                snapshot=reviewed.snapshot,
            )
            durable_task, task_created = self._store.create_deploy_task_once(task)
            if durable_task.material_hash != task.material_hash:
                raise InvariantViolation("durable deploy task changed.")
            self._append_audit_once(
                plan=consumed_plan,
                operation=operation,
                correlation_id=normalized_correlation,
                occurred_at=operation.created_at,
            )
            receipt = self._enqueuer.enqueue(
                operation_id=durable_task.operation_id,
                expected_operation_version=durable_task.expected_operation_version,
                workload_id=durable_task.workload_id,
                expected_workload_version=durable_task.expected_workload_version,
                admission_id=durable_task.admission_id,
                expected_admission_version=durable_task.expected_admission_version,
                expected_source_sha=durable_task.expected_source_sha,
                idempotency_key=durable_task.idempotency_key,
                queued_at=durable_task.queued_at,
                snapshot=reviewed.snapshot,
                secret_attachments=durable_task.secret_attachments,
            )
        except DeploymentDenied:
            raise
        except (
            AccessDenied,
            DesiredStateDenied,
            IdempotencyConflict,
            InvariantViolation,
            ManifestValidationError,
            NotFound,
            PlanValidationError,
            SnapshotValidationError,
            StoreError,
            TaskConflictError,
            ValueError,
            VersionConflict,
        ):
            raise DeploymentDenied() from None
        return {
            "action": "deploy_from_plan",
            "operation_id": str(operation.id),
            "state": operation.state.value,
            "queued": True,
            "replayed": was_consumed or not task_created or not receipt.created,
        }

    def deploy_from_github_webhook(
        self,
        *,
        body: bytes,
        signature_header: str,
        event_name: str,
        delivery_id: str,
    ) -> dict[str, object]:
        """Verify one GitHub push and durably create its deterministic task."""

        now = self._now()
        try:
            policy, webhook_secret = self._require_github_configuration()
            verify_github_webhook_signature(
                body=body,
                signature_header=signature_header,
                webhook_secret=webhook_secret,
            )
            verified = self._verify_push_against_durable_refs(
                body=body,
                signature_header=signature_header,
                event_name=event_name,
                delivery_id=delivery_id,
                policy=policy,
                webhook_secret=webhook_secret,
            )
            current = self._auto_deploy_workload_for(verified)
            owner = self._store.get_user(current.owner_id)
            if owner.state is not UserState.ACTIVE:
                raise DeploymentDenied("owner_inactive")
            admission_id = RepositoryAdmissionId(
                _delivery_scoped_id("github-admission", verified.delivery_id)
            )
            plan_id = DeploymentPlanId(
                _delivery_scoped_id("github-plan", verified.delivery_id)
            )
            operation_id = OperationId(
                _delivery_scoped_id("github-operation", verified.delivery_id)
            )
            is_recovery = (
                current.repository_admission_id == admission_id
                and current.source_sha == verified.sha
            )
            if is_recovery:
                admission = self._store.get_repository_admission(admission_id)
                proposed = current
                snapshot = _copy_snapshot(self._source.fetch_snapshot(admission))
            else:
                if current.source_sha == verified.sha:
                    raise DeploymentDenied("source_sha_not_new")
                admission = RepositoryAdmission(
                    id=admission_id,
                    repository_numeric_id=verified.repository_numeric_id,
                    owner=verified.owner,
                    name=verified.name,
                    installation_id=verified.installation_id,
                    state=RepositoryAdmissionState.ADMITTED,
                    admitted_sha=verified.sha,
                    created_at=now,
                    updated_at=now,
                )
                snapshot = _copy_snapshot(self._source.fetch_snapshot(admission))
                classification = classify_snapshot(snapshot)
                if (
                    isinstance(classification, ClassificationQuestion)
                    or type(classification) is not WorkloadClassification
                    or classification.kind is not current.kind
                ):
                    raise DeploymentDenied("classification_denied")
                proposed = current.advance_source(
                    repository_admission_id=admission.id,
                    source_sha=admission.admitted_sha,
                    desired_manifest_hash=_desired_manifest_hash(
                        snapshot=snapshot,
                        classification=classification,
                        template=build_template_for(classification),
                    ),
                    at=now,
                )
            auto_principal = AuthenticatedPrincipal(
                user_id=AUTO_DEPLOY_ACTOR_ID,
                email="github-auto-deploy@mim.internal",
                role=UserRole.ADMIN,
            )
            reviewed = self._review_loaded(
                principal=auto_principal,
                workload=proposed,
                admission=admission,
                snapshot=snapshot,
                now=now,
            )
            if is_recovery:
                plan = self._store.get_deployment_plan(plan_id)
                operation = self._store.get_operation(operation_id)
                durable_task = self._store.get_deploy_task(operation_id)
                if (
                    plan.actor_id != AUTO_DEPLOY_ACTOR_ID
                    or plan.workload_id != proposed.id
                    or plan.action != _ACTION
                    or plan.policy_version != _POLICY_VERSION
                    or plan.state is not PlanState.CONSUMED
                    or operation.actor_id != AUTO_DEPLOY_ACTOR_ID
                    or operation.workload_id != proposed.id
                    or operation.action != _ACTION
                    or operation.idempotency_key
                    != f"github:{verified.delivery_id}"
                    or operation.request_hash != plan.material_hash
                    or durable_task.operation_id != operation.id
                    or durable_task.workload_id != proposed.id
                    or durable_task.admission_id != admission.id
                    or durable_task.expected_admission_version
                    != admission.version
                    or durable_task.expected_source_sha != proposed.source_sha
                    or durable_task.idempotency_key
                    != operation.idempotency_key
                    or not _task_matches_snapshot(
                        task=durable_task,
                        snapshot=snapshot,
                    )
                ):
                    raise DeploymentDenied("auto_deploy_recovery_changed")
                if (
                    operation.state is OperationState.QUEUED
                    and (
                        operation.version
                        != durable_task.expected_operation_version
                        or proposed.version
                        != durable_task.expected_workload_version
                    )
                ):
                    raise DeploymentDenied("auto_deploy_recovery_changed")
                task = durable_task
                expected_workload_version = proposed.version
            else:
                material_hash = hash_plan_material(
                    reviewed.material,
                    action=_ACTION,
                    policy_version=_POLICY_VERSION,
                )
                plan = DeploymentPlan(
                    id=plan_id,
                    actor_id=AUTO_DEPLOY_ACTOR_ID,
                    workload_id=proposed.id,
                    action=_ACTION,
                    material_hash=material_hash,
                    policy_version=_POLICY_VERSION,
                    state=PlanState.ISSUED,
                    expires_at=now + timedelta(minutes=PLAN_EXPIRY_MINUTES),
                    created_at=now,
                    updated_at=now,
                    sanitized_summary=reviewed.summary,
                )
                operation = Operation(
                    id=operation_id,
                    actor_id=AUTO_DEPLOY_ACTOR_ID,
                    workload_id=proposed.id,
                    action=_ACTION,
                    idempotency_key=f"github:{verified.delivery_id}",
                    request_hash=plan.material_hash,
                    state=OperationState.QUEUED,
                    created_at=now,
                    updated_at=now,
                )
                task = QueuedDeployTask.from_snapshot(
                    operation_id=operation.id,
                    expected_operation_version=operation.version,
                    workload_id=proposed.id,
                    expected_workload_version=proposed.version,
                    admission_id=admission.id,
                    expected_admission_version=admission.version,
                    expected_source_sha=proposed.source_sha,
                    idempotency_key=operation.idempotency_key,
                    queued_at=operation.created_at,
                    snapshot=snapshot,
                )
                expected_workload_version = current.version
            committed = self._store.apply_github_auto_deploy_once(
                delivery_id=verified.delivery_id,
                delivery_hash=hashlib.sha256(body).hexdigest(),
                source_ref=verified.ref,
                expected_workload_version=expected_workload_version,
                admission=admission,
                workload=proposed,
                plan=plan,
                operation=operation,
                task=task,
                consumed_at=now,
            )
            self._append_audit_once(
                plan=committed.plan,
                operation=committed.operation,
                correlation_id=f"github:{verified.delivery_id}",
                occurred_at=committed.operation.created_at,
            )
            receipt = (
                self._enqueue_durable_task(committed.task)
                if committed.operation.state is OperationState.QUEUED
                else None
            )
        except DeploymentDenied:
            raise
        except (
            AlreadyExists,
            DesiredStateDenied,
            GitHubWebhookError,
            GitHubSourceError,
            IdempotencyConflict,
            InvariantViolation,
            ManifestValidationError,
            NotFound,
            ReplayDetected,
            SnapshotValidationError,
            StoreError,
            TaskConflictError,
            ValueError,
            VersionConflict,
        ):
            raise DeploymentDenied("github_auto_deploy_denied") from None
        return {
            "action": "github_auto_deploy",
            "operation_id": str(committed.operation.id),
            "workload_id": str(committed.workload.id),
            "state": committed.operation.state.value,
            "queued": committed.operation.state is OperationState.QUEUED,
            "replayed": (
                committed.replayed or receipt is None or not receipt.created
            ),
        }

    def _review(
        self,
        *,
        principal: AuthenticatedPrincipal,
        workload_id: WorkloadId,
        now: datetime,
    ) -> _ReviewedDeployment:
        workload = self._store.get_workload(workload_id)
        require_owner_or_admin(principal, workload.owner_id)
        owner = self._store.get_user(workload.owner_id)
        if owner.state is not UserState.ACTIVE:
            raise DeploymentDenied("owner_inactive")
        if workload.state not in _SAFE_WORKLOAD_STATES:
            raise DeploymentDenied("workload_unavailable")
        admission = self._store.get_repository_admission(
            workload.repository_admission_id
        )
        if (
            admission.state is not RepositoryAdmissionState.ADMITTED
            or admission.owner != GITHUB_OWNER
            or admission.id != workload.repository_admission_id
            or admission.admitted_sha != workload.source_sha
        ):
            raise DeploymentDenied("source_admission_changed")
        snapshot = _copy_snapshot(self._source.fetch_snapshot(admission))
        return self._review_loaded(
            principal=principal,
            workload=workload,
            admission=admission,
            snapshot=snapshot,
            now=now,
        )

    def _review_loaded(
        self,
        *,
        principal: AuthenticatedPrincipal,
        workload: Workload,
        admission: RepositoryAdmission,
        snapshot: dict[str, bytes],
        now: datetime,
    ) -> _ReviewedDeployment:
        owner = self._store.get_user(workload.owner_id)
        if owner.state is not UserState.ACTIVE:
            raise DeploymentDenied("owner_inactive")
        if workload.state not in _SAFE_WORKLOAD_STATES:
            raise DeploymentDenied("workload_unavailable")
        if (
            admission.state is not RepositoryAdmissionState.ADMITTED
            or admission.owner != GITHUB_OWNER
            or admission.id != workload.repository_admission_id
            or admission.admitted_sha != workload.source_sha
        ):
            raise DeploymentDenied("source_admission_changed")
        classification = classify_snapshot(snapshot)
        if (
            isinstance(classification, ClassificationQuestion)
            or type(classification) is not WorkloadClassification
            or classification.kind is not workload.kind
        ):
            raise DeploymentDenied("classification_denied")
        template = build_template_for(classification)
        preview = _render_preview(
            workload=workload,
            admission=admission,
            snapshot=snapshot,
            context=self._render_context,
            signing_key=self._signing_key,
            issued_at=now,
        )
        resource_decision, cost_decision = self._policy_decisions(
            workload=workload
        )
        if resource_decision.service_limit_reached:
            raise DeploymentDenied("service_quota_exceeded")
        if (
            classification.schedule_cron is not None
            and resource_decision.schedule_limit_reached
        ):
            raise DeploymentDenied("schedule_quota_exceeded")
        if (
            cost_decision.block_new
            or cost_decision.pause
            or cost_decision.emergency_stop
        ):
            raise DeploymentDenied("cost_policy_denied")
        material = _plan_material(
            principal=principal,
            workload=workload,
            admission=admission,
            snapshot=snapshot,
            classification=classification,
            template=template,
            preview=preview,
            resource_reason_codes=resource_decision.reason_codes,
            cost_reason_codes=cost_decision.reason_codes,
            cost_policy_krw=cost_decision.projected_user_cost_krw,
        )
        summary_items: list[tuple[str, str]] = [
            ("repository_owner", admission.owner),
            ("repository_name", admission.name),
        ]
        if workload.auto_deploy_ref is not None:
            summary_items.append(("selected_ref", workload.auto_deploy_ref))
        summary_items.extend(
            [
                ("immutable_sha", workload.source_sha),
                ("source_root", "."),
                ("workload_kind", classification.kind.value),
                ("deployment_target", preview.payload.target.value),
                (
                    "resource_impact",
                    (
                        "upsert_cloud_run_job"
                        if classification.schedule_cron is not None
                        else "upsert_cloud_run_service"
                    ),
                ),
                (
                    "current_month_policy_cost_krw",
                    str(cost_decision.projected_user_cost_krw),
                ),
                ("monthly_budget_cap_krw", str(TARGET_MONTHLY_BUDGET_KRW)),
                ("service_quota_limit", str(PER_USER_SERVICE_LIMIT)),
                ("schedule_quota_limit", str(PER_USER_SCHEDULE_LIMIT)),
            ]
        )
        return _ReviewedDeployment(
            workload=workload,
            admission=admission,
            snapshot=snapshot,
            material=material,
            summary=tuple(summary_items),
        )

    def _require_github_configuration(
        self,
    ) -> tuple[SelectedRepositoryPolicy, bytes]:
        if self._github_policy is None or self._github_webhook_secret is None:
            raise DeploymentDenied("github_webhook_unavailable")
        return self._github_policy, self._github_webhook_secret

    def _verify_push_against_durable_refs(
        self,
        *,
        body: bytes,
        signature_header: str,
        event_name: str,
        delivery_id: str,
        policy: SelectedRepositoryPolicy,
        webhook_secret: bytes,
    ) -> VerifiedGitHubPush:
        refs = {
            workload.auto_deploy_ref
            for workload in self._store.list_workloads()
            if workload.auto_deploy_enabled and workload.auto_deploy_ref is not None
        }
        candidates = tuple(sorted(refs)) or (_DEFAULT_GITHUB_REF,)
        for allowed_ref in candidates:
            try:
                return verify_github_push(
                    body=body,
                    signature_header=signature_header,
                    webhook_secret=webhook_secret,
                    event_name=event_name,
                    delivery_id=delivery_id,
                    allowed_ref=allowed_ref,
                    policy=policy,
                )
            except GitHubWebhookError:
                continue
        raise DeploymentDenied("github_push_denied")

    def _auto_deploy_workload_for(
        self,
        verified: VerifiedGitHubPush,
    ) -> Workload:
        matches: list[Workload] = []
        for workload in self._store.list_workloads():
            if (
                workload.auto_deploy_enabled is not True
                or workload.auto_deploy_ref != verified.ref
                or workload.state not in _SAFE_WORKLOAD_STATES
            ):
                continue
            try:
                admission = self._store.get_repository_admission(
                    workload.repository_admission_id
                )
            except NotFound:
                continue
            if (
                admission.state is RepositoryAdmissionState.ADMITTED
                and admission.repository_numeric_id
                == verified.repository_numeric_id
                and admission.owner == verified.owner
                and admission.name == verified.name
                and admission.installation_id == verified.installation_id
            ):
                matches.append(workload)
        if len(matches) != 1:
            raise DeploymentDenied("auto_deploy_target_denied")
        return matches[0]

    def _enqueue_durable_task(
        self,
        task: QueuedDeployTask,
    ) -> DeploymentQueueReceipt:
        return self._enqueuer.enqueue_task(task)

    def _policy_decisions(self, *, workload: Workload) -> tuple[Any, Any]:
        active_services = sum(
            1
            for item in self._store.list_workloads(owner_id=workload.owner_id)
            if item.id != workload.id and item.state is not WorkloadState.ARCHIVED
        )
        active_schedules = sum(
            1
            for item in self._store.list_schedules(owner_id=workload.owner_id)
            if item.workload_id != workload.id and item.state.value != "archived"
        )
        active_secrets = sum(
            1
            for item in self._store.list_secret_metadata(owner_id=workload.owner_id)
            if item.lifecycle_state is not SecretLifecycleState.DESTROYED
        )
        resource = evaluate_resource_policy(
            ResourceInventory(
                active_services=active_services,
                active_schedules=active_schedules,
                active_secrets=active_secrets,
            )
        )
        now = self._now()
        try:
            require_current_org_cost_guard(store=self._store, now=now)
        except OrgCostGuardDenied as exc:
            raise DeploymentDenied("cost_policy_denied") from exc
        ledger = build_usage_ledger(
            usage_entries_for_utc_month(
                self._store.list_usage_entries(owner_id=workload.owner_id),
                now=now,
            )
        )
        return resource, evaluate_cost_policy(
            snapshot=build_cost_snapshot(ledger, user_id=workload.owner_id)
        )

    def _append_audit_once(
        self,
        *,
        plan: DeploymentPlan,
        operation: Operation,
        correlation_id: str,
        occurred_at: datetime,
    ) -> None:
        record = build_audit_record(
            event_id=f"audit-{operation.id}",
            actor_id=operation.actor_id,
            action=_ACTION,
            target_ref=f"workload:{operation.workload_id}",
            policy_decision="allowed",
            correlation_id=correlation_id,
            outcome="queued",
            occurred_at=occurred_at,
            plan=plan,
            after_ref=f"operation:{operation.id}",
        )
        try:
            self._store.append_audit_event(record.event)
        except AlreadyExists:
            return

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise DeploymentDenied("clock_invalid")
        return value


def _secure_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def _delivery_scoped_id(prefix: str, delivery_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"mim:github-auto-deploy:v1\x00")
    digest.update(prefix.encode("ascii"))
    digest.update(b"\x00")
    digest.update(delivery_id.encode("ascii"))
    return f"{prefix}-{digest.hexdigest()[:24]}"


def _desired_manifest_hash(
    *,
    snapshot: Mapping[str, bytes],
    classification: WorkloadClassification,
    template: BuildTemplate,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"mim:desired-manifest:v1\x00")
    for path, content in sorted(snapshot.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(content).digest())
    for value in (
        classification.kind.value,
        classification.entrypoint,
        classification.schedule_cron or "",
        template.runtime,
        *template.install_command,
        *template.build_command,
        *template.launch_command,
        *template.required_files,
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _require_identifier(value: str) -> str:
    if type(value) is not str or _IDEMPOTENCY_PATTERN.fullmatch(value) is None:
        raise DeploymentDenied("identifier_invalid")
    return value


def _require_hash(value: str) -> str:
    if type(value) is not str or _HEX_SHA256_PATTERN.fullmatch(value) is None:
        raise DeploymentDenied("plan_hash_invalid")
    return value


def _require_idempotency_key(value: str) -> str:
    if (
        type(value) is not str
        or _IDEMPOTENCY_PATTERN.fullmatch(value) is None
        or value.startswith("github:")
    ):
        raise DeploymentDenied("idempotency_key_invalid")
    return value


def _require_correlation_id(value: str) -> str:
    if type(value) is not str or _CORRELATION_PATTERN.fullmatch(value) is None:
        raise DeploymentDenied("correlation_id_invalid")
    return value


def _copy_snapshot(snapshot: Mapping[str, bytes]) -> dict[str, bytes]:
    if not isinstance(snapshot, Mapping):
        raise DeploymentDenied("snapshot_invalid")
    copied: dict[str, bytes] = {}
    for path, content in snapshot.items():
        if type(path) is not str or type(content) is not bytes:
            raise DeploymentDenied("snapshot_invalid")
        copied[path] = bytes(content)
    return copied


def _task_matches_snapshot(
    *,
    task: QueuedDeployTask,
    snapshot: Mapping[str, bytes],
) -> bool:
    attestation = SnapshotAttestation.from_snapshot(snapshot)
    return (
        task.expected_snapshot_digest == attestation.digest
        and task.expected_snapshot_file_count == attestation.file_count
        and task.expected_snapshot_byte_count == attestation.byte_count
    )


def _render_preview(
    *,
    workload: Workload,
    admission: RepositoryAdmission,
    snapshot: dict[str, bytes],
    context: DesiredStateRenderContext,
    signing_key: bytes,
    issued_at: datetime,
) -> SignedDesiredStateEnvelope:
    digest = hashlib.sha256(b"mim:review-render:v1\x00")
    for path, content in sorted(snapshot.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(content).digest())
    return render_signed_desired_state(
        workload=workload,
        admission=admission,
        snapshot=snapshot,
        image_digest=digest.hexdigest(),
        context=context,
        issued_at=issued_at,
        signing_key=signing_key,
    )


def _plan_material(
    *,
    principal: AuthenticatedPrincipal,
    workload: Workload,
    admission: RepositoryAdmission,
    snapshot: Mapping[str, bytes],
    classification: WorkloadClassification,
    template: BuildTemplate,
    preview: SignedDesiredStateEnvelope,
    resource_reason_codes: tuple[str, ...],
    cost_reason_codes: tuple[str, ...],
    cost_policy_krw: int,
) -> dict[str, object]:
    payload = preview.payload
    return {
        "schema": "mim-deployment-plan-v1",
        "actor_id": str(principal.user_id),
        "workload": {
            "id": str(workload.id),
            "owner_id": str(workload.owner_id),
            "version": workload.version,
            "kind": workload.kind.value,
            "source_sha": workload.source_sha,
            "manifest_hash": workload.desired_manifest_hash,
        },
        "admission": {
            "id": str(admission.id),
            "version": admission.version,
            "repository_numeric_id": admission.repository_numeric_id,
            "owner": admission.owner,
            "name": admission.name,
            "installation_id": admission.installation_id,
            "sha": admission.admitted_sha,
        },
        "snapshot": {
            "digest": payload.snapshot_digest,
            "file_count": len(snapshot),
            "byte_count": sum(len(content) for content in snapshot.values()),
        },
        "classification": {
            "kind": classification.kind.value,
            "entrypoint": classification.entrypoint,
            "schedule_cron": classification.schedule_cron,
        },
        "trusted_render": {
            "target": payload.target.value,
            "auth_mode": payload.auth_mode.value,
            "ingress": payload.ingress.value,
            "allow_unauthenticated": payload.allow_unauthenticated,
            "custom_domain": payload.custom_domain,
            "runtime": template.runtime,
            "install_command": template.install_command,
            "build_command": template.build_command,
            "launch_command": template.launch_command,
            "required_files": template.required_files,
            "service_min_instances": payload.service_min_instances,
            "service_max_instances": payload.service_max_instances,
        },
        "policy": {
            "version": _POLICY_VERSION,
            "resource_reason_codes": resource_reason_codes,
            "cost_reason_codes": cost_reason_codes,
            "user_policy_krw": cost_policy_krw,
        },
    }


__all__ = ["DeploymentDenied", "DeploymentService"]
