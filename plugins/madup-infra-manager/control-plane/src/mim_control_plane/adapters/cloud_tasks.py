"""Cloud Tasks delivery backed by durable, private deploy material."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import ParseResult, urlparse

from google.api_core.exceptions import AlreadyExists, GoogleAPICallError
from google.cloud import tasks_v2

from mim_control_plane.config import REGION
from mim_control_plane.domain.models import OperationId
from mim_control_plane.ports.execution import (
    DeploymentQueuePort,
    DeploymentQueueReceipt,
    ExecutionPlaneError,
    QueuedDeployTask,
    TaskConflictError,
)

_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_RUN_HOST_PATTERN = re.compile(
    r"^mim-deploy-worker-[a-z0-9-]+(?:\.[a-z0-9-]+)*\.run\.app$"
)
_QUEUE_ID = "mim-private-workers"
_WORKER_PATH = "/internal/deploy"
_REFERENCE_SCHEMA = "mim.deploy.ref.v1"
_TASK_ID_HASH_PREFIX = b"mim:deploy-task:v1\x00"
_MAX_REFERENCE_BODY_BYTES = 4096


class CloudTasksDeliveryError(ExecutionPlaneError):
    """Raised when a durable task cannot be delivered exactly."""


class DurableDeployTaskStore(Protocol):
    def create_deploy_task_once(
        self,
        task: QueuedDeployTask,
    ) -> tuple[QueuedDeployTask, bool]: ...

    def get_deploy_task(self, operation_id: OperationId) -> QueuedDeployTask: ...


class CloudTasksClient(Protocol):
    def create_task(
        self,
        *,
        request: tasks_v2.CreateTaskRequest,
    ) -> tasks_v2.Task: ...

    def get_task(
        self,
        *,
        request: tasks_v2.GetTaskRequest,
    ) -> tasks_v2.Task: ...


@dataclass(frozen=True, slots=True)
class CloudTasksSettings:
    project_id: str
    location: str
    queue_id: str
    worker_url: str
    worker_audience: str
    oidc_service_account_email: str

    def __post_init__(self) -> None:
        if (
            type(self.project_id) is not str
            or _PROJECT_ID_PATTERN.fullmatch(self.project_id) is None
        ):
            raise ValueError("project_id is invalid.")
        if self.location != REGION:
            raise ValueError("location must match the fixed MIM region.")
        if self.queue_id != _QUEUE_ID:
            raise ValueError("queue_id must match the private MIM queue.")
        expected_identity = (
            f"mim-deploy-worker@{self.project_id}.iam.gserviceaccount.com"
        )
        if self.oidc_service_account_email != expected_identity:
            raise ValueError("OIDC identity must be the dedicated deploy worker.")

        worker = _validated_https_url(self.worker_url, allow_path=True)
        audience = _validated_https_url(self.worker_audience, allow_path=False)
        if (
            worker.hostname is None
            or _RUN_HOST_PATTERN.fullmatch(worker.hostname) is None
        ):
            raise ValueError("worker_url must target the MIM deploy worker.")
        if worker.path != _WORKER_PATH:
            raise ValueError("worker_url path is invalid.")
        if (worker.scheme, worker.netloc) != (audience.scheme, audience.netloc):
            raise ValueError("worker_audience must be the exact worker origin.")

    @property
    def parent(self) -> str:
        return (
            f"projects/{self.project_id}/locations/{self.location}/"
            f"queues/{self.queue_id}"
        )


@dataclass(frozen=True, slots=True)
class CloudTasksDeploymentQueue(DeploymentQueuePort):
    settings: CloudTasksSettings
    material_store: DurableDeployTaskStore
    client: CloudTasksClient

    def enqueue(self, task: QueuedDeployTask) -> DeploymentQueueReceipt:
        expected = self._build_task(task)
        durable_task, _ = self.material_store.create_deploy_task_once(task)
        if durable_task.material_hash != task.material_hash:
            raise TaskConflictError("durable deploy task material changed.")
        request = tasks_v2.CreateTaskRequest(
            parent=self.settings.parent,
            task=expected,
        )
        try:
            created = self.client.create_task(request=request)
        except AlreadyExists:
            existing = self._get_existing(expected.name)
            if not _same_delivery(existing, expected):
                raise TaskConflictError(
                    "queued Cloud Task configuration changed."
                ) from None
            return DeploymentQueueReceipt(task=durable_task, created=False)
        except GoogleAPICallError:
            raise CloudTasksDeliveryError(
                "queued Cloud Task delivery failed."
            ) from None

        if not isinstance(created, tasks_v2.Task) or created.name != expected.name:
            raise CloudTasksDeliveryError("queued Cloud Task response was invalid.")
        return DeploymentQueueReceipt(task=durable_task, created=True)

    def get(self, operation_id: OperationId) -> QueuedDeployTask:
        return self.material_store.get_deploy_task(operation_id)

    def _build_task(self, task: QueuedDeployTask) -> tasks_v2.Task:
        task_id = (
            "deploy-"
            + hashlib.sha256(
                _TASK_ID_HASH_PREFIX + task.idempotency_key.encode("utf-8")
            ).hexdigest()
        )
        name = f"{self.settings.parent}/tasks/{task_id}"
        body = json.dumps(
            {
                "material_hash": task.material_hash,
                "operation_id": str(task.operation_id),
                "schema": _REFERENCE_SCHEMA,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > _MAX_REFERENCE_BODY_BYTES:
            raise ValueError("queued Cloud Task reference is too large.")
        return tasks_v2.Task(
            name=name,
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=self.settings.worker_url,
                headers={"Content-Type": "application/json"},
                body=body,
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self.settings.oidc_service_account_email,
                    audience=self.settings.worker_audience,
                ),
            ),
        )

    def _get_existing(self, name: str) -> tasks_v2.Task:
        try:
            return self.client.get_task(
                request=tasks_v2.GetTaskRequest(
                    name=name,
                    response_view=tasks_v2.Task.View.FULL,
                )
            )
        except GoogleAPICallError:
            raise CloudTasksDeliveryError(
                "existing queued Cloud Task could not be verified."
            ) from None


def _validated_https_url(value: object, *, allow_path: bool) -> ParseResult:
    if type(value) is not str or not value:
        raise ValueError("worker URL is invalid.")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("worker URL is invalid.")
    if not allow_path and parsed.path:
        raise ValueError("worker audience must not contain a path.")
    return parsed


def _same_delivery(actual: tasks_v2.Task, expected: tasks_v2.Task) -> bool:
    if not isinstance(actual, tasks_v2.Task):
        return False
    actual_request = actual.http_request
    expected_request = expected.http_request
    return (
        actual.name == expected.name
        and actual_request.http_method == expected_request.http_method
        and actual_request.url == expected_request.url
        and dict(actual_request.headers) == dict(expected_request.headers)
        and actual_request.body == expected_request.body
        and actual_request.oidc_token.service_account_email
        == expected_request.oidc_token.service_account_email
        and actual_request.oidc_token.audience == expected_request.oidc_token.audience
        and not actual_request.oauth_token.service_account_email
        and not actual_request.oauth_token.scope
    )
