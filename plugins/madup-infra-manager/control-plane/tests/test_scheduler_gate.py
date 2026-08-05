from __future__ import annotations

import dataclasses
import threading
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import Schedule, ScheduleId, UserId, WorkloadId
from mim_control_plane.domain.states import ScheduleState
from mim_control_plane.ports.store import InvariantViolation, VersionConflict
from mim_control_plane.services.schedules import (
    normalize_schedule_policy,
    schedule_is_due,
)

NOW = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)


def schedule(
    *,
    state: ScheduleState = ScheduleState.ENABLED,
    version: int = 1,
    lease_token: str | None = None,
    lease_expires_at: datetime | None = None,
    consecutive_failures: int = 0,
    last_attempt_at: datetime | None = None,
    last_success_at: datetime | None = None,
    updated_at: datetime = NOW,
) -> Schedule:
    return Schedule(
        id=ScheduleId("sch-1"),
        owner_id=UserId("usr-1"),
        workload_id=WorkloadId("wrk-1"),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=state,
        created_at=NOW,
        updated_at=updated_at,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        consecutive_failures=consecutive_failures,
        last_attempt_at=last_attempt_at,
        last_success_at=last_success_at,
        version=version,
    )


class SchedulePolicyTests(unittest.TestCase):
    def test_normalize_schedule_accepts_exact_policy_and_hourly_alias(self) -> None:
        self.assertEqual(
            normalize_schedule_policy("0 * * * *", "Asia/Seoul"),
            ("0 * * * *", "Asia/Seoul"),
        )
        self.assertEqual(
            normalize_schedule_policy("hourly", "Asia/Seoul"),
            ("0 * * * *", "Asia/Seoul"),
        )

    def test_normalize_schedule_rejects_other_values_without_echoing_input(
        self,
    ) -> None:
        cases = (
            ("@hourly", "Asia/Seoul"),
            ("30 * * * *", "Asia/Seoul"),
            ("0 */2 * * *", "Asia/Seoul"),
            ("daily", "Asia/Seoul"),
            (True, "Asia/Seoul"),
            (1, "Asia/Seoul"),
            ("0 * * * *", "UTC"),
            ("0 * * * *", True),
        )

        for cron, timezone in cases:
            with self.subTest(cron=cron, timezone=timezone):
                with self.assertRaises(ValueError) as ctx:
                    normalize_schedule_policy(cron, timezone)
                self.assertNotIn(str(cron), str(ctx.exception))
                self.assertNotIn(str(timezone), str(ctx.exception))

    def test_schedule_due_uses_exact_seoul_hour_boundaries_across_us_dst_dates(
        self,
    ) -> None:
        hourly = schedule()
        due_ticks = (
            datetime(2026, 3, 8, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
            datetime(2026, 11, 1, 5, 0, tzinfo=UTC),
            datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
        )
        not_due_ticks = (
            datetime(2026, 3, 8, 0, 1, tzinfo=UTC),
            datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
            datetime(2026, 11, 1, 5, 59, tzinfo=UTC),
            datetime(2026, 11, 1, 6, 0, 1, tzinfo=UTC),
        )

        for tick_at in due_ticks:
            with self.subTest(tick_at=tick_at, due=True):
                self.assertTrue(schedule_is_due(hourly, tick_at=tick_at))
        for tick_at in not_due_ticks:
            with self.subTest(tick_at=tick_at, due=False):
                self.assertFalse(schedule_is_due(hourly, tick_at=tick_at))


class ScheduleLeaseStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_schedule(schedule())

    def test_create_schedule_rejects_one_sided_persisted_lease_material(self) -> None:
        for malformed in (
            schedule(lease_token="lease-valid_token"),
            schedule(lease_expires_at=NOW + timedelta(minutes=5)),
        ):
            with self.subTest(malformed=malformed):
                store = MemoryStore()
                with self.assertRaises(InvariantViolation):
                    store.create_schedule(malformed)

    def test_acquire_schedule_lease_records_token_and_expiry(self) -> None:
        leased = self.store.acquire_schedule_lease(
            ScheduleId("sch-1"),
            expected_version=1,
            lease_token="lease-valid_1",
            lease_expires_at=NOW + timedelta(minutes=5),
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(leased.version, 2)
        self.assertEqual(leased.lease_token, "lease-valid_1")
        self.assertEqual(leased.lease_expires_at, NOW + timedelta(minutes=5))

    def test_active_lease_is_denied_but_expired_lease_can_be_replaced(self) -> None:
        leased = self.store.acquire_schedule_lease(
            ScheduleId("sch-1"),
            expected_version=1,
            lease_token="lease-valid_1",
            lease_expires_at=NOW + timedelta(minutes=5),
            now=NOW + timedelta(seconds=1),
        )

        with self.assertRaises(InvariantViolation):
            self.store.acquire_schedule_lease(
                ScheduleId("sch-1"),
                expected_version=2,
                lease_token="lease-valid_2",
                lease_expires_at=NOW + timedelta(minutes=6),
                now=NOW + timedelta(minutes=1),
            )

        replaced = self.store.acquire_schedule_lease(
            ScheduleId("sch-1"),
            expected_version=2,
            lease_token="lease-valid_2",
            lease_expires_at=NOW + timedelta(minutes=10),
            now=NOW + timedelta(minutes=5),
        )

        self.assertEqual(replaced.version, 3)
        self.assertEqual(replaced.lease_token, "lease-valid_2")
        self.assertEqual(replaced.lease_expires_at, NOW + timedelta(minutes=10))
        self.assertNotEqual(replaced.lease_token, leased.lease_token)

    def test_expired_reacquire_requires_a_different_token(self) -> None:
        store = MemoryStore()
        store.create_schedule(
            schedule(
                lease_token="lease-valid_1",
                lease_expires_at=NOW + timedelta(minutes=5),
            )
        )

        with self.assertRaises(InvariantViolation):
            store.acquire_schedule_lease(
                ScheduleId("sch-1"),
                expected_version=1,
                lease_token="lease-valid_1",
                lease_expires_at=NOW + timedelta(minutes=10),
                now=NOW + timedelta(minutes=5),
            )

    def test_non_enabled_schedule_cannot_acquire_lease(self) -> None:
        for blocked_state in (
            ScheduleState.DISABLED,
            ScheduleState.PAUSED,
            ScheduleState.QUARANTINED,
            ScheduleState.ARCHIVED,
        ):
            with self.subTest(blocked_state=blocked_state):
                store = MemoryStore()
                store.create_schedule(schedule(state=blocked_state))
                with self.assertRaises(InvariantViolation):
                    store.acquire_schedule_lease(
                        ScheduleId("sch-1"),
                        expected_version=1,
                        lease_token="lease-valid_1",
                        lease_expires_at=NOW + timedelta(minutes=5),
                        now=NOW + timedelta(seconds=1),
                    )

    def test_acquire_schedule_lease_rejects_stale_version_and_invalid_expected_version(
        self,
    ) -> None:
        with self.assertRaises(VersionConflict):
            self.store.acquire_schedule_lease(
                ScheduleId("sch-1"),
                expected_version=2,
                lease_token="lease-valid_1",
                lease_expires_at=NOW + timedelta(minutes=5),
                now=NOW + timedelta(seconds=1),
            )
        for bad_version in (True, 0, "1"):
            with self.subTest(bad_version=bad_version):
                with self.assertRaises(ValueError):
                    self.store.acquire_schedule_lease(
                        ScheduleId("sch-1"),
                        expected_version=cast(Any, bad_version),
                        lease_token="lease-valid_1",
                        lease_expires_at=NOW + timedelta(minutes=5),
                        now=NOW + timedelta(seconds=1),
                    )

    def test_acquire_schedule_lease_rejects_invalid_token_grammar_and_secret_markers(
        self,
    ) -> None:
        for bad_token in (
            "",
            "lease-",
            "lease.with.dot",
            "lease/with/slash",
            "lease with space",
            "lease-\ncontrol",
            "x" * 129,
            True,
            "lease-ghp_secret",
            "lease-sk-secret",
            "lease-api_key_secret",
            "lease-authorization_secret",
            "lease-cookie_secret",
            "lease-bearer_secret",
            "lease-access_token_secret",
            "lease-refresh_token_secret",
        ):
            with self.subTest(bad_token=bad_token):
                store = MemoryStore()
                store.create_schedule(schedule())
                with self.assertRaises(ValueError):
                    store.acquire_schedule_lease(
                        ScheduleId("sch-1"),
                        expected_version=1,
                        lease_token=cast(Any, bad_token),
                        lease_expires_at=NOW + timedelta(minutes=5),
                        now=NOW + timedelta(seconds=1),
                    )

    def test_acquire_schedule_lease_rejects_malformed_persisted_lease_state(
        self,
    ) -> None:
        for malformed in (
            schedule(lease_token="lease-valid_1"),
            schedule(lease_expires_at=NOW + timedelta(minutes=5)),
        ):
            with self.subTest(malformed=malformed):
                store = MemoryStore()
                store._schedules[ScheduleId("sch-1")] = dataclasses.replace(malformed)
                with self.assertRaises(InvariantViolation):
                    store.acquire_schedule_lease(
                        ScheduleId("sch-1"),
                        expected_version=1,
                        lease_token="lease-valid_2",
                        lease_expires_at=NOW + timedelta(minutes=10),
                        now=NOW + timedelta(seconds=1),
                    )
                loaded = store.get_schedule(ScheduleId("sch-1"))
                self.assertEqual(loaded.lease_token, malformed.lease_token)
                self.assertEqual(loaded.lease_expires_at, malformed.lease_expires_at)

    def test_acquire_schedule_lease_rejects_expired_or_non_utc_expiry(self) -> None:
        with self.assertRaises(ValueError):
            self.store.acquire_schedule_lease(
                ScheduleId("sch-1"),
                expected_version=1,
                lease_token="lease-valid_1",
                lease_expires_at=NOW,
                now=NOW + timedelta(seconds=1),
            )
        for bad_token in ("", "x" * 129, True):
            with self.subTest(bad_token=bad_token):
                with self.assertRaises(ValueError):
                    self.store.acquire_schedule_lease(
                        ScheduleId("sch-1"),
                        expected_version=1,
                        lease_token="lease-valid_1",
                        lease_expires_at=cast(Any, bad_token),
                        now=NOW + timedelta(seconds=1),
                    )

    def test_complete_schedule_run_rejects_non_enabled_state_after_legal_transition(
        self,
    ) -> None:
        store = MemoryStore()
        store.create_schedule(
            schedule(
                lease_token="lease-valid_1",
                lease_expires_at=NOW + timedelta(minutes=5),
            )
        )
        paused = store.save_schedule(
            dataclasses.replace(
                schedule(
                    lease_token="lease-valid_1",
                    lease_expires_at=NOW + timedelta(minutes=5),
                ),
                state=ScheduleState.PAUSED,
                version=2,
                updated_at=NOW + timedelta(seconds=1),
            ),
            expected_version=1,
        )

        with self.assertRaises(InvariantViolation):
            store.complete_schedule_run(
                ScheduleId("sch-1"),
                expected_version=paused.version,
                lease_token="lease-valid_1",
                succeeded=True,
                completed_at=NOW + timedelta(minutes=1),
            )
        loaded = store.get_schedule(ScheduleId("sch-1"))
        self.assertEqual(loaded.state, ScheduleState.PAUSED)
        self.assertEqual(loaded.lease_token, "lease-valid_1")

    def test_complete_schedule_run_requires_positive_int_expected_version(self) -> None:
        self.store.acquire_schedule_lease(
            ScheduleId("sch-1"),
            expected_version=1,
            lease_token="lease-valid_1",
            lease_expires_at=NOW + timedelta(minutes=5),
            now=NOW + timedelta(seconds=1),
        )
        for bad_version in (True, 0, "2"):
            with self.subTest(bad_version=bad_version):
                with self.assertRaises(ValueError):
                    self.store.complete_schedule_run(
                        ScheduleId("sch-1"),
                        expected_version=cast(Any, bad_version),
                        lease_token="lease-valid_1",
                        succeeded=True,
                        completed_at=NOW + timedelta(minutes=1),
                    )

    def test_complete_schedule_run_rejects_malformed_persisted_lease_state(
        self,
    ) -> None:
        for malformed in (
            schedule(lease_token="lease-valid_1"),
            schedule(lease_expires_at=NOW + timedelta(minutes=5)),
        ):
            with self.subTest(malformed=malformed):
                store = MemoryStore()
                store._schedules[ScheduleId("sch-1")] = dataclasses.replace(malformed)
                with self.assertRaises(InvariantViolation):
                    store.complete_schedule_run(
                        ScheduleId("sch-1"),
                        expected_version=1,
                        lease_token="lease-valid_1",
                        succeeded=True,
                        completed_at=NOW + timedelta(minutes=1),
                    )

    def test_complete_schedule_run_rejects_invalid_token_grammar(self) -> None:
        leased = self.store.acquire_schedule_lease(
            ScheduleId("sch-1"),
            expected_version=1,
            lease_token="lease-valid_1",
            lease_expires_at=NOW + timedelta(minutes=5),
            now=NOW + timedelta(seconds=1),
        )
        self.assertEqual(leased.version, 2)
        for bad_token in ("lease.with.dot", "lease-cookie_secret", True):
            with self.subTest(bad_token=bad_token):
                with self.assertRaises(ValueError):
                    self.store.complete_schedule_run(
                        ScheduleId("sch-1"),
                        expected_version=leased.version,
                        lease_token=cast(Any, bad_token),
                        succeeded=True,
                        completed_at=NOW + timedelta(minutes=1),
                    )

    def test_complete_schedule_run_success_clears_lease_and_resets_failures(
        self,
    ) -> None:
        store = MemoryStore()
        store.create_schedule(
            schedule(
                version=1,
                lease_token="lease-valid_1",
                lease_expires_at=NOW + timedelta(minutes=5),
                consecutive_failures=2,
            )
        )

        completed = store.complete_schedule_run(
            ScheduleId("sch-1"),
            expected_version=1,
            lease_token="lease-valid_1",
            succeeded=True,
            completed_at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(completed.version, 2)
        self.assertEqual(completed.consecutive_failures, 0)
        self.assertIsNone(completed.lease_token)
        self.assertIsNone(completed.lease_expires_at)
        self.assertEqual(completed.last_attempt_at, NOW + timedelta(minutes=1))
        self.assertEqual(completed.last_success_at, NOW + timedelta(minutes=1))
        self.assertEqual(completed.state, ScheduleState.ENABLED)

    def test_complete_schedule_run_disables_exactly_on_third_failure(self) -> None:
        first_store = MemoryStore()
        first_store.create_schedule(
            schedule(
                version=1,
                lease_token="lease-valid_1",
                lease_expires_at=NOW + timedelta(minutes=5),
            )
        )
        first = first_store.complete_schedule_run(
            ScheduleId("sch-1"),
            expected_version=1,
            lease_token="lease-valid_1",
            succeeded=False,
            completed_at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(first.consecutive_failures, 1)
        self.assertEqual(first.state, ScheduleState.ENABLED)

        second_store = MemoryStore()
        second_store.create_schedule(
            schedule(
                version=1,
                lease_token="lease-valid_1",
                lease_expires_at=NOW + timedelta(minutes=5),
                consecutive_failures=1,
            )
        )
        second = second_store.complete_schedule_run(
            ScheduleId("sch-1"),
            expected_version=1,
            lease_token="lease-valid_1",
            succeeded=False,
            completed_at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(second.consecutive_failures, 2)
        self.assertEqual(second.state, ScheduleState.ENABLED)

        third_store = MemoryStore()
        third_store.create_schedule(
            schedule(
                version=1,
                lease_token="lease-valid_1",
                lease_expires_at=NOW + timedelta(minutes=5),
                consecutive_failures=2,
            )
        )
        third = third_store.complete_schedule_run(
            ScheduleId("sch-1"),
            expected_version=1,
            lease_token="lease-valid_1",
            succeeded=False,
            completed_at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(third.consecutive_failures, 3)
        self.assertEqual(third.state, ScheduleState.DISABLED)
        self.assertEqual(third.last_attempt_at, NOW + timedelta(minutes=1))
        self.assertIsNone(third.last_success_at)

    def test_complete_schedule_run_rejects_stale_token_version_or_expired_lease(
        self,
    ) -> None:
        leased = self.store.acquire_schedule_lease(
            ScheduleId("sch-1"),
            expected_version=1,
            lease_token="lease-valid_1",
            lease_expires_at=NOW + timedelta(minutes=5),
            now=NOW + timedelta(seconds=1),
        )

        with self.assertRaises(VersionConflict):
            self.store.complete_schedule_run(
                ScheduleId("sch-1"),
                expected_version=1,
                lease_token="lease-valid_1",
                succeeded=True,
                completed_at=NOW + timedelta(minutes=1),
            )
        with self.assertRaises(InvariantViolation):
            self.store.complete_schedule_run(
                ScheduleId("sch-1"),
                expected_version=leased.version,
                lease_token="lease-valid_2",
                succeeded=True,
                completed_at=NOW + timedelta(minutes=1),
            )
        with self.assertRaises(InvariantViolation):
            self.store.complete_schedule_run(
                ScheduleId("sch-1"),
                expected_version=leased.version,
                lease_token="lease-valid_1",
                succeeded=True,
                completed_at=NOW + timedelta(minutes=5),
            )
        with self.assertRaises(ValueError):
            self.store.complete_schedule_run(
                ScheduleId("sch-1"),
                expected_version=leased.version,
                lease_token="lease-valid_1",
                succeeded=cast(Any, 1),
                completed_at=NOW + timedelta(minutes=1),
            )

    def test_generic_save_schedule_cannot_bypass_gate_managed_fields(self) -> None:
        with self.assertRaises(InvariantViolation):
            self.store.save_schedule(
                dataclasses.replace(
                    schedule(),
                    lease_token="lease-valid_1",
                    lease_expires_at=NOW + timedelta(minutes=5),
                    version=2,
                    updated_at=NOW + timedelta(seconds=1),
                ),
                expected_version=1,
            )
        with self.assertRaises(InvariantViolation):
            self.store.save_schedule(
                dataclasses.replace(
                    schedule(),
                    consecutive_failures=1,
                    last_attempt_at=NOW + timedelta(minutes=1),
                    last_success_at=NOW + timedelta(minutes=1),
                    version=2,
                    updated_at=NOW + timedelta(seconds=1),
                ),
                expected_version=1,
            )

    def test_concurrent_acquire_has_single_winner(self) -> None:
        barrier = threading.Barrier(2)
        results: list[str] = []
        failures: list[type[BaseException]] = []

        def contender(token: str) -> None:
            try:
                barrier.wait()
                leased = self.store.acquire_schedule_lease(
                    ScheduleId("sch-1"),
                    expected_version=1,
                    lease_token=token,
                    lease_expires_at=NOW + timedelta(minutes=5),
                    now=NOW + timedelta(seconds=1),
                )
                results.append(leased.lease_token or "")
            except BaseException as exc:  # pragma: no cover - test harness capture
                failures.append(type(exc))

        first = threading.Thread(target=contender, args=("lease-valid_1",))
        second = threading.Thread(target=contender, args=("lease-valid_2",))
        first.start()
        second.start()
        first.join()
        second.join()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn(failures[0], {InvariantViolation, VersionConflict})


if __name__ == "__main__":
    unittest.main()
