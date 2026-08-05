from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from mim_control_plane.adapters.fake_schedule import (
    FakeScheduleControlPort,
    FakeScheduleRunDispatcher,
)
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.config import TARGET_MONTHLY_BUDGET_KRW
from mim_control_plane.domain.models import (
    DeploymentPlanId,
    OperationId,
    OrgCostGuard,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    UsageEntry,
    UsageEntryId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    ActivityOutcome,
    ActivitySurface,
    OperationState,
    ScheduleState,
    UsageConfidence,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.store import VersionConflict
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.services.schedule_management import (
    ScheduleDenied,
    ScheduleManagementService,
)
from mim_control_plane.services.usage import ActivityAction

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


def seed_org_cost_guard(
    store: MemoryStore,
    *,
    evaluated_at: datetime = NOW,
) -> None:
    store.create_org_cost_guard(
        OrgCostGuard(
            evaluated_at=evaluated_at,
            latest_usage_collected_at=evaluated_at,
            emergency_stop=False,
            org_policy_cost_krw=0,
        )
    )


def principal(
    *,
    user_id: str = "usr-1",
    role: UserRole = UserRole.USER,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=UserId(user_id),
        email=f"{user_id}@madup.com",
        role=role,
    )


def user(
    *,
    user_id: str = "usr-1",
    state: UserState = UserState.ACTIVE,
    role: UserRole = UserRole.USER,
) -> User:
    return User(
        id=UserId(user_id),
        email=f"{user_id}@madup.com",
        role=role,
        state=state,
        groups=frozenset({"mim-users"}),
        identity_synced_at=NOW,
        created_at=NOW - timedelta(days=10),
        updated_at=NOW,
    )


def workload(
    *,
    workload_id: str = "wrk-1",
    owner_id: str = "usr-1",
    kind: WorkloadKind = WorkloadKind.SCHEDULED_SCRIPT,
    state: WorkloadState = WorkloadState.ACTIVE,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name="hourly-batch",
        kind=kind,
        state=state,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-hash",
        created_at=NOW - timedelta(days=5),
        updated_at=NOW,
        last_activity_at=NOW - timedelta(hours=1),
    )


def schedule(
    *,
    schedule_id: str = "sch-1",
    owner_id: str = "usr-1",
    workload_id: str = "wrk-1",
    state: ScheduleState = ScheduleState.ENABLED,
    consecutive_failures: int = 0,
    last_attempt_at: datetime | None = None,
    last_success_at: datetime | None = None,
) -> Schedule:
    return Schedule(
        id=ScheduleId(schedule_id),
        owner_id=UserId(owner_id),
        workload_id=WorkloadId(workload_id),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=state,
        created_at=NOW,
        updated_at=NOW,
        consecutive_failures=consecutive_failures,
        last_attempt_at=last_attempt_at,
        last_success_at=last_success_at,
    )


def usage_entry(
    *,
    entry_id: str = "use-1",
    owner_id: str = "usr-1",
    estimated_cost_krw: int = 0,
    finalized_cost_krw: int | None = None,
) -> UsageEntry:
    return UsageEntry(
        id=UsageEntryId(entry_id),
        owner_id=UserId(owner_id),
        workload_id=WorkloadId("wrk-1"),
        service_category="cloud_run",
        estimated_cost_krw=estimated_cost_krw,
        finalized_cost_krw=finalized_cost_krw,
        confidence=UsageConfidence.ESTIMATED,
        collected_at=NOW,
    )


class RecordingUsageScopeStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.usage_owner_ids: list[UserId | None] = []

    def list_usage_entries(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[UsageEntry, ...]:
        self.usage_owner_ids.append(owner_id)
        return super().list_usage_entries(owner_id=owner_id)


class RecordingHeartbeatStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.saved_activity_at: list[datetime | None] = []
        self.fail_save = False
        self.conflict_refresh: Workload | None = None

    def save_workload(  # type: ignore[override]
        self,
        workload: Workload,
        *,
        expected_version: int,
    ) -> Workload:
        self.saved_activity_at.append(workload.last_activity_at)
        if self.fail_save:
            raise RuntimeError("synthetic heartbeat failure")
        if self.conflict_refresh is not None:
            refresh = self.conflict_refresh
            self.conflict_refresh = None
            current = self.get_workload(refresh.id)
            MemoryStore.save_workload(
                self,
                refresh,
                expected_version=current.version,
            )
            raise VersionConflict("synthetic heartbeat conflict")
        return super().save_workload(workload, expected_version=expected_version)


class ScheduleManagementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(user())
        seed_org_cost_guard(self.store)
        self.store.create_workload(workload())
        self.scheduler = FakeScheduleControlPort()
        self.dispatcher = FakeScheduleRunDispatcher()
        self.service = ScheduleManagementService(
            store=self.store,
            scheduler=self.scheduler,
            dispatcher=self.dispatcher,
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

    def _id_factory(self, prefix: str) -> str:
        counters = getattr(self, "_counters", {})
        next_value = counters.get(prefix, 0) + 1
        counters[prefix] = next_value
        self._counters = counters
        return f"{prefix}-{next_value}"

    def test_plan_schedule_returns_exact_hourly_review_for_owned_script(self) -> None:
        reviewed = self.service.plan_schedule(
            principal=principal(),
            workload_id="wrk-1",
        )

        self.assertEqual(reviewed["action"], "plan_schedule")
        self.assertEqual(reviewed["status"], "ready")
        self.assertEqual(reviewed["workload_id"], "wrk-1")
        self.assertEqual(
            reviewed["policy"],
            {"cron": "0 * * * *", "timezone": "Asia/Seoul"},
        )
        stored = self.store.get_deployment_plan(DeploymentPlanId(reviewed["plan_id"]))
        self.assertEqual(stored.action, "create_schedule")
        self.assertEqual(stored.actor_id, UserId("usr-1"))

    def test_create_schedule_from_plan_persists_retry_safe_cloud_state(
        self,
    ) -> None:
        reviewed = self.service.plan_schedule(
            principal=principal(),
            workload_id="wrk-1",
        )
        self.scheduler.ensure_error = RuntimeError("temporary")

        with self.assertRaises(ScheduleDenied):
            self.service.create_schedule_from_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="schedule-idem-1",
            )

        persisted_schedule = self.store.list_schedules(owner_id=UserId("usr-1"))
        self.assertEqual(len(persisted_schedule), 1)
        persisted_operations = (self.store.get_operation(OperationId("operation-1")),)
        self.assertEqual(persisted_operations[0].state, OperationState.QUEUED)
        self.assertEqual(len(self.scheduler.ensure_calls), 1)

        self.scheduler.ensure_error = None
        replay = self.service.create_schedule_from_plan(
            principal=principal(),
            plan_id=reviewed["plan_id"],
            plan_hash=reviewed["plan_hash"],
            idempotency_key="schedule-idem-1",
        )

        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["schedule_id"], str(persisted_schedule[0].id))
        self.assertEqual(len(self.scheduler.ensure_calls), 2)
        self.assertEqual(len(self.store.list_schedules(owner_id=UserId("usr-1"))), 1)

    def test_create_schedule_from_plan_blocks_on_schedule_limit_and_cost_pause(
        self,
    ) -> None:
        reviewed = self.service.plan_schedule(
            principal=principal(),
            workload_id="wrk-1",
        )
        for index in range(3):
            self.store.create_schedule(
                schedule(
                    schedule_id=f"sch-existing-{index}",
                    workload_id=f"wrk-existing-{index}",
                )
            )

        with self.assertRaises(ScheduleDenied):
            self.service.create_schedule_from_plan(
                principal=principal(),
                plan_id=reviewed["plan_id"],
                plan_hash=reviewed["plan_hash"],
                idempotency_key="schedule-idem-limit",
            )

        limited_store = MemoryStore()
        limited_store.create_user(user())
        seed_org_cost_guard(limited_store)
        limited_store.create_workload(workload())
        limited_store.append_usage_entry(
            usage_entry(
                estimated_cost_krw=1000,
                finalized_cost_krw=1000,
            )
        )
        expensive = ScheduleManagementService(
            store=limited_store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=lambda prefix: f"{prefix}-fixed",
            lease_token_factory=lambda: "lease-fixed",
        )
        with self.assertRaises(ScheduleDenied):
            expensive.plan_schedule(principal=principal(), workload_id="wrk-1")

    def test_plan_schedule_uses_owner_scoped_usage_entries_for_cost_checks(
        self,
    ) -> None:
        store = RecordingUsageScopeStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        store.create_workload(workload())
        service = ScheduleManagementService(
            store=store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

        reviewed = service.plan_schedule(principal=principal(), workload_id="wrk-1")

        self.assertEqual(reviewed["status"], "ready")
        self.assertEqual(store.usage_owner_ids, [UserId("usr-1")])

    def test_plan_schedule_fails_closed_when_org_guard_is_missing(self) -> None:
        store = RecordingUsageScopeStore()
        store.create_user(user())
        store.create_workload(workload())
        service = ScheduleManagementService(
            store=store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

        with self.assertRaises(ScheduleDenied):
            service.plan_schedule(principal=principal(), workload_id="wrk-1")

        self.assertEqual(store.usage_owner_ids, [])

    def test_pause_and_resume_enforce_owner_scope_and_apply_exact_scheduler_state(
        self,
    ) -> None:
        current = self.store.create_schedule(schedule())

        paused = self.service.pause_schedule(
            principal=principal(),
            schedule_id=str(current.id),
        )

        self.assertEqual(paused["state"], "paused")
        self.assertEqual(self.scheduler.pause_calls, (str(current.id),))
        resumed = self.service.resume_schedule(
            principal=principal(),
            schedule_id=str(current.id),
        )
        self.assertEqual(resumed["state"], "enabled")
        self.assertEqual(self.scheduler.resume_calls, (str(current.id),))

        with self.assertRaises(ScheduleDenied):
            self.service.pause_schedule(
                principal=principal(user_id="usr-2"),
                schedule_id=str(current.id),
            )

    def test_execute_schedule_tick_replays_same_hour_and_records_success_activity(
        self,
    ) -> None:
        current = self.store.create_schedule(schedule())
        tick = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)

        first = self.service.execute_schedule_tick(
            schedule_id=str(current.id),
            workload_id="wrk-1",
            tick_at=tick,
        )
        replay = self.service.execute_schedule_tick(
            schedule_id=str(current.id),
            workload_id="wrk-1",
            tick_at=tick,
        )

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.dispatcher.calls), 1)
        events = self.store.list_activity_events(user_id=UserId("usr-1"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, ActivityAction.SCHEDULE_RUN)
        self.assertEqual(events[0].surface, ActivitySurface.WORKER)
        self.assertEqual(events[0].outcome, ActivityOutcome.SUCCEEDED)

    def test_execute_schedule_tick_disables_after_third_failure_with_sanitized_error(
        self,
    ) -> None:
        self.store.create_schedule(schedule(consecutive_failures=2))
        self.dispatcher.dispatch_error = RuntimeError("secret-token-should-not-leak")

        result = self.service.execute_schedule_tick(
            schedule_id="sch-1",
            workload_id="wrk-1",
            tick_at=datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
        )

        self.assertEqual(result["state"], "disabled")
        self.assertEqual(result["outcome"], "failed")
        self.assertNotIn("secret-token-should-not-leak", result["reason"])
        updated = self.store.get_schedule(ScheduleId("sch-1"))
        self.assertEqual(updated.state, ScheduleState.DISABLED)
        self.assertEqual(updated.consecutive_failures, 3)
        events = self.store.list_activity_events(user_id=UserId("usr-1"))
        self.assertEqual(events[-1].outcome, ActivityOutcome.FAILED)

    def test_execute_schedule_tick_updates_workload_activity_for_success_and_failure(
        self,
    ) -> None:
        tick = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)

        success_store = RecordingHeartbeatStore()
        success_store.create_user(user())
        seed_org_cost_guard(success_store)
        success_store.create_workload(
            replace(
                workload(),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        success_store.create_schedule(schedule())
        success = ScheduleManagementService(
            store=success_store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

        success_result = success.execute_schedule_tick(
            schedule_id="sch-1",
            workload_id="wrk-1",
            tick_at=tick,
        )

        self.assertEqual(success_result["outcome"], "succeeded")
        self.assertEqual(success_store.saved_activity_at, [tick])
        self.assertEqual(
            success_store.get_workload(WorkloadId("wrk-1")).last_activity_at,
            tick,
        )

        failure_store = RecordingHeartbeatStore()
        failure_store.create_user(user())
        seed_org_cost_guard(failure_store)
        failure_store.create_workload(
            replace(
                workload(),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        failure_store.create_schedule(schedule())
        failing_dispatcher = FakeScheduleRunDispatcher()
        failing_dispatcher.dispatch_error = RuntimeError("worker failed")
        failure = ScheduleManagementService(
            store=failure_store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=failing_dispatcher,
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

        failure_result = failure.execute_schedule_tick(
            schedule_id="sch-1",
            workload_id="wrk-1",
            tick_at=tick,
        )

        self.assertEqual(failure_result["outcome"], "failed")
        self.assertEqual(failure_store.saved_activity_at, [tick])
        self.assertEqual(
            failure_store.get_workload(WorkloadId("wrk-1")).last_activity_at,
            tick,
        )

    def test_execute_schedule_tick_heartbeat_failure_emits_sanitized_signal(
        self,
    ) -> None:
        tick = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
        store = RecordingHeartbeatStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        store.create_workload(
            replace(
                workload(),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        store.create_schedule(schedule())
        store.fail_save = True
        service = ScheduleManagementService(
            store=store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

        result = service.execute_schedule_tick(
            schedule_id="sch-1",
            workload_id="wrk-1",
            tick_at=tick,
        )

        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(store.saved_activity_at, [tick])
        events = store.list_audit_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "heartbeat_write_failed")
        self.assertEqual(events[0].target_ref, "schedule_worker")
        self.assertEqual(events[0].policy_decision, "best_effort_suppressed")
        signal_text = "|".join(
            (
                str(events[0].id),
                events[0].action,
                events[0].target_ref,
                events[0].policy_decision,
                events[0].correlation_id,
            )
        )
        for forbidden in ("usr-1", "wrk-1", "usr-1@madup.com", "token"):
            self.assertNotIn(forbidden, signal_text)

    def test_schedule_simultaneous_heartbeat_failures_persist_distinct_signals(
        self,
    ) -> None:
        tick = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
        store = RecordingHeartbeatStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        store.create_workload(
            replace(
                workload(),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        store.create_workload(
            replace(
                workload(workload_id="wrk-2"),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        store.create_schedule(schedule())
        store.create_schedule(schedule(schedule_id="sch-2", workload_id="wrk-2"))
        store.fail_save = True
        service = ScheduleManagementService(
            store=store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

        first = service.execute_schedule_tick(
            schedule_id="sch-1",
            workload_id="wrk-1",
            tick_at=tick,
        )
        second = service.execute_schedule_tick(
            schedule_id="sch-2",
            workload_id="wrk-2",
            tick_at=tick,
        )

        self.assertEqual(first["outcome"], "succeeded")
        self.assertEqual(second["outcome"], "succeeded")
        events = store.list_audit_events()
        self.assertEqual(len(events), 2)
        self.assertEqual({event.action for event in events}, {"heartbeat_write_failed"})
        self.assertEqual({event.target_ref for event in events}, {"schedule_worker"})
        self.assertEqual(
            {event.policy_decision for event in events},
            {"best_effort_suppressed"},
        )
        self.assertEqual(len({event.id for event in events}), 2)
        self.assertEqual(len({event.correlation_id for event in events}), 2)
        for event in events:
            signal_text = "|".join(
                (
                    str(event.id),
                    event.action,
                    event.target_ref,
                    event.policy_decision,
                    event.correlation_id,
                )
            )
            for forbidden in ("sch-1", "sch-2", "wrk-1", "wrk-2"):
                self.assertNotIn(forbidden, signal_text)

    def test_execute_schedule_tick_skips_workload_heartbeat_for_replay_and_denial(
        self,
    ) -> None:
        tick = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)

        replay_store = RecordingHeartbeatStore()
        replay_store.create_user(user())
        seed_org_cost_guard(replay_store)
        replay_store.create_workload(workload())
        replay_store.create_schedule(schedule())
        replay_service = ScheduleManagementService(
            store=replay_store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

        first = replay_service.execute_schedule_tick(
            schedule_id="sch-1",
            workload_id="wrk-1",
            tick_at=tick,
        )
        replay = replay_service.execute_schedule_tick(
            schedule_id="sch-1",
            workload_id="wrk-1",
            tick_at=tick,
        )

        self.assertEqual(first["outcome"], "succeeded")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay_store.saved_activity_at, [tick])

        denied_store = RecordingHeartbeatStore()
        denied_store.create_user(user())
        seed_org_cost_guard(denied_store)
        denied_store.create_workload(
            replace(
                workload(),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        denied_store.create_schedule(schedule())
        denied_store.append_usage_entry(
            usage_entry(
                estimated_cost_krw=TARGET_MONTHLY_BUDGET_KRW,
                finalized_cost_krw=TARGET_MONTHLY_BUDGET_KRW,
            )
        )
        denied_service = ScheduleManagementService(
            store=denied_store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

        denied = denied_service.execute_schedule_tick(
            schedule_id="sch-1",
            workload_id="wrk-1",
            tick_at=tick,
        )

        self.assertEqual(denied["outcome"], "denied")
        self.assertEqual(denied_store.saved_activity_at, [])
        self.assertEqual(
            denied_store.get_workload(WorkloadId("wrk-1")).last_activity_at,
            NOW - timedelta(hours=2),
        )

    def test_execute_schedule_tick_conflict_does_not_retry_into_failed_state(
        self,
    ) -> None:
        tick = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
        store = RecordingHeartbeatStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        current = store.create_workload(
            replace(
                workload(),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        store.create_schedule(schedule())
        store.conflict_refresh = replace(
            current,
            state=WorkloadState.FAILED,
            updated_at=tick,
            version=current.version + 1,
        )
        service = ScheduleManagementService(
            store=store,
            scheduler=FakeScheduleControlPort(),
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-fixed",
        )

        result = service.execute_schedule_tick(
            schedule_id="sch-1",
            workload_id="wrk-1",
            tick_at=tick,
        )

        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(store.saved_activity_at, [tick])
        self.assertEqual(
            store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.FAILED,
        )
        self.assertEqual(
            store.get_workload(WorkloadId("wrk-1")).last_activity_at,
            NOW - timedelta(hours=2),
        )
        self.assertEqual(store.list_audit_events(), ())


if __name__ == "__main__":
    unittest.main()
