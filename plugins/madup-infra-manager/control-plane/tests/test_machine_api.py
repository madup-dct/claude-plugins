from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.domain.models import (  # noqa: E402
    OperationId,
    RepositoryAdmissionId,
    WorkloadId,
)
from mim_control_plane.machine_api import (  # noqa: E402
    build_deploy_worker_app,
    build_schedule_gateway_app,
)
from mim_control_plane.ports.execution import (  # noqa: E402
    QueuedDeployTask,
    RetryableExecutionPlaneError,
    TaskNotFoundError,
)
from mim_control_plane.security.google_machine_identity import (  # noqa: E402
    MachineRequestDenied,
)
from mim_control_plane.services.schedule_management import ScheduleDenied  # noqa: E402

NOW = datetime(2026, 8, 4, 6, 0, 0, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"
REGION = "asia-northeast3"
RUN_APP_SUFFIX = ".run" + ".app"
DEPLOY_AUDIENCE = (
    "https://mim-deploy-worker-123456789012.asia-northeast3" + RUN_APP_SUFFIX
)
DEPLOY_SERVICE_ACCOUNT = (
    "mim-deploy-worker@mim-prod-123456.iam.gserviceaccount.com"
)
SCHEDULE_AUDIENCE = (
    "https://mim-schedule-gateway-123456789012.asia-northeast3" + RUN_APP_SUFFIX
)
SCHEDULE_SERVICE_ACCOUNT = (
    "mim-schedule-gateway@mim-prod-123456.iam.gserviceaccount.com"
)
APP_GATEWAY_SERVICE_ACCOUNT = (
    "mim-app-gateway@mim-prod-123456.iam.gserviceaccount.com"
)


def queued_task() -> QueuedDeployTask:
    return QueuedDeployTask.from_snapshot(
        operation_id=OperationId("op-1"),
        expected_operation_version=1,
        workload_id=WorkloadId("wrk-1"),
        expected_workload_version=2,
        admission_id=RepositoryAdmissionId("adm-1"),
        expected_admission_version=3,
        expected_source_sha="a" * 40,
        idempotency_key="idem-1",
        queued_at=NOW,
        snapshot={"app.py": b"print('ok')"},
    )


class AllowAllAuthenticator:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[tuple[str, str], ...], str]] = []

    def authenticate(
        self,
        headers: tuple[tuple[str, str], ...],
        *,
        expected_service_account_email: str,
    ) -> None:
        self.calls.append((headers, expected_service_account_email))


@dataclasses.dataclass
class FakeWorkerResult:
    status: str


class FakeDeployWorker:
    def __init__(self, *, status: str = "completed") -> None:
        self.status = status
        self.calls: list[tuple[str, datetime]] = []

    def run(self, *, operation_id: str, now: datetime) -> FakeWorkerResult:
        self.calls.append((operation_id, now))
        return FakeWorkerResult(status=self.status)


class RetryingDeployWorker:
    def __init__(self, *, failure: str) -> None:
        self.failure = failure
        self.calls: list[tuple[str, datetime]] = []

    def run(self, *, operation_id: str, now: datetime) -> FakeWorkerResult:
        self.calls.append((operation_id, now))
        raise RetryableExecutionPlaneError(self.failure)


class FakeScheduleManagement:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, datetime]] = []

    def execute_schedule_tick(
        self,
        *,
        schedule_id: str,
        workload_id: str,
        tick_at: datetime,
    ) -> dict[str, object]:
        self.calls.append((schedule_id, workload_id, tick_at))
        return {
            "action": "execute_schedule_tick",
            "schedule_id": schedule_id,
            "outcome": "succeeded",
            "state": "enabled",
        }


class FakeAppAuthorization:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def authorize(self, request: object) -> object:
        payload = dataclasses.asdict(request)
        self.calls.append(payload)
        return dataclasses.make_dataclass(
            "Decision",
            [
                ("schema", str),
                ("public_host", str),
                ("workload_id", str),
                ("upstream_url", str),
                ("upstream_audience", str),
                ("expires_at", datetime),
            ],
            frozen=True,
        )(
            schema="mim.app-authorization.v1",
            public_host=payload["public_host"],
            workload_id="wrk-1",
            upstream_url="https://mim-svc-bde131f06b2f-abcdefg-an.a"
            + RUN_APP_SUFFIX,
            upstream_audience="https://mim-svc-bde131f06b2f-abcdefg-an.a"
            + RUN_APP_SUFFIX,
            expires_at=NOW + timedelta(seconds=30),
        )


