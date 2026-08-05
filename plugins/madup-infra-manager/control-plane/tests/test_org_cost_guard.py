from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.services.org_cost_guard import (
    ORG_COST_GUARD_MAX_AGE,
    OrgCostGuardDenied,
    persist_org_cost_guard,
    require_current_org_cost_guard,
)
from mim_control_plane.services.quota import CostPolicyDecision

NOW = datetime(2026, 8, 5, 4, 0, 0, tzinfo=UTC)


def decision(
    *,
    emergency_stop: bool = False,
    org_policy_cost_krw: int = 0,
) -> CostPolicyDecision:
    return CostPolicyDecision(
        user_percent=0,
        warn=False,
        block_new=False,
        pause=False,
        emergency_stop=emergency_stop,
        projected_user_cost_krw=0,
        org_policy_cost_krw=org_policy_cost_krw,
        reason_codes=(
            ("org_emergency_ceiling_reached",) if emergency_stop else ()
        ),
    )


class OrgCostGuardServiceTests(unittest.TestCase):
    def test_bootstrap_stays_closed_until_usage_ingest_persists_fresh_clear_guard(
        self,
    ) -> None:
        store = MemoryStore()

        with self.assertRaises(OrgCostGuardDenied):
            require_current_org_cost_guard(store=store, now=NOW)

        created = persist_org_cost_guard(
            store=store,
            evaluated_at=NOW,
            latest_usage_collected_at=NOW - timedelta(minutes=15),
            decision=decision(org_policy_cost_krw=321),
        )

        self.assertFalse(created.emergency_stop)
        self.assertEqual(
            require_current_org_cost_guard(store=store, now=NOW),
            created,
        )

    def test_persist_reuses_identical_snapshot_and_versions_real_changes(self) -> None:
        store = MemoryStore()

        created = persist_org_cost_guard(
            store=store,
            evaluated_at=NOW,
            latest_usage_collected_at=NOW - timedelta(minutes=15),
            decision=decision(org_policy_cost_krw=321),
        )
        replayed = persist_org_cost_guard(
            store=store,
            evaluated_at=NOW,
            latest_usage_collected_at=NOW - timedelta(minutes=15),
            decision=decision(org_policy_cost_krw=321),
        )
        updated = persist_org_cost_guard(
            store=store,
            evaluated_at=NOW + timedelta(hours=1),
            latest_usage_collected_at=NOW + timedelta(minutes=30),
            decision=decision(emergency_stop=True, org_policy_cost_krw=12_345),
        )
        updated_replay = persist_org_cost_guard(
            store=store,
            evaluated_at=NOW + timedelta(hours=1),
            latest_usage_collected_at=NOW + timedelta(minutes=30),
            decision=decision(emergency_stop=True, org_policy_cost_krw=12_345),
        )

        self.assertEqual(created.version, 1)
        self.assertEqual(replayed, created)
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated_replay, updated)
        self.assertTrue(updated.emergency_stop)
        self.assertEqual(updated.org_policy_cost_krw, 12_345)

    def test_require_current_guard_rejects_stale_and_emergency(self) -> None:
        store = MemoryStore()
        persist_org_cost_guard(
            store=store,
            evaluated_at=NOW - ORG_COST_GUARD_MAX_AGE - timedelta(seconds=1),
            latest_usage_collected_at=NOW - timedelta(hours=3),
            decision=decision(org_policy_cost_krw=100),
        )
        with self.assertRaises(OrgCostGuardDenied):
            require_current_org_cost_guard(store=store, now=NOW)

        emergency_store = MemoryStore()
        persist_org_cost_guard(
            store=emergency_store,
            evaluated_at=NOW,
            latest_usage_collected_at=NOW - timedelta(minutes=5),
            decision=decision(emergency_stop=True, org_policy_cost_krw=11_000),
        )
        with self.assertRaises(OrgCostGuardDenied):
            require_current_org_cost_guard(store=emergency_store, now=NOW)

        future_store = MemoryStore()
        persist_org_cost_guard(
            store=future_store,
            evaluated_at=NOW + timedelta(seconds=1),
            latest_usage_collected_at=NOW,
            decision=decision(org_policy_cost_krw=1),
        )
        with self.assertRaises(OrgCostGuardDenied):
            require_current_org_cost_guard(store=future_store, now=NOW)

    def test_require_current_guard_accepts_exact_freshness_boundary(self) -> None:
        store = MemoryStore()
        guard = persist_org_cost_guard(
            store=store,
            evaluated_at=NOW - ORG_COST_GUARD_MAX_AGE,
            latest_usage_collected_at=NOW - ORG_COST_GUARD_MAX_AGE,
            decision=decision(org_policy_cost_krw=100),
        )

        self.assertEqual(require_current_org_cost_guard(store=store, now=NOW), guard)

    def test_persist_rejects_older_or_conflicting_same_timestamp_updates(self) -> None:
        store = MemoryStore()
        persist_org_cost_guard(
            store=store,
            evaluated_at=NOW,
            latest_usage_collected_at=NOW - timedelta(minutes=5),
            decision=decision(org_policy_cost_krw=500),
        )

        with self.assertRaisesRegex(ValueError, "monotonic"):
            persist_org_cost_guard(
                store=store,
                evaluated_at=NOW - timedelta(seconds=1),
                latest_usage_collected_at=NOW - timedelta(minutes=6),
                decision=decision(org_policy_cost_krw=400),
            )

        with self.assertRaisesRegex(ValueError, "same evaluated_at"):
            persist_org_cost_guard(
                store=store,
                evaluated_at=NOW,
                latest_usage_collected_at=NOW - timedelta(minutes=4),
                decision=decision(org_policy_cost_krw=501),
            )
