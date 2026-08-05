"""Bounded authoritative Google Directory snapshot records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256

from mim_control_plane.domain.models import AuditEvent, AuditEventId, User
from mim_control_plane.domain.states import UserState

DIRECTORY_READONLY_SCOPES = (
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
    "https://www.googleapis.com/auth/admin.directory.group.readonly",
    "https://www.googleapis.com/auth/admin.directory.group.member.readonly",
)
MAX_DIRECTORY_SNAPSHOT_USERS = 10000
_REDACTION_PREFIX = b"mim:directory-sync:redaction:v1\x00"
_DIRECTORY_POLICY_DECISIONS = frozenset(
    {
        "directory_active_member",
        "directory_group_missing",
        "directory_inactive",
        "directory_missing",
        "directory_terminal_offboarded",
    }
)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be UTC-aware.")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware.")
    return value


def _redact(value: str) -> str:
    digest = sha256(_REDACTION_PREFIX + value.encode("utf-8")).hexdigest()
    return digest[:12]


@dataclass(frozen=True, slots=True)
class DirectorySnapshotUser:
    directory_user_id: str
    email: str
    active: bool
    in_required_group: bool

    def __post_init__(self) -> None:
        directory_user_id = _require_text(self.directory_user_id, "directory_user_id")
        if directory_user_id != self.directory_user_id:
            raise ValueError(
                "directory_user_id must not contain surrounding whitespace."
            )
        normalized_email = _require_text(self.email, "email")
        if normalized_email != self.email:
            raise ValueError("email must not contain surrounding whitespace.")
        normalized_email = normalized_email.casefold()
        object.__setattr__(self, "email", normalized_email)
        if type(self.active) is not bool:
            raise ValueError("active must be an exact bool.")
        if type(self.in_required_group) is not bool:
            raise ValueError("in_required_group must be an exact bool.")

    def __repr__(self) -> str:
        return (
            "DirectorySnapshotUser("
            f"directory_user_id={_redact(self.directory_user_id)!r}, "
            f"email={_redact(self.email)!r}, active={self.active!r}, "
            f"in_required_group={self.in_required_group!r})"
        )


@dataclass(frozen=True, slots=True)
class DirectoryAuthoritativeSnapshot:
    snapshot_id: str
    required_group: str
    started_at: datetime
    completed_at: datetime
    users: tuple[DirectorySnapshotUser, ...]

    def __post_init__(self) -> None:
        _require_text(self.snapshot_id, "snapshot_id")
        normalized_group = _require_text(self.required_group, "required_group")
        if normalized_group != self.required_group:
            raise ValueError("required_group must not contain surrounding whitespace.")
        started_at = _require_utc(self.started_at, "started_at")
        completed_at = _require_utc(self.completed_at, "completed_at")
        if completed_at < started_at:
            raise ValueError("completed_at must not precede started_at.")
        if not isinstance(self.users, tuple):
            raise ValueError("users must be an immutable tuple.")
        if len(self.users) > MAX_DIRECTORY_SNAPSHOT_USERS:
            raise ValueError("users exceeds the bounded directory snapshot size.")
        seen_ids: set[str] = set()
        seen_emails: set[str] = set()
        for user in self.users:
            if not isinstance(user, DirectorySnapshotUser):
                raise ValueError("users must contain directory snapshot users.")
            directory_user_id = user.directory_user_id.casefold()
            if directory_user_id in seen_ids:
                raise ValueError("directory snapshot user IDs must be unique.")
            seen_ids.add(directory_user_id)
            if user.email in seen_emails:
                raise ValueError("directory snapshot emails must be unique.")
            seen_emails.add(user.email)

    def __repr__(self) -> str:
        return (
            "DirectoryAuthoritativeSnapshot("
            f"snapshot_id={self.snapshot_id!r}, "
            f"required_group={self.required_group!r}, "
            f"started_at={self.started_at.isoformat()!r}, "
            f"completed_at={self.completed_at.isoformat()!r}, "
            f"users={len(self.users)!r})"
        )


@dataclass(frozen=True, slots=True)
class DirectoryUserReconciliation:
    user: User
    expected_version: int
    required_group: str
    policy_decision: str

    def __post_init__(self) -> None:
        if not isinstance(self.user, User):
            raise ValueError("user must be a persisted user record.")
        if (
            not isinstance(self.expected_version, int)
            or isinstance(self.expected_version, bool)
            or self.expected_version < 1
        ):
            raise ValueError("expected_version must be a positive integer.")
        if self.user.version != self.expected_version + 1:
            raise ValueError("user version must increment exactly once.")
        normalized_group = _require_text(self.required_group, "required_group")
        if normalized_group != self.required_group:
            raise ValueError("required_group must not contain surrounding whitespace.")
        if self.policy_decision not in _DIRECTORY_POLICY_DECISIONS:
            raise ValueError("policy_decision is invalid.")

    def __repr__(self) -> str:
        return (
            "DirectoryUserReconciliation("
            f"user={_redact(str(self.user.id))!r}, "
            f"expected_version={self.expected_version!r}, "
            f"required_group={_redact(self.required_group)!r}, "
            f"policy_decision={self.policy_decision!r})"
        )


def validate_directory_snapshot(
    snapshot: object,
) -> DirectoryAuthoritativeSnapshot:
    if not isinstance(snapshot, DirectoryAuthoritativeSnapshot):
        raise ValueError("directory snapshot is invalid.")
    return DirectoryAuthoritativeSnapshot(
        snapshot_id=snapshot.snapshot_id,
        required_group=snapshot.required_group,
        started_at=snapshot.started_at,
        completed_at=snapshot.completed_at,
        users=snapshot.users,
    )


def projected_target_state(
    *,
    current_state: UserState,
    present: bool,
    active: bool,
    in_required_group: bool,
) -> UserState:
    if current_state is UserState.OFFBOARDED:
        return UserState.OFFBOARDED
    if not present:
        return UserState.OFFBOARDED
    if active and in_required_group:
        return UserState.ACTIVE
    return UserState.SUSPENDED


def build_directory_audit_event(
    *,
    snapshot_id: str,
    required_group: str,
    policy_decision: str,
    user_before: User,
    user_after: User,
    synced_at: datetime,
) -> AuditEvent:
    _require_text(snapshot_id, "snapshot_id")
    normalized_group = _require_text(required_group, "required_group")
    if normalized_group != required_group:
        raise ValueError("required_group must not contain surrounding whitespace.")
    if policy_decision not in _DIRECTORY_POLICY_DECISIONS:
        raise ValueError("policy_decision is invalid.")
    _require_utc(synced_at, "synced_at")
    correlation = sha256(
        f"dirsync-correlation:{snapshot_id}".encode("utf-8")
    ).hexdigest()[:24]
    audit_id = AuditEventId(
        sha256(
            (
                f"dirsync-audit:{snapshot_id}:{user_before.id}:"
                f"{user_before.version}:{user_after.version}:{user_after.state.value}:"
                f"{policy_decision}"
            ).encode("utf-8")
        ).hexdigest()[:32]
    )
    required_group_before = "1" if required_group in user_before.groups else "0"
    required_group_after = "1" if required_group in user_after.groups else "0"
    return AuditEvent(
        id=audit_id,
        actor_id=None,
        action="directory_identity_sync",
        target_ref=f"user:{_redact(str(user_before.id))}",
        policy_decision=policy_decision,
        before_ref=f"{user_before.state.value}:{required_group_before}:v{user_before.version}",
        after_ref=f"{user_after.state.value}:{required_group_after}:v{user_after.version}",
        correlation_id=correlation,
        outcome="locked"
        if user_after.state in {UserState.SUSPENDED, UserState.OFFBOARDED}
        else "active",
        occurred_at=synced_at,
    )
