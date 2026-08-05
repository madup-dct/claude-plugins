from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime
from typing import Any, cast

from mim_control_plane.config import (
    ADMIN_BUDGET_CEILING_KRW,
    DEFAULT_SECRET_LIMIT,
    HARD_SECRET_LIMIT,
    PER_USER_SCHEDULE_LIMIT,
    PER_USER_SERVICE_LIMIT,
    TARGET_MONTHLY_BUDGET_KRW,
)
from mim_control_plane.domain.models import UsageEntry, UsageEntryId, UserId, WorkloadId
from mim_control_plane.domain.states import UsageConfidence
from mim_control_plane.services.quota import (
    QuotaPolicyError,
    ResourceInventory,
    evaluate_cost_policy,
    evaluate_resource_policy,
)
from mim_control_plane.services.usage import (
    CostSnapshot,
    UsageLedgerError,
    build_cost_snapshot,
    build_usage_ledger,
    usage_entries_for_utc_month,
)

NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
USER = UserId("usr-1")
OTHER_USER = UserId("usr-2")


def usage_entry(
    *,
    entry_id: str,
    owner_id: UserId | None,
    estimated_cost_krw: int,
    finalized_cost_krw: int | None = None,
    confidence: UsageConfidence = UsageConfidence.ESTIMATED,
    workload_id: str | None = "wrk-1",
    collected_at: datetime = NOW,
) -> UsageEntry:
    return UsageEntry(
        id=UsageEntryId(entry_id),
        owner_id=owner_id,
        workload_id=None if workload_id is None else WorkloadId(workload_id),
        service_category="cloud_run",
        estimated_cost_krw=estimated_cost_krw,
        finalized_cost_krw=finalized_cost_krw,
        confidence=confidence,
        collected_at=collected_at,
    )


def ledger(*entries: UsageEntry):
    return build_usage_ledger(entries)


