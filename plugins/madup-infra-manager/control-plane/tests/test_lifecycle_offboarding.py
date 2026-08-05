from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from typing import cast

from mim_control_plane.domain.models import (
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
    AdminDecisionKind,
    CleanupExecutionDecisionKind,
    ComputeTarget,
    ComputeTargetKind,
    plan_user_lifecycle,
    revalidate_cleanup_action,
)

NOW = datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC)


def user(
    *,
    state: UserState = UserState.ACTIVE,
    updated_at: datetime = NOW,
    version: int = 1,
) -> User:
    return User(
        id=UserId("usr-1"),
        email="person@madup.com",
        role=UserRole.USER,
        state=state,
        groups=frozenset({"mim-users"}),
        identity_synced_at=NOW,
        created_at=NOW - timedelta(days=60),
        updated_at=updated_at,
        version=version,
    )


def workload(
    *,
    workload_id: str = "wrk-1",
    kind: WorkloadKind = WorkloadKind.STREAMLIT,
    state: WorkloadState = WorkloadState.ACTIVE,
    created_at: datetime = NOW - timedelta(days=60),
    updated_at: datetime = NOW,
    last_activity_at: datetime | None = NOW - timedelta(days=1),
    version: int = 1,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId("usr-1"),
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name=workload_id,
        kind=kind,
        state=state,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-hash",
        created_at=created_at,
        updated_at=updated_at,
        last_activity_at=last_activity_at,
        version=version,
    )


def schedule(
    *,
    schedule_id: str = "sch-1",
    workload_id: str = "wrk-1",
    state: ScheduleState = ScheduleState.ENABLED,
) -> Schedule:
    return Schedule(
        id=ScheduleId(schedule_id),
        owner_id=UserId("usr-1"),
        workload_id=WorkloadId(workload_id),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=state,
        created_at=NOW - timedelta(days=60),
        updated_at=NOW,
    )


