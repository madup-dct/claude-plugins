"""Deterministic fake adapters for signed Slack ingress tests."""

from __future__ import annotations

from mim_control_plane.ports.slack import (
    SlackIdentityResolution,
    SlackIdentityResolver,
    SlackReplayClaim,
    SlackReplayDetected,
    SlackReplayRegistry,
    SlackResolutionNotFound,
)


class FakeSlackReplayRegistry(SlackReplayRegistry):
    def __init__(self) -> None:
        self.claims: list[SlackReplayClaim] = []
        self._claims_by_fingerprint: dict[str, SlackReplayClaim] = {}

    def claim_once(self, claim: SlackReplayClaim) -> None:
        self._claims_by_fingerprint = {
            fingerprint: existing
            for fingerprint, existing in self._claims_by_fingerprint.items()
            if existing.expires_at > claim.claimed_at
        }
        if claim.fingerprint in self._claims_by_fingerprint:
            raise SlackReplayDetected("signed Slack request was replayed")
        self._claims_by_fingerprint[claim.fingerprint] = claim
        self.claims.append(claim)


class FakeSlackIdentityResolver(SlackIdentityResolver):
    def __init__(
        self,
        *,
        resolutions: dict[tuple[str, str | None, str], SlackIdentityResolution],
    ) -> None:
        self._resolutions = dict(resolutions)

    def resolve_identity(
        self,
        *,
        team_id: str,
        enterprise_id: str | None,
        slack_user_id: str,
    ) -> SlackIdentityResolution:
        resolution = self._resolutions.get((team_id, enterprise_id, slack_user_id))
        if resolution is None:
            raise SlackResolutionNotFound("trusted Slack identity mapping was missing")
        return resolution
