from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, date, datetime, timedelta, timezone

from mim_control_plane.domain.models import (
    ActivityEvent,
    ActivityEventId,
    AuditEvent,
    AuditEventId,
    DailyUsageAggregate,
    DeploymentPlan,
    DeploymentPlanId,
    LifecycleAction,
    LifecycleActionId,
    Operation,
    OperationId,
    OriginRequestClaim,
    OriginRequestId,
    RepositoryAdmission,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    SecretId,
    SecretMetadata,
    UsageEntry,
    UsageEntryId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    OPERATION_TRANSITIONS,
    ActivityOutcome,
    ActivitySurface,
    InvalidTransition,
    LifecycleActionKind,
    LifecycleActionState,
    OperationState,
    PlanState,
    RepositoryAdmissionState,
    ScheduleState,
    SecretLifecycleState,
    SecretRotationState,
    UsageConfidence,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)

NOW = datetime(2026, 8, 2, 1, 2, 3, tzinfo=UTC)


class DomainModelTests(unittest.TestCase):
    def test_workload_record_activity_refreshes_same_state_exactly_once(self) -> None:
        workload = Workload(
            id=WorkloadId("wrk-1"),
            owner_id=UserId("usr-1"),
            repository_admission_id=RepositoryAdmissionId("repo-1"),
            name="sample-app",
            kind=WorkloadKind.STREAMLIT,
            state=WorkloadState.ACTIVE,
            source_sha="a" * 40,
            desired_manifest_hash="manifest-hash",
            created_at=NOW - timedelta(days=1),
            updated_at=NOW,
            last_activity_at=NOW - timedelta(hours=2),
        )

        refreshed = workload.record_activity(at=NOW + timedelta(minutes=1))

        self.assertEqual(refreshed.state, WorkloadState.ACTIVE)
        self.assertEqual(refreshed.last_activity_at, NOW + timedelta(minutes=1))
        self.assertEqual(refreshed.updated_at, NOW + timedelta(minutes=1))
        self.assertEqual(refreshed.version, workload.version + 1)
        self.assertEqual(workload.last_activity_at, NOW - timedelta(hours=2))

        with self.assertRaisesRegex(ValueError, "transition time"):
            workload.record_activity(at=NOW - timedelta(seconds=1))

    def test_workload_auto_deploy_policy_and_source_advance_are_explicit(self) -> None:
        workload = Workload(
            id=WorkloadId("wrk-auto"),
            owner_id=UserId("usr-1"),
            repository_admission_id=RepositoryAdmissionId("repo-old"),
            name="sample-app",
            kind=WorkloadKind.STREAMLIT,
            state=WorkloadState.ACTIVE,
            source_sha="a" * 40,
            desired_manifest_hash="manifest-old",
            created_at=NOW,
            updated_at=NOW,
            auto_deploy_enabled=True,
            auto_deploy_ref="refs/heads/main",
        )

        advanced = workload.advance_source(
            repository_admission_id=RepositoryAdmissionId("repo-new"),
            source_sha="b" * 40,
            desired_manifest_hash="manifest-new",
            at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(advanced.repository_admission_id, "repo-new")
        self.assertEqual(advanced.source_sha, "b" * 40)
        self.assertEqual(advanced.desired_manifest_hash, "manifest-new")
        self.assertEqual(advanced.version, workload.version + 1)
        self.assertEqual(advanced.auto_deploy_ref, "refs/heads/main")
        self.assertTrue(advanced.auto_deploy_enabled)
        self.assertEqual(workload.repository_admission_id, "repo-old")

        invalid_policies = (
            {"auto_deploy_enabled": True, "auto_deploy_ref": None},
            {"auto_deploy_enabled": False, "auto_deploy_ref": "refs/heads/main"},
            {"auto_deploy_enabled": True, "auto_deploy_ref": "refs/heads/main..bad"},
            {"auto_deploy_enabled": 1, "auto_deploy_ref": None},
        )
        for changes in invalid_policies:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    dataclasses.replace(workload, **changes)

        for bad_sha in ("a" * 40, "0" * 40, "A" * 40, "short"):
            with self.subTest(bad_sha=bad_sha):
                with self.assertRaises(ValueError):
                    workload.advance_source(
                        repository_admission_id=RepositoryAdmissionId("repo-new"),
                        source_sha=bad_sha,
                        desired_manifest_hash="manifest-new",
                        at=NOW + timedelta(minutes=1),
                    )

    def test_all_task_three_records_are_immutable_and_use_aware_utc(self) -> None:
        user_id = UserId("usr-1")
        workload_id = WorkloadId("wrk-1")
        repository_id = RepositoryAdmissionId("repo-1")
        operation_id = OperationId("op-1")

        records = (
            User(
                id=user_id,
                email="person@madup.com",
                role=UserRole.USER,
                state=UserState.ACTIVE,
                groups=frozenset({"mim-users"}),
                identity_synced_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            ),
            RepositoryAdmission(
                id=repository_id,
                repository_numeric_id=123,
                owner="madupmarketing",
                name="sample-app",
                installation_id=456,
                state=RepositoryAdmissionState.ADMITTED,
                admitted_sha="a" * 40,
                created_at=NOW,
                updated_at=NOW,
            ),
            Workload(
                id=workload_id,
                owner_id=user_id,
                repository_admission_id=repository_id,
                name="sample-app",
                kind=WorkloadKind.STREAMLIT,
                state=WorkloadState.ACTIVE,
                source_sha="a" * 40,
                desired_manifest_hash="manifest-hash",
                created_at=NOW,
                updated_at=NOW,
                last_activity_at=NOW,
            ),
            DeploymentPlan(
                id=DeploymentPlanId("plan-1"),
                actor_id=user_id,
                workload_id=workload_id,
                action="deploy",
                material_hash="plan-hash",
                policy_version="policy-v1",
                state=PlanState.ISSUED,
                expires_at=NOW + timedelta(minutes=15),
                created_at=NOW,
                updated_at=NOW,
                sanitized_summary=(("workload", "sample-app"),),
            ),
            Operation(
                id=operation_id,
                actor_id=user_id,
                workload_id=workload_id,
                action="deploy",
                idempotency_key="idem-1",
                request_hash="request-hash",
                state=OperationState.QUEUED,
                created_at=NOW,
                updated_at=NOW,
            ),
            Schedule(
                id=ScheduleId("sch-1"),
                owner_id=user_id,
                workload_id=workload_id,
                cron="0 * * * *",
                timezone="Asia/Seoul",
                state=ScheduleState.ENABLED,
                created_at=NOW,
                updated_at=NOW,
            ),
            SecretMetadata(
                id=SecretId("sec-1"),
                owner_id=user_id,
                name="slack-bot",
                integration_type="slack_oauth",
                attached_workload_ids=(workload_id,),
                active_version=1,
                rotation_state=SecretRotationState.STABLE,
                lifecycle_state=SecretLifecycleState.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
            ),
            UsageEntry(
                id=UsageEntryId("use-1"),
                owner_id=user_id,
                workload_id=workload_id,
                service_category="cloud_run",
                estimated_cost_krw=10,
                finalized_cost_krw=None,
                confidence=UsageConfidence.ESTIMATED,
                collected_at=NOW,
            ),
            ActivityEvent(
                id=ActivityEventId("act-1"),
                user_id=user_id,
                surface=ActivitySurface.MCP,
                action="plan_deploy",
                target_ref=str(workload_id),
                outcome=ActivityOutcome.SUCCEEDED,
                latency_bucket="lt_250ms",
                correlation_id="corr-1",
                occurred_at=NOW,
            ),
            DailyUsageAggregate(
                day=date(2026, 8, 2),
                user_id=user_id,
                active_users=1,
                dashboard_visits=0,
                mcp_actions=1,
                deployments=0,
                schedule_executions=0,
                successes=1,
                failures=0,
                policy_denials=0,
                version=1,
                updated_at=NOW,
            ),
            AuditEvent(
                id=AuditEventId("audit-1"),
                actor_id=user_id,
                action="plan_deploy",
                target_ref=str(workload_id),
                policy_decision="allowed",
                before_ref=None,
                after_ref="plan-1",
                correlation_id="corr-1",
                outcome="succeeded",
                occurred_at=NOW,
            ),
            LifecycleAction(
                id=LifecycleActionId("life-1"),
                workload_id=workload_id,
                kind=LifecycleActionKind.INACTIVITY_WARNING,
                state=LifecycleActionState.PLANNED,
                reason="23_days_inactive",
                eligible_at=NOW,
                observed_workload_version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
            OriginRequestClaim(
                request_id=OriginRequestId("req-1"),
                body_hash="body-hash",
                claimed_at=NOW,
                expires_at=NOW + timedelta(seconds=60),
            ),
        )

        for record in records:
            self.assertTrue(dataclasses.is_dataclass(record))
            first_field = dataclasses.fields(record)[0].name
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(record, first_field, getattr(record, first_field))

    def test_naive_or_non_utc_persistence_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "UTC-aware"):
            User(
                id=UserId("usr-1"),
                email="person@madup.com",
                role=UserRole.USER,
                state=UserState.ACTIVE,
                groups=frozenset(),
                identity_synced_at=NOW.replace(tzinfo=None),
                created_at=NOW,
                updated_at=NOW,
            )

        non_utc = NOW.astimezone(tz=timezone(timedelta(hours=9)))
        with self.assertRaisesRegex(ValueError, "UTC-aware"):
            OriginRequestClaim(
                request_id=OriginRequestId("req-1"),
                body_hash="hash",
                claimed_at=non_utc,
                expires_at=non_utc + timedelta(seconds=60),
            )

    def test_operation_rejects_skips_and_terminal_retransitions(self) -> None:
        operation = Operation(
            id=OperationId("op-1"),
            actor_id=UserId("usr-1"),
            workload_id=None,
            action="deploy",
            idempotency_key="idem-1",
            request_hash="hash-1",
            state=OperationState.QUEUED,
            created_at=NOW,
            updated_at=NOW,
        )

        with self.assertRaises(InvalidTransition):
            operation.transition(
                OperationState.SUCCEEDED,
                at=NOW + timedelta(seconds=1),
            )

        building = operation.transition(
            OperationState.BUILDING,
            at=NOW + timedelta(seconds=1),
        )
        self.assertEqual(building.version, 2)
        self.assertEqual(operation.version, 1)

        failed = building.transition(
            OperationState.FAILED,
            at=NOW + timedelta(seconds=2),
            sanitized_failure="build_failed",
        )
        with self.assertRaises(InvalidTransition):
            failed.transition(OperationState.QUEUED, at=NOW + timedelta(seconds=3))

    def test_created_updated_and_expiry_ordering_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "updated_at"):
            Operation(
                id=OperationId("op-1"),
                actor_id=UserId("usr-1"),
                workload_id=None,
                action="deploy",
                idempotency_key="idem-1",
                request_hash="hash-1",
                state=OperationState.PLANNED,
                created_at=NOW,
                updated_at=NOW - timedelta(seconds=1),
            )

        with self.assertRaisesRegex(ValueError, "expires_at"):
            OriginRequestClaim(
                request_id=OriginRequestId("req-1"),
                body_hash="hash",
                claimed_at=NOW,
                expires_at=NOW,
            )

    def test_every_stateful_record_enforces_its_closed_transition_map(self) -> None:
        user = User(
            id=UserId("usr-1"),
            email="person@madup.com",
            role=UserRole.USER,
            state=UserState.OFFBOARDED,
            groups=frozenset(),
            identity_synced_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        admission = RepositoryAdmission(
            id=RepositoryAdmissionId("repo-1"),
            repository_numeric_id=1,
            owner="madupmarketing",
            name="sample",
            installation_id=2,
            state=RepositoryAdmissionState.PENDING,
            admitted_sha="a" * 40,
            created_at=NOW,
            updated_at=NOW,
        )
        workload = Workload(
            id=WorkloadId("wrk-1"),
            owner_id=UserId("usr-1"),
            repository_admission_id=RepositoryAdmissionId("repo-1"),
            name="sample",
            kind=WorkloadKind.NEXTJS,
            state=WorkloadState.ARCHIVED,
            source_sha="a" * 40,
            desired_manifest_hash="manifest",
            created_at=NOW,
            updated_at=NOW,
        )
        plan = DeploymentPlan(
            id=DeploymentPlanId("plan-1"),
            actor_id=UserId("usr-1"),
            workload_id=None,
            action="deploy",
            material_hash="hash",
            policy_version="v1",
            state=PlanState.CONSUMED,
            expires_at=NOW + timedelta(minutes=15),
            created_at=NOW,
            updated_at=NOW,
        )
        schedule = Schedule(
            id=ScheduleId("sch-1"),
            owner_id=UserId("usr-1"),
            workload_id=WorkloadId("wrk-1"),
            cron="0 * * * *",
            timezone="Asia/Seoul",
            state=ScheduleState.ARCHIVED,
            created_at=NOW,
            updated_at=NOW,
        )
        secret = SecretMetadata(
            id=SecretId("sec-1"),
            owner_id=UserId("usr-1"),
            name="slack",
            integration_type="slack_oauth",
            attached_workload_ids=(),
            active_version=1,
            rotation_state=SecretRotationState.STABLE,
            lifecycle_state=SecretLifecycleState.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        lifecycle = LifecycleAction(
            id=LifecycleActionId("life-1"),
            workload_id=WorkloadId("wrk-1"),
            kind=LifecycleActionKind.DELETE_COMPUTE,
            state=LifecycleActionState.EXECUTED,
            reason="inactive",
            eligible_at=NOW,
            observed_workload_version=1,
            created_at=NOW,
            updated_at=NOW,
            executed_at=NOW,
        )

        invalid_calls = (
            lambda: user.transition_state(UserState.ACTIVE, at=NOW),
            lambda: admission.transition_state(
                RepositoryAdmissionState.REVOKED,
                at=NOW,
            ),
            lambda: workload.transition_state(WorkloadState.ACTIVE, at=NOW),
            lambda: plan.transition_state(PlanState.ISSUED, at=NOW),
            lambda: schedule.transition_state(ScheduleState.ENABLED, at=NOW),
            lambda: secret.transition_lifecycle(
                SecretLifecycleState.DESTROYED,
                at=NOW,
            ),
            lambda: secret.transition_rotation(
                SecretRotationState.RETIRING_OLD_VERSION,
                at=NOW,
            ),
            lambda: lifecycle.transition_state(
                LifecycleActionState.PLANNED,
                at=NOW,
            ),
        )
        for call in invalid_calls:
            with self.assertRaises(InvalidTransition):
                call()

        valid_transitions = (
            dataclasses.replace(user, state=UserState.ACTIVE).transition_state(
                UserState.SUSPENDED,
                at=NOW + timedelta(seconds=1),
            ),
            admission.transition_state(
                RepositoryAdmissionState.ADMITTED,
                at=NOW + timedelta(seconds=1),
            ),
            workload.transition_state(
                WorkloadState.PLANNED,
                at=NOW + timedelta(seconds=1),
            ),
            dataclasses.replace(plan, state=PlanState.ISSUED).transition_state(
                PlanState.CONSUMED,
                at=NOW + timedelta(seconds=1),
            ),
            dataclasses.replace(
                schedule,
                state=ScheduleState.ENABLED,
            ).transition_state(
                ScheduleState.DISABLED,
                at=NOW + timedelta(seconds=1),
            ),
            secret.transition_lifecycle(
                SecretLifecycleState.LOCKED,
                at=NOW + timedelta(seconds=1),
            ),
            secret.transition_rotation(
                SecretRotationState.ROTATING,
                at=NOW + timedelta(seconds=1),
            ),
            dataclasses.replace(
                lifecycle,
                state=LifecycleActionState.PLANNED,
                executed_at=None,
            ).transition_state(
                LifecycleActionState.EXECUTED,
                at=NOW + timedelta(seconds=1),
            ),
        )
        self.assertTrue(all(record.version == 2 for record in valid_transitions))

    def test_transition_maps_and_nested_plan_summary_are_deeply_immutable(self) -> None:
        with self.assertRaises(TypeError):
            OPERATION_TRANSITIONS[OperationState.QUEUED] = frozenset(  # type: ignore[index]
                {OperationState.SUCCEEDED}
            )

        mutable_summary = (["workload", "sample"],)
        with self.assertRaisesRegex(ValueError, "immutable"):
            DeploymentPlan(
                id=DeploymentPlanId("plan-1"),
                actor_id=UserId("usr-1"),
                workload_id=None,
                action="deploy",
                material_hash="hash",
                policy_version="v1",
                state=PlanState.ISSUED,
                expires_at=NOW + timedelta(minutes=15),
                created_at=NOW,
                updated_at=NOW,
                sanitized_summary=mutable_summary,  # type: ignore[arg-type]
            )

    def test_lifecycle_execution_state_requires_a_matching_timestamp(self) -> None:
        common = {
            "id": LifecycleActionId("life-1"),
            "workload_id": WorkloadId("wrk-1"),
            "kind": LifecycleActionKind.DELETE_COMPUTE,
            "reason": "inactive",
            "eligible_at": NOW,
            "observed_workload_version": 1,
            "created_at": NOW,
            "updated_at": NOW,
        }
        with self.assertRaisesRegex(ValueError, "executed_at"):
            LifecycleAction(
                **common,
                state=LifecycleActionState.EXECUTED,
                executed_at=None,
            )
        with self.assertRaisesRegex(ValueError, "executed_at"):
            LifecycleAction(
                **common,
                state=LifecycleActionState.PLANNED,
                executed_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
