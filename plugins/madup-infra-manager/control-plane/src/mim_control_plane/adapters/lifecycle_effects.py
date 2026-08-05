"""Concrete production adapters for lifecycle worker side effects."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from google.api_core.exceptions import NotFound as GoogleNotFound
from google.cloud import scheduler_v1
from google.iam.v1 import iam_policy_pb2, policy_pb2  # type: ignore[import-untyped]

from mim_control_plane.adapters.cloud_run import (
    _require_safe_job_boundary,
    _require_safe_service_boundary,
)
from mim_control_plane.adapters.secret_manager import _runtime_service_account
from mim_control_plane.config import COMPANY_DOMAIN, REGION
from mim_control_plane.domain.models import (
    AppHostnameBindingState,
    AuditEvent,
    AuditEventId,
    Schedule,
    ScheduleId,
    SecretId,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    ScheduleState,
    SecretLifecycleState,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.slack_oauth import SlackOAuthInstallRepositoryError
from mim_control_plane.ports.store import (
    AlreadyExists,
    NotFound,
    Store,
    VersionConflict,
)
from mim_control_plane.services.app_hostname import build_app_hostname
from mim_control_plane.services.runtime_naming import (
    app_gateway_invoker_member,
    cloud_run_job_name,
    cloud_run_service_name,
    normalize_reviewed_breakglass_members,
    provider_secret_id,
)
from mim_control_plane.services.schedules import require_utc_datetime

_CENTRAL_PROJECT_ID = "mim-prod-123456"
_GOOGLEAPIS_URL = "https://iap.googleapis.com/v1"
_IAP_ROLE = "roles/iap.httpsResourceAccessor"
_SECRET_ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"
_SECRET_METADATA_READER_ROLE = "roles/secretmanager.viewer"
_SECRET_VERSION_MANAGER_ROLE = "roles/secretmanager.secretVersionManager"
_GATEWAY_PATH = "/v1/schedules/execute"
_LIFECYCLE_POLICY_KINDS = frozenset(
    {"cloud_run_service", "cloud_run_job", "cloud_scheduler_job"}
)
_ALLOWLISTED_REASON_CODES = frozenset(
    {
        "suspended",
        "offboarded",
        "suspended_notification",
        "offboarded_notification",
        "suspended_transfer_window",
        "offboarded_transfer_window",
        "suspended_disable_schedule",
        "offboarded_disable_schedule",
        "offboarded_7d_quarantined",
        "23_days_inactive",
        "30_days_inactive",
        "user_cost_pause",
        "org_emergency_stop",
    }
)
_LOCK_REASON_STATES = {
    "suspended": UserState.SUSPENDED,
    "offboarded": UserState.OFFBOARDED,
}


class LifecycleEffectsError(RuntimeError):
    """Sanitized lifecycle adapter failure."""


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _require_reason(reason: str) -> str:
    if type(reason) is not str or reason not in _ALLOWLISTED_REASON_CODES:
        raise LifecycleEffectsError("lifecycle effect was denied.")
    return reason


def _require_locked_user_reason(user_state: UserState, reason: str) -> None:
    expected = _LOCK_REASON_STATES.get(reason)
    if expected is None or user_state is not expected:
        raise LifecycleEffectsError("lifecycle effect was denied.")


def _require_access_removal_reason(user_state: UserState, reason: str) -> None:
    if reason == "user_cost_pause":
        if user_state is not UserState.ACTIVE:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        return
    if reason == "org_emergency_stop":
        return
    _require_locked_user_reason(user_state, reason)


def _require_schedule_effect_reason(user_state: UserState, reason: str) -> None:
    if reason == "user_cost_pause":
        if user_state is not UserState.ACTIVE:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        return
    if reason == "org_emergency_stop":
        return
    _require_stateful_user_reason(user_state, reason)


def _require_stateful_user_reason(user_state: UserState, reason: str) -> None:
    if reason.startswith("offboarded_"):
        if user_state is not UserState.OFFBOARDED:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        return
    if reason.startswith("suspended_"):
        if user_state is not UserState.SUSPENDED:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        return
    if reason in {"23_days_inactive", "30_days_inactive"}:
        if user_state is not UserState.ACTIVE:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        return
    raise LifecycleEffectsError("lifecycle effect was denied.")


def _require_exact_version(observed: int, expected: int) -> None:
    if type(expected) is not int or isinstance(expected, bool) or observed != expected:
        raise LifecycleEffectsError("lifecycle effect was denied.")


def _normalize_company_member(value: object) -> str:
    if type(value) is not str:
        raise LifecycleEffectsError("lifecycle effect was denied.")
    normalized = value.casefold().strip()
    if (
        not normalized
        or value != value.strip()
        or not (normalized.startswith("user:") or normalized.startswith("group:"))
        or not normalized.endswith(f"@{COMPANY_DOMAIN}")
    ):
        raise LifecycleEffectsError("lifecycle effect was denied.")
    return normalized


def _normalize_admin_members(members: Sequence[str]) -> tuple[str, ...]:
    if isinstance(members, str):
        raise LifecycleEffectsError("lifecycle effect was denied.")
    normalized = tuple(_normalize_company_member(member) for member in members)
    if not normalized or len(set(normalized)) != len(normalized):
        raise LifecycleEffectsError("lifecycle effect was denied.")
    return normalized


def _owner_member(email: str) -> str:
    return _normalize_company_member(f"user:{email}")


def _policy_bindings(policy: Mapping[str, object]) -> list[dict[str, object]]:
    bindings = policy.get("bindings")
    if not isinstance(bindings, list):
        raise LifecycleEffectsError("lifecycle effect was denied.")
    copied: list[dict[str, object]] = []
    for binding in bindings:
        if not isinstance(binding, Mapping):
            raise LifecycleEffectsError("lifecycle effect was denied.")
        copied.append(copy.deepcopy(dict(binding)))
    return copied


def _require_iap_policy_members(
    policy: Mapping[str, object],
    *,
    allowed_members: tuple[frozenset[str], ...],
) -> tuple[str, int, frozenset[str]]:
    etag = policy.get("etag")
    version = policy.get("version")
    if not isinstance(etag, str) or not etag or type(version) is not int:
        raise LifecycleEffectsError("lifecycle effect was denied.")
    bindings = _policy_bindings(policy)
    if len(bindings) != 1:
        raise LifecycleEffectsError("lifecycle effect was denied.")
    binding = bindings[0]
    if binding.get("role") != _IAP_ROLE or set(binding.keys()) != {"role", "members"}:
        raise LifecycleEffectsError("lifecycle effect was denied.")
    members = binding.get("members")
    if not isinstance(members, list):
        raise LifecycleEffectsError("lifecycle effect was denied.")
    normalized = tuple(_normalize_company_member(member) for member in members)
    if len(set(normalized)) != len(normalized):
        raise LifecycleEffectsError("lifecycle effect was denied.")
    observed = frozenset(normalized)
    if observed not in allowed_members:
        raise LifecycleEffectsError("lifecycle effect was denied.")
    return etag, version, observed


def _resource_iap_name(*, project_number: str, workload_id: WorkloadId) -> str:
    return (
        f"projects/{project_number}/iap_web/cloud_run-{REGION}/services/"
        f"mim-svc-{hashlib.sha256(str(workload_id).encode('utf-8')).hexdigest()[:12]}"
    )


def _deepcopy_mapping(value: Mapping[str, object]) -> dict[str, object]:
    copied = copy.deepcopy(dict(value))
    if not isinstance(copied, dict):
        raise LifecycleEffectsError("lifecycle effect was denied.")
    return copied


def _deterministic_audit_id(*parts: str) -> AuditEventId:
    digest = hashlib.sha256()
    digest.update(b"mim:lifecycle-audit:v1\x00")
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return AuditEventId(f"audit-lifecycle-{digest.hexdigest()[:24]}")


def _deterministic_correlation_id(*parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"mim:lifecycle-correlation:v1\x00")
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return f"lc-{digest.hexdigest()[:20]}"


class LifecycleAuditSessionGate:
    def __init__(
        self,
        *,
        store: Store,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._clock = clock

    def deny_user_sessions(self, *, user_id: UserId, reason: str) -> None:
        _require_locked_user_reason(
            self._store.get_user(user_id).state,
            _require_reason(reason),
        )
        _append_lifecycle_audit(
            store=self._store,
            action="lifecycle_deny_sessions",
            target_ref=f"user:{user_id}",
            reason=reason,
            occurred_at=require_utc_datetime(self._clock(), label="lifecycle audit"),
        )


class LifecycleIapAccessManager:
    def __init__(
        self,
        *,
        store: Store,
        session: Any,
        project_number: str,
        admin_members: Sequence[str],
        timeout: float = 10.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if type(project_number) is not str or not project_number.isdigit():
            raise ValueError("project_number is invalid.")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or timeout <= 0
        ):
            raise ValueError("timeout is invalid.")
        self._store = store
        self._session = session
        self._project_number = project_number
        self._admin_members = _normalize_admin_members(admin_members)
        self._timeout = float(timeout)
        self._clock = clock

    def remove_owner_access(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        reason: str,
    ) -> None:
        _require_reason(reason)
        try:
            workload = self._store.get_workload(workload_id)
            _require_exact_version(workload.version, expected_workload_version)
            user = self._store.get_user(workload.owner_id)
            _require_access_removal_reason(user.state, reason)
            if workload.kind is WorkloadKind.SCHEDULED_SCRIPT:
                return
            try:
                binding = self._store.get_app_hostname_binding(
                    build_app_hostname(str(workload.name), str(workload.id))
                )
            except NotFound:
                return
            if (
                binding.workload_id != workload.id
                or binding.owner_id != workload.owner_id
            ):
                raise LifecycleEffectsError("lifecycle effect was denied.")
            if binding.state is AppHostnameBindingState.RETIRED:
                return
            if binding.state is AppHostnameBindingState.DISABLED:
                return
            updated = binding.transition_state(
                AppHostnameBindingState.DISABLED,
                at=require_utc_datetime(self._clock(), label="lifecycle binding"),
            )
            self._store.save_app_hostname_binding(
                updated,
                expected_version=binding.version,
            )
            _append_lifecycle_audit(
                store=self._store,
                action="lifecycle_disable_app_host",
                target_ref=f"workload:{workload.id}",
                reason=reason,
                occurred_at=require_utc_datetime(
                    self._clock(),
                    label="lifecycle audit",
                ),
            )
        except (LifecycleEffectsError, NotFound, VersionConflict):
            raise LifecycleEffectsError("lifecycle effect was denied.") from None
        except Exception:
            raise LifecycleEffectsError("lifecycle effect was denied.") from None

    def _post_json(
        self,
        url: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        response = self._session.post(url, json=dict(payload), timeout=self._timeout)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, Mapping):
            raise LifecycleEffectsError("lifecycle effect was denied.")
        return copy.deepcopy(dict(body))


def _secret_expected_members(
    workload_ids: tuple[WorkloadId, ...],
) -> dict[str, frozenset[str]]:
    runtimes = frozenset(
        "serviceAccount:"
        + _runtime_service_account(
            workload_id=workload_id,
            project_id=_CENTRAL_PROJECT_ID,
        )
        for workload_id in workload_ids
    )
    expected = {
        _SECRET_METADATA_READER_ROLE: frozenset(
            {
                "serviceAccount:"
                f"mim-deploy-worker@{_CENTRAL_PROJECT_ID}.iam.gserviceaccount.com"
            }
        ),
        _SECRET_VERSION_MANAGER_ROLE: frozenset(
            {
                "serviceAccount:"
                f"mim-control-plane@{_CENTRAL_PROJECT_ID}.iam.gserviceaccount.com"
            }
        ),
    }
    if runtimes:
        expected[_SECRET_ACCESSOR_ROLE] = runtimes
    return expected


def _require_exact_secret_policy(
    policy: policy_pb2.Policy,
    *,
    expected: dict[str, frozenset[str]],
) -> None:
    found: dict[str, list[policy_pb2.Binding]] = {role: [] for role in expected}
    for binding in policy.bindings:
        if binding.role not in found:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        found[binding.role].append(binding)
    for role, expected_members in expected.items():
        bindings = found[role]
        if len(bindings) != 1:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        binding = bindings[0]
        if (
            binding.HasField("condition")
            or frozenset(binding.members) != expected_members
        ):
            raise LifecycleEffectsError("lifecycle effect was denied.")


class LifecycleSecretBindingManager:
    def __init__(self, *, store: Store, client: Any) -> None:
        self._store = store
        self._client = client

    def remove_workload_bindings(
        self,
        *,
        workload_id: WorkloadId,
        secret_ids: tuple[SecretId, ...],
        expected_workload_version: int,
    ) -> None:
        if type(secret_ids) is not tuple:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        try:
            workload = self._store.get_workload(workload_id)
            _require_exact_version(workload.version, expected_workload_version)
            self._store.get_user(workload.owner_id)
            for secret_id in secret_ids:
                secret = self._store.get_secret_metadata(secret_id)
                if (
                    secret.owner_id != workload.owner_id
                    or workload_id not in secret.attached_workload_ids
                    or secret.lifecycle_state is SecretLifecycleState.DESTROYED
                ):
                    raise LifecycleEffectsError("lifecycle effect was denied.")
                remaining = tuple(
                    sorted(
                        (
                            candidate
                            for candidate in secret.attached_workload_ids
                            if candidate != workload_id
                        ),
                        key=str,
                    )
                )
                expected = _secret_expected_members(secret.attached_workload_ids)
                remaining_expected = _secret_expected_members(remaining)
                resource = (
                    f"projects/{_CENTRAL_PROJECT_ID}/secrets/"
                    f"{provider_secret_id(str(secret.id))}"
                )
                current = self._client.get_iam_policy(
                    iam_policy_pb2.GetIamPolicyRequest(resource=resource)
                )
                try:
                    _require_exact_secret_policy(current, expected=expected)
                except LifecycleEffectsError:
                    _require_exact_secret_policy(
                        current,
                        expected=remaining_expected,
                    )
                    continue
                proposed = policy_pb2.Policy(
                    version=max(current.version, 3),
                    etag=current.etag,
                )
                if remaining:
                    proposed.bindings.add(
                        role=_SECRET_ACCESSOR_ROLE,
                        members=tuple(
                            sorted(remaining_expected[_SECRET_ACCESSOR_ROLE])
                        ),
                    )
                proposed.bindings.add(
                    role=_SECRET_METADATA_READER_ROLE,
                    members=tuple(
                        sorted(remaining_expected[_SECRET_METADATA_READER_ROLE])
                    ),
                )
                proposed.bindings.add(
                    role=_SECRET_VERSION_MANAGER_ROLE,
                    members=tuple(
                        sorted(remaining_expected[_SECRET_VERSION_MANAGER_ROLE])
                    ),
                )
                updated = self._client.set_iam_policy(
                    iam_policy_pb2.SetIamPolicyRequest(
                        resource=resource,
                        policy=proposed,
                    )
                )
                _require_exact_secret_policy(
                    updated,
                    expected=remaining_expected,
                )
                readback = self._client.get_iam_policy(
                    iam_policy_pb2.GetIamPolicyRequest(resource=resource)
                )
                _require_exact_secret_policy(
                    readback,
                    expected=remaining_expected,
                )
        except (LifecycleEffectsError, NotFound, VersionConflict):
            raise LifecycleEffectsError("lifecycle effect was denied.") from None
        except Exception:
            raise LifecycleEffectsError("lifecycle effect was denied.") from None


class LifecycleSlackGrantManager:
    def __init__(
        self,
        *,
        store: Store,
        repository: Any,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._repository = repository
        self._clock = clock

    def revoke_user_grant(self, *, user_id: UserId, reason: str) -> None:
        _require_reason(reason)
        try:
            user = self._store.get_user(user_id)
            _require_locked_user_reason(user.state, reason)
            revoked_at = require_utc_datetime(
                self._clock(),
                label="lifecycle revoke",
            )
            links = self._repository.list_active_identity_links_for_mim_user(
                mim_user_id=str(user_id)
            )
            for link in links:
                self._repository.revoke_identity_link(
                    install_id=link.install_id,
                    mim_user_id=str(user_id),
                    revoked_at=revoked_at,
                )
        except (LifecycleEffectsError, NotFound, SlackOAuthInstallRepositoryError):
            raise LifecycleEffectsError("lifecycle effect was denied.") from None
        except Exception:
            raise LifecycleEffectsError("lifecycle effect was denied.") from None


class LifecycleAuditNotifier:
    def __init__(
        self,
        *,
        store: Store,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._clock = clock

    def notify_admin(self, *, user_id: UserId, kind: str, reason: str) -> None:
        if kind != "notify_admin":
            raise LifecycleEffectsError("lifecycle effect was denied.")
        user = self._store.get_user(user_id)
        _require_stateful_user_reason(user.state, _require_reason(reason))
        _append_lifecycle_audit(
            store=self._store,
            action="lifecycle_notify_admin",
            target_ref=f"user:{user_id}",
            reason=reason,
            occurred_at=require_utc_datetime(self._clock(), label="lifecycle audit"),
            after_ref=f"kind:{kind}",
        )

    def notify_owner(
        self,
        *,
        user_id: UserId,
        workload_id: WorkloadId,
        reason: str,
    ) -> None:
        user = self._store.get_user(user_id)
        workload = self._store.get_workload(workload_id)
        if workload.owner_id != user_id:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        _require_stateful_user_reason(user.state, _require_reason(reason))
        _append_lifecycle_audit(
            store=self._store,
            action="lifecycle_notify_owner",
            target_ref=f"workload:{workload_id}",
            reason=reason,
            occurred_at=require_utc_datetime(self._clock(), label="lifecycle audit"),
            before_ref=f"user:{user_id}",
        )


class LifecycleAuditTransferManager:
    def __init__(
        self,
        *,
        store: Store,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._clock = clock

    def open_transfer_window(
        self,
        *,
        user_id: UserId,
        workload_ids: tuple[WorkloadId, ...],
        reason: str,
    ) -> None:
        user = self._store.get_user(user_id)
        _require_stateful_user_reason(user.state, _require_reason(reason))
        if type(workload_ids) is not tuple:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        for workload_id in workload_ids:
            workload = self._store.get_workload(workload_id)
            if workload.owner_id != user_id:
                raise LifecycleEffectsError("lifecycle effect was denied.")
        _append_lifecycle_audit(
            store=self._store,
            action="lifecycle_transfer_window",
            target_ref=f"user:{user_id}",
            reason=reason,
            occurred_at=require_utc_datetime(self._clock(), label="lifecycle audit"),
            after_ref=",".join(
                sorted(str(workload_id) for workload_id in workload_ids)
            ),
        )


def _append_lifecycle_audit(
    *,
    store: Store,
    action: str,
    target_ref: str,
    reason: str,
    occurred_at: datetime,
    before_ref: str | None = None,
    after_ref: str | None = None,
) -> None:
    event_id = _deterministic_audit_id(
        action,
        target_ref,
        reason,
        before_ref or "",
        after_ref or "",
    )
    event = AuditEvent(
        id=event_id,
        actor_id=None,
        action=action,
        target_ref=target_ref,
        policy_decision=reason,
        before_ref=before_ref,
        after_ref=after_ref,
        correlation_id=_deterministic_correlation_id(action, target_ref, reason),
        outcome="recorded",
        occurred_at=occurred_at,
    )
    try:
        store.append_audit_event(event)
    except AlreadyExists:
        return


def _scheduler_job_resource(schedule_id: ScheduleId) -> str:
    digest = hashlib.sha256(str(schedule_id).encode("utf-8")).hexdigest()[:20]
    return f"projects/{_CENTRAL_PROJECT_ID}/locations/{REGION}/jobs/mim-sch-{digest}"


def _scheduler_description(schedule_id: ScheduleId) -> str:
    digest = hashlib.sha256(str(schedule_id).encode("utf-8")).hexdigest()[:20]
    return f"MIM managed hourly schedule {digest}"


def _gateway_audience(project_number: str) -> str:
    return f"https://mim-schedule-gateway-{project_number}.{REGION}.run.app"


def _scheduler_service_account() -> str:
    return f"mim-schedule-gateway@{_CENTRAL_PROJECT_ID}.iam.gserviceaccount.com"


def _gateway_target(
    schedule: Schedule,
    *,
    project_number: str,
) -> scheduler_v1.HttpTarget:
    body = json.dumps(
        {
            "schedule_id": str(schedule.id),
            "workload_id": str(schedule.workload_id),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    audience = _gateway_audience(project_number)
    return scheduler_v1.HttpTarget(
        uri=f"{audience}{_GATEWAY_PATH}",
        http_method=scheduler_v1.HttpMethod.POST,
        headers={"Content-Type": "application/json"},
        body=body,
        oidc_token=scheduler_v1.OidcToken(
            service_account_email=_scheduler_service_account(),
            audience=audience,
        ),
    )


def _require_scheduler_job_boundary(
    job: scheduler_v1.Job,
    *,
    schedule: Schedule,
    project_number: str,
) -> None:
    expected_name = _scheduler_job_resource(schedule.id)
    if (
        job.name != expected_name
        or job.description != _scheduler_description(schedule.id)
        or job.schedule != schedule.cron
        or job.time_zone != schedule.timezone
        or job.retry_config.retry_count != 0
        or job.attempt_deadline.seconds != 30
    ):
        raise LifecycleEffectsError("lifecycle effect was denied.")
    if job.http_target != _gateway_target(schedule, project_number=project_number):
        raise LifecycleEffectsError("lifecycle effect was denied.")


class LifecycleScheduleManager:
    def __init__(
        self,
        *,
        store: Store,
        client: Any,
        project_number: str,
    ) -> None:
        if type(project_number) is not str or not project_number.isdigit():
            raise ValueError("project_number is invalid.")
        self._store = store
        self._client = client
        self._project_number = project_number

    def apply_schedule_state(
        self,
        *,
        schedule_id: ScheduleId,
        workload_id: WorkloadId,
        target_state: ScheduleState,
        expected_schedule_version: int,
        reason: str,
    ) -> None:
        _require_reason(reason)
        try:
            schedule = self._store.get_schedule(schedule_id)
            if schedule.workload_id != workload_id:
                raise LifecycleEffectsError("lifecycle effect was denied.")
            _require_exact_version(schedule.version, expected_schedule_version)
            workload = self._store.get_workload(workload_id)
            user = self._store.get_user(workload.owner_id)
            _require_schedule_effect_reason(user.state, reason)
            if target_state in {
                ScheduleState.DISABLED,
                ScheduleState.PAUSED,
                ScheduleState.QUARANTINED,
            }:
                self._pause_job(schedule)
                return
            if target_state is ScheduleState.ARCHIVED:
                self._delete_job(schedule)
                return
            if target_state is ScheduleState.ENABLED:
                return
        except (LifecycleEffectsError, NotFound, VersionConflict):
            raise LifecycleEffectsError("lifecycle effect was denied.") from None
        except Exception:
            raise LifecycleEffectsError("lifecycle effect was denied.") from None
        raise LifecycleEffectsError("lifecycle effect was denied.")

    def _pause_job(self, schedule: Schedule) -> None:
        name = _scheduler_job_resource(schedule.id)
        try:
            job = self._client.get_job(scheduler_v1.GetJobRequest(name=name))
        except GoogleNotFound:
            return
        _require_scheduler_job_boundary(
            job,
            schedule=schedule,
            project_number=self._project_number,
        )
        if job.state is scheduler_v1.Job.State.PAUSED:
            return
        if job.state is not scheduler_v1.Job.State.ENABLED:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        paused = self._client.pause_job(scheduler_v1.PauseJobRequest(name=name))
        _require_scheduler_job_boundary(
            paused,
            schedule=schedule,
            project_number=self._project_number,
        )
        if paused.state is not scheduler_v1.Job.State.PAUSED:
            raise LifecycleEffectsError("lifecycle effect was denied.")

    def _delete_job(self, schedule: Schedule) -> None:
        name = _scheduler_job_resource(schedule.id)
        try:
            job = self._client.get_job(scheduler_v1.GetJobRequest(name=name))
        except GoogleNotFound:
            return
        _require_scheduler_job_boundary(
            job,
            schedule=schedule,
            project_number=self._project_number,
        )
        self._client.delete_job(scheduler_v1.DeleteJobRequest(name=name))


class LifecycleComputeManager:
    def __init__(
        self,
        *,
        store: Store,
        services_client: Any,
        jobs_client: Any,
        scheduler_client: Any,
        project_number: str,
        reviewed_breakglass_members: Sequence[str] = (),
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if type(project_number) is not str or not project_number.isdigit():
            raise ValueError("project_number is invalid.")
        self._store = store
        self._services = services_client
        self._jobs = jobs_client
        self._scheduler = scheduler_client
        self._project_number = project_number
        self._reviewed_breakglass_members = normalize_reviewed_breakglass_members(
            list(reviewed_breakglass_members)
        )
        self._gateway_invoker_members = (
            app_gateway_invoker_member(_CENTRAL_PROJECT_ID),
            *self._reviewed_breakglass_members,
        )
        self._clock = clock

    def delete_compute(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        target_kinds: tuple[str, ...],
        retain_image_until: datetime | None,
    ) -> None:
        if type(target_kinds) is not tuple or not target_kinds:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        if retain_image_until is not None:
            require_utc_datetime(retain_image_until, label="retain image until")
        normalized = tuple(target_kinds)
        if any(kind not in _LIFECYCLE_POLICY_KINDS for kind in normalized):
            raise LifecycleEffectsError("lifecycle effect was denied.")
        if len(set(normalized)) != len(normalized):
            raise LifecycleEffectsError("lifecycle effect was denied.")
        current_at = require_utc_datetime(
            self._clock(),
            label="lifecycle compute binding",
        )
        try:
            workload = self._store.get_workload(workload_id)
            _require_exact_version(workload.version, expected_workload_version)
            if workload.state is WorkloadState.ARCHIVED:
                raise LifecycleEffectsError("lifecycle effect was denied.")
            self._store.get_user(workload.owner_id)
            allowed = {
                WorkloadKind.NEXTJS: frozenset({"cloud_run_service"}),
                WorkloadKind.STREAMLIT: frozenset({"cloud_run_service"}),
                WorkloadKind.SCHEDULED_SCRIPT: frozenset(
                    {"cloud_run_job", "cloud_scheduler_job"}
                ),
            }[workload.kind]
            if not set(normalized).issubset(allowed):
                raise LifecycleEffectsError("lifecycle effect was denied.")
            web_workload = workload.kind in {
                WorkloadKind.NEXTJS,
                WorkloadKind.STREAMLIT,
            }
            for target in normalized:
                if target == "cloud_run_service":
                    self._delete_service(
                        workload_id,
                        workload_owner_id=str(workload.owner_id),
                    )
                elif target == "cloud_run_job":
                    self._delete_job(
                        workload_id,
                        workload_owner_id=str(workload.owner_id),
                    )
                else:
                    schedules = self._store.list_schedules(owner_id=workload.owner_id)
                    for schedule in schedules:
                        if schedule.workload_id == workload_id:
                            self._delete_scheduler_schedule(schedule)
            # Preserve the public binding until the exact Cloud Run deletion
            # has passed boundary validation and completed.  A retry can retire
            # an idempotently missing service, but must never hide a live
            # service after a denied or failed delete.
            if web_workload:
                self._retire_app_binding(workload=workload, at=current_at)
        except (LifecycleEffectsError, NotFound, VersionConflict):
            raise LifecycleEffectsError("lifecycle effect was denied.") from None
        except Exception:
            raise LifecycleEffectsError("lifecycle effect was denied.") from None

    def _retire_app_binding(
        self,
        *,
        workload: Workload,
        at: datetime,
    ) -> None:
        public_host = build_app_hostname(str(workload.name), str(workload.id))
        try:
            binding = self._store.get_app_hostname_binding(public_host)
        except NotFound:
            return
        if binding.workload_id != workload.id or binding.owner_id != workload.owner_id:
            raise LifecycleEffectsError("lifecycle effect was denied.")
        if binding.state is AppHostnameBindingState.RETIRED:
            return
        retired = binding.transition_state(AppHostnameBindingState.RETIRED, at=at)
        self._store.save_app_hostname_binding(
            retired,
            expected_version=binding.version,
        )

    def _delete_service(
        self,
        workload_id: WorkloadId,
        *,
        workload_owner_id: str,
    ) -> None:
        name = cloud_run_service_name(
            project_id=_CENTRAL_PROJECT_ID,
            region=REGION,
            workload_id=str(workload_id),
        )
        try:
            service = self._services.get_service(name=name)
        except GoogleNotFound:
            return
        policy = self._services.get_iam_policy(
            iam_policy_pb2.GetIamPolicyRequest(resource=name)
        )
        _require_safe_service_boundary(
            service,
            policy=policy,
            project_id=_CENTRAL_PROJECT_ID,
            region=REGION,
            workload_id=str(workload_id),
            workload_owner_id=workload_owner_id,
            gateway_invoker_members=self._gateway_invoker_members,
        )
        self._services.delete_service(name=name).result()

    def _delete_job(
        self,
        workload_id: WorkloadId,
        *,
        workload_owner_id: str,
    ) -> None:
        name = cloud_run_job_name(
            project_id=_CENTRAL_PROJECT_ID,
            region=REGION,
            workload_id=str(workload_id),
        )
        try:
            job = self._jobs.get_job(name=name)
        except GoogleNotFound:
            return
        policy = self._jobs.get_iam_policy(
            iam_policy_pb2.GetIamPolicyRequest(resource=name)
        )
        _require_safe_job_boundary(
            job,
            policy=policy,
            project_id=_CENTRAL_PROJECT_ID,
            region=REGION,
            workload_id=str(workload_id),
            workload_owner_id=workload_owner_id,
        )
        self._jobs.delete_job(name=name).result()

    def _delete_scheduler_schedule(self, schedule: Schedule) -> None:
        name = _scheduler_job_resource(schedule.id)
        try:
            job = self._scheduler.get_job(scheduler_v1.GetJobRequest(name=name))
        except GoogleNotFound:
            return
        _require_scheduler_job_boundary(
            job,
            schedule=schedule,
            project_number=self._project_number,
        )
        self._scheduler.delete_job(scheduler_v1.DeleteJobRequest(name=name))


__all__ = [
    "LifecycleAuditNotifier",
    "LifecycleAuditSessionGate",
    "LifecycleAuditTransferManager",
    "LifecycleComputeManager",
    "LifecycleEffectsError",
    "LifecycleIapAccessManager",
    "LifecycleScheduleManager",
    "LifecycleSecretBindingManager",
    "LifecycleSlackGrantManager",
]
