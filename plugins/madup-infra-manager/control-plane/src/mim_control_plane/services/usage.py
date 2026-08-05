"""Usage attribution and normalized activity metrics for the MIM control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Iterable, Mapping

from mim_control_plane.config import ADMIN_BUDGET_CEILING_KRW, TARGET_MONTHLY_BUDGET_KRW
from mim_control_plane.domain.models import (
    ActivityEvent,
    ActivityEventId,
    AuditEvent,
    DailyUsageAggregate,
    UsageEntry,
    UserId,
)
from mim_control_plane.domain.states import ActivityOutcome, ActivitySurface

_TARGET_REF_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/"
)
_CORRELATION_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)
_GENERIC_ACTIVITY_ERROR = "Activity payload contains unsupported or unsafe values."
_TARGET_REF_MAX_LENGTH = 128
_CORRELATION_ID_MAX_LENGTH = 64
_CORRELATION_ID_PREFIX = "corr-"


class ActivityMetricsError(ValueError):
    """Raised when normalized activity input or metric computation fails closed."""


class UsageLedgerError(ValueError):
    """Raised when immutable usage inputs would produce unsafe attribution."""


class ActivityAction(StrEnum):
    VIEW_DASHBOARD = "view_dashboard"
    PLAN_DEPLOY = "plan_deploy"
    PLAN_SCHEDULE = "plan_schedule"
    GET_OPERATION = "get_operation"
    LIST_WORKLOADS = "list_workloads"
    GET_USAGE = "get_usage"
    EXPLAIN_FAILURE = "explain_failure"
    DEPLOY_EXECUTION = "deploy_execution"
    SCHEDULE_RUN = "schedule_run"
    REPAIR_EXECUTION = "repair_execution"


@dataclass(frozen=True, slots=True)
class UsageLedger:
    entries: tuple[UsageEntry, ...]
    direct_entries: tuple[UsageEntry, ...]
    shared_entries: tuple[UsageEntry, ...]
    latest_collected_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise ValueError("entries must be immutable.")
        if not isinstance(self.direct_entries, tuple):
            raise ValueError("direct_entries must be immutable.")
        if not isinstance(self.shared_entries, tuple):
            raise ValueError("shared_entries must be immutable.")
        _require_utc_or_none(self.latest_collected_at, "latest_collected_at")


@dataclass(frozen=True, slots=True)
class CostSnapshot:
    user_direct_estimated_krw: int
    user_direct_finalized_krw: int
    user_policy_krw: int
    org_direct_estimated_krw: int
    org_direct_finalized_krw: int
    org_direct_policy_krw: int
    shared_estimated_krw: int
    shared_finalized_krw: int
    shared_policy_krw: int
    user_percent: int

    def __post_init__(self) -> None:
        for field_name in (
            "user_direct_estimated_krw",
            "user_direct_finalized_krw",
            "user_policy_krw",
            "org_direct_estimated_krw",
            "org_direct_finalized_krw",
            "org_direct_policy_krw",
            "shared_estimated_krw",
            "shared_finalized_krw",
            "shared_policy_krw",
            "user_percent",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class ActivityMetricsSummary:
    active_users_24h: int
    active_users_7d: int
    active_users_30d: int
    dashboard_unique_visitors_24h: int
    dashboard_unique_visitors_7d: int
    dashboard_unique_visitors_30d: int
    dashboard_visits_24h: int
    dashboard_visits_7d: int
    dashboard_visits_30d: int
    mcp_actions_30d: int
    deployments_30d: int
    schedule_runs_30d: int
    successes_30d: int
    failures_30d: int
    denials_30d: int

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            _require_non_negative_int(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class DailyActivityRollup:
    organization: DailyUsageAggregate
    by_user: Mapping[UserId, DailyUsageAggregate]

    def __post_init__(self) -> None:
        if not isinstance(self.by_user, Mapping):
            raise ValueError("by_user must be a mapping.")


@dataclass(frozen=True, slots=True)
class ActivityRetentionPlan:
    cutoff: datetime
    expired_event_ids: tuple[ActivityEventId, ...]
    keep_event_ids: tuple[ActivityEventId, ...]

    def __post_init__(self) -> None:
        _require_utc(self.cutoff, "cutoff")
        if not isinstance(self.expired_event_ids, tuple):
            raise ValueError("expired_event_ids must be immutable.")
        if not isinstance(self.keep_event_ids, tuple):
            raise ValueError("keep_event_ids must be immutable.")


def build_usage_ledger(entries: Iterable[UsageEntry]) -> UsageLedger:
    """Split immutable usage entries into direct and shared buckets."""

    ordered_entries = tuple(
        sorted(entries, key=lambda entry: (entry.collected_at, str(entry.id)))
    )
    seen_ids: set[ActivityEventId | str] = set()
    for entry in ordered_entries:
        if entry.id in seen_ids:
            raise UsageLedgerError("Usage entries must not repeat the same ID.")
        seen_ids.add(entry.id)
    direct_entries = tuple(
        entry for entry in ordered_entries if entry.owner_id is not None
    )
    shared_entries = tuple(entry for entry in ordered_entries if entry.owner_id is None)
    latest = None if not ordered_entries else ordered_entries[-1].collected_at
    return UsageLedger(
        entries=ordered_entries,
        direct_entries=direct_entries,
        shared_entries=shared_entries,
        latest_collected_at=latest,
    )


def usage_entries_for_utc_month(
    entries: Iterable[UsageEntry],
    *,
    now: datetime,
) -> tuple[UsageEntry, ...]:
    """Return only usage entries collected within the explicit UTC calendar month."""

    _require_utc(now, "now")
    return tuple(
        entry
        for entry in entries
        if entry.collected_at.year == now.year and entry.collected_at.month == now.month
    )


def build_cost_snapshot(ledger: UsageLedger, *, user_id: UserId) -> CostSnapshot:
    """Compute immutable direct/shared cost totals for a user and the organization."""

    user_entries = tuple(
        entry for entry in ledger.direct_entries if entry.owner_id == user_id
    )
    user_direct_estimated = sum(entry.estimated_cost_krw for entry in user_entries)
    user_direct_finalized = sum(
        entry.finalized_cost_krw or 0 for entry in user_entries
    )
    org_direct_estimated = sum(
        entry.estimated_cost_krw for entry in ledger.direct_entries
    )
    org_direct_finalized = sum(
        entry.finalized_cost_krw or 0 for entry in ledger.direct_entries
    )
    shared_estimated = sum(
        entry.estimated_cost_krw for entry in ledger.shared_entries
    )
    shared_finalized = sum(
        entry.finalized_cost_krw or 0 for entry in ledger.shared_entries
    )
    user_policy = _policy_cost(user_entries)
    org_direct_policy = _policy_cost(ledger.direct_entries)
    shared_policy = _policy_cost(ledger.shared_entries)
    return CostSnapshot(
        user_direct_estimated_krw=user_direct_estimated,
        user_direct_finalized_krw=user_direct_finalized,
        user_policy_krw=user_policy,
        org_direct_estimated_krw=org_direct_estimated,
        org_direct_finalized_krw=org_direct_finalized,
        org_direct_policy_krw=org_direct_policy,
        shared_estimated_krw=shared_estimated,
        shared_finalized_krw=shared_finalized,
        shared_policy_krw=shared_policy,
        user_percent=(user_policy * 100) // TARGET_MONTHLY_BUDGET_KRW,
    )


def ingest_activity_event(
    *,
    event_id: str,
    trusted_user_id: UserId,
    trusted_correlation_id: str,
    trusted_occurred_at: datetime,
    observed_at: datetime,
    payload: Mapping[str, object],
) -> ActivityEvent:
    """Convert reviewed action metadata plus trusted server context into an event."""

    _require_target_ref(event_id, "event_id", max_length=64)
    _require_target_ref(str(trusted_user_id), "trusted_user_id", max_length=64)
    correlation_id = _parse_correlation_id(trusted_correlation_id)
    occurred_at = _parse_trusted_occurred_at(
        trusted_occurred_at,
        observed_at=observed_at,
    )
    if not isinstance(payload, Mapping):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)

    allowed_keys = frozenset(
        {
            "surface",
            "action",
            "target_ref",
            "outcome",
            "latency_ms",
        }
    )
    if set(payload) - allowed_keys:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)

    surface = _parse_surface(payload.get("surface"))
    action = _parse_action(payload.get("action"))
    target_ref = _parse_optional_safe_ref(payload.get("target_ref"), "target_ref")
    outcome = _parse_outcome(payload.get("outcome"))
    latency_bucket = _latency_bucket(payload.get("latency_ms"))
    return ActivityEvent(
        id=ActivityEventId(event_id),
        user_id=trusted_user_id,
        surface=surface,
        action=action,
        target_ref=target_ref,
        outcome=outcome,
        latency_bucket=latency_bucket,
        correlation_id=correlation_id,
        occurred_at=occurred_at,
    )


def compute_activity_metrics(
    events: Iterable[ActivityEvent],
    *,
    now: datetime,
    user_id: UserId | None = None,
) -> ActivityMetricsSummary:
    """Compute deterministic rolling-window activity metrics."""

    _require_utc(now, "now")
    filtered_events = _filter_events(tuple(events), user_id=user_id)
    validated_events = _validated_activity_events(filtered_events, now=now)
    window_24h = tuple(
        _events_in_window(
            validated_events,
            start=now - timedelta(hours=24),
            now=now,
        )
    )
    window_7d = tuple(
        _events_in_window(
            validated_events,
            start=now - timedelta(days=7),
            now=now,
        )
    )
    window_30d = tuple(
        _events_in_window(validated_events, start=now - timedelta(days=30), now=now)
    )
    return ActivityMetricsSummary(
        active_users_24h=len({event.user_id for event in window_24h}),
        active_users_7d=len({event.user_id for event in window_7d}),
        active_users_30d=len({event.user_id for event in window_30d}),
        dashboard_unique_visitors_24h=len(_dashboard_visitors(window_24h)),
        dashboard_unique_visitors_7d=len(_dashboard_visitors(window_7d)),
        dashboard_unique_visitors_30d=len(_dashboard_visitors(window_30d)),
        dashboard_visits_24h=_dashboard_visits(window_24h),
        dashboard_visits_7d=_dashboard_visits(window_7d),
        dashboard_visits_30d=_dashboard_visits(window_30d),
        mcp_actions_30d=sum(
            1 for event in window_30d if event.surface is ActivitySurface.MCP
        ),
        deployments_30d=sum(
            1 for event in window_30d if event.action == ActivityAction.DEPLOY_EXECUTION
        ),
        schedule_runs_30d=sum(
            1 for event in window_30d if event.action == ActivityAction.SCHEDULE_RUN
        ),
        successes_30d=sum(
            1 for event in window_30d if event.outcome is ActivityOutcome.SUCCEEDED
        ),
        failures_30d=sum(
            1 for event in window_30d if event.outcome is ActivityOutcome.FAILED
        ),
        denials_30d=sum(
            1 for event in window_30d if event.outcome is ActivityOutcome.DENIED
        ),
    )


def aggregate_daily_activity(
    events: Iterable[ActivityEvent],
    *,
    day: date,
    now: datetime | None = None,
) -> DailyActivityRollup:
    """Create fresh day-bucket payloads with version=1 for later persistence."""

    now_utc = datetime.now(UTC) if now is None else now
    _require_utc(now_utc, "now")
    validated_events = tuple(_validated_activity_events(events, now=now_utc))
    day_start = datetime.combine(day, time.min, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    day_events = tuple(
        event
        for event in validated_events
        if day_start <= event.occurred_at < day_end
    )
    org_updated_at = max((event.occurred_at for event in day_events), default=day_start)
    organization = _daily_usage_aggregate(
        day=day,
        user_id=None,
        events=day_events,
        updated_at=org_updated_at,
    )
    user_ids = tuple(sorted({event.user_id for event in day_events}, key=str))
    by_user = {
        user_id: _daily_usage_aggregate(
            day=day,
            user_id=user_id,
            events=tuple(event for event in day_events if event.user_id == user_id),
            updated_at=max(
                (
                    event.occurred_at
                    for event in day_events
                    if event.user_id == user_id
                ),
                default=day_start,
            ),
        )
        for user_id in user_ids
    }
    return DailyActivityRollup(
        organization=organization,
        by_user=MappingProxyType(by_user),
    )


def plan_activity_retention(
    events: Iterable[ActivityEvent | AuditEvent],
    *,
    now: datetime,
) -> ActivityRetentionPlan:
    """Plan detailed activity expiry without touching append-only audit events."""

    _require_utc(now, "now")
    cutoff = now - timedelta(days=30)
    expired_ids: list[ActivityEventId] = []
    keep_ids: list[ActivityEventId] = []
    seen_ids: set[ActivityEventId] = set()
    for event in events:
        if isinstance(event, AuditEvent):
            raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
        if not isinstance(event, ActivityEvent):
            raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
        if event.id in seen_ids:
            raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
        seen_ids.add(event.id)
        _validate_event_time(event.occurred_at, now=now)
        if event.occurred_at < cutoff:
            expired_ids.append(event.id)
        else:
            keep_ids.append(event.id)
    return ActivityRetentionPlan(
        cutoff=cutoff,
        expired_event_ids=tuple(sorted(expired_ids, key=str)),
        keep_event_ids=tuple(sorted(keep_ids, key=str)),
    )


def _daily_usage_aggregate(
    *,
    day: date,
    user_id: UserId | None,
    events: tuple[ActivityEvent, ...],
    updated_at: datetime,
) -> DailyUsageAggregate:
    return DailyUsageAggregate(
        day=day,
        user_id=user_id,
        active_users=0 if not events else len({event.user_id for event in events}),
        dashboard_visits=_dashboard_visits(events),
        mcp_actions=sum(1 for event in events if event.surface is ActivitySurface.MCP),
        deployments=sum(
            1 for event in events if event.action == ActivityAction.DEPLOY_EXECUTION
        ),
        schedule_executions=sum(
            1 for event in events if event.action == ActivityAction.SCHEDULE_RUN
        ),
        successes=sum(
            1 for event in events if event.outcome is ActivityOutcome.SUCCEEDED
        ),
        failures=sum(1 for event in events if event.outcome is ActivityOutcome.FAILED),
        policy_denials=sum(
            1 for event in events if event.outcome is ActivityOutcome.DENIED
        ),
        version=1,
        updated_at=updated_at,
    )


def _validated_activity_events(
    events: Iterable[ActivityEvent],
    *,
    now: datetime,
) -> tuple[ActivityEvent, ...]:
    validated: list[ActivityEvent] = []
    seen_ids: set[ActivityEventId] = set()
    for event in events:
        if not isinstance(event, ActivityEvent):
            raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
        if event.id in seen_ids:
            raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
        seen_ids.add(event.id)
        _validate_event_time(event.occurred_at, now=now)
        validated.append(event)
    return tuple(
        sorted(validated, key=lambda event: (event.occurred_at, str(event.id)))
    )


def _filter_events(
    events: Iterable[ActivityEvent],
    *,
    user_id: UserId | None,
) -> tuple[ActivityEvent, ...]:
    if user_id is None:
        return tuple(events)
    return tuple(event for event in events if event.user_id == user_id)


def _events_in_window(
    events: Iterable[ActivityEvent],
    *,
    start: datetime,
    now: datetime,
) -> tuple[ActivityEvent, ...]:
    return tuple(event for event in events if start <= event.occurred_at <= now)


def _dashboard_visitors(events: Iterable[ActivityEvent]) -> frozenset[UserId]:
    return frozenset(
        event.user_id
        for event in events
        if event.surface is ActivitySurface.DASHBOARD
        and event.action == ActivityAction.VIEW_DASHBOARD
    )


def _dashboard_visits(events: Iterable[ActivityEvent]) -> int:
    return sum(
        1
        for event in events
        if event.surface is ActivitySurface.DASHBOARD
        and event.action == ActivityAction.VIEW_DASHBOARD
    )


def _validate_event_time(value: datetime, *, now: datetime) -> None:
    _require_utc(value, "occurred_at")
    if value > now:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)


def _parse_surface(value: object) -> ActivitySurface:
    if not isinstance(value, str):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    try:
        return ActivitySurface(value)
    except ValueError as exc:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR) from exc


def _parse_action(value: object) -> ActivityAction:
    if not isinstance(value, str):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    try:
        return ActivityAction(value)
    except ValueError as exc:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR) from exc


def _parse_outcome(value: object) -> ActivityOutcome:
    if not isinstance(value, str):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    try:
        return ActivityOutcome(value)
    except ValueError as exc:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR) from exc


def _parse_optional_safe_ref(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    _require_target_ref(value, field_name, max_length=_TARGET_REF_MAX_LENGTH)
    return value


def _parse_correlation_id(value: object) -> str:
    if not isinstance(value, str):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    _require_correlation_id(value)
    return value


def _parse_trusted_occurred_at(value: object, *, observed_at: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    _require_utc(value, "trusted_occurred_at")
    _require_utc(observed_at, "observed_at")
    if value > observed_at:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    return value


def _latency_bucket(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ActivityMetricsError("latency_ms must be a non-negative integer.")
    latency_ms = value
    if latency_ms < 250:
        return "lt_250ms"
    if latency_ms < 1000:
        return "lt_1000ms"
    if latency_ms < 5000:
        return "lt_5000ms"
    return "gte_5000ms"


def _require_target_ref(value: str, field_name: str, *, max_length: int) -> None:
    if not isinstance(value, str):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    stripped = value.strip()
    if not stripped or stripped != value:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    if len(stripped) > max_length:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    if any(character not in _TARGET_REF_CHARS for character in stripped):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    lowered = stripped.lower()
    secret_markers = (
        "authorization",
        "bearer",
        "cookie",
        "session=",
        "openai_api_key",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "ghp_",
        "sk-",
        "curl/",
        "mozilla/",
        "python-requests/",
        "wget/",
    )
    if any(marker in lowered for marker in secret_markers):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    if field_name == "target_ref" and lowered.startswith("env"):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    if (
        _looks_like_ipv4(stripped)
        or _looks_like_ipv6(stripped)
        or _looks_like_host_port(stripped)
    ):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)


def _require_correlation_id(value: str) -> None:
    if not isinstance(value, str):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    stripped = value.strip()
    if not stripped or stripped != value:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    if not stripped.startswith(_CORRELATION_ID_PREFIX):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    opaque_suffix = stripped.removeprefix(_CORRELATION_ID_PREFIX)
    if not opaque_suffix:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    if len(stripped) > _CORRELATION_ID_MAX_LENGTH:
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    if any(character not in _CORRELATION_ID_CHARS for character in opaque_suffix):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    lowered = stripped.lower()
    if any(
        marker in lowered
        for marker in (
            "curl",
            "mozilla",
            "python-requests",
            "wget",
            "bearer",
            "ghp_",
            "sk-",
            "api_key",
            "apikey",
            "authorization",
            "cookie",
            "session",
        )
    ):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)
    if (
        _looks_like_ipv4(stripped)
        or _looks_like_ipv6(stripped)
        or _looks_like_host_port(stripped)
    ):
        raise ActivityMetricsError(_GENERIC_ACTIVITY_ERROR)


def _looks_like_ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def _looks_like_ipv6(value: str) -> bool:
    if ":" not in value:
        return False
    return all(
        segment == ""
        or all(
            character in "0123456789abcdefABCDEF" for character in segment
        )
        for segment in value.split(":")
    )


def _looks_like_host_port(value: str) -> bool:
    if ":" not in value or value.count(":") != 1:
        return False
    host, port = value.rsplit(":", maxsplit=1)
    if not host or not port.isdigit():
        return False
    return "." in host or host.isalpha() or "-" in host


def _policy_cost(entries: Iterable[UsageEntry]) -> int:
    return sum(
        max(entry.estimated_cost_krw, entry.finalized_cost_krw or 0)
        for entry in entries
    )


def _require_non_negative_int(
    value: object,
    field_name: str,
    *,
    error_type: type[Exception] = ValueError,
) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise error_type(f"{field_name} must be a non-negative integer.")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ActivityMetricsError(f"{field_name} must be UTC-aware.")


def _require_utc_or_none(value: datetime | None, field_name: str) -> None:
    if value is None:
        return
    _require_utc(value, field_name)


__all__ = [
    "ADMIN_BUDGET_CEILING_KRW",
    "ActivityAction",
    "ActivityMetricsError",
    "ActivityMetricsSummary",
    "ActivityRetentionPlan",
    "CostSnapshot",
    "DailyActivityRollup",
    "TARGET_MONTHLY_BUDGET_KRW",
    "UsageLedger",
    "aggregate_daily_activity",
    "build_cost_snapshot",
    "build_usage_ledger",
    "compute_activity_metrics",
    "ingest_activity_event",
    "plan_activity_retention",
    "usage_entries_for_utc_month",
    "UsageLedgerError",
]
