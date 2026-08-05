"""Bounded Cloud Scheduler adapter for MIM schedule targets."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from google.api_core.exceptions import NotFound
from google.cloud import scheduler_v1
from google.protobuf import duration_pb2, field_mask_pb2  # type: ignore[import-untyped]

from mim_control_plane.config import REGION
from mim_control_plane.domain.models import Schedule
from mim_control_plane.domain.states import ScheduleState
from mim_control_plane.services.schedules import normalize_schedule_policy

_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_SERVICE_ACCOUNT_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@"
    r"([a-z][a-z0-9-]{4,28}[a-z0-9])\.iam\.gserviceaccount\.com$"
)
_GATEWAY_PATH = "/v1/schedules/execute"
_ATTEMPT_DEADLINE_SECONDS = 30
_UPDATE_PATHS = (
    "attempt_deadline",
    "description",
    "http_target",
    "retry_config",
    "schedule",
    "time_zone",
)
_FAILED = "scheduler operation was denied."


class CloudSchedulerAdapterError(RuntimeError):
    """Raised when a Scheduler request falls outside the reviewed boundary."""


class SchedulerAuthMode(StrEnum):
    OIDC = "oidc"
    OAUTH = "oauth"


@dataclass(frozen=True, slots=True)
class SchedulerJobMetadata:
    name: str
    created: bool
    auth_mode: SchedulerAuthMode


class CloudSchedulerAdapter:
    """Create or repair deterministic hourly Scheduler jobs in one location."""

    def __init__(
        self,
        *,
        client: Any,
        project_id: str,
        project_number: str,
        region: str,
        scheduler_service_account: str,
    ) -> None:
        self._project_id = _require_project_id(project_id)
        self._project_number = _require_numeric_project_number(project_number)
        if type(region) is not str or region != REGION:
            raise ValueError("region is invalid.")
        self._region = region
        self._scheduler_service_account = _require_service_account(
            scheduler_service_account,
            project_id=self._project_id,
        )
        if self._scheduler_service_account != (
            f"mim-schedule-gateway@{self._project_id}.iam.gserviceaccount.com"
        ):
            raise ValueError("service account is invalid.")
        self._gateway_audience = (
            "https://mim-schedule-gateway-"
            f"{self._project_number}.{self._region}.run.app"
        )
        self._gateway_uri = f"{self._gateway_audience}{_GATEWAY_PATH}"
        self._client = client

    def __repr__(self) -> str:
        return (
            "CloudSchedulerAdapter("
            f"project_id={self._project_id!r}, region={self._region!r})"
        )

    def ensure_gateway_schedule(
        self,
        schedule: Schedule,
    ) -> SchedulerJobMetadata:
        self._require_schedule(schedule)
        body = json.dumps(
            {
                "schedule_id": str(schedule.id),
                "workload_id": str(schedule.workload_id),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        target = scheduler_v1.HttpTarget(
            uri=self._gateway_uri,
            http_method=scheduler_v1.HttpMethod.POST,
            headers={"Content-Type": "application/json"},
            body=body,
            oidc_token=scheduler_v1.OidcToken(
                service_account_email=self._scheduler_service_account,
                audience=self._gateway_audience,
            ),
        )
        return self._ensure(
            schedule=schedule,
            target=target,
            auth_mode=SchedulerAuthMode.OIDC,
        )

    def ensure_enabled(self, schedule: Schedule) -> None:
        self.ensure_gateway_schedule(schedule)

    def pause(self, schedule: Schedule) -> SchedulerJobMetadata:
        self._require_schedule_state(schedule, ScheduleState.PAUSED)
        metadata = self.ensure_gateway_schedule(
            Schedule(
                id=schedule.id,
                owner_id=schedule.owner_id,
                workload_id=schedule.workload_id,
                cron=schedule.cron,
                timezone=schedule.timezone,
                state=ScheduleState.ENABLED,
                created_at=schedule.created_at,
                updated_at=schedule.updated_at,
                consecutive_failures=schedule.consecutive_failures,
                lease_token=schedule.lease_token,
                lease_expires_at=schedule.lease_expires_at,
                last_attempt_at=schedule.last_attempt_at,
                last_success_at=schedule.last_success_at,
                version=schedule.version,
            )
        )
        try:
            paused = self._client.pause_job(
                scheduler_v1.PauseJobRequest(name=metadata.name)
            )
        except Exception:
            raise CloudSchedulerAdapterError(_FAILED) from None
        _require_response_name(paused, expected=metadata.name)
        if paused.state is not scheduler_v1.Job.State.PAUSED:
            raise CloudSchedulerAdapterError(_FAILED)
        return metadata

    def resume(self, schedule: Schedule) -> SchedulerJobMetadata:
        self._require_schedule_state(schedule, ScheduleState.ENABLED)
        return self.ensure_gateway_schedule(schedule)

    def ensure_direct_job_schedule(
        self,
        schedule: Schedule,
        *,
        cloud_run_job_name: str,
    ) -> SchedulerJobMetadata:
        del schedule, cloud_run_job_name
        raise CloudSchedulerAdapterError(_FAILED)

    def _ensure(
        self,
        *,
        schedule: Schedule,
        target: scheduler_v1.HttpTarget,
        auth_mode: SchedulerAuthMode,
    ) -> SchedulerJobMetadata:
        name = self._job_resource(schedule)
        desired = scheduler_v1.Job(
            name=name,
            description=f"MIM managed hourly schedule {_stable_hash(str(schedule.id))}",
            http_target=target,
            schedule=schedule.cron,
            time_zone=schedule.timezone,
            retry_config=scheduler_v1.RetryConfig(retry_count=0),
            attempt_deadline=duration_pb2.Duration(seconds=_ATTEMPT_DEADLINE_SECONDS),
        )
        try:
            existing = self._client.get_job(scheduler_v1.GetJobRequest(name=name))
        except NotFound:
            try:
                created = self._client.create_job(
                    scheduler_v1.CreateJobRequest(
                        parent=self._parent,
                        job=desired,
                    )
                )
            except Exception:
                raise CloudSchedulerAdapterError(_FAILED) from None
            self._ensure_enabled(created, desired=desired)
            return SchedulerJobMetadata(
                name=name,
                created=True,
                auth_mode=auth_mode,
            )
        except Exception:
            raise CloudSchedulerAdapterError(_FAILED) from None

        _require_response_name(existing, expected=name)
        if _managed_job_equal(existing, desired):
            self._ensure_enabled(existing, desired=desired)
            return SchedulerJobMetadata(
                name=name,
                created=False,
                auth_mode=auth_mode,
            )
        try:
            updated = self._client.update_job(
                scheduler_v1.UpdateJobRequest(
                    job=desired,
                    update_mask=field_mask_pb2.FieldMask(paths=_UPDATE_PATHS),
                )
            )
        except Exception:
            raise CloudSchedulerAdapterError(_FAILED) from None
        self._ensure_enabled(updated, desired=desired)
        return SchedulerJobMetadata(
            name=name,
            created=False,
            auth_mode=auth_mode,
        )

    def _ensure_enabled(
        self,
        job: scheduler_v1.Job,
        *,
        desired: scheduler_v1.Job,
    ) -> None:
        _require_response_name(job, expected=desired.name)
        if not _managed_job_equal(job, desired):
            raise CloudSchedulerAdapterError(_FAILED)
        if job.state is scheduler_v1.Job.State.ENABLED:
            return
        if job.state is not scheduler_v1.Job.State.PAUSED:
            raise CloudSchedulerAdapterError(_FAILED)
        try:
            resumed = self._client.resume_job(
                scheduler_v1.ResumeJobRequest(name=desired.name)
            )
        except Exception:
            raise CloudSchedulerAdapterError(_FAILED) from None
        _require_response_name(resumed, expected=desired.name)
        if (
            resumed.state is not scheduler_v1.Job.State.ENABLED
            or not _managed_job_equal(resumed, desired)
        ):
            raise CloudSchedulerAdapterError(_FAILED)

    def _require_schedule(self, schedule: Schedule) -> None:
        try:
            if type(schedule) is not Schedule:
                raise ValueError
            if schedule.state is not ScheduleState.ENABLED:
                raise ValueError
            cron, timezone = normalize_schedule_policy(
                schedule.cron,
                schedule.timezone,
            )
            if cron != schedule.cron or timezone != schedule.timezone:
                raise ValueError
        except Exception:
            raise CloudSchedulerAdapterError(_FAILED) from None

    def _require_schedule_state(
        self,
        schedule: Schedule,
        expected_state: ScheduleState,
    ) -> None:
        if type(schedule) is not Schedule or schedule.state is not expected_state:
            raise CloudSchedulerAdapterError(_FAILED)
        cron, timezone = normalize_schedule_policy(schedule.cron, schedule.timezone)
        if cron != schedule.cron or timezone != schedule.timezone:
            raise CloudSchedulerAdapterError(_FAILED)

    def _job_resource(self, schedule: Schedule) -> str:
        return f"{self._parent}/jobs/mim-sch-{_stable_hash(str(schedule.id))}"

    @property
    def _parent(self) -> str:
        return f"projects/{self._project_id}/locations/{self._region}"


def _require_project_id(value: object) -> str:
    if type(value) is not str or _PROJECT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("project_id is invalid.")
    return value


def _require_service_account(value: object, *, project_id: str) -> str:
    if type(value) is not str:
        raise ValueError("service account is invalid.")
    match = _SERVICE_ACCOUNT_PATTERN.fullmatch(value)
    if match is None or match.group(1) != project_id:
        raise ValueError("service account is invalid.")
    return value


def _require_numeric_project_number(value: object) -> str:
    if type(value) is not str or not value.isdigit() or value.startswith("0"):
        raise ValueError("project_number is invalid.")
    return value


def _require_response_name(job: Any, *, expected: str) -> None:
    if not isinstance(job, scheduler_v1.Job) or job.name != expected:
        raise CloudSchedulerAdapterError(_FAILED)


def _managed_job_equal(
    current: scheduler_v1.Job,
    desired: scheduler_v1.Job,
) -> bool:
    return (
        current.name == desired.name
        and current.description == desired.description
        and current.http_target == desired.http_target
        and current.schedule == desired.schedule
        and current.time_zone == desired.time_zone
        and current.retry_config == desired.retry_config
        and current.attempt_deadline == desired.attempt_deadline
    )


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
