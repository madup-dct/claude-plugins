from __future__ import annotations

import dataclasses
import importlib
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Mapping, cast

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
FIXTURE_ROOT = TEST_ROOT / "fixtures" / "repos"
for path in (TEST_ROOT, SRC_ROOT):
    import sys

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.fake_execution import (  # noqa: E402
    FakeArtifactRegistryPort,
    FakeBuildPort,
    FakeDeploymentQueue,
    FakeDesiredStateArtifactPort,
    FakeRuntimeIdentityPort,
    FakeRuntimePort,
    FakeSecretMetadataPort,
)
from mim_control_plane.adapters.memory_store import MemoryStore  # noqa: E402
from mim_control_plane.domain.models import (  # noqa: E402
    AppHostnameBindingState,
    Operation,
    OperationId,
    RepositoryAdmission,
    RepositoryAdmissionId,
    SecretId,
    SecretMetadata,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (  # noqa: E402
    OPERATION_TRANSITIONS,
    OperationState,
    RepositoryAdmissionState,
    SecretLifecycleState,
    SecretRotationState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.execution import (  # noqa: E402
    ArtifactConflictError,
    DeploymentQueueReceipt,
    ExecutionPlaneError,
    PrivateDeployEnqueuer,
    RetryableExecutionPlaneError,
    SecretAttachmentReference,
    TaskConflictError,
)
from mim_control_plane.services.app_hostname import (  # noqa: E402
    AppHostnameBindingService,
)
from mim_control_plane.services.render import (  # noqa: E402
    DesiredStateRenderContext,
)
from mim_control_plane.services.runtime_identity import (  # noqa: E402
    runtime_identity_spec,
)
from mim_control_plane.services.runtime_naming import (  # noqa: E402
    cloud_run_service_name,
)
from mim_control_plane.workers.deploy import (  # noqa: E402
    DeployWorkerResult,
    PrivateDeployWorker,
)

NOW = datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC)
KEY = b"k" * 32
SECRET_VALUE = "sk-live-private-secret"
REGION = "asia-northeast3"


def load_fixture_snapshot(name: str) -> dict[str, bytes]:
    fixture_dir = FIXTURE_ROOT / name
    snapshot: dict[str, bytes] = {}
    for path in sorted(fixture_dir.rglob("*")):
        if path.is_file():
            snapshot[path.relative_to(fixture_dir).as_posix()] = path.read_bytes()
    return snapshot


def admission(
    *,
    admission_id: str = "repo-1",
    owner: str = "madupmarketing",
    state: RepositoryAdmissionState = RepositoryAdmissionState.ADMITTED,
    admitted_sha: str = "b" * 40,
    version: int = 1,
) -> RepositoryAdmission:
    return RepositoryAdmission(
        id=RepositoryAdmissionId(admission_id),
        repository_numeric_id=42,
        owner=owner,
        name="sample-app",
        installation_id=99,
        state=state,
        admitted_sha=admitted_sha,
        created_at=NOW - timedelta(days=7),
        updated_at=NOW - timedelta(minutes=2),
        version=version,
    )


def workload(
    *,
    workload_id: str = "wrk-1",
    admission_id: str = "repo-1",
    kind: WorkloadKind = WorkloadKind.NEXTJS,
    state: WorkloadState = WorkloadState.ACTIVE,
    source_sha: str = "b" * 40,
    last_healthy_image_digest: str | None = "sha256:" + "a" * 64,
    version: int = 1,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId("usr-1"),
        repository_admission_id=RepositoryAdmissionId(admission_id),
        name="sample-app",
        kind=kind,
        state=state,
        source_sha=source_sha,
        desired_manifest_hash="manifest-hash-1",
        created_at=NOW - timedelta(days=7),
        updated_at=NOW - timedelta(minutes=2),
        last_activity_at=NOW - timedelta(minutes=2),
        last_healthy_image_digest=last_healthy_image_digest,
        version=version,
    )


def operation(
    *,
    operation_id: str = "op-1",
    workload_id: str = "wrk-1",
    version: int = 1,
    state: OperationState = OperationState.QUEUED,
    action: str = "deploy",
) -> Operation:
    return Operation(
        id=OperationId(operation_id),
        actor_id=UserId("usr-1"),
        workload_id=WorkloadId(workload_id),
        action=action,
        idempotency_key="idem-1",
        request_hash="request-hash-1",
        state=state,
        created_at=NOW - timedelta(minutes=3),
        updated_at=NOW - timedelta(minutes=1),
        version=version,
    )


def secret_metadata(
    *,
    secret_id: str = "sec-1",
    workload_id: str = "wrk-1",
    active_version: int = 1,
    version: int = 1,
    lifecycle_state: SecretLifecycleState = SecretLifecycleState.ACTIVE,
) -> SecretMetadata:
    return SecretMetadata(
        id=SecretId(secret_id),
        owner_id=UserId("usr-1"),
        name="slack-bot",
        integration_type="slack_oauth",
        attached_workload_ids=(WorkloadId(workload_id),),
        active_version=active_version,
        rotation_state=SecretRotationState.STABLE,
        lifecycle_state=lifecycle_state,
        created_at=NOW - timedelta(days=5),
        updated_at=NOW - timedelta(minutes=2),
        version=version,
    )


def context() -> DesiredStateRenderContext:
    return DesiredStateRenderContext(project_id="madup-prod1", key_id="deploy-key-1")


class RecordingMemoryStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.saved_operation_states: list[OperationState] = []

    def save_operation(
        self,
        operation_record: Operation,
        *,
        expected_version: int,
    ) -> Operation:
        saved = super().save_operation(
            operation_record,
            expected_version=expected_version,
        )
        self.saved_operation_states.append(saved.state)
        return saved


class FakeSourceSnapshotPort:
    def __init__(
        self,
        *,
        snapshot: Mapping[str, bytes] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = dict(snapshot or load_fixture_snapshot("nextjs"))
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def fetch_snapshot(
        self,
        admission_record: RepositoryAdmission,
    ) -> Mapping[str, bytes]:
        self.calls.append((str(admission_record.id), admission_record.admitted_sha))
        if self.error is not None:
            raise self.error
        return dict(self.snapshot)


def make_harness(
    *,
    store: RecordingMemoryStore | None = None,
    snapshot: Mapping[str, bytes] | None = None,
    operation_record: Operation | None = None,
    workload_record: Workload | None = None,
    admission_record: RepositoryAdmission | None = None,
    secret_record: SecretMetadata | None = None,
    secret_refs: tuple[SecretAttachmentReference, ...] = (),
    build: FakeBuildPort | None = None,
    registry: FakeArtifactRegistryPort | None = None,
    artifacts: FakeDesiredStateArtifactPort | None = None,
    runtime_identity: FakeRuntimeIdentityPort | None = None,
    runtime: FakeRuntimePort | None = None,
    source: FakeSourceSnapshotPort | None = None,
) -> SimpleNamespace:
    store = store or RecordingMemoryStore()
    current_admission = admission_record or admission()
    current_workload = workload_record or workload(
        admission_id=str(current_admission.id),
        source_sha=current_admission.admitted_sha,
    )
    current_operation = operation_record or operation(
        workload_id=str(current_workload.id),
    )
    current_snapshot = dict(snapshot or load_fixture_snapshot("nextjs"))

    store.create_repository_admission(current_admission)
    store.create_workload(current_workload)
    store.create_operation_once(current_operation)
    if secret_record is not None:
        store.create_secret_metadata(secret_record)

    queue = FakeDeploymentQueue()
    enqueuer = PrivateDeployEnqueuer(queue=queue)
    receipt = enqueuer.enqueue(
        operation_id=current_operation.id,
        expected_operation_version=current_operation.version,
        workload_id=current_workload.id,
        expected_workload_version=current_workload.version,
        admission_id=current_admission.id,
        expected_admission_version=current_admission.version,
        expected_source_sha=current_workload.source_sha,
        idempotency_key=current_operation.idempotency_key,
        queued_at=NOW,
        snapshot=current_snapshot,
        secret_attachments=secret_refs,
    )
    fake_build = build or FakeBuildPort()
    fake_registry = registry or FakeArtifactRegistryPort()
    fake_artifacts = artifacts or FakeDesiredStateArtifactPort()
    fake_runtime_identity = runtime_identity or FakeRuntimeIdentityPort(
        email=runtime_identity_spec(
            project_id=context().project_id,
            workload_id=str(current_workload.id),
        ).email
    )
    fake_runtime = runtime or FakeRuntimePort()
    fake_secrets = FakeSecretMetadataPort(store=store)
    fake_source = source or FakeSourceSnapshotPort(snapshot=current_snapshot)
    worker = PrivateDeployWorker(
        store=store,
        queue=queue,
        source=fake_source,
        build=fake_build,
        registry=fake_registry,
        artifacts=fake_artifacts,
        runtime_identity=fake_runtime_identity,
        runtime=fake_runtime,
        secrets=fake_secrets,
        render_context=context(),
        signing_key=KEY,
    )
    return SimpleNamespace(
        store=store,
        queue=queue,
        enqueuer=enqueuer,
        receipt=receipt,
        task=receipt.task,
        build=fake_build,
        registry=fake_registry,
        artifacts=fake_artifacts,
        runtime_identity=fake_runtime_identity,
        runtime=fake_runtime,
        source=fake_source,
        secrets=fake_secrets,
        worker=worker,
        operation=current_operation,
        workload=current_workload,
        admission=current_admission,
    )


def run_worker(
    harness: SimpleNamespace,
    *,
    when: datetime = NOW,
) -> DeployWorkerResult:
    return harness.worker.run(operation_id=str(harness.operation.id), now=when)


def override_task(harness: SimpleNamespace, **changes: object) -> None:
    harness.queue._tasks_by_operation[harness.operation.id] = dataclasses.replace(  # type: ignore[attr-defined]
        harness.task,
        **changes,
    )


class ConflictingArtifactPort(FakeDesiredStateArtifactPort):
    def create_once(self, *, operation_id: OperationId, envelope):  # type: ignore[override]
        raise ArtifactConflictError("artifact conflict")


class DeployingMutationStore(RecordingMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.on_deploying: Callable[[], None] | None = None
        self._mutated = False

    def save_operation(
        self,
        operation_record: Operation,
        *,
        expected_version: int,
    ) -> Operation:
        saved = super().save_operation(
            operation_record,
            expected_version=expected_version,
        )
        if (
            saved.state is OperationState.DEPLOYING
            and not self._mutated
            and self.on_deploying is not None
        ):
            self._mutated = True
            self.on_deploying()
        return saved


class PrivateWorkerTests(unittest.TestCase):
    def test_matching_disabled_hostname_binding_reactivates_on_healthy_redeploy(
        self,
    ) -> None:
        harness = make_harness()
        service_resource = cloud_run_service_name(
            project_id=context().project_id,
            region=REGION,
            workload_id=str(harness.workload.id),
        )
        service_uri = f"https://{service_resource.rsplit('/', 1)[1]}-uc.a.run.app"
        created = AppHostnameBindingService(store=harness.store).create_active_binding(
            workload=harness.workload,
            service_resource=service_resource,
            service_uri=service_uri,
            now=NOW,
        )
        harness.store.save_app_hostname_binding(
            created.transition_state(
                AppHostnameBindingState.DISABLED,
                at=NOW + timedelta(seconds=1),
            ),
            expected_version=created.version,
        )

        result = run_worker(harness, when=NOW + timedelta(seconds=2))

        binding = harness.store.get_app_hostname_binding(created.public_host)
        self.assertEqual(result.operation.state, OperationState.SUCCEEDED)
        self.assertEqual(binding.state, AppHostnameBindingState.ACTIVE)
        self.assertEqual(binding.version, created.version + 2)

    def test_retired_or_drifted_hostname_binding_quarantines_redeploy(self) -> None:
        scenarios = (
            ("retired", AppHostnameBindingState.RETIRED, None),
            (
                "drifted-uri",
                AppHostnameBindingState.DISABLED,
                "https://mim-svc-5251ebcdff9f-abcdefg-an.a.run" + ".app",
            ),
        )
        for label, state, service_uri in scenarios:
            with self.subTest(case=label):
                harness = make_harness()
                service_resource = cloud_run_service_name(
                    project_id=context().project_id,
                    region=REGION,
                    workload_id=str(harness.workload.id),
                )
                created = AppHostnameBindingService(
                    store=harness.store
                ).create_active_binding(
                    workload=harness.workload,
                    service_resource=service_resource,
                    service_uri=service_uri
                    or f"https://{service_resource.rsplit('/', 1)[1]}-uc.a.run.app",
                    now=NOW,
                )
                harness.store.save_app_hostname_binding(
                    created.transition_state(
                        state,
                        at=NOW + timedelta(seconds=1),
                    ),
                    expected_version=created.version,
                )

                result = run_worker(harness, when=NOW + timedelta(seconds=2))
                binding = harness.store.get_app_hostname_binding(created.public_host)

                self.assertEqual(result.operation.state, OperationState.QUARANTINED)
                self.assertEqual(result.operation.sanitized_failure, "deploy_denied")
                self.assertEqual(binding.state, state)

    def test_public_enqueue_surface_only_exposes_queue_port(self) -> None:
        self.assertEqual(
            [field.name for field in dataclasses.fields(PrivateDeployEnqueuer)],
            ["queue"],
        )
        queue = FakeDeploymentQueue()
        enqueuer = PrivateDeployEnqueuer(queue=queue)
        self.assertIsInstance(
            enqueuer.enqueue(
                operation_id=OperationId("op-1"),
                expected_operation_version=1,
                workload_id=WorkloadId("wrk-1"),
                expected_workload_version=2,
                admission_id=RepositoryAdmissionId("repo-1"),
                expected_admission_version=3,
                expected_source_sha="b" * 40,
                idempotency_key="idem-1",
                queued_at=NOW,
                snapshot=load_fixture_snapshot("nextjs"),
            ),
            DeploymentQueueReceipt,
        )
        with self.assertRaises(TypeError):
            PrivateDeployEnqueuer(queue=queue, build=FakeBuildPort())  # type: ignore[call-arg]

    def test_happy_path_uses_exact_state_sequence_and_one_effect_chain(self) -> None:
        secret_record = secret_metadata()
        secret_refs = (
            SecretAttachmentReference(
                secret_id=str(secret_record.id),
                secret_version=secret_record.active_version,
                metadata_version=secret_record.version,
            ),
        )
        harness = make_harness(
            secret_record=secret_record,
            secret_refs=secret_refs,
        )

        result = harness.worker.run(operation_id=str(harness.operation.id), now=NOW)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.operation.state, OperationState.SUCCEEDED)
        self.assertEqual(
            harness.store.saved_operation_states,
            [
                OperationState.BUILDING,
                OperationState.DEPLOYING,
                OperationState.VERIFYING,
                OperationState.SUCCEEDED,
            ],
        )
        self.assertEqual(len(harness.build.calls), 1)
        self.assertEqual(len(harness.registry.calls), 1)
        self.assertEqual(len(harness.artifacts.calls), 1)
        self.assertEqual(harness.runtime_identity.calls, [harness.workload.id])
        self.assertEqual(len(harness.runtime.apply_calls), 1)
        self.assertEqual(len(harness.runtime.health_checks), 1)
        self.assertEqual(harness.runtime.rollback_calls, [])
        self.assertEqual(len(harness.secrets.calls), 1)
        self.assertIn(
            "pkg.dev",
            harness.runtime.apply_calls[0].image_uri,
        )

    def test_runtime_identity_is_ensured_after_build_before_secret_resolution(
        self,
    ) -> None:
        events: list[str] = []
        identity = FakeRuntimeIdentityPort(
            email=runtime_identity_spec(
                project_id=context().project_id,
                workload_id="wrk-1",
            ).email,
            on_ensure=lambda _: events.append("identity"),
        )
        build = FakeBuildPort(on_build=lambda _: events.append("build"))
        harness = make_harness(build=build, runtime_identity=identity)

        result = run_worker(harness)

        self.assertEqual(result.operation.state, OperationState.SUCCEEDED)
        self.assertEqual(events, ["build", "identity"])
        self.assertEqual(len(harness.secrets.calls), 1)

    def test_runtime_identity_failure_stops_before_secrets_and_runtime(self) -> None:
        identity = FakeRuntimeIdentityPort(
            error=ExecutionPlaneError(f"identity failed {SECRET_VALUE}")
        )
        harness = make_harness(runtime_identity=identity)

        result = run_worker(harness)

        self.assertEqual(result.operation.state, OperationState.FAILED)
        self.assertEqual(result.operation.sanitized_failure, "deploy_failed")
        self.assertEqual(len(harness.build.calls), 1)
        self.assertEqual(harness.secrets.calls, [])
        self.assertEqual(harness.artifacts.calls, [])
        self.assertEqual(harness.runtime.apply_calls, [])
        self.assertNotIn(SECRET_VALUE, repr(result))

    def test_duplicate_enqueue_and_terminal_redelivery_are_idempotent(
        self,
    ) -> None:
        harness = make_harness()

        replay = harness.enqueuer.enqueue(
            operation_id=harness.operation.id,
            expected_operation_version=harness.operation.version,
            workload_id=harness.workload.id,
            expected_workload_version=harness.workload.version,
            admission_id=harness.admission.id,
            expected_admission_version=harness.admission.version,
            expected_source_sha=harness.workload.source_sha,
            idempotency_key=harness.operation.idempotency_key,
            queued_at=NOW + timedelta(seconds=30),
            snapshot=load_fixture_snapshot("nextjs"),
        )
        self.assertFalse(replay.created)
        self.assertEqual(replay.task, harness.task)

        with self.assertRaises(TaskConflictError):
            harness.enqueuer.enqueue(
                operation_id=harness.operation.id,
                expected_operation_version=harness.operation.version,
                workload_id=harness.workload.id,
                expected_workload_version=harness.workload.version,
                admission_id=harness.admission.id,
                expected_admission_version=harness.admission.version,
                expected_source_sha=harness.workload.source_sha,
                idempotency_key=harness.operation.idempotency_key,
                queued_at=NOW,
                snapshot={"app/page.tsx": b"different"},
            )

        first = run_worker(harness)
        build_count = len(harness.build.calls)
        registry_count = len(harness.registry.calls)
        artifact_count = len(harness.artifacts.calls)
        apply_count = len(harness.runtime.apply_calls)
        health_count = len(harness.runtime.health_checks)

        replay_result = run_worker(harness, when=NOW + timedelta(seconds=1))
        self.assertEqual(first.operation, replay_result.operation)
        self.assertEqual(len(harness.build.calls), build_count)
        self.assertEqual(len(harness.registry.calls), registry_count)
        self.assertEqual(len(harness.artifacts.calls), artifact_count)
        self.assertEqual(len(harness.runtime.apply_calls), apply_count)
        self.assertEqual(len(harness.runtime.health_checks), health_count)

    def test_preflight_only_allows_active_or_failed_workloads(self) -> None:
        allowed_states = (WorkloadState.ACTIVE, WorkloadState.FAILED)
        denied_states = (
            WorkloadState.PAUSED,
            WorkloadState.QUARANTINED,
            WorkloadState.ARCHIVED,
        )
        for state in allowed_states:
            with self.subTest(state=state.value):
                harness = make_harness(workload_record=workload(state=state))
                result = run_worker(harness)
                self.assertEqual(result.operation.state, OperationState.SUCCEEDED)

        for state in denied_states:
            with self.subTest(state=f"deny:{state.value}"):
                harness = make_harness(workload_record=workload(state=state))
                result = run_worker(harness)
                self.assertEqual(result.operation.state, OperationState.QUARANTINED)
                self.assertEqual(harness.build.calls, [])

    def test_concurrent_worker_calls_only_build_once(self) -> None:
        entered_build = threading.Event()
        release_build = threading.Event()

        def on_build(_request) -> None:
            entered_build.set()
            release_build.wait(timeout=2)

        harness = make_harness(build=FakeBuildPort(on_build=on_build))
        barrier = threading.Barrier(2)
        results: list[DeployWorkerResult] = []

        def worker_result() -> DeployWorkerResult:
            return run_worker(harness)

        def run_thread() -> None:
            barrier.wait(timeout=2)
            results.append(worker_result())

        threads = [threading.Thread(target=run_thread) for _ in range(2)]
        for thread in threads:
            thread.start()

        self.assertTrue(entered_build.wait(timeout=2))
        release_build.set()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(harness.build.calls), 1)
        self.assertLessEqual(len(harness.registry.calls), 1)
        self.assertLessEqual(len(harness.artifacts.calls), 1)
        self.assertLessEqual(len(harness.runtime.apply_calls), 1)
        self.assertLessEqual(len(harness.runtime.health_checks), 1)
        statuses = sorted(result.status for result in results)
        self.assertEqual(statuses[0], "completed")
        self.assertIn(statuses[1], {"completed", "in_progress"})

    def test_midflight_workload_drift_is_denied_before_runtime_apply(self) -> None:
        build = FakeBuildPort()
        harness = make_harness(build=build)

        def on_build(_request) -> None:
            updated = harness.workload.transition_state(
                WorkloadState.PAUSED,
                at=NOW + timedelta(seconds=1),
            )
            harness.store.save_workload(
                updated,
                expected_version=harness.workload.version,
            )

        build.on_build = on_build

        result = run_worker(harness)

        self.assertEqual(result.operation.state, OperationState.QUARANTINED)
        self.assertEqual(harness.runtime.apply_calls, [])

    def test_midflight_admission_drift_is_denied_before_runtime_apply(self) -> None:
        build = FakeBuildPort()
        harness = make_harness(build=build)

        def on_build(_request) -> None:
            updated = harness.admission.transition_state(
                RepositoryAdmissionState.REVOKED,
                at=NOW + timedelta(seconds=1),
            )
            harness.store.save_repository_admission(
                updated,
                expected_version=harness.admission.version,
            )

        build.on_build = on_build

        result = run_worker(harness)

        self.assertEqual(result.operation.state, OperationState.QUARANTINED)
        self.assertEqual(harness.runtime.apply_calls, [])

    def test_post_deploying_drift_is_denied_before_runtime_apply(self) -> None:
        for case in ("workload", "admission"):
            with self.subTest(case=case):
                store = DeployingMutationStore()
                harness = make_harness(store=store)

                def mutate_workload() -> None:
                    updated = harness.workload.transition_state(
                        WorkloadState.PAUSED,
                        at=NOW + timedelta(seconds=1),
                    )
                    store.save_workload(
                        updated,
                        expected_version=harness.workload.version,
                    )

                def mutate_admission() -> None:
                    updated = harness.admission.transition_state(
                        RepositoryAdmissionState.REVOKED,
                        at=NOW + timedelta(seconds=1),
                    )
                    store.save_repository_admission(
                        updated,
                        expected_version=harness.admission.version,
                    )

                store.on_deploying = (
                    mutate_workload if case == "workload" else mutate_admission
                )

                result = run_worker(harness)

                self.assertEqual(result.operation.state, OperationState.QUARANTINED)
                self.assertEqual(harness.runtime.apply_calls, [])

    def test_stale_or_mismatched_records_are_quarantined_before_build(self) -> None:
        cases: tuple[tuple[str, dict[str, object]], ...] = (
            (
                "operation_version",
                dict(
                    operation_expected_version=2,
                ),
            ),
            (
                "workload_version",
                dict(
                    workload_expected_version=5,
                ),
            ),
            (
                "admission_version",
                dict(
                    admission_expected_version=3,
                ),
            ),
            (
                "relation",
                dict(workload_record=workload(admission_id="repo-other")),
            ),
            (
                "state",
                dict(
                    admission_record=admission(state=RepositoryAdmissionState.PENDING)
                ),
            ),
            (
                "workload_sha",
                dict(workload_record=workload(source_sha="c" * 40)),
            ),
            (
                "admission_sha",
                dict(
                    admission_record=admission(admitted_sha="c" * 40),
                    workload_record=workload(source_sha="b" * 40),
                ),
            ),
            (
                "owner",
                dict(admission_record=admission(owner="other-owner")),
            ),
        )
        for name, mutation in cases:
            with self.subTest(case=name):
                current_admission = (
                    cast(
                        RepositoryAdmission | None,
                        mutation.get("admission_record"),
                    )
                    or admission()
                )
                current_workload = cast(
                    Workload | None,
                    mutation.get("workload_record"),
                ) or workload(
                    admission_id=str(current_admission.id),
                    source_sha=current_admission.admitted_sha,
                )
                current_operation = cast(
                    Operation | None,
                    mutation.get("operation_record"),
                ) or operation(
                    workload_id=str(current_workload.id),
                )
                harness = make_harness(
                    admission_record=current_admission,
                    workload_record=current_workload,
                    operation_record=current_operation,
                )
                if "operation_expected_version" in mutation:
                    override_task(
                        harness,
                        expected_operation_version=cast(
                            int,
                            mutation["operation_expected_version"],
                        ),
                    )
                if "workload_expected_version" in mutation:
                    override_task(
                        harness,
                        expected_workload_version=cast(
                            int,
                            mutation["workload_expected_version"],
                        ),
                    )
                if "admission_expected_version" in mutation:
                    override_task(
                        harness,
                        expected_admission_version=cast(
                            int,
                            mutation["admission_expected_version"],
                        ),
                    )

                result = run_worker(harness)

                self.assertEqual(result.operation.state, OperationState.QUARANTINED)
                self.assertEqual(result.operation.sanitized_failure, "deploy_denied")
                self.assertEqual(harness.build.calls, [])

    def test_build_digest_and_artifact_tamper_are_denied(self) -> None:
        malformed = make_harness(
            build=FakeBuildPort(digest_override="sha256:" + "a" * 64)
        )
        malformed_result = run_worker(malformed)
        self.assertEqual(malformed_result.operation.state, OperationState.QUARANTINED)
        self.assertEqual(malformed.registry.calls, [])

        conflict = make_harness(artifacts=ConflictingArtifactPort())
        conflict_result = run_worker(conflict)
        self.assertEqual(conflict_result.operation.state, OperationState.QUARANTINED)
        self.assertEqual(conflict_result.operation.sanitized_failure, "deploy_denied")

        tampered = make_harness(
            artifacts=FakeDesiredStateArtifactPort(tamper_signature=True)
        )
        tampered_result = run_worker(tampered)
        self.assertEqual(tampered_result.operation.state, OperationState.QUARANTINED)
        self.assertEqual(tampered_result.operation.sanitized_failure, "deploy_denied")

    def test_unhealthy_deploy_rolls_back_or_fails_closed(self) -> None:
        rollback = make_harness(runtime=FakeRuntimePort(healthy=False))
        rollback_result = run_worker(rollback)
        self.assertEqual(rollback_result.operation.state, OperationState.ROLLED_BACK)
        self.assertEqual(
            rollback.runtime.rollback_calls[0].image_digest,
            str(rollback.workload.last_healthy_image_digest),
        )
        self.assertEqual(
            rollback.runtime.rollback_calls[0].workload_owner_id,
            rollback.workload.owner_id,
        )

        for digest in (None, "sha256:NOT-VALID"):
            with self.subTest(digest=digest):
                harness = make_harness(
                    workload_record=workload(last_healthy_image_digest=digest),
                    runtime=FakeRuntimePort(healthy=False),
                )
                result = run_worker(harness)
                self.assertEqual(result.operation.state, OperationState.FAILED)
                self.assertEqual(result.operation.sanitized_failure, "deploy_unhealthy")
                self.assertEqual(harness.runtime.rollback_calls, [])

    def test_rollback_re_reads_current_workload_digest(self) -> None:
        new_digest = "sha256:" + "b" * 64
        harness = make_harness()

        class RefreshingRuntime(FakeRuntimePort):
            def verify_health(self, desired_state) -> bool:  # type: ignore[override]
                updated = dataclasses.replace(
                    harness.workload,
                    last_healthy_image_digest=new_digest,
                    updated_at=NOW + timedelta(seconds=1),
                    version=harness.workload.version + 1,
                )
                harness.store.save_workload(
                    updated,
                    expected_version=harness.workload.version,
                )
                return super().verify_health(desired_state)

        harness.runtime = RefreshingRuntime(healthy=False)
        harness.worker = dataclasses.replace(harness.worker, runtime=harness.runtime)
        result = run_worker(harness)
        self.assertEqual(result.operation.state, OperationState.ROLLED_BACK)
        self.assertEqual(harness.runtime.rollback_calls[0].image_digest, new_digest)
        self.assertEqual(
            harness.runtime.rollback_calls[0].workload_owner_id,
            harness.store.get_workload(harness.workload.id).owner_id,
        )

        invalid = make_harness()

        class InvalidatingRuntime(FakeRuntimePort):
            def verify_health(self, desired_state) -> bool:  # type: ignore[override]
                updated = dataclasses.replace(
                    invalid.workload,
                    last_healthy_image_digest=None,
                    updated_at=NOW + timedelta(seconds=1),
                    version=invalid.workload.version + 1,
                )
                invalid.store.save_workload(
                    updated,
                    expected_version=invalid.workload.version,
                )
                return super().verify_health(desired_state)

        invalid.runtime = InvalidatingRuntime(healthy=False)
        invalid.worker = dataclasses.replace(invalid.worker, runtime=invalid.runtime)
        invalid_result = run_worker(invalid)
        self.assertEqual(invalid_result.operation.state, OperationState.FAILED)
        self.assertEqual(invalid.runtime.rollback_calls, [])

    def test_secret_metadata_validation_uses_ids_and_versions_only(self) -> None:
        secret_record = secret_metadata()
        valid_ref = SecretAttachmentReference(
            secret_id=str(secret_record.id),
            secret_version=secret_record.active_version,
            metadata_version=secret_record.version,
        )
        harness = make_harness(
            secret_record=secret_record,
            secret_refs=(valid_ref,),
        )
        success = run_worker(harness)
        self.assertEqual(success.operation.state, OperationState.SUCCEEDED)
        self.assertEqual(harness.secrets.calls[0].attachments, (valid_ref,))

        mismatch_cases = (
            SecretAttachmentReference(
                secret_id=str(secret_record.id),
                secret_version=secret_record.active_version + 1,
                metadata_version=secret_record.version,
            ),
            SecretAttachmentReference(
                secret_id=str(secret_record.id),
                secret_version=secret_record.active_version,
                metadata_version=secret_record.version + 1,
            ),
        )
        for attachment in mismatch_cases:
            with self.subTest(attachment=attachment):
                denied = make_harness(
                    secret_record=secret_record,
                    secret_refs=(attachment,),
                )
                denied_result = run_worker(denied)
                self.assertEqual(
                    denied_result.operation.state, OperationState.QUARANTINED
                )
                self.assertEqual(
                    denied_result.operation.sanitized_failure, "deploy_denied"
                )

        wrong_workload_secret = secret_metadata(workload_id="wrk-other")
        wrong_workload = make_harness(
            secret_record=wrong_workload_secret,
            secret_refs=(
                SecretAttachmentReference(
                    secret_id=str(wrong_workload_secret.id),
                    secret_version=wrong_workload_secret.active_version,
                    metadata_version=wrong_workload_secret.version,
                ),
            ),
        )
        wrong_workload_result = run_worker(wrong_workload)
        self.assertEqual(
            wrong_workload_result.operation.state, OperationState.QUARANTINED
        )

    def test_snapshot_injection_is_ignored_or_denied(self) -> None:
        safe_snapshot = load_fixture_snapshot("nextjs")
        safe_snapshot["cloudbuild.yaml"] = b"steps:\n- name: attacker\n"
        safe_snapshot["main.tf"] = b'resource "google_project" "x" {}'
        safe = make_harness(snapshot=safe_snapshot)
        safe_result = run_worker(safe)
        self.assertEqual(safe_result.operation.state, OperationState.SUCCEEDED)
        self.assertEqual(
            safe.build.calls[0].build_command,
            ("./node_modules/.bin/next", "build"),
        )

        denied_snapshot = load_fixture_snapshot("scheduled_script")
        denied_snapshot["mim.yaml"] = (
            b"kind: scheduled_script\n"
            b"entrypoint: main.py\n"
            b"schedule: '0 * * * *'\n"
            b"build_steps:\n"
            b"  - gcloud builds submit\n"
        )
        denied = make_harness(
            snapshot=denied_snapshot,
            workload_record=workload(kind=WorkloadKind.SCHEDULED_SCRIPT),
        )
        denied_result = run_worker(denied)
        self.assertEqual(denied_result.operation.state, OperationState.QUARANTINED)
        self.assertEqual(denied.build.calls, [])

    def test_refetched_snapshot_attestation_mismatch_quarantines_before_build(
        self,
    ) -> None:
        harness = make_harness(
            source=FakeSourceSnapshotPort(snapshot={"app/page.tsx": b"different"}),
        )

        result = run_worker(harness)

        self.assertEqual(result.operation.state, OperationState.QUARANTINED)
        self.assertEqual(result.operation.sanitized_failure, "deploy_denied")
        self.assertEqual(harness.build.calls, [])
        self.assertEqual(harness.runtime.apply_calls, [])

    def test_source_fetch_failure_leaves_operation_queued_for_retry(self) -> None:
        harness = make_harness(
            source=FakeSourceSnapshotPort(
                error=RetryableExecutionPlaneError("source_fetch_failed")
            )
        )

        with self.assertRaises(RetryableExecutionPlaneError) as ctx:
            run_worker(harness)

        self.assertEqual(ctx.exception.sanitized_failure, "source_fetch_failed")
        self.assertEqual(
            harness.store.get_operation(harness.operation.id).state,
            OperationState.QUEUED,
        )
        self.assertEqual(harness.build.calls, [])
        self.assertEqual(harness.runtime.apply_calls, [])

    def test_github_source_unavailable_stays_retryable(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        harness = make_harness(
            source=FakeSourceSnapshotPort(
                error=github.GitHubSourceUnavailableError("transport down")
            )
        )

        with self.assertRaises(RetryableExecutionPlaneError):
            run_worker(harness)

        self.assertEqual(
            harness.store.get_operation(harness.operation.id).state,
            OperationState.QUEUED,
        )
        self.assertEqual(harness.build.calls, [])

    def test_github_source_integrity_failure_quarantines_before_build(self) -> None:
        github = importlib.import_module("mim_control_plane.adapters.github")
        harness = make_harness(
            source=FakeSourceSnapshotPort(
                error=github.GitHubSourceIntegrityError("repository metadata drifted")
            )
        )

        result = run_worker(harness)

        self.assertEqual(result.operation.state, OperationState.QUARANTINED)
        self.assertEqual(result.operation.sanitized_failure, "deploy_denied")
        self.assertEqual(harness.build.calls, [])

    def test_unclassified_source_failure_fails_closed_before_build(self) -> None:
        harness = make_harness(
            source=FakeSourceSnapshotPort(
                error=RuntimeError(f"unexpected source failure {SECRET_VALUE}")
            )
        )

        result = run_worker(harness)

        self.assertEqual(result.operation.state, OperationState.QUARANTINED)
        self.assertEqual(result.operation.sanitized_failure, "deploy_denied")
        self.assertNotIn(SECRET_VALUE, result.operation.sanitized_failure or "")
        self.assertEqual(harness.build.calls, [])
        self.assertEqual(harness.runtime.apply_calls, [])

    def test_errors_are_sanitized_and_sensitive_material_stays_out_of_repr(
        self,
    ) -> None:
        snapshot = load_fixture_snapshot("nextjs")
        snapshot["secret.txt"] = SECRET_VALUE.encode("utf-8")
        harness = make_harness(
            snapshot=snapshot,
            build=FakeBuildPort(
                error=ExecutionPlaneError(f"bad build {SECRET_VALUE} {KEY!r}")
            ),
        )

        result = run_worker(harness)

        self.assertEqual(result.operation.state, OperationState.FAILED)
        self.assertEqual(result.operation.sanitized_failure, "build_failed")
        self.assertNotIn(SECRET_VALUE, repr(harness.task))
        self.assertNotIn(SECRET_VALUE, repr(harness.worker))
        self.assertNotIn(str(KEY), repr(harness.worker))
        self.assertNotIn(SECRET_VALUE, repr(harness.build.calls))

        artifact_harness = make_harness(snapshot=snapshot)
        run_worker(artifact_harness)
        envelope = artifact_harness.artifacts.get(artifact_harness.operation.id)
        self.assertNotIn(SECRET_VALUE, repr(envelope))

    def test_runtime_health_execution_error_fails_deploy(self) -> None:
        harness = make_harness(
            runtime=FakeRuntimePort(
                health_error=ExecutionPlaneError("runtime health probe failed")
            )
        )

        result = run_worker(harness)

        self.assertEqual(result.operation.state, OperationState.FAILED)
        self.assertEqual(result.operation.sanitized_failure, "deploy_failed")
        self.assertEqual(len(harness.runtime.apply_calls), 1)

    def test_runtime_route_readback_error_fails_deploy(self) -> None:
        class ReadbackFailingRuntime(FakeRuntimePort):
            def readback_service_route(self, desired_state):  # type: ignore[override]
                raise ExecutionPlaneError("runtime route readback failed")

        harness = make_harness(runtime=ReadbackFailingRuntime())

        result = run_worker(harness)

        self.assertEqual(result.operation.state, OperationState.FAILED)
        self.assertEqual(result.operation.sanitized_failure, "deploy_failed")
        self.assertEqual(len(harness.runtime.apply_calls), 1)

    def test_recorded_operation_sequence_matches_closed_state_machine(self) -> None:
        harness = make_harness(runtime=FakeRuntimePort(healthy=False))

        run_worker(harness)

        sequence = [OperationState.QUEUED, *harness.store.saved_operation_states]
        self.assertEqual(
            sequence,
            [
                OperationState.QUEUED,
                OperationState.BUILDING,
                OperationState.DEPLOYING,
                OperationState.VERIFYING,
                OperationState.ROLLED_BACK,
            ],
        )
        for current, nxt in zip(sequence, sequence[1:]):
            self.assertIn(nxt, OPERATION_TRANSITIONS[current])
