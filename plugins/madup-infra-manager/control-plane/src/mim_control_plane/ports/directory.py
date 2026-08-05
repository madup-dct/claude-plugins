"""Ports for centrally managed Google Directory identity reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from mim_control_plane.domain.directory_sync import (
    DIRECTORY_READONLY_SCOPES,
    MAX_DIRECTORY_SNAPSHOT_USERS,
    DirectoryAuthoritativeSnapshot,
    DirectoryUserReconciliation,
)
from mim_control_plane.domain.models import AuditEvent, AuditEventId, User, UserId


class DirectoryProviderError(RuntimeError):
    """Raised when the central directory snapshot cannot be completed."""


@dataclass(frozen=True, slots=True)
class DirectorySyncLeaseClaim:
    """Opaque ownership proof for one bounded Directory sync execution."""

    token: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.token, str)
            or not self.token
            or self.token != self.token.strip()
            or len(self.token) > 256
        ):
            raise ValueError("directory sync lease token is invalid.")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("directory sync lease expiry must be UTC-aware.")

    def __repr__(self) -> str:
        return "DirectorySyncLeaseClaim(redacted=True)"


@dataclass(frozen=True, slots=True)
class DirectoryIdentityRepositoryResult:
    snapshot_id: str
    material_hash: str
    replayed: bool
    applied_user_ids: tuple[UserId, ...]
    locked_user_ids: tuple[UserId, ...]
    audit_event_ids: tuple[AuditEventId, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id.strip():
            raise ValueError("snapshot_id must be a non-empty string.")
        if (
            not isinstance(self.material_hash, str)
            or len(self.material_hash) != 64
            or any(char not in "0123456789abcdef" for char in self.material_hash)
        ):
            raise ValueError("material_hash must be a lowercase sha256 digest.")
        if type(self.replayed) is not bool:
            raise ValueError("replayed must be an exact bool.")
        if len(self.applied_user_ids) > MAX_DIRECTORY_SNAPSHOT_USERS:
            raise ValueError("applied_user_ids exceeds the bounded size.")
        for field_name, values in (
            ("applied_user_ids", self.applied_user_ids),
            ("locked_user_ids", self.locked_user_ids),
            ("audit_event_ids", self.audit_event_ids),
        ):
            if not isinstance(values, tuple):
                raise ValueError(f"{field_name} must be an immutable tuple.")
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{field_name} entries must be non-empty strings.")
                if value != value.strip():
                    raise ValueError(
                        f"{field_name} entries must not contain surrounding whitespace."
                    )
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique.")
        applied_set = set(self.applied_user_ids)
        if not set(self.locked_user_ids).issubset(applied_set):
            raise ValueError("locked_user_ids must be a subset of applied_user_ids.")
        locked_set = set(self.locked_user_ids)
        if self.locked_user_ids != tuple(
            user_id for user_id in self.applied_user_ids if user_id in locked_set
        ):
            raise ValueError("locked_user_ids must preserve applied user ordering.")
        if len(self.audit_event_ids) != len(self.applied_user_ids):
            raise ValueError("audit_event_ids must match applied_user_ids one-for-one.")

    def __repr__(self) -> str:
        return (
            "DirectoryIdentityRepositoryResult("
            f"snapshot_id={self.snapshot_id!r}, replayed={self.replayed!r}, "
            f"applied_users={len(self.applied_user_ids)!r}, "
            f"locked_users={len(self.locked_user_ids)!r}, "
            f"audit_events={len(self.audit_event_ids)!r})"
        )


class DirectoryProvider(Protocol):
    def fetch_snapshot(
        self,
        *,
        required_group: str,
        now: datetime,
    ) -> DirectoryAuthoritativeSnapshot: ...


class DirectoryIdentityRepository(Protocol):
    def list_users(self) -> tuple[User, ...]: ...

    def apply_snapshot_once(
        self,
        *,
        snapshot_id: str,
        material_hash: str,
        reconciliations: tuple[DirectoryUserReconciliation, ...],
        audit_events: tuple[AuditEvent, ...],
    ) -> DirectoryIdentityRepositoryResult: ...


class DirectorySyncLease(Protocol):
    def try_acquire(
        self,
        *,
        now: datetime,
        duration: timedelta,
    ) -> DirectorySyncLeaseClaim | None: ...

    def release(self, claim: DirectorySyncLeaseClaim) -> None: ...


__all__ = [
    "DIRECTORY_READONLY_SCOPES",
    "DirectoryIdentityRepository",
    "DirectoryIdentityRepositoryResult",
    "DirectoryProvider",
    "DirectoryProviderError",
    "DirectorySyncLease",
    "DirectorySyncLeaseClaim",
]
