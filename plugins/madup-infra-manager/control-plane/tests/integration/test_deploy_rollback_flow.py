from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from mim_control_plane.adapters.fake_execution import (
    FakeArtifactRegistryPort,
    FakeBuildPort,
    FakeDeploymentQueue,
    FakeDesiredStateArtifactPort,
    FakeRuntimeIdentityPort,
    FakeRuntimePort,
    FakeSecretMetadataPort,
)
from mim_control_plane.adapters.github import GitHubSourceError
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import (
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
from mim_control_plane.domain.states import (
    OperationState,
    PlanState,
    RepositoryAdmissionState,
    ScheduleState,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.execution import PrivateDeployEnqueuer
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.services.deployments import (
    DeploymentDenied,
    DeploymentService,
)
from mim_control_plane.services.render import (
    DesiredStateRenderContext,
    VerifiedDesiredState,
)
from mim_control_plane.services.runtime_identity import runtime_identity_spec
from mim_control_plane.workers.deploy import PrivateDeployWorker

NOW = datetime(2026, 8, 4, 4, 0, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class MutableSource:
    def __init__(self, snapshot: dict[str, bytes]) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    def fetch_snapshot(
        self,
        admission: RepositoryAdmission,
    ) -> MappingProxyType[str, bytes]:
        self.calls.append(admission.admitted_sha)
        return MappingProxyType(dict(self.snapshot))


class DeterministicIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        count = self.counts.get(prefix, 0) + 1
        self.counts[prefix] = count
        return f"{prefix}-{count}"


def seed_store(
    *,
    kind: WorkloadKind = WorkloadKind.STREAMLIT,
) -> tuple[MemoryStore, User, RepositoryAdmission, Workload]:
    store = MemoryStore()
    store.create_org_cost_guard(
        OrgCostGuard(
            evaluated_at=NOW,
            latest_usage_collected_at=NOW,
            emergency_stop=False,
            org_policy_cost_krw=0,
        )
    )
    owner = store.create_user(
        User(
            id=UserId("usr-1"),
            email="person@madup.com",
            role=UserRole.USER,
            state=UserState.ACTIVE,
            groups=frozenset({"mim-users"}),
            identity_synced_at=NOW,
            created_at=NOW - timedelta(days=1),
            updated_at=NOW,
        )
    )
    admission = store.create_repository_admission(
        RepositoryAdmission(
            id=RepositoryAdmissionId("repo-1"),
            repository_numeric_id=123,
            owner="madupmarketing",
            name="streamlit-app",
            installation_id=456,
            state=RepositoryAdmissionState.ADMITTED,
            admitted_sha="a" * 40,
            created_at=NOW - timedelta(days=1),
            updated_at=NOW,
        )
    )
    workload = store.create_workload(
        Workload(
            id=WorkloadId("wrk-1"),
            owner_id=owner.id,
            repository_admission_id=admission.id,
            name="streamlit-app",
            kind=kind,
            state=WorkloadState.ACTIVE,
            source_sha=admission.admitted_sha,
            desired_manifest_hash="manifest-1",
            created_at=NOW - timedelta(days=1),
            updated_at=NOW,
            last_healthy_image_digest="sha256:" + "f" * 64,
        )
    )
    return store, owner, admission, workload


def principal(user: User) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=user.id,
        email=user.email,
        role=user.role,
    )


def make_service(
    *,
    store: MemoryStore,
    source: MutableSource,
    queue: FakeDeploymentQueue,
    clock: MutableClock,
) -> DeploymentService:
    return DeploymentService(
        store=store,
        source=source,
        enqueuer=PrivateDeployEnqueuer(queue=queue),
        render_context=DesiredStateRenderContext(
            project_id="mim-prod-123456",
            key_id="deploy-key-1",
        ),
        signing_key=b"s" * 32,
        clock=clock,
        id_factory=DeterministicIds(),
    )


class DeployRollbackFlowTests(unittest.TestCase):
    def test_scheduled_deploy_revalidates_schedule_quota(self) -> None:
        store, owner, _, workload = seed_store(
            kind=WorkloadKind.SCHEDULED_SCRIPT
        )
        for index in range(3):
            store.create_schedule(
                Schedule(
                    id=ScheduleId(f"sch-{index}"),
                    owner_id=owner.id,
                    workload_id=WorkloadId(f"wrk-other-{index}"),
                    cron="0 * * * *",
                    timezone="Asia/Seoul",
                    state=ScheduleState.ENABLED,
                    created_at=NOW - timedelta(days=1),
                    updated_at=NOW,
                )
            )
        service = make_service(
            store=store,
            source=MutableSource(
                {
                    "main.py": b"print('hourly')\n",
                    "mim.yaml": (
                        b"kind: scheduled_script\n"
                        b"entrypoint: main.py\n"
                        b"schedule: hourly\n"
                    ),
                }
            ),
            queue=FakeDeploymentQueue(),
            clock=MutableClock(NOW + timedelta(seconds=1)),
        )

        with self.assertRaises(DeploymentDenied) as caught:
            service.plan_deploy(
                principal=principal(owner),
                workload_id=str(workload.id),
            )

        self.assertEqual(caught.exception.reason_code, "schedule_quota_exceeded")

    def test_source_adapter_failure_is_returned_only_as_generic_denial(self) -> None:
        store, owner, _, workload = seed_store()

        class FailingSource(MutableSource):
            def fetch_snapshot(
                self,
                admission: RepositoryAdmission,
            ) -> MappingProxyType[str, bytes]:
                del admission
                raise GitHubSourceError("Bearer ghp_private_value")

        service = make_service(
            store=store,
            source=FailingSource({}),
            queue=FakeDeploymentQueue(),
            clock=MutableClock(NOW + timedelta(seconds=1)),
        )

        with self.assertRaises(DeploymentDenied) as caught:
            service.plan_deploy(
                principal=principal(owner),
                workload_id=str(workload.id),
            )

        self.assertEqual(str(caught.exception), "Deployment request was denied.")
        self.assertNotIn("private_value", str(caught.exception))

    def test_actor_bound_plan_revalidates_and_queues_exact_source(self) -> None:
        store, owner, admission, workload = seed_store()
        source = MutableSource(
            {
                "app.py": b"import streamlit as st\nst.write('ok')\n",
                "requirements.txt": b"streamlit==1.40.0\n",
            }
        )
        queue = FakeDeploymentQueue()
        clock = MutableClock(NOW + timedelta(seconds=1))
        service = make_service(
            store=store,
            source=source,
            queue=queue,
            clock=clock,
        )

        reviewed = service.plan_deploy(
            principal=principal(owner),
            workload_id=str(workload.id),
        )

        self.assertEqual(reviewed["action"], "plan_deploy")
        self.assertEqual(reviewed["status"], "ready")
        self.assertRegex(str(reviewed["plan_hash"]), r"^[0-9a-f]{64}$")
        self.assertEqual(reviewed["actor_id"], str(owner.id))
        self.assertEqual(
            reviewed["material_summary"],
            {
                "repository_owner": "madupmarketing",
                "repository_name": "streamlit-app",
                "immutable_sha": "a" * 40,
                "source_root": ".",
                "workload_kind": "streamlit",
                "deployment_target": "cloud_run_service",
                "resource_impact": "upsert_cloud_run_service",
                "current_month_policy_cost_krw": "0",
                "monthly_budget_cap_krw": "1000",
                "service_quota_limit": "2",
                "schedule_quota_limit": "3",
            },
        )
        self.assertNotIn("source", reviewed)
        self.assertNotIn("token", str(reviewed).casefold())
        self.assertNotIn("clone", str(reviewed).casefold())
        self.assertNotIn("origin", str(reviewed).casefold())
        persisted_plan = store.get_deployment_plan(reviewed["plan_id"])
        self.assertEqual(persisted_plan.state, PlanState.ISSUED)
        self.assertEqual(
            dict(persisted_plan.sanitized_summary),
            reviewed["material_summary"],
        )

        deployed = service.deploy_from_plan(
            principal=principal(owner),
            plan_id=str(reviewed["plan_id"]),
            plan_hash=str(reviewed["plan_hash"]),
            idempotency_key="manual-1",
            correlation_id="corr-1",
        )
        replay = service.deploy_from_plan(
            principal=principal(owner),
            plan_id=str(reviewed["plan_id"]),
            plan_hash=str(reviewed["plan_hash"]),
            idempotency_key="manual-1",
            correlation_id="corr-retry",
        )

        self.assertEqual(deployed["operation_id"], replay["operation_id"])
        self.assertFalse(deployed["replayed"])
        self.assertTrue(replay["replayed"])
        task = queue.get(deployed["operation_id"])
        self.assertEqual(task.expected_source_sha, admission.admitted_sha)
        self.assertEqual(task.workload_id, workload.id)
        self.assertEqual(store.get_deploy_task(task.operation_id), task)
        self.assertEqual(
            store.get_deployment_plan(reviewed["plan_id"]).state,
            PlanState.CONSUMED,
        )
        self.assertEqual(len(store.list_audit_events()), 1)

        refreshed_digest = "sha256:" + "e" * 64
        worker_now = clock.value + timedelta(seconds=1)

        class RefreshingUnhealthyRuntime(FakeRuntimePort):
            def verify_health(self, desired_state: VerifiedDesiredState) -> bool:
                current = store.get_workload(workload.id)
                store.save_workload(
                    dataclasses.replace(
                        current,
                        last_healthy_image_digest=refreshed_digest,
                        updated_at=worker_now,
                        version=current.version + 1,
                    ),
                    expected_version=current.version,
                )
                return super().verify_health(desired_state)

        runtime = RefreshingUnhealthyRuntime(healthy=False)
        runtime_identity = FakeRuntimeIdentityPort(
            email=runtime_identity_spec(
                project_id="mim-prod-123456",
                workload_id=str(workload.id),
            ).email
        )
        result = PrivateDeployWorker(
            store=store,
            queue=queue,
            source=source,
            build=FakeBuildPort(),
            registry=FakeArtifactRegistryPort(),
            artifacts=FakeDesiredStateArtifactPort(),
            runtime_identity=runtime_identity,
            runtime=runtime,
            secrets=FakeSecretMetadataPort(store=store),
            render_context=DesiredStateRenderContext(
                project_id="mim-prod-123456",
                key_id="deploy-key-1",
            ),
            signing_key=b"s" * 32,
        ).run(
            operation_id=str(deployed["operation_id"]),
            now=worker_now,
        )

        self.assertEqual(result.operation.state, OperationState.ROLLED_BACK)
        self.assertEqual(runtime_identity.calls, [workload.id])
        self.assertEqual(len(runtime.apply_calls), 1)
        self.assertEqual(len(runtime.health_checks), 1)
        self.assertEqual(runtime.rollback_calls[0].workload_id, workload.id)
        self.assertEqual(runtime.rollback_calls[0].workload_owner_id, owner.id)
        self.assertEqual(runtime.rollback_calls[0].image_digest, refreshed_digest)
        refreshed = store.get_workload(workload.id)
        self.assertEqual(refreshed.last_healthy_image_digest, refreshed_digest)
        self.assertEqual(refreshed.version, task.expected_workload_version + 1)

    def test_plan_actor_expiry_and_snapshot_drift_fail_before_queue(self) -> None:
        store, owner, _, workload = seed_store()
        other = store.create_user(
            User(
                id=UserId("usr-2"),
                email="other@madup.com",
                role=UserRole.USER,
                state=UserState.ACTIVE,
                groups=frozenset({"mim-users"}),
                identity_synced_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        source = MutableSource(
            {
                "app.py": b"import streamlit\n",
                "requirements.txt": b"streamlit==1.40.0\n",
            }
        )
        queue = FakeDeploymentQueue()
        clock = MutableClock(NOW + timedelta(seconds=1))
        service = make_service(
            store=store,
            source=source,
            queue=queue,
            clock=clock,
        )
        reviewed = service.plan_deploy(
            principal=principal(owner),
            workload_id=str(workload.id),
        )

        with self.assertRaises(DeploymentDenied):
            service.deploy_from_plan(
                principal=principal(other),
                plan_id=str(reviewed["plan_id"]),
                plan_hash=str(reviewed["plan_hash"]),
                idempotency_key="manual-other",
                correlation_id="corr-other",
            )

        source.snapshot["app.py"] = b"import streamlit\nprint('changed')\n"
        with self.assertRaises(DeploymentDenied):
            service.deploy_from_plan(
                principal=principal(owner),
                plan_id=str(reviewed["plan_id"]),
                plan_hash=str(reviewed["plan_hash"]),
                idempotency_key="manual-drift",
                correlation_id="corr-drift",
            )

        source.snapshot["app.py"] = b"import streamlit\n"
        clock.value = NOW + timedelta(minutes=20)
        with self.assertRaises(DeploymentDenied):
            service.deploy_from_plan(
                principal=principal(owner),
                plan_id=str(reviewed["plan_id"]),
                plan_hash=str(reviewed["plan_hash"]),
                idempotency_key="manual-expired",
                correlation_id="corr-expired",
            )
        with self.assertRaises(Exception):
            queue.get("missing")


if __name__ == "__main__":
    unittest.main()
