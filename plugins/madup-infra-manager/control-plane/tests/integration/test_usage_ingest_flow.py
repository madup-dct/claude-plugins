from __future__ import annotations

import hashlib
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.memory_store import MemoryStore  # noqa: E402
from mim_control_plane.domain.models import (  # noqa: E402
    ActivityEventId,
    RepositoryAdmission,
    RepositoryAdmissionId,
    UsageEntry,
    UsageEntryId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (  # noqa: E402
    RepositoryAdmissionState,
    UsageConfidence,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.workers.usage_ingest import (  # noqa: E402
    ActivityIngestRequest,
    BillingCostRecord,
    UsageIngestWorker,
)

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def user(*, user_id: str) -> User:
    return User(
        id=UserId(user_id),
        email=f"{user_id}@madup.com",
        role=UserRole.USER,
        state=UserState.ACTIVE,
        groups=frozenset({"mim-users"}),
        identity_synced_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=90),
        updated_at=NOW - timedelta(days=1),
        version=1,
    )


def admission() -> RepositoryAdmission:
    return RepositoryAdmission(
        id=RepositoryAdmissionId("repo-1"),
        repository_numeric_id=42,
        owner="madupmarketing",
        name="sample-app",
        installation_id=9,
        state=RepositoryAdmissionState.ADMITTED,
        admitted_sha="a" * 40,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=1),
        version=1,
    )


def workload(*, workload_id: str, owner_id: str) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name=workload_id,
        kind=WorkloadKind.NEXTJS,
        state=WorkloadState.ACTIVE,
        source_sha="b" * 40,
        desired_manifest_hash="manifest-hash",
        created_at=NOW - timedelta(days=60),
        updated_at=NOW - timedelta(days=1),
        last_activity_at=NOW - timedelta(days=1),
        last_healthy_image_digest="sha256:" + "3" * 64,
        version=1,
    )


def labels_for(
    *,
    owner_id: str | None,
    workload_id: str | None,
) -> tuple[tuple[str, str], ...]:
    labels: list[tuple[str, str]] = [("managed-by", "mim-control-plane")]
    if owner_id is not None:
        labels.append(("owner-hash", _stable_hash(owner_id)))
    if workload_id is not None:
        labels.append(("workload-hash", _stable_hash(workload_id)))
    return tuple(labels)


@dataclass(frozen=True, slots=True)
class FakeBillingSource:
    rows: tuple[BillingCostRecord, ...]

    def fetch_cost_records(
        self,
        *,
        now: datetime,
    ) -> tuple[BillingCostRecord, ...]:
        del now
        return self.rows


