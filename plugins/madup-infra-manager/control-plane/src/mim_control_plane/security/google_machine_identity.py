"""Fail-closed Google OIDC verification for private machine-only routes."""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token


class MachineRequestDenied(PermissionError):
    """Raised when a private machine request falls outside the reviewed boundary."""


@dataclass(frozen=True, slots=True)
class GoogleMachinePrincipal:
    subject: str
    email: str
    audience: str
    issued_at: datetime
    expires_at: datetime


class TokenVerifier(Protocol):
    def __call__(
        self,
        token: str,
        request: object,
        audience: str,
    ) -> Mapping[str, object]: ...


_TEXT_PATTERN = re.compile(r"^[ -~]{1,512}$")
_FORBIDDEN_HEADER_NAMES = frozenset(
    {
        "cf-access-jwt-assertion",
        "cookie",
        "origin",
        "proxy-authorization",
        "referer",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-fetch-user",
        "set-cookie",
        "x-hub-signature-256",
    }
)
_FORBIDDEN_HEADER_PREFIXES = (
    "sec-ch-ua",
    "x-github-",
    "x-mim-origin-",
)


@dataclass(frozen=True, slots=True)
class GoogleOidcMachineAuthenticator:
    audience: str
    service_account_email: str
    token_verifier: TokenVerifier | None = None
    transport_request: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "audience", _require_text(self.audience))
        object.__setattr__(
            self,
            "service_account_email",
            _require_text(self.service_account_email),
        )
        if self.token_verifier is None:
            object.__setattr__(self, "token_verifier", _verify_google_token)
        if self.transport_request is None:
            object.__setattr__(self, "transport_request", GoogleAuthRequest())

    def authenticate(
        self,
        headers: tuple[tuple[str, str], ...],
        *,
        expected_service_account_email: str | None = None,
    ) -> GoogleMachinePrincipal:
        token = _extract_bearer_token(headers)
        expected_email = (
            self.service_account_email
            if expected_service_account_email is None
            else _require_text(expected_service_account_email)
        )
        try:
            payload = self.token_verifier(  # type: ignore[misc]
                token,
                self.transport_request,
                self.audience,
            )
        except Exception:
            raise MachineRequestDenied("Machine request was denied.") from None
        return _principal_from_payload(
            payload=payload,
            expected_audience=self.audience,
            expected_service_account_email=expected_email,
        )


def _verify_google_token(
    token: str,
    request: object,
    audience: str,
) -> Mapping[str, object]:
    if not isinstance(request, GoogleAuthRequest):
        raise TypeError("transport request is invalid")
    return id_token.verify_oauth2_token(
        token,
        request,
        audience,
        clock_skew_in_seconds=0,
    )


def _extract_bearer_token(headers: tuple[tuple[str, str], ...]) -> str:
    token: str | None = None
    for raw_name, raw_value in headers:
        name = _normalized_header_name(raw_name)
        value = _require_text(raw_value)
        if name in _FORBIDDEN_HEADER_NAMES or any(
            name.startswith(prefix) for prefix in _FORBIDDEN_HEADER_PREFIXES
        ):
            raise MachineRequestDenied("Machine request was denied.")
        if name != "authorization":
            continue
        if token is not None:
            raise MachineRequestDenied("Machine request was denied.")
        if not value.startswith("Bearer "):
            raise MachineRequestDenied("Machine request was denied.")
        candidate = value.removeprefix("Bearer ")
        if not candidate or candidate.strip() != candidate:
            raise MachineRequestDenied("Machine request was denied.")
        token = candidate
    if token is None:
        raise MachineRequestDenied("Machine request was denied.")
    return token


def _principal_from_payload(
    *,
    payload: Mapping[str, object],
    expected_audience: str,
    expected_service_account_email: str,
) -> GoogleMachinePrincipal:
    subject = _require_text(payload.get("sub"))
    email = _require_text(payload.get("email"))
    if not hmac.compare_digest(email, expected_service_account_email):
        raise MachineRequestDenied("Machine request was denied.")
    if payload.get("email_verified") is not True:
        raise MachineRequestDenied("Machine request was denied.")
    if not _audience_matches(payload.get("aud"), expected_audience):
        raise MachineRequestDenied("Machine request was denied.")
    issued_at = _numeric_date(payload.get("iat"))
    expires_at = _numeric_date(payload.get("exp"))
    return GoogleMachinePrincipal(
        subject=subject,
        email=email,
        audience=expected_audience,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _audience_matches(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return hmac.compare_digest(value, expected)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return False
    audiences = tuple(_require_text(item) for item in value)
    return len(audiences) == 1 and hmac.compare_digest(audiences[0], expected)


def _numeric_date(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MachineRequestDenied("Machine request was denied.")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise MachineRequestDenied("Machine request was denied.") from None


def _normalized_header_name(value: object) -> str:
    return _require_text(value).casefold()


def _require_text(value: object) -> str:
    if type(value) is not str:
        raise MachineRequestDenied("Machine request was denied.")
    candidate = value.strip()
    if _TEXT_PATTERN.fullmatch(candidate) is None:
        raise MachineRequestDenied("Machine request was denied.")
    return candidate
