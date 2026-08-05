"""Plan-bound secret lifecycle management with write-only value handling."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol, cast

from mim_control_plane.adapters.secret_manager import (
    ManagedSecretMetadata,
    ObservedSecretState,
    SecretVersionMetadata,
    SecretVersionStateMetadata,
)
from mim_control_plane.domain.models import (
    DeploymentPlan,
    DeploymentPlanId,
    Operation,
    OperationId,
    SecretId,
    SecretMetadata,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.plans import (
    PlanExpired,
    PlanValidationError,
    consume_plan_with_operation,
    hash_plan_material,
)
from mim_control_plane.domain.states import (
    OperationState,
    PlanState,
    SecretLifecycleState,
    SecretMutationState,
    SecretRotationState,
    UserState,
    WorkloadState,
)
from mim_control_plane.ports.execution import SecretAttachmentReference
from mim_control_plane.ports.store import (
    IdempotencyConflict,
    InvariantViolation,
    NotFound,
    Store,
    StoreError,
    VersionConflict,
)
from mim_control_plane.security.authorization import (
    AccessDenied,
    require_owner_or_admin,
)
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.services.org_cost_guard import (
    OrgCostGuardDenied,
    require_current_org_cost_guard,
)
from mim_control_plane.services.quota import (
    ResourceInventory,
    evaluate_cost_policy,
    evaluate_resource_policy,
)
from mim_control_plane.services.usage import (
    build_cost_snapshot,
    build_usage_ledger,
    usage_entries_for_utc_month,
)

_PLAN_ACTION = "manage_secret"
_PLAN_POLICY_VERSION = "mim-secret-v1"
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HEX_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECRET_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SAFE_WORKLOAD_STATES = frozenset(
    {
        WorkloadState.ACTIVE,
        WorkloadState.PAUSED,
        WorkloadState.FAILED,
        WorkloadState.QUARANTINED,
    }
)
_RETIREMENT_WINDOW = timedelta(days=7)
_MAX_WORKLOAD_ATTACHMENTS = 5
_MAX_PAYLOAD_BYTES = 16 * 1024
_NONE_SENTINEL = "none"


class SecretDenied(PermissionError):
    """Sanitized fail-closed denial for secret mutation requests."""

    def __init__(self, reason_code: str = "secret_denied") -> None:
        super().__init__("Secret request was denied.")
        self.reason_code = reason_code


class ManagedSecretPort(Protocol):
    def ensure_secret(
        self,
        *,
        secret_id: SecretId,
        workload_ids: tuple[WorkloadId, ...],
    ) -> ManagedSecretMetadata: ...

    def add_version(
        self,
        *,
        secret_id: SecretId,
        payload: bytes,
    ) -> SecretVersionMetadata: ...

    def ensure_exact_bindings(
        self,
        *,
        secret_id: SecretId,
        workload_ids: tuple[WorkloadId, ...],
    ) -> None: ...

    def disable_old_version(
        self,
        *,
        secret_id: SecretId,
        version_name: str,
        active_version: int,
        retirement_not_before: datetime,
        now: datetime,
    ) -> SecretVersionStateMetadata: ...

    def destroy_old_version(
        self,
        *,
        secret_id: SecretId,
        version_name: str,
        active_version: int,
        retirement_not_before: datetime,
        now: datetime,
    ) -> SecretVersionStateMetadata: ...

    def resolve(
        self,
        *,
        workload_id: WorkloadId,
        attachments: tuple[SecretAttachmentReference, ...],
    ) -> tuple[object, ...]: ...

    def probe_secret(
        self,
        *,
        secret_id: SecretId,
        workload_ids: tuple[WorkloadId, ...],
    ) -> ObservedSecretState: ...


class SecretManagementService:
    def __init__(
        self,
        *,
        store: Store,
        secret_port: ManagedSecretPort,
        clock: Callable[[], datetime],
        id_factory: Callable[[str], str],
    ) -> None:
        self._store = store
        self._secret_port = secret_port
        self._clock = clock
        self._id_factory = id_factory

    def plan_secret_write(
        self,
        *,
        principal: AuthenticatedPrincipal,
        secret_name: str,
        integration_type: str,
        workload_ids: tuple[str, ...],
    ) -> dict[str, object]:
        now = self._now()
        try:
            normalized_secret_name = _require_secret_name(secret_name)
            workloads = self._review_workloads(
                principal=principal,
                workload_ids=workload_ids,
            )
            owner_id = workloads[0].owner_id
            current = self._secret_by_name(
                owner_id=owner_id,
                secret_name=normalized_secret_name,
            )
            if current is None:
                self._require_secret_capacity(owner_id)
                secret_id = SecretId(
                    _stable_secret_id(
                        owner_id=owner_id,
                        secret_name=normalized_secret_name,
                    )
                )
                mode = "create"
                baseline_metadata_version = 0
                baseline_active_version = 0
            else:
                self._require_existing_secret_scope(
                    principal=principal,
                    secret=current,
                    integration_type=integration_type,
                )
                if not set(current.attached_workload_ids).issubset(
                    {workload.id for workload in workloads}
                ):
                    raise SecretDenied("secret_detach_not_allowed")
                secret_id = current.id
                mode = "rotate"
                baseline_metadata_version = current.version
                baseline_active_version = current.active_version
            material = _secret_material(
                mode=mode,
                secret_id=secret_id,
                owner_id=owner_id,
                secret_name=normalized_secret_name,
                integration_type=integration_type,
                workload_ids=tuple(workload.id for workload in workloads),
                baseline_metadata_version=baseline_metadata_version,
                baseline_active_version=baseline_active_version,
            )
            plan = self._store.create_deployment_plan(
                DeploymentPlan(
                    id=DeploymentPlanId(self._id_factory("plan")),
                    actor_id=principal.user_id,
                    workload_id=workloads[0].id,
                    action=_PLAN_ACTION,
                    material_hash=hash_plan_material(
                        material,
                        action=_PLAN_ACTION,
                        policy_version=_PLAN_POLICY_VERSION,
                    ),
                    policy_version=_PLAN_POLICY_VERSION,
                    state=PlanState.ISSUED,
                    expires_at=now + timedelta(minutes=15),
                    created_at=now,
                    updated_at=now,
                    sanitized_summary=_summary_from_material(material),
                )
            )
        except SecretDenied:
            raise
        except (AccessDenied, NotFound, StoreError, ValueError):
            raise SecretDenied() from None
        return {
            "action": "plan_secret_write",
            "plan_id": str(plan.id),
            "plan_hash": plan.material_hash,
            "mode": material["mode"],
            "secret_id": material["secret_id"],
            "secret_name": material["secret_name"],
            "workload_ids": cast(tuple[str, ...], material["workload_ids"]),
        }

    def plan_secret_attach(
        self,
        *,
        principal: AuthenticatedPrincipal,
        secret_id: str,
        workload_ids: tuple[str, ...],
    ) -> dict[str, object]:
        now = self._now()
        try:
            current = self._authorized_secret(
                principal=principal,
                secret_id=SecretId(_require_identifier(secret_id)),
            )
            workloads = self._review_workloads(
                principal=principal,
                workload_ids=workload_ids,
            )
            if any(workload.owner_id != current.owner_id for workload in workloads):
                raise SecretDenied("secret_owner_mismatch")
            desired_ids = tuple(workload.id for workload in workloads)
            if not set(current.attached_workload_ids).issubset(set(desired_ids)):
                raise SecretDenied("secret_detach_not_allowed")
            material = _secret_material(
                mode="attach",
                secret_id=current.id,
                owner_id=current.owner_id,
                secret_name=current.name,
                integration_type=current.integration_type,
                workload_ids=desired_ids,
                baseline_metadata_version=current.version,
                baseline_active_version=current.active_version,
            )
            plan = self._store.create_deployment_plan(
                DeploymentPlan(
                    id=DeploymentPlanId(self._id_factory("plan")),
                    actor_id=principal.user_id,
                    workload_id=workloads[0].id,
                    action=_PLAN_ACTION,
                    material_hash=hash_plan_material(
                        material,
                        action=_PLAN_ACTION,
                        policy_version=_PLAN_POLICY_VERSION,
                    ),
                    policy_version=_PLAN_POLICY_VERSION,
                    state=PlanState.ISSUED,
                    expires_at=now + timedelta(minutes=15),
                    created_at=now,
                    updated_at=now,
                    sanitized_summary=_summary_from_material(material),
                )
            )
        except SecretDenied:
            raise
        except (AccessDenied, NotFound, StoreError, ValueError):
            raise SecretDenied() from None
        return {
            "action": "plan_secret_attach",
            "plan_id": str(plan.id),
            "plan_hash": plan.material_hash,
            "mode": "attach",
            "secret_id": str(current.id),
            "workload_ids": cast(tuple[str, ...], material["workload_ids"]),
        }

    def apply_secret_plan(
        self,
        *,
        principal: AuthenticatedPrincipal,
        plan_id: str,
        plan_hash: str,
        idempotency_key: str,
        payload: bytes | None = None,
    ) -> dict[str, object]:
        now = self._now()
        try:
            normalized_plan_id = DeploymentPlanId(_require_identifier(plan_id))
            normalized_plan_hash = _require_hash(plan_hash)
            normalized_idempotency = _require_idempotency_key(idempotency_key)
            plan = self._store.get_deployment_plan(normalized_plan_id)
            if not hmac.compare_digest(plan.material_hash, normalized_plan_hash):
                raise SecretDenied("plan_hash_mismatch")
            material = _material_from_summary(plan.sanitized_summary)
            if plan.workload_id is None:
                raise SecretDenied("plan_missing_workload")
            _require_secret_name(cast(str, material["secret_name"]))
            workloads = self._review_workloads(
                principal=principal,
                workload_ids=tuple(cast(tuple[str, ...], material["workload_ids"])),
            )
            owner_id = workloads[0].owner_id
            if str(owner_id) != cast(str, material["owner_id"]):
                raise SecretDenied("plan_owner_mismatch")
            mode = cast(str, material["mode"])
            desired_workload_ids = tuple(workload.id for workload in workloads)
            current = self._current_secret_for_material(
                principal=principal,
                material=material,
            )
            was_consumed = plan.state is PlanState.CONSUMED
            if not was_consumed:
                self._require_fresh_baseline(current=current, material=material)
            requested_operation_id = OperationId(self._id_factory("operation"))
            requested_operation = Operation(
                id=requested_operation_id,
                actor_id=principal.user_id,
                workload_id=workloads[0].id,
                action=_PLAN_ACTION,
                idempotency_key=normalized_idempotency,
                request_hash=plan.material_hash,
                state=OperationState.QUEUED,
                created_at=now,
                updated_at=now,
            )
            try:
                consumed_plan, operation = consume_plan_with_operation(
                    self._store,
                    plan_id=plan.id,
                    actor_id=principal.user_id,
                    material=material,
                    action=_PLAN_ACTION,
                    policy_version=_PLAN_POLICY_VERSION,
                    operation=requested_operation,
                    consumed_at=now,
                )
            except PlanExpired:
                if not was_consumed:
                    raise
                consumed_plan, operation = (
                    self._store.consume_deployment_plan_with_operation(
                        plan_id=plan.id,
                        actor_id=principal.user_id,
                        expected_material_hash=plan.material_hash,
                        expected_action=_PLAN_ACTION,
                        policy_version=_PLAN_POLICY_VERSION,
                        consumed_at=now,
                        operation=requested_operation,
                    )
                )
            if operation.id != requested_operation_id:
                if operation.state is OperationState.SUCCEEDED:
                    return _replayed_result_from_operation(operation)
                next_secret = self._resume_or_replay_operation(
                    operation=operation,
                    current=current,
                    material=material,
                    desired_workload_ids=desired_workload_ids,
                    payload=payload,
                    now=now,
                )
                finalized = self._mark_operation_succeeded(
                    operation,
                    at=now,
                    result_summary=_result_summary_for_secret(
                        secret=next_secret,
                        mode=mode,
                    ),
                )
                return _canonical_result_from_secret(
                    operation_id=finalized.id,
                    secret=next_secret,
                    mode=mode,
                    replayed=True,
                )
            next_secret = self._execute_apply(
                operation=operation,
                mode=mode,
                current=current,
                owner_id=owner_id,
                desired_workload_ids=desired_workload_ids,
                material=material,
                payload=payload,
                now=now,
            )
            finalized = self._mark_operation_succeeded(
                operation,
                at=now,
                result_summary=_result_summary_for_secret(
                    secret=next_secret,
                    mode=mode,
                ),
            )
        except SecretDenied:
            raise
        except (
            AccessDenied,
            IdempotencyConflict,
            InvariantViolation,
            NotFound,
            PlanValidationError,
            StoreError,
            ValueError,
            VersionConflict,
        ):
            raise SecretDenied() from None
        except Exception:
            raise SecretDenied() from None
        return _canonical_result_from_secret(
            operation_id=finalized.id,
            secret=next_secret,
            mode=cast(str, material["mode"]),
            replayed=(
                operation.id != requested_operation_id
                or consumed_plan.version > 2
            ),
        )

    def finalize_secret_retirement(self, *, secret_id: str) -> dict[str, object]:
        now = self._now()
        try:
            current = self._store.get_secret_metadata(
                SecretId(_require_identifier(secret_id))
            )
            if current.rotation_state is not SecretRotationState.RETIRING_OLD_VERSION:
                return {
                    "action": "finalize_secret_retirement",
                    "secret_id": str(current.id),
                    "state": "stable",
                    "replayed": True,
                }
            if (
                current.retiring_version is None
                or current.retirement_not_before is None
                or now < current.retirement_not_before
            ):
                return {
                    "action": "finalize_secret_retirement",
                    "secret_id": str(current.id),
                    "state": "pending",
                    "replayed": True,
                }
            managed = self._secret_port.ensure_secret(
                secret_id=current.id,
                workload_ids=current.attached_workload_ids,
            )
            if managed.created:
                raise SecretDenied("secret_resource_missing")
            self._secret_port.destroy_old_version(
                secret_id=current.id,
                version_name=f"{managed.name}/versions/{current.retiring_version}",
                active_version=current.active_version,
                retirement_not_before=current.retirement_not_before,
                now=now,
            )
            saved = self._store.save_secret_metadata(
                current.complete_retirement(at=now),
                expected_version=current.version,
            )
        except (NotFound, StoreError, ValueError, VersionConflict):
            raise SecretDenied() from None
        except Exception:
            raise SecretDenied() from None
        return {
            "action": "finalize_secret_retirement",
            "secret_id": str(saved.id),
            "state": "retired",
            "replayed": False,
        }

    def _execute_apply(
        self,
        *,
        operation: Operation,
        mode: str,
        current: SecretMetadata | None,
        owner_id: UserId,
        desired_workload_ids: tuple[WorkloadId, ...],
        material: dict[str, object],
        payload: bytes | None,
        now: datetime,
    ) -> SecretMetadata:
        if current is not None and self._is_final_exact_state(
            current=current,
            material=material,
        ):
            return current
        if mode == "create":
            draft = self._stage_create_secret(
                operation=operation,
                owner_id=owner_id,
                desired_workload_ids=desired_workload_ids,
                material=material,
                payload=payload,
                now=now,
            )
            return self._resume_create_secret(
                draft=draft,
                payload=payload,
                now=now,
            )
        secret = _require_secret(current)
        if mode == "attach":
            draft = self._stage_attach_secret(
                operation=operation,
                current=secret,
                desired_workload_ids=desired_workload_ids,
                now=now,
            )
            return self._resume_attach_secret(draft=draft, now=now)
        if mode == "rotate":
            draft = self._stage_rotate_secret(
                operation=operation,
                current=secret,
                desired_workload_ids=desired_workload_ids,
                payload=payload,
                now=now,
            )
            return self._resume_rotate_secret(draft=draft, payload=payload, now=now)
        raise SecretDenied("secret_mode_invalid")

    def _stage_create_secret(
        self,
        *,
        operation: Operation,
        owner_id: UserId,
        desired_workload_ids: tuple[WorkloadId, ...],
        material: dict[str, object],
        payload: bytes | None,
        now: datetime,
    ) -> SecretMetadata:
        normalized_payload = _require_payload(payload)
        return self._store.create_secret_metadata(
            SecretMetadata.create_draft(
                id=SecretId(cast(str, material["secret_id"])),
                owner_id=owner_id,
                name=cast(str, material["secret_name"]),
                integration_type=cast(str, material["integration_type"]),
                attached_workload_ids=desired_workload_ids,
                mutation_idempotency_key=operation.idempotency_key,
                pending_payload_sha256=_payload_sha256(normalized_payload),
                created_at=now,
            )
        )

    def _resume_create_secret(
        self,
        *,
        draft: SecretMetadata,
        payload: bytes | None,
        now: datetime,
    ) -> SecretMetadata:
        normalized_payload = _require_payload(payload)
        _require_payload_match(
            draft=draft,
            payload=normalized_payload,
        )
        desired_workload_ids = _require_pending_workloads(draft)
        ensured = self._secret_port.ensure_secret(
            secret_id=draft.id,
            workload_ids=desired_workload_ids,
        )
        if ensured.created:
            draft = self._store.save_secret_metadata(
                draft.advance_mutation_progress(at=now),
                expected_version=draft.version,
            )
        observed = self._secret_port.probe_secret(
            secret_id=draft.id,
            workload_ids=desired_workload_ids,
        )
        if not observed.exists or not observed.exact_bindings:
            raise SecretDenied("secret_recovery_unproven")
        if observed.enabled_versions == ():
            version = self._secret_port.add_version(
                secret_id=draft.id,
                payload=normalized_payload,
            )
            if version.version != 1:
                raise SecretDenied("secret_create_version_invalid")
            draft = self._store.save_secret_metadata(
                draft.advance_mutation_progress(at=now),
                expected_version=draft.version,
            )
            return self._store.save_secret_metadata(
                draft.finalize_creation(active_version=version.version, at=now),
                expected_version=draft.version,
            )
        if (
            observed.enabled_versions == (1,)
            and not observed.disabled_versions
            and not observed.destroyed_versions
        ):
            return self._store.save_secret_metadata(
                draft.finalize_creation(active_version=1, at=now),
                expected_version=draft.version,
            )
        raise SecretDenied("secret_recovery_unproven")

    def _stage_attach_secret(
        self,
        *,
        operation: Operation,
        current: SecretMetadata,
        desired_workload_ids: tuple[WorkloadId, ...],
        now: datetime,
    ) -> SecretMetadata:
        if current.attached_workload_ids == desired_workload_ids:
            return current
        return self._store.save_secret_metadata(
            current.begin_attachment(
                attached_workload_ids=desired_workload_ids,
                mutation_idempotency_key=operation.idempotency_key,
                at=now,
            ),
            expected_version=current.version,
        )

    def _resume_attach_secret(
        self,
        *,
        draft: SecretMetadata,
        now: datetime,
    ) -> SecretMetadata:
        if draft.mutation_state is SecretMutationState.IDLE:
            return draft
        desired_workload_ids = _require_pending_workloads(draft)
        observed = self._secret_port.probe_secret(
            secret_id=draft.id,
            workload_ids=desired_workload_ids,
        )
        if not observed.exists:
            raise SecretDenied("secret_resource_missing")
        if not observed.exact_bindings:
            self._secret_port.ensure_exact_bindings(
                secret_id=draft.id,
                workload_ids=desired_workload_ids,
            )
        return self._store.save_secret_metadata(
            draft.finalize_attachment(at=now),
            expected_version=draft.version,
        )

    def _stage_rotate_secret(
        self,
        *,
        operation: Operation,
        current: SecretMetadata,
        desired_workload_ids: tuple[WorkloadId, ...],
        payload: bytes | None,
        now: datetime,
    ) -> SecretMetadata:
        normalized_payload = _require_payload(payload)
        return self._store.save_secret_metadata(
            current.begin_rotation(
                attached_workload_ids=desired_workload_ids,
                mutation_idempotency_key=operation.idempotency_key,
                pending_payload_sha256=_payload_sha256(normalized_payload),
                at=now,
            ),
            expected_version=current.version,
        )

    def _resume_rotate_secret(
        self,
        *,
        draft: SecretMetadata,
        payload: bytes | None,
        now: datetime,
    ) -> SecretMetadata:
        normalized_payload = _require_payload(payload)
        _require_payload_match(draft=draft, payload=normalized_payload)
        desired_workload_ids = _require_pending_workloads(draft)
        observed = self._secret_port.probe_secret(
            secret_id=draft.id,
            workload_ids=desired_workload_ids,
        )
        if not observed.exists:
            raise SecretDenied("secret_resource_missing")
        if not observed.exact_bindings:
            ensured = self._secret_port.ensure_secret(
                secret_id=draft.id,
                workload_ids=desired_workload_ids,
            )
            if ensured.created:
                raise SecretDenied("secret_resource_missing")
            draft = self._store.save_secret_metadata(
                draft.advance_mutation_progress(at=now),
                expected_version=draft.version,
            )
            observed = self._secret_port.probe_secret(
                secret_id=draft.id,
                workload_ids=desired_workload_ids,
            )
        old_version = draft.active_version
        newer_enabled = tuple(
            version for version in observed.enabled_versions if version > old_version
        )
        if len(newer_enabled) > 1:
            raise SecretDenied("secret_recovery_unproven")
        if not newer_enabled:
            if old_version not in observed.enabled_versions:
                raise SecretDenied("secret_recovery_unproven")
            created = self._secret_port.add_version(
                secret_id=draft.id,
                payload=normalized_payload,
            )
            new_version = created.version
            draft = self._store.save_secret_metadata(
                draft.advance_mutation_progress(at=now),
                expected_version=draft.version,
            )
        else:
            new_version = newer_enabled[0]
        retirement_not_before = _retirement_not_before(draft)
        if old_version in observed.enabled_versions:
            self._secret_port.disable_old_version(
                secret_id=draft.id,
                version_name=f"{observed.name}/versions/{old_version}",
                active_version=new_version,
                retirement_not_before=retirement_not_before,
                now=now,
            )
            draft = self._store.save_secret_metadata(
                draft.advance_mutation_progress(at=now),
                expected_version=draft.version,
            )
        elif (
            old_version not in observed.disabled_versions
            and old_version not in observed.destroyed_versions
        ):
            raise SecretDenied("secret_recovery_unproven")
        return self._store.save_secret_metadata(
            draft.record_rotation(
                active_version=new_version,
                retiring_version=old_version,
                retirement_not_before=retirement_not_before,
                attached_workload_ids=desired_workload_ids,
                at=now,
            ),
            expected_version=draft.version,
        )

    def _review_workloads(
        self,
        *,
        principal: AuthenticatedPrincipal,
        workload_ids: tuple[str, ...],
    ) -> tuple[Workload, ...]:
        normalized_ids = _normalize_workload_ids(workload_ids)
        workloads = tuple(
            self._store.get_workload(workload_id) for workload_id in normalized_ids
        )
        owner_id = workloads[0].owner_id
        owner = self._store.get_user(owner_id)
        if owner.state is not UserState.ACTIVE:
            raise SecretDenied("owner_inactive")
        for workload in workloads:
            require_owner_or_admin(principal, workload.owner_id)
            if workload.owner_id != owner_id:
                raise SecretDenied("mixed_owner_workloads")
            if workload.state not in _SAFE_WORKLOAD_STATES:
                raise SecretDenied("workload_state_invalid")
        return workloads

    def _authorized_secret(
        self,
        *,
        principal: AuthenticatedPrincipal,
        secret_id: SecretId,
    ) -> SecretMetadata:
        secret = self._store.get_secret_metadata(secret_id)
        require_owner_or_admin(principal, secret.owner_id)
        if secret.lifecycle_state is SecretLifecycleState.DESTROYED:
            raise SecretDenied("secret_destroyed")
        return secret

    def _resume_or_replay_operation(
        self,
        *,
        operation: Operation,
        current: SecretMetadata | None,
        material: dict[str, object],
        desired_workload_ids: tuple[WorkloadId, ...],
        payload: bytes | None,
        now: datetime,
    ) -> SecretMetadata:
        if current is not None and self._is_final_exact_state(
            current=current,
            material=material,
        ):
            return current
        if current is None:
            raise SecretDenied("secret_replay_unproven")
        if current.mutation_idempotency_key != operation.idempotency_key:
            raise SecretDenied("secret_replay_unproven")
        mode = cast(str, material["mode"])
        if mode == "create" and current.mutation_state is SecretMutationState.CREATING:
            return self._resume_create_secret(draft=current, payload=payload, now=now)
        if mode == "attach" and current.mutation_state is SecretMutationState.ATTACHING:
            return self._resume_attach_secret(draft=current, now=now)
        if mode == "rotate" and current.mutation_state is SecretMutationState.ROTATING:
            if not set(current.attached_workload_ids).issubset(
                set(desired_workload_ids)
            ):
                raise SecretDenied("secret_detach_not_allowed")
            return self._resume_rotate_secret(draft=current, payload=payload, now=now)
        raise SecretDenied("secret_replay_unproven")

    def _current_secret_for_material(
        self,
        *,
        principal: AuthenticatedPrincipal,
        material: dict[str, object],
    ) -> SecretMetadata | None:
        secret_id = SecretId(cast(str, material["secret_id"]))
        try:
            secret = self._authorized_secret(principal=principal, secret_id=secret_id)
        except (NotFound, AccessDenied, SecretDenied):
            if cast(str, material["mode"]) == "create":
                return None
            raise
        if str(secret.owner_id) != cast(str, material["owner_id"]):
            raise SecretDenied("secret_owner_mismatch")
        if secret.name != cast(str, material["secret_name"]):
            raise SecretDenied("secret_name_mismatch")
        if secret.integration_type != cast(str, material["integration_type"]):
            raise SecretDenied("secret_integration_mismatch")
        return secret

    def _require_existing_secret_scope(
        self,
        *,
        principal: AuthenticatedPrincipal,
        secret: SecretMetadata,
        integration_type: str,
    ) -> None:
        require_owner_or_admin(principal, secret.owner_id)
        if secret.lifecycle_state is SecretLifecycleState.DESTROYED:
            raise SecretDenied("secret_destroyed")
        if secret.mutation_state is not SecretMutationState.IDLE:
            raise SecretDenied("secret_mutation_inflight")
        if secret.integration_type != integration_type:
            raise SecretDenied("secret_integration_mismatch")

    def _secret_by_name(
        self,
        *,
        owner_id: UserId,
        secret_name: str,
    ) -> SecretMetadata | None:
        matches = [
            item
            for item in self._store.list_secret_metadata(owner_id=owner_id)
            if item.name == secret_name
            and item.lifecycle_state is not SecretLifecycleState.DESTROYED
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise SecretDenied("secret_name_conflict")
        return matches[0]

    def _require_secret_capacity(self, owner_id: UserId) -> None:
        inventory = ResourceInventory(
            active_services=sum(
                1
                for workload in self._store.list_workloads(owner_id=owner_id)
                if workload.state in _SAFE_WORKLOAD_STATES
            ),
            active_schedules=0,
            active_secrets=sum(
                1
                for secret in self._store.list_secret_metadata(owner_id=owner_id)
                if secret.lifecycle_state is not SecretLifecycleState.DESTROYED
            ),
        )
        resource = evaluate_resource_policy(inventory)
        now = self._now()
        try:
            require_current_org_cost_guard(store=self._store, now=now)
        except OrgCostGuardDenied as exc:
            raise SecretDenied("secret_policy_blocked") from exc
        cost = evaluate_cost_policy(
            snapshot=build_cost_snapshot(
                build_usage_ledger(
                    usage_entries_for_utc_month(
                        self._store.list_usage_entries(owner_id=owner_id),
                        now=now,
                    )
                ),
                user_id=owner_id,
            )
        )
        if (
            resource.secret_limit_reached
            or cost.block_new
            or cost.pause
            or cost.emergency_stop
        ):
            raise SecretDenied("secret_policy_blocked")

    def _is_final_exact_state(
        self,
        *,
        current: SecretMetadata,
        material: dict[str, object],
    ) -> bool:
        if current.mutation_state is not SecretMutationState.IDLE:
            return False
        desired_ids = tuple(
            WorkloadId(item) for item in cast(tuple[str, ...], material["workload_ids"])
        )
        mode = cast(str, material["mode"])
        if current.attached_workload_ids != desired_ids:
            return False
        observed = self._secret_port.probe_secret(
            secret_id=current.id,
            workload_ids=current.attached_workload_ids,
        )
        if (
            not observed.exists
            or not observed.exact_bindings
            or observed.enabled_versions != (current.active_version,)
        ):
            return False
        if mode == "create":
            return (
                current.rotation_state is SecretRotationState.STABLE
                and not observed.disabled_versions
                and not observed.destroyed_versions
                and current.active_version == 1
            )
        if mode == "attach":
            return current.rotation_state is SecretRotationState.STABLE
        baseline_active_version = cast(int, material["baseline_active_version"])
        return (
            current.active_version > baseline_active_version
            and current.rotation_state is SecretRotationState.RETIRING_OLD_VERSION
            and current.retiring_version is not None
            and current.retiring_version
            in observed.disabled_versions + observed.destroyed_versions
        )

    def _require_fresh_baseline(
        self,
        *,
        current: SecretMetadata | None,
        material: dict[str, object],
    ) -> None:
        baseline_metadata_version = cast(int, material["baseline_metadata_version"])
        baseline_active_version = cast(int, material["baseline_active_version"])
        mode = cast(str, material["mode"])
        if mode == "create":
            if (
                baseline_metadata_version != 0
                or baseline_active_version != 0
                or current is not None
            ):
                raise SecretDenied("secret_plan_stale")
            return
        if current is None:
            raise SecretDenied("secret_plan_stale")
        if (
            current.version != baseline_metadata_version
            or current.active_version != baseline_active_version
            or current.mutation_state is not SecretMutationState.IDLE
        ):
            raise SecretDenied("secret_plan_stale")

    def _mark_operation_succeeded(
        self,
        operation: Operation,
        *,
        at: datetime,
        result_summary: tuple[tuple[str, str], ...],
    ) -> Operation:
        current = self._store.get_operation(operation.id)
        if current.state is OperationState.SUCCEEDED and current.result_summary:
            return current
        if current.state is OperationState.QUEUED:
            current = self._store.save_operation(
                current.transition(OperationState.BUILDING, at=at),
                expected_version=current.version,
            )
        if current.state is OperationState.BUILDING:
            current = self._store.save_operation(
                current.transition(OperationState.DEPLOYING, at=at),
                expected_version=current.version,
            )
        if current.state is OperationState.DEPLOYING:
            current = self._store.save_operation(
                current.transition(OperationState.VERIFYING, at=at),
                expected_version=current.version,
            )
        if current.state is OperationState.VERIFYING:
            current = self._store.save_operation(
                current.transition(OperationState.SUCCEEDED, at=at),
                expected_version=current.version,
            )
        if current.state is not OperationState.SUCCEEDED:
            raise SecretDenied("secret_operation_state_invalid")
        if current.result_summary != result_summary:
            current = self._store.save_operation(
                current.record_result(result_summary=result_summary, at=at),
                expected_version=current.version,
            )
        return current

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise SecretDenied("clock_invalid")
        return value


def _secret_material(
    *,
    mode: str,
    secret_id: SecretId,
    owner_id: UserId,
    secret_name: str,
    integration_type: str,
    workload_ids: tuple[WorkloadId, ...],
    baseline_metadata_version: int,
    baseline_active_version: int,
) -> dict[str, object]:
    if mode not in {"create", "rotate", "attach"}:
        raise ValueError("secret mode is invalid.")
    return {
        "mode": mode,
        "secret_id": str(secret_id),
        "owner_id": str(owner_id),
        "secret_name": secret_name,
        "integration_type": integration_type,
        "workload_ids": tuple(str(workload_id) for workload_id in workload_ids),
        "baseline_metadata_version": baseline_metadata_version,
        "baseline_active_version": baseline_active_version,
    }


def _summary_from_material(material: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return (
        ("mode", cast(str, material["mode"])),
        ("secret_id", cast(str, material["secret_id"])),
        ("owner_id", cast(str, material["owner_id"])),
        ("secret_name", cast(str, material["secret_name"])),
        ("integration_type", cast(str, material["integration_type"])),
        ("workload_ids", ",".join(cast(tuple[str, ...], material["workload_ids"]))),
        (
            "baseline_metadata_version",
            str(cast(int, material["baseline_metadata_version"])),
        ),
        (
            "baseline_active_version",
            str(cast(int, material["baseline_active_version"])),
        ),
    )


def _material_from_summary(
    summary: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    data = dict(summary)
    required = {
        "mode",
        "secret_id",
        "owner_id",
        "secret_name",
        "integration_type",
        "workload_ids",
        "baseline_metadata_version",
        "baseline_active_version",
    }
    if set(data) != required:
        raise SecretDenied("secret_plan_summary_invalid")
    workload_ids = tuple(
        _require_identifier(item)
        for item in data["workload_ids"].split(",")
        if item.strip()
    )
    if not workload_ids:
        raise SecretDenied("secret_plan_summary_invalid")
    return {
        "mode": data["mode"],
        "secret_id": _require_identifier(data["secret_id"]),
        "owner_id": _require_identifier(data["owner_id"]),
        "secret_name": _require_identifier(data["secret_name"]),
        "integration_type": _require_identifier(data["integration_type"]),
        "workload_ids": workload_ids,
        "baseline_metadata_version": _require_non_negative_int(
            data["baseline_metadata_version"]
        ),
        "baseline_active_version": _require_non_negative_int(
            data["baseline_active_version"]
        ),
    }


def _normalize_workload_ids(workload_ids: tuple[str, ...]) -> tuple[WorkloadId, ...]:
    if (
        type(workload_ids) is not tuple
        or not workload_ids
        or len(workload_ids) > _MAX_WORKLOAD_ATTACHMENTS
    ):
        raise ValueError("workload_ids are invalid.")
    normalized = tuple(WorkloadId(_require_identifier(item)) for item in workload_ids)
    if len(set(normalized)) != len(normalized):
        raise ValueError("workload_ids are invalid.")
    return tuple(sorted(normalized))


def _require_payload(payload: bytes | None) -> bytes:
    if (
        type(payload) is not bytes
        or not payload
        or len(payload) > _MAX_PAYLOAD_BYTES
    ):
        raise SecretDenied("secret_payload_invalid")
    return bytes(payload)


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_payload_match(
    *,
    draft: SecretMetadata,
    payload: bytes,
) -> None:
    expected = draft.pending_payload_sha256
    if expected is None or not hmac.compare_digest(expected, _payload_sha256(payload)):
        raise SecretDenied("secret_payload_invalid")


def _require_identifier(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("identifier is invalid.")
    return value


def _require_secret_name(value: object) -> str:
    if type(value) is not str or _SECRET_NAME_PATTERN.fullmatch(value) is None:
        raise SecretDenied("secret_name_invalid")
    return value


def _require_idempotency_key(value: object) -> str:
    if type(value) is not str or _IDEMPOTENCY_PATTERN.fullmatch(value) is None:
        raise ValueError("idempotency_key is invalid.")
    return value


def _require_hash(value: object) -> str:
    if type(value) is not str or _HEX_SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("hash is invalid.")
    return value


def _require_non_negative_int(value: object) -> int:
    if type(value) is not str or not value.isdigit():
        raise ValueError("integer value is invalid.")
    return int(value)


def _stable_secret_id(*, owner_id: UserId, secret_name: str) -> str:
    digest = hashlib.sha256(f"{owner_id}\x00{secret_name}".encode("utf-8")).hexdigest()
    return f"sec-{digest[:20]}"


def _require_pending_workloads(secret: SecretMetadata) -> tuple[WorkloadId, ...]:
    if (
        secret.pending_workload_ids is None
        or not secret.pending_workload_ids
        or len(secret.pending_workload_ids) > _MAX_WORKLOAD_ATTACHMENTS
    ):
        raise SecretDenied("secret_pending_workloads_invalid")
    return secret.pending_workload_ids


def _retirement_not_before(secret: SecretMetadata) -> datetime:
    return secret.updated_at + _RETIREMENT_WINDOW


def _require_secret(secret: SecretMetadata | None) -> SecretMetadata:
    if secret is None:
        raise SecretDenied("secret_missing")
    return secret


def _result_summary_for_secret(
    *,
    secret: SecretMetadata,
    mode: str,
) -> tuple[tuple[str, str], ...]:
    return (
        ("secret_id", str(secret.id)),
        ("mode", mode),
        ("active_version", str(secret.active_version)),
        ("rotation_state", secret.rotation_state.value),
        (
            "retiring_version",
            _NONE_SENTINEL
            if secret.retiring_version is None
            else str(secret.retiring_version),
        ),
        (
            "attached_workload_ids",
            ",".join(str(workload_id) for workload_id in secret.attached_workload_ids),
        ),
    )


def _replayed_result_from_operation(operation: Operation) -> dict[str, object]:
    data = dict(operation.result_summary)
    required = {
        "secret_id",
        "mode",
        "active_version",
        "rotation_state",
        "retiring_version",
        "attached_workload_ids",
    }
    if set(data) != required:
        raise SecretDenied("secret_replay_unproven")
    return {
        "action": "apply_secret_plan",
        "operation_id": str(operation.id),
        "secret_id": _require_identifier(data["secret_id"]),
        "mode": _require_identifier(data["mode"]),
        "active_version": _require_non_negative_int(data["active_version"]),
        "rotation_state": _require_identifier(data["rotation_state"]),
        "retiring_version": (
            None
            if data["retiring_version"] == _NONE_SENTINEL
            else _require_non_negative_int(data["retiring_version"])
        ),
        "attached_workload_ids": tuple(
            _require_identifier(item)
            for item in data["attached_workload_ids"].split(",")
            if item.strip()
        ),
        "replayed": True,
    }


def _canonical_result_from_secret(
    *,
    operation_id: OperationId,
    secret: SecretMetadata,
    mode: str,
    replayed: bool,
) -> dict[str, object]:
    return {
        "action": "apply_secret_plan",
        "operation_id": str(operation_id),
        "secret_id": str(secret.id),
        "mode": mode,
        "active_version": secret.active_version,
        "rotation_state": secret.rotation_state.value,
        "retiring_version": secret.retiring_version,
        "attached_workload_ids": tuple(
            str(workload_id) for workload_id in secret.attached_workload_ids
        ),
        "replayed": replayed,
    }
