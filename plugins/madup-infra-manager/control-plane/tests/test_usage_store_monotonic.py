from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = TEST_ROOT.parent / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.memory_store import MemoryStore  # noqa: E402
from mim_control_plane.domain.models import (  # noqa: E402
    UsageEntry,
    UsageEntryId,
    UserId,
    WorkloadId,
)
from mim_control_plane.domain.states import UsageConfidence  # noqa: E402
from mim_control_plane.ports.store import (  # noqa: E402
    InvariantViolation,
    VersionConflict,
)

NOW = datetime(2026, 8, 4, tzinfo=UTC)


def usage_entry() -> UsageEntry:
    return UsageEntry(
        id=UsageEntryId("billing-row-1"),
        owner_id=UserId("usr-1"),
        workload_id=WorkloadId("wrk-1"),
        service_category="cloud_run",
        estimated_cost_krw=100,
        finalized_cost_krw=None,
        confidence=UsageConfidence.ESTIMATED,
        collected_at=NOW,
    )


class MonotonicUsageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.current = self.store.append_usage_entry(usage_entry())

    def test_update_strengthens_cost_evidence_and_exact_retry_is_noop(self) -> None:
        updated = replace(
            self.current,
            estimated_cost_krw=150,
            finalized_cost_krw=140,
            confidence=UsageConfidence.FINALIZED,
            collected_at=NOW + timedelta(hours=1),
        )

        first = self.store.upsert_usage_entry_monotonic(
            current=self.current,
            updated=updated,
        )
        replay = self.store.upsert_usage_entry_monotonic(
            current=self.current,
            updated=updated,
        )

        self.assertEqual(first, updated)
        self.assertEqual(replay, updated)
        self.assertEqual(self.store.list_usage_entries(), (updated,))

    def test_stale_conflicting_update_fails_closed(self) -> None:
        updated = replace(
            self.current,
            estimated_cost_krw=150,
            collected_at=NOW + timedelta(hours=1),
        )
        self.store.upsert_usage_entry_monotonic(
            current=self.current,
            updated=updated,
        )

        with self.assertRaises(VersionConflict):
            self.store.upsert_usage_entry_monotonic(
                current=self.current,
                updated=replace(
                    self.current,
                    estimated_cost_krw=160,
                    collected_at=NOW + timedelta(hours=2),
                ),
            )

    def test_immutable_material_and_monotonic_fields_are_enforced(self) -> None:
        invalid_updates = (
            replace(self.current, owner_id=UserId("usr-2")),
            replace(self.current, workload_id=WorkloadId("wrk-2")),
            replace(self.current, service_category="bigquery"),
            replace(self.current, estimated_cost_krw=99),
            replace(
                self.current,
                confidence=UsageConfidence.MEASURED,
                collected_at=NOW - timedelta(seconds=1),
            ),
        )
        for updated in invalid_updates:
            with self.subTest(updated=updated):
                with self.assertRaises(InvariantViolation):
                    self.store.upsert_usage_entry_monotonic(
                        current=self.current,
                        updated=updated,
                    )

        finalized = replace(
            self.current,
            finalized_cost_krw=100,
            confidence=UsageConfidence.FINALIZED,
        )
        self.store.upsert_usage_entry_monotonic(
            current=self.current,
            updated=finalized,
        )
        for updated in (
            replace(finalized, finalized_cost_krw=None),
            replace(finalized, finalized_cost_krw=99),
            replace(finalized, confidence=UsageConfidence.MEASURED),
        ):
            with self.subTest(updated=updated):
                with self.assertRaises(InvariantViolation):
                    self.store.upsert_usage_entry_monotonic(
                        current=finalized,
                        updated=updated,
                    )


if __name__ == "__main__":
    unittest.main()
