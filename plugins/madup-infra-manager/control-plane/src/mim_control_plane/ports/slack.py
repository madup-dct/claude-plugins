"""Private Slack ingress ports for replay claims and identity resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


class SlackReplayDetected(RuntimeError):
    """Raised when a signed Slack request fingerprint is reused."""


class SlackResolutionNotFound(LookupError):
    """Raised when no trusted Slack identity mapping exists."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be exact text.")
    return value.strip()


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware.")


@dataclass(frozen=True, slots=True)
class SlackReplayClaim:
    fingerprint: str
    claimed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.fingerprint, "fingerprint")
        _require_utc(self.claimed_at, "claimed_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.claimed_at:
            raise ValueError("expires_at must follow claimed_at.")

    def __repr__(self) -> str:
        return (
            "SlackReplayClaim("
            f"fingerprint={self.fingerprint[:12]!r}, "
            f"claimed_at={self.claimed_at.isoformat()!r}, "
            f"expires_at={self.expires_at.isoformat()!r})"
        )


@dataclass(frozen=True, slots=True)
class SlackIdentityResolution:
    install_id: str
    company_email: str

    def __post_init__(self) -> None:
        _require_text(self.install_id, "install_id")
        normalized_email = _require_text(self.company_email, "company_email")
        object.__setattr__(self, "company_email", normalized_email.casefold())

    def __repr__(self) -> str:
        return (
            "SlackIdentityResolution("
            f"install_id={self.install_id!r}, company_email='<redacted>')"
        )


class SlackReplayRegistry(Protocol):
    def claim_once(self, claim: SlackReplayClaim) -> None: ...


class SlackIdentityResolver(Protocol):
    def resolve_identity(
        self,
        *,
        team_id: str,
        enterprise_id: str | None,
        slack_user_id: str,
    ) -> SlackIdentityResolution: ...
