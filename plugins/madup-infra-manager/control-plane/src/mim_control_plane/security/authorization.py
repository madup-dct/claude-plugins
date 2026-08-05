"""Tenant, lifecycle, role, and ownership authorization policy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from mim_control_plane.domain.models import UserId
from mim_control_plane.domain.states import UserRole, UserState
from mim_control_plane.ports.store import NotFound, Store
from mim_control_plane.security.identity import (
    AuthenticatedPrincipal,
    IdentityClaims,
)


class AccessDenied(PermissionError):
    """Raised when a valid assertion is outside local MIM policy."""


class IdentityPolicy:
    """Bind Access claims to fresh, active, centrally synchronized users."""

    def __init__(
        self,
        *,
        store: Store,
        issuer: str,
        audience: str,
        company_domain: str,
        required_group: str,
        max_staleness: timedelta,
        clock: Callable[[], datetime],
    ) -> None:
        if max_staleness <= timedelta(0):
            raise ValueError("Identity staleness window must be positive.")
        self._store = store
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._company_domain = company_domain.casefold()
        self._required_group = required_group
        self._max_staleness = max_staleness
        self._clock = clock

    def authorize(self, claims: IdentityClaims) -> AuthenticatedPrincipal:
        now = self._clock()
        _require_utc(now)
        _require_utc(claims.issued_at)
        _require_utc(claims.expires_at)
        if claims.issuer != self._issuer or claims.audience != (self._audience,):
            raise AccessDenied("Identity is not authorized for MIM.")
        if claims.issued_at > now or claims.expires_at <= now:
            raise AccessDenied("Identity is not authorized for MIM.")
        if claims.expires_at <= claims.issued_at:
            raise AccessDenied("Identity is not authorized for MIM.")

        return self.authorize_resolved_user(
            user_id=UserId(claims.subject),
            email=claims.email,
        )

    def authorize_resolved_user(
        self,
        *,
        user_id: UserId,
        email: str,
    ) -> AuthenticatedPrincipal:
        now = self._clock()
        _require_utc(now)
        normalized_email = email.strip().casefold()
        local, separator, domain = normalized_email.rpartition("@")
        if not separator or not local or domain != self._company_domain:
            raise AccessDenied("Identity is not authorized for MIM.")

        try:
            record = self._store.get_user(user_id)
        except NotFound:
            raise AccessDenied("Identity is not authorized for MIM.") from None
        if record.email.casefold() != normalized_email:
            raise AccessDenied("Identity is not authorized for MIM.")
        if record.state is not UserState.ACTIVE:
            raise AccessDenied("Identity is not authorized for MIM.")
        if self._required_group not in record.groups:
            raise AccessDenied("Identity is not authorized for MIM.")

        identity_age = now - record.identity_synced_at
        if identity_age < timedelta(0) or identity_age > self._max_staleness:
            raise AccessDenied("Identity is not authorized for MIM.")
        return AuthenticatedPrincipal(
            user_id=record.id,
            email=record.email,
            role=record.role,
        )


def require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.role is not UserRole.ADMIN:
        raise AccessDenied("Administrator role is required.")


def require_owner_or_admin(
    principal: AuthenticatedPrincipal,
    owner_id: UserId,
) -> None:
    if principal.role is not UserRole.ADMIN and principal.user_id != owner_id:
        raise AccessDenied("Resource is outside the authorized MIM scope.")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise AccessDenied("Identity is not authorized for MIM.")
