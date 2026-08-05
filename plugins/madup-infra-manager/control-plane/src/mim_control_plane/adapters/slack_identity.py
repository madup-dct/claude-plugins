"""Bridge Slack OAuth persistence into bounded central identity records."""

from __future__ import annotations

from mim_control_plane.adapters.firestore_slack_oauth import (
    FirestoreSlackOAuthRepository,
)
from mim_control_plane.domain.central_identity import (
    SlackIdentityLink,
    SlackIdentityLinkState,
    SlackSharedInstall,
    SlackSharedInstallState,
)
from mim_control_plane.domain.slack_oauth import (
    SlackOAuthIdentityLink,
    SlackOAuthIdentityLinkState,
    SlackOAuthInstallState,
    SlackOAuthSharedInstall,
)
from mim_control_plane.ports.identity import (
    IdentityLinkDirectory,
    SharedInstallDirectory,
)
from mim_control_plane.ports.slack_oauth import SlackOAuthInstallRepositoryError
from mim_control_plane.ports.store import NotFound


class FirestoreSlackIdentityDirectory(SharedInstallDirectory, IdentityLinkDirectory):
    """Read only the exact Slack install/link metadata needed for central auth."""

    def __init__(self, *, repository: FirestoreSlackOAuthRepository) -> None:
        self._repository = repository

    def get_shared_install(
        self,
        *,
        install_id: str,
        team_id: str,
        enterprise_id: str | None,
    ) -> SlackSharedInstall:
        try:
            record = self._repository.get_shared_install(install_id=install_id)
            if (
                record.install_id != install_id
                or record.team_id != team_id
                or record.enterprise_id != enterprise_id
            ):
                raise NotFound("shared install was not found.")
            return SlackSharedInstall(
                install_id=record.install_id,
                team_id=record.team_id,
                enterprise_id=record.enterprise_id,
                granted_scopes=record.granted_scopes,
                installer_mim_user_id=record.installer_mim_user_id,
                installer_email=record.installer_email,
                created_at=record.created_at,
                updated_at=record.updated_at,
                state=_map_install_state(record),
                revoked_at=record.revoked_at,
                version=record.version,
            )
        except (SlackOAuthInstallRepositoryError, ValueError, NotFound):
            raise NotFound("shared install was not found.") from None

    def get_identity_link(
        self,
        *,
        install_id: str,
        team_id: str,
        slack_user_id: str,
    ) -> SlackIdentityLink:
        try:
            record = self._repository.get_identity_link_by_slack_user(
                install_id=install_id,
                team_id=team_id,
                slack_user_id=slack_user_id,
            )
            if (
                record.install_id != install_id
                or record.team_id != team_id
                or record.slack_user_id != slack_user_id
            ):
                raise NotFound("identity link was not found.")
            return SlackIdentityLink(
                install_id=record.install_id,
                team_id=record.team_id,
                slack_user_id=record.slack_user_id,
                mim_user_id=record.mim_user_id,
                company_email=record.company_email,
                verified_at=record.created_at,
                created_at=record.created_at,
                updated_at=record.updated_at,
                state=_map_link_state(record),
                revoked_at=record.revoked_at,
                version=record.version,
            )
        except (SlackOAuthInstallRepositoryError, ValueError, NotFound):
            raise NotFound("identity link was not found.") from None


def _map_install_state(record: SlackOAuthSharedInstall) -> SlackSharedInstallState:
    if record.state is SlackOAuthInstallState.ACTIVE:
        return SlackSharedInstallState.ACTIVE
    if record.state is SlackOAuthInstallState.REVOKED:
        return SlackSharedInstallState.REVOKED
    raise ValueError("shared install state is invalid")


def _map_link_state(record: SlackOAuthIdentityLink) -> SlackIdentityLinkState:
    if record.state is SlackOAuthIdentityLinkState.ACTIVE:
        return SlackIdentityLinkState.ACTIVE
    if record.state is SlackOAuthIdentityLinkState.REVOKED:
        return SlackIdentityLinkState.REVOKED
    raise ValueError("identity link state is invalid")
