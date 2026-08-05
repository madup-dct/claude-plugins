"""Fail-closed verification for signed Slack slash-command ingress."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mim_control_plane.ports.slack import SlackReplayClaim

_DENIED_MESSAGE = "Slack request was denied."
_MAX_BODY_BYTES = 64 * 1024
_MAX_FIELDS = 16
_MAX_FIELD_BYTES = 4096
_WINDOW = timedelta(minutes=5)
_SIGNATURE_PATTERN = re.compile(r"^v0=[0-9a-f]{64}$")
_TIMESTAMP_PATTERN = re.compile(r"^[0-9]{1,16}$")
_HEX_PAIR_PATTERN = re.compile(r"[0-9A-Fa-f]{2}")
_IGNORED_SLACK_FIELDS = frozenset(
    {
        "token",
        "team_domain",
        "channel_id",
        "channel_name",
        "user_name",
        "response_url",
        "trigger_id",
        "api_app_id",
        "enterprise_name",
        "is_enterprise_install",
    }
)
_BLOCKED_FIELDS = frozenset(
    {
        "api_app_token",
        "access_token",
        "refresh_token",
        "client_secret",
        "signing_secret",
        "authorization",
        "bearer_token",
    }
)
_ALLOWED_FIELDS = frozenset(
    {
        "team_id",
        "enterprise_id",
        "user_id",
        "command",
        "text",
    }
) | _IGNORED_SLACK_FIELDS


class SlackDenied(PermissionError):
    """Raised when a Slack ingress request cannot be trusted."""


@dataclass(frozen=True, slots=True)
class VerifiedSlackCommand:
    team_id: str
    enterprise_id: str | None
    slack_user_id: str
    command: str
    text: str
    verified_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", _require_token(self.team_id, "team_id"))
        if self.enterprise_id is not None:
            object.__setattr__(
                self,
                "enterprise_id",
                _require_token(self.enterprise_id, "enterprise_id"),
            )
        object.__setattr__(
            self,
            "slack_user_id",
            _require_token(self.slack_user_id, "slack_user_id"),
        )
        object.__setattr__(self, "command", _require_command(self.command))
        if not isinstance(self.text, str):
            raise ValueError("text must be exact text.")
        _require_utc(self.verified_at)

    def __repr__(self) -> str:
        return (
            "VerifiedSlackCommand("
            f"team_id={self.team_id!r}, enterprise_id={self.enterprise_id!r}, "
            f"command={self.command!r}, verified_at={self.verified_at.isoformat()!r}, "
            "text='<redacted>')"
        )


@dataclass(frozen=True, slots=True)
class SlackVerificationResult:
    command: VerifiedSlackCommand
    replay_claim: SlackReplayClaim


def sign_slack_request(*, body: bytes, timestamp: int, key: bytes) -> str:
    """Render the canonical Slack HMAC header value for fixtures/tests."""

    _validate_key(key)
    if not isinstance(body, bytes):
        raise SlackDenied(_DENIED_MESSAGE)
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise SlackDenied(_DENIED_MESSAGE)
    message = _slack_signature_message(body=body, timestamp=timestamp)
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    return f"v0={digest}"


class SlackRequestVerifier:
    """Verify signed slash-command bodies and derive bounded metadata only."""

    def __init__(
        self,
        *,
        allowed_commands: frozenset[str],
        signing_secret: bytes,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(allowed_commands, frozenset) or not allowed_commands:
            raise ValueError("allowed_commands must be a non-empty frozenset.")
        normalized_commands = frozenset(
            _require_command(command) for command in allowed_commands
        )
        _validate_key(signing_secret)
        self._allowed_commands = normalized_commands
        self._signing_secret = bytes(signing_secret)
        self._clock = clock

    def verify(
        self,
        *,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
    ) -> SlackVerificationResult:
        if not isinstance(body, bytes) or len(body) > _MAX_BODY_BYTES:
            raise SlackDenied(_DENIED_MESSAGE)
        now = self._clock()
        _require_utc(now)
        signature, timestamp = _extract_signed_headers(headers)
        _require_form_content_type(headers)
        signed_at = _timestamp_to_utc(timestamp)
        age = now - signed_at
        if age < -_WINDOW or age > _WINDOW:
            raise SlackDenied(_DENIED_MESSAGE)

        expected = sign_slack_request(
            body=body,
            timestamp=timestamp,
            key=self._signing_secret,
        )
        if not hmac.compare_digest(expected, signature):
            raise SlackDenied(_DENIED_MESSAGE)

        fields = _parse_slash_form(body)
        command = _require_command(fields["command"])
        if command not in self._allowed_commands:
            raise SlackDenied(_DENIED_MESSAGE)
        verified = VerifiedSlackCommand(
            team_id=_require_token(fields["team_id"], "team_id"),
            enterprise_id=_optional_token(fields.get("enterprise_id"), "enterprise_id"),
            slack_user_id=_require_token(fields["user_id"], "user_id"),
            command=command,
            text=_require_text_field(fields["text"]),
            verified_at=now,
        )
        fingerprint = hashlib.sha256(
            "\n".join(
                (
                    "mim-slack-replay-v1",
                    signature,
                    str(timestamp),
                    verified.team_id,
                    verified.enterprise_id or "",
                    verified.slack_user_id,
                    verified.command,
                    hashlib.sha256(verified.text.encode("utf-8")).hexdigest(),
                )
            ).encode("utf-8")
        ).hexdigest()
        return SlackVerificationResult(
            command=verified,
            replay_claim=SlackReplayClaim(
                fingerprint=fingerprint,
                claimed_at=now,
                expires_at=now + _WINDOW,
            ),
        )


def _extract_signed_headers(headers: tuple[tuple[str, str], ...]) -> tuple[str, int]:
    signature: str | None = None
    timestamp_raw: str | None = None
    for name, value in headers:
        if not isinstance(name, str) or not isinstance(value, str):
            raise SlackDenied(_DENIED_MESSAGE)
        normalized_name = name.strip().casefold()
        if normalized_name == "x-slack-signature":
            if signature is not None:
                raise SlackDenied(_DENIED_MESSAGE)
            candidate = value.strip()
            if _SIGNATURE_PATTERN.fullmatch(candidate) is None:
                raise SlackDenied(_DENIED_MESSAGE)
            signature = candidate
        elif normalized_name == "x-slack-request-timestamp":
            if timestamp_raw is not None:
                raise SlackDenied(_DENIED_MESSAGE)
            candidate = value.strip()
            if _TIMESTAMP_PATTERN.fullmatch(candidate) is None:
                raise SlackDenied(_DENIED_MESSAGE)
            timestamp_raw = candidate
    if signature is None or timestamp_raw is None:
        raise SlackDenied(_DENIED_MESSAGE)
    return signature, int(timestamp_raw)


def _parse_slash_form(body: bytes) -> dict[str, str]:
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SlackDenied(_DENIED_MESSAGE) from None
    if not decoded:
        raise SlackDenied(_DENIED_MESSAGE)
    segments = decoded.split("&")
    if len(segments) > _MAX_FIELDS:
        raise SlackDenied(_DENIED_MESSAGE)

    parsed: dict[str, str] = {}
    seen_keys: set[str] = set()
    for segment in segments:
        raw_key, separator, raw_value = segment.partition("=")
        if not separator:
            raise SlackDenied(_DENIED_MESSAGE)
        key = _decode_form_component(raw_key)
        value = _decode_form_component(raw_value)
        if not key or len(key.encode("utf-8")) > 128:
            raise SlackDenied(_DENIED_MESSAGE)
        if len(value.encode("utf-8")) > _MAX_FIELD_BYTES:
            raise SlackDenied(_DENIED_MESSAGE)
        normalized_key = key.casefold()
        if normalized_key in _BLOCKED_FIELDS:
            raise SlackDenied(_DENIED_MESSAGE)
        if normalized_key not in _ALLOWED_FIELDS:
            raise SlackDenied(_DENIED_MESSAGE)
        if normalized_key in seen_keys:
            raise SlackDenied(_DENIED_MESSAGE)
        seen_keys.add(normalized_key)
        if normalized_key in _IGNORED_SLACK_FIELDS:
            continue
        parsed[normalized_key] = value

    required = {"team_id", "user_id", "command", "text"}
    if not required.issubset(parsed):
        raise SlackDenied(_DENIED_MESSAGE)
    return parsed


def _require_form_content_type(headers: tuple[tuple[str, str], ...]) -> None:
    content_type: str | None = None
    for name, value in headers:
        if not isinstance(name, str) or not isinstance(value, str):
            raise SlackDenied(_DENIED_MESSAGE)
        if name.strip().casefold() != "content-type":
            continue
        if content_type is not None:
            raise SlackDenied(_DENIED_MESSAGE)
        candidate = value.strip()
        if not candidate:
            raise SlackDenied(_DENIED_MESSAGE)
        content_type = candidate
    if content_type is None:
        raise SlackDenied(_DENIED_MESSAGE)
    parts = [part.strip().casefold() for part in content_type.split(";")]
    if parts[0] != "application/x-www-form-urlencoded":
        raise SlackDenied(_DENIED_MESSAGE)
    for parameter in parts[1:]:
        if parameter != "charset=utf-8":
            raise SlackDenied(_DENIED_MESSAGE)


def _decode_form_component(value: str) -> str:
    if not isinstance(value, str):
        raise SlackDenied(_DENIED_MESSAGE)
    _validate_percent_escapes(value)
    from urllib.parse import unquote_to_bytes

    decoded = unquote_to_bytes(value.replace("+", " "))
    try:
        return decoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SlackDenied(_DENIED_MESSAGE) from None


def _require_command(value: object) -> str:
    command = _require_token(value, "command")
    if not command.startswith("/"):
        raise ValueError("command must start with '/'.")
    return command


def _require_token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be exact text.")
    return value


def _optional_token(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_token(value, field_name)


def _require_text_field(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("text must be exact text.")
    return value


def _slack_signature_message(*, body: bytes, timestamp: int) -> bytes:
    return b"v0:" + str(timestamp).encode("ascii") + b":" + body


def _validate_percent_escapes(value: str) -> None:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or _HEX_PAIR_PATTERN.fullmatch(
            value[index + 1 : index + 3]
        ) is None:
            raise SlackDenied(_DENIED_MESSAGE)
        index += 3


def _timestamp_to_utc(timestamp: int) -> datetime:
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise SlackDenied(_DENIED_MESSAGE)
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise SlackDenied(_DENIED_MESSAGE) from None


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("Slack signing secret must contain at least 32 bytes.")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise SlackDenied(_DENIED_MESSAGE)
