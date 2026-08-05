"""Pure helpers for the approved MIM schedule policy."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mim_control_plane.domain.models import Schedule

APPROVED_SCHEDULE_CRON = "0 * * * *"
APPROVED_SCHEDULE_TIMEZONE = "Asia/Seoul"
MAX_SCHEDULE_LEASE_TOKEN_LENGTH = 128
_LEASE_TOKEN_PATTERN = re.compile(r"^lease-[A-Za-z0-9_-]+$")
_LEASE_TOKEN_SECRET_MARKERS = (
    "ghp_",
    "sk-",
    "api_key",
    "authorization",
    "cookie",
    "bearer",
    "access_token",
    "access-token",
    "refresh_token",
    "refresh-token",
)

_SEOUL = ZoneInfo(APPROVED_SCHEDULE_TIMEZONE)


def normalize_schedule_policy(
    cron: object,
    timezone: object,
) -> tuple[str, str]:
    """Accept only the reviewed hourly Seoul schedule policy."""

    if type(cron) is not str or type(timezone) is not str:
        raise ValueError("schedule policy is invalid.")
    normalized_cron = APPROVED_SCHEDULE_CRON if cron == "hourly" else cron
    if (
        normalized_cron != APPROVED_SCHEDULE_CRON
        or timezone != APPROVED_SCHEDULE_TIMEZONE
    ):
        raise ValueError("schedule policy is invalid.")
    return APPROVED_SCHEDULE_CRON, APPROVED_SCHEDULE_TIMEZONE


def schedule_is_due(schedule: Schedule, *, tick_at: datetime) -> bool:
    """Return whether a UTC tick lands exactly on the approved Seoul hour."""

    normalize_schedule_policy(schedule.cron, schedule.timezone)
    tick_at = require_utc_datetime(tick_at, label="schedule tick")
    local_tick = tick_at.astimezone(_SEOUL)
    return (
        local_tick.minute == 0
        and local_tick.second == 0
        and local_tick.microsecond == 0
    )


def require_schedule_lease_token(token: object) -> str:
    """Return a bounded opaque lease token or reject it."""

    if type(token) is not str:
        raise ValueError("schedule lease is invalid.")
    if (
        not token
        or len(token) > MAX_SCHEDULE_LEASE_TOKEN_LENGTH
        or _LEASE_TOKEN_PATTERN.fullmatch(token) is None
    ):
        raise ValueError("schedule lease is invalid.")
    lowered = token.lower()
    if any(marker in lowered for marker in _LEASE_TOKEN_SECRET_MARKERS):
        raise ValueError("schedule lease is invalid.")
    return token


def require_expected_version(value: object, *, label: str) -> int:
    """Require an exact positive integer optimistic version."""

    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} is invalid.")
    return value


def require_utc_datetime(value: object, *, label: str) -> datetime:
    """Require an aware UTC datetime value."""

    if not isinstance(value, datetime):
        raise ValueError(f"{label} is invalid.")
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} is invalid.")
    return value