class RouteAwareAuthenticator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def authenticate(
        self,
        headers: tuple[tuple[str, str], ...],
        *,
        expected_service_account_email: str,
    ) -> None:
        header_map = {name.lower(): value for name, value in headers}
        token = header_map.get("authorization", "")
        self.calls.append((token, expected_service_account_email))
        if token == "Bearer schedule-token":
            self._expect(
                expected_service_account_email,
                SCHEDULE_SERVICE_ACCOUNT,
            )
            return
        if token == "Bearer machine-token":
            self._expect(
                expected_service_account_email,
                APP_GATEWAY_SERVICE_ACCOUNT,
            )
            return
        raise MachineRequestDenied("Machine request was denied.")

    @staticmethod
    def _expect(observed: str, expected: str) -> None:
        if observed != expected:
            raise MachineRequestDenied("Machine request was denied.")


class RecordingReadinessCheck:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


class FakeQueue:
    def __init__(self, task: QueuedDeployTask | None = None) -> None:
        self.task = task or queued_task()
        self.calls: list[OperationId] = []

    def get(self, operation_id: OperationId) -> QueuedDeployTask:
        self.calls.append(operation_id)
        if str(operation_id) != str(self.task.operation_id):
            raise TaskNotFoundError("missing")
        return self.task


class MissingTaskQueue:
    def get(self, operation_id: OperationId) -> QueuedDeployTask:
        raise TaskNotFoundError(str(operation_id))


