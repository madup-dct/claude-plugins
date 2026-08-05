"""Firestore persistence for authoritative Directory identity snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Callable, Iterable, Protocol, cast

from mim_control_plane.config import Settings
from mim_control_plane.domain.directory_sync import DirectoryUserReconciliation
from mim_control_plane.domain.models import (
    AuditEvent,
    AuditEventId,
    User,
    UserId,
)
from mim_control_plane.domain.states import UserRole, UserState
from mim_control_plane.ports.directory import (
    DirectoryIdentityRepositoryResult,
    DirectorySyncLeaseClaim,
)
from mim_control_plane.ports.store import (
    AlreadyExists,
    IdempotencyConflict,
    InvariantViolation,
    NotFound,
    StoreError,
    VersionConflict,
)
from mim_control_plane.services.directory_repository import (
    require_directory_material_hash,
    require_directory_snapshot_id,
    validate_directory_snapshot_write,
)

_SCHEMA_VERSION = 1
_USERS_COLLECTION = "users"
_AUDIT_EVENTS_COLLECTION = "audit_events"
_SNAPSHOT_LEDGER_COLLECTION = "directory_snapshot_ledger"
_DIRECTORY_SYNC_LEASES_COLLECTION = "directory_sync_leases"
_DOCUMENT_ID_PREFIX = b"mim:firestore-directory:v1\x00"
_LEASE_TOKEN_HASH_PREFIX = b"mim:directory-sync-lease-token:v1\x00"
_DOCUMENT_KINDS = frozenset({"user", "audit", "snapshot", "lease"})
_MAX_LEASE_DURATION = timedelta(minutes=15)
_USER_DOCUMENT_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "email",
        "role",
        "state",
        "groups",
        "identity_synced_at",
        "created_at",
        "updated_at",
        "version",
    }
)
_LEDGER_DOCUMENT_FIELDS = frozenset(
    {
        "schema_version",
        "snapshot_id",
        "material_hash",
        "applied_user_ids",
        "locked_user_ids",
        "audit_event_ids",
    }
)
_LEASE_DOCUMENT_FIELDS = frozenset(
    {
        "schema_version",
        "required_group",
        "token_hash",
        "acquired_at",
        "expires_at",
    }
)


class _DocumentSnapshot(Protocol):
    id: str
    exists: bool

    def to_dict(self) -> dict[str, object] | None: ...


class _DocumentReference(Protocol):
    id: str

    def get(self, *, transaction: object | None = None) -> _DocumentSnapshot: ...


class _Query(Protocol):
    def limit(self, count: int) -> _Query: ...

    def stream(self) -> Iterable[_DocumentSnapshot]: ...


class _Collection(_Query, Protocol):
    def document(self, document_id: str) -> _DocumentReference: ...


class _FirestoreClient(Protocol):
    def collection(self, name: str) -> _Collection: ...


class _Transaction(Protocol):
    def set(
        self,
        reference: _DocumentReference,
        data: dict[str, object],
    ) -> None: ...

    def create(
        self,
        reference: _DocumentReference,
        data: dict[str, object],
    ) -> None: ...

    def delete(self, reference: _DocumentReference) -> None: ...


def _google_auth_compute_engine_credentials_factory() -> object:
    from google.auth import compute_engine

    return compute_engine.Credentials()


def _compute_metadata_credentials() -> object:
    return _google_auth_compute_engine_credentials_factory()


def _firestore_client_factory(
    *,
    project: str,
    database: str,
    credentials: object,
) -> object:
    from google.cloud import firestore_v1

    return firestore_v1.Client(
        project=project,
        database=database,
        credentials=credentials,
    )


def _run_firestore_transaction(
    client: object,
    operation: Callable[[object], object],
) -> object:
    from google.cloud import firestore_v1

    transaction_factory = getattr(client, "transaction")
    transaction = transaction_factory(max_attempts=5)
    return firestore_v1.transactional(operation)(transaction)


def _document_id(*, kind: str, logical_id: str) -> str:
    if kind not in _DOCUMENT_KINDS:
        raise InvariantViolation("directory repository document kind is invalid.")
    if (
        not isinstance(logical_id, str)
        or not logical_id.strip()
        or logical_id != logical_id.strip()
    ):
        raise InvariantViolation("directory repository document ID is invalid.")
    digest = sha256()
    digest.update(_DOCUMENT_ID_PREFIX)
    digest.update(kind.encode("ascii"))
    digest.update(b"\x00")
    digest.update(logical_id.encode("utf-8"))
    return digest.hexdigest()


def _new_lease_token() -> str:
    return token_urlsafe(32)


def _require_utc_datetime(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{field_name} must be UTC-aware.")
    return value


def _require_lease_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
    ):
        raise ValueError("directory sync lease token is invalid.")
    return value


def _lease_token_hash(token: str) -> str:
    digest = sha256()
    digest.update(_LEASE_TOKEN_HASH_PREFIX)
    digest.update(token.encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class _DirectorySyncLeaseRecord:
    required_group: str
    token_hash: str
    acquired_at: datetime
    expires_at: datetime


def _lease_record_from_snapshot(
    snapshot: _DocumentSnapshot,
    *,
    expected_document_id: str,
) -> _DirectorySyncLeaseRecord:
    try:
        if snapshot.exists is not True or snapshot.id != expected_document_id:
            raise ValueError
        data = _require_exact_mapping(
            snapshot.to_dict(),
            fields=_LEASE_DOCUMENT_FIELDS,
        )
        if type(data["schema_version"]) is not int:
            raise ValueError
        if data["schema_version"] != _SCHEMA_VERSION:
            raise ValueError
        required_group = cast(str, data["required_group"])
        token_hash = cast(str, data["token_hash"])
        acquired_at = _require_utc_datetime(data["acquired_at"], "acquired_at")
        expires_at = _require_utc_datetime(data["expires_at"], "expires_at")
        if (
            not isinstance(required_group, str)
            or not required_group
            or required_group != required_group.strip()
            or len(required_group) > 128
            or not isinstance(token_hash, str)
            or len(token_hash) != 64
            or any(char not in "0123456789abcdef" for char in token_hash)
            or expires_at <= acquired_at
            or expires_at - acquired_at > _MAX_LEASE_DURATION
        ):
            raise ValueError
        return _DirectorySyncLeaseRecord(
            required_group=required_group,
            token_hash=token_hash,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError, InvariantViolation):
        raise InvariantViolation("directory sync lease data is invalid.") from None


def _serialize_lease_record(
    *,
    required_group: str,
    token_hash: str,
    acquired_at: datetime,
    expires_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "required_group": required_group,
        "token_hash": token_hash,
        "acquired_at": acquired_at,
        "expires_at": expires_at,
    }


def _lease_record_matches_claim(
    record: _DirectorySyncLeaseRecord,
    *,
    required_group: str,
    token_hash: str,
    acquired_at: datetime,
    expires_at: datetime,
) -> bool:
    return (
        record.required_group == required_group
        and record.token_hash == token_hash
        and record.acquired_at == acquired_at
        and record.expires_at == expires_at
    )


def _recover_committed_lease(
    *,
    reference: _DocumentReference,
    required_group: str,
    token_hash: str,
    acquired_at: datetime,
    expires_at: datetime,
) -> bool:
    try:
        snapshot = reference.get()
        if not snapshot.exists:
            return False
        record = _lease_record_from_snapshot(
            snapshot,
            expected_document_id=reference.id,
        )
        return _lease_record_matches_claim(
            record,
            required_group=required_group,
            token_hash=token_hash,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )
    except Exception:
        return False


def _require_exact_mapping(
    value: object,
    *,
    fields: frozenset[str],
) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != fields:
        raise InvariantViolation("directory repository data is invalid.")
    return value


def _user_from_snapshot(snapshot: _DocumentSnapshot) -> User:
    try:
        if snapshot.exists is not True:
            raise ValueError
        data = _require_exact_mapping(
            snapshot.to_dict(),
            fields=_USER_DOCUMENT_FIELDS,
        )
        if type(data["schema_version"]) is not int:
            raise ValueError
        if data["schema_version"] != _SCHEMA_VERSION:
            raise ValueError
        groups = data["groups"]
        if not isinstance(groups, list) or any(
            not isinstance(group, str) or not group.strip() or group != group.strip()
            for group in groups
        ):
            raise ValueError
        if groups != sorted(set(groups)):
            raise ValueError
        user = User(
            id=UserId(cast(str, data["id"])),
            email=cast(str, data["email"]),
            role=UserRole(cast(str, data["role"])),
            state=UserState(cast(str, data["state"])),
            groups=frozenset(groups),
            identity_synced_at=cast(datetime, data["identity_synced_at"]),
            created_at=cast(datetime, data["created_at"]),
            updated_at=cast(datetime, data["updated_at"]),
            version=cast(int, data["version"]),
        )
        if snapshot.id != _document_id(kind="user", logical_id=str(user.id)):
            raise ValueError
        return user
    except (KeyError, TypeError, ValueError, InvariantViolation):
        raise InvariantViolation("directory repository data is invalid.") from None


def _serialize_user(user: User) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "id": str(user.id),
        "email": user.email,
        "role": user.role.value,
        "state": user.state.value,
        "groups": sorted(user.groups),
        "identity_synced_at": user.identity_synced_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "version": user.version,
    }


def _serialize_audit_event(event: AuditEvent) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "id": str(event.id),
        "actor_id": None if event.actor_id is None else str(event.actor_id),
        "action": event.action,
        "target_ref": event.target_ref,
        "policy_decision": event.policy_decision,
        "before_ref": event.before_ref,
        "after_ref": event.after_ref,
        "correlation_id": event.correlation_id,
        "outcome": event.outcome,
        "occurred_at": event.occurred_at,
    }


def _serialize_ledger(
    result: DirectoryIdentityRepositoryResult,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "snapshot_id": result.snapshot_id,
        "material_hash": result.material_hash,
        "applied_user_ids": [str(user_id) for user_id in result.applied_user_ids],
        "locked_user_ids": [str(user_id) for user_id in result.locked_user_ids],
        "audit_event_ids": [str(event_id) for event_id in result.audit_event_ids],
    }


def _ledger_from_snapshot(
    snapshot: _DocumentSnapshot,
    *,
    max_identities: int,
) -> DirectoryIdentityRepositoryResult:
    try:
        if snapshot.exists is not True:
            raise ValueError
        data = _require_exact_mapping(
            snapshot.to_dict(),
            fields=_LEDGER_DOCUMENT_FIELDS,
        )
        if type(data["schema_version"]) is not int:
            raise ValueError
        if data["schema_version"] != _SCHEMA_VERSION:
            raise ValueError
        snapshot_id = cast(str, data["snapshot_id"])
        material_hash = cast(str, data["material_hash"])
        if snapshot.id != _document_id(kind="snapshot", logical_id=snapshot_id):
            raise ValueError
        require_directory_snapshot_id(snapshot_id)
        require_directory_material_hash(material_hash)
        sequence_fields: dict[str, list[str]] = {}
        for field_name in (
            "applied_user_ids",
            "locked_user_ids",
            "audit_event_ids",
        ):
            values = data[field_name]
            if not isinstance(values, list) or any(
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                for value in values
            ):
                raise ValueError
            if len(values) > max_identities:
                raise ValueError
            sequence_fields[field_name] = values
        return DirectoryIdentityRepositoryResult(
            snapshot_id=snapshot_id,
            material_hash=material_hash,
            replayed=False,
            applied_user_ids=tuple(
                UserId(value) for value in sequence_fields["applied_user_ids"]
            ),
            locked_user_ids=tuple(
                UserId(value) for value in sequence_fields["locked_user_ids"]
            ),
            audit_event_ids=tuple(
                AuditEventId(value) for value in sequence_fields["audit_event_ids"]
            ),
        )
    except (KeyError, TypeError, ValueError, StoreError):
        raise InvariantViolation("directory repository data is invalid.") from None


def _replay_result(
    *,
    snapshot_id: str,
    material_hash: str,
) -> DirectoryIdentityRepositoryResult:
    return DirectoryIdentityRepositoryResult(
        snapshot_id=snapshot_id,
        material_hash=material_hash,
        replayed=True,
        applied_user_ids=(),
        locked_user_ids=(),
        audit_event_ids=(),
    )


def _recover_committed_snapshot(
    *,
    client: _FirestoreClient,
    snapshot_id: str,
    material_hash: str,
    max_identities: int,
) -> DirectoryIdentityRepositoryResult | None:
    try:
        reference = client.collection(_SNAPSHOT_LEDGER_COLLECTION).document(
            _document_id(kind="snapshot", logical_id=snapshot_id)
        )
        snapshot = reference.get()
        if not snapshot.exists:
            return None
        recorded = _ledger_from_snapshot(
            snapshot,
            max_identities=max_identities,
        )
        if recorded.snapshot_id != snapshot_id:
            return None
        if recorded.material_hash != material_hash:
            raise IdempotencyConflict("directory snapshot material conflicts.")
        return _replay_result(
            snapshot_id=snapshot_id,
            material_hash=material_hash,
        )
    except IdempotencyConflict:
        raise
    except Exception:
        return None


@dataclass(frozen=True, slots=True)
class FirestoreDirectorySyncLease:
    """Serializes the private Directory reader inside the central project."""

    settings: Settings = field(repr=False)
    required_group: str
    token_factory: Callable[[], str] = field(default=_new_lease_token, repr=False)
    credentials_loader: Callable[[], object] = field(
        default=_compute_metadata_credentials,
        repr=False,
    )
    client_factory: Callable[..., object] = field(
        default=_firestore_client_factory,
        repr=False,
    )
    transaction_runner: Callable[
        [object, Callable[[object], object]],
        object,
    ] = field(default=_run_firestore_transaction, repr=False)
    _client: object = field(init=False, repr=False)
    _reference: _DocumentReference = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            if not isinstance(self.settings, Settings):
                raise ValueError
            if (
                not isinstance(self.required_group, str)
                or not self.required_group
                or self.required_group != self.required_group.strip()
                or len(self.required_group) > 128
            ):
                raise ValueError
            credentials = self.credentials_loader()
            client = self.client_factory(
                project=self.settings.project_id,
                database=self.settings.firestore_database_id,
                credentials=credentials,
            )
            reference = (
                cast(_FirestoreClient, client)
                .collection(_DIRECTORY_SYNC_LEASES_COLLECTION)
                .document(_document_id(kind="lease", logical_id=self.required_group))
            )
            object.__setattr__(self, "_client", client)
            object.__setattr__(self, "_reference", reference)
        except Exception:
            raise StoreError("Directory sync lease initialization failed.") from None

    def try_acquire(
        self,
        *,
        now: datetime,
        duration: timedelta,
    ) -> DirectorySyncLeaseClaim | None:
        acquired_at = _require_utc_datetime(now, "directory sync lease")
        if (
            not isinstance(duration, timedelta)
            or duration <= timedelta(0)
            or duration > _MAX_LEASE_DURATION
        ):
            raise ValueError("directory sync lease duration is invalid.")
        token = _require_lease_token(self.token_factory())
        token_hash = _lease_token_hash(token)
        expires_at = acquired_at + duration
        claim = DirectorySyncLeaseClaim(token=token, expires_at=expires_at)

        def operation(raw_transaction: object) -> DirectorySyncLeaseClaim | None:
            transaction = cast(_Transaction, raw_transaction)
            snapshot = self._reference.get(transaction=transaction)
            if snapshot.exists:
                current = _lease_record_from_snapshot(
                    snapshot,
                    expected_document_id=self._reference.id,
                )
                if current.required_group != self.required_group:
                    raise InvariantViolation("directory sync lease data is invalid.")
                if current.expires_at > acquired_at:
                    if _lease_record_matches_claim(
                        current,
                        required_group=self.required_group,
                        token_hash=token_hash,
                        acquired_at=acquired_at,
                        expires_at=expires_at,
                    ):
                        return claim
                    return None
            transaction.set(
                self._reference,
                _serialize_lease_record(
                    required_group=self.required_group,
                    token_hash=token_hash,
                    acquired_at=acquired_at,
                    expires_at=expires_at,
                ),
            )
            return claim

        try:
            result = self.transaction_runner(self._client, operation)
            if result is not None and not isinstance(
                result,
                DirectorySyncLeaseClaim,
            ):
                raise InvariantViolation("directory sync lease result is invalid.")
            return cast(DirectorySyncLeaseClaim | None, result)
        except Exception:
            if _recover_committed_lease(
                reference=self._reference,
                required_group=self.required_group,
                token_hash=token_hash,
                acquired_at=acquired_at,
                expires_at=expires_at,
            ):
                return claim
            raise StoreError("Directory sync lease operation failed.") from None

    def release(self, claim: DirectorySyncLeaseClaim) -> None:
        if not isinstance(claim, DirectorySyncLeaseClaim):
            raise ValueError("directory sync lease claim is invalid.")
        token_hash = _lease_token_hash(_require_lease_token(claim.token))

        def operation(raw_transaction: object) -> None:
            transaction = cast(_Transaction, raw_transaction)
            snapshot = self._reference.get(transaction=transaction)
            if not snapshot.exists:
                return
            current = _lease_record_from_snapshot(
                snapshot,
                expected_document_id=self._reference.id,
            )
            if (
                current.required_group != self.required_group
                or current.token_hash != token_hash
            ):
                raise InvariantViolation("directory sync lease ownership is invalid.")
            transaction.delete(self._reference)

        try:
            result = self.transaction_runner(self._client, operation)
            if result is not None:
                raise InvariantViolation("directory sync lease result is invalid.")
        except StoreError:
            raise StoreError("Directory sync lease operation failed.") from None
        except Exception:
            raise StoreError("Directory sync lease operation failed.") from None

    def __repr__(self) -> str:
        return "FirestoreDirectorySyncLease(redacted=True)"


@dataclass(frozen=True, slots=True)
class FirestoreDirectoryIdentityRepository:
    """Persists MIM identity state inside the explicit central GCP boundary."""

    settings: Settings = field(repr=False)
    credentials_loader: Callable[[], object] = field(
        default=_compute_metadata_credentials,
        repr=False,
    )
    client_factory: Callable[..., object] = field(
        default=_firestore_client_factory,
        repr=False,
    )
    transaction_runner: Callable[
        [object, Callable[[object], object]],
        object,
    ] = field(default=_run_firestore_transaction, repr=False)
    _client: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            if not isinstance(self.settings, Settings):
                raise ValueError
            credentials = self.credentials_loader()
            client = self.client_factory(
                project=self.settings.project_id,
                database=self.settings.firestore_database_id,
                credentials=credentials,
            )
            object.__setattr__(self, "_client", client)
        except Exception:
            raise StoreError("Directory repository initialization failed.") from None

    def list_users(self) -> tuple[User, ...]:
        try:
            client = cast(_FirestoreClient, self._client)
            snapshots = tuple(
                client.collection(_USERS_COLLECTION)
                .limit(self.settings.pilot_max_identities + 1)
                .stream()
            )
            if len(snapshots) > self.settings.pilot_max_identities:
                raise InvariantViolation("directory repository data is invalid.")
            users = tuple(_user_from_snapshot(snapshot) for snapshot in snapshots)
            seen_ids: set[UserId] = set()
            seen_emails: set[str] = set()
            for user in users:
                normalized_email = user.email.casefold()
                if user.id in seen_ids or normalized_email in seen_emails:
                    raise InvariantViolation("directory repository data is invalid.")
                seen_ids.add(user.id)
                seen_emails.add(normalized_email)
            return tuple(sorted(users, key=lambda user: str(user.id)))
        except StoreError:
            raise StoreError("Directory repository operation failed.") from None
        except Exception:
            raise StoreError("Directory repository operation failed.") from None

    def apply_snapshot_once(
        self,
        *,
        snapshot_id: str,
        material_hash: str,
        reconciliations: tuple[DirectoryUserReconciliation, ...],
        audit_events: tuple[AuditEvent, ...],
    ) -> DirectoryIdentityRepositoryResult:
        try:
            require_directory_snapshot_id(snapshot_id)
            require_directory_material_hash(material_hash)
            if not isinstance(reconciliations, tuple):
                raise InvariantViolation("directory reconciliations must be immutable.")
            if not isinstance(audit_events, tuple):
                raise InvariantViolation("directory audit events must be immutable.")
            if len(reconciliations) > self.settings.pilot_max_identities:
                raise InvariantViolation(
                    "directory reconciliations exceed the pilot identity limit."
                )
            if len(audit_events) > self.settings.pilot_max_identities:
                raise InvariantViolation(
                    "directory audit events exceed the pilot identity limit."
                )
            for reconciliation in reconciliations:
                if not isinstance(reconciliation, DirectoryUserReconciliation):
                    raise InvariantViolation("directory reconciliation is invalid.")
            for event in audit_events:
                if not isinstance(event, AuditEvent):
                    raise InvariantViolation("directory audit event is invalid.")

            client = cast(_FirestoreClient, self._client)
            ledger_reference = client.collection(_SNAPSHOT_LEDGER_COLLECTION).document(
                _document_id(kind="snapshot", logical_id=snapshot_id)
            )
            user_references = tuple(
                client.collection(_USERS_COLLECTION).document(
                    _document_id(
                        kind="user",
                        logical_id=str(reconciliation.user.id),
                    )
                )
                for reconciliation in reconciliations
            )
            audit_references = tuple(
                client.collection(_AUDIT_EVENTS_COLLECTION).document(
                    _document_id(kind="audit", logical_id=str(event.id))
                )
                for event in audit_events
            )

            def operation(raw_transaction: object) -> DirectoryIdentityRepositoryResult:
                transaction = cast(_Transaction, raw_transaction)
                ledger_snapshot = ledger_reference.get(transaction=transaction)
                if ledger_snapshot.exists:
                    recorded = _ledger_from_snapshot(
                        ledger_snapshot,
                        max_identities=self.settings.pilot_max_identities,
                    )
                    if recorded.snapshot_id != snapshot_id:
                        raise InvariantViolation(
                            "directory repository data is invalid."
                        )
                    if recorded.material_hash != material_hash:
                        raise IdempotencyConflict(
                            "directory snapshot material conflicts."
                        )
                    return _replay_result(
                        snapshot_id=snapshot_id,
                        material_hash=material_hash,
                    )

                user_snapshots = tuple(
                    reference.get(transaction=transaction)
                    for reference in user_references
                )
                audit_snapshots = tuple(
                    reference.get(transaction=transaction)
                    for reference in audit_references
                )
                current_users: dict[UserId, User] = {}
                for reconciliation, snapshot in zip(
                    reconciliations,
                    user_snapshots,
                    strict=True,
                ):
                    if not snapshot.exists:
                        raise NotFound("user was not found.")
                    current = _user_from_snapshot(snapshot)
                    if current.id != reconciliation.user.id:
                        raise InvariantViolation(
                            "directory repository data is invalid."
                        )
                    current_users[current.id] = current
                existing_audit_ids = frozenset(
                    event.id
                    for event, snapshot in zip(
                        audit_events,
                        audit_snapshots,
                        strict=True,
                    )
                    if snapshot.exists
                )
                result = validate_directory_snapshot_write(
                    snapshot_id=snapshot_id,
                    material_hash=material_hash,
                    reconciliations=reconciliations,
                    audit_events=audit_events,
                    current_users=current_users,
                    existing_audit_event_ids=existing_audit_ids,
                    max_identities=self.settings.pilot_max_identities,
                )
                for reference, reconciliation in zip(
                    user_references,
                    reconciliations,
                    strict=True,
                ):
                    transaction.set(reference, _serialize_user(reconciliation.user))
                for reference, event in zip(
                    audit_references,
                    audit_events,
                    strict=True,
                ):
                    transaction.create(reference, _serialize_audit_event(event))
                transaction.create(ledger_reference, _serialize_ledger(result))
                return result

            result = self.transaction_runner(self._client, operation)
            if not isinstance(result, DirectoryIdentityRepositoryResult):
                raise InvariantViolation("directory repository result is invalid.")
            return result
        except (
            AlreadyExists,
            IdempotencyConflict,
            InvariantViolation,
            NotFound,
            VersionConflict,
        ):
            raise
        except StoreError:
            recovered = _recover_committed_snapshot(
                client=cast(_FirestoreClient, self._client),
                snapshot_id=snapshot_id,
                material_hash=material_hash,
                max_identities=self.settings.pilot_max_identities,
            )
            if recovered is not None:
                return recovered
            raise StoreError("Directory repository operation failed.") from None
        except Exception:
            recovered = _recover_committed_snapshot(
                client=cast(_FirestoreClient, self._client),
                snapshot_id=snapshot_id,
                material_hash=material_hash,
                max_identities=self.settings.pilot_max_identities,
            )
            if recovered is not None:
                return recovered
            raise StoreError("Directory repository operation failed.") from None

    def __repr__(self) -> str:
        return "FirestoreDirectoryIdentityRepository(redacted=True)"
