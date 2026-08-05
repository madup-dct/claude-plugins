# ruff: noqa: E402

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = TEST_ROOT.parent / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import MaintenanceJobStatus
from mim_control_plane.ports.store import VersionConflict

NOW = datetime(2026, 8, 4, 5, 0, 0, tzinfo=UTC)


class MaintenanceJobStatusStoreTests(unittest.TestCase):
    def test_started_and_terminal_statuses_round_trip_with_versions(self) -> None:
        store = MemoryStore()

        started = store.record_maintenance_job_started(
            job_name="identity-sync",
            run_id="run-1",
            started_at=NOW,
        )

        self.assertEqual(
            started,
            MaintenanceJobStatus(
                job_name="identity-sync",
                run_id="run-1",
                started_at=NOW,
                finished_at=None,
                succeeded_at=None,
                failed_at=None,
                outcome="started",
                summary=(),
                failure_code=None,
                failure_class=None,
                version=1,
            ),
        )

        completed = store.record_maintenance_job_terminal(
            job_name="identity-sync",
            run_id="run-1",
            expected_version=started.version,
            finished_at=NOW + timedelta(minutes=2),
            outcome="completed",
            summary=(("processed_users", 3), ("updated_users", 2)),
        )

        self.assertEqual(completed.version, 2)
        self.assertEqual(completed.outcome, "completed")
        self.assertEqual(completed.finished_at, NOW + timedelta(minutes=2))
        self.assertEqual(completed.succeeded_at, NOW + timedelta(minutes=2))
        self.assertIsNone(completed.failed_at)
        self.assertEqual(
            store.get_maintenance_job_status("identity-sync"),
            completed,
        )
        self.assertEqual(store.list_maintenance_job_statuses(), (completed,))

    def test_terminal_write_rejects_stale_run_id_after_newer_start(self) -> None:
        store = MemoryStore()

        first = store.record_maintenance_job_started(
            job_name="usage-ingest",
            run_id="run-old",
            started_at=NOW,
        )
        current = store.record_maintenance_job_started(
            job_name="usage-ingest",
            run_id="run-new",
            started_at=NOW + timedelta(minutes=10),
        )

        self.assertEqual(first.version, 1)
        self.assertEqual(current.version, 2)

        with self.assertRaises(VersionConflict):
            store.record_maintenance_job_terminal(
                job_name="usage-ingest",
                run_id="run-old",
                expected_version=current.version,
                finished_at=NOW + timedelta(minutes=11),
                outcome="failed",
                summary=(("billing_appended_entries", 0),),
                failure_code="runtime_error",
                failure_class="RuntimeError",
            )


if __name__ == "__main__":
    unittest.main()