def deploy_body(task: QueuedDeployTask) -> bytes:
    return json.dumps(
        {
            "material_hash": task.material_hash,
            "operation_id": str(task.operation_id),
            "schema": "mim.deploy.ref.v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def deploy_headers(task: QueuedDeployTask) -> dict[str, str]:
    task_name = "deploy-" + hashlib.sha256(
        b"mim:deploy-task:v1\x00" + task.idempotency_key.encode("utf-8")
    ).hexdigest()
    return {
        "Authorization": "Bearer machine-token",
        "Content-Type": "application/json",
        "X-CloudTasks-QueueName": "mim-private-workers",
        "X-CloudTasks-TaskName": task_name,
        "X-CloudTasks-TaskRetryCount": "0",
    }


def schedule_headers(schedule_id: str) -> dict[str, str]:
    job_suffix = hashlib.sha256(schedule_id.encode("utf-8")).hexdigest()[:20]
    return {
        "Authorization": "Bearer schedule-token",
        "Content-Type": "application/json",
        "X-CloudScheduler": "true",
        "X-CloudScheduler-JobName": (
            f"projects/{PROJECT_ID}/locations/{REGION}/jobs/mim-sch-{job_suffix}"
        ),
        "X-CloudScheduler-ScheduleTime": "2026-08-04T06:00:00Z",
    }


def app_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer machine-token",
        "Content-Type": "application/json",
    }


class MachineApiTests(unittest.TestCase):
    def test_deploy_worker_readyz_succeeds_only_when_probe_passes(self) -> None:
        readiness = RecordingReadinessCheck()
        client = TestClient(
            build_deploy_worker_app(
                authenticator=AllowAllAuthenticator(),
                expected_service_account_email=DEPLOY_SERVICE_ACCOUNT,
                queue=FakeQueue(),
                worker=FakeDeployWorker(),
                clock=lambda: NOW,
                readiness_check=readiness,
            )
        )

        self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(client.get("/readyz").json(), {"status": "ready"})
        self.assertEqual(readiness.calls, 1)

    def test_deploy_worker_readyz_fails_closed_when_probe_raises(self) -> None:
        client = TestClient(
            build_deploy_worker_app(
                authenticator=AllowAllAuthenticator(),
                expected_service_account_email=DEPLOY_SERVICE_ACCOUNT,
                queue=FakeQueue(),
                worker=FakeDeployWorker(),
                clock=lambda: NOW,
                readiness_check=RecordingReadinessCheck(error=RuntimeError("down")),
            )
        )

        self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
        response = client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready"})

    def test_internal_deploy_requires_exact_task_headers_and_material_hash(
        self,
    ) -> None:
        task = queued_task()
        authenticator = AllowAllAuthenticator()
        queue = FakeQueue(task)
        worker = FakeDeployWorker(status="completed")
        client = TestClient(
            build_deploy_worker_app(
                authenticator=authenticator,
                expected_service_account_email=DEPLOY_SERVICE_ACCOUNT,
                queue=queue,
                worker=worker,
                clock=lambda: NOW,
                readiness_check=RecordingReadinessCheck(),
            )
        )

        ok = client.post(
            "/internal/deploy",
            content=deploy_body(task),
            headers=deploy_headers(task),
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json(), {"status": "completed"})
        self.assertEqual(worker.calls, [("op-1", NOW)])
        self.assertEqual(len(authenticator.calls), 1)
        self.assertEqual(queue.calls, [OperationId("op-1")])

        bad_body = json.dumps(
            {
                "material_hash": "0" * 64,
                "operation_id": "op-1",
                "schema": "mim.deploy.ref.v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        denied = client.post(
            "/internal/deploy",
            content=bad_body,
            headers=deploy_headers(task),
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(worker.calls, [("op-1", NOW)])

    def test_internal_deploy_rejects_header_drift_before_worker_execution(self) -> None:
        task = queued_task()
        worker = FakeDeployWorker()
        client = TestClient(
            build_deploy_worker_app(
                authenticator=AllowAllAuthenticator(),
                expected_service_account_email=DEPLOY_SERVICE_ACCOUNT,
                queue=FakeQueue(task),
                worker=worker,
                clock=lambda: NOW,
                readiness_check=RecordingReadinessCheck(),
            )
        )
        headers = deploy_headers(task)
        headers["X-CloudTasks-TaskName"] = "deploy-evil"

        response = client.post(
            "/internal/deploy",
            content=deploy_body(task),
            headers=headers,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(worker.calls, [])

    def test_internal_deploy_rejects_machine_identity_before_streaming_body(
        self,
    ) -> None:
        body_reader = AsyncMock(side_effect=AssertionError("body must not be read"))
        client = TestClient(
            build_deploy_worker_app(
                authenticator=RouteAwareAuthenticator(),
                expected_service_account_email=DEPLOY_SERVICE_ACCOUNT,
                queue=FakeQueue(queued_task()),
                worker=FakeDeployWorker(),
                clock=lambda: NOW,
                readiness_check=RecordingReadinessCheck(),
            )
        )

        with patch(
            "mim_control_plane.machine_api.read_bounded_http_body",
            body_reader,
        ):
            response = client.post(
                "/internal/deploy",
                content=b"{}",
                headers={"Authorization": "Bearer invalid"},
            )

        self.assertEqual(response.status_code, 403)
        body_reader.assert_not_awaited()

    def test_private_machine_routes_reject_oversized_bodies_before_auth(self) -> None:
        task = queued_task()
        deploy_authenticator = AllowAllAuthenticator()
        deploy_worker = FakeDeployWorker()
        deploy_client = TestClient(
            build_deploy_worker_app(
                authenticator=deploy_authenticator,
                expected_service_account_email=DEPLOY_SERVICE_ACCOUNT,
                queue=FakeQueue(task),
                worker=deploy_worker,
                clock=lambda: NOW,
                readiness_check=RecordingReadinessCheck(),
            )
        )
        schedule_authenticator = AllowAllAuthenticator()
        scheduler = FakeScheduleManagement()
        schedule_client = TestClient(
            build_schedule_gateway_app(
                authenticator=schedule_authenticator,
                expected_service_account_email=SCHEDULE_SERVICE_ACCOUNT,
                schedule_management=scheduler,
                scheduler_project_id=PROJECT_ID,
                scheduler_region=REGION,
                readiness_check=RecordingReadinessCheck(),
            )
        )

        deploy_response = deploy_client.post(
            "/internal/deploy",
            content=b"x" * 4097,
        )
        schedule_response = schedule_client.post(
            "/v1/schedules/execute",
            content=b"x" * 4097,
        )

        self.assertEqual(deploy_response.status_code, 413)
        self.assertEqual(schedule_response.status_code, 413)
        self.assertEqual(deploy_authenticator.calls, [])
        self.assertEqual(schedule_authenticator.calls, [])
        self.assertEqual(deploy_worker.calls, [])
        self.assertEqual(scheduler.calls, [])

    def test_internal_deploy_returns_retryable_failure_without_consuming_queue(
        self,
    ) -> None:
        task = queued_task()
        worker = RetryingDeployWorker(failure="source_fetch_failed")
        client = TestClient(
            build_deploy_worker_app(
                authenticator=AllowAllAuthenticator(),
                expected_service_account_email=DEPLOY_SERVICE_ACCOUNT,
                queue=FakeQueue(task),
                worker=worker,
                clock=lambda: NOW,
                readiness_check=RecordingReadinessCheck(),
            ),
            raise_server_exceptions=False,
        )

        response = client.post(
            "/internal/deploy",
            content=deploy_body(task),
            headers=deploy_headers(task),
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"failure": "source_fetch_failed", "status": "queued"},
        )
        self.assertEqual(worker.calls, [("op-1", NOW)])

    def test_internal_deploy_rejects_task_retry_beyond_queue_limit(self) -> None:
        task = queued_task()
        worker = FakeDeployWorker()
        client = TestClient(
            build_deploy_worker_app(
                authenticator=AllowAllAuthenticator(),
                expected_service_account_email=DEPLOY_SERVICE_ACCOUNT,
                queue=FakeQueue(task),
                worker=worker,
                clock=lambda: NOW,
                readiness_check=RecordingReadinessCheck(),
            )
        )
        headers = deploy_headers(task)
        headers["X-CloudTasks-TaskRetryCount"] = "4"

        response = client.post(
            "/internal/deploy",
            content=deploy_body(task),
            headers=headers,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(worker.calls, [])

    def test_internal_deploy_maps_missing_task_to_generic_403(self) -> None:
        task = queued_task()
        worker = FakeDeployWorker()
        client = TestClient(
            build_deploy_worker_app(
                authenticator=AllowAllAuthenticator(),
                expected_service_account_email=DEPLOY_SERVICE_ACCOUNT,
                queue=MissingTaskQueue(),
                worker=worker,
                clock=lambda: NOW,
                readiness_check=RecordingReadinessCheck(),
            )
        )

        response = client.post(
            "/internal/deploy",
            content=deploy_body(task),
            headers=deploy_headers(task),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "Machine request was denied."})
        self.assertEqual(worker.calls, [])

    def test_schedule_gateway_uses_trusted_scheduler_tick_and_exact_job_name(
        self,
    ) -> None:
        authenticator = AllowAllAuthenticator()
        scheduler = FakeScheduleManagement()
        readiness = RecordingReadinessCheck()
        client = TestClient(
            build_schedule_gateway_app(
                authenticator=authenticator,
                expected_service_account_email=SCHEDULE_SERVICE_ACCOUNT,
                schedule_management=scheduler,
                scheduler_project_id=PROJECT_ID,
                scheduler_region=REGION,
                readiness_check=readiness,
            )
        )

        self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(client.get("/readyz").json(), {"status": "ready"})
        self.assertEqual(readiness.calls, 1)

        response = client.post(
            "/v1/schedules/execute",
            content=json.dumps(
                {"schedule_id": "sch-1", "workload_id": "wrk-1"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers=schedule_headers("sch-1"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(
            scheduler.calls,
            [("sch-1", "wrk-1", datetime(2026, 8, 4, 6, 0, 0, tzinfo=UTC))],
        )
        self.assertEqual(len(authenticator.calls), 1)

    def test_schedule_gateway_readyz_fails_closed_when_probe_raises(self) -> None:
        client = TestClient(
            build_schedule_gateway_app(
                authenticator=AllowAllAuthenticator(),
                expected_service_account_email=SCHEDULE_SERVICE_ACCOUNT,
                schedule_management=FakeScheduleManagement(),
                scheduler_project_id=PROJECT_ID,
                scheduler_region=REGION,
                readiness_check=RecordingReadinessCheck(
                    error=RuntimeError("scheduler down")
                ),
            )
        )

        self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
        response = client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready"})

    def test_schedule_gateway_maps_schedule_denied_to_generic_403(self) -> None:
        class DenyingScheduleManagement(FakeScheduleManagement):
            def execute_schedule_tick(
                self,
                *,
                schedule_id: str,
                workload_id: str,
                tick_at: datetime,
            ) -> dict[str, object]:
                del schedule_id, workload_id, tick_at
                raise ScheduleDenied()

        client = TestClient(
            build_schedule_gateway_app(
                authenticator=AllowAllAuthenticator(),
                expected_service_account_email=SCHEDULE_SERVICE_ACCOUNT,
                schedule_management=DenyingScheduleManagement(),
                scheduler_project_id=PROJECT_ID,
                scheduler_region=REGION,
                readiness_check=RecordingReadinessCheck(),
            )
        )

        response = client.post(
            "/v1/schedules/execute",
            content=b'{"schedule_id":"sch-1","workload_id":"wrk-1"}',
            headers=schedule_headers("sch-1"),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "Machine request was denied."})

    def test_schedule_gateway_rejects_bad_scheduler_headers(self) -> None:
        client = TestClient(
            build_schedule_gateway_app(
                authenticator=AllowAllAuthenticator(),
                expected_service_account_email=SCHEDULE_SERVICE_ACCOUNT,
                schedule_management=FakeScheduleManagement(),
                scheduler_project_id=PROJECT_ID,
                scheduler_region=REGION,
                readiness_check=RecordingReadinessCheck(),
            )
        )
        headers = schedule_headers("sch-1")
        headers["X-CloudScheduler-JobName"] = (
            f"projects/{PROJECT_ID}/locations/{REGION}/jobs/mim-sch-wrong"
        )

        response = client.post(
            "/v1/schedules/execute",
            content=b'{"schedule_id":"sch-1","workload_id":"wrk-1"}',
            headers=headers,
        )

        self.assertEqual(response.status_code, 403)

    def test_schedule_gateway_mounts_route_specific_app_authorization(self) -> None:
        authenticator = RouteAwareAuthenticator()
        app_authorization = FakeAppAuthorization()
        client = TestClient(
            build_schedule_gateway_app(
                authenticator=authenticator,
                expected_service_account_email=SCHEDULE_SERVICE_ACCOUNT,
                schedule_management=FakeScheduleManagement(),
                scheduler_project_id=PROJECT_ID,
                scheduler_region=REGION,
                readiness_check=RecordingReadinessCheck(),
                expected_app_service_account_email=APP_GATEWAY_SERVICE_ACCOUNT,
                app_authorization=app_authorization,
            )
        )

        authorize_response = client.post(
            "/v1/apps/authorize",
            headers=app_headers(),
            content=json.dumps(
                {
                    "schema": "mim.app-authorization.v1",
                    "public_host": "north-star-bde131f06b2f.madup.app",
                    "method": "GET",
                    "request_target": "/",
                    "access_subject": "usr-1",
                    "access_email": "person@madup.com",
                    "edge_request_id": "req-1",
                    "edge_timestamp": int(NOW.timestamp()),
                    "edge_body_sha256": "a" * 64,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        schedule_response = client.post(
            "/v1/schedules/execute",
            content=b'{"schedule_id":"sch-1","workload_id":"wrk-1"}',
            headers=schedule_headers("sch-1"),
        )

        self.assertEqual(authorize_response.status_code, 200)
        self.assertEqual(schedule_response.status_code, 200)
        self.assertEqual(len(app_authorization.calls), 1)
        self.assertEqual(
            authenticator.calls,
            [
                ("Bearer machine-token", APP_GATEWAY_SERVICE_ACCOUNT),
                ("Bearer schedule-token", SCHEDULE_SERVICE_ACCOUNT),
            ],
        )
