"""Private usage ingestion worker for MIM-only billing and activity data."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Mapping, Protocol

from mim_control_plane.domain.models import (
    ActivityEvent,
    DailyUsageAggregate,
    OrgCostGuard,
    UsageEntry,
    UsageEntryId,
    UserId,
    WorkloadId,
)
from mim_control_plane.domain.states import UsageConfidence
from mim_control_plane.services.org_cost_guard import persist_org_cost_guard
from mim_control_plane.services.quota import CostPolicyDecision, evaluate_cost_policy
from mim_control_plane.services.schedules import require_utc_datetime
from mim_control_plane.services.usage import (
    aggregate_daily_activity,
    build_cost_snapshot,
    build_usage_ledger,
    ingest_activity_event,
    plan_activity_retention,
    usage_entries_for_utc_month,
)

_MIM_PROJECT_ID = "mim-prod-123456"
_MIM_MANAGED_BY = "mim-control-plane"
_CONFIDENCE_RANK = {
    UsageConfidence.ESTIMATED: 1,
    UsageConfidence.MEASURED: 2,
    UsageConfidence.FINALIZED: 3,
}


class BillingSource(Protocol):
    def fetch_cost_records(
        self,
        *,
        now: datetime,
    ) -> tuple["BillingCostRecord", ...]: ...


class UsageIngestionStore(Protocol):
    def append_usage_entry(self, entry: UsageEntry) -> UsageEntry: ...

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

    def upsert_usage_entry_monotonic(
        self,
        *,
        current: UsageEntry,
        updated: UsageEntry,
    ) -> UsageEntry: ...

    def append_activity_event(self, event: ActivityEvent) -> ActivityEvent: ...

    def list_activity_events(
        self,
        *,
        user_id: UserId | None = None,
    ) -> tuple[ActivityEvent, ...]: ...

    def create_daily_usage_aggregate(
        self,
        aggregate: DailyUsageAggregate,
    ) -> DailyUsageAggregate: ...

    def get_daily_usage_aggregate(
        self,
        day: date,
        user_id: UserId | None,
    ) -> DailyUsageAggregate: ...

    def save_daily_usage_aggregate(
        self,
        aggregate: DailyUsageAggregate,
        *,
        expected_version: int,
    ) -> DailyUsageAggregate: ...

    def expire_activity_events(
        self,
        *,
        event_ids: tuple[str, ...],
    ) -> tuple[str, ...]: ...


class CostEnforcer(Protocol):
    def enforce_user_policy(
        self,
        *,
        user_id: UserId,
        user_percent: int,
        warn: bool,
        block_new: bool,
        pause: bool,
        basis_entry_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> None: ...

    def enforce_org_policy(
        self,
        *,
        emergency_stop: bool,
        basis_entry_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class BillingCostRecord:
    entry_id: str
    project_id: str
    workload_id: WorkloadId | None
    owner_id: UserId | None
    service_category: str
    estimated_cost_krw: int
    finalized_cost_krw: int | None
    confidence: UsageConfidence
    collected_at: datetime
    labels: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id.strip():
            raise ValueError("entry_id must be a non-empty string.")
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValueError("project_id must be a non-empty string.")
        if (
            not isinstance(self.service_category, str)
            or not self.service_category.strip()
        ):
            raise ValueError("service_category must be a non-empty string.")
        if not isinstance(self.estimated_cost_krw, int) or self.estimated_cost_krw < 0:
            raise ValueError("estimated_cost_krw must be a non-negative integer.")
        if self.finalized_cost_krw is not None and (
            not isinstance(self.finalized_cost_krw, int) or self.finalized_cost_krw < 0
        ):
            raise ValueError("finalized_cost_krw must be a non-negative integer.")
        require_utc_datetime(self.collected_at, label="billing record")
        if type(self.labels) is not tuple:
            raise ValueError("labels must be immutable.")
        for item in self.labels:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("labels must contain exact key/value tuples.")


@dataclass(frozen=True, slots=True)
class ActivityIngestRequest:
    event_id: str
    trusted_user_id: UserId
    trusted_correlation_id: str
    trusted_occurred_at: datetime
    observed_at: datetime
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class BillingIngestResult:
    appended_entry_ids: tuple[str, ...]
    updated_entry_ids: tuple[str, ...]
    ignored_entry_ids: tuple[str, ...]
    user_decisions: Mapping[UserId, CostPolicyDecision]
    organization_decision: CostPolicyDecision


@dataclass(frozen=True, slots=True)
class ActivityIngestResult:
    appended_event_ids: tuple[str, ...]
    expired_event_ids: tuple[str, ...]
    organization_rollups: Mapping[date, DailyUsageAggregate]
    user_rollups: Mapping[date, Mapping[UserId, DailyUsageAggregate]]


@dataclass(frozen=True, slots=True)
class UsageIngestWorker:
    store: UsageIngestionStore
    billing: BillingSource
    retention: UsageIngestionStore
    enforcer: CostEnforcer

    def ingest_billing(self, *, now: datetime) -> BillingIngestResult:
        current_now = require_utc_datetime(now, label="usage ingest")
        appended: list[str] = []
        updated: list[str] = []
        ignored: list[str] = []
        existing_by_id = {
            str(entry.id): entry for entry in self.store.list_usage_entries()
        }
        for record in self.billing.fetch_cost_records(now=current_now):
            if not self._is_mim_record(record):
                ignored.append(record.entry_id)
                continue
            incoming = UsageEntry(
                id=UsageEntryId(record.entry_id),
                owner_id=record.owner_id,
                workload_id=record.workload_id,
                service_category=record.service_category,
                estimated_cost_krw=record.estimated_cost_krw,
                finalized_cost_krw=record.finalized_cost_krw,
                confidence=record.confidence,
                collected_at=record.collected_at,
            )
            current = existing_by_id.get(record.entry_id)
            if current is None:
                self.store.append_usage_entry(incoming)
                existing_by_id[record.entry_id] = incoming
                appended.append(record.entry_id)
                continue
            merged = self._merge_usage_entry(current=current, incoming=incoming)
            if merged == current:
                continue
            self.store.upsert_usage_entry_monotonic(current=current, updated=merged)
            existing_by_id[record.entry_id] = merged
            updated.append(record.entry_id)

        user_ids = tuple(
            sorted(
                {
                    entry.owner_id
                    for entry in self.store.list_usage_entries()
                    if entry.owner_id is not None
                },
                key=str,
            )
        )
        # Persist the organization-wide guard before any external per-user
        # enforcement effect can fail. Hot paths must never keep trusting an
        # older clear guard after this billing snapshot has been stored.
        organization_decision = self._enforce_org_policy(now=current_now)
        user_decisions = {
            user_id: self._enforce_user_policy(user_id=user_id, now=current_now)
            for user_id in user_ids
        }
        return BillingIngestResult(
            appended_entry_ids=tuple(appended),
            updated_entry_ids=tuple(updated),
            ignored_entry_ids=tuple(ignored),
            user_decisions=MappingProxyType(user_decisions),
            organization_decision=organization_decision,
        )

    def ingest_activity(
        self,
        *,
        requests: tuple[ActivityIngestRequest, ...],
        now: datetime,
    ) -> ActivityIngestResult:
        current_now = require_utc_datetime(now, label="activity ingest")
        appended: list[str] = []
        affected_days: set[date] = set()
        existing_ids = {
            str(event.id) for event in self.store.list_activity_events()
        }
        for request in requests:
            event = ingest_activity_event(
                event_id=request.event_id,
                trusted_user_id=request.trusted_user_id,
                trusted_correlation_id=request.trusted_correlation_id,
                trusted_occurred_at=request.trusted_occurred_at,
                observed_at=request.observed_at,
                payload=request.payload,
            )
            if str(event.id) not in existing_ids:
                self.store.append_activity_event(event)
                existing_ids.add(str(event.id))
                appended.append(str(event.id))
            affected_days.add(event.occurred_at.date())

        retention_plan = plan_activity_retention(
            self.store.list_activity_events(),
            now=current_now,
        )
        expired = self.retention.expire_activity_events(
            event_ids=tuple(
                str(event_id) for event_id in retention_plan.expired_event_ids
            )
        )
        organization_rollups: dict[date, DailyUsageAggregate] = {}
        user_rollups: dict[date, Mapping[UserId, DailyUsageAggregate]] = {}
        for day in sorted(affected_days):
            rollup = aggregate_daily_activity(
                self.store.list_activity_events(),
                day=day,
                now=current_now,
            )
            organization_rollups[day] = self._upsert_aggregate(rollup.organization)
            user_rollups[day] = MappingProxyType(
                {
                    user_id: self._upsert_aggregate(aggregate)
                    for user_id, aggregate in rollup.by_user.items()
                }
            )
        return ActivityIngestResult(
            appended_event_ids=tuple(appended),
            expired_event_ids=expired,
            organization_rollups=MappingProxyType(organization_rollups),
            user_rollups=MappingProxyType(user_rollups),
        )

    def rollup_persisted_activity(
        self,
        *,
        now: datetime,
    ) -> ActivityIngestResult:
        current_now = require_utc_datetime(now, label="activity rollup")
        current_events = self.store.list_activity_events()
        retention_plan = plan_activity_retention(
            current_events,
            now=current_now,
        )
        expired_event_ids = set(retention_plan.expired_event_ids)
        touched_users_by_day: dict[date, set[UserId]] = {}
        touched_days = {
            event.occurred_at.date()
            for event in current_events
            if event.id in expired_event_ids
        }
        for event in current_events:
            if event.id not in expired_event_ids:
                continue
            touched_users_by_day.setdefault(event.occurred_at.date(), set()).add(
                event.user_id
            )
        expired = self.retention.expire_activity_events(
            event_ids=tuple(
                str(event_id) for event_id in retention_plan.expired_event_ids
            )
        )
        retained_events = self.store.list_activity_events()
        organization_rollups: dict[date, DailyUsageAggregate] = {}
        user_rollups: dict[date, Mapping[UserId, DailyUsageAggregate]] = {}
        retained_days = {event.occurred_at.date() for event in retained_events}
        for day in sorted(retained_days | touched_days):
            rollup = aggregate_daily_activity(
                retained_events,
                day=day,
                now=current_now,
            )
            organization = rollup.organization
            if day in touched_days and day not in retained_days:
                organization = self._zero_aggregate(
                    day=day,
                    user_id=None,
                    updated_at=current_now,
                )
            organization_rollups[day] = self._upsert_aggregate(organization)
            day_user_rollups = {
                user_id: self._upsert_aggregate(aggregate)
                for user_id, aggregate in rollup.by_user.items()
            }
            for user_id in sorted(touched_users_by_day.get(day, ()), key=str):
                if user_id in day_user_rollups:
                    continue
                day_user_rollups[user_id] = self._upsert_aggregate(
                    self._zero_aggregate(
                        day=day,
                        user_id=user_id,
                        updated_at=current_now,
                    )
                )
            user_rollups[day] = MappingProxyType(day_user_rollups)
        return ActivityIngestResult(
            appended_event_ids=(),
            expired_event_ids=expired,
            organization_rollups=MappingProxyType(organization_rollups),
            user_rollups=MappingProxyType(user_rollups),
        )

    def _is_mim_record(self, record: BillingCostRecord) -> bool:
        if record.project_id != _MIM_PROJECT_ID:
            return False
        label_map = self._label_map(record.labels)
        if label_map is None:
            return False
        if label_map.get("managed-by") != _MIM_MANAGED_BY:
            return False
        direct = record.owner_id is not None or record.workload_id is not None
        if direct:
            if record.owner_id is None or record.workload_id is None:
                return False
            return (
                label_map.get("owner-hash") == self._stable_hash(str(record.owner_id))
                and label_map.get("workload-hash")
                == self._stable_hash(str(record.workload_id))
            )
        return (
            "owner-hash" not in label_map
            and "workload-hash" not in label_map
        )

    def _merge_usage_entry(
        self,
        *,
        current: UsageEntry,
        incoming: UsageEntry,
    ) -> UsageEntry:
        if any(
            getattr(current, field_name) != getattr(incoming, field_name)
            for field_name in ("id", "owner_id", "workload_id", "service_category")
        ):
            raise ValueError("usage entry material conflicts.")
        if incoming.estimated_cost_krw < current.estimated_cost_krw:
            raise ValueError("usage entry estimates must not decrease.")
        if (
            current.finalized_cost_krw is not None
            and incoming.finalized_cost_krw is not None
            and incoming.finalized_cost_krw < current.finalized_cost_krw
        ):
            raise ValueError("usage entry finalized cost must not decrease.")
        merged_finalized = current.finalized_cost_krw
        if incoming.finalized_cost_krw is not None:
            merged_finalized = incoming.finalized_cost_krw
        merged = UsageEntry(
            id=current.id,
            owner_id=current.owner_id,
            workload_id=current.workload_id,
            service_category=current.service_category,
            estimated_cost_krw=max(
                current.estimated_cost_krw,
                incoming.estimated_cost_krw,
            ),
            finalized_cost_krw=merged_finalized,
            confidence=max(
                (current.confidence, incoming.confidence),
                key=lambda item: _CONFIDENCE_RANK[item],
            ),
            collected_at=max(current.collected_at, incoming.collected_at),
        )
        return current if merged == current else merged

    def _enforce_user_policy(
        self,
        *,
        user_id: UserId,
        now: datetime,
    ) -> CostPolicyDecision:
        current_entries = usage_entries_for_utc_month(
            self.store.list_usage_entries(owner_id=user_id),
            now=now,
        )
        current_ledger = build_usage_ledger(current_entries)
        decision = evaluate_cost_policy(
            snapshot=build_cost_snapshot(current_ledger, user_id=user_id)
        )
        if decision.warn or decision.block_new or decision.pause:
            basis_ids = tuple(str(entry.id) for entry in current_entries)
            self.enforcer.enforce_user_policy(
                user_id=user_id,
                user_percent=decision.user_percent,
                warn=decision.warn,
                block_new=decision.block_new,
                pause=decision.pause,
                basis_entry_ids=basis_ids,
                idempotency_key=(
                    f"user:{user_id}:{decision.user_percent}:"
                    f"{decision.projected_user_cost_krw}"
                ),
            )
        return decision

    def _enforce_org_policy(self, *, now: datetime) -> CostPolicyDecision:
        current_entries = usage_entries_for_utc_month(
            self.store.list_usage_entries(),
            now=now,
        )
        decision = evaluate_cost_policy(
            snapshot=build_cost_snapshot(
                build_usage_ledger(current_entries),
                user_id=UserId("org-platform"),
            )
        )
        persist_org_cost_guard(
            store=self.store,
            evaluated_at=now,
            latest_usage_collected_at=max(
                (entry.collected_at for entry in current_entries),
                default=None,
            ),
            decision=decision,
        )
        if decision.emergency_stop:
            self.enforcer.enforce_org_policy(
                emergency_stop=True,
                basis_entry_ids=tuple(str(entry.id) for entry in current_entries),
                idempotency_key=f"org:{decision.org_policy_cost_krw}",
            )
        return decision

    def _upsert_aggregate(self, aggregate: DailyUsageAggregate) -> DailyUsageAggregate:
        try:
            return self.store.create_daily_usage_aggregate(aggregate)
        except Exception:
            current = self.store.get_daily_usage_aggregate(
                aggregate.day,
                aggregate.user_id,
            )
            updated = DailyUsageAggregate(
                day=aggregate.day,
                user_id=aggregate.user_id,
                active_users=aggregate.active_users,
                dashboard_visits=aggregate.dashboard_visits,
                mcp_actions=aggregate.mcp_actions,
                deployments=aggregate.deployments,
                schedule_executions=aggregate.schedule_executions,
                successes=aggregate.successes,
                failures=aggregate.failures,
                policy_denials=aggregate.policy_denials,
                version=current.version + 1,
                updated_at=aggregate.updated_at,
            )
            return self.store.save_daily_usage_aggregate(
                updated,
                expected_version=current.version,
            )

    @staticmethod
    def _zero_aggregate(
        *,
        day: date,
        user_id: UserId | None,
        updated_at: datetime,
    ) -> DailyUsageAggregate:
        return DailyUsageAggregate(
            day=day,
            user_id=user_id,
            active_users=0,
            dashboard_visits=0,
            mcp_actions=0,
            deployments=0,
            schedule_executions=0,
            successes=0,
            failures=0,
            policy_denials=0,
            version=1,
            updated_at=require_utc_datetime(updated_at, label="zero aggregate"),
        )

    @staticmethod
    def _label_map(
        labels: tuple[tuple[str, str], ...],
    ) -> dict[str, str] | None:
        mapping: dict[str, str] = {}
        for key, value in labels:
            if key in mapping:
                return None
            mapping[key] = value
        return mapping

    @staticmethod
    def _stable_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


__all__ = [
    "ActivityIngestRequest",
    "ActivityIngestResult",
    "BillingCostRecord",
    "BillingIngestResult",
    "UsageIngestionStore",
    "UsageIngestWorker",
]
