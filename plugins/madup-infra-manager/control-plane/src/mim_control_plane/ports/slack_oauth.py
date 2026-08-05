"""Private ports for centrally managed Slack OAuth state and credentials."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from mim_control_plane.domain.central_identity import SlackSharedInstall
from mim_control_plane.domain.slack_oauth import (
    SlackOAuthGrant,
    SlackOAuthIdentityLink,
    SlackOAuthPendingState,
    SlackOAuthSharedInstall,
    SlackOAuthTenant,
)


class SlackOAuthStateRejected(RuntimeError):
    """Raised when a state token is missing, expired, tampered, or replayed."""


class SlackOAuthStateOwnerMismatch(RuntimeError):
    """Raised when a state exists but belongs to a different bound admin."""


class SlackOAuthStateStoreError(RuntimeError):
    """Raised when the private OAuth state repository cannot persist safely."""


class SlackOAuthProviderError(RuntimeError):
    """Raised when the private Slack OAuth provider cannot finish safely."""


class SlackOAuthInstallRepositoryError(RuntimeError):
    """Raised when install metadata or identity-link persistence fails closed."""


class SlackOAuthCredentialVaultError(RuntimeError):
    """Raised when the dedicated Slack credential vault fails closed."""


class SlackOAuthStateRepository(Protocol):
    def create_pending_state(
        self,
        state: SlackOAuthPendingState,
    ) -> SlackOAuthPendingState: ...

    def consume_pending_state(
        self,
        *,
        state_id: str,
        state_hash: str,
        expected_installer_mim_user_id: str,
        expected_installer_email: str,
        expected_tenant: SlackOAuthTenant,
        expected_redirect_uri: str,
        expected_scopes: tuple[str, ...],
        now: datetime,
    ) -> SlackOAuthPendingState: ...


class SlackOAuthInstallRepository(Protocol):
    def get_active_install_for_tenant(
        self,
        *,
        team_id: str,
        enterprise_id: str | None,
    ) -> SlackOAuthSharedInstall | None: ...

    def save_shared_install(
        self,
        install: SlackOAuthSharedInstall,
    ) -> SlackOAuthSharedInstall: ...

    def get_shared_install(self, *, install_id: str) -> SlackOAuthSharedInstall: ...

    def revoke_shared_install(
        self,
        *,
        install_id: str,
        revoked_at: datetime,
    ) -> SlackOAuthSharedInstall: ...

    def save_identity_link(
        self,
        link: SlackOAuthIdentityLink,
    ) -> SlackOAuthIdentityLink: ...

    def revoke_identity_link(
        self,
        *,
        install_id: str,
        mim_user_id: str,
        revoked_at: datetime,
    ) -> SlackOAuthIdentityLink: ...


class SlackOAuthCredentialVault(Protocol):
    def destroy_secret_ref(self, *, secret_ref: str) -> None: ...


class SlackOAuthProvider(Protocol):
    def exchange_installation_code(
        self,
        *,
        code: str,
        redirect_uri: str,
    ) -> SlackOAuthGrant: ...

    def revoke_installation(self, *, secret_ref: str) -> None: ...

    def uninstall_installation(
        self,
        *,
        secret_ref: str,
        app_id: str,
        team_id: str,
        enterprise_id: str | None,
        is_enterprise_install: bool,
    ) -> None: ...


class SlackSharedInstallWriter(Protocol):
    """Legacy compatibility surface for pre-Task15A tests and wiring."""

    def save_shared_install(
        self,
        install: SlackSharedInstall,
    ) -> SlackSharedInstall: ...
