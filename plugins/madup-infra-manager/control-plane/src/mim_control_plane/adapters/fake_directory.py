"""Deterministic fake Google Directory provider for sync tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from mim_control_plane.domain.directory_sync import DirectoryAuthoritativeSnapshot
from mim_control_plane.ports.directory import DirectoryProvider, DirectoryProviderError


@dataclass(frozen=True, slots=True)
class DirectoryFetchCall:
    required_group: str
    requested_at: datetime


class FakeDirectoryProvider(DirectoryProvider):
    def __init__(
        self,
        *,
        snapshot: DirectoryAuthoritativeSnapshot | None = None,
        snapshot_override: DirectoryAuthoritativeSnapshot | object | None = None,
        error: Exception | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._snapshot_override = snapshot_override
        self._error = error
        self.fetch_calls: tuple[DirectoryFetchCall, ...] = ()

    def fetch_snapshot(
        self,
        *,
        required_group: str,
        now: datetime,
    ) -> DirectoryAuthoritativeSnapshot:
        self.fetch_calls = self.fetch_calls + (
            DirectoryFetchCall(required_group=required_group, requested_at=now),
        )
        if self._error is not None:
            if isinstance(self._error, DirectoryProviderError):
                raise self._error
            raise DirectoryProviderError("Directory snapshot failed.") from None
        if self._snapshot_override is not None:
            return self._snapshot_override  # type: ignore[return-value]
        if self._snapshot is None:
            raise DirectoryProviderError("Directory snapshot failed.")
        return self._snapshot

    def __repr__(self) -> str:
        call_proof = sha256(str(len(self.fetch_calls)).encode("utf-8")).hexdigest()[:12]
        return (
            "FakeDirectoryProvider("
            f"fetch_calls={len(self.fetch_calls)!r}, call_proof={call_proof!r})"
        )
