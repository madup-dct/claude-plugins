"""Framework-independent source snapshot acquisition contract."""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from mim_control_plane.domain.models import RepositoryAdmission


@runtime_checkable
class SourceSnapshotPort(Protocol):
    """Load one admitted immutable commit without caller-supplied source routing.

    Implementations must authenticate centrally, enforce the classifier snapshot
    file/byte bounds, and return an immutable mapping with immutable byte values.
    The admission record is the sole routing input: callers cannot supply an
    owner, repository, ref, URL, installation token, or personal credential.
    """

    def fetch_snapshot(
        self,
        admission: RepositoryAdmission,
    ) -> Mapping[str, bytes]: ...
