"""Central authorization for app-gateway routing decisions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

from mim_control_plane.domain.models import (
    AuditEvent,
    AuditEventId,
    OriginRequestClaim,
    OriginRequestId,
    UserId,
    Workload,
)
from mim_control_plane.domain.states import WorkloadKind, WorkloadState
from mim_control_plane.ports.store import (
    AlreadyExists,
    NotFound,
    ReplayDetected,
    Store,
    VersionConflict,
)
from mim_control_plane.security.authorization import (
    AccessDenied,
    IdentityPolicy,
    require_owner_or_admin,
)
from mim_control_plane.services.app_hostname import validate_app_public_host
from mim_control_plane.services.org_cost_guard import (
    OrgCostGuardDenied,
    require_current_org_cost_guard,
)
from mim_control_plane.services.quota import evaluate_cost_policy
from mim_control_plane.services.usage import (
    build_cost_snapshot,
    build_usage_ledger,
    usage_entries_for_utc_month,
)

_METHOD_PATTERN = re.compile(r"^[A-Z]{3,16}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_TARGET_PATTERN = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/?%\-]*$")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_SCHEMA = "mim.app-authorization.v1"
_REPLAY_WINDOW = timedelta(seconds=60)
_DECISION_TTL = timedelta(seconds=30)
_ACTIVITY_HEARTBEAT_WINDOW = timedelta(hours=1)
_GENERIC_DENIED = "App request was denied."
_HEARTBEAT_AUDIT_ACTION = "heartbeat_write_failed"
_HEARTBEAT_AUDIT_TARGET = "app_gateway"
_HEARTBEAT_AUDIT_DECISION = "best_effort_suppressed"
_HEARTBEAT_AUDIT_OUTCOME = "recorded"


class AppGatewayAuthorizationDenied(PermissionError):
    """Raised when an app request falls outside the reviewed boundary."""


@dataclass(frozen=True, slots=True)
class AppAuthorizationRequest:
    schema: str
    public_host: str
    method: str
    request_target: str
    access_subject: str
    access_email: str
    edge_request_id: str
    edge_timestamp: int
    edge_body_sha256: str

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError("schema is invalid")
        validate_app_public_host(self.public_host)
        if _METHOD_PATTERN.fullmatch(self.method) is None:
            raise ValueError("method is invalid")
        if not _is_valid_request_target(self.request_target):
            raise ValueError("request_target is invalid")
        if _IDENTIFIER_PATTERN.fullmatch(self.access_subject) is None:
            raise ValueError("access_subject is invalid")
        if (
            type(self.access_email) is not str
            or self.access_email != self.access_email.strip()
        ):
            raise ValueError("access_email is invalid")
        if _IDENTIFIER_PATTERN.fullmatch(self.edge_request_id) is None:
            raise ValueError("edge_request_id is invalid")
        if (
            type(self.edge_timestamp) is not int
            or isinstance(self.edge_timestamp, bool)
        ):
            raise ValueError("edge_timestamp is invalid")
        if _SHA256_PATTERN.fullmatch(self.edge_body_sha256) is None:
            raise ValueError("edge_body_sha256 is invalid")


@dataclass(frozen=True, slots=True)
class AppAuthorizationDecision:
    schema: str
    public_host: str
    workload_id: str
    upstream_url: str
    upstream_audience: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError("schema is invalid")
        validate_app_public_host(self.public_host)
        if type(self.workload_id) is not str or not self.workload_id:
            raise ValueError("workload_id is invalid")
        if self.upstream_url != self.upstream_audience:
            raise ValueError("upstream_audience must equal upstream_url")
        if (
            self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("expires_at must be UTC-aware")


class AppGatewayAuthorizationService:
    def __init__(
        self,
        *,
        store: Store,
        identity_policy: IdentityPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._identity_policy = identity_policy
        self._clock = clock

    def authorize(self, request: AppAuthorizationRequest) -> AppAuthorizationDecision:
        try:
            now = self._clock()
            _require_utc(now)
            edge_time = datetime.fromtimestamp(request.edge_timestamp, tz=UTC)
            age = now - edge_time
            if age < timedelta(0) or age > _REPLAY_WINDOW:
                raise AppGatewayAuthorizationDenied(_GENERIC_DENIED)
            self._store.claim_origin_request(
                OriginRequestClaim(
                    request_id=OriginRequestId(request.edge_request_id),
                    body_hash=request.edge_body_sha256,
                    claimed_at=edge_time,
                    expires_at=edge_time + _REPLAY_WINDOW,
                )
            )
            binding = self._store.get_app_hostname_binding(request.public_host)
            if str(binding.public_host) != request.public_host:
                raise AppGatewayAuthorizationDenied(_GENERIC_DENIED)
            if str(binding.state) != "active":
                raise AppGatewayAuthorizationDenied(_GENERIC_DENIED)
            workload = self._store.get_workload(binding.workload_id)
            if (
                workload.id != binding.workload_id
                or workload.owner_id != binding.owner_id
                or workload.kind != binding.workload_kind
            ):
                raise AppGatewayAuthorizationDenied(_GENERIC_DENIED)
            principal = self._identity_policy.authorize_resolved_user(
                user_id=UserId(request.access_subject),
                email=request.access_email,
            )
            require_owner_or_admin(principal, binding.owner_id)
            if workload.kind not in (WorkloadKind.NEXTJS, WorkloadKind.STREAMLIT):
                raise AppGatewayAuthorizationDenied(_GENERIC_DENIED)
            if workload.state is not WorkloadState.ACTIVE:
                raise AppGatewayAuthorizationDenied(_GENERIC_DENIED)
            require_current_org_cost_guard(store=self._store, now=now)
            decision = self._current_cost_decision(user_id=binding.owner_id, now=now)
            if decision.pause or decision.emergency_stop:
                raise AppGatewayAuthorizationDenied(_GENERIC_DENIED)
            self._record_workload_activity_best_effort(
                workload_id=workload.id,
                request_id=request.edge_request_id,
                now=now,
            )
            return AppAuthorizationDecision(
                schema=_SCHEMA,
                public_host=request.public_host,
                workload_id=str(workload.id),
                upstream_url=binding.upstream_url,
                upstream_audience=binding.upstream_audience,
                expires_at=now + _DECISION_TTL,
            )
        except (
            AccessDenied,
            AppGatewayAuthorizationDenied,
            NotFound,
            OrgCostGuardDenied,
            ReplayDetected,
            ValueError,
        ):
            raise AppGatewayAuthorizationDenied(_GENERIC_DENIED) from None

    def _current_cost_decision(self, *, user_id: UserId, now: datetime):
        owner_entries = usage_entries_for_utc_month(
            self._store.list_usage_entries(owner_id=user_id),
            now=now,
        )
        return evaluate_cost_policy(
            snapshot=build_cost_snapshot(
                build_usage_ledger(owner_entries),
                user_id=user_id,
            )
        )

    def _record_workload_activity_best_effort(
        self,
        *,
        workload_id,
        request_id: str,
        now: datetime,
    ) -> None:
        try:
            current = self._store.get_workload(workload_id)
            if not _eligible_app_heartbeat_workload(current):
                return
            if _heartbeat_is_recent(current.last_activity_at, now=now):
                return
            self._store.save_workload(
                current.record_activity(at=now),
                expected_version=current.version,
            )
        except VersionConflict:
            try:
                reloaded = self._store.get_workload(workload_id)
                if not _eligible_app_heartbeat_workload(reloaded):
                    return
                if _heartbeat_is_recent(reloaded.last_activity_at, now=now):
                    return
                self._store.save_workload(
                    reloaded.record_activity(at=now),
                    expected_version=reloaded.version,
                )
            except (NotFound, ValueError, VersionConflict):
                return
            except Exception:
                self._append_heartbeat_failure_signal(
                    occurred_at=now,
                    discriminator=_heartbeat_signal_discriminator(
                        workload_id=str(workload_id),
                        request_id=request_id,
                    ),
                )
                return
        except (NotFound, ValueError):
            return
        except Exception:
            self._append_heartbeat_failure_signal(
                occurred_at=now,
                discriminator=_heartbeat_signal_discriminator(
                    workload_id=str(workload_id),
                    request_id=request_id,
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


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("datetime must be UTC-aware")


def _is_valid_request_target(value: object) -> bool:
    if (
        type(value) is not str
        or value.startswith("//")
        or value.count("?") > 1
        or _REQUEST_TARGET_PATTERN.fullmatch(value) is None
    ):
        return False
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in _HEX_DIGITS
            or value[index + 2] not in _HEX_DIGITS
        ):
            return False
        index += 3
    return True


def _heartbeat_is_recent(last_activity_at: datetime | None, *, now: datetime) -> bool:
    return (
        last_activity_at is not None
        and last_activity_at > now - _ACTIVITY_HEARTBEAT_WINDOW
    )


def _eligible_app_heartbeat_workload(workload: Workload) -> bool:
    return (
        workload.kind in (WorkloadKind.NEXTJS, WorkloadKind.STREAMLIT)
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
            f"app-heartbeat:{target_ref}:{occurred_at.isoformat()}:"
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
            f"app-heartbeat-corr:{target_ref}:{occurred_at.isoformat()}:"
            f"{discriminator}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"corr-heartbeat-{digest}"


def _heartbeat_signal_discriminator(*, workload_id: str, request_id: str) -> str:
    return hashlib.sha256(
        f"app-heartbeat-signal:{workload_id}:{request_id}".encode("utf-8")
    ).hexdigest()[:24]
