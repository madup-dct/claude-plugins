"""Deterministic fake adapters for schedule lifecycle and execution tests."""

from __future__ import annotations

from dataclasses import dataclass

from mim_control_plane.domain.models import Schedule
from mim_control_plane.ports.schedule import (
    ScheduleControlPort,
    ScheduledRunReceipt,
    ScheduledRunRequest,
    ScheduleExecutionError,
    ScheduleRunDispatcher,
)


@dataclass(frozen=True, slots=True)
class FakeScheduleDispatchCall:
    schedule_id: str
    workload_id: str
    tick_at: str
    lease_token: str


class FakeScheduleControlPort(ScheduleControlPort):
    def __init__(self) -> None:
        self.ensure_calls: tuple[str, ...] = ()
        self.pause_calls: tuple[str, ...] = ()
        self.resume_calls: tuple[str, ...] = ()
        self.ensure_error: Exception | None = None
        self.pause_error: Exception | None = None
        self.resume_error: Exception | None = None

    def ensure_enabled(self, schedule: Schedule) -> None:
        self.ensure_calls = (*self.ensure_calls, str(schedule.id))
        if self.ensure_error is not None:
            raise self.ensure_error

    def pause(self, schedule: Schedule) -> None:
        self.pause_calls = (*self.pause_calls, str(schedule.id))
        if self.pause_error is not None:
            raise self.pause_error

    def resume(self, schedule: Schedule) -> None:
        self.resume_calls = (*self.resume_calls, str(schedule.id))
        if self.resume_error is not None:
            raise self.resume_error


class FakeScheduleRunDispatcher(ScheduleRunDispatcher):
    def __init__(self) -> None:
        self.calls: list[FakeScheduleDispatchCall] = []
        self.dispatch_error: Exception | None = None

    def dispatch(self, request: ScheduledRunRequest) -> ScheduledRunReceipt:
        if type(request) is not ScheduledRunRequest:
            raise ScheduleExecutionError("scheduled run request must be exact.")
        self.calls.append(
            FakeScheduleDispatchCall(
                schedule_id=str(request.schedule_id),
                workload_id=str(request.workload_id),
                tick_at=request.tick_at.isoformat(),
                lease_token=request.lease_token,
            )
        )
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return ScheduledRunReceipt(
            run_reference=f"run:{request.schedule_id}:{request.tick_at.isoformat()}",
            created=True,
        )
