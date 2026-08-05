"""Metadata-only domain records for centrally managed Slack OAuth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from string import hexdigits
from urllib.parse import urlparse

from mim_control_plane.domain.models import UserId

_STATE_ID_LENGTH = 24
_STATE_HASH_LENGTH = 64
_INSTALL_ID_LENGTH = 32


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be exact text.")
    if value != value.strip():
        raise ValueError(f"{field_name} must be exact text.")
    return value


def _require_hex(value: object, field_name: str, *, expected_length: int) -> str:
    normalized = _require_text(value, field_name)
    if len(normalized) != expected_length:
        raise ValueError(f"{field_name} must be {expected_length} hex characters.")
    if any(character not in hexdigits for character in normalized):
        raise ValueError(f"{field_name} must be lowercase hexadecimal.")
    if normalized.lower() != normalized:
        raise ValueError(f"{field_name} must be lowercase hexadecimal.")
    return normalized


def _require_text_tuple(values: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name} must be an immutable tuple.")
    normalized = tuple(_require_text(value, field_name) for value in values)
    if not normalized:
        raise ValueError(f"{field_name} must not be empty.")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates.")
    return normalized


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware.")


def _require_https_url(value: object, field_name: str) -> str:
    normalized = _require_text(value, field_name)
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment or parsed.query:
        raise ValueError(f"{field_name} must be a fixed HTTPS URL.")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not embed credentials.")
    return normalized


class SlackOAuthInstallState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class SlackOAuthIdentityLinkState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


def _require_version(version: int) -> None:
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("version must be a positive integer.")


def _validate_revocation(
    *,
    state: SlackOAuthInstallState | SlackOAuthIdentityLinkState,
    created_at: datetime,
    updated_at: datetime,
    revoked_at: datetime | None,
) -> None:
    if revoked_at is not None:
        _require_utc(revoked_at, "revoked_at")
        if revoked_at < created_at:
            raise ValueError("revoked_at must not precede created_at.")
        if revoked_at > updated_at:
            raise ValueError("revoked_at must not follow updated_at.")
    if state in (SlackOAuthInstallState.REVOKED, SlackOAuthIdentityLinkState.REVOKED):
        if revoked_at is None:
            raise ValueError("revoked records require revoked_at.")
    elif revoked_at is not None:
        raise ValueError("active records must not set revoked_at.")


@dataclass(frozen=True, slots=True)
class SlackOAuthTenant:
    team_id: str
    enterprise_id: str | None

    def __post_init__(self) -> None:
        _require_text(self.team_id, "team_id")
        if self.enterprise_id is not None:
            _require_text(self.enterprise_id, "enterprise_id")


@dataclass(frozen=True, slots=True)
class SlackOAuthPendingState:
    state_id: str
    state_hash: str
    installer_mim_user_id: UserId
    installer_email: str
    required_scopes: tuple[str, ...]
    redirect_uri: str
    install_tenant: SlackOAuthTenant
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "state_id",
            _require_hex(self.state_id, "state_id", expected_length=_STATE_ID_LENGTH),
        )
        object.__setattr__(
            self,
            "state_hash",
            _require_hex(
                self.state_hash,
                "state_hash",
                expected_length=_STATE_HASH_LENGTH,
            ),
        )
        _require_text(self.installer_mim_user_id, "installer_mim_user_id")
        object.__setattr__(
            self,
            "installer_email",
            _require_text(self.installer_email, "installer_email").casefold(),
        )
        object.__setattr__(
            self,
            "required_scopes",
            _require_text_tuple(self.required_scopes, "required_scopes"),
        )
        object.__setattr__(
            self,
            "redirect_uri",
            _require_https_url(self.redirect_uri, "redirect_uri"),
        )
        if type(self.install_tenant) is not SlackOAuthTenant:
            raise ValueError("install_tenant must be a SlackOAuthTenant.")
        _require_utc(self.issued_at, "issued_at")
        _require_utc(self.expires_at, "expires_at")
        if self.consumed_at is not None:
            _require_utc(self.consumed_at, "consumed_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must follow issued_at.")
        if self.consumed_at is not None and self.consumed_at < self.issued_at:
            raise ValueError("consumed_at must not precede issued_at.")
        if self.consumed_at is not None and self.consumed_at >= self.expires_at:
            raise ValueError("consumed_at must precede expires_at.")
        _require_version(self.version)

    def __repr__(self) -> str:
        return (
            "SlackOAuthPendingState("
            f"state_id={self.state_id!r}, "
            f"installer_mim_user_id={self.installer_mim_user_id!r}, "
            f"install_tenant={self.install_tenant!r}, "
            f"expires_at={self.expires_at.isoformat()!r}, "
            f"consumed={self.consumed_at is not None!r}, "
            f"version={self.version})"
        )


@dataclass(frozen=True, slots=True)
class SlackOAuthGrant:
    app_id: str
    team_id: str
    enterprise_id: str | None
    is_enterprise_install: bool
    granted_scopes: tuple[str, ...]
    secret_ref: str

    def __post_init__(self) -> None:
        _require_text(self.app_id, "app_id")
        _require_text(self.team_id, "team_id")
        if self.enterprise_id is not None:
            _require_text(self.enterprise_id, "enterprise_id")
        if type(self.is_enterprise_install) is not bool:
            raise ValueError("is_enterprise_install must be an exact bool.")
        if self.is_enterprise_install and self.enterprise_id is None:
            raise ValueError("enterprise installs require enterprise_id.")
        object.__setattr__(
            self,
            "granted_scopes",
            _require_text_tuple(self.granted_scopes, "granted_scopes"),
        )
        _require_text(self.secret_ref, "secret_ref")

    def __repr__(self) -> str:
        return (
            "SlackOAuthGrant("
            f"app_id={self.app_id!r}, team_id={self.team_id!r}, "
            f"enterprise_id={self.enterprise_id!r}, "
            f"is_enterprise_install={self.is_enterprise_install!r}, "
            f"granted_scopes={self.granted_scopes!r}, "
            "secret_ref='<redacted>')"
        )


@dataclass(frozen=True, slots=True)
class SlackOAuthSharedInstall:
    install_id: str
    app_id: str
    team_id: str
    enterprise_id: str | None
    is_enterprise_install: bool
    granted_scopes: tuple[str, ...]
    secret_ref: str
    installer_mim_user_id: UserId
    installer_email: str
    created_at: datetime
    updated_at: datetime
    state: SlackOAuthInstallState
    revoked_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "install_id",
            _require_hex(
                self.install_id,
                "install_id",
                expected_length=_INSTALL_ID_LENGTH,
            ),
        )
        _require_text(self.app_id, "app_id")
        _require_text(self.team_id, "team_id")
        if self.enterprise_id is not None:
            _require_text(self.enterprise_id, "enterprise_id")
        if type(self.is_enterprise_install) is not bool:
            raise ValueError("is_enterprise_install must be an exact bool.")
        if self.is_enterprise_install and self.enterprise_id is None:
            raise ValueError("enterprise installs require enterprise_id.")
        object.__setattr__(
            self,
            "granted_scopes",
            _require_text_tuple(self.granted_scopes, "granted_scopes"),
        )
        _require_text(self.secret_ref, "secret_ref")
        _require_text(self.installer_mim_user_id, "installer_mim_user_id")
        object.__setattr__(
            self,
            "installer_email",
            _require_text(self.installer_email, "installer_email").casefold(),
        )
        if not isinstance(self.state, SlackOAuthInstallState):
            raise ValueError("state must be a SlackOAuthInstallState.")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at.")
        _validate_revocation(
            state=self.state,
            created_at=self.created_at,
            updated_at=self.updated_at,
            revoked_at=self.revoked_at,
        )
        _require_version(self.version)

    def __repr__(self) -> str:
        return (
            "SlackOAuthSharedInstall("
            f"install_id={self.install_id!r}, app_id={self.app_id!r}, "
            f"team_id={self.team_id!r}, enterprise_id={self.enterprise_id!r}, "
            f"state={self.state.value!r}, version={self.version})"
        )


@dataclass(frozen=True, slots=True)
class SlackOAuthIdentityLink:
    install_id: str
    team_id: str
    slack_user_id: str
    mim_user_id: UserId
    company_email: str
    created_at: datetime
    updated_at: datetime
    state: SlackOAuthIdentityLinkState
    revoked_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "install_id",
            _require_hex(
                self.install_id,
                "install_id",
                expected_length=_INSTALL_ID_LENGTH,
            ),
        )
        _require_text(self.team_id, "team_id")
        _require_text(self.slack_user_id, "slack_user_id")
        _require_text(self.mim_user_id, "mim_user_id")
        object.__setattr__(
            self,
            "company_email",
            _require_text(self.company_email, "company_email").casefold(),
        )
        if not isinstance(self.state, SlackOAuthIdentityLinkState):
            raise ValueError("state must be a SlackOAuthIdentityLinkState.")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at.")
        _validate_revocation(
            state=self.state,
            created_at=self.created_at,
            updated_at=self.updated_at,
            revoked_at=self.revoked_at,
        )
        _require_version(self.version)

    def __repr__(self) -> str:
        return (
            "SlackOAuthIdentityLink("
            f"install_id={self.install_id!r}, team_id={self.team_id!r}, "
            f"mim_user_id={self.mim_user_id!r}, state={self.state.value!r}, "
            f"version={self.version})"
        )
