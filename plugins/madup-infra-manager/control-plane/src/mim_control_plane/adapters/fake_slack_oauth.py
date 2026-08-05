"""Deterministic fake adapters for centrally managed Slack OAuth tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from threading import Lock
from typing import cast

from mim_control_plane.domain.central_identity import SlackSharedInstall
from mim_control_plane.domain.slack_oauth import (
    SlackOAuthGrant,
    SlackOAuthIdentityLink,
    SlackOAuthPendingState,
    SlackOAuthSharedInstall,
    SlackOAuthTenant,
)
from mim_control_plane.ports.slack_oauth import (
    SlackOAuthInstallRepository,
    SlackOAuthInstallRepositoryError,
    SlackOAuthProvider,
    SlackOAuthProviderError,
    SlackOAuthStateOwnerMismatch,
    SlackOAuthStateRejected,
    SlackOAuthStateRepository,
    SlackOAuthStateStoreError,
    SlackSharedInstallWriter,
)

_CODE_PROOF_PREFIX = b"mim:slack-oauth:code-proof:v1\x00"


@dataclass(frozen=True, slots=True)
class SlackOAuthExchangeCall:
    code_proof: str
    redirect_uri: str


class FakeSlackOAuthStateRepository(SlackOAuthStateRepository):
    def __init__(self) -> None:
        self.created_records: list[SlackOAuthPendingState] = []
        self.consumed_records: list[SlackOAuthPendingState] = []
        self._records: dict[str, SlackOAuthPendingState] = {}
        self._lock = Lock()

    def create_pending_state(
        self,
        state: SlackOAuthPendingState,
    ) -> SlackOAuthPendingState:
        with self._lock:
            if state.state_id in self._records:
                raise SlackOAuthStateStoreError("Slack OAuth state could not be saved.")
            self._records[state.state_id] = state
            self.created_records.append(state)
            return state

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
    ) -> SlackOAuthPendingState:
        with self._lock:
            record = self._records.get(state_id)
            if record is None or record.state_hash != state_hash:
                raise SlackOAuthStateRejected("Slack OAuth state was rejected.")
            if (
                record.installer_mim_user_id != expected_installer_mim_user_id
                or record.installer_email != expected_installer_email.casefold()
            ):
                raise SlackOAuthStateOwnerMismatch("Slack OAuth state was rejected.")
            if record.install_tenant != expected_tenant:
                raise SlackOAuthStateRejected("Slack OAuth state was rejected.")
            if record.redirect_uri != expected_redirect_uri:
                raise SlackOAuthStateRejected("Slack OAuth state was rejected.")
            if record.required_scopes != expected_scopes:
                raise SlackOAuthStateRejected("Slack OAuth state was rejected.")
            if now >= record.expires_at or record.consumed_at is not None:
                raise SlackOAuthStateRejected("Slack OAuth state was rejected.")
            consumed = replace(
                record,
                consumed_at=now,
                version=record.version + 1,
            )
            self._records[state_id] = consumed
            self.consumed_records.append(consumed)
            return consumed


class FakeSlackOAuthProvider(SlackOAuthProvider):
    def __init__(self) -> None:
        self.exchange_calls: tuple[SlackOAuthExchangeCall, ...] = ()
        self.revoked_refs: tuple[str, ...] = ()
        self.uninstalled_refs: tuple[str, ...] = ()
        self._grants_by_code_proof: dict[str, SlackOAuthGrant] = {}

    def register_code(self, code: str, grant: SlackOAuthGrant) -> None:
        self._grants_by_code_proof[_code_proof(code)] = grant

    def exchange_installation_code(
        self,
        *,
        code: str,
        redirect_uri: str,
    ) -> SlackOAuthGrant:
        proof = _code_proof(code)
        grant = self._grants_by_code_proof.get(proof)
        if grant is None:
            raise SlackOAuthProviderError("Slack OAuth provider failed.")
        self.exchange_calls = self.exchange_calls + (
            SlackOAuthExchangeCall(code_proof=proof, redirect_uri=redirect_uri),
        )
        return grant

    def revoke_installation(self, *, secret_ref: str) -> None:
        self.revoked_refs = self.revoked_refs + (secret_ref,)

    def uninstall_installation(
        self,
        *,
        secret_ref: str,
        app_id: str,
        team_id: str,
        enterprise_id: str | None,
        is_enterprise_install: bool,
    ) -> None:
        del app_id, team_id, enterprise_id, is_enterprise_install
        self.uninstalled_refs = self.uninstalled_refs + (secret_ref,)


class FakeSlackOAuthInstallRepository(SlackOAuthInstallRepository):
    def __init__(self) -> None:
        self.installs: dict[str, SlackOAuthSharedInstall] = {}
        self.links: dict[tuple[str, str], SlackOAuthIdentityLink] = {}

    def get_active_install_for_tenant(
        self,
        *,
        team_id: str,
        enterprise_id: str | None,
    ) -> SlackOAuthSharedInstall | None:
        for install in self.installs.values():
            if (
                install.team_id == team_id
                and install.enterprise_id == enterprise_id
                and install.revoked_at is None
            ):
                return install
        return None

    def save_shared_install(
        self,
        install: SlackOAuthSharedInstall,
    ) -> SlackOAuthSharedInstall:
        existing = self.get_active_install_for_tenant(
            team_id=install.team_id,
            enterprise_id=install.enterprise_id,
        )
        if existing is not None and existing.install_id != install.install_id:
            raise SlackOAuthInstallRepositoryError("active install exists")
        self.installs[install.install_id] = install
        return install

    def get_shared_install(self, *, install_id: str) -> SlackOAuthSharedInstall:
        try:
            return self.installs[install_id]
        except KeyError:
            raise SlackOAuthInstallRepositoryError("missing install") from None

    def revoke_shared_install(
        self,
        *,
        install_id: str,
        revoked_at: datetime,
    ) -> SlackOAuthSharedInstall:
        current = self.get_shared_install(install_id=install_id)
        revoked = replace(
            current,
            updated_at=revoked_at,
            revoked_at=revoked_at,
            version=current.version + 1,
        )
        self.installs[install_id] = revoked
        return revoked

    def save_identity_link(
        self,
        link: SlackOAuthIdentityLink,
    ) -> SlackOAuthIdentityLink:
        self.links[(link.install_id, link.mim_user_id)] = link
        return link

    def revoke_identity_link(
        self,
        *,
        install_id: str,
        mim_user_id: str,
        revoked_at: datetime,
    ) -> SlackOAuthIdentityLink:
        key = (install_id, mim_user_id)
        try:
            current = self.links[key]
        except KeyError:
            raise SlackOAuthInstallRepositoryError("missing link") from None
        revoked = replace(
            current,
            updated_at=revoked_at,
            revoked_at=revoked_at,
            version=current.version + 1,
        )
        self.links[key] = revoked
        return revoked


class FakeSlackSharedInstallWriter(SlackSharedInstallWriter):
    def __init__(self) -> None:
        self.saved_records: list[SlackSharedInstall] = []

    def save_shared_install(
        self,
        install: SlackSharedInstall,
    ) -> SlackSharedInstall:
        self.saved_records.append(install)
        return cast(SlackSharedInstall, install)


def _code_proof(code: str) -> str:
    return sha256(_CODE_PROOF_PREFIX + code.encode("utf-8")).hexdigest()