class RecordingCostEnforcer:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.user_calls: dict[str, tuple[int, tuple[str, ...]]] = {}
        self.org_calls: dict[str, tuple[int, tuple[str, ...]]] = {}

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
    ) -> None:
        current_ids = tuple(
            str(entry.id)
            for entry in self.store.list_usage_entries(owner_id=user_id)
        )
        self.assertEqual if False else None
        expected = tuple(sorted(current_ids))
        actual = tuple(sorted(basis_entry_ids))
        if actual != expected:
            raise AssertionError("user enforcement did not use fresh ledger state")
        severity = (
            100 if pause else 90 if block_new else 70 if warn else 0
        )
        self.user_calls[idempotency_key] = (severity, actual)

    def enforce_org_policy(
        self,
        *,
        emergency_stop: bool,
        basis_entry_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> None:
        current_ids = tuple(str(entry.id) for entry in self.store.list_usage_entries())
        if tuple(sorted(basis_entry_ids)) != tuple(sorted(current_ids)):
            raise AssertionError("org enforcement did not use fresh ledger state")
        if emergency_stop:
            self.org_calls[idempotency_key] = (10000, tuple(sorted(basis_entry_ids)))


class RetainingMemoryStore(MemoryStore):
    def expire_activity_events(self, *, event_ids: tuple[str, ...]) -> tuple[str, ...]:
        removed: list[str] = []
        for event_id in event_ids:
            normalized = ActivityEventId(event_id)
            if normalized in self._activity_events:
                del self._activity_events[normalized]
                removed.append(event_id)
        return tuple(removed)

    def upsert_usage_entry_monotonic(
        self,
        *,
        current: UsageEntry,
        updated: UsageEntry,
    ) -> UsageEntry:
        self._usage_entries[current.id] = updated
        return updated


class UsageIngestFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RetainingMemoryStore()
        self.store.create_user(user(user_id="usr-1"))
        self.store.create_user(user(user_id="usr-2"))
        self.store.create_user(user(user_id="usr-3"))
        self.store.create_repository_admission(admission())
        self.store.create_workload(workload(workload_id="wrk-1", owner_id="usr-1"))
        self.store.create_workload(workload(workload_id="wrk-2", owner_id="usr-2"))
        self.store.create_workload(workload(workload_id="wrk-3", owner_id="usr-3"))

    def test_billing_ingestion_enforces_70_90_100_and_org_emergency_thresholds(
        self,
    ) -> None:
        enforcer = RecordingCostEnforcer(self.store)
        worker = UsageIngestWorker(
            store=self.store,
            billing=FakeBillingSource(
                rows=(
                    BillingCostRecord(
                        entry_id="bill-user-70",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-1"),
                        owner_id=UserId("usr-1"),
                        service_category="cloud_run",
                        estimated_cost_krw=700,
                        finalized_cost_krw=None,
                        confidence=UsageConfidence.ESTIMATED,
                        collected_at=NOW,
                        labels=labels_for(owner_id="usr-1", workload_id="wrk-1"),
                    ),
                    BillingCostRecord(
                        entry_id="bill-user-90",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-2"),
                        owner_id=UserId("usr-2"),
                        service_category="cloud_run",
                        estimated_cost_krw=900,
                        finalized_cost_krw=900,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW,
                        labels=labels_for(owner_id="usr-2", workload_id="wrk-2"),
                    ),
                    BillingCostRecord(
                        entry_id="bill-user-100",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-3"),
                        owner_id=UserId("usr-3"),
                        service_category="cloud_run",
                        estimated_cost_krw=1000,
                        finalized_cost_krw=1000,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW,
                        labels=labels_for(owner_id="usr-3", workload_id="wrk-3"),
                    ),
                    BillingCostRecord(
                        entry_id="bill-platform",
                        project_id="mim-prod-123456",
                        workload_id=None,
                        owner_id=None,
                        service_category="control_plane",
                        estimated_cost_krw=9_100,
                        finalized_cost_krw=9_100,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW,
                        labels=labels_for(owner_id=None, workload_id=None),
                    ),
                    BillingCostRecord(
                        entry_id="bill-ignore",
                        project_id="internal-sensitive-prod",
                        workload_id=None,
                        owner_id=None,
                        service_category="bigquery",
                        estimated_cost_krw=999_999,
                        finalized_cost_krw=999_999,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW,
                        labels=labels_for(owner_id=None, workload_id=None),
                    ),
                )
            ),
            retention=self.store,
            enforcer=enforcer,
        )

        result = worker.ingest_billing(now=NOW)
        retry = worker.ingest_billing(now=NOW + timedelta(minutes=1))

        self.assertEqual(result.ignored_entry_ids, ("bill-ignore",))
        self.assertEqual(result.user_decisions[UserId("usr-1")].user_percent, 70)
        self.assertEqual(result.user_decisions[UserId("usr-2")].user_percent, 90)
        self.assertEqual(result.user_decisions[UserId("usr-3")].user_percent, 100)
        self.assertTrue(result.user_decisions[UserId("usr-3")].pause)
        self.assertTrue(result.organization_decision.emergency_stop)
        self.assertEqual(len(self.store.list_usage_entries()), 4)
        self.assertEqual(len(enforcer.user_calls), 3)
        self.assertEqual(len(enforcer.org_calls), 1)
        first_guard = self.store.get_org_cost_guard()
        self.assertTrue(first_guard.emergency_stop)
        self.assertEqual(first_guard.org_policy_cost_krw, 11_700)
        self.assertEqual(first_guard.latest_usage_collected_at, NOW)
        self.assertEqual(retry.appended_entry_ids, ())
        self.assertEqual(len(enforcer.user_calls), 3)
        self.assertEqual(len(enforcer.org_calls), 1)
        replay_guard = self.store.get_org_cost_guard()
        self.assertTrue(replay_guard.emergency_stop)
        self.assertEqual(replay_guard.latest_usage_collected_at, NOW)
        self.assertEqual(replay_guard.evaluated_at, NOW + timedelta(minutes=1))

    def test_delayed_finalization_updates_entry_monotonically_and_rejects_decrease(
        self,
    ) -> None:
        worker = UsageIngestWorker(
            store=self.store,
            billing=FakeBillingSource(
                rows=(
                    BillingCostRecord(
                        entry_id="bill-finalize",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-1"),
                        owner_id=UserId("usr-1"),
                        service_category="cloud_run",
                        estimated_cost_krw=500,
                        finalized_cost_krw=None,
                        confidence=UsageConfidence.ESTIMATED,
                        collected_at=NOW,
                        labels=labels_for(owner_id="usr-1", workload_id="wrk-1"),
                    ),
                )
            ),
            retention=self.store,
            enforcer=RecordingCostEnforcer(self.store),
        )
        worker.ingest_billing(now=NOW)
        worker = UsageIngestWorker(
            store=self.store,
            billing=FakeBillingSource(
                rows=(
                    BillingCostRecord(
                        entry_id="bill-finalize",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-1"),
                        owner_id=UserId("usr-1"),
                        service_category="cloud_run",
                        estimated_cost_krw=500,
                        finalized_cost_krw=700,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW + timedelta(hours=1),
                        labels=labels_for(owner_id="usr-1", workload_id="wrk-1"),
                    ),
                )
            ),
            retention=self.store,
            enforcer=RecordingCostEnforcer(self.store),
        )

        finalized = worker.ingest_billing(now=NOW + timedelta(hours=1))
        retry = worker.ingest_billing(now=NOW + timedelta(hours=2))

        entry = self.store.list_usage_entries(owner_id=UserId("usr-1"))[0]
        self.assertEqual(finalized.updated_entry_ids, ("bill-finalize",))
        self.assertEqual(entry.finalized_cost_krw, 700)
        self.assertEqual(entry.confidence, UsageConfidence.FINALIZED)
        self.assertEqual(retry.updated_entry_ids, ())
        self.assertEqual(
            len(self.store.list_usage_entries(owner_id=UserId("usr-1"))),
            1,
        )

        worker = UsageIngestWorker(
            store=self.store,
            billing=FakeBillingSource(
                rows=(
                    BillingCostRecord(
                        entry_id="bill-finalize",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-1"),
                        owner_id=UserId("usr-1"),
                        service_category="cloud_run",
                        estimated_cost_krw=400,
                        finalized_cost_krw=400,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW + timedelta(hours=3),
                        labels=labels_for(owner_id="usr-1", workload_id="wrk-1"),
                    ),
                )
            ),
            retention=self.store,
            enforcer=RecordingCostEnforcer(self.store),
        )
        with self.assertRaises(ValueError):
            worker.ingest_billing(now=NOW + timedelta(hours=3))

    def test_invalid_or_duplicate_label_bindings_are_ignored(self) -> None:
        worker = UsageIngestWorker(
            store=self.store,
            billing=FakeBillingSource(
                rows=(
                    BillingCostRecord(
                        entry_id="good",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-1"),
                        owner_id=UserId("usr-1"),
                        service_category="cloud_run",
                        estimated_cost_krw=100,
                        finalized_cost_krw=100,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW,
                        labels=labels_for(owner_id="usr-1", workload_id="wrk-1"),
                    ),
                    BillingCostRecord(
                        entry_id="dup-owner",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-1"),
                        owner_id=UserId("usr-1"),
                        service_category="cloud_run",
                        estimated_cost_krw=100,
                        finalized_cost_krw=100,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW,
                        labels=(
                            ("managed-by", "mim-control-plane"),
                            ("owner-hash", _stable_hash("usr-1")),
                            ("owner-hash", _stable_hash("usr-1")),
                            ("workload-hash", _stable_hash("wrk-1")),
                        ),
                    ),
                    BillingCostRecord(
                        entry_id="mismatch",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-2"),
                        owner_id=UserId("usr-2"),
                        service_category="cloud_run",
                        estimated_cost_krw=100,
                        finalized_cost_krw=100,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW,
                        labels=labels_for(owner_id="usr-9", workload_id="wrk-2"),
                    ),
                )
            ),
            retention=self.store,
            enforcer=RecordingCostEnforcer(self.store),
        )

        result = worker.ingest_billing(now=NOW)

        self.assertEqual(
            result.ignored_entry_ids,
            ("dup-owner", "mismatch"),
        )
        self.assertEqual(len(self.store.list_usage_entries()), 1)

    def test_previous_month_usage_entries_do_not_trigger_current_month_policy(
        self,
    ) -> None:
        previous_month = BillingCostRecord(
            entry_id="bill-july",
            project_id="mim-prod-123456",
            workload_id=WorkloadId("wrk-1"),
            owner_id=UserId("usr-1"),
            service_category="cloud_run",
            estimated_cost_krw=9_900,
            finalized_cost_krw=9_900,
            confidence=UsageConfidence.FINALIZED,
            collected_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
            labels=labels_for(owner_id="usr-1", workload_id="wrk-1"),
        )
        self.store.append_usage_entry(
            UsageEntry(
                id=UsageEntryId(previous_month.entry_id),
                owner_id=previous_month.owner_id,
                workload_id=previous_month.workload_id,
                service_category=previous_month.service_category,
                estimated_cost_krw=previous_month.estimated_cost_krw,
                finalized_cost_krw=previous_month.finalized_cost_krw,
                confidence=previous_month.confidence,
                collected_at=previous_month.collected_at,
            )
        )
        enforcer = RecordingCostEnforcer(self.store)
        worker = UsageIngestWorker(
            store=self.store,
            billing=FakeBillingSource(
                rows=(
                    BillingCostRecord(
                        entry_id="bill-august",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-1"),
                        owner_id=UserId("usr-1"),
                        service_category="cloud_run",
                        estimated_cost_krw=100,
                        finalized_cost_krw=100,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW,
                        labels=labels_for(owner_id="usr-1", workload_id="wrk-1"),
                    ),
                )
            ),
            retention=self.store,
            enforcer=enforcer,
        )

        result = worker.ingest_billing(now=NOW)

        self.assertFalse(result.user_decisions[UserId("usr-1")].warn)
        self.assertFalse(result.organization_decision.emergency_stop)
        self.assertEqual(enforcer.user_calls, {})
        self.assertEqual(enforcer.org_calls, {})
        guard = self.store.get_org_cost_guard()
        self.assertFalse(guard.emergency_stop)
        self.assertEqual(guard.org_policy_cost_krw, 100)
        self.assertEqual(guard.latest_usage_collected_at, NOW)

    def test_emergency_guard_persists_before_org_enforcement_side_effects(self) -> None:
        class ExplodingEnforcer(RecordingCostEnforcer):
            def enforce_org_policy(
                self,
                *,
                emergency_stop: bool,
                basis_entry_ids: tuple[str, ...],
                idempotency_key: str,
            ) -> None:
                super().enforce_org_policy(
                    emergency_stop=emergency_stop,
                    basis_entry_ids=basis_entry_ids,
                    idempotency_key=idempotency_key,
                )
                raise RuntimeError("org enforcement failed")

        worker = UsageIngestWorker(
            store=self.store,
            billing=FakeBillingSource(
                rows=(
                    BillingCostRecord(
                        entry_id="bill-emergency",
                        project_id="mim-prod-123456",
                        workload_id=None,
                        owner_id=None,
                        service_category="control_plane",
                        estimated_cost_krw=11_000,
                        finalized_cost_krw=11_000,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW,
                        labels=labels_for(owner_id=None, workload_id=None),
                    ),
                )
            ),
            retention=self.store,
            enforcer=ExplodingEnforcer(self.store),
        )

        with self.assertRaisesRegex(RuntimeError, "org enforcement failed"):
            worker.ingest_billing(now=NOW)

        guard = self.store.get_org_cost_guard()
        self.assertTrue(guard.emergency_stop)
        self.assertEqual(guard.org_policy_cost_krw, 11_000)
        self.assertEqual(guard.latest_usage_collected_at, NOW)

    def test_emergency_guard_persists_before_user_enforcement_side_effects(
        self,
    ) -> None:
        class ExplodingUserEnforcer(RecordingCostEnforcer):
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
            ) -> None:
                super().enforce_user_policy(
                    user_id=user_id,
                    user_percent=user_percent,
                    warn=warn,
                    block_new=block_new,
                    pause=pause,
                    basis_entry_ids=basis_entry_ids,
                    idempotency_key=idempotency_key,
                )
                raise RuntimeError("user enforcement failed")

        worker = UsageIngestWorker(
            store=self.store,
            billing=FakeBillingSource(
                rows=(
                    BillingCostRecord(
                        entry_id="bill-user-emergency",
                        project_id="mim-prod-123456",
                        workload_id=WorkloadId("wrk-1"),
                        owner_id=UserId("usr-1"),
                        service_category="cloud_run",
                        estimated_cost_krw=11_000,
                        finalized_cost_krw=11_000,
                        confidence=UsageConfidence.FINALIZED,
                        collected_at=NOW,
                        labels=labels_for(
                            owner_id="usr-1",
                            workload_id="wrk-1",
                        ),
                    ),
                )
            ),
            retention=self.store,
            enforcer=ExplodingUserEnforcer(self.store),
        )

        with self.assertRaisesRegex(RuntimeError, "user enforcement failed"):
            worker.ingest_billing(now=NOW)

        guard = self.store.get_org_cost_guard()
        self.assertTrue(guard.emergency_stop)
        self.assertEqual(guard.org_policy_cost_krw, 11_000)
        self.assertEqual(guard.latest_usage_collected_at, NOW)

    def test_activity_ingestion_rolls_up_every_affected_day_and_expires_old_events(
        self,
    ) -> None:
        worker = UsageIngestWorker(
            store=self.store,
            billing=FakeBillingSource(rows=()),
            retention=self.store,
            enforcer=RecordingCostEnforcer(self.store),
        )

        activity_result = worker.ingest_activity(
            requests=(
                ActivityIngestRequest(
                    event_id="act-day4",
                    trusted_user_id=UserId("usr-1"),
                    trusted_correlation_id="corr-1",
                    trusted_occurred_at=NOW,
                    observed_at=NOW,
                    payload={
                        "surface": "dashboard",
                        "action": "view_dashboard",
                        "target_ref": "wrk-1",
                        "outcome": "succeeded",
                        "latency_ms": 120,
                    },
                ),
                ActivityIngestRequest(
                    event_id="act-day3",
                    trusted_user_id=UserId("usr-2"),
                    trusted_correlation_id="corr-2",
                    trusted_occurred_at=NOW - timedelta(days=1),
                    observed_at=NOW,
                    payload={
                        "surface": "mcp",
                        "action": "plan_deploy",
                        "target_ref": "wrk-2",
                        "outcome": "succeeded",
                        "latency_ms": 200,
                    },
                ),
                ActivityIngestRequest(
                    event_id="act-old",
                    trusted_user_id=UserId("usr-3"),
                    trusted_correlation_id="corr-3",
                    trusted_occurred_at=NOW - timedelta(days=31),
                    observed_at=NOW,
                    payload={
                        "surface": "dashboard",
                        "action": "view_dashboard",
                        "target_ref": "wrk-3",
                        "outcome": "succeeded",
                        "latency_ms": 300,
                    },
                ),
            ),
            now=NOW,
        )

        self.assertEqual(activity_result.expired_event_ids, ("act-old",))
        self.assertEqual(len(self.store.list_activity_events()), 2)
        august_4 = activity_result.organization_rollups[date(2026, 8, 4)]
        august_3 = activity_result.organization_rollups[date(2026, 8, 3)]
        july_4 = activity_result.organization_rollups[date(2026, 7, 4)]
        self.assertEqual(august_4.dashboard_visits, 1)
        self.assertEqual(august_3.mcp_actions, 1)
        self.assertEqual(july_4.dashboard_visits, 0)
        self.assertEqual(
            activity_result.user_rollups[date(2026, 8, 4)][
                UserId("usr-1")
            ].dashboard_visits,
            1,
        )
        self.assertEqual(
            self.store.get_daily_usage_aggregate(
                date(2026, 8, 3),
                UserId("usr-2"),
            ).mcp_actions,
            1,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
