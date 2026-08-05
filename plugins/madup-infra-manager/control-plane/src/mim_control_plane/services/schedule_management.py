"""Plan-bound schedule management and deterministic hourly execution."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from datetime import datetime, timedelta

from mim_control_plane.domain.models import (
    AuditEvent,
    AuditEventId,
    DeploymentPlan,
    DeploymentPlanId,
    Operation,
    OperationId,
    Schedule,
    ScheduleId,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.plans import hash_plan_material, validate_plan_request
from mim_control_plane.domain.states import (
    ActivityOutcome,
    ActivitySurface,
    OperationState,
    PlanState,
    ScheduleState,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.schedule import (
    ScheduleControlPort,
    ScheduledRunRequest,
    ScheduleRunDispatcher,
)
from mim_control_plane.ports.store import (
    AlreadyExists,
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
from mim_control_plane.services.schedules import (
    APPROVED_SCHEDULE_CRON,
    APPROVED_SCHEDULE_TIMEZONE,
    normalize_schedule_policy,
    require_schedule_lease_token,
    require_utc_datetime,
    schedule_is_due,
)
from mim_control_plane.services.usage import (
    ActivityAction,
    build_cost_snapshot,
    build_usage_ledger,
    ingest_activity_event,
    usage_entries_for_utc_month,
)

_PLAN_ACTION = "create_schedule"
_PLAN_POLICY_VERSION = "mim-schedule-v1"
_RUN_ACTION = "schedule_run"
_SAFE_WORKLOAD_STATES = frozenset({WorkloadState.ACTIVE, WorkloadState.FAILED})
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LEASE_DURATION = timedelta(minutes=5)
_ACTIVITY_HEARTBEAT_WINDOW = timedelta(hours=1)
_HEARTBEAT_AUDIT_ACTION = "heartbeat_write_failed"
_HEARTBEAT_AUDIT_TARGET = "schedule_worker"
_HEARTBEAT_AUDIT_DECISION = "best_effort_suppressed"
_HEARTBEAT_AUDIT_OUTCOME = "recorded"


class ScheduleDenied(PermissionError):
    """Sanitized fail-closed denial for schedule management operations."""

    def __init__(self, reason_code: str = "schedule_denied") -> None:
        super().__init__("Schedule request was denied.")
        self.reason_code = reason_code


class ScheduleManagementService:
    def __init__(
        self,
        *,
        store: Store,
        scheduler: ScheduleControlPort,
        dispatcher: ScheduleRunDispatcher,
        clock: Callable[[], datetime],
        id_factory: Callable[[str], str],
        lease_token_factory: Callable[[], str],
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._dispatcher = dispatcher
        self._clock = clock
        self._id_factory = id_factory
        self._lease_token_factory = lease_token_factory

    def plan_schedule(
        self,
        *,
        principal: AuthenticatedPrincipal,
        workload_id: str,
    ) -> dict[str, object]:
        now = self._now()
        try:
            workload = self._review_workload(
                principal=principal,
                workload_id=WorkloadId(_require_identifier(workload_id)),
            )
            self._require_schedule_capacity(workload.owner_id)
            material = self._plan_material(workload)
            material_hash = hash_plan_material(
                material,
                action=_PLAN_ACTION,
                policy_version=_PLAN_POLICY_VERSION,
            )
            plan = self._store.create_deployment_plan(
                DeploymentPlan(
                    id=DeploymentPlanId(self._id_factory("plan")),
                    actor_id=principal.user_id,
                    workload_id=workload.id,
                    action=_PLAN_ACTION,
                    material_hash=material_hash,
                    policy_version=_PLAN_POLICY_VERSION,
                    state=PlanState.ISSUED,
                    expires_at=now + timedelta(minutes=15),
                    created_at=now,
                    updated_at=now,
                    sanitized_summary=(
                        ("workload_id", str(workload.id)),
                        ("cron", APPROVED_SCHEDULE_CRON),
                        ("timezone", APPROVED_SCHEDULE_TIMEZONE),
                    ),
                )
            )
        except ScheduleDenied:
            raise
        except (AccessDenied, NotFound, StoreError, ValueError):
            raise ScheduleDenied() from None
        return {
            "action": "plan_schedule",
            "status": "ready",
            "workload_id": str(workload.id),
            "plan_id": str(plan.id),
            "plan_hash": plan.material_hash,
            "policy": {
                "cron": APPROVED_SCHEDULE_CRON,
                "timezone": APPROVED_SCHEDULE_TIMEZONE,
            },
        }

    def create_schedule_from_plan(
        self,
        *,
        principal: AuthenticatedPrincipal,
        plan_id: str,
        plan_hash: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        now = self._now()
        requested_operation_id = OperationId(self._id_factory("operation"))
        try:
            normalized_plan_id = DeploymentPlanId(_require_identifier(plan_id))
            normalized_plan_hash = _require_hash(plan_hash)
            normalized_idempotency = _require_idempotency_key(idempotency_key)
            plan = self._store.get_deployment_plan(normalized_plan_id)
            if not hmac.compare_digest(plan.material_hash, normalized_plan_hash):
                raise ScheduleDenied("plan_hash_mismatch")
            if plan.workload_id is None:
                raise ScheduleDenied("plan_missing_workload")
            workload = self._review_workload(
                principal=principal,
                workload_id=plan.workload_id,
            )
            self._require_schedule_capacity(workload.owner_id)
            material = self._plan_material(workload)
            validate_plan_request(
                plan,
                actor_id=principal.user_id,
                material=material,
                action=_PLAN_ACTION,
                policy_version=_PLAN_POLICY_VERSION,
                at=now,
            )
            schedule = self._schedule_for_plan(plan=plan, workload=workload, now=now)
            requested_operation = Operation(
                id=requested_operation_id,
                actor_id=principal.user_id,
                workload_id=workload.id,
                action=_PLAN_ACTION,
                idempotency_key=normalized_idempotency,
                request_hash=plan.material_hash,
                state=OperationState.QUEUED,
                created_at=now,
                updated_at=now,
            )
            persisted_plan, persisted_schedule, persisted_operation = (
                self._store.consume_schedule_plan_with_operation(
                    plan_id=plan.id,
                    actor_id=principal.user_id,
                    expected_material_hash=plan.material_hash,
                    expected_action=_PLAN_ACTION,
                    policy_version=_PLAN_POLICY_VERSION,
                    consumed_at=now,
                    schedule=schedule,
                    operation=requested_operation,
                )
            )
            self._scheduler.ensure_enabled(persisted_schedule)
        except ScheduleDenied:
            raise
        except (
            AccessDenied,
            IdempotencyConflict,
            InvariantViolation,
            NotFound,
            StoreError,
            ValueError,
            VersionConflict,
        ):
            raise ScheduleDenied() from None
        except Exception:
            raise ScheduleDenied() from None
        return {
            "action": "create_schedule_from_plan",
            "schedule_id": str(persisted_schedule.id),
            "operation_id": str(persisted_operation.id),
            "state": persisted_schedule.state.value,
            "replayed": persisted_operation.id != requested_operation_id
            or persisted_plan.version > 2,
        }

    def pause_schedule(
        self,
        *,
        principal: AuthenticatedPrincipal,
        schedule_id: str,
    ) -> dict[str, object]:
        current = self._authorized_schedule(
            principal=principal,
            schedule_id=ScheduleId(_require_identifier(schedule_id)),
        )
        try:
            if current.state is ScheduleState.PAUSED:
                target = current
                replayed = True
            elif current.state is ScheduleState.ENABLED:
                at = self._now()
                target = self._store.save_schedule(
                    current.transition_state(ScheduleState.PAUSED, at=at),
                    expected_version=current.version,
                )
                replayed = False
            else:
                raise ScheduleDenied()
            self._scheduler.pause(target)
        except ScheduleDenied:
            raise
        except (InvariantViolation, StoreError, ValueError, VersionConflict):
            raise ScheduleDenied() from None
        except Exception:
            raise ScheduleDenied() from None
        return {
            "action": "pause_schedule",
            "schedule_id": str(target.id),
            "state": target.state.value,
            "replayed": replayed,
        }

    def resume_schedule(
        self,
        *,
        principal: AuthenticatedPrincipal,
        schedule_id: str,
    ) -> dict[str, object]:
        current = self._authorized_schedule(
            principal=principal,
            schedule_id=ScheduleId(_require_identifier(schedule_id)),
        )
        try:
            if current.state is ScheduleState.ENABLED:
                target = current
                replayed = True
            elif current.state in {ScheduleState.PAUSED, ScheduleState.DISABLED}:
                workload = self._review_workload(
                    principal=principal,
                    workload_id=current.workload_id,
                )
                self._require_schedule_capacity(workload.owner_id)
                at = self._now()
                target = self._store.save_schedule(
                    current.transition_state(ScheduleState.ENABLED, at=at),
                    expected_version=current.version,
                )
                replayed = False
            else:
                raise ScheduleDenied()
            self._scheduler.resume(target)
        except ScheduleDenied:
            raise
        except (InvariantViolation, StoreError, ValueError, VersionConflict):
            raise ScheduleDenied() from None
        except Exception:
            raise ScheduleDenied() from None
        return {
            "action": "resume_schedule",
            "schedule_id": str(target.id),
            "state": target.state.value,
            "replayed": replayed,
        }

    def execute_schedule_tick(
        self,
        *,
        schedule_id: str,
        workload_id: str,
        tick_at: datetime,
    ) -> dict[str, object]:
        trusted_tick = require_utc_datetime(tick_at, label="schedule tick")
        current = self._store.get_schedule(ScheduleId(_require_identifier(schedule_id)))
        expected_workload_id = WorkloadId(_require_identifier(workload_id))
        if current.workload_id != expected_workload_id:
            raise ScheduleDenied()
        if current.state is not ScheduleState.ENABLED:
            raise ScheduleDenied()
        if not schedule_is_due(current, tick_at=trusted_tick):
            raise ScheduleDenied()
        if (
            current.last_attempt_at is not None
            and current.last_attempt_at >= trusted_tick
        ):
            return {
                "action": "execute_schedule_tick",
                "schedule_id": str(current.id),
                "state": current.state.value,
                "outcome": "replayed",
                "replayed": True,
            }

        workload = self._store.get_workload(current.workload_id)
        owner = self._store.get_user(current.owner_id)
        lease_token = require_schedule_lease_token(self._lease_token_factory())
        leased = self._store.acquire_schedule_lease(
            current.id,
            expected_version=current.version,
            lease_token=lease_token,
            lease_expires_at=trusted_tick + _LEASE_DURATION,
            now=trusted_tick,
        )
        operation = self._store.create_operation_once(
            Operation(
                id=OperationId(self._id_factory("operation")),
                actor_id=owner.id,
                workload_id=workload.id,
                action=_RUN_ACTION,
                idempotency_key=_tick_idempotency_key(current.id, trusted_tick),
                request_hash=_tick_request_hash(current.id, workload.id, trusted_tick),
                state=OperationState.PLANNED,
                created_at=trusted_tick,
                updated_at=trusted_tick,
            )
        )
        if operation.state is not OperationState.PLANNED:
            if (
                leased.last_attempt_at is not None
                and leased.last_attempt_at >= trusted_tick
            ):
                return {
                    "action": "execute_schedule_tick",
                    "schedule_id": str(leased.id),
                    "state": leased.state.value,
                    "outcome": "replayed",
                    "replayed": True,
                }
            if operation.state is not OperationState.QUEUED:
                raise ScheduleDenied()
            queued = operation
        else:
            queued = self._store.save_operation(
                operation.transition(OperationState.QUEUED, at=trusted_tick),
                expected_version=operation.version,
            )

        outcome = ActivityOutcome.SUCCEEDED
        reason = "schedule execution completed."
        succeeded = True
        try:
            self._revalidate_execution_policy(
                owner_id=owner.id,
                workload_id=workload.id,
            )
            self._dispatcher.dispatch(
                ScheduledRunRequest(
                    schedule_id=leased.id,
                    workload_id=leased.workload_id,
                    tick_at=trusted_tick,
                    lease_token=lease_token,
                )
            )
            self._mark_operation_succeeded(queued, at=trusted_tick)
        except ScheduleDenied:
            outcome = ActivityOutcome.DENIED
            reason = "schedule execution was denied."
            succeeded = False
            self._mark_operation_failed(queued, at=trusted_tick)
        except Exception:
            outcome = ActivityOutcome.FAILED
            reason = "schedule execution failed."
            succeeded = False
            self._mark_operation_failed(queued, at=trusted_tick)

        completed = self._store.complete_schedule_run(
            leased.id,
            expected_version=leased.version,
            lease_token=lease_token,
            succeeded=succeeded,
            completed_at=trusted_tick,
        )
        if outcome in {ActivityOutcome.SUCCEEDED, ActivityOutcome.FAILED}:
            self._record_workload_activity_best_effort(
                schedule_id=leased.id,
                workload_id=workload.id,
                run_discriminator=_tick_idempotency_key(leased.id, trusted_tick),
                at=trusted_tick,
            )
        self._store.append_activity_event(
            ingest_activity_event(
                event_id=_activity_event_id(leased.id, trusted_tick),
                trusted_user_id=completed.owner_id,
                trusted_correlation_id=_correlation_id(leased.id, trusted_tick),
                trusted_occurred_at=trusted_tick,
                observed_at=trusted_tick,
                payload={
                    "surface": ActivitySurface.WORKER.value,
                    "action": ActivityAction.SCHEDULE_RUN.value,
                    "target_ref": str(leased.id),
                    "outcome": outcome.value,
                    "latency_ms": 250,
                },
            )
        )
        return {
            "action": "execute_schedule_tick",
            "schedule_id": str(completed.id),
            "state": completed.state.value,
            "outcome": outcome.value,
            "reason": reason,
            "replayed": False,
        }

    def _review_workload(
        self,
        *,
        principal: AuthenticatedPrincipal,
        workload_id: WorkloadId,
    ) -> Workload:
        workload = self._store.get_workload(workload_id)
        require_owner_or_admin(principal, workload.owner_id)
        owner = self._store.get_user(workload.owner_id)
        if owner.state is not UserState.ACTIVE:
            raise ScheduleDenied("owner_inactive")
        if workload.kind is not WorkloadKind.SCHEDULED_SCRIPT:
            raise ScheduleDenied("workload_kind_invalid")
        if workload.state not in _SAFE_WORKLOAD_STATES:
            raise ScheduleDenied("workload_state_invalid")
        return workload

    def _authorized_schedule(
        self,
        *,
        principal: AuthenticatedPrincipal,
        schedule_id: ScheduleId,
    ) -> Schedule:
        try:
            current = self._store.get_schedule(schedule_id)
            require_owner_or_admin(principal, current.owner_id)
        except (AccessDenied, NotFound):
            raise ScheduleDenied() from None
        return current

    def _plan_material(self, workload: Workload) -> dict[str, object]:
        cron, timezone = normalize_schedule_policy("hourly", APPROVED_SCHEDULE_TIMEZONE)
        return {
            "workload_id": str(workload.id),
            "owner_id": str(workload.owner_id),
            "cron": cron,
            "timezone": timezone,
        }

    def _require_schedule_capacity(self, owner_id: UserId) -> None:
        schedules = self._store.list_schedules(owner_id=owner_id)
        workloads = self._store.list_workloads(owner_id=owner_id)
        inventory = ResourceInventory(
            active_services=sum(
                1
                for workload in workloads
                if workload.state in {WorkloadState.ACTIVE, WorkloadState.FAILED}
            ),
            active_schedules=sum(
                1
                for schedule in schedules
                if schedule.state in {
                    ScheduleState.ENABLED,
                    ScheduleState.PAUSED,
                    ScheduleState.DISABLED,
                }
            ),
            active_secrets=len(self._store.list_secret_metadata(owner_id=owner_id)),
        )
        resource = evaluate_resource_policy(inventory)
        now = self._now()
        try:
            require_current_org_cost_guard(store=self._store, now=now)
        except OrgCostGuardDenied as exc:
            raise ScheduleDenied("schedule_policy_blocked") from exc
        owner_ledger = build_usage_ledger(
            usage_entries_for_utc_month(
                self._store.list_usage_entries(owner_id=owner_id),
                now=now,
            )
        )
        cost = evaluate_cost_policy(
            snapshot=build_cost_snapshot(owner_ledger, user_id=owner_id),
        )
        if (
            resource.schedule_limit_reached
            or cost.block_new
            or cost.pause
            or cost.emergency_stop
        ):
            raise ScheduleDenied("schedule_policy_blocked")

    def _revalidate_execution_policy(
        self,
        *,
        owner_id: UserId,
        workload_id: WorkloadId,
    ) -> None:
        owner = self._store.get_user(owner_id)
        if owner.state is not UserState.ACTIVE:
            raise ScheduleDenied("owner_inactive")
        workload = self._store.get_workload(workload_id)
        if workload.state not in _SAFE_WORKLOAD_STATES:
            raise ScheduleDenied("workload_state_invalid")
        self._require_schedule_capacity(owner_id)

    def _schedule_for_plan(
        self,
        *,
        plan: DeploymentPlan,
        workload: Workload,
        now: datetime,
    ) -> Schedule:
        digest = hashlib.sha256(str(plan.id).encode("utf-8")).hexdigest()[:20]
        return Schedule(
            id=ScheduleId(f"sch-{digest}"),
            owner_id=workload.owner_id,
            workload_id=workload.id,
            cron=APPROVED_SCHEDULE_CRON,
            timezone=APPROVED_SCHEDULE_TIMEZONE,
            state=ScheduleState.ENABLED,
            created_at=now,
            updated_at=now,
        )

    def _mark_operation_succeeded(self, operation: Operation, *, at: datetime) -> None:
        building = self._store.save_operation(
            operation.transition(OperationState.BUILDING, at=at),
            expected_version=operation.version,
        )
        deploying = self._store.save_operation(
            building.transition(OperationState.DEPLOYING, at=at),
            expected_version=building.version,
        )
        verifying = self._store.save_operation(
            deploying.transition(OperationState.VERIFYING, at=at),
            expected_version=deploying.version,
        )
        self._store.save_operation(
            verifying.transition(OperationState.SUCCEEDED, at=at),
            expected_version=verifying.version,
        )

    def _mark_operation_failed(self, operation: Operation, *, at: datetime) -> None:
        current = self._store.get_operation(operation.id)
        if current.state is OperationState.QUEUED:
            self._store.save_operation(
                current.transition(OperationState.FAILED, at=at),
                expected_version=current.version,
            )

    def _now(self) -> datetime:
        return require_utc_datetime(self._clock(), label="schedule clock")

    def _record_workload_activity_best_effort(
        self,
        *,
        schedule_id: ScheduleId,
        workload_id: WorkloadId,
        run_discriminator: str,
        at: datetime,
    ) -> None:
        try:
            current = self._store.get_workload(workload_id)
            if not _eligible_schedule_heartbeat_workload(current):
                return
            if _heartbeat_is_recent(current.last_activity_at, at=at):
                return
            self._store.save_workload(
                current.record_activity(at=at),
                expected_version=current.version,
            )
        except VersionConflict:
            try:
                reloaded = self._store.get_workload(workload_id)
                if not _eligible_schedule_retry_workload(reloaded):
                    return
                if _heartbeat_is_recent(reloaded.last_activity_at, at=at):
                    return
                self._store.save_workload(
                    reloaded.record_activity(at=at),
                    expected_version=reloaded.version,
                )
            except (NotFound, ValueError, VersionConflict):
                return
            except Exception:
                self._append_heartbeat_failure_signal(
                    occurred_at=at,
                    discriminator=_heartbeat_signal_discriminator(
                        schedule_id=str(schedule_id),
                        workload_id=str(workload_id),
                        run_discriminator=run_discriminator,
                    ),
                )
                return
        except (NotFound, ValueError):
            return
        except Exception:
            self._append_heartbeat_failure_signal(
                occurred_at=at,
                discriminator=_heartbeat_signal_discriminator(
                    schedule_id=str(schedule_id),
                    workload_id=str(workload_id),
                    run_discriminator=run_discriminator,
                ),
            )
            return

    def _append_heartbeat_failure_signal(
        self,
        *,
        occurred_at: datetime,
        discriminator: str,
    ) -> None:
        event = AuditEvent(
            id=_heartbeat_audit_event_id(
                target_ref=_HEARTBEAT_AUDIT_TARGET,
                occurred_at=occurred_at,
                discriminator=discriminator,
            ),
            actor_id=None,
            action=_HEARTBEAT_AUDIT_ACTION,
            target_ref=_HEARTBEAT_AUDIT_TARGET,
            policy_decision=_HEARTBEAT_AUDIT_DECISION,
            before_ref=None,
            after_ref=None,
            correlation_id=_heartbeat_audit_correlation_id(
                target_ref=_HEARTBEAT_AUDIT_TARGET,
                occurred_at=occurred_at,
                discriminator=discriminator,
            ),
            outcome=_HEARTBEAT_AUDIT_OUTCOME,
            occurred_at=occurred_at,
        )
        try:
            self._store.append_audit_event(event)
        except AlreadyExists:
            return
        except Exception:
            return


def _require_identifier(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("identifier is invalid.")
    return value


def _require_idempotency_key(value: object) -> str:
    if type(value) is not str or _IDEMPOTENCY_PATTERN.fullmatch(value) is None:
        raise ValueError("idempotency_key is invalid.")
    return value


def _require_hash(value: object) -> str:
    if type(value) is not str or len(value) != 64:
        raise ValueError("hash is invalid.")
    if any(char not in "0123456789abcdef" for char in value):
        raise ValueError("hash is invalid.")
    return value


def _tick_idempotency_key(schedule_id: ScheduleId, tick_at: datetime) -> str:
    return f"schedule:{schedule_id}:{tick_at.strftime('%Y%m%d%H')}"


def _tick_request_hash(
    schedule_id: ScheduleId,
    workload_id: WorkloadId,
    tick_at: datetime,
) -> str:
    payload = f"{schedule_id}\x00{workload_id}\x00{tick_at.isoformat()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _activity_event_id(schedule_id: ScheduleId, tick_at: datetime) -> str:
    digest = hashlib.sha256(
        f"{schedule_id}:{tick_at.isoformat()}".encode("utf-8")
    ).hexdigest()[:24]
    return f"schrun-{digest}"


def _correlation_id(schedule_id: ScheduleId, tick_at: datetime) -> str:
    digest = hashlib.sha256(
        f"{schedule_id}:{tick_at.isoformat()}".encode("utf-8")
    ).hexdigest()[:24]
    return f"corr-{digest}"


def _heartbeat_is_recent(last_activity_at: datetime | None, *, at: datetime) -> bool:
    return (
        last_activity_at is not None
        and last_activity_at > at - _ACTIVITY_HEARTBEAT_WINDOW
    )


def _eligible_schedule_heartbeat_workload(workload: Workload) -> bool:
    return (
        workload.kind is WorkloadKind.SCHEDULED_SCRIPT
        and workload.state in _SAFE_WORKLOAD_STATES
    )


def _eligible_schedule_retry_workload(workload: Workload) -> bool:
    return (
        workload.kind is WorkloadKind.SCHEDULED_SCRIPT
        and workload.state is WorkloadState.ACTIVE
    )


def _heartbeat_audit_event_id(
    *,
    target_ref: str,
    occurred_at: datetime,
    discriminator: str,
) -> AuditEventId:
    digest = hashlib.sha256(
        (
            f"schedule-heartbeat:{target_ref}:{occurred_at.isoformat()}:"
            f"{discriminator}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    return AuditEventId(f"audit-heartbeat-{digest}")


def _heartbeat_audit_correlation_id(
    *,
    target_ref: str,
    occurred_at: datetime,
    discriminator: str,
) -> str:
    digest = hashlib.sha256(
        (
            f"schedule-heartbeat-corr:{target_ref}:{occurred_at.isoformat()}:"
            f"{discriminator}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"corr-heartbeat-{digest}"


def _heartbeat_signal_discriminator(
    *,
    schedule_id: str,
    workload_id: str,
    run_discriminator: str,
) -> str:
    return hashlib.sha256(
        (
            "schedule-heartbeat-signal:"
            f"{schedule_id}:{workload_id}:{run_discriminator}"
        ).encode("utf-8")
    ).hexdigest()[:24]
