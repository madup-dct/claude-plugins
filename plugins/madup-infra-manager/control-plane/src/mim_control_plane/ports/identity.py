"""Ports for central install/link lookup and action authorization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mim_control_plane.domain.central_identity import (
    ActionIntent,
    SlackIdentityLink,
    SlackSharedInstall,
)
from mim_control_plane.security.identity import AuthenticatedPrincipal


@dataclass(frozen=True, slots=True)
class ActionPolicyDecision:
    allowed: bool
    reason_code: str
    audit_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed must be an exact bool.")
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise ValueError("reason_code must be a non-empty string.")
        if self.audit_message is not None and (
            not isinstance(self.audit_message, str) or not self.audit_message.strip()
        ):
            raise ValueError("audit_message must be non-empty when set.")


class SharedInstallDirectory(Protocol):
    def get_shared_install(
        self,
        *,
        install_id: str,
        team_id: str,
        enterprise_id: str | None,
    ) -> SlackSharedInstall: ...


class IdentityLinkDirectory(Protocol):
    def get_identity_link(
        self,
        *,
        install_id: str,
        team_id: str,
        slack_user_id: str,
    ) -> SlackIdentityLink: ...


class ActionPolicyAuthorizer(Protocol):
    def authorize(
        self,
        *,
        principal: AuthenticatedPrincipal,
        intent: ActionIntent,
        surface: str,
    ) -> ActionPolicyDecision: ...
