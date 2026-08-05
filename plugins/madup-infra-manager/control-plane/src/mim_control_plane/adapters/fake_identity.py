"""Deterministic fake identity adapters for central auth tests."""

from __future__ import annotations

from dataclasses import dataclass

from mim_control_plane.domain.central_identity import (
    ActionIntent,
    SlackIdentityLink,
    SlackSharedInstall,
)
from mim_control_plane.domain.models import UserId
from mim_control_plane.ports.identity import (
    ActionPolicyAuthorizer,
    ActionPolicyDecision,
    IdentityLinkDirectory,
    SharedInstallDirectory,
)
from mim_control_plane.ports.store import NotFound
from mim_control_plane.security.identity import AuthenticatedPrincipal


@dataclass(frozen=True, slots=True)
class ActionAuthorizationCall:
    principal: AuthenticatedPrincipal
    intent: ActionIntent
    surface: str


class FakeIdentityRegistry(SharedInstallDirectory, IdentityLinkDirectory):
    def __init__(
        self,
        *,
        installs: tuple[SlackSharedInstall, ...] = (),
        links: tuple[SlackIdentityLink, ...] = (),
    ) -> None:
        self._installs = {
            (record.install_id, record.team_id, record.enterprise_id): record
            for record in installs
        }
        self._links = {
            (record.install_id, record.team_id, record.slack_user_id): record
            for record in links
        }

    def get_shared_install(
        self,
        *,
        install_id: str,
        team_id: str,
        enterprise_id: str | None,
    ) -> SlackSharedInstall:
        record = self._installs.get((install_id, team_id, enterprise_id))
        if record is None:
            raise NotFound("shared install was not found.")
        return record

    def get_identity_link(
        self,
        *,
        install_id: str,
        team_id: str,
        slack_user_id: str,
    ) -> SlackIdentityLink:
        record = self._links.get((install_id, team_id, slack_user_id))
        if record is None:
            raise NotFound("identity link was not found.")
        return record


class FakeActionPolicyAuthorizer(ActionPolicyAuthorizer):
    def __init__(self) -> None:
        self.calls: list[ActionAuthorizationCall] = []
        self._denials: dict[tuple[UserId, str, str], ActionPolicyDecision] = {}

    def deny(
        self,
        *,
        user_id: UserId,
        action: str,
        resource_id: str,
        reason_code: str,
        audit_message: str | None = None,
    ) -> None:
        self._denials[(user_id, action, resource_id)] = ActionPolicyDecision(
            allowed=False,
            reason_code=reason_code,
            audit_message=audit_message,
        )

    def authorize(
        self,
        *,
        principal: AuthenticatedPrincipal,
        intent: ActionIntent,
        surface: str,
    ) -> ActionPolicyDecision:
        self.calls.append(
            ActionAuthorizationCall(
                principal=principal,
                intent=intent,
                surface=surface,
            )
        )
        return self._denials.get(
            (principal.user_id, intent.action.value, intent.resource_id),
            ActionPolicyDecision(allowed=True, reason_code="allowed"),
        )
