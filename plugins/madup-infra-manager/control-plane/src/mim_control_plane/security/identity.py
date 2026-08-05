"""Ordered origin and Cloudflare Access identity authentication."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import jwt

from mim_control_plane.domain.models import UserId
from mim_control_plane.domain.states import UserRole
from mim_control_plane.security.origin import OriginHmacVerifier, OriginRequest


class TokenDenied(PermissionError):
    """Raised when an Access assertion is absent or invalid."""


@dataclass(frozen=True, slots=True)
class IdentityClaims:
    subject: str
    email: str
    issuer: str
    audience: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    user_id: UserId
    email: str
    role: UserRole


@dataclass(frozen=True, slots=True)
class AuthenticationRequest:
    origin: OriginRequest
    headers: tuple[tuple[str, str], ...]


class AccessTokenVerifier(Protocol):
    def verify(self, token: str) -> IdentityClaims: ...


class IdentityAuthorizer(Protocol):
    def authorize(self, claims: IdentityClaims) -> AuthenticatedPrincipal: ...


class _SigningKey(Protocol):
    @property
    def key(self) -> Any: ...


class JwksClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> _SigningKey: ...


class IdentityAuthenticator:
    """Enforce Worker proof before spending work on a user assertion."""

    def __init__(
        self,
        *,
        origin_verifier: OriginHmacVerifier,
        jwt_verifier: AccessTokenVerifier,
        identity_policy: IdentityAuthorizer,
    ) -> None:
        self._origin_verifier = origin_verifier
        self._jwt_verifier = jwt_verifier
        self._identity_policy = identity_policy

    def authenticate(self, request: AuthenticationRequest) -> AuthenticatedPrincipal:
        self._origin_verifier.verify(request.origin)
        token = _extract_access_assertion(request.headers)
        claims = self._jwt_verifier.verify(token)
        return self._identity_policy.authorize(claims)


class CloudflareJwtVerifier:
    """Validate an Access JWT with exact issuer/audience and cached JWKS."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_client: JwksClient | None = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        if not self._issuer or not self._audience:
            raise ValueError("Cloudflare issuer and audience are required.")
        self._jwks_client: JwksClient = jwks_client or cast(
            JwksClient,
            jwt.PyJWKClient(
                f"{self._issuer}/cdn-cgi/access/certs",
                cache_keys=True,
                max_cached_keys=16,
                cache_jwk_set=True,
                lifespan=300,
                timeout=5,
            ),
        )

    def verify(self, token: str) -> IdentityClaims:
        if not isinstance(token, str) or not token:
            raise TokenDenied("Access assertion was denied.")
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=0,
                options={
                    "require": ["sub", "email", "iss", "aud", "iat", "exp"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise TokenDenied("Access assertion was denied.") from None
        return _claims_from_payload(payload)


def _claims_from_payload(payload: object) -> IdentityClaims:
    if not isinstance(payload, dict):
        raise TokenDenied("Access assertion was denied.")
    try:
        subject = _required_text(payload["sub"])
        email = _required_text(payload["email"])
        issuer = _required_text(payload["iss"])
        audience = _audiences(payload["aud"])
        issued_at = _numeric_date(payload["iat"])
        expires_at = _numeric_date(payload["exp"])
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise TokenDenied("Access assertion was denied.") from None
    return IdentityClaims(
        subject=subject,
        email=email,
        issuer=issuer,
        audience=audience,
        issued_at=issued_at,
        expires_at=expires_at,
    )


_FORBIDDEN_CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
    }
)


def _extract_access_assertion(headers: tuple[tuple[str, str], ...]) -> str:
    token: str | None = None
    for raw_name, raw_value in headers:
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise TokenDenied("Access assertion was denied.")
        normalized_name = raw_name.strip().casefold()
        if normalized_name in _FORBIDDEN_CREDENTIAL_HEADERS:
            raise TokenDenied("Access assertion was denied.")
        if normalized_name != "cf-access-jwt-assertion":
            continue
        if token is not None:
            raise TokenDenied("Access assertion was denied.")
        candidate = raw_value.strip()
        if not candidate:
            raise TokenDenied("Access assertion was denied.")
        token = candidate
    if token is None:
        raise TokenDenied("Access assertion was denied.")
    return token


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("claim is not text")
    return value.strip()


def _audiences(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (_required_text(value),)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise ValueError("audience is invalid")
    audiences = tuple(_required_text(item) for item in value)
    if not audiences:
        raise ValueError("audience is empty")
    return audiences


def _numeric_date(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("numeric date is invalid")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError) as exc:
        raise ValueError("numeric date is invalid") from exc
