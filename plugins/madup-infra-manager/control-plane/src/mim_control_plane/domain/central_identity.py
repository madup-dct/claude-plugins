"""Bounded central identity records for browser and Slack entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from mim_control_plane.domain.models import UserId


class SlackSharedInstallState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class SlackIdentityLinkState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ActionName(StrEnum):
    VIEW_DASHBOARD = "view_dashboard"
    VIEW_USAGE = "view_usage"
    DEPLOY_WORKLOAD = "deploy_workload"
    MANAGE_SCHEDULE = "manage_schedule"
    ADMIN_USAGE_OVERVIEW = "admin_usage_overview"


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_text_tuple(values: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name} must be an immutable tuple.")
    normalized = tuple(_require_text(value, field_name) for value in values)
    if not normalized:
        raise ValueError(f"{field_name} must not be empty.")
    return normalized


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware.")


def _require_version(version: int) -> None:
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("version must be a positive integer.")


def _validate_revocation(
    *,
    state: SlackSharedInstallState | SlackIdentityLinkState,
    created_at: datetime,
    revoked_at: datetime | None,
    updated_at: datetime,
) -> None:
    if revoked_at is not None:
        _require_utc(revoked_at, "revoked_at")
        if revoked_at < created_at:
            raise ValueError("revoked_at must not precede created_at.")
        if revoked_at > updated_at:
            raise ValueError("revoked_at must not follow updated_at.")
    if state in (
        SlackSharedInstallState.REVOKED,
        SlackIdentityLinkState.REVOKED,
    ):
        if revoked_at is None:
            raise ValueError("revoked records require revoked_at.")
    elif revoked_at is not None:
        raise ValueError("active records must not set revoked_at.")


@dataclass(frozen=True, slots=True)
class SlackSharedInstall:
    install_id: str
    team_id: str
    enterprise_id: str | None
    granted_scopes: tuple[str, ...]
    installer_mim_user_id: UserId
    installer_email: str
    created_at: datetime
    updated_at: datetime
    state: SlackSharedInstallState
    revoked_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.state, SlackSharedInstallState):
            raise ValueError("state must be a SlackSharedInstallState.")
        _require_text(self.install_id, "install_id")
        _require_text(self.team_id, "team_id")
        if self.enterprise_id is not None:
            _require_text(self.enterprise_id, "enterprise_id")
        _require_text_tuple(self.granted_scopes, "granted_scopes")
        _require_text(self.installer_mim_user_id, "installer_mim_user_id")
        normalized_email = _require_text(self.installer_email, "installer_email")
        object.__setattr__(self, "installer_email", normalized_email.casefold())
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at.")
        _validate_revocation(
            state=self.state,
            created_at=self.created_at,
            revoked_at=self.revoked_at,
            updated_at=self.updated_at,
        )
        _require_version(self.version)

    def __repr__(self) -> str:
        return (
            "SlackSharedInstall("
            f"install_id={self.install_id!r}, team_id={self.team_id!r}, "
            f"state={self.state.value!r}, version={self.version})"
        )


@dataclass(frozen=True, slots=True)
class SlackIdentityLink:
    install_id: str
    team_id: str
    slack_user_id: str
    mim_user_id: UserId
    company_email: str
    verified_at: datetime
    created_at: datetime
    updated_at: datetime
    state: SlackIdentityLinkState
    revoked_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.state, SlackIdentityLinkState):
            raise ValueError("state must be a SlackIdentityLinkState.")
        _require_text(self.install_id, "install_id")
        _require_text(self.team_id, "team_id")
        _require_text(self.slack_user_id, "slack_user_id")
        _require_text(self.mim_user_id, "mim_user_id")
        normalized_email = _require_text(self.company_email, "company_email").casefold()
        object.__setattr__(self, "company_email", normalized_email)
        _require_utc(self.verified_at, "verified_at")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at.")
        if self.verified_at < self.created_at:
            raise ValueError("verified_at must not precede created_at.")
        _validate_revocation(
            state=self.state,
            created_at=self.created_at,
            revoked_at=self.revoked_at,
            updated_at=self.updated_at,
        )
        _require_version(self.version)

    def __repr__(self) -> str:
        return (
            "SlackIdentityLink("
            f"install_id={self.install_id!r}, team_id={self.team_id!r}, "
            f"mim_user_id={self.mim_user_id!r}, state={self.state.value!r}, "
            f"version={self.version})"
        )


@dataclass(frozen=True, slots=True)
class VerifiedSlackActor:
    install_id: str
    team_id: str
    enterprise_id: str | None
    slack_user_id: str
    company_email: str
    verified_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.install_id, "install_id")
        _require_text(self.team_id, "team_id")
        if self.enterprise_id is not None:
            _require_text(self.enterprise_id, "enterprise_id")
        _require_text(self.slack_user_id, "slack_user_id")
        normalized_email = _require_text(self.company_email, "company_email").casefold()
        object.__setattr__(self, "company_email", normalized_email)
        _require_utc(self.verified_at, "verified_at")

    def __repr__(self) -> str:
        return (
            "VerifiedSlackActor("
            f"install_id={self.install_id!r}, team_id={self.team_id!r}, "
            "enterprise_id="
            f"{self.enterprise_id!r}, "
            f"verified_at={self.verified_at.isoformat()!r})"
        )


@dataclass(frozen=True, slots=True)
class ActionIntent:
    action: ActionName
    resource_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.action, ActionName):
            raise ValueError("action must be an ActionName.")
        _require_text(self.resource_id, "resource_id")