class LifecycleOffboardingTests(unittest.TestCase):
    def test_suspended_user_quarantines_workloads_disables_schedules_and_notifies_admin(
        self,
    ) -> None:
        suspended_at = NOW - timedelta(days=2)
        decision = plan_user_lifecycle(
            user=user(state=UserState.SUSPENDED, updated_at=suspended_at, version=3),
            workloads=(
                workload(workload_id="wrk-web", kind=WorkloadKind.NEXTJS),
                workload(
                    workload_id="wrk-job",
                    kind=WorkloadKind.SCHEDULED_SCRIPT,
                    state=WorkloadState.PAUSED,
                ),
            ),
            schedules=(schedule(workload_id="wrk-job"),),
            holds=frozenset(),
            account_locked_at=suspended_at,
            now=NOW,
        )

        self.assertEqual(
            {proposal.workload_id for proposal in decision.workload_transitions},
            {WorkloadId("wrk-web"), WorkloadId("wrk-job")},
        )
        self.assertTrue(
            all(
                proposal.target_state is WorkloadState.QUARANTINED
                for proposal in decision.workload_transitions
            )
        )
        self.assertEqual(len(decision.schedule_transitions), 1)
        self.assertEqual(
            decision.schedule_transitions[0].target_state,
            ScheduleState.DISABLED,
        )
        self.assertEqual(
            {item.kind for item in decision.admin_decisions},
            {
                AdminDecisionKind.NOTIFY_ADMIN,
                AdminDecisionKind.TRANSFER_WINDOW,
            },
        )
        self.assertEqual(decision.admin_decisions[0].user_id, UserId("usr-1"))
        self.assertEqual(decision.planned_actions, ())

    def test_offboard_cleanup_requires_quarantine_and_hits_exact_seven_day_boundary(
        self,
    ) -> None:
        locked_at = NOW - timedelta(days=7)
        quarantined = workload(
            kind=WorkloadKind.SCHEDULED_SCRIPT,
            state=WorkloadState.QUARANTINED,
            updated_at=locked_at,
            last_activity_at=NOW - timedelta(days=40),
        )
        before = plan_user_lifecycle(
            user=user(
                state=UserState.OFFBOARDED,
                updated_at=NOW - timedelta(days=1),
                version=4,
            ),
            workloads=(quarantined,),
            schedules=(schedule(workload_id="wrk-1"),),
            holds=frozenset(),
            account_locked_at=locked_at,
            now=locked_at + timedelta(days=7) - timedelta(seconds=1),
        )
        at_boundary = plan_user_lifecycle(
            user=user(
                state=UserState.OFFBOARDED,
                updated_at=NOW - timedelta(days=1),
                version=4,
            ),
            workloads=(quarantined,),
            schedules=(schedule(workload_id="wrk-1"),),
            holds=frozenset(),
            account_locked_at=locked_at,
            now=locked_at + timedelta(days=7),
        )

        self.assertEqual(before.planned_actions, ())
        self.assertEqual(len(at_boundary.planned_actions), 1)
        self.assertEqual(
            at_boundary.planned_actions[0].action.kind,
            LifecycleActionKind.DELETE_COMPUTE,
        )
        self.assertEqual(
            {target.kind for target in at_boundary.planned_actions[0].compute_targets},
            {
                ComputeTargetKind.CLOUD_RUN_JOB,
                ComputeTargetKind.CLOUD_SCHEDULER_JOB,
            },
        )

    def test_inactivity_boundaries_use_last_activity_else_created_at(self) -> None:
        anchor = NOW - timedelta(days=23)
        active_workload = workload(
            kind=WorkloadKind.STREAMLIT,
            last_activity_at=anchor,
            updated_at=anchor,
        )
        fallback_workload = workload(
            workload_id="wrk-2",
            kind=WorkloadKind.NEXTJS,
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
            last_activity_at=None,
        )

        before_warning = plan_user_lifecycle(
            user=user(),
            workloads=(active_workload,),
            schedules=(),
            holds=frozenset(),
            now=anchor + timedelta(days=23) - timedelta(seconds=1),
        )
        at_warning = plan_user_lifecycle(
            user=user(),
            workloads=(active_workload,),
            schedules=(),
            holds=frozenset(),
            now=anchor + timedelta(days=23),
        )
        before_cleanup = plan_user_lifecycle(
            user=user(),
            workloads=(fallback_workload,),
            schedules=(),
            holds=frozenset(),
            now=(
                fallback_workload.created_at
                + timedelta(days=30)
                - timedelta(seconds=1)
            ),
        )
        at_cleanup = plan_user_lifecycle(
            user=user(),
            workloads=(fallback_workload,),
            schedules=(),
            holds=frozenset(),
            now=fallback_workload.created_at + timedelta(days=30),
        )

        self.assertEqual(before_warning.planned_actions, ())
        self.assertEqual(len(at_warning.planned_actions), 1)
        self.assertEqual(
            at_warning.planned_actions[0].action.kind,
            LifecycleActionKind.INACTIVITY_WARNING,
        )
        self.assertEqual(len(before_cleanup.planned_actions), 1)
        self.assertEqual(
            before_cleanup.planned_actions[0].action.kind,
            LifecycleActionKind.INACTIVITY_WARNING,
        )
        self.assertEqual(len(at_cleanup.planned_actions), 1)
        self.assertEqual(
            at_cleanup.planned_actions[0].action.kind,
            LifecycleActionKind.DELETE_COMPUTE,
        )

    def test_holds_block_cleanup_planning_and_cancel_existing_cleanup(self) -> None:
        inactive_at = NOW - timedelta(days=30)
        current_workload = workload(
            kind=WorkloadKind.NEXTJS,
            last_activity_at=inactive_at,
            updated_at=inactive_at,
            version=2,
        )

        planned = plan_user_lifecycle(
            user=user(version=5),
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=None,
            now=inactive_at + timedelta(days=30),
        ).planned_actions[0]
        blocked = plan_user_lifecycle(
            user=user(version=5),
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset({current_workload.id}),
            account_locked_at=None,
            now=inactive_at + timedelta(days=30),
        )
        cancelled = revalidate_cleanup_action(
            planned,
            user=user(version=5),
            workload=current_workload,
            holds=frozenset({current_workload.id}),
            account_locked_at=None,
            now=inactive_at + timedelta(days=30),
        )

        self.assertEqual(blocked.planned_actions, ())
        self.assertEqual(cancelled.kind, CleanupExecutionDecisionKind.CANCEL)
        self.assertEqual(cancelled.compute_targets, ())
        self.assertEqual(cancelled.action.state, LifecycleActionState.CANCELLED)

    def test_cleanup_revalidation_rejects_stale_state_activity_and_reactivation(
        self,
    ) -> None:
        inactive_at = NOW - timedelta(days=30)
        current_user = user(updated_at=NOW - timedelta(days=60), version=2)
        current_workload = workload(
            last_activity_at=inactive_at,
            updated_at=inactive_at,
            version=3,
        )
        planned = plan_user_lifecycle(
            user=current_user,
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=None,
            now=inactive_at + timedelta(days=30),
        ).planned_actions[0]
        locked_at = NOW - timedelta(days=8)
        suspended_cleanup = plan_user_lifecycle(
            user=user(
                state=UserState.SUSPENDED,
                updated_at=NOW - timedelta(days=2),
                version=6,
            ),
            workloads=(
                workload(
                    state=WorkloadState.QUARANTINED,
                    updated_at=locked_at,
                    version=5,
                    last_activity_at=NOW - timedelta(days=40),
                ),
            ),
            schedules=(),
            holds=frozenset(),
            account_locked_at=locked_at,
            now=NOW,
        ).planned_actions[0]

        stale_cases = (
            (
                "reactivated",
                suspended_cleanup,
                user(state=UserState.ACTIVE, updated_at=NOW, version=7),
                workload(
                    state=WorkloadState.QUARANTINED,
                    updated_at=locked_at,
                    version=5,
                    last_activity_at=NOW - timedelta(days=40),
                ),
                locked_at,
            ),
            (
                "new_activity",
                planned,
                current_user,
                workload(
                    updated_at=NOW,
                    version=4,
                    last_activity_at=NOW,
                ),
                None,
            ),
            (
                "stale_user_version",
                planned,
                user(updated_at=NOW - timedelta(days=60), version=9),
                current_workload,
                None,
            ),
            (
                "stale_workload_state",
                planned,
                current_user,
                workload(
                    state=WorkloadState.PAUSED,
                    updated_at=inactive_at,
                    version=3,
                    last_activity_at=inactive_at,
                ),
                None,
            ),
        )

        for label, action, next_user, next_workload, lock_anchor in stale_cases:
            with self.subTest(label=label):
                decision = revalidate_cleanup_action(
                    action,
                    user=next_user,
                    workload=next_workload,
                    holds=frozenset(),
                    account_locked_at=lock_anchor,
                    now=NOW,
                )
                self.assertEqual(decision.kind, CleanupExecutionDecisionKind.CANCEL)
                self.assertEqual(decision.compute_targets, ())

    def test_cleanup_revalidation_executes_only_with_exact_observed_state(self) -> None:
        inactive_at = NOW - timedelta(days=30)
        current_user = user(updated_at=NOW - timedelta(days=60), version=2)
        current_workload = workload(
            kind=WorkloadKind.SCHEDULED_SCRIPT,
            state=WorkloadState.QUARANTINED,
            updated_at=NOW - timedelta(days=9),
            last_activity_at=inactive_at,
            version=3,
        )
        planned = plan_user_lifecycle(
            user=current_user,
            workloads=(current_workload,),
            schedules=(schedule(workload_id="wrk-1"),),
            holds=frozenset(),
            account_locked_at=None,
            now=inactive_at + timedelta(days=30),
        ).planned_actions[0]

        decision = revalidate_cleanup_action(
            planned,
            user=current_user,
            workload=current_workload,
            holds=frozenset(),
            account_locked_at=None,
            now=inactive_at + timedelta(days=30),
        )

        self.assertEqual(decision.kind, CleanupExecutionDecisionKind.EXECUTE)
        self.assertEqual(
            {target.kind for target in decision.compute_targets},
            {
                ComputeTargetKind.CLOUD_RUN_JOB,
                ComputeTargetKind.CLOUD_SCHEDULER_JOB,
            },
        )

    def test_compute_targets_are_typed_and_idempotent(self) -> None:
        inactive_at = NOW - timedelta(days=30)
        current = workload(
            kind=WorkloadKind.NEXTJS,
            last_activity_at=inactive_at,
            updated_at=inactive_at,
            version=4,
        )

        first = plan_user_lifecycle(
            user=user(version=3),
            workloads=(current,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=None,
            now=inactive_at + timedelta(days=30),
        ).planned_actions[0]
        second = plan_user_lifecycle(
            user=user(version=3),
            workloads=(current,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=None,
            now=inactive_at + timedelta(days=30),
        ).planned_actions[0]
        with self.assertRaises(ValueError):
            ComputeTarget(
                workload_id=current.id,
                kind="cloud_sql_instance",  # type: ignore[arg-type]
            )

        self.assertEqual(first.action.id, second.action.id)
        self.assertEqual(
            first.compute_targets,
            (
                ComputeTarget(
                    workload_id=current.id,
                    kind=ComputeTargetKind.CLOUD_RUN_SERVICE,
                ),
            ),
        )

    def test_locked_cleanup_uses_explicit_anchor_across_status_advance(self) -> None:
        locked_at = NOW - timedelta(days=7)
        current_workload = workload(
            kind=WorkloadKind.NEXTJS,
            state=WorkloadState.QUARANTINED,
            updated_at=locked_at,
            last_activity_at=NOW - timedelta(days=40),
            version=4,
        )

        before = plan_user_lifecycle(
            user=user(
                state=UserState.OFFBOARDED,
                updated_at=NOW - timedelta(days=1),
                version=8,
            ),
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=locked_at,
            now=NOW - timedelta(seconds=1),
        )
        at_boundary = plan_user_lifecycle(
            user=user(
                state=UserState.OFFBOARDED,
                updated_at=NOW - timedelta(days=1),
                version=8,
            ),
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=locked_at,
            now=NOW,
        )

        self.assertEqual(before.planned_actions, ())
        self.assertEqual(len(at_boundary.planned_actions), 1)
        self.assertEqual(at_boundary.planned_actions[0].action.eligible_at, NOW)

    def test_locked_cleanup_id_stays_stable_across_user_version_churn(self) -> None:
        locked_at = NOW - timedelta(days=8)
        current_workload = workload(
            kind=WorkloadKind.NEXTJS,
            state=WorkloadState.QUARANTINED,
            updated_at=locked_at,
            last_activity_at=NOW - timedelta(days=40),
            version=4,
        )
        suspended = plan_user_lifecycle(
            user=user(
                state=UserState.SUSPENDED,
                updated_at=NOW - timedelta(days=2),
                version=6,
            ),
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=locked_at,
            now=NOW,
        ).planned_actions[0]
        offboarded = plan_user_lifecycle(
            user=user(
                state=UserState.OFFBOARDED,
                updated_at=NOW - timedelta(days=1),
                version=9,
            ),
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=locked_at,
            now=NOW,
        ).planned_actions[0]

        self.assertEqual(suspended.action.id, offboarded.action.id)
        cancelled = revalidate_cleanup_action(
            suspended,
            user=user(
                state=UserState.OFFBOARDED,
                updated_at=NOW - timedelta(days=1),
                version=9,
            ),
            workload=current_workload,
            holds=frozenset(),
            account_locked_at=locked_at,
            now=NOW,
        )
        self.assertEqual(cancelled.kind, CleanupExecutionDecisionKind.CANCEL)
        self.assertEqual(cancelled.compute_targets, ())

    def test_locked_cleanup_denies_missing_future_or_non_utc_anchor(self) -> None:
        current_workload = workload(
            kind=WorkloadKind.NEXTJS,
            state=WorkloadState.QUARANTINED,
            updated_at=NOW - timedelta(days=8),
            last_activity_at=NOW - timedelta(days=40),
            version=4,
        )

        missing = plan_user_lifecycle(
            user=user(state=UserState.SUSPENDED, updated_at=NOW - timedelta(days=1)),
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=None,
            now=NOW,
        )
        future = plan_user_lifecycle(
            user=user(state=UserState.SUSPENDED, updated_at=NOW - timedelta(days=1)),
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=NOW + timedelta(seconds=1),
            now=NOW,
        )
        non_utc = plan_user_lifecycle(
            user=user(state=UserState.SUSPENDED, updated_at=NOW - timedelta(days=1)),
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=NOW.replace(tzinfo=None),  # type: ignore[arg-type]
            now=NOW,
        )

        self.assertEqual(missing.planned_actions, ())
        self.assertEqual(future.planned_actions, ())
        self.assertEqual(non_utc.planned_actions, ())

    def test_locked_cleanup_cancels_on_anchor_owner_timestamp_or_activity_drift(
        self,
    ) -> None:
        locked_at = NOW - timedelta(days=8)
        current_user = user(
            state=UserState.OFFBOARDED,
            updated_at=NOW - timedelta(days=1),
            version=7,
        )
        current_workload = workload(
            state=WorkloadState.QUARANTINED,
            updated_at=locked_at,
            last_activity_at=NOW - timedelta(days=40),
            version=6,
        )
        planned = plan_user_lifecycle(
            user=current_user,
            workloads=(current_workload,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=locked_at,
            now=NOW,
        ).planned_actions[0]

        stale_cases = (
            (
                "user_swap",
                user(
                    state=UserState.OFFBOARDED,
                    updated_at=NOW - timedelta(days=1),
                    version=7,
                ).__class__(
                    id=UserId("usr-2"),
                    email="other@madup.com",
                    role=UserRole.USER,
                    state=UserState.OFFBOARDED,
                    groups=frozenset({"mim-users"}),
                    identity_synced_at=NOW,
                    created_at=NOW - timedelta(days=60),
                    updated_at=NOW - timedelta(days=1),
                    version=7,
                ),
                current_workload,
                locked_at,
            ),
            (
                "owner_transfer",
                current_user,
                workload(
                    state=WorkloadState.QUARANTINED,
                    updated_at=locked_at,
                    last_activity_at=NOW - timedelta(days=40),
                    version=6,
                ).__class__(
                    id=WorkloadId("wrk-1"),
                    owner_id=UserId("usr-2"),
                    repository_admission_id=RepositoryAdmissionId("repo-1"),
                    name="wrk-1",
                    kind=WorkloadKind.STREAMLIT,
                    state=WorkloadState.QUARANTINED,
                    source_sha="a" * 40,
                    desired_manifest_hash="manifest-hash",
                    created_at=NOW - timedelta(days=60),
                    updated_at=locked_at,
                    last_activity_at=NOW - timedelta(days=40),
                    version=6,
                ),
                locked_at,
            ),
            (
                "timestamp_only_drift",
                current_user,
                workload(
                    state=WorkloadState.QUARANTINED,
                    updated_at=locked_at + timedelta(seconds=1),
                    last_activity_at=NOW - timedelta(days=40),
                    version=6,
                ),
                locked_at,
            ),
            (
                "activity_change",
                current_user,
                workload(
                    state=WorkloadState.QUARANTINED,
                    updated_at=locked_at,
                    last_activity_at=NOW - timedelta(days=1),
                    version=6,
                ),
                locked_at,
            ),
            (
                "anchor_change",
                current_user,
                current_workload,
                locked_at + timedelta(seconds=1),
            ),
        )

        for label, next_user, next_workload, next_anchor in stale_cases:
            with self.subTest(label=label):
                decision = revalidate_cleanup_action(
                    planned,
                    user=next_user,
                    workload=next_workload,
                    holds=frozenset(),
                    account_locked_at=next_anchor,
                    now=NOW,
                )
                self.assertEqual(decision.kind, CleanupExecutionDecisionKind.CANCEL)
                self.assertEqual(decision.compute_targets, ())

    def test_malformed_holds_and_archived_workloads_deny_cleanup(self) -> None:
        inactive_at = NOW - timedelta(days=30)
        active_candidate = workload(
            kind=WorkloadKind.NEXTJS,
            state=WorkloadState.ACTIVE,
            updated_at=inactive_at,
            last_activity_at=inactive_at,
            version=2,
        )
        archived_candidate = workload(
            workload_id="wrk-archived",
            kind=WorkloadKind.NEXTJS,
            state=WorkloadState.ARCHIVED,
            updated_at=NOW - timedelta(days=8),
            last_activity_at=NOW - timedelta(days=40),
            version=3,
        )

        malformed_cases = (
            ("not_frozenset", (active_candidate.id,)),  # type: ignore[arg-type]
            ("empty_string", frozenset({WorkloadId("")})),
            ("whitespace", frozenset({"   "})),  # type: ignore[arg-type]
            ("int_value", frozenset({1})),  # type: ignore[arg-type]
            ("bool_value", frozenset({True})),  # type: ignore[arg-type]
        )
        for label, holds in malformed_cases:
            with self.subTest(label=label):
                decision = plan_user_lifecycle(
                    user=user(version=5),
                    workloads=(active_candidate,),
                    schedules=(),
                    holds=cast(frozenset[WorkloadId], holds),
                    account_locked_at=None,
                    now=inactive_at + timedelta(days=30),
                )
                self.assertEqual(decision.planned_actions, ())

        active_decision = plan_user_lifecycle(
            user=user(version=5),
            workloads=(archived_candidate,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=None,
            now=inactive_at + timedelta(days=30),
        )
        locked_decision = plan_user_lifecycle(
            user=user(
                state=UserState.OFFBOARDED,
                updated_at=NOW - timedelta(days=1),
                version=5,
            ),
            workloads=(archived_candidate,),
            schedules=(),
            holds=frozenset(),
            account_locked_at=NOW - timedelta(days=8),
            now=NOW,
        )

        self.assertEqual(active_decision.planned_actions, ())
        self.assertEqual(locked_decision.planned_actions, ())


if __name__ == "__main__":
    unittest.main()
