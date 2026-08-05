# ruff: noqa: E402, E501

from __future__ import annotations

import importlib
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = TEST_ROOT.parent / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import (
    LifecycleAction,
    LifecycleActionId,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    LifecycleActionKind,
    LifecycleActionState,
    ScheduleState,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.services.lifecycle import (
    PlannedLifecycleAction,
    plan_user_lifecycle,
)
from mim_control_plane.workers.lifecycle import (
    LifecycleActionExecutionResult,
    LifecycleReconcileResult,
)

NOW = datetime(2026, 8, 4, 0, 0, 0, tzinfo=UTC)


def user(
    *,
    user_id: str,
    state: UserState = UserState.ACTIVE,
    updated_at: datetime | None = None,
) -> User:
    anchor = NOW if updated_at is None else updated_at
    return User(
        id=UserId(user_id),
        email=f"{user_id}@madup.com",
        role=UserRole.USER,
        state=state,
        groups=frozenset({"mim-users"}),
        identity_synced_at=anchor - timedelta(days=1),
        created_at=anchor - timedelta(days=90),
        updated_at=anchor,
        version=1,
    )


def workload(
    *,
    workload_id: str,
    owner_id: str,
    state: WorkloadState = WorkloadState.ACTIVE,
    last_activity_at: datetime | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    version: int = 1,
) -> Workload:
    created = NOW - timedelta(days=90) if created_at is None else created_at
    updated = NOW - timedelta(days=1) if updated_at is None else updated_at
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name=workload_id,
        kind=WorkloadKind.NEXTJS,
        state=state,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-hash",
        created_at=created,
        updated_at=updated,
        last_activity_at=last_activity_at,
        version=version,
    )


def schedule(*, schedule_id: str, workload_id: str, owner_id: str) -> Schedule:
    return Schedule(
        id=ScheduleId(schedule_id),
        owner_id=UserId(owner_id),
        workload_id=WorkloadId(workload_id),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=ScheduleState.ENABLED,
        created_at=NOW - timedelta(days=90),
        updated_at=NOW - timedelta(days=1),
    )


def planned_warning(*, workload_id: str, eligible_at: datetime) -> PlannedLifecycleAction:
    return PlannedLifecycleAction(
        action=LifecycleAction(
            id=LifecycleActionId(f"life-{workload_id}-warning"),
            workload_id=WorkloadId(workload_id),
            kind=LifecycleActionKind.INACTIVITY_WARNING,
            state=LifecycleActionState.PLANNED,
            reason="23_days_inactive",
            eligible_at=eligible_at,
            observed_workload_version=1,
            created_at=NOW,
            updated_at=NOW,
            version=1,
        )
    )


@dataclass
class RecordingLifecycleWorker:
    reconcile_results: dict[str, LifecycleReconcileResult]
    reconcile_failures: dict[str, Exception]
    execute_result_kind: str = "executed"

    def __post_init__(self) -> None:
        self.reconcile_calls: list[tuple[UserId, datetime | None, frozenset[WorkloadId], datetime]] = []
        self.execute_calls: list[tuple[LifecycleActionId, UserId, frozenset[WorkloadId], datetime | None, datetime]] = []

    def reconcile_user(
        self,
        *,
        user_id: UserId,
        account_locked_at: datetime | None,
        holds: frozenset[WorkloadId],
        now: datetime,
    ) -> LifecycleReconcileResult:
        self.reconcile_calls.append((user_id, account_locked_at, holds, now))
        failure = self.reconcile_failures.get(str(user_id))
        if failure is not None:
            raise failure
        return self.reconcile_results[str(user_id)]

    def execute_planned_action(
        self,
        planned: PlannedLifecycleAction,
        *,
        user_id: UserId,
        holds: frozenset[WorkloadId],
        account_locked_at: datetime | None,
        now: datetime,
    ) -> LifecycleActionExecutionResult:
        self.execute_calls.append(
            (planned.action.id, user_id, holds, account_locked_at, now)
        )
        return LifecycleActionExecutionResult(
            kind=self.execute_result_kind,
            action=planned.action,
        )


