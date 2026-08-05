"""Dedicated private HTTP apps for deploy workers and schedule gateways."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from mim_control_plane.app_gateway_api import (
    AppAuthorizationPort,
    mount_app_authorization_route,
)
from mim_control_plane.domain.models import OperationId
from mim_control_plane.http_body import (
    preflight_bounded_http_body,
    read_bounded_http_body,
)
from mim_control_plane.ports.execution import (
    QueuedDeployTask,
    RetryableExecutionPlaneError,
    TaskNotFoundError,
)
from mim_control_plane.security.google_machine_identity import (
    GoogleOidcMachineAuthenticator,
    MachineRequestDenied,
)
from mim_control_plane.services.schedule_management import ScheduleDenied

_MAX_REQUEST_BYTES = 4096
_LOWER_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_NUMERIC_PATTERN = re.compile(r"^[0-9]{1,12}$")
_QUEUE_NAME = "mim-private-workers"
_MAX_TASK_RETRY_COUNT = 3
_DEPLOY_SCHEMA = "mim.deploy.ref.v1"
_TASK_ID_HASH_PREFIX = b"mim:deploy-task:v1\x00"
_SCHEDULER_TRUE = "true"
_REGION_PATTERN = re.compile(r"^[a-z]+(?:-[a-z0-9]+)+[0-9]$")
_PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


class DeployWorker(Protocol):
    def run(self, *, operation_id: str, now: datetime) -> object: ...


class DeployTaskLookup(Protocol):
    def get(self, operation_id: OperationId) -> QueuedDeployTask: ...


class ScheduleManagementPort(Protocol):
    def execute_schedule_tick(
        self,
        *,
        schedule_id: str,
        workload_id: str,
        tick_at: datetime,
    ) -> Mapping[str, object]: ...


class ReadinessCheck(Protocol):
    def __call__(self) -> None: ...


def build_deploy_worker_app(
    *,
    authenticator: GoogleOidcMachineAuthenticator | object,
    expected_service_account_email: str,
    queue: DeployTaskLookup,
    worker: DeployWorker,
    clock: Callable[[], datetime],
    readiness_check: ReadinessCheck,
) -> FastAPI:
    _require_text(expected_service_account_email)
    app = _build_machine_app(readiness_check=readiness_check)

    @app.post("/internal/deploy")
    async def internal_deploy(request: Request) -> JSONResponse:
        preflight_bounded_http_body(
            request,
            max_bytes=_MAX_REQUEST_BYTES,
        )
        try:
            header_pairs = _header_pairs(request)
            _authenticate_machine_request(
                authenticator=authenticator,
                headers=header_pairs,
                expected_service_account_email=expected_service_account_email,
            )
            body = await read_bounded_http_body(
                request,
                max_bytes=_MAX_REQUEST_BYTES,
            )
            payload = _parse_exact_json(
                body,
                expected_keys=frozenset({"material_hash", "operation_id", "schema"}),
            )
            if payload["schema"] != _DEPLOY_SCHEMA:
                raise MachineRequestDenied("Machine request was denied.")
            operation_id = _require_identifier(payload["operation_id"])
            material_hash = _require_material_hash(payload["material_hash"])
            task = queue.get(OperationId(operation_id))
            _validate_cloud_tasks_headers(
                headers=header_pairs,
                idempotency_key=task.idempotency_key,
            )
            if not hmac.compare_digest(material_hash, task.material_hash):
                raise MachineRequestDenied("Machine request was denied.")
            result = worker.run(operation_id=operation_id, now=clock())
            status = _require_text(getattr(result, "status", None))
        except RetryableExecutionPlaneError as exc:
            return JSONResponse(
                {"failure": exc.sanitized_failure, "status": "queued"},
                status_code=503,
            )
        except MachineRequestDenied:
            raise HTTPException(
                status_code=403,
                detail="Machine request was denied.",
            ) from None
        except TaskNotFoundError:
            raise HTTPException(
                status_code=403,
                detail="Machine request was denied.",
            ) from None
        return JSONResponse({"status": status})

    return app


def build_schedule_gateway_app(
    *,
    authenticator: GoogleOidcMachineAuthenticator | object,
    expected_service_account_email: str,
    schedule_management: ScheduleManagementPort,
    scheduler_project_id: str,
    scheduler_region: str,
    readiness_check: ReadinessCheck,
    expected_app_service_account_email: str | None = None,
    app_authorization: AppAuthorizationPort | None = None,
) -> FastAPI:
    project_id = _require_project_id(scheduler_project_id)
    region = _require_region(scheduler_region)
    _require_text(expected_service_account_email)
    app = _build_machine_app(readiness_check=readiness_check)

    @app.post("/v1/schedules/execute")
    async def execute_schedule(request: Request) -> JSONResponse:
        preflight_bounded_http_body(
            request,
            max_bytes=_MAX_REQUEST_BYTES,
        )
        try:
            header_pairs = _header_pairs(request)
            _authenticate_machine_request(
                authenticator=authenticator,
                headers=header_pairs,
                expected_service_account_email=expected_service_account_email,
            )
            body = await read_bounded_http_body(
                request,
                max_bytes=_MAX_REQUEST_BYTES,
            )
            payload = _parse_exact_json(
                body,
                expected_keys=frozenset({"schedule_id", "workload_id"}),
            )
            schedule_id = _require_identifier(payload["schedule_id"])
            workload_id = _require_identifier(payload["workload_id"])
            tick_at = _validate_scheduler_headers(
                headers=header_pairs,
                project_id=project_id,
                region=region,
                schedule_id=schedule_id,
            )
            schedule_management.execute_schedule_tick(
                schedule_id=schedule_id,
                workload_id=workload_id,
                tick_at=tick_at,
            )
        except MachineRequestDenied:
            raise HTTPException(
                status_code=403,
                detail="Machine request was denied.",
            ) from None
        except ScheduleDenied:
            raise HTTPException(
                status_code=403,
                detail="Machine request was denied.",
            ) from None
        return JSONResponse({"status": "ok"})

    if app_authorization is not None:
        if expected_app_service_account_email is None:
            raise ValueError(
                "expected_app_service_account_email is required when app "
                "authorization is mounted."
            )
        mount_app_authorization_route(
            app=app,
            authenticator=authenticator,
            expected_service_account_email=expected_app_service_account_email,
            authorization_service=app_authorization,
        )

    return app


def _build_machine_app(*, readiness_check: ReadinessCheck) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            readiness_check()
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return app


def _authenticate_machine_request(
    *,
    authenticator: object,
    headers: tuple[tuple[str, str], ...],
    expected_service_account_email: str,
) -> None:
    authenticate = getattr(authenticator, "authenticate", None)
    if not callable(authenticate):
        raise TypeError("machine authenticator is invalid")
    authenticate(
        headers,
        expected_service_account_email=expected_service_account_email,
    )


def _header_pairs(request: Request) -> tuple[tuple[str, str], ...]:
    raw_headers = request.scope.get("headers")
    if not isinstance(raw_headers, list):
        raise MachineRequestDenied("Machine request was denied.")
    pairs: list[tuple[str, str]] = []
    for item in raw_headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            raise MachineRequestDenied("Machine request was denied.")
        pairs.append(
            (
                item[0].decode("latin-1"),
                item[1].decode("latin-1"),
            )
        )
    return tuple(pairs)


def _parse_exact_json(
    body: bytes,
    *,
    expected_keys: frozenset[str],
) -> dict[str, str]:
    if type(body) is not bytes or len(body) > _MAX_REQUEST_BYTES:
        raise MachineRequestDenied("Machine request was denied.")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MachineRequestDenied("Machine request was denied.") from None
    if not isinstance(payload, dict) or frozenset(payload) != expected_keys:
        raise MachineRequestDenied("Machine request was denied.")
    result: dict[str, str] = {}
    for key in expected_keys:
        result[key] = _require_text(payload.get(key))
    return result


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MachineRequestDenied("Machine request was denied.")
        result[key] = value
    return result


def _validate_cloud_tasks_headers(
    *,
    headers: tuple[tuple[str, str], ...],
    idempotency_key: str,
) -> None:
    indexed = _single_headers(
        headers,
        names=(
            "x-cloudtasks-queuename",
            "x-cloudtasks-taskname",
            "x-cloudtasks-taskretrycount",
        ),
    )
    if indexed["x-cloudtasks-queuename"] != _QUEUE_NAME:
        raise MachineRequestDenied("Machine request was denied.")
    expected_task_name = "deploy-" + hashlib.sha256(
        _TASK_ID_HASH_PREFIX + idempotency_key.encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(indexed["x-cloudtasks-taskname"], expected_task_name):
        raise MachineRequestDenied("Machine request was denied.")
    retry_count = indexed["x-cloudtasks-taskretrycount"]
    if _NUMERIC_PATTERN.fullmatch(retry_count) is None:
        raise MachineRequestDenied("Machine request was denied.")
    if int(retry_count) > _MAX_TASK_RETRY_COUNT:
        raise MachineRequestDenied("Machine request was denied.")


def _validate_scheduler_headers(
    *,
    headers: tuple[tuple[str, str], ...],
    project_id: str,
    region: str,
    schedule_id: str,
) -> datetime:
    indexed = _single_headers(
        headers,
        names=(
            "x-cloudscheduler",
            "x-cloudscheduler-jobname",
            "x-cloudscheduler-scheduletime",
        ),
    )
    if indexed["x-cloudscheduler"].casefold() != _SCHEDULER_TRUE:
        raise MachineRequestDenied("Machine request was denied.")
    expected_job_name = (
        f"projects/{project_id}/locations/{region}/jobs/"
        f"mim-sch-{hashlib.sha256(schedule_id.encode('utf-8')).hexdigest()[:20]}"
    )
    if not hmac.compare_digest(indexed["x-cloudscheduler-jobname"], expected_job_name):
        raise MachineRequestDenied("Machine request was denied.")
    return _parse_scheduler_time(indexed["x-cloudscheduler-scheduletime"])


def _single_headers(
    headers: tuple[tuple[str, str], ...],
    *,
    names: tuple[str, ...],
) -> dict[str, str]:
    required = set(names)
    result: dict[str, str] = {}
    for raw_name, raw_value in headers:
        name = _require_text(raw_name).casefold()
        if name not in required:
            continue
        if name in result:
            raise MachineRequestDenied("Machine request was denied.")
        result[name] = _require_text(raw_value)
    if set(result) != required:
        raise MachineRequestDenied("Machine request was denied.")
    return result


def _parse_scheduler_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MachineRequestDenied("Machine request was denied.") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise MachineRequestDenied("Machine request was denied.")
    if parsed.microsecond != 0:
        raise MachineRequestDenied("Machine request was denied.")
    return parsed.astimezone(UTC)


def _require_material_hash(value: object) -> str:
    candidate = _require_text(value)
    if _LOWER_SHA256_PATTERN.fullmatch(candidate) is None:
        raise MachineRequestDenied("Machine request was denied.")
    return candidate


def _require_identifier(value: object) -> str:
    candidate = _require_text(value)
    if _IDENTIFIER_PATTERN.fullmatch(candidate) is None:
        raise MachineRequestDenied("Machine request was denied.")
    return candidate


def _require_project_id(value: object) -> str:
    candidate = _require_text(value)
    if _PROJECT_PATTERN.fullmatch(candidate) is None:
        raise MachineRequestDenied("Machine request was denied.")
    return candidate


def _require_region(value: object) -> str:
    candidate = _require_text(value)
    if _REGION_PATTERN.fullmatch(candidate) is None:
        raise MachineRequestDenied("Machine request was denied.")
    return candidate


def _require_text(value: object) -> str:
    if type(value) is not str:
        raise MachineRequestDenied("Machine request was denied.")
    candidate = value.strip()
    if not candidate:
        raise MachineRequestDenied("Machine request was denied.")
    return candidate
