from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import unittest
from datetime import UTC, datetime

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2

from mim_control_plane.adapters.cloud_tasks import (
    CloudTasksDeploymentQueue,
    CloudTasksSettings,
)
from mim_control_plane.domain.models import (
    OperationId,
    RepositoryAdmissionId,
    WorkloadId,
)
from mim_control_plane.ports.execution import (
    QueuedDeployTask,
    TaskConflictError,
    TaskNotFoundError,
)

NOW = datetime(2026, 8, 4, 6, 0, 0, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"
RUN_APP_SUFFIX = ".run" + ".app"
WORKER_AUDIENCE = f"https://mim-deploy-worker-abc-an.a{RUN_APP_SUFFIX}"
WORKER_URL = f"{WORKER_AUDIENCE}/internal/deploy"


def queued_task(*, idempotency_key: str = "idem-1") -> QueuedDeployTask:
    return QueuedDeployTask.from_snapshot(
        operation_id=OperationId("op-1"),
        expected_operation_version=1,
        workload_id=WorkloadId("wrk-1"),
        expected_workload_version=2,
        admission_id=RepositoryAdmissionId("adm-1"),
        expected_admission_version=3,
        expected_source_sha="a" * 40,
        idempotency_key=idempotency_key,
        queued_at=NOW,
        snapshot={
            "app.py": b"secret-source-marker" + (b"x" * 240_000),
            "package.json": b'{"name":"demo"}' + (b"y" * 240_000),
            "app/page.tsx": b"export default function Page() {}" + (b"z" * 220_000),
        },
    )


class FakeDurableDeployTaskStore:
    def __init__(self) -> None:
        self._by_operation: dict[OperationId, QueuedDeployTask] = {}
        self._by_idempotency: dict[str, QueuedDeployTask] = {}
        self.create_calls = 0

    def create_deploy_task_once(
        self,
        task: QueuedDeployTask,
    ) -> tuple[QueuedDeployTask, bool]:
        self.create_calls += 1
        existing = self._by_operation.get(task.operation_id)
        if existing is None:
            existing = self._by_idempotency.get(task.idempotency_key)
        if existing is not None:
            if existing.material_hash != task.material_hash:
                raise TaskConflictError("queued deploy task material changed.")
            return existing, False
        self._by_operation[task.operation_id] = task
        self._by_idempotency[task.idempotency_key] = task
        return task, True

    def get_deploy_task(self, operation_id: OperationId) -> QueuedDeployTask:
        try:
            return self._by_operation[operation_id]
        except KeyError:
            raise TaskNotFoundError("queued deploy task was not found.") from None


class FakeCloudTasksClient:
    def __init__(self) -> None:
        self.create_requests: list[tasks_v2.CreateTaskRequest] = []
        self.get_requests: list[tasks_v2.GetTaskRequest] = []
        self.raise_already_exists = False
        self.existing_task: tasks_v2.Task | None = None

    def create_task(
        self,
        *,
        request: tasks_v2.CreateTaskRequest,
    ) -> tasks_v2.Task:
        self.create_requests.append(request)
        if self.raise_already_exists:
            raise AlreadyExists("already exists")
        created = copy.deepcopy(request.task)
        self.existing_task = created
        return created

    def get_task(
        self,
        *,
        request: tasks_v2.GetTaskRequest,
    ) -> tasks_v2.Task:
        self.get_requests.append(request)
        if self.existing_task is None:
            raise AssertionError("test did not configure an existing task")
        return copy.deepcopy(self.existing_task)


def settings(**overrides: str) -> CloudTasksSettings:
    values = {
        "project_id": PROJECT_ID,
        "location": "asia-northeast3",
        "queue_id": "mim-private-workers",
        "worker_url": WORKER_URL,
        "worker_audience": WORKER_AUDIENCE,
        "oidc_service_account_email": (
            f"mim-deploy-worker@{PROJECT_ID}.iam.gserviceaccount.com"
        ),
    }
    values.update(overrides)
    return CloudTasksSettings(**values)


class CloudTasksDeliveryTests(unittest.TestCase):
    def build_queue(
        self,
    ) -> tuple[
        CloudTasksDeploymentQueue,
        FakeDurableDeployTaskStore,
        FakeCloudTasksClient,
    ]:
        material_store = FakeDurableDeployTaskStore()
        client = FakeCloudTasksClient()
        queue = CloudTasksDeploymentQueue(
            settings=settings(),
            material_store=material_store,
            client=client,
        )
        return queue, material_store, client

    def test_enqueue_persists_material_and_sends_only_a_small_reference(self) -> None:
        queue, material_store, client = self.build_queue()
        task = queued_task()

        receipt = queue.enqueue(task)

        self.assertTrue(receipt.created)
        self.assertEqual(material_store.get_deploy_task(task.operation_id), task)
        self.assertEqual(len(client.create_requests), 1)
        request = client.create_requests[0]
        self.assertEqual(
            request.parent,
            (
                f"projects/{PROJECT_ID}/locations/asia-northeast3/"
                "queues/mim-private-workers"
            ),
        )
        expected_suffix = hashlib.sha256(
            b"mim:deploy-task:v1\x00" + task.idempotency_key.encode()
        ).hexdigest()
        self.assertEqual(
            request.task.name,
            f"{request.parent}/tasks/deploy-{expected_suffix}",
        )
        http_request = request.task.http_request
        self.assertEqual(http_request.http_method, tasks_v2.HttpMethod.POST)
        self.assertEqual(http_request.url, WORKER_URL)
        self.assertEqual(
            http_request.oidc_token.service_account_email,
            f"mim-deploy-worker@{PROJECT_ID}.iam.gserviceaccount.com",
        )
        self.assertEqual(http_request.oidc_token.audience, WORKER_AUDIENCE)
        self.assertEqual(
            dict(http_request.headers),
            {"Content-Type": "application/json"},
        )
        self.assertLess(len(http_request.body), 1024)
        payload = json.loads(http_request.body)
        self.assertEqual(
            payload,
            {
                "material_hash": task.material_hash,
                "operation_id": str(task.operation_id),
                "schema": "mim.deploy.ref.v1",
            },
        )
        rendered = http_request.body.decode("utf-8")
        self.assertNotIn(task.idempotency_key, rendered)
        self.assertNotIn(task.expected_source_sha, rendered)
        self.assertNotIn("secret-source-marker", rendered)
        self.assertNotIn("authorization", str(dict(http_request.headers)).casefold())

    def test_queued_task_exposes_attestation_only_not_snapshot_bytes(self) -> None:
        task = queued_task()

        self.assertFalse(hasattr(task, "snapshot"))
        self.assertEqual(task.expected_snapshot_file_count, 3)
        self.assertGreater(task.expected_snapshot_byte_count, 700_000)
        self.assertNotIn("secret-source-marker", repr(task))
        self.assertNotIn("app.py", repr(task))

    def test_duplicate_delivery_verifies_the_exact_existing_cloud_task(self) -> None:
        queue, _, client = self.build_queue()
        task = queued_task()
        first = queue.enqueue(task)
        self.assertTrue(first.created)
        client.raise_already_exists = True

        replay = queue.enqueue(task)

        self.assertFalse(replay.created)
        self.assertEqual(len(client.get_requests), 1)
        self.assertEqual(client.get_requests[0].name, client.existing_task.name)
        self.assertEqual(
            client.get_requests[0].response_view,
            tasks_v2.Task.View.FULL,
        )

    def test_existing_cloud_task_drift_fails_closed(self) -> None:
        queue, _, client = self.build_queue()
        task = queued_task()
        queue.enqueue(task)
        assert client.existing_task is not None
        client.existing_task.http_request.url = (
            f"https://mim-deploy-worker-evil-an.a{RUN_APP_SUFFIX}/internal/deploy"
        )
        client.raise_already_exists = True

        with self.assertRaises(TaskConflictError):
            queue.enqueue(task)

    def test_material_conflict_never_calls_cloud_tasks(self) -> None:
        queue, _, client = self.build_queue()
        queue.enqueue(queued_task())

        with self.assertRaises(TaskConflictError):
            queue.enqueue(queued_task(idempotency_key="different"))

        self.assertEqual(len(client.create_requests), 1)

    def test_get_uses_durable_storage_after_process_restart(self) -> None:
        queue, material_store, client = self.build_queue()
        task = queued_task()
        queue.enqueue(task)
        restarted = CloudTasksDeploymentQueue(
            settings=settings(),
            material_store=material_store,
            client=client,
        )
        self.assertEqual(restarted.get(task.operation_id), task)

    def test_oversized_reference_is_rejected_before_durable_or_remote_write(
        self,
    ) -> None:
        queue, material_store, client = self.build_queue()
        task = dataclasses.replace(
            queued_task(),
            operation_id=OperationId("op-" + ("x" * 5000)),
        )

        with self.assertRaises(ValueError):
            queue.enqueue(task)

        self.assertEqual(material_store.create_calls, 0)
        self.assertEqual(client.create_requests, [])

    def test_settings_reject_credential_and_target_substitution(self) -> None:
        invalid = (
            {"worker_url": "http://mim-deploy-worker-an.a.run.app/internal/deploy"},
            {"worker_url": f"{WORKER_AUDIENCE}/other"},
            {"worker_audience": f"https://other-an.a{RUN_APP_SUFFIX}"},
            {"worker_audience": f"{WORKER_AUDIENCE}/"},
            {
                "oidc_service_account_email": (
                    f"employee@{PROJECT_ID}.iam.gserviceaccount.com"
                )
            },
            {"project_id": "Other_Project"},
            {"queue_id": "../other"},
        )
        for override in invalid:
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    settings(**override)


if __name__ == "__main__":
    unittest.main()