class MaintenanceSweepTests(unittest.TestCase):
    def test_run_sorts_users_derives_locked_anchor_executes_actions_and_isolates_hold_failures(
        self,
    ) -> None:
        module = importlib.import_module("mim_control_plane.workers.maintenance")
        store = MemoryStore()
        locked_at = NOW - timedelta(days=2)
        store.create_user(user(user_id="usr-b", state=UserState.ACTIVE))
        store.create_user(user(user_id="usr-a", state=UserState.SUSPENDED, updated_at=locked_at))
        store.create_user(user(user_id="usr-c", state=UserState.ACTIVE))
        worker = RecordingLifecycleWorker(
            reconcile_results={
                "usr-a": LifecycleReconcileResult(
                    user=store.get_user(UserId("usr-a")),
                    planned_actions=(planned_warning(workload_id="wrk-a", eligible_at=NOW),),
                    persisted_action_ids=(LifecycleActionId("life-wrk-a-warning"),),
                ),
                "usr-b": LifecycleReconcileResult(
                    user=store.get_user(UserId("usr-b")),
                    planned_actions=(),
                    persisted_action_ids=(),
                ),
            },
            reconcile_failures={},
        )

        class HoldResolver:
            def __init__(self) -> None:
                self.calls: list[tuple[UserId, datetime]] = []

            def resolve_holds(
                self,
                *,
                user_id: UserId,
                now: datetime,
            ) -> object:
                self.calls.append((user_id, now))
                if user_id == UserId("usr-a"):
                    return frozenset({WorkloadId("wrk-hold")})
                if user_id == UserId("usr-c"):
                    return {"wrk-bad"}
                return frozenset()

        holds = HoldResolver()
        sweep = module.MaintenanceSweep(store=store, lifecycle=worker, hold_resolver=holds)

        result = sweep.run(now=NOW)

        self.assertEqual([str(user_id) for user_id, _ in holds.calls], ["usr-a", "usr-b", "usr-c"])
        self.assertEqual(
            [
                (str(user_id), account_locked_at, tuple(sorted(str(item) for item in hold_set)))
                for user_id, account_locked_at, hold_set, _ in worker.reconcile_calls
            ],
            [
                ("usr-a", locked_at, ("wrk-hold",)),
                ("usr-b", None, ()),
            ],
        )
        self.assertEqual(
            [
                (str(action_id), str(user_id), tuple(sorted(str(item) for item in hold_set)))
                for action_id, user_id, hold_set, _, _ in worker.execute_calls
            ],
            [("life-wrk-a-warning", "usr-a", ("wrk-hold",))],
        )
        self.assertEqual(result.processed_users, 2)
        self.assertEqual(result.failed_users, 1)
        self.assertEqual(result.replayed_users, 0)
        self.assertEqual(result.executed_actions, 1)
        self.assertEqual(result.noop_actions, 0)
        self.assertEqual(result.cancelled_actions, 0)

    def test_run_recovers_idempotent_replay_from_existing_planned_action_material(
        self,
    ) -> None:
        module = importlib.import_module("mim_control_plane.workers.maintenance")
        store = MemoryStore()
        store.create_user(user(user_id="usr-1"))
        target = workload(
            workload_id="wrk-1",
            owner_id="usr-1",
            last_activity_at=NOW - timedelta(days=23),
            updated_at=NOW - timedelta(days=23),
        )
        store.create_workload(target)
        store.create_schedule(schedule(schedule_id="sch-1", workload_id="wrk-1", owner_id="usr-1"))
        decision = plan_user_lifecycle(
            user=store.get_user(UserId("usr-1")),
            workloads=(target,),
            schedules=store.list_schedules(owner_id=UserId("usr-1")),
            holds=frozenset(),
            now=NOW,
        )
        planned = self.assert_singleton(decision.planned_actions)
        store.create_lifecycle_action(
            LifecycleAction(
                id=planned.action.id,
                workload_id=planned.action.workload_id,
                kind=planned.action.kind,
                state=LifecycleActionState.PLANNED,
                reason=planned.action.reason,
                eligible_at=planned.action.eligible_at,
                observed_workload_version=planned.action.observed_workload_version,
                created_at=NOW - timedelta(hours=1),
                updated_at=NOW - timedelta(hours=1),
                version=1,
            )
        )
        worker = RecordingLifecycleWorker(
            reconcile_results={},
            reconcile_failures={
                "usr-1": ValueError("stored lifecycle action conflicts with planned action.")
            },
        )

        class HoldResolver:
            def resolve_holds(
                self,
                *,
                user_id: UserId,
                now: datetime,
            ) -> frozenset[WorkloadId]:
                del user_id, now
                return frozenset()

        sweep = module.MaintenanceSweep(
            store=store,
            lifecycle=worker,
            hold_resolver=HoldResolver(),
        )

        result = sweep.run(now=NOW)

        self.assertEqual(result.processed_users, 1)
        self.assertEqual(result.failed_users, 0)
        self.assertEqual(result.replayed_users, 1)
        self.assertEqual(result.replayed_actions, 1)
        self.assertEqual(
            [str(action_id) for action_id, *_ in worker.execute_calls],
            [str(planned.action.id)],
        )

    @staticmethod
    def assert_singleton(values: tuple[PlannedLifecycleAction, ...]) -> PlannedLifecycleAction:
        if len(values) != 1:
            raise AssertionError(f"expected 1 planned action, got {len(values)}")
        return values[0]


if __name__ == "__main__":
    unittest.main()
