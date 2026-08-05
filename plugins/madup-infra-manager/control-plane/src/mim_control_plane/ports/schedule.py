"""Private schedule orchestration contracts for the MIM control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from mim_control_plane.domain.models import Schedule, ScheduleId, WorkloadId


class ScheduleExecutionError(RuntimeError):
    """Raised when a schedule control or dispatch action fails closed."""


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be UTC-aware.")


def _require_text(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")


@dataclass(frozen=True, slots=True)
class ScheduledRunRequest:
    schedule_id: ScheduleId
    workload_id: WorkloadId
    tick_at: datetime
    lease_token: str

    def __post_init__(self) -> None:
        _require_text(str(self.schedule_id), "schedule_id")
        _require_text(str(self.workload_id), "workload_id")
        _require_utc(self.tick_at, "tick_at")
        _require_text(self.lease_token, "lease_token")


@dataclass(frozen=True, slots=True)
class ScheduledRunReceipt:
    run_reference: str
    created: bool

    def __post_init__(self) -> None:
        _require_text(self.run_reference, "run_reference")
        if type(self.created) is not bool:
            raise ValueError("created must be an exact bool.")


class ScheduleControlPort(Protocol):
    def ensure_enabled(self, schedule: Schedule) -> None: ...
    def pause(self, schedule: Schedule) -> None: ...
    def resume(self, schedule: Schedule) -> None: ...


class ScheduleRunDispatcher(Protocol):
    def dispatch(self, request: ScheduledRunRequest) -> ScheduledRunReceipt: ...
