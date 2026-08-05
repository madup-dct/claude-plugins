"""Central identity gateway for browser and shared Slack entrypoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from mim_control_plane.domain.central_identity import (
    ActionIntent,
    SlackIdentityLinkState,
    SlackSharedInstallState,
    VerifiedSlackActor,
)
from mim_control_plane.ports.identity import (
    ActionPolicyAuthorizer,
    ActionPolicyDecision,
    IdentityLinkDirectory,
    SharedInstallDirectory,
)
from mim_control_plane.ports.store import NotFound
from mim_control_plane.security.authorization import (
    AccessDenied,
    IdentityPolicy,
    require_admin,
)
from mim_control_plane.security.identity import (
    AuthenticatedPrincipal,
    AuthenticationRequest,
    IdentityAuthenticator,
)

_DENIED_MESSAGE = "Identity is not authorized for MIM."
_SLACK_ACTOR_MAX_AGE = timedelta(minutes=5)


class CentralIdentityDenied(PermissionError):
    """Raised when a browser or shared-Slack actor falls outside MIM policy."""


class IdentitySurface(StrEnum):
    BROWSER = "browser"
    SLACK = "slack"


@dataclass(frozen=True, slots=True)
class BrowserActionRequest:
    authentication_request: AuthenticationRequest
    intent: ActionIntent


@dataclass(frozen=True, slots=True)
class SlackActionRequest:
    actor: VerifiedSlackActor
    intent: ActionIntent


@dataclass(frozen=True, slots=True)
class AuthorizedAction:
    principal: AuthenticatedPrincipal
    intent: ActionIntent
    surface: IdentitySurface


class CentralIdentityGateway:
    def __init__(
        self,
        *,
        browser_authenticator: IdentityAuthenticator,
        identity_policy: IdentityPolicy,
        shared_install_directory: SharedInstallDirectory | None,
        identity_link_directory: IdentityLinkDirectory | None,
        action_authorizer: ActionPolicyAuthorizer,
        required_slack_scopes: frozenset[str],
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(required_slack_scopes, frozenset):
            raise ValueError("required_slack_scopes must be a frozenset.")
        for scope in required_slack_scopes:
            if not isinstance(scope, str) or not scope.strip():
                raise ValueError("required_slack_scopes must contain exact text.")
        self._browser_authenticator = browser_authenticator
        self._identity_policy = identity_policy
        self._shared_install_directory = shared_install_directory
        self._identity_link_directory = identity_link_directory
        self._action_authorizer = action_authorizer
        self._required_slack_scopes = frozenset(
            scope.strip() for scope in required_slack_scopes
        )
        self._clock = clock

    def authorize_browser(self, request: BrowserActionRequest) -> AuthorizedAction:
        return self.authorize_browser_for(
            authentication_request=request.authentication_request,
            intent_factory=lambda _principal: request.intent,
        )

    def authorize_browser_for(
        self,
        *,
        authentication_request: AuthenticationRequest,
        intent_factory: Callable[[AuthenticatedPrincipal], ActionIntent],
    ) -> AuthorizedAction:
        """Authenticate once, then derive the authorized resource from that identity."""
        try:
            principal = self._browser_authenticator.authenticate(
                authentication_request
            )
            return self._authorize_action(
                principal=principal,
                intent=intent_factory(principal),
                surface=IdentitySurface.BROWSER,
            )
        except PermissionError:
            raise CentralIdentityDenied(_DENIED_MESSAGE) from None

    def authorize_authenticated_browser_for(
        self,
        *,
        authorized_browser: AuthorizedAction,
        intent_factory: Callable[[AuthenticatedPrincipal], ActionIntent],
    ) -> AuthorizedAction:
        """Re-authorize a previously authenticated browser principal."""
        try:
            if type(authorized_browser) is not AuthorizedAction:
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            if authorized_browser.surface is not IdentitySurface.BROWSER:
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            if type(authorized_browser.principal) is not AuthenticatedPrincipal:
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            if type(authorized_browser.intent) is not ActionIntent:
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            return self._authorize_action(
                principal=authorized_browser.principal,
                intent=intent_factory(authorized_browser.principal),
                surface=IdentitySurface.BROWSER,
            )
        except (CentralIdentityDenied, PermissionError, ValueError):
            raise CentralIdentityDenied(_DENIED_MESSAGE) from None

    def authorize_slack(self, request: SlackActionRequest) -> AuthorizedAction:
        try:
            if (
                not self._required_slack_scopes
                or self._shared_install_directory is None
                or self._identity_link_directory is None
            ):
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            actor = request.actor
            self._require_fresh_actor(actor)
            install = self._shared_install_directory.get_shared_install(
                install_id=actor.install_id,
                team_id=actor.team_id,
                enterprise_id=actor.enterprise_id,
            )
            if install.state is not SlackSharedInstallState.ACTIVE:
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            if (
                install.install_id != actor.install_id
                or install.team_id != actor.team_id
                or install.enterprise_id != actor.enterprise_id
            ):
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            if not self._required_slack_scopes.issubset(
                frozenset(install.granted_scopes)
            ):
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            installer = self._identity_policy.authorize_resolved_user(
                user_id=install.installer_mim_user_id,
                email=install.installer_email,
            )
            require_admin(installer)
            link = self._identity_link_directory.get_identity_link(
                install_id=actor.install_id,
                team_id=actor.team_id,
                slack_user_id=actor.slack_user_id,
            )
            if link.state is not SlackIdentityLinkState.ACTIVE:
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            if (
                link.install_id != install.install_id
                or link.team_id != install.team_id
                or link.slack_user_id != actor.slack_user_id
            ):
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            if link.company_email != actor.company_email:
                raise CentralIdentityDenied(_DENIED_MESSAGE)
            principal = self._identity_policy.authorize_resolved_user(
                user_id=link.mim_user_id,
                email=link.company_email,
            )
            return self._authorize_action(
                principal=principal,
                intent=request.intent,
                surface=IdentitySurface.SLACK,
            )
        except (AccessDenied, CentralIdentityDenied, NotFound, ValueError):
            raise CentralIdentityDenied(_DENIED_MESSAGE) from None

    def _authorize_action(
        self,
        *,
        principal: AuthenticatedPrincipal,
        intent: ActionIntent,
        surface: IdentitySurface,
    ) -> AuthorizedAction:
        decision = self._action_authorizer.authorize(
            principal=principal,
            intent=intent,
            surface=surface.value,
        )
        if type(decision) is not ActionPolicyDecision or decision.allowed is not True:
            raise CentralIdentityDenied(_DENIED_MESSAGE)
        return AuthorizedAction(
            principal=principal,
            intent=intent,
            surface=surface,
        )

    def _require_fresh_actor(self, actor: VerifiedSlackActor) -> None:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise CentralIdentityDenied(_DENIED_MESSAGE)
        age = now - actor.verified_at
        if age < timedelta(0) or age > _SLACK_ACTOR_MAX_AGE:
            raise CentralIdentityDenied(_DENIED_MESSAGE)
