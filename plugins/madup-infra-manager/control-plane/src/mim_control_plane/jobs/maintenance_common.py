"""Shared helpers for leased maintenance Cloud Run jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Mapping, Protocol, TextIO, runtime_checkable
from uuid import uuid4

from mim_control_plane.config import Settings


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class OverlapLeaseClaim:
    token: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.token, str)
            or not self.token
            or self.token != self.token.strip()
            or len(self.token) > 256
        ):
            raise ValueError("overlap lease token is invalid.")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("overlap lease expiry must be UTC-aware.")

    def __repr__(self) -> str:
        return "OverlapLeaseClaim(redacted=True)"


class OverlapLease(Protocol):
    def try_acquire(
        self,
        *,
        now: datetime,
        duration: timedelta,
    ) -> OverlapLeaseClaim | None: ...

    def release(self, claim: OverlapLeaseClaim) -> None: ...


class MaintenanceStatusStore(Protocol):
    def record_maintenance_job_started(
        self,
        *,
        job_name: str,
        run_id: str,
        started_at: datetime,
    ) -> object: ...

    def record_maintenance_job_terminal(
        self,
        *,
        job_name: str,
        run_id: str,
        expected_version: int,
        finished_at: datetime,
        outcome: str,
        summary: tuple[tuple[str, int], ...],
        failure_code: str | None = None,
        failure_class: str | None = None,
    ) -> object: ...


@runtime_checkable
class SettingsCarrier(Protocol):
    @property
    def public_settings(self) -> Settings: ...


def resolve_settings(source: Mapping[str, object] | object) -> Settings:
    if isinstance(source, Mapping):
        return Settings.from_mapping(source)
    if isinstance(source, SettingsCarrier):
        return source.public_settings
    raise ValueError("maintenance runtime settings source is invalid.")


def require_job_time(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError("maintenance job time is invalid.")
    return value


def write_event(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    stream.write("\n")


def generate_run_id() -> str:
    return uuid4().hex


def summarize_counts(**counts: int) -> tuple[tuple[str, int], ...]:
    summary: list[tuple[str, int]] = []
    for key, value in counts.items():
        if type(value) is not int or value < 0:
            raise ValueError("maintenance summary count is invalid.")
        summary.append((key, value))
    return tuple(summary)


def failure_metadata(exc: Exception) -> tuple[str, str]:
    lowered = exc.__class__.__name__.casefold()
    if "store" in lowered or "firestore" in lowered:
        return ("persistence_error", "persistence")
    if isinstance(exc, ValueError):
        return ("invalid_runtime", "validation")
    return ("runtime_error", "internal")


def hash_browser_request_id(request_id: str) -> str:
    digest = sha256()
    digest.update(b"mim:dashboard-view:v1\x00")
    digest.update(request_id.encode("utf-8"))
    return digest.hexdigest()


__all__ = [
    "MaintenanceStatusStore",
    "OverlapLease",
    "OverlapLeaseClaim",
    "SettingsCarrier",
    "failure_metadata",
    "generate_run_id",
    "hash_browser_request_id",
    "require_job_time",
    "resolve_settings",
    "summarize_counts",
    "utcnow",
    "write_event",
]
