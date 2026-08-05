"""Private worker for centrally managed Google Directory identity sync."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable

from mim_control_plane.domain.directory_sync import (
    MAX_DIRECTORY_SNAPSHOT_USERS,
    DirectoryAuthoritativeSnapshot,
    DirectorySnapshotUser,
    DirectoryUserReconciliation,
    build_directory_audit_event,
    projected_target_state,
    validate_directory_snapshot,
)
from mim_control_plane.domain.models import AuditEvent, AuditEventId, User, UserId
from mim_control_plane.domain.states import UserState
from mim_control_plane.ports.directory import (
    DirectoryIdentityRepository,
    DirectoryIdentityRepositoryResult,
    DirectoryProvider,
    DirectoryProviderError,
)
from mim_control_plane.ports.store import StoreError
from mim_control_plane.services.schedules import require_utc_datetime


class DirectorySyncFailed(RuntimeError):
    """Raised when a full authoritative identity sync cannot be applied."""


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class DirectoryIdentitySyncResult:
    snapshot_id: str
    replayed: bool
    processed_users: int
    updated_users: int
    ignored_directory_users: int
    active_users: int
    suspended_users: int
    offboarded_users: int
    locked_user_ids: tuple[UserId, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id.strip():
            raise ValueError("snapshot_id must be a non-empty string.")
        if type(self.replayed) is not bool:
            raise ValueError("replayed must be an exact bool.")
        for field_name in (
            "processed_users",
            "updated_users",
            "ignored_directory_users",
            "active_users",
            "suspended_users",
            "offboarded_users",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer.")
        if not isinstance(self.locked_user_ids, tuple):
            raise ValueError("locked_user_ids must be immutable.")


@dataclass(frozen=True, slots=True)
class DirectoryIdentitySyncWorker:
    directory: DirectoryProvider
    repository: DirectoryIdentityRepository
    required_group: str
    max_snapshot_age: timedelta
    max_collection_duration: timedelta
    clock: Callable[[], datetime] = field(default=_utcnow)

    def run(self, *, now: datetime) -> DirectoryIdentitySyncResult:
        request_now = require_utc_datetime(now, label="directory sync")
        try:
            required_group = _validate_required_group(self.required_group)
            snapshot = validate_directory_snapshot(
                self.directory.fetch_snapshot(
                    required_group=required_group,
                    now=request_now,
                )
            )
            if snapshot.required_group != required_group:
                raise ValueError("directory snapshot required group is invalid.")
            validation_now = require_utc_datetime(
                self.clock(),
                label="directory sync",
            )
            self._validate_snapshot_timing(snapshot=snapshot, now=validation_now)
            users = _validate_repository_users(self.repository.list_users())
            reconciliations, audits, ignored_directory_users = (
                self._build_changes(snapshot=snapshot, users=users)
            )
            material_hash = _snapshot_material_hash(snapshot=snapshot)
            result = self.repository.apply_snapshot_once(
                snapshot_id=snapshot.snapshot_id,
                material_hash=material_hash,
                reconciliations=reconciliations,
                audit_events=audits,
            )
            result = _validate_repository_result(
                result,
                expected_snapshot_id=snapshot.snapshot_id,
                expected_material_hash=material_hash,
                expected_applied_user_ids=tuple(
                    reconciliation.user.id for reconciliation in reconciliations
                ),
                expected_locked_user_ids=tuple(
                    reconciliation.user.id
                    for reconciliation in reconciliations
                    if reconciliation.user.state
                    in {UserState.SUSPENDED, UserState.OFFBOARDED}
                ),
                expected_audit_event_ids=tuple(audit.id for audit in audits),
            )
            persisted_users = _validate_repository_users(self.repository.list_users())
            if not result.replayed:
                persisted_by_id = {user.id: user for user in persisted_users}
                for reconciliation in reconciliations:
                    persisted = persisted_by_id.get(reconciliation.user.id)
                    if persisted != reconciliation.user:
                        raise ValueError("directory reconciliation was not persisted.")
            return _build_sync_result(
                snapshot_id=snapshot.snapshot_id,
                replayed=result.replayed,
                projected_users=persisted_users,
                updated_users=0 if result.replayed else len(result.applied_user_ids),
                ignored_directory_users=ignored_directory_users,
            )
        except (
            DirectoryProviderError,
            StoreError,
            TypeError,
            ValueError,
            AttributeError,
        ):
            raise DirectorySyncFailed("Directory identity sync failed.") from None

    def _validate_snapshot_timing(
        self,
        *,
        snapshot: DirectoryAuthoritativeSnapshot,
        now: datetime,
    ) -> None:
        if (
            not isinstance(self.max_snapshot_age, timedelta)
            or self.max_snapshot_age <= timedelta(0)
        ):
            raise ValueError("max_snapshot_age must be positive.")
        if (
            not isinstance(self.max_collection_duration, timedelta)
            or self.max_collection_duration <= timedelta(0)
        ):
            raise ValueError("max_collection_duration must be positive.")
        if snapshot.completed_at > now:
            raise ValueError("directory snapshot is invalid.")
        if now - snapshot.completed_at > self.max_snapshot_age:
            raise ValueError("directory snapshot is invalid.")
        if snapshot.completed_at - snapshot.started_at > self.max_collection_duration:
            raise ValueError("directory snapshot is invalid.")

    def _build_changes(
        self,
        *,
        snapshot: DirectoryAuthoritativeSnapshot,
        users: tuple[User, ...],
    ) -> tuple[
        tuple[DirectoryUserReconciliation, ...],
        tuple[AuditEvent, ...],
        int,
    ]:
        users_by_email = {user.email.casefold(): user for user in users}
        snapshot_by_email = {entry.email: entry for entry in snapshot.users}
        reconciliations: list[DirectoryUserReconciliation] = []
        audits = []
        for current in users:
            directory_user = snapshot_by_email.get(current.email.casefold())
            policy_decision = _directory_policy_decision(
                current=current,
                directory_user=directory_user,
            )
            updated = self._reconcile_user(
                current=current,
                directory_user=directory_user,
                synced_at=snapshot.completed_at,
            )
            if updated != current:
                reconciliations.append(
                    DirectoryUserReconciliation(
                        user=updated,
                        expected_version=current.version,
                        required_group=self.required_group,
                        policy_decision=policy_decision,
                    )
                )
                audits.append(
                    build_directory_audit_event(
                        snapshot_id=snapshot.snapshot_id,
                        required_group=self.required_group,
                        policy_decision=policy_decision,
                        user_before=current,
                        user_after=updated,
                        synced_at=snapshot.completed_at,
                    )
                )
        ignored_directory_users = sum(
            1
            for email in snapshot_by_email
            if email not in users_by_email
        )
        return (
            tuple(reconciliations),
            tuple(audits),
            ignored_directory_users,
        )

    def _reconcile_user(
        self,
        *,
        current: User,
        directory_user: DirectorySnapshotUser | None,
        synced_at: datetime,
    ) -> User:
        target_state = projected_target_state(
            current_state=current.state,
            present=directory_user is not None,
            active=bool(directory_user and directory_user.active),
            in_required_group=bool(directory_user and directory_user.in_required_group),
        )
        return current.reconcile_identity(
            target_state=target_state,
            required_group=self.required_group,
            in_required_group=bool(directory_user and directory_user.in_required_group),
            synced_at=synced_at,
        )


def _snapshot_material_hash(*, snapshot: DirectoryAuthoritativeSnapshot) -> str:
    digest = hashlib.sha256()
    digest.update(f"{snapshot.snapshot_id}\n".encode("utf-8"))
    digest.update(f"{snapshot.required_group}\n".encode("utf-8"))
    digest.update(f"{snapshot.started_at.isoformat()}\n".encode("utf-8"))
    digest.update(f"{snapshot.completed_at.isoformat()}\n".encode("utf-8"))
    for user in snapshot.users:
        digest.update(
            (
                f"{user.directory_user_id}\n{user.email}\n"
                f"{int(user.active)}\n{int(user.in_required_group)}\n"
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _validate_repository_result(
    result: object,
    *,
    expected_snapshot_id: str,
    expected_material_hash: str,
    expected_applied_user_ids: tuple[UserId, ...],
    expected_locked_user_ids: tuple[UserId, ...],
    expected_audit_event_ids: tuple[AuditEventId, ...],
) -> DirectoryIdentityRepositoryResult:
    if not isinstance(result, DirectoryIdentityRepositoryResult):
        raise ValueError("directory repository result is invalid.")
    validated = DirectoryIdentityRepositoryResult(
        snapshot_id=result.snapshot_id,
        material_hash=result.material_hash,
        replayed=result.replayed,
        applied_user_ids=result.applied_user_ids,
        locked_user_ids=result.locked_user_ids,
        audit_event_ids=result.audit_event_ids,
    )
    if validated.snapshot_id != expected_snapshot_id:
        raise ValueError("directory repository result is invalid.")
    if validated.material_hash != expected_material_hash:
        raise ValueError("directory repository result is invalid.")
    if validated.replayed:
        if (
            validated.applied_user_ids
            or validated.locked_user_ids
            or validated.audit_event_ids
        ):
            raise ValueError("directory repository result is invalid.")
    elif (
        validated.applied_user_ids != expected_applied_user_ids
        or validated.locked_user_ids != expected_locked_user_ids
        or validated.audit_event_ids != expected_audit_event_ids
    ):
        raise ValueError("directory repository result is invalid.")
    return validated


def _validate_required_group(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("required_group must be a non-empty string.")
    if value != value.strip():
        raise ValueError("required_group must not contain surrounding whitespace.")
    return value


def _validate_repository_users(value: object) -> tuple[User, ...]:
    if not isinstance(value, tuple):
        raise ValueError("directory repository users must be immutable.")
    if len(value) > MAX_DIRECTORY_SNAPSHOT_USERS:
        raise ValueError("directory repository users exceed the bounded size.")
    validated: list[User] = []
    seen_ids: set[UserId] = set()
    seen_emails: set[str] = set()
    for item in value:
        if not isinstance(item, User):
            raise ValueError("directory repository user is invalid.")
        user = User(
            id=item.id,
            email=item.email,
            role=item.role,
            state=item.state,
            groups=item.groups,
            identity_synced_at=item.identity_synced_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
            version=item.version,
        )
        normalized_email = user.email.casefold()
        if user.id in seen_ids or normalized_email in seen_emails:
            raise ValueError("directory repository users must be unique.")
        seen_ids.add(user.id)
        seen_emails.add(normalized_email)
        validated.append(user)
    return tuple(sorted(validated, key=lambda user: str(user.id)))


def _directory_policy_decision(
    *,
    current: User,
    directory_user: DirectorySnapshotUser | None,
) -> str:
    if current.state is UserState.OFFBOARDED:
        return "directory_terminal_offboarded"
    if directory_user is None:
        return "directory_missing"
    if not directory_user.active:
        return "directory_inactive"
    if not directory_user.in_required_group:
        return "directory_group_missing"
    return "directory_active_member"


def _build_sync_result(
    *,
    snapshot_id: str,
    replayed: bool,
    projected_users: tuple[User, ...],
    updated_users: int,
    ignored_directory_users: int,
) -> DirectoryIdentitySyncResult:
    active_users = sum(1 for user in projected_users if user.state is UserState.ACTIVE)
    suspended_users = sum(
        1 for user in projected_users if user.state is UserState.SUSPENDED
    )
    offboarded_users = sum(
        1 for user in projected_users if user.state is UserState.OFFBOARDED
    )
    locked_user_ids = tuple(
        user.id
        for user in projected_users
        if user.state in {UserState.SUSPENDED, UserState.OFFBOARDED}
    )
    return DirectoryIdentitySyncResult(
        snapshot_id=snapshot_id,
        replayed=replayed,
        processed_users=len(projected_users),
        updated_users=updated_users,
        ignored_directory_users=ignored_directory_users,
        active_users=active_users,
        suspended_users=suspended_users,
        offboarded_users=offboarded_users,
        locked_user_ids=locked_user_ids,
    )
