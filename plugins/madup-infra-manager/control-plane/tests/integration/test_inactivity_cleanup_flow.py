from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.fake_schedule import (  # noqa: E402
    FakeScheduleControlPort,
    FakeScheduleRunDispatcher,
)
from mim_control_plane.adapters.memory_store import MemoryStore  # noqa: E402
from mim_control_plane.domain.models import (  # noqa: E402
    OrgCostGuard,
    RepositoryAdmission,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (  # noqa: E402
    LifecycleActionKind,
    LifecycleActionState,
    RepositoryAdmissionState,
    ScheduleState,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.services.schedule_management import (  # noqa: E402
    ScheduleManagementService,
)
from mim_control_plane.workers.lifecycle import LifecycleWorker  # noqa: E402

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


def user() -> User:
    return User(
        id=UserId("usr-1"),
        email="person@madup.com",
        role=UserRole.USER,
        state=UserState.ACTIVE,
        groups=frozenset({"mim-users"}),
        identity_synced_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=120),
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


def workload(
    *,
    last_activity_at: datetime | None,
    state: WorkloadState = WorkloadState.ACTIVE,
    version: int = 1,
) -> Workload:
    return Workload(
        id=WorkloadId("wrk-1"),
        owner_id=UserId("usr-1"),
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name="wrk-1",
        kind=WorkloadKind.SCHEDULED_SCRIPT,
        state=state,
        source_sha="b" * 40,
        desired_manifest_hash="manifest-hash",
        created_at=NOW - timedelta(days=60),
        updated_at=NOW - timedelta(days=1),
        last_activity_at=last_activity_at,
        last_healthy_image_digest="sha256:" + "2" * 64,
        version=version,
    )


def schedule(
    *,
    state: ScheduleState = ScheduleState.ENABLED,
    version: int = 1,
) -> Schedule:
    return Schedule(
        id=ScheduleId("sch-1"),
        owner_id=UserId("usr-1"),
        workload_id=WorkloadId("wrk-1"),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=state,
        created_at=NOW - timedelta(days=60),
        updated_at=NOW - timedelta(days=1),
        version=version,
    )


class NullSessions:
    def deny_user_sessions(self, *, user_id: UserId, reason: str) -> None:
        del user_id, reason


class NullAccess:
    def remove_owner_access(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        reason: str,
    ) -> None:
        del workload_id, expected_workload_version, reason


class NullBindings:
    def remove_workload_bindings(
        self,
        *,
        workload_id: WorkloadId,
        secret_ids: tuple[str, ...],
        expected_workload_version: int,
    ) -> None:
        del workload_id, secret_ids, expected_workload_version


class NullSlack:
    def revoke_user_grant(self, *, user_id: UserId, reason: str) -> None:
        del user_id, reason


class NullTransfer:
    def open_transfer_window(
        self,
        *,
        user_id: UserId,
        workload_ids: tuple[WorkloadId, ...],
        reason: str,
    ) -> None:
        del user_id, workload_ids, reason


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def notify_admin(self, *, user_id: UserId, kind: str, reason: str) -> None:
        self.calls.append((str(user_id), kind, reason))

    def notify_owner(
        self,
        *,
        user_id: UserId,
        workload_id: WorkloadId,
        reason: str,
    ) -> None:
        self.calls.append((str(user_id), str(workload_id), reason))


class RecordingScheduleManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[ScheduleId, ScheduleState, int]] = []

    def apply_schedule_state(
        self,
        *,
        schedule_id: ScheduleId,
        workload_id: WorkloadId,
        target_state: ScheduleState,
        expected_schedule_version: int,
        reason: str,
    ) -> None:
        del workload_id, reason
        self.events.append(f"schedule:{schedule_id}:{target_state.value}")
        self.calls.append((schedule_id, target_state, expected_schedule_version))


class RecordingCompute:
    def __init__(
        self,
        *,
        fail_first: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.fail_first = fail_first
        self.events = [] if events is None else events
        self.calls: list[tuple[WorkloadId, int, tuple[str, ...]]] = []
        self._attempts = 0

    def delete_compute(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        target_kinds: tuple[str, ...],
        retain_image_until: datetime | None,
    ) -> None:
        del retain_image_until
        self.events.append(f"compute:{workload_id}")
        self.calls.append((workload_id, expected_workload_version, target_kinds))
        self._attempts += 1
        if self.fail_first and self._attempts == 1:
            raise RuntimeError("transient delete failure")


class InactivityCleanupFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(user())
        self.store.create_repository_admission(admission())

    def worker(
        self,
        *,
        notifier: RecordingNotifier | None = None,
        compute: RecordingCompute | None = None,
        schedules: RecordingScheduleManager | None = None,
    ) -> LifecycleWorker:
        return LifecycleWorker(
            store=self.store,
            sessions=NullSessions(),
            access=NullAccess(),
            secret_bindings=NullBindings(),
            slack_grants=NullSlack(),
            notifier=notifier or RecordingNotifier(),
            transfer=NullTransfer(),
            schedules=schedules or RecordingScheduleManager([]),
            compute=compute or RecordingCompute(),
        )

    def _schedule_service(self) -> ScheduleManagementService:
        counters: dict[str, int] = {}

        def id_factory(prefix: str) -> str:
            counters[prefix] = counters.get(prefix, 0) + 1
            return f"{prefix}-{counters[prefix]}"

        return ScheduleManagementService(
            store=self.store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

    def test_day23_warning_and_day30_cleanup_archive_compute_but_keep_image(
        self,
    ) -> None:
        anchor = NOW - timedelta(days=23)
        self.store.create_workload(workload(last_activity_at=anchor))
        self.store.create_schedule(schedule())
        notifier = RecordingNotifier()
        events: list[str] = []
        schedules = RecordingScheduleManager(events)
        compute = RecordingCompute(events=events)
        worker = self.worker(
            notifier=notifier,
            compute=compute,
            schedules=schedules,
        )

        warning_result = worker.reconcile_user(
            user_id=UserId("usr-1"),
            account_locked_at=None,
            holds=frozenset(),
            now=NOW,
        )
        self.assertEqual(len(warning_result.planned_actions), 1)
        self.assertEqual(
            warning_result.planned_actions[0].action.kind,
            LifecycleActionKind.INACTIVITY_WARNING,
        )

        warning_exec = worker.execute_planned_action(
            warning_result.planned_actions[0],
            user_id=UserId("usr-1"),
            holds=frozenset(),
            account_locked_at=None,
            now=NOW,
        )
        self.assertEqual(warning_exec.action.state, LifecycleActionState.EXECUTED)
        self.assertIn(("usr-1", "wrk-1", "23_days_inactive"), notifier.calls)

        cleanup_time = anchor + timedelta(days=30)
        cleanup_result = worker.reconcile_user(
            user_id=UserId("usr-1"),
            account_locked_at=None,
            holds=frozenset(),
            now=cleanup_time,
        )
        self.assertEqual(len(cleanup_result.planned_actions), 1)
        self.assertEqual(
            cleanup_result.planned_actions[0].action.kind,
            LifecycleActionKind.DELETE_COMPUTE,
        )

        cleanup_exec = worker.execute_planned_action(
            cleanup_result.planned_actions[0],
            user_id=UserId("usr-1"),
            holds=frozenset(),
            account_locked_at=None,
            now=cleanup_time,
        )

        self.assertEqual(cleanup_exec.action.state, LifecycleActionState.EXECUTED)
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.ARCHIVED,
        )
        self.assertEqual(
            self.store.get_schedule(ScheduleId("sch-1")).state,
            ScheduleState.ARCHIVED,
        )
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).last_healthy_image_digest,
            "sha256:" + "2" * 64,
        )
        self.assertEqual(compute.calls[0][0], WorkloadId("wrk-1"))
        self.assertLess(
            events.index("schedule:sch-1:archived"),
            events.index("compute:wrk-1"),
        )

    def test_new_activity_before_cleanup_cancels_stale_deletion(self) -> None:
        anchor = NOW - timedelta(days=30)
        self.store.create_workload(workload(last_activity_at=anchor))
        planned = (
            self.worker()
            .reconcile_user(
                user_id=UserId("usr-1"),
                account_locked_at=None,
                holds=frozenset(),
                now=NOW,
            )
            .planned_actions[0]
        )

        current = self.store.get_workload(WorkloadId("wrk-1"))
        refreshed = replace(
            current,
            updated_at=NOW + timedelta(minutes=1),
            last_activity_at=NOW + timedelta(minutes=1),
            version=current.version + 1,
        )
        self.store.save_workload(refreshed, expected_version=current.version)

        executed = self.worker().execute_planned_action(
            planned,
            user_id=UserId("usr-1"),
            holds=frozenset(),
            account_locked_at=None,
            now=NOW + timedelta(minutes=1),
        )

        self.assertEqual(executed.action.state, LifecycleActionState.CANCELLED)
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.ACTIVE,
        )

    def test_schedule_execution_heartbeat_cancels_pending_inactivity_cleanup(
        self,
    ) -> None:
        anchor = NOW - timedelta(days=30)
        self.store.create_org_cost_guard(
            OrgCostGuard(
                evaluated_at=NOW,
                latest_usage_collected_at=NOW,
                emergency_stop=False,
                org_policy_cost_krw=0,
            )
        )
        self.store.create_workload(workload(last_activity_at=anchor))
        self.store.create_schedule(schedule())
        worker = self.worker()
        planned = worker.reconcile_user(
            user_id=UserId("usr-1"),
            account_locked_at=None,
            holds=frozenset(),
            now=NOW,
        ).planned_actions[0]

        tick_result = self._schedule_service().execute_schedule_tick(
            schedule_id="sch-1",
            workload_id="wrk-1",
            tick_at=NOW,
        )
        executed = worker.execute_planned_action(
            planned,
            user_id=UserId("usr-1"),
            holds=frozenset(),
            account_locked_at=None,
            now=NOW,
        )

        self.assertEqual(tick_result["outcome"], "succeeded")
        self.assertEqual(executed.action.state, LifecycleActionState.CANCELLED)
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.ACTIVE,
        )

    def test_cleanup_retry_is_idempotent_after_schedule_archive_partial_effect(
        self,
    ) -> None:
        anchor = NOW - timedelta(days=30)
        self.store.create_workload(
            workload(
                last_activity_at=anchor,
                state=WorkloadState.QUARANTINED,
            )
        )
        self.store.create_schedule(schedule())
        events: list[str] = []
        schedules = RecordingScheduleManager(events)
        compute = RecordingCompute(fail_first=True, events=events)
        worker = self.worker(compute=compute, schedules=schedules)
        planned = worker.reconcile_user(
            user_id=UserId("usr-1"),
            account_locked_at=None,
            holds=frozenset(),
            now=NOW,
        ).planned_actions[0]

        with self.assertRaises(RuntimeError):
            worker.execute_planned_action(
                planned,
                user_id=UserId("usr-1"),
                holds=frozenset(),
                account_locked_at=None,
                now=NOW,
            )
        self.assertEqual(
            self.store.get_schedule(ScheduleId("sch-1")).state,
            ScheduleState.ARCHIVED,
        )
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.QUARANTINED,
        )

        compute.fail_first = False
        executed = worker.execute_planned_action(
            planned,
            user_id=UserId("usr-1"),
            holds=frozenset(),
            account_locked_at=None,
            now=NOW + timedelta(minutes=1),
        )

        self.assertEqual(executed.action.state, LifecycleActionState.EXECUTED)
        self.assertEqual(len(compute.calls), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
