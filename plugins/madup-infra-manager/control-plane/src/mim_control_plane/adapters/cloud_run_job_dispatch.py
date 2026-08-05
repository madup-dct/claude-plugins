"""Bounded Cloud Run Job dispatcher for schedule ticks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal, Protocol, TypeAlias, cast

from mim_control_plane.config import REGION as CONFIG_REGION
from mim_control_plane.ports.schedule import (
    ScheduledRunReceipt,
    ScheduledRunRequest,
    ScheduleExecutionError,
)
from mim_control_plane.services.runtime_identity import runtime_identity_spec

_CENTRAL_PROJECT_ID = "mim-prod-123456"
_FAILED = "scheduled run was denied."
_MANAGED_BY = "mim-control-plane"
_IMAGE_PREFIX = "mim/workloads"
_JOB_COMMAND = ("python",)
_JOB_ARGS = ("main.py",)

_google_run_v2: Any | None
try:  # pragma: no cover - exercised when official library is present
    from google.cloud import run_v2 as _google_run_v2_module
except Exception:  # pragma: no cover - local fallback for tests
    _google_run_v2 = None
else:  # pragma: no cover - exercised when official library is present
    _google_run_v2 = _google_run_v2_module


class RunJobOperation(Protocol):
    def result(self, timeout: float | None = None) -> object: ...


class JobsClient(Protocol):
    def get_job(self, request: object) -> object: ...
    def run_job(self, request: object) -> RunJobOperation: ...


class ExecutionsClient(Protocol):
    def list_executions(self, request: object) -> object: ...


class DispatchLedger(Protocol):
    def get(self, *, schedule_id: str, tick_at: datetime) -> object | None: ...
    def claim(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
    ) -> object: ...
    def mark_succeeded(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
        run_reference: str,
    ) -> object: ...
    def mark_ambiguous(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _EnvVar:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class _ContainerOverride:
    env: tuple[_EnvVar, ...]
    name: str = ""


@dataclass(frozen=True, slots=True)
class _RunJobOverrides:
    container_overrides: tuple[_ContainerOverride, ...]
    task_count: int


@dataclass(frozen=True, slots=True)
class _RunJobRequest:
    name: str
    overrides: _RunJobOverrides
    validate_only: bool = False


@dataclass(frozen=True, slots=True)
class _GetJobRequest:
    name: str


@dataclass(frozen=True, slots=True)
class _ListExecutionsRequest:
    parent: str


@dataclass(frozen=True, slots=True)
class CloudRunJobDispatcher:
    jobs_client: JobsClient
    executions_client: ExecutionsClient
    ledger: DispatchLedger
    project_id: str
    region: str
    operation_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or self.project_id != _CENTRAL_PROJECT_ID:
            raise ValueError("Cloud Run schedule project is invalid.")
        if type(self.region) is not str or self.region != CONFIG_REGION:
            raise ValueError("Cloud Run schedule region is invalid.")
        if not all(
            callable(getattr(self.jobs_client, method, None))
            for method in ("get_job", "run_job")
        ):
            raise ValueError("Cloud Run jobs client is invalid.")
        if not callable(getattr(self.executions_client, "list_executions", None)):
            raise ValueError("Cloud Run executions client is invalid.")
        if not all(
            callable(getattr(self.ledger, method, None))
            for method in ("get", "claim", "mark_succeeded", "mark_ambiguous")
        ):
            raise ValueError("Cloud Run dispatch ledger is invalid.")
        if (
            not isinstance(self.operation_timeout_seconds, float)
            or not 1.0 <= self.operation_timeout_seconds <= 300.0
        ):
            raise ValueError("Cloud Run job timeout is invalid.")

    @staticmethod
    def stable_tick_token(request: ScheduledRunRequest) -> str:
        if type(request) is not ScheduledRunRequest:
            raise ValueError("scheduled run request is invalid.")
        digest = hashlib.sha256()
        digest.update(str(request.schedule_id).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(request.workload_id).encode("utf-8"))
        digest.update(b"\0")
        digest.update(request.tick_at.isoformat().encode("ascii"))
        return digest.hexdigest()

    def dispatch(self, request: ScheduledRunRequest) -> ScheduledRunReceipt:
        try:
            if type(request) is not ScheduledRunRequest:
                raise ValueError
            expected_job_name = _job_name(
                project_id=self.project_id,
                region=self.region,
                workload_id=str(request.workload_id),
            )
            expected_service_account = runtime_identity_spec(
                project_id=self.project_id,
                workload_id=str(request.workload_id),
            ).email
            boundary = _JobBoundary(
                job_name=expected_job_name,
                schedule_id=str(request.schedule_id),
                tick_at=request.tick_at,
                workload_id=str(request.workload_id),
                service_account=expected_service_account,
                tick_token=self.stable_tick_token(request),
                expected_env=self._stable_env(request),
            )
            job = self.jobs_client.get_job(_build_get_job_request(expected_job_name))
            _require_safe_job_boundary(
                job,
                project_id=self.project_id,
                region=self.region,
                boundary=boundary,
            )

            existing = self.ledger.get(
                schedule_id=str(request.schedule_id),
                tick_at=request.tick_at,
            )
            if existing is not None:
                _require_matching_stable_token(existing, boundary=boundary)
                return self._resolve_existing(existing=existing, boundary=boundary)

            claimed = self.ledger.claim(
                schedule_id=str(request.schedule_id),
                tick_at=request.tick_at,
                stable_token=boundary.tick_token,
            )
            _require_matching_stable_token(claimed, boundary=boundary)
            if _record_state(claimed) != "claimed":
                return self._resolve_existing(existing=claimed, boundary=boundary)

            try:
                execution = self.jobs_client.run_job(
                    _build_run_job_request(expected_job_name, request)
                ).result(timeout=self.operation_timeout_seconds)
            except Exception:
                ambiguous = self.ledger.mark_ambiguous(
                    schedule_id=str(request.schedule_id),
                    tick_at=request.tick_at,
                    stable_token=boundary.tick_token,
                )
                _require_matching_stable_token(ambiguous, boundary=boundary)
                return self._reconcile(boundary=boundary)

            run_reference = _exact_execution_reference(
                execution,
                boundary=boundary,
            )
            succeeded = self.ledger.mark_succeeded(
                schedule_id=str(request.schedule_id),
                tick_at=request.tick_at,
                stable_token=boundary.tick_token,
                run_reference=run_reference,
            )
            _require_matching_stable_token(succeeded, boundary=boundary)
            return ScheduledRunReceipt(run_reference=run_reference, created=True)
        except Exception:
            raise ScheduleExecutionError(_FAILED) from None

    def _resolve_existing(
        self,
        *,
        existing: object,
        boundary: "_JobBoundary",
    ) -> ScheduledRunReceipt:
        state = _record_state(existing)
        if state == "succeeded":
            run_reference = _record_run_reference(existing)
            if run_reference is None:
                raise ValueError
            return ScheduledRunReceipt(run_reference=run_reference, created=False)
        if state in {"claimed", "ambiguous"}:
            return self._reconcile(boundary=boundary)
        raise ValueError

    def _reconcile(
        self,
        *,
        boundary: "_JobBoundary",
    ) -> ScheduledRunReceipt:
        matches: list[str] = []
        raw = self.executions_client.list_executions(
            _build_list_executions_request(boundary.job_name)
        )
        for execution in _as_execution_iterable(raw):
            try:
                matches.append(_exact_execution_reference(execution, boundary=boundary))
            except Exception:
                continue
        if len(matches) != 1:
            raise ScheduleExecutionError(_FAILED)
        run_reference = matches[0]
        self.ledger.mark_succeeded(
            schedule_id=boundary.schedule_id,
            tick_at=boundary.tick_at,
            stable_token=boundary.tick_token,
            run_reference=run_reference,
        )
        return ScheduledRunReceipt(run_reference=run_reference, created=False)

    def _stable_env(
        self,
        request: ScheduledRunRequest,
    ) -> dict[str, str]:
        return {
            "MIM_SCHEDULE_ID": str(request.schedule_id),
            "MIM_WORKLOAD_ID": str(request.workload_id),
            "MIM_TICK_AT": request.tick_at.isoformat(),
            "MIM_TICK_TOKEN": self.stable_tick_token(request),
        }


@dataclass(frozen=True, slots=True)
class _JobBoundary:
    job_name: str
    schedule_id: str
    tick_at: datetime
    workload_id: str
    service_account: str
    tick_token: str
    expected_env: dict[str, str]


def _build_get_job_request(name: str) -> object:
    if _google_run_v2 is not None:  # pragma: no branch
        return _google_run_v2.GetJobRequest(name=name)
    return _GetJobRequest(name=name)


def _build_list_executions_request(parent: str) -> object:
    if _google_run_v2 is not None:  # pragma: no branch
        return _google_run_v2.ListExecutionsRequest(parent=parent)
    return _ListExecutionsRequest(parent=parent)


def _build_run_job_request(name: str, request: ScheduledRunRequest) -> object:
    env_pairs = (
        ("MIM_SCHEDULE_ID", str(request.schedule_id)),
        ("MIM_WORKLOAD_ID", str(request.workload_id)),
        ("MIM_LEASE_TOKEN", request.lease_token),
        ("MIM_TICK_AT", request.tick_at.isoformat()),
        ("MIM_TICK_TOKEN", CloudRunJobDispatcher.stable_tick_token(request)),
    )
    if _google_run_v2 is not None:  # pragma: no branch
        return _google_run_v2.RunJobRequest(
            name=name,
            validate_only=False,
            overrides=_google_run_v2.RunJobRequest.Overrides(
                task_count=1,
                container_overrides=(
                    _google_run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=tuple(
                            _google_run_v2.EnvVar(name=env_name, value=env_value)
                            for env_name, env_value in env_pairs
                        )
                    ),
                ),
            ),
        )
    return _RunJobRequest(
        name=name,
        validate_only=False,
        overrides=_RunJobOverrides(
            task_count=1,
            container_overrides=(
                _ContainerOverride(
                    env=tuple(
                        _EnvVar(name=env_name, value=env_value)
                        for env_name, env_value in env_pairs
                    )
                ),
            ),
        ),
    )


def _job_name(*, project_id: str, region: str, workload_id: str) -> str:
    suffix = hashlib.sha256(workload_id.encode("utf-8")).hexdigest()[:12]
    return f"projects/{project_id}/locations/{region}/jobs/mim-job-{suffix}"


def _workload_hash(workload_id: str) -> str:
    return hashlib.sha256(workload_id.encode("utf-8")).hexdigest()[:12]


RecordState: TypeAlias = Literal["claimed", "ambiguous", "succeeded"]


def _record_state(record: object) -> RecordState:
    state = _record_string_value(record, "state")
    if state not in ("claimed", "ambiguous", "succeeded"):
        raise ValueError
    return cast(RecordState, state)


def _record_run_reference(record: object) -> str | None:
    value = _record_value(record, "run_reference")
    if value is None:
        return None
    if type(value) is not str or not value:
        raise ValueError
    return value


def _record_stable_token(record: object) -> str:
    value = _record_string_value(record, "stable_token")
    if (
        len(value) != 64
        or value.lower() != value
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError
    return value


def _record_value(record: object, key: str) -> object:
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _record_string_value(record: object, key: str) -> str:
    value = _record_value(record, key)
    if type(value) is not str:
        raise ValueError
    return value


def _require_matching_stable_token(record: object, *, boundary: "_JobBoundary") -> None:
    if _record_stable_token(record) != boundary.tick_token:
        raise ValueError


def _extract_name(value: object) -> str:
    name = getattr(value, "name", None)
    if type(name) is not str or not name:
        raise ValueError
    return name


def _extract_job_reference(value: object) -> str:
    job = getattr(value, "job", None)
    if type(job) is not str or not job:
        raise ValueError
    return job


def _extract_labels(value: object) -> dict[str, str]:
    labels = getattr(value, "labels", None)
    if not isinstance(labels, dict):
        raise ValueError
    if any(type(key) is not str or type(val) is not str for key, val in labels.items()):
        raise ValueError
    return dict(labels)


def _extract_task_template(value: object) -> object:
    template = getattr(value, "template", None)
    nested = getattr(template, "template", None)
    if nested is not None:
        return nested
    if template is not None:
        return template
    raise ValueError


def _extract_service_account(task: object) -> str:
    service_account = getattr(task, "service_account", None)
    if type(service_account) is not str or not service_account:
        raise ValueError
    return service_account


def _extract_timeout_seconds(task: object) -> int:
    timeout = getattr(task, "timeout", None)
    seconds = getattr(timeout, "seconds", None)
    if type(seconds) is not int or isinstance(seconds, bool):
        raise ValueError
    return seconds


def _extract_containers(task: object) -> tuple[object, ...]:
    containers = getattr(task, "containers", None)
    if not isinstance(containers, tuple) or not containers:
        raise ValueError
    return containers


def _extract_task_count(value: object) -> int:
    direct_task_count = getattr(value, "task_count", None)
    if type(direct_task_count) is int and not isinstance(direct_task_count, bool):
        return direct_task_count
    template = getattr(value, "template", None)
    task_count = getattr(template, "task_count", None)
    if type(task_count) is not int or isinstance(task_count, bool):
        raise ValueError
    return task_count


def _extract_parallelism(value: object) -> int:
    direct_parallelism = getattr(value, "parallelism", None)
    if type(direct_parallelism) is int and not isinstance(
        direct_parallelism,
        bool,
    ):
        return direct_parallelism
    template = getattr(value, "template", None)
    parallelism = getattr(template, "parallelism", None)
    if type(parallelism) is not int or isinstance(parallelism, bool):
        raise ValueError
    return parallelism


def _extract_max_retries(task: object) -> int:
    retries = getattr(task, "max_retries", None)
    if type(retries) is not int or isinstance(retries, bool):
        raise ValueError
    return retries


def _extract_volumes(task: object) -> tuple[object, ...]:
    volumes = getattr(task, "volumes", None)
    if not isinstance(volumes, tuple):
        raise ValueError
    return volumes


def _extract_vpc_connector(task: object) -> str:
    vpc_access = getattr(task, "vpc_access", None)
    connector = getattr(vpc_access, "connector", None)
    if type(connector) is not str:
        raise ValueError
    return connector


def _extract_container_env(container: object) -> dict[str, str]:
    env = getattr(container, "env", None)
    if not isinstance(env, tuple):
        raise ValueError
    pairs: dict[str, str] = {}
    for item in env:
        name = getattr(item, "name", None)
        value = getattr(item, "value", None)
        if type(name) is not str or type(value) is not str:
            raise ValueError
        pairs[name] = value
    return pairs


def _require_safe_job_boundary(
    job: object,
    *,
    project_id: str,
    region: str,
    boundary: _JobBoundary,
) -> None:
    if _extract_name(job) != boundary.job_name:
        raise ValueError
    labels = _extract_labels(job)
    if labels.get("managed-by") != _MANAGED_BY:
        raise ValueError
    if labels.get("workload-hash") != _workload_hash(boundary.workload_id):
        raise ValueError
    if _extract_task_count(job) != 1 or _extract_parallelism(job) != 1:
        raise ValueError
    task = _extract_task_template(job)
    if _extract_volumes(task) or _extract_vpc_connector(task):
        raise ValueError
    if _extract_service_account(task) != boundary.service_account:
        raise ValueError
    if _extract_max_retries(task) != 1 or _extract_timeout_seconds(task) != 300:
        raise ValueError
    container = _require_one_container(task)
    if not _container_matches_boundary(
        container,
        project_id=project_id,
        region=region,
        require_empty_env=True,
    ):
        raise ValueError


def _container_matches_boundary(
    container: object,
    *,
    project_id: str,
    region: str,
    require_empty_env: bool,
) -> bool:
    image = getattr(container, "image", None)
    command = getattr(container, "command", None)
    args = getattr(container, "args", None)
    resources = getattr(container, "resources", None)
    limits = getattr(resources, "limits", None)
    cpu_idle = getattr(resources, "cpu_idle", None)
    startup_cpu_boost = getattr(resources, "startup_cpu_boost", None)
    ports = getattr(container, "ports", None)
    volume_mounts = getattr(container, "volume_mounts", None)
    env = getattr(container, "env", None)
    return (
        type(image) is str
        and _is_mim_image_uri(value=image, project_id=project_id, region=region)
        and command == _JOB_COMMAND
        and args == _JOB_ARGS
        and isinstance(limits, dict)
        and dict(limits) == {"cpu": "1", "memory": "512Mi"}
        and cpu_idle is False
        and startup_cpu_boost is False
        and ports == ()
        and volume_mounts == ()
        and isinstance(env, tuple)
        and ((not require_empty_env) or env == ())
    )


def _require_one_container(task: object) -> object:
    containers = _extract_containers(task)
    if len(containers) != 1:
        raise ValueError
    return containers[0]


def _exact_execution_reference(execution: object, *, boundary: _JobBoundary) -> str:
    name = _extract_name(execution)
    if not name.startswith(f"{boundary.job_name}/executions/"):
        raise ValueError
    if _extract_job_reference(execution) != boundary.job_name:
        raise ValueError
    labels = _extract_labels(execution)
    if labels.get("managed-by") != _MANAGED_BY:
        raise ValueError
    if labels.get("workload-hash") != _workload_hash(boundary.workload_id):
        raise ValueError
    if _extract_task_count(execution) != 1 or _extract_parallelism(execution) != 1:
        raise ValueError
    task = _extract_task_template(execution)
    if _extract_volumes(task) or _extract_vpc_connector(task):
        raise ValueError
    if _extract_service_account(task) != boundary.service_account:
        raise ValueError
    if _extract_max_retries(task) != 1 or _extract_timeout_seconds(task) != 300:
        raise ValueError
    container = _require_one_container(task)
    if not _container_matches_boundary(
        container,
        project_id=_CENTRAL_PROJECT_ID,
        region=CONFIG_REGION,
        require_empty_env=False,
    ):
        raise ValueError
    env = _extract_container_env(container)
    if any(
        env.get(key) != value
        for key, value in boundary.expected_env.items()
    ):
        raise ValueError
    return name


def _extract_digest(value: str) -> str:
    marker = "@sha256:"
    digest = value.rsplit(marker, 1)[1] if marker in value else value
    if (
        len(digest) != 64
        or digest.lower() != digest
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError
    return digest


def _is_mim_image_uri(*, project_id: str, region: str, value: str) -> bool:
    prefix = f"{region}-docker.pkg.dev/{project_id}/{_IMAGE_PREFIX}@sha256:"
    if type(value) is not str or not value.startswith(prefix):
        return False
    try:
        return value == f"{prefix}{_extract_digest(value)}"
    except Exception:
        return False


def _as_execution_iterable(value: object) -> Iterable[object]:
    if isinstance(value, tuple):
        return value
    if not hasattr(value, "__iter__"):
        raise ValueError
    return cast(Iterable[object], value)
