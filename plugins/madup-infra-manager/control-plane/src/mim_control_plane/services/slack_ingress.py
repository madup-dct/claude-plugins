"""Private signed Slack ingress before central identity authorization."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from mim_control_plane.domain.central_identity import VerifiedSlackActor
from mim_control_plane.ports.slack import (
    SlackIdentityResolver,
    SlackReplayDetected,
    SlackReplayRegistry,
    SlackResolutionNotFound,
)
from mim_control_plane.security.slack import (
    SlackDenied,
    SlackRequestVerifier,
)

_DENIED_MESSAGE = "Slack request was denied."


class SlackIngressDenied(PermissionError):
    """Raised when signed Slack ingress cannot prove a bounded actor."""


@dataclass(frozen=True, slots=True)
class SlackIngressRequest:
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.headers, tuple):
            raise ValueError("headers must be an immutable tuple.")
        if not isinstance(self.body, bytes):
            raise ValueError("body must be raw bytes.")

    def __repr__(self) -> str:
        return (
            "SlackIngressRequest("
            f"headers={len(self.headers)!r} pairs, body={len(self.body)!r} bytes)"
        )


@dataclass(frozen=True, slots=True)
class VerifiedSlackIngress:
    actor: VerifiedSlackActor
    command: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command:
            raise ValueError("command must be exact text.")
        if not isinstance(self.text, str):
            raise ValueError("text must be exact text.")

    def __repr__(self) -> str:
        return (
            "VerifiedSlackIngress("
            f"actor={self.actor!r}, command={self.command!r}, text='<redacted>')"
        )


class SlackIngressGateway:
    def __init__(
        self,
        *,
        allowed_commands: frozenset[str],
        signing_secret: bytes,
        replay_registry: SlackReplayRegistry,
        identity_resolver: SlackIdentityResolver,
        clock: Callable[[], datetime],
    ) -> None:
        self._replay_registry = replay_registry
        self._identity_resolver = identity_resolver
        self._verifier = SlackRequestVerifier(
            allowed_commands=allowed_commands,
            signing_secret=signing_secret,
            clock=clock,
        )

    def ingress(self, request: SlackIngressRequest) -> VerifiedSlackIngress:
        try:
            verified = self._verifier.verify(
                headers=request.headers,
                body=request.body,
            )
            self._replay_registry.claim_once(verified.replay_claim)
            resolution = self._identity_resolver.resolve_identity(
                team_id=verified.command.team_id,
                enterprise_id=verified.command.enterprise_id,
                slack_user_id=verified.command.slack_user_id,
            )
            actor = VerifiedSlackActor(
                install_id=resolution.install_id,
                team_id=verified.command.team_id,
                enterprise_id=verified.command.enterprise_id,
                slack_user_id=verified.command.slack_user_id,
                company_email=resolution.company_email,
                verified_at=verified.command.verified_at,
            )
            return VerifiedSlackIngress(
                actor=actor,
                command=verified.command.command,
                text=verified.command.text,
            )
        except (
            SlackDenied,
            SlackReplayDetected,
            SlackResolutionNotFound,
            AttributeError,
            TypeError,
            ValueError,
        ):
            raise SlackIngressDenied(_DENIED_MESSAGE) from None
