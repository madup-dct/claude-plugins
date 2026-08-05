from __future__ import annotations

import importlib
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = TEST_ROOT.parent / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.memory_store import MemoryStore  # noqa: E402
from mim_control_plane.domain.models import (  # noqa: E402
    AuditEvent,
    AuditEventId,
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
    RepositoryAdmissionState,
    ScheduleState,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)

NOW = datetime(2026, 8, 4, 4, 0, 0, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"


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
        id=RepositoryAdmissionId("adm-1"),
        repository_numeric_id=101,
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
    workload_id: str,
    owner_id: str,
    state: WorkloadState = WorkloadState.ACTIVE,
    version: int = 1,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id=RepositoryAdmissionId("adm-1"),
        name=workload_id,
        kind=WorkloadKind.NEXTJS,
        state=state,
        source_sha="b" * 40,
        desired_manifest_hash="manifest-1",
        created_at=NOW - timedelta(days=60),
        updated_at=NOW - timedelta(days=1),
        last_activity_at=NOW - timedelta(hours=1),
        version=version,
    )


def schedule(
    *,
    schedule_id: str,
    owner_id: str,
    workload_id: str,
    state: ScheduleState = ScheduleState.ENABLED,
    version: int = 1,
) -> Schedule:
    return Schedule(
        id=ScheduleId(schedule_id),
        owner_id=UserId(owner_id),
        workload_id=WorkloadId(workload_id),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=state,
        created_at=NOW - timedelta(days=20),
        updated_at=NOW - timedelta(days=1),
        version=version,
    )


@dataclass(frozen=True, slots=True)
class FakeAccessCall:
    workload_id: str
    expected_workload_version: int
    reason: str


class FakeWorkloadAccessEffects:
    def __init__(self) -> None:
        self.calls: tuple[FakeAccessCall, ...] = ()
        self.error: Exception | None = None

    def remove_owner_access(
        self,
        *,
        workload_id: WorkloadId,
        expected_workload_version: int,
        reason: str,
    ) -> None:
        self.calls = (
            *self.calls,
            FakeAccessCall(
                workload_id=str(workload_id),
                expected_workload_version=expected_workload_version,
                reason=reason,
            ),
        )
        if self.error is not None:
            raise self.error


@dataclass(frozen=True, slots=True)
class FakeScheduleCall:
    schedule_id: str
    workload_id: str
    target_state: str
    expected_schedule_version: int
    reason: str


class FakeScheduleEffects:
    def __init__(self) -> None:
        self.calls: tuple[FakeScheduleCall, ...] = ()
        self.error: Exception | None = None

    def apply_schedule_state(
        self,
        *,
        schedule_id: ScheduleId,
        workload_id: WorkloadId,
        target_state: ScheduleState,
        expected_schedule_version: int,
        reason: str,
    ) -> None:
        self.calls = (
            *self.calls,
            FakeScheduleCall(
                schedule_id=str(schedule_id),
                workload_id=str(workload_id),
                target_state=str(target_state),
                expected_schedule_version=expected_schedule_version,
                reason=reason,
            ),
        )
        if self.error is not None:
            raise self.error


class CostEnforcementAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module(
            "mim_control_plane.adapters.cost_enforcement"
        )
        self.store = MemoryStore()
        self.store.create_user(user(user_id="usr-1"))
        self.store.create_user(user(user_id="usr-2"))
        self.store.create_repository_admission(admission())
        self.access_effects = FakeWorkloadAccessEffects()
        self.schedule_effects = FakeScheduleEffects()

    def adapter(self) -> Any:
        return self.module.CostEnforcementAdapter(
            store=self.store,
            workload_access=self.access_effects,
            schedule_effects=self.schedule_effects,
            project_id=PROJECT_ID,
            clock=lambda: NOW,
        )

    def test_constructor_rejects_non_central_project_and_missing_dependencies(
        self,
    ) -> None:
        adapter_type = self.module.CostEnforcementAdapter
        with self.assertRaises(ValueError):
            adapter_type(
                store=self.store,
                workload_access=self.access_effects,
                schedule_effects=self.schedule_effects,
                project_id="other-project",
                clock=lambda: NOW,
            )
        with self.assertRaises(ValueError):
            adapter_type(
                store=self.store,
                workload_access=None,
                schedule_effects=self.schedule_effects,
                project_id=PROJECT_ID,
                clock=lambda: NOW,
            )
        with self.assertRaises(ValueError):
            adapter_type(
                store=self.store,
                workload_access=self.access_effects,
                schedule_effects=None,
                project_id=PROJECT_ID,
                clock=lambda: NOW,
            )

    def test_warn_and_block_new_record_redacted_audits_without_pausing(self) -> None:
        self.store.create_workload(workload(workload_id="wrk-1", owner_id="usr-1"))
        self.store.create_schedule(
            schedule(schedule_id="sch-1", owner_id="usr-1", workload_id="wrk-1")
        )
        adapter = self.adapter()

        adapter.enforce_user_policy(
            user_id=UserId("usr-1"),
            user_percent=70,
            warn=True,
            block_new=False,
            pause=False,
            basis_entry_ids=("bill-secret-1",),
            idempotency_key="user:usr-1:70:secret-token",
        )
        adapter.enforce_user_policy(
            user_id=UserId("usr-1"),
            user_percent=90,
            warn=True,
            block_new=True,
            pause=False,
            basis_entry_ids=("bill-secret-2",),
            idempotency_key="user:usr-1:90:secret-token",
        )

        self.assertEqual(self.access_effects.calls, ())
        self.assertEqual(self.schedule_effects.calls, ())
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.ACTIVE,
        )
        events = self.store.list_audit_events()
        self.assertEqual(
            tuple(sorted(event.action for event in events)),
            ("cost_policy_block_new", "cost_policy_warn"),
        )
        for event in events:
            self.assertNotIn("bill-secret", event.policy_decision)
            self.assertNotIn("secret-token", event.policy_decision)
            self.assertNotIn("secret-token", event.correlation_id)

    def test_pause_owner_workloads_and_schedules_and_exact_replay_is_idempotent(
        self,
    ) -> None:
        self.store.create_workload(workload(workload_id="wrk-1", owner_id="usr-1"))
        self.store.create_workload(
            workload(
                workload_id="wrk-2",
                owner_id="usr-1",
                state=WorkloadState.PAUSED,
            )
        )
        self.store.create_workload(
            workload(
                workload_id="wrk-3",
                owner_id="usr-1",
                state=WorkloadState.ARCHIVED,
            )
        )
        self.store.create_schedule(
            schedule(schedule_id="sch-1", owner_id="usr-1", workload_id="wrk-1")
        )
        self.store.create_schedule(
            schedule(
                schedule_id="sch-2",
                owner_id="usr-1",
                workload_id="wrk-2",
                state=ScheduleState.PAUSED,
            )
        )
        adapter = self.adapter()

        adapter.enforce_user_policy(
            user_id=UserId("usr-1"),
            user_percent=100,
            warn=True,
            block_new=True,
            pause=True,
            basis_entry_ids=("bill-1", "bill-2"),
            idempotency_key="user:usr-1:100:1000",
        )
        adapter.enforce_user_policy(
            user_id=UserId("usr-1"),
            user_percent=100,
            warn=True,
            block_new=True,
            pause=True,
            basis_entry_ids=("bill-1", "bill-2"),
            idempotency_key="user:usr-1:100:1000",
        )

        self.assertEqual(
            self.access_effects.calls,
            (
                FakeAccessCall(
                    workload_id="wrk-1",
                    expected_workload_version=1,
                    reason="user_cost_pause",
                ),
            ),
        )
        self.assertEqual(
            self.schedule_effects.calls,
            (
                FakeScheduleCall(
                    schedule_id="sch-1",
                    workload_id="wrk-1",
                    target_state="paused",
                    expected_schedule_version=1,
                    reason="user_cost_pause",
                ),
            ),
        )
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.PAUSED,
        )
        self.assertEqual(
            self.store.get_schedule(ScheduleId("sch-1")).state,
            ScheduleState.PAUSED,
        )
        self.assertEqual(
            len(self.store.list_audit_events()),
            3,
        )

    def test_org_emergency_stop_pauses_all_eligible_non_archived_resources(
        self,
    ) -> None:
        self.store.create_workload(workload(workload_id="wrk-1", owner_id="usr-1"))
        self.store.create_workload(
            workload(
                workload_id="wrk-2",
                owner_id="usr-2",
                state=WorkloadState.QUARANTINED,
            )
        )
        self.store.create_workload(
            workload(
                workload_id="wrk-3",
                owner_id="usr-2",
                state=WorkloadState.ARCHIVED,
            )
        )
        self.store.create_schedule(
            schedule(schedule_id="sch-1", owner_id="usr-1", workload_id="wrk-1")
        )
        self.store.create_schedule(
            schedule(schedule_id="sch-2", owner_id="usr-2", workload_id="wrk-2")
        )
        self.store.create_schedule(
            schedule(
                schedule_id="sch-3",
                owner_id="usr-2",
                workload_id="wrk-3",
                state=ScheduleState.DISABLED,
            )
        )
        adapter = self.adapter()

        adapter.enforce_org_policy(
            emergency_stop=True,
            basis_entry_ids=("org-bill-1", "org-bill-2"),
            idempotency_key="org:20000",
        )

        self.assertEqual(
            self.access_effects.calls,
            (
                FakeAccessCall(
                    workload_id="wrk-1",
                    expected_workload_version=1,
                    reason="org_emergency_stop",
                ),
            ),
        )
        self.assertEqual(
            self.schedule_effects.calls,
            (
                FakeScheduleCall(
                    schedule_id="sch-1",
                    workload_id="wrk-1",
                    target_state="paused",
                    expected_schedule_version=1,
                    reason="org_emergency_stop",
                ),
                FakeScheduleCall(
                    schedule_id="sch-2",
                    workload_id="wrk-2",
                    target_state="paused",
                    expected_schedule_version=1,
                    reason="org_emergency_stop",
                ),
            ),
        )
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.PAUSED,
        )
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-2")).state,
            WorkloadState.QUARANTINED,
        )
        self.assertEqual(
            self.store.get_schedule(ScheduleId("sch-1")).state,
            ScheduleState.PAUSED,
        )
        self.assertEqual(
            self.store.get_schedule(ScheduleId("sch-2")).state,
            ScheduleState.PAUSED,
        )

    def test_conflicting_audit_replay_is_rejected(self) -> None:
        self.store.create_workload(workload(workload_id="wrk-1", owner_id="usr-1"))
        adapter = self.adapter()
        module = cast(Any, self.module)
        original = module._audit_event_id
        module._audit_event_id = lambda *_args: AuditEventId("audit-conflict")
        try:
            self.store.append_audit_event(
                AuditEvent(
                    id=AuditEventId("audit-conflict"),
                    actor_id=None,
                    action="other_action",
                    target_ref="user:usr-1",
                    policy_decision="other",
                    before_ref=None,
                    after_ref=None,
                    correlation_id="other-correlation",
                    outcome="recorded",
                    occurred_at=NOW,
                )
            )
            with self.assertRaises(RuntimeError):
                adapter.enforce_user_policy(
                    user_id=UserId("usr-1"),
                    user_percent=70,
                    warn=True,
                    block_new=False,
                    pause=False,
                    basis_entry_ids=("bill-1",),
                    idempotency_key="user:usr-1:70:1",
                )
        finally:
            module._audit_event_id = original

    def test_exact_audit_replay_accepts_a_later_retry_time(self) -> None:
        times = iter((NOW, NOW + timedelta(hours=1)))
        adapter = self.module.CostEnforcementAdapter(
            store=self.store,
            workload_access=self.access_effects,
            schedule_effects=self.schedule_effects,
            project_id=PROJECT_ID,
            clock=lambda: next(times),
        )

        for _ in range(2):
            adapter.enforce_user_policy(
                user_id=UserId("usr-1"),
                user_percent=70,
                warn=True,
                block_new=False,
                pause=False,
                basis_entry_ids=("bill-1",),
                idempotency_key="user:usr-1:70:700",
            )

        self.assertEqual(len(self.store.list_audit_events()), 1)

    def test_effect_failure_is_fail_closed_and_does_not_persist_paused_state(
        self,
    ) -> None:
        self.store.create_workload(workload(workload_id="wrk-1", owner_id="usr-1"))
        self.store.create_schedule(
            schedule(schedule_id="sch-1", owner_id="usr-1", workload_id="wrk-1")
        )
        self.access_effects.error = RuntimeError("boom secret")
        adapter = self.adapter()

        with self.assertRaises(RuntimeError) as raised:
            adapter.enforce_user_policy(
                user_id=UserId("usr-1"),
                user_percent=100,
                warn=True,
                block_new=True,
                pause=True,
                basis_entry_ids=("bill-1",),
                idempotency_key="user:usr-1:100:1",
            )

        self.assertNotIn("boom secret", str(raised.exception))
        self.assertEqual(
            self.store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.ACTIVE,
        )
        self.assertEqual(
            self.store.get_schedule(ScheduleId("sch-1")).state,
            ScheduleState.ENABLED,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