class UsageCostPolicyTests(unittest.TestCase):
    def test_usage_entries_for_utc_month_filters_by_explicit_utc_calendar_month(
        self,
    ) -> None:
        current = usage_entry(
            entry_id="use-current",
            owner_id=USER,
            estimated_cost_krw=100,
            collected_at=NOW,
        )
        previous = usage_entry(
            entry_id="use-previous",
            owner_id=USER,
            estimated_cost_krw=900,
            collected_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
        )

        filtered = usage_entries_for_utc_month((current, previous), now=NOW)

        self.assertEqual(filtered, (current,))

    def test_duplicate_usage_entry_ids_are_rejected(self) -> None:
        duplicate = usage_entry(
            entry_id="use-1",
            owner_id=USER,
            estimated_cost_krw=10,
        )
        with self.assertRaises(UsageLedgerError):
            ledger(duplicate, duplicate)

    def test_shared_platform_cost_does_not_consume_user_limit(self) -> None:
        snapshot = build_cost_snapshot(
            ledger(
                usage_entry(
                    entry_id="use-1",
                    owner_id=USER,
                    estimated_cost_krw=900,
                    finalized_cost_krw=900,
                ),
                usage_entry(
                    entry_id="use-2",
                    owner_id=None,
                    estimated_cost_krw=50_000,
                    finalized_cost_krw=50_000,
                    workload_id=None,
                ),
            ),
            user_id=USER,
        )

        decision = evaluate_cost_policy(snapshot=snapshot)

        self.assertEqual(snapshot.user_direct_estimated_krw, 900)
        self.assertEqual(snapshot.shared_estimated_krw, 50_000)
        self.assertEqual(decision.user_percent, 90)
        self.assertTrue(decision.block_new)
        self.assertTrue(decision.emergency_stop)
        self.assertIn("user_block_new_threshold_reached", decision.reason_codes)
        self.assertIn("org_emergency_ceiling_reached", decision.reason_codes)

    def test_estimated_and_finalized_costs_are_reported_separately(self) -> None:
        snapshot = build_cost_snapshot(
            ledger(
                usage_entry(
                    entry_id="use-1",
                    owner_id=USER,
                    estimated_cost_krw=650,
                    finalized_cost_krw=800,
                    confidence=UsageConfidence.FINALIZED,
                )
            ),
            user_id=USER,
        )

        decision = evaluate_cost_policy(snapshot=snapshot)

        self.assertEqual(snapshot.user_direct_estimated_krw, 650)
        self.assertEqual(snapshot.user_direct_finalized_krw, 800)
        self.assertEqual(snapshot.user_policy_krw, 800)
        self.assertTrue(decision.warn)
        self.assertFalse(decision.block_new)
        self.assertIn("user_warn_threshold_reached", decision.reason_codes)

    def test_cost_policy_uses_per_entry_conservative_basis(self) -> None:
        snapshot = build_cost_snapshot(
            ledger(
                usage_entry(
                    entry_id="use-user-1",
                    owner_id=USER,
                    estimated_cost_krw=900,
                    finalized_cost_krw=0,
                ),
                usage_entry(
                    entry_id="use-user-2",
                    owner_id=USER,
                    estimated_cost_krw=0,
                    finalized_cost_krw=900,
                    confidence=UsageConfidence.FINALIZED,
                ),
                usage_entry(
                    entry_id="use-other-1",
                    owner_id=OTHER_USER,
                    estimated_cost_krw=200,
                    finalized_cost_krw=500,
                    confidence=UsageConfidence.FINALIZED,
                ),
                usage_entry(
                    entry_id="use-shared-1",
                    owner_id=None,
                    estimated_cost_krw=300,
                    finalized_cost_krw=100,
                    workload_id=None,
                ),
                usage_entry(
                    entry_id="use-shared-2",
                    owner_id=None,
                    estimated_cost_krw=50,
                    finalized_cost_krw=400,
                    confidence=UsageConfidence.FINALIZED,
                    workload_id=None,
                ),
            ),
            user_id=USER,
        )

        self.assertEqual(snapshot.user_policy_krw, 1800)
        self.assertEqual(snapshot.org_direct_policy_krw, 2300)
        self.assertEqual(snapshot.shared_policy_krw, 700)
        self.assertEqual(snapshot.user_percent, 180)

    def test_reservation_and_projection_are_monotonic(self) -> None:
        snapshot = build_cost_snapshot(
            ledger(
                usage_entry(
                    entry_id="use-1",
                    owner_id=USER,
                    estimated_cost_krw=850,
                    finalized_cost_krw=850,
                )
            ),
            user_id=USER,
        )

        without_reserve = evaluate_cost_policy(snapshot=snapshot)
        with_reserve = evaluate_cost_policy(
            snapshot=snapshot,
            lag_reservation_krw=100,
            proposed_cost_krw=50,
        )

        self.assertFalse(without_reserve.pause)
        self.assertTrue(with_reserve.pause)
        self.assertIn("user_projected_limit_reached", with_reserve.reason_codes)

    def test_cost_threshold_boundaries_are_exact(self) -> None:
        cases = (
            (699, False, False, False, ()),
            (700, True, False, False, ("user_warn_threshold_reached",)),
            (899, True, False, False, ("user_warn_threshold_reached",)),
            (
                900,
                True,
                True,
                False,
                (
                    "user_warn_threshold_reached",
                    "user_block_new_threshold_reached",
                ),
            ),
            (
                1000,
                True,
                True,
                True,
                (
                    "user_warn_threshold_reached",
                    "user_block_new_threshold_reached",
                    "user_projected_limit_reached",
                ),
            ),
        )

        for amount, warn, block_new, pause, reason_codes in cases:
            with self.subTest(amount=amount):
                snapshot = build_cost_snapshot(
                    ledger(
                        usage_entry(
                            entry_id=f"use-{amount}",
                            owner_id=USER,
                            estimated_cost_krw=amount,
                            finalized_cost_krw=amount,
                        )
                    ),
                    user_id=USER,
                )
                decision = evaluate_cost_policy(snapshot=snapshot)
                self.assertEqual(decision.user_percent, amount // 10)
                self.assertEqual(decision.warn, warn)
                self.assertEqual(decision.block_new, block_new)
                self.assertEqual(decision.pause, pause)
                self.assertEqual(decision.reason_codes, reason_codes)

    def test_org_emergency_boundary_is_exact(self) -> None:
        safe_snapshot = build_cost_snapshot(
            ledger(
                usage_entry(
                    entry_id="use-safe",
                    owner_id=USER,
                    estimated_cost_krw=5_000,
                    finalized_cost_krw=5_000,
                ),
                usage_entry(
                    entry_id="shared-safe",
                    owner_id=None,
                    estimated_cost_krw=4_999,
                    finalized_cost_krw=4_999,
                    workload_id=None,
                ),
            ),
            user_id=USER,
        )
        emergency_snapshot = build_cost_snapshot(
            ledger(
                usage_entry(
                    entry_id="use-emergency",
                    owner_id=USER,
                    estimated_cost_krw=5_000,
                    finalized_cost_krw=5_000,
                ),
                usage_entry(
                    entry_id="shared-emergency",
                    owner_id=None,
                    estimated_cost_krw=5_000,
                    finalized_cost_krw=5_000,
                    workload_id=None,
                ),
            ),
            user_id=USER,
        )

        self.assertFalse(evaluate_cost_policy(snapshot=safe_snapshot).emergency_stop)
        emergency = evaluate_cost_policy(snapshot=emergency_snapshot)
        self.assertTrue(emergency.emergency_stop)
        self.assertIn("org_emergency_ceiling_reached", emergency.reason_codes)

    def test_org_projection_includes_current_user_projection(self) -> None:
        snapshot = build_cost_snapshot(
            ledger(
                usage_entry(
                    entry_id="use-safe",
                    owner_id=USER,
                    estimated_cost_krw=9_950,
                    finalized_cost_krw=9_950,
                )
            ),
            user_id=USER,
        )

        decision = evaluate_cost_policy(
            snapshot=snapshot,
            lag_reservation_krw=50,
            proposed_cost_krw=50,
        )

        self.assertTrue(decision.emergency_stop)
        self.assertEqual(decision.org_policy_cost_krw, 10_050)

    def test_per_user_isolation_ignores_other_users_direct_spend(self) -> None:
        usage = ledger(
            usage_entry(
                entry_id="use-1",
                owner_id=USER,
                estimated_cost_krw=200,
                finalized_cost_krw=200,
            ),
            usage_entry(
                entry_id="use-2",
                owner_id=OTHER_USER,
                estimated_cost_krw=900,
                finalized_cost_krw=900,
            ),
        )

        user_snapshot = build_cost_snapshot(usage, user_id=USER)
        other_snapshot = build_cost_snapshot(usage, user_id=OTHER_USER)

        self.assertEqual(user_snapshot.user_percent, 20)
        self.assertEqual(other_snapshot.user_percent, 90)
        self.assertFalse(evaluate_cost_policy(snapshot=user_snapshot).block_new)
        self.assertTrue(evaluate_cost_policy(snapshot=other_snapshot).block_new)

    def test_previous_month_entries_do_not_contribute_to_current_month_policy(
        self,
    ) -> None:
        usage = build_usage_ledger(
            usage_entries_for_utc_month(
                (
                    usage_entry(
                        entry_id="use-previous-user",
                        owner_id=USER,
                        estimated_cost_krw=900,
                        finalized_cost_krw=900,
                        collected_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
                    ),
                    usage_entry(
                        entry_id="use-current-user",
                        owner_id=USER,
                        estimated_cost_krw=200,
                        finalized_cost_krw=200,
                        collected_at=NOW,
                    ),
                    usage_entry(
                        entry_id="use-previous-shared",
                        owner_id=None,
                        estimated_cost_krw=9_900,
                        finalized_cost_krw=9_900,
                        workload_id=None,
                        collected_at=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
                    ),
                ),
                now=NOW,
            )
        )

        snapshot = build_cost_snapshot(usage, user_id=USER)
        decision = evaluate_cost_policy(snapshot=snapshot)

        self.assertEqual(snapshot.user_policy_krw, 200)
        self.assertEqual(snapshot.org_direct_policy_krw, 200)
        self.assertEqual(snapshot.shared_policy_krw, 0)
        self.assertFalse(decision.warn)
        self.assertFalse(decision.emergency_stop)

    def test_cost_snapshot_rejects_invalid_money_inputs(self) -> None:
        valid_snapshot = CostSnapshot(
            user_direct_estimated_krw=0,
            user_direct_finalized_krw=0,
            user_policy_krw=0,
            org_direct_estimated_krw=0,
            org_direct_finalized_krw=0,
            org_direct_policy_krw=0,
            shared_estimated_krw=0,
            shared_finalized_krw=0,
            shared_policy_krw=0,
            user_percent=0,
        )
        self.assertTrue(dataclasses.is_dataclass(valid_snapshot))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(valid_snapshot, "user_percent", 1)

        invalid_values: tuple[object, ...] = (-1, True, 1.5, "100")
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(QuotaPolicyError):
                    evaluate_cost_policy(
                        snapshot=valid_snapshot,
                        lag_reservation_krw=cast(Any, value),
                    )
                with self.assertRaises(QuotaPolicyError):
                    evaluate_cost_policy(
                        snapshot=valid_snapshot,
                        proposed_cost_krw=cast(Any, value),
                    )
                with self.assertRaises(QuotaPolicyError):
                    evaluate_cost_policy(
                        snapshot=valid_snapshot,
                        org_projected_additional_krw=cast(Any, value),
                    )

    def test_resource_caps_block_new_items_beyond_exact_limits(self) -> None:
        allowed = evaluate_resource_policy(
            ResourceInventory(
                active_services=PER_USER_SERVICE_LIMIT - 1,
                active_schedules=PER_USER_SCHEDULE_LIMIT - 1,
                active_secrets=DEFAULT_SECRET_LIMIT - 1,
            )
        )
        blocked = evaluate_resource_policy(
            ResourceInventory(
                active_services=PER_USER_SERVICE_LIMIT,
                active_schedules=PER_USER_SCHEDULE_LIMIT,
                active_secrets=DEFAULT_SECRET_LIMIT,
            )
        )

        self.assertFalse(allowed.service_limit_reached)
        self.assertFalse(allowed.schedule_limit_reached)
        self.assertFalse(allowed.secret_limit_reached)
        self.assertEqual(allowed.secret_limit, DEFAULT_SECRET_LIMIT)
        self.assertTrue(blocked.service_limit_reached)
        self.assertTrue(blocked.schedule_limit_reached)
        self.assertTrue(blocked.secret_limit_reached)
        self.assertEqual(
            blocked.reason_codes,
            (
                "service_limit_reached",
                "schedule_limit_reached",
                "secret_limit_reached",
            ),
        )

    def test_explicit_secret_limit_must_stay_within_central_bounds(self) -> None:
        approved = evaluate_resource_policy(
            ResourceInventory(
                active_services=0,
                active_schedules=0,
                active_secrets=4,
                approved_secret_limit=4,
            )
        )
        self.assertEqual(approved.secret_limit, 4)

        invalid_limits: tuple[object, ...] = (0, HARD_SECRET_LIMIT + 1, True, "4")
        for invalid_limit in invalid_limits:
            with self.subTest(invalid_limit=invalid_limit):
                with self.assertRaises(QuotaPolicyError):
                    evaluate_resource_policy(
                        ResourceInventory(
                            active_services=0,
                            active_schedules=0,
                            active_secrets=0,
                            approved_secret_limit=cast(Any, invalid_limit),
                        )
                    )

    def test_config_constants_match_task_policy(self) -> None:
        self.assertEqual(PER_USER_SERVICE_LIMIT, 2)
        self.assertEqual(PER_USER_SCHEDULE_LIMIT, 3)
        self.assertEqual(DEFAULT_SECRET_LIMIT, 5)
        self.assertEqual(HARD_SECRET_LIMIT, 10)
        self.assertEqual(TARGET_MONTHLY_BUDGET_KRW, 1000)
        self.assertEqual(ADMIN_BUDGET_CEILING_KRW, 10000)


if __name__ == "__main__":
    unittest.main()
