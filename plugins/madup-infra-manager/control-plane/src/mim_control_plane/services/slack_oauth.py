"""Centrally administered Slack OAuth state and metadata installation flow."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from urllib.parse import urlencode, urlparse

from mim_control_plane.domain.slack_oauth import (
    SlackOAuthGrant,
    SlackOAuthIdentityLink,
    SlackOAuthInstallState,
    SlackOAuthPendingState,
    SlackOAuthSharedInstall,
    SlackOAuthTenant,
)
from mim_control_plane.ports.slack_oauth import (
    SlackOAuthCredentialVault,
    SlackOAuthCredentialVaultError,
    SlackOAuthInstallRepository,
    SlackOAuthInstallRepositoryError,
    SlackOAuthProvider,
    SlackOAuthProviderError,
    SlackOAuthStateOwnerMismatch,
    SlackOAuthStateRejected,
    SlackOAuthStateRepository,
    SlackOAuthStateStoreError,
)
from mim_control_plane.security.authorization import (
    AccessDenied,
    IdentityPolicy,
    require_admin,
)
from mim_control_plane.security.identity import AuthenticatedPrincipal

_DENIED_MESSAGE = "Slack installation was denied."
_FLOW_MESSAGE = "Slack installation could not be completed."
_MAX_STATE_TTL = timedelta(minutes=10)
_MIN_STATE_LENGTH = 43
_STATE_HASH_PREFIX = b"mim:slack-oauth:state-hash:v2\x00"
_STATE_ID_PREFIX = b"mim:slack-oauth:state-id:v2\x00"
_INSTALL_ID_PREFIX = b"mim:slack-oauth:install-id:v1\x00"
_SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"


class SlackOAuthDenied(PermissionError):
    """Raised when the current operator is not allowed to administer Slack."""


class SlackOAuthFlowError(RuntimeError):
    """Raised when the bounded Slack OAuth flow cannot be completed safely."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be exact text.")
    if value != value.strip():
        raise ValueError(f"{field_name} must be exact text.")
    return value


def _require_https_url(value: object, field_name: str) -> str:
    normalized = _require_text(value, field_name)
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must be an HTTPS URL.")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not embed credentials.")
    return normalized


def _require_scope_tuple(values: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name} must be an immutable tuple.")
    normalized = tuple(_require_text(value, field_name) for value in values)
    if not normalized:
        raise ValueError(f"{field_name} must not be empty.")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates.")
    return normalized


