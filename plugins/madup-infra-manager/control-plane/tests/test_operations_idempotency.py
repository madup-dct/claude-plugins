from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import (
    AuditEvent,
    AuditEventId,
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
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    InvalidTransition,
    LifecycleActionKind,
    LifecycleActionState,
    OperationState,
    PlanState,
    RepositoryAdmissionState,
    ScheduleState,
    SecretLifecycleState,
    SecretRotationState,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.execution import QueuedDeployTask
from mim_control_plane.ports.store import (
    AUTO_DEPLOY_ACTOR_ID,
    AlreadyExists,
    IdempotencyConflict,
    InvariantViolation,
    NotFound,
    ReplayDetected,
    VersionConflict,
)

NOW = datetime(2026, 8, 2, tzinfo=UTC)


def operation(
    *,
    operation_id: str = "op-1",
    idempotency_key: str = "idem-1",
    request_hash: str = "request-hash",
) -> Operation:
    return Operation(
        id=OperationId(operation_id),
        actor_id=UserId("usr-1"),
        workload_id=None,
        action="deploy",
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        state=OperationState.QUEUED,
        created_at=NOW,
        updated_at=NOW,
    )


class OperationIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()

    def test_same_idempotency_material_returns_original_operation(self) -> None:
        first = self.store.create_operation_once(operation())
        replay = self.store.create_operation_once(
            operation(operation_id="op-retry"),
        )

        self.assertEqual(replay, first)
        self.assertEqual(replay.id, OperationId("op-1"))

    def test_same_idempotency_key_with_different_material_is_rejected(self) -> None:
        self.store.create_operation_once(operation())

        with self.assertRaises(IdempotencyConflict):
            self.store.create_operation_once(
                operation(operation_id="op-2", request_hash="different-hash"),
            )

    def test_operation_updates_require_exact_optimistic_version(self) -> None:
        original = self.store.create_operation_once(operation())
        building = original.transition(
            OperationState.BUILDING,
            at=NOW + timedelta(seconds=1),
        )
        saved = self.store.save_operation(building, expected_version=1)
        self.assertEqual(saved.version, 2)

        failed = building.transition(
            OperationState.FAILED,
            at=NOW + timedelta(seconds=2),
            sanitized_failure="build_failed",
        )
        with self.assertRaises(VersionConflict):
            self.store.save_operation(failed, expected_version=1)

    def test_store_returns_copies_and_cannot_be_mutated_through_aliases(self) -> None:
        stored = self.store.create_operation_once(operation())
        loaded = self.store.get_operation(stored.id)

        self.assertEqual(loaded, stored)
        self.assertIsNot(loaded, stored)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            loaded.state = OperationState.SUCCEEDED  # type: ignore[misc]

    def test_audit_is_append_only_and_ordered_deterministically(self) -> None:
        later = AuditEvent(
            id=AuditEventId("audit-b"),
            actor_id=UserId("usr-1"),
            action="deploy",
            target_ref="wrk-1",
            policy_decision="allowed",
            before_ref=None,
            after_ref="rev-1",
            correlation_id="corr-1",
            outcome="succeeded",
            occurred_at=NOW + timedelta(seconds=1),
        )
        earlier = dataclasses.replace(
            later,
            id=AuditEventId("audit-a"),
            occurred_at=NOW,
        )

        self.store.append_audit_event(later)
        self.store.append_audit_event(earlier)
        self.assertEqual(
            [event.id for event in self.store.list_audit_events()],
            [AuditEventId("audit-a"), AuditEventId("audit-b")],
        )
        with self.assertRaises(AlreadyExists):
            self.store.append_audit_event(earlier)

    def test_origin_request_claim_is_atomic_and_create_only(self) -> None:
        claim = OriginRequestClaim(
            request_id=OriginRequestId("req-1"),
            body_hash="body-hash",
            claimed_at=NOW,
            expires_at=NOW + timedelta(seconds=60),
        )
        self.store.claim_origin_request(claim)

        with self.assertRaises(ReplayDetected):
            self.store.claim_origin_request(claim)

    def test_github_auto_deploy_is_one_atomic_deterministic_outcome(self) -> None:
        user = User(
            id=UserId("usr-auto"),
            email="auto@madup.com",
            role=UserRole.USER,
            state=UserState.ACTIVE,
            groups=frozenset({"mim-users"}),
            identity_synced_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        old_admission = RepositoryAdmission(
            id=RepositoryAdmissionId("repo-old"),
            repository_numeric_id=123,
            owner="madupmarketing",
            name="sample",
            installation_id=456,
            state=RepositoryAdmissionState.ADMITTED,
            admitted_sha="a" * 40,
            created_at=NOW,
            updated_at=NOW,
        )
        current = Workload(
            id=WorkloadId("wrk-auto"),
            owner_id=user.id,
            repository_admission_id=old_admission.id,
            name="sample",
            kind=WorkloadKind.STREAMLIT,
            state=WorkloadState.ACTIVE,
            source_sha=old_admission.admitted_sha,
            desired_manifest_hash="manifest-old",
            created_at=NOW,
            updated_at=NOW,
            auto_deploy_enabled=True,
            auto_deploy_ref="refs/heads/main",
        )
        new_admission = dataclasses.replace(
            old_admission,
            id=RepositoryAdmissionId("repo-new"),
            admitted_sha="b" * 40,
            created_at=NOW + timedelta(minutes=1),
            updated_at=NOW + timedelta(minutes=1),
        )
        advanced = current.advance_source(
            repository_admission_id=new_admission.id,
            source_sha=new_admission.admitted_sha,
            desired_manifest_hash="manifest-new",
            at=NOW + timedelta(minutes=1),
        )
        plan = DeploymentPlan(
            id=DeploymentPlanId("plan-auto"),
            actor_id=AUTO_DEPLOY_ACTOR_ID,
            workload_id=current.id,
            action="deploy",
            material_hash="a" * 64,
            policy_version="mim-deploy-v1",
            state=PlanState.ISSUED,
            expires_at=NOW + timedelta(minutes=16),
            created_at=NOW + timedelta(minutes=1),
            updated_at=NOW + timedelta(minutes=1),
        )
        queued_operation = Operation(
            id=OperationId("op-auto"),
            actor_id=AUTO_DEPLOY_ACTOR_ID,
            workload_id=current.id,
            action="deploy",
            idempotency_key=(
                "github:11111111-1111-1111-1111-111111111111"
            ),
            request_hash=plan.material_hash,
            state=OperationState.QUEUED,
            created_at=NOW + timedelta(minutes=1),
            updated_at=NOW + timedelta(minutes=1),
        )
        task = QueuedDeployTask.from_snapshot(
            operation_id=queued_operation.id,
            expected_operation_version=queued_operation.version,
            workload_id=advanced.id,
            expected_workload_version=advanced.version,
            admission_id=new_admission.id,
            expected_admission_version=new_admission.version,
            expected_source_sha=new_admission.admitted_sha,
            idempotency_key=queued_operation.idempotency_key,
            queued_at=queued_operation.created_at,
            snapshot={"app.py": b"import streamlit\n"},
        )
        self.store.create_user(user)
        self.store.create_repository_admission(old_admission)
        self.store.create_workload(current)

        first = self.store.apply_github_auto_deploy_once(
            delivery_id="11111111-1111-1111-1111-111111111111",
            delivery_hash="1" * 64,
            source_ref="refs/heads/main",
            expected_workload_version=current.version,
            admission=new_admission,
            workload=advanced,
            plan=plan,
            operation=queued_operation,
            task=task,
            consumed_at=NOW + timedelta(minutes=1),
        )
        replay = self.store.apply_github_auto_deploy_once(
            delivery_id="11111111-1111-1111-1111-111111111111",
            delivery_hash="1" * 64,
            source_ref="refs/heads/main",
            expected_workload_version=current.version,
            admission=new_admission,
            workload=advanced,
            plan=plan,
            operation=queued_operation,
            task=task,
            consumed_at=NOW + timedelta(minutes=2),
        )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.operation, first.operation)
        self.assertEqual(replay.task.material_hash, first.task.material_hash)
        self.assertEqual(self.store.get_workload(current.id), advanced)
        self.assertEqual(
            self.store.get_deploy_task(queued_operation.id).material_hash,
            task.material_hash,
        )
        self.assertEqual(first.plan.state, PlanState.CONSUMED)

        with self.assertRaises(ReplayDetected):
            self.store.apply_github_auto_deploy_once(
                delivery_id="11111111-1111-1111-1111-111111111111",
                delivery_hash="2" * 64,
                source_ref="refs/heads/main",
                expected_workload_version=current.version,
                admission=new_admission,
                workload=advanced,
                plan=plan,
                operation=queued_operation,
                task=task,
                consumed_at=NOW + timedelta(minutes=2),
            )

    def test_stale_auto_deploy_advances_nothing(self) -> None:
        user = User(
            id=UserId("usr-auto"),
            email="auto@madup.com",
            role=UserRole.USER,
            state=UserState.ACTIVE,
            groups=frozenset({"mim-users"}),
            identity_synced_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        old_admission = RepositoryAdmission(
            id=RepositoryAdmissionId("repo-old"),
            repository_numeric_id=123,
            owner="madupmarketing",
            name="sample",
            installation_id=456,
            state=RepositoryAdmissionState.ADMITTED,
            admitted_sha="a" * 40,
            created_at=NOW,
            updated_at=NOW,
        )
        current = Workload(
            id=WorkloadId("wrk-auto"),
            owner_id=user.id,
            repository_admission_id=old_admission.id,
            name="sample",
            kind=WorkloadKind.STREAMLIT,
            state=WorkloadState.ACTIVE,
            source_sha=old_admission.admitted_sha,
            desired_manifest_hash="manifest-old",
            created_at=NOW,
            updated_at=NOW,
            auto_deploy_enabled=True,
            auto_deploy_ref="refs/heads/main",
        )
        self.store.create_user(user)
        self.store.create_repository_admission(old_admission)
        self.store.create_workload(current)
        new_admission = dataclasses.replace(
            old_admission,
            id=RepositoryAdmissionId("repo-new"),
            admitted_sha="b" * 40,
            created_at=NOW + timedelta(minutes=1),
            updated_at=NOW + timedelta(minutes=1),
        )
        advanced = current.advance_source(
            repository_admission_id=new_admission.id,
            source_sha=new_admission.admitted_sha,
            desired_manifest_hash="manifest-new",
            at=NOW + timedelta(minutes=1),
        )
        plan = DeploymentPlan(
            id=DeploymentPlanId("plan-auto"),
            actor_id=AUTO_DEPLOY_ACTOR_ID,
            workload_id=current.id,
            action="deploy",
            material_hash="a" * 64,
            policy_version="mim-deploy-v1",
            state=PlanState.ISSUED,
            expires_at=NOW + timedelta(minutes=16),
            created_at=NOW + timedelta(minutes=1),
            updated_at=NOW + timedelta(minutes=1),
        )
        queued_operation = Operation(
            id=OperationId("op-auto"),
            actor_id=AUTO_DEPLOY_ACTOR_ID,
            workload_id=current.id,
            action="deploy",
            idempotency_key=(
                "github:22222222-2222-2222-2222-222222222222"
            ),
            request_hash=plan.material_hash,
            state=OperationState.QUEUED,
            created_at=NOW + timedelta(minutes=1),
            updated_at=NOW + timedelta(minutes=1),
        )
        task = QueuedDeployTask.from_snapshot(
            operation_id=queued_operation.id,
            expected_operation_version=1,
            workload_id=current.id,
            expected_workload_version=advanced.version,
            admission_id=new_admission.id,
            expected_admission_version=1,
            expected_source_sha=new_admission.admitted_sha,
            idempotency_key=queued_operation.idempotency_key,
            queued_at=queued_operation.created_at,
            snapshot={"app.py": b"import streamlit\n"},
        )

        with self.assertRaises(VersionConflict):
            self.store.apply_github_auto_deploy_once(
                delivery_id="22222222-2222-2222-2222-222222222222",
                delivery_hash="2" * 64,
                source_ref="refs/heads/main",
                expected_workload_version=99,
                admission=new_admission,
                workload=advanced,
                plan=plan,
                operation=queued_operation,
                task=task,
                consumed_at=NOW + timedelta(minutes=1),
            )
        self.assertEqual(self.store.get_workload(current.id), current)
        with self.assertRaises(NotFound):
            self.store.get_repository_admission(new_admission.id)
        with self.assertRaises(NotFound):
            self.store.get_operation(queued_operation.id)

    def test_store_rejects_state_machine_bypass_snapshots(self) -> None:
        original = self.store.create_operation_once(operation())
        skipped = dataclasses.replace(
            original,
            state=OperationState.SUCCEEDED,
            version=2,
            updated_at=NOW + timedelta(seconds=1),
        )
        with self.assertRaises(InvalidTransition):
            self.store.save_operation(skipped, expected_version=1)

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
        self.store.create_repository_admission(admission)
        skipped_admission = dataclasses.replace(
            admission,
            state=RepositoryAdmissionState.REVOKED,
            version=2,
            updated_at=NOW + timedelta(seconds=1),
        )
        with self.assertRaises(InvalidTransition):
            self.store.save_repository_admission(
                skipped_admission,
                expected_version=1,
            )

    def test_generic_saves_cannot_rewrite_identity_or_policy_material(self) -> None:
        user = User(
            id=UserId("usr-1"),
            email="person@madup.com",
            role=UserRole.USER,
            state=UserState.ACTIVE,
            groups=frozenset({"mim-users"}),
            identity_synced_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        self.store.create_user(user)
        user_rewrites = (
            dataclasses.replace(user, email="changed@madup.com"),
            dataclasses.replace(user, role=UserRole.ADMIN),
            dataclasses.replace(user, groups=frozenset({"other"})),
        )
        for rewrite in user_rewrites:
            with self.assertRaises(InvariantViolation):
                self.store.save_user(
                    dataclasses.replace(
                        rewrite,
                        version=2,
                        updated_at=NOW + timedelta(seconds=1),
                    ),
                    expected_version=1,
                )

        admission = RepositoryAdmission(
            id=RepositoryAdmissionId("repo-1"),
            repository_numeric_id=1,
            owner="madupmarketing",
            name="sample",
            installation_id=2,
            state=RepositoryAdmissionState.ADMITTED,
            admitted_sha="a" * 40,
            created_at=NOW,
            updated_at=NOW,
        )
        self.store.create_repository_admission(admission)

        workload = Workload(
            id=WorkloadId("wrk-1"),
            owner_id=user.id,
            repository_admission_id=admission.id,
            name="sample",
            kind=WorkloadKind.STREAMLIT,
            state=WorkloadState.ACTIVE,
            source_sha=admission.admitted_sha,
            desired_manifest_hash="manifest",
            created_at=NOW,
            updated_at=NOW,
        )
        self.store.create_workload(workload)
        with self.assertRaises(InvariantViolation):
            self.store.save_workload(
                dataclasses.replace(
                    workload,
                    owner_id=UserId("usr-2"),
                    version=2,
                    updated_at=NOW + timedelta(seconds=1),
                ),
                expected_version=1,
            )

        plan = DeploymentPlan(
            id=DeploymentPlanId("plan-1"),
            actor_id=user.id,
            workload_id=workload.id,
            action="deploy",
            material_hash="plan-hash",
            policy_version="v1",
            state=PlanState.ISSUED,
            expires_at=NOW + timedelta(minutes=15),
            created_at=NOW,
            updated_at=NOW,
        )
        self.store.create_deployment_plan(plan)
        with self.assertRaises(InvariantViolation):
            self.store.save_deployment_plan(
                dataclasses.replace(
                    plan,
                    material_hash="changed",
                    version=2,
                    updated_at=NOW + timedelta(seconds=1),
                ),
                expected_version=1,
            )

        schedule = Schedule(
            id=ScheduleId("sch-1"),
            owner_id=user.id,
            workload_id=workload.id,
            cron="0 * * * *",
            timezone="Asia/Seoul",
            state=ScheduleState.ENABLED,
            created_at=NOW,
            updated_at=NOW,
        )
        self.store.create_schedule(schedule)
        with self.assertRaises(InvariantViolation):
            self.store.save_schedule(
                dataclasses.replace(
                    schedule,
                    cron="30 * * * *",
                    version=2,
                    updated_at=NOW + timedelta(seconds=1),
                ),
                expected_version=1,
            )

        secret = SecretMetadata(
            id=SecretId("sec-1"),
            owner_id=user.id,
            name="slack",
            integration_type="slack_oauth",
            attached_workload_ids=(workload.id,),
            active_version=1,
            rotation_state=SecretRotationState.STABLE,
            lifecycle_state=SecretLifecycleState.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        self.store.create_secret_metadata(secret)
        with self.assertRaises(InvariantViolation):
            self.store.save_secret_metadata(
                dataclasses.replace(
                    secret,
                    name="renamed",
                    version=2,
                    updated_at=NOW + timedelta(seconds=1),
                ),
                expected_version=1,
            )

        action = LifecycleAction(
            id=LifecycleActionId("life-1"),
            workload_id=workload.id,
            kind=LifecycleActionKind.INACTIVITY_WARNING,
            state=LifecycleActionState.PLANNED,
            reason="inactive",
            eligible_at=NOW,
            observed_workload_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        self.store.create_lifecycle_action(action)
        with self.assertRaises(InvariantViolation):
            self.store.save_lifecycle_action(
                dataclasses.replace(
                    action,
                    reason="changed",
                    version=2,
                    updated_at=NOW + timedelta(seconds=1),
                ),
                expected_version=1,
            )


if __name__ == "__main__":
    unittest.main()
