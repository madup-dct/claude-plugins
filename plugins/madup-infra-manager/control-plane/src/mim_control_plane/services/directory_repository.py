"""Shared validation for atomic Directory identity persistence."""

from __future__ import annotations

from collections.abc import Mapping, Set

from mim_control_plane.domain.directory_sync import (
    DirectoryUserReconciliation,
    build_directory_audit_event,
)
from mim_control_plane.domain.models import AuditEvent, AuditEventId, User, UserId
from mim_control_plane.domain.states import UserState, require_user_transition
from mim_control_plane.ports.directory import DirectoryIdentityRepositoryResult
from mim_control_plane.ports.store import (
    AlreadyExists,
    InvariantViolation,
    NotFound,
    VersionConflict,
)


def require_directory_snapshot_id(snapshot_id: object) -> str:
    if (
        not isinstance(snapshot_id, str)
        or not snapshot_id.strip()
        or snapshot_id != snapshot_id.strip()
    ):
        raise InvariantViolation("directory snapshot ID is invalid.")
    return snapshot_id


def require_directory_material_hash(material_hash: object) -> str:
    if (
        not isinstance(material_hash, str)
        or len(material_hash) != 64
        or any(character not in "0123456789abcdef" for character in material_hash)
    ):
        raise InvariantViolation("directory snapshot material is invalid.")
    return material_hash


def validate_directory_snapshot_write(
    *,
    snapshot_id: str,
    material_hash: str,
    reconciliations: tuple[DirectoryUserReconciliation, ...],
    audit_events: tuple[AuditEvent, ...],
    current_users: Mapping[UserId, User],
    existing_audit_event_ids: Set[AuditEventId],
    max_identities: int,
) -> DirectoryIdentityRepositoryResult:
    require_directory_snapshot_id(snapshot_id)
    require_directory_material_hash(material_hash)
    if not isinstance(reconciliations, tuple):
        raise InvariantViolation("directory reconciliations must be immutable.")
    if not isinstance(audit_events, tuple):
        raise InvariantViolation("directory audit events must be immutable.")
    if (
        not isinstance(max_identities, int)
        or isinstance(max_identities, bool)
        or max_identities < 1
    ):
        raise InvariantViolation("directory identity limit is invalid.")
    if len(reconciliations) > max_identities:
        raise InvariantViolation(
            "directory reconciliations exceed the pilot identity limit."
        )
    if len(audit_events) > max_identities:
        raise InvariantViolation(
            "directory audit events exceed the pilot identity limit."
        )

    user_ids: set[UserId] = set()
    locked_user_ids: list[UserId] = []
    ordered_current_users: list[User] = []
    for reconciliation in reconciliations:
        if not isinstance(reconciliation, DirectoryUserReconciliation):
            raise InvariantViolation("directory reconciliation is invalid.")
        if reconciliation.user.id in user_ids:
            raise InvariantViolation(
                "directory reconciliations must target unique users."
            )
        user_ids.add(reconciliation.user.id)
        current = current_users.get(reconciliation.user.id)
        if current is None:
            raise NotFound("user was not found.")
        if current.version != reconciliation.expected_version:
            raise VersionConflict("stale user version.")
        if reconciliation.user.version != reconciliation.expected_version + 1:
            raise VersionConflict("next user version must increment exactly once.")
        if current.created_at != reconciliation.user.created_at:
            raise InvariantViolation("user created_at is immutable.")
        if reconciliation.user.updated_at < current.updated_at:
            raise InvariantViolation("user updated_at cannot move backward.")
        if any(
            getattr(current, field_name) != getattr(reconciliation.user, field_name)
            for field_name in ("id", "email", "role", "created_at")
        ):
            raise InvariantViolation("user immutable policy material changed.")
        if reconciliation.user.state != current.state:
            require_user_transition(current.state, reconciliation.user.state)
        try:
            expected_user = current.reconcile_identity(
                target_state=reconciliation.user.state,
                required_group=reconciliation.required_group,
                in_required_group=(
                    reconciliation.required_group in reconciliation.user.groups
                ),
                synced_at=reconciliation.user.identity_synced_at,
            )
        except ValueError:
            raise InvariantViolation("directory reconciliation is invalid.") from None
        if expected_user != reconciliation.user:
            raise InvariantViolation(
                "directory reconciliation changed unauthorized user material."
            )
        ordered_current_users.append(current)
        if reconciliation.user.state in {
            UserState.SUSPENDED,
            UserState.OFFBOARDED,
        }:
            locked_user_ids.append(reconciliation.user.id)

    if len(audit_events) != len(reconciliations):
        raise InvariantViolation(
            "directory reconciliations require one exact audit event each."
        )
    audit_ids: set[AuditEventId] = set()
    for reconciliation, current, event in zip(
        reconciliations,
        ordered_current_users,
        audit_events,
        strict=True,
    ):
        if not isinstance(event, AuditEvent):
            raise InvariantViolation("directory audit event is invalid.")
        if event.id in audit_ids or event.id in existing_audit_event_ids:
            raise AlreadyExists("audit event already exists.")
        try:
            expected_event = build_directory_audit_event(
                snapshot_id=snapshot_id,
                required_group=reconciliation.required_group,
                policy_decision=reconciliation.policy_decision,
                user_before=current,
                user_after=reconciliation.user,
                synced_at=reconciliation.user.identity_synced_at,
            )
        except ValueError:
            raise InvariantViolation("directory audit event is invalid.") from None
        if event != expected_event:
            raise InvariantViolation(
                "directory audit event does not match reconciliation."
            )
        audit_ids.add(event.id)

    return DirectoryIdentityRepositoryResult(
        snapshot_id=snapshot_id,
        material_hash=material_hash,
        replayed=False,
        applied_user_ids=tuple(
            reconciliation.user.id for reconciliation in reconciliations
        ),
        locked_user_ids=tuple(locked_user_ids),
        audit_event_ids=tuple(event.id for event in audit_events),
    )


__all__ = [
    "require_directory_material_hash",
    "require_directory_snapshot_id",
    "validate_directory_snapshot_write",
]
