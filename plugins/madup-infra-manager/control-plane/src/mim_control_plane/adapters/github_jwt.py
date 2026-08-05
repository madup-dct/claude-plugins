"""GitHub App JWT provider backed by an exact private key."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from mim_control_plane.adapters.github import GitHubSourceIntegrityError


@dataclass(frozen=True, slots=True)
class GitHubAppPrivateKeyJwtProvider:
    app_id: str
    private_key_pem: str

    def __post_init__(self) -> None:
        if type(self.app_id) is not str or not self.app_id.isdigit():
            raise ValueError("GitHub App ID must be a numeric string.")
        if type(self.private_key_pem) is not str or "BEGIN" not in self.private_key_pem:
            raise ValueError("GitHub App private key must be PEM text.")

    def get_app_jwt(self, *, now: datetime) -> str:
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise GitHubSourceIntegrityError(
                "GitHub installation token minting failed."
            )
        issued_at = int(now.timestamp()) - 30
        expires_at = issued_at + int(timedelta(minutes=9).total_seconds())
        try:
            token = jwt.encode(
                {
                    "iss": self.app_id,
                    "iat": issued_at,
                    "exp": expires_at,
                },
                self.private_key_pem,
                algorithm="RS256",
            )
        except Exception:
            raise GitHubSourceIntegrityError(
                "GitHub installation token minting failed."
            ) from None
        if type(token) is not str or not token:
            raise GitHubSourceIntegrityError(
                "GitHub installation token minting failed."
            )
        return token
