"""Central Firestore-backed maintenance lease and lifecycle hold adapters."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Protocol, cast

from mim_control_plane.config import FIRESTORE_DATABASE_ID, Settings
from mim_control_plane.domain.models import UserId, WorkloadId
from mim_control_plane.jobs.maintenance_common import (
    OverlapLease,
    OverlapLeaseClaim,
)
from mim_control_plane.ports.store import Store
from mim_control_plane.services.schedules import require_utc_datetime
from mim_control_plane.workers.maintenance import HoldResolver

_CENTRAL_PROJECT_ID = "mim-prod-123456"
_LEASE_COLLECTION = "maintenance_overlap_leases"
_HOLD_COLLECTION = "lifecycle_hold_sets"
_SCHEMA_VERSION = 1
_MAX_LEASE_DURATION = timedelta(hours=12)
_MAX_HOLD_FRESHNESS = timedelta(hours=1)
_LEASE_FIELDS = frozenset(
    {"schema_version", "lease_name", "token_hash", "acquired_at", "expires_at"}
)
_HOLD_FIELDS = frozenset(
    {
        "schema_version",
        "user_id",
        "hold_workload_ids",
        "owned_workload_ids",
        "issued_at",
        "expires_at",
    }
)


class MaintenanceStateError(RuntimeError):
    """Sanitized fail-closed Firestore maintenance-state error."""


class _DocumentSnapshot(Protocol):
    id: str
    exists: bool

    def to_dict(self) -> dict[str, object] | None: ...


class _DocumentReference(Protocol):
    id: str

    def get(self, *, transaction: object | None = None) -> _DocumentSnapshot: ...


class _Collection(Protocol):
    def document(self, document_id: str) -> _DocumentReference: ...


class _FirestoreClient(Protocol):
    project: str
    database: str

    def collection(self, name: str) -> _Collection: ...


class _Transaction(Protocol):
    def set(self, reference: _DocumentReference, data: dict[str, object]) -> None: ...

    def delete(self, reference: _DocumentReference) -> None: ...


def _compute_metadata_credentials() -> object:
    from google.auth import compute_engine

    return compute_engine.Credentials()


def _firestore_client_factory(
    *,
    project: str,
    database: str,
    credentials: object,
) -> object:
    from google.cloud import firestore  # type: ignore[import-untyped]

    return firestore.Client(
        project=project,
        database=database,
        credentials=credentials,
    )


def _run_transaction(
    client: object,
    operation: Callable[[object], object],
) -> object:
    from google.cloud import firestore_v1

    transaction_factory = getattr(client, "transaction")
    transaction = transaction_factory(max_attempts=5)
    return firestore_v1.transactional(operation)(transaction)


def _require_exact_settings(settings: Settings) -> None:
    if not isinstance(settings, Settings):
        raise ValueError("settings are invalid.")
    if settings.project_id != _CENTRAL_PROJECT_ID:
        raise ValueError("settings project_id is invalid.")
    if settings.firestore_database_id != FIRESTORE_DATABASE_ID:
        raise ValueError("settings firestore database is invalid.")


def _require_exact_name(value: str, *, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 128
    ):
        raise ValueError(f"{field_name} is invalid.")
    return value


def _require_token(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
    ):
        raise ValueError("token is invalid.")
    return value


def _token_hash(token: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"mim:maintenance-overlap-lease:v1\x00")
    digest.update(_require_token(token).encode("utf-8"))
    return digest.hexdigest()


def _lease_document_id(lease_name: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"mim:maintenance-overlap-lease-doc:v1\x00")
    normalized_name = _require_exact_name(lease_name, field_name="lease_name")
    digest.update(normalized_name.encode("utf-8"))
    return digest.hexdigest()


def _hold_document_id(user_id: UserId) -> str:
    digest = hashlib.sha256()
    digest.update(b"mim:lifecycle-hold-set:v1\x00")
    normalized_user_id = _require_exact_name(str(user_id), field_name="user_id")
    digest.update(normalized_user_id.encode("utf-8"))
    return digest.hexdigest()


def _require_string_list(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if type(value) is not list:
        raise MaintenanceStateError("maintenance state was denied.")
    normalized = []
    seen: set[str] = set()
    for item in value:
        normalized_item = _require_exact_name(str(item), field_name=field_name)
        if type(item) is not str or normalized_item in seen:
            raise MaintenanceStateError("maintenance state was denied.")
        seen.add(normalized_item)
        normalized.append(normalized_item)
    return tuple(normalized)


def _require_mapping_fields(
    data: object,
    *,
    expected_fields: frozenset[str],
) -> dict[str, object]:
    if not isinstance(data, dict) or frozenset(data) != expected_fields:
        raise MaintenanceStateError("maintenance state was denied.")
    return dict(data)


def _new_token() -> str:
    return secrets.token_urlsafe(24)


@dataclass(frozen=True, slots=True)
class FirestoreNamedOverlapLease(OverlapLease):
    settings: Settings = field(repr=False)
    lease_name: str = field(repr=False)
    credentials_loader: Callable[[], object] = field(
        default=_compute_metadata_credentials,
        repr=False,
    )
    client_factory: Callable[..., object] = field(
        default=_firestore_client_factory,
        repr=False,
    )
    transaction_runner: Callable[[object, Callable[[object], object]], object] = field(
        default=_run_transaction,
        repr=False,
    )
    token_factory: Callable[[], str] = field(default=_new_token, repr=False)
    _client: object = field(init=False, repr=False)
    _reference: _DocumentReference = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _require_exact_settings(self.settings)
        lease_name = _require_exact_name(self.lease_name, field_name="lease_name")
        credentials = self.credentials_loader()
        client = cast(
            _FirestoreClient,
            self.client_factory(
                project=self.settings.project_id,
                database=self.settings.firestore_database_id,
                credentials=credentials,
            ),
        )
        if (
            client.project != _CENTRAL_PROJECT_ID
            or client.database != FIRESTORE_DATABASE_ID
        ):
            raise ValueError("Firestore client boundary is invalid.")
        reference = client.collection(_LEASE_COLLECTION).document(
            _lease_document_id(lease_name)
        )
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_reference", reference)

    def try_acquire(
        self,
        *,
        now: datetime,
        duration: timedelta,
    ) -> OverlapLeaseClaim | None:
        acquired_at = require_utc_datetime(now, label="maintenance overlap lease")
        if (
            not isinstance(duration, timedelta)
            or duration <= timedelta(0)
            or duration > _MAX_LEASE_DURATION
        ):
            raise ValueError("lease duration is invalid.")
        token = _require_token(self.token_factory())
        expires_at = acquired_at + duration
        claim = OverlapLeaseClaim(token=token, expires_at=expires_at)
        token_hash = _token_hash(token)

        def operation(raw_transaction: object) -> OverlapLeaseClaim | None:
            transaction = cast(_Transaction, raw_transaction)
            snapshot = self._reference.get(transaction=transaction)
            if snapshot.exists:
                record = _lease_record_from_snapshot(
                    snapshot,
                    expected_document_id=self._reference.id,
                    expected_lease_name=self.lease_name,
                )
                if record.expires_at > acquired_at:
                    return None
            transaction.set(
                self._reference,
                {
                    "schema_version": _SCHEMA_VERSION,
                    "lease_name": self.lease_name,
                    "token_hash": token_hash,
                    "acquired_at": acquired_at,
                    "expires_at": expires_at,
                },
            )
            return claim

        try:
            result = self.transaction_runner(self._client, operation)
        except Exception:
            raise MaintenanceStateError("maintenance state was denied.") from None
        if result is not None and not isinstance(result, OverlapLeaseClaim):
            raise MaintenanceStateError("maintenance state was denied.")
        return cast(OverlapLeaseClaim | None, result)

    def release(self, claim: OverlapLeaseClaim) -> None:
        if not isinstance(claim, OverlapLeaseClaim):
            raise ValueError("lease claim is invalid.")
        token_hash = _token_hash(claim.token)

        def operation(raw_transaction: object) -> None:
            transaction = cast(_Transaction, raw_transaction)
            snapshot = self._reference.get(transaction=transaction)
            if not snapshot.exists:
                return
            record = _lease_record_from_snapshot(
                snapshot,
                expected_document_id=self._reference.id,
                expected_lease_name=self.lease_name,
            )
            if record.token_hash != token_hash or record.expires_at != claim.expires_at:
                raise MaintenanceStateError("maintenance state was denied.")
            transaction.delete(self._reference)

        try:
            result = self.transaction_runner(self._client, operation)
        except Exception:
            raise MaintenanceStateError("maintenance state was denied.") from None
        if result is not None:
            raise MaintenanceStateError("maintenance state was denied.")


@dataclass(frozen=True, slots=True)
class FirestoreLifecycleHoldResolver(HoldResolver):
    settings: Settings = field(repr=False)
    store: Store = field(repr=False)
    credentials_loader: Callable[[], object] = field(
        default=_compute_metadata_credentials,
        repr=False,
    )
    client_factory: Callable[..., object] = field(
        default=_firestore_client_factory,
        repr=False,
    )
    _client: object = field(init=False, repr=False)
    _collection: _Collection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _require_exact_settings(self.settings)
        credentials = self.credentials_loader()
        client = cast(
            _FirestoreClient,
            self.client_factory(
                project=self.settings.project_id,
                database=self.settings.firestore_database_id,
                credentials=credentials,
            ),
        )
        if (
            client.project != _CENTRAL_PROJECT_ID
            or client.database != FIRESTORE_DATABASE_ID
        ):
            raise ValueError("Firestore client boundary is invalid.")
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_collection", client.collection(_HOLD_COLLECTION))

    def resolve_holds(
        self,
        *,
        user_id: UserId,
        now: datetime,
    ) -> frozenset[WorkloadId]:
        current_now = require_utc_datetime(now, label="lifecycle hold resolution")
        try:
            self.store.get_user(user_id)
            owned_workload_ids = tuple(
                sorted(
                    (
                        str(workload.id)
                        for workload in self.store.list_workloads(owner_id=user_id)
                    )
                )
            )
            snapshot = self._collection.document(_hold_document_id(user_id)).get()
            if snapshot.exists is not True:
                return frozenset()
            record = _hold_record_from_snapshot(
                snapshot,
                expected_document_id=_hold_document_id(user_id),
                expected_user_id=str(user_id),
            )
            if record.issued_at > current_now:
                raise MaintenanceStateError("maintenance state was denied.")
            if current_now >= record.expires_at:
                raise MaintenanceStateError("maintenance state was denied.")
            if current_now - record.issued_at > _MAX_HOLD_FRESHNESS:
                raise MaintenanceStateError("maintenance state was denied.")
            if record.owned_workload_ids != owned_workload_ids:
                raise MaintenanceStateError("maintenance state was denied.")
            if not set(record.hold_workload_ids).issubset(set(owned_workload_ids)):
                raise MaintenanceStateError("maintenance state was denied.")
            return frozenset(WorkloadId(item) for item in record.hold_workload_ids)
        except MaintenanceStateError:
            raise
        except Exception:
            raise MaintenanceStateError("maintenance state was denied.") from None


@dataclass(frozen=True, slots=True)
class _LeaseRecord:
    token_hash: str
    expires_at: datetime


def _lease_record_from_snapshot(
    snapshot: _DocumentSnapshot,
    *,
    expected_document_id: str,
    expected_lease_name: str,
) -> _LeaseRecord:
    if snapshot.id != expected_document_id:
        raise MaintenanceStateError("maintenance state was denied.")
    data = _require_mapping_fields(snapshot.to_dict(), expected_fields=_LEASE_FIELDS)
    if data["schema_version"] != _SCHEMA_VERSION:
        raise MaintenanceStateError("maintenance state was denied.")
    if data["lease_name"] != expected_lease_name:
        raise MaintenanceStateError("maintenance state was denied.")
    token_hash = data["token_hash"]
    if (
        type(token_hash) is not str
        or len(token_hash) != 64
        or any(char not in "0123456789abcdef" for char in token_hash)
    ):
        raise MaintenanceStateError("maintenance state was denied.")
    acquired_at = require_utc_datetime(
        data["acquired_at"],
        label="maintenance overlap lease",
    )
    expires_at = require_utc_datetime(
        data["expires_at"],
        label="maintenance overlap lease",
    )
    if expires_at <= acquired_at:
        raise MaintenanceStateError("maintenance state was denied.")
    return _LeaseRecord(token_hash=token_hash, expires_at=expires_at)


@dataclass(frozen=True, slots=True)
class _HoldRecord:
    hold_workload_ids: tuple[str, ...]
    owned_workload_ids: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime


def _hold_record_from_snapshot(
    snapshot: _DocumentSnapshot,
    *,
    expected_document_id: str,
    expected_user_id: str,
) -> _HoldRecord:
    if snapshot.id != expected_document_id:
        raise MaintenanceStateError("maintenance state was denied.")
    data = _require_mapping_fields(snapshot.to_dict(), expected_fields=_HOLD_FIELDS)
    if data["schema_version"] != _SCHEMA_VERSION:
        raise MaintenanceStateError("maintenance state was denied.")
    if data["user_id"] != expected_user_id:
        raise MaintenanceStateError("maintenance state was denied.")
    issued_at = require_utc_datetime(
        data["issued_at"],
        label="lifecycle hold set",
    )
    expires_at = require_utc_datetime(
        data["expires_at"],
        label="lifecycle hold set",
    )
    if expires_at <= issued_at:
        raise MaintenanceStateError("maintenance state was denied.")
    return _HoldRecord(
        hold_workload_ids=_require_string_list(
            data["hold_workload_ids"],
            field_name="hold_workload_ids",
        ),
        owned_workload_ids=_require_string_list(
            data["owned_workload_ids"],
            field_name="owned_workload_ids",
        ),
        issued_at=issued_at,
        expires_at=expires_at,
    )


__all__ = [
    "FirestoreLifecycleHoldResolver",
    "FirestoreNamedOverlapLease",
    "MaintenanceStateError",
]