def _require_utc(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("clock must return UTC-aware datetimes.")


def _require_authorization_result_url(value: object) -> str:
    normalized = _require_text(value, "authorization_url")
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "slack.com"
        or parsed.path != "/oauth/v2/authorize"
        or not parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("authorization_url must be the Slack OAuth redirect URL.")
    return normalized


def _state_hash(raw_state: str) -> str:
    return sha256(_STATE_HASH_PREFIX + raw_state.encode("utf-8")).hexdigest()


def _state_id(raw_state: str) -> str:
    return sha256(_STATE_ID_PREFIX + raw_state.encode("utf-8")).hexdigest()[:24]


def _install_id(
    *,
    app_id: str,
    team_id: str,
    enterprise_id: str | None,
    is_enterprise_install: bool,
) -> str:
    digest = sha256()
    digest.update(_INSTALL_ID_PREFIX)
    for value in (
        app_id,
        team_id,
        enterprise_id or "",
        "1" if is_enterprise_install else "0",
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


def _default_state_factory() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class SlackOAuthStartRequest:
    principal: AuthenticatedPrincipal

    def __post_init__(self) -> None:
        if type(self.principal) is not AuthenticatedPrincipal:
            raise ValueError("principal must be an AuthenticatedPrincipal.")

    def __repr__(self) -> str:
        return f"SlackOAuthStartRequest(principal={self.principal.user_id!r})"


@dataclass(frozen=True, slots=True)
class SlackOAuthStartResult:
    authorization_url: str
    state_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_authorization_result_url(self.authorization_url)
        _require_text(self.state_id, "state_id")
        _require_utc(self.expires_at)

    def __repr__(self) -> str:
        return (
            "SlackOAuthStartResult("
            "authorization_url='<redacted>', "
            f"state_id={self.state_id!r}, "
            f"expires_at={self.expires_at.isoformat()!r})"
        )


@dataclass(frozen=True, slots=True)
class SlackOAuthCallbackRequest:
    principal: AuthenticatedPrincipal
    state: str
    code: str

    def __post_init__(self) -> None:
        if type(self.principal) is not AuthenticatedPrincipal:
            raise ValueError("principal must be an AuthenticatedPrincipal.")
        _require_text(self.state, "state")
        _require_text(self.code, "code")

    def __repr__(self) -> str:
        return (
            "SlackOAuthCallbackRequest("
            f"principal={self.principal.user_id!r}, "
            "state='<redacted>', code='<redacted>')"
        )


@dataclass(frozen=True, slots=True)
class SlackOAuthCompletionResult:
    install_id: str
    app_id: str
    team_id: str
    enterprise_id: str | None
    is_enterprise_install: bool
    granted_scopes: tuple[str, ...]
    secret_ref: str

    def __post_init__(self) -> None:
        _require_text(self.install_id, "install_id")
        _require_text(self.app_id, "app_id")
        _require_text(self.team_id, "team_id")
        if self.enterprise_id is not None:
            _require_text(self.enterprise_id, "enterprise_id")
        if type(self.is_enterprise_install) is not bool:
            raise ValueError("is_enterprise_install must be an exact bool.")
        object.__setattr__(
            self,
            "granted_scopes",
            _require_scope_tuple(self.granted_scopes, "granted_scopes"),
        )
        _require_text(self.secret_ref, "secret_ref")

    def __repr__(self) -> str:
        return (
            "SlackOAuthCompletionResult("
            f"install_id={self.install_id!r}, app_id={self.app_id!r}, "
            f"team_id={self.team_id!r}, enterprise_id={self.enterprise_id!r}, "
            f"is_enterprise_install={self.is_enterprise_install!r}, "
            f"granted_scopes={self.granted_scopes!r}, secret_ref='<redacted>')"
        )


@dataclass(frozen=True, slots=True)
class SlackOAuthInstallRevokeRequest:
    principal: AuthenticatedPrincipal
    install_id: str

    def __post_init__(self) -> None:
        if type(self.principal) is not AuthenticatedPrincipal:
            raise ValueError("principal must be an AuthenticatedPrincipal.")
        _require_text(self.install_id, "install_id")


@dataclass(frozen=True, slots=True)
class SlackOAuthEmployeeGrantRevokeRequest:
    principal: AuthenticatedPrincipal
    install_id: str
    mim_user_id: str

    def __post_init__(self) -> None:
        if type(self.principal) is not AuthenticatedPrincipal:
            raise ValueError("principal must be an AuthenticatedPrincipal.")
        _require_text(self.install_id, "install_id")
        _require_text(self.mim_user_id, "mim_user_id")


class SlackOAuthService:
    def __init__(
        self,
        *,
        identity_policy: IdentityPolicy,
        state_repository: SlackOAuthStateRepository,
        install_repository: SlackOAuthInstallRepository,
        credential_vault: SlackOAuthCredentialVault,
        provider: SlackOAuthProvider,
        client_id: str,
        client_secret: str,
        authorize_url: str,
        redirect_uri: str,
        required_scopes: tuple[str, ...],
        install_tenant: SlackOAuthTenant,
        clock: Callable[[], datetime],
        state_factory: Callable[[], str] = _default_state_factory,
        state_ttl: timedelta = _MAX_STATE_TTL,
    ) -> None:
        if state_ttl <= timedelta(0) or state_ttl > _MAX_STATE_TTL:
            raise ValueError("state_ttl must be positive and at most ten minutes.")
        self._identity_policy = identity_policy
        self._state_repository = state_repository
        self._install_repository = install_repository
        self._credential_vault = credential_vault
        self._provider = provider
        self._client_id = _require_text(client_id, "client_id")
        _require_text(client_secret, "client_secret")
        self._authorize_url = self._require_authorize_url(authorize_url)
        self._redirect_uri = _require_https_url(redirect_uri, "redirect_uri")
        self._required_scopes = _require_scope_tuple(required_scopes, "required_scopes")
        if type(install_tenant) is not SlackOAuthTenant:
            raise ValueError("install_tenant must be a SlackOAuthTenant.")
        self._install_tenant = install_tenant
        self._clock = clock
        self._state_factory = state_factory
        self._state_ttl = state_ttl

    def start_installation(
        self,
        request: SlackOAuthStartRequest,
    ) -> SlackOAuthStartResult:
        try:
            principal = self._reauthorize_admin(request.principal)
            now = self._now()
            raw_state = self._issue_state()
            pending = SlackOAuthPendingState(
                state_id=_state_id(raw_state),
                state_hash=_state_hash(raw_state),
                installer_mim_user_id=principal.user_id,
                installer_email=principal.email,
                required_scopes=self._required_scopes,
                redirect_uri=self._redirect_uri,
                install_tenant=self._install_tenant,
                issued_at=now,
                expires_at=now + self._state_ttl,
            )
            created = self._state_repository.create_pending_state(pending)
            if type(created) is not SlackOAuthPendingState or created != pending:
                raise ValueError("state repository returned malformed state")
            query = {
                "client_id": self._client_id,
                "scope": ",".join(self._required_scopes),
                "redirect_uri": self._redirect_uri,
                "state": raw_state,
                "team": self._install_tenant.team_id,
            }
            return SlackOAuthStartResult(
                authorization_url=f"{self._authorize_url}?{urlencode(query)}",
                state_id=created.state_id,
                expires_at=created.expires_at,
            )
        except AccessDenied:
            raise SlackOAuthDenied(_DENIED_MESSAGE) from None
        except (
            SlackOAuthStateStoreError,
            RuntimeError,
            ValueError,
            TypeError,
        ):
            raise SlackOAuthFlowError(_FLOW_MESSAGE) from None

    def complete_installation(
        self,
        request: SlackOAuthCallbackRequest,
    ) -> SlackOAuthCompletionResult:
        try:
            principal = self._reauthorize_admin(request.principal)
            pending = self._state_repository.consume_pending_state(
                state_id=_state_id(request.state),
                state_hash=_state_hash(request.state),
                expected_installer_mim_user_id=principal.user_id,
                expected_installer_email=principal.email,
                expected_tenant=self._install_tenant,
                expected_redirect_uri=self._redirect_uri,
                expected_scopes=self._required_scopes,
                now=self._now(),
            )
            self._validate_pending_state(
                pending, principal=principal, raw_state=request.state
            )
        except (AccessDenied, SlackOAuthStateOwnerMismatch):
            raise SlackOAuthDenied(_DENIED_MESSAGE) from None
        except (
            SlackOAuthStateRejected,
            SlackOAuthStateStoreError,
            RuntimeError,
            ValueError,
            TypeError,
        ):
            raise SlackOAuthFlowError(_FLOW_MESSAGE) from None

        grant: SlackOAuthGrant | None = None
        try:
            existing = self._install_repository.get_active_install_for_tenant(
                team_id=self._install_tenant.team_id,
                enterprise_id=self._install_tenant.enterprise_id,
            )
            if existing is not None:
                raise ValueError("active install already exists for tenant")
            grant = self._provider.exchange_installation_code(
                code=request.code,
                redirect_uri=pending.redirect_uri,
            )
            if type(grant) is not SlackOAuthGrant:
                raise ValueError("provider returned malformed grant")
            self._validate_grant(grant)
            completed_at = self._now()
            install = SlackOAuthSharedInstall(
                install_id=_install_id(
                    app_id=grant.app_id,
                    team_id=grant.team_id,
                    enterprise_id=grant.enterprise_id,
                    is_enterprise_install=grant.is_enterprise_install,
                ),
                app_id=grant.app_id,
                team_id=grant.team_id,
                enterprise_id=grant.enterprise_id,
                is_enterprise_install=grant.is_enterprise_install,
                granted_scopes=grant.granted_scopes,
                secret_ref=grant.secret_ref,
                installer_mim_user_id=principal.user_id,
                installer_email=principal.email,
                created_at=completed_at,
                updated_at=completed_at,
                state=SlackOAuthInstallState.ACTIVE,
            )
            saved = self._install_repository.save_shared_install(install)
            if type(saved) is not SlackOAuthSharedInstall or saved != install:
                raise ValueError("install repository returned malformed install")
            return SlackOAuthCompletionResult(
                install_id=saved.install_id,
                app_id=saved.app_id,
                team_id=saved.team_id,
                enterprise_id=saved.enterprise_id,
                is_enterprise_install=saved.is_enterprise_install,
                granted_scopes=saved.granted_scopes,
                secret_ref=saved.secret_ref,
            )
        except (
            SlackOAuthProviderError,
            SlackOAuthInstallRepositoryError,
            SlackOAuthCredentialVaultError,
            RuntimeError,
            ValueError,
            TypeError,
        ):
            if type(grant) is SlackOAuthGrant:
                self._best_effort_revoke_and_destroy(grant.secret_ref)
            raise SlackOAuthFlowError(_FLOW_MESSAGE) from None

    def revoke_installation(
        self,
        request: SlackOAuthInstallRevokeRequest,
    ) -> SlackOAuthSharedInstall:
        try:
            self._reauthorize_admin(request.principal)
            current = self._install_repository.get_shared_install(
                install_id=request.install_id
            )
            self._provider.uninstall_installation(
                secret_ref=current.secret_ref,
                app_id=current.app_id,
                team_id=current.team_id,
                enterprise_id=current.enterprise_id,
                is_enterprise_install=current.is_enterprise_install,
            )
            self._credential_vault.destroy_secret_ref(secret_ref=current.secret_ref)
            return self._install_repository.revoke_shared_install(
                install_id=current.install_id,
                revoked_at=self._now(),
            )
        except AccessDenied:
            raise SlackOAuthDenied(_DENIED_MESSAGE) from None
        except (
            SlackOAuthProviderError,
            SlackOAuthCredentialVaultError,
            SlackOAuthInstallRepositoryError,
            RuntimeError,
            ValueError,
            TypeError,
        ):
            raise SlackOAuthFlowError(_FLOW_MESSAGE) from None

    def revoke_employee_grant(
        self,
        request: SlackOAuthEmployeeGrantRevokeRequest,
    ) -> SlackOAuthIdentityLink:
        try:
            self._reauthorize_admin(request.principal)
            return self._install_repository.revoke_identity_link(
                install_id=request.install_id,
                mim_user_id=request.mim_user_id,
                revoked_at=self._now(),
            )
        except AccessDenied:
            raise SlackOAuthDenied(_DENIED_MESSAGE) from None
        except (SlackOAuthInstallRepositoryError, RuntimeError, ValueError, TypeError):
            raise SlackOAuthFlowError(_FLOW_MESSAGE) from None

    def _validate_pending_state(
        self,
        pending: SlackOAuthPendingState,
        *,
        principal: AuthenticatedPrincipal,
        raw_state: str,
    ) -> None:
        if pending.state_id != _state_id(raw_state):
            raise ValueError("state repository returned malformed state")
        if pending.state_hash != _state_hash(raw_state):
            raise ValueError("state repository returned malformed state")
        if pending.installer_mim_user_id != principal.user_id:
            raise ValueError("state repository returned malformed state")
        if pending.installer_email != principal.email.casefold():
            raise ValueError("state repository returned malformed state")
        if pending.required_scopes != self._required_scopes:
            raise ValueError("state repository returned malformed state")
        if pending.redirect_uri != self._redirect_uri:
            raise ValueError("state repository returned malformed state")
        if pending.install_tenant != self._install_tenant:
            raise ValueError("state repository returned malformed state")
        if pending.expires_at - pending.issued_at != self._state_ttl:
            raise ValueError("state repository returned malformed state")
        if pending.consumed_at is None:
            raise ValueError("state repository returned malformed state")

    def _reauthorize_admin(
        self,
        principal: AuthenticatedPrincipal,
    ) -> AuthenticatedPrincipal:
        authorized = self._identity_policy.authorize_resolved_user(
            user_id=principal.user_id,
            email=principal.email,
        )
        require_admin(authorized)
        return authorized

    def _validate_grant(self, grant: SlackOAuthGrant) -> None:
        tenant = SlackOAuthTenant(
            team_id=grant.team_id, enterprise_id=grant.enterprise_id
        )
        if tenant != self._install_tenant:
            raise ValueError("grant tenant is outside policy")
        if not set(self._required_scopes).issubset(set(grant.granted_scopes)):
            raise ValueError("grant scopes are outside policy")

    def _best_effort_revoke_and_destroy(self, secret_ref: str) -> None:
        try:
            self._provider.revoke_installation(secret_ref=secret_ref)
        except (SlackOAuthProviderError, RuntimeError, ValueError, TypeError):
            pass
        try:
            self._credential_vault.destroy_secret_ref(secret_ref=secret_ref)
        except (SlackOAuthCredentialVaultError, RuntimeError, ValueError, TypeError):
            pass

    def _issue_state(self) -> str:
        raw_state = _require_text(self._state_factory(), "state")
        if len(raw_state) < _MIN_STATE_LENGTH:
            raise ValueError("state must contain at least 43 URL-safe characters.")
        allowed = set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        if any(character not in allowed for character in raw_state):
            raise ValueError("state must be URL-safe")
        return raw_state

    def _now(self) -> datetime:
        now = self._clock()
        _require_utc(now)
        return now

    def _require_authorize_url(self, value: object) -> str:
        normalized = _require_https_url(value, "authorize_url")
        if normalized != _SLACK_AUTHORIZE_URL:
            raise ValueError("authorize_url must be the official Slack OAuth URL.")
        return normalized
