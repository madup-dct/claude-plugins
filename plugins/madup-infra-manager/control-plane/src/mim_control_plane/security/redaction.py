"""Allowlist output schemas and fail-closed redaction helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class RedactionError(ValueError):
    """Raised when output falls outside the reviewed allowlist schema."""


class OutputSurface(StrEnum):
    API = "api"
    AUDIT = "audit"
    DASHBOARD = "dashboard"
    CLAUDE = "claude"


_ALLOWED_TOP_LEVEL_KEYS: Final[
    Mapping[OutputSurface, frozenset[str]]
] = MappingProxyType(
    {
        OutputSurface.API: frozenset(
            {
                "action",
                "correlation_id",
                "message",
                "plan_hash",
                "status",
                "summary",
                "target_ref",
            }
        ),
        OutputSurface.AUDIT: frozenset(
            {
                "action",
                "correlation_id",
                "message",
                "outcome",
                "plan_hash",
                "policy_decision",
                "summary",
                "target_ref",
            }
        ),
        OutputSurface.DASHBOARD: frozenset(
            {
                "action",
                "failure_class",
                "message",
                "plan_hash",
                "status",
                "summary",
                "target_ref",
            }
        ),
        OutputSurface.CLAUDE: frozenset(
            {
                "action",
                "failure_class",
                "message",
                "next_action",
                "plan_hash",
                "summary",
                "target_ref",
            }
        ),
    }
)
_SENSITIVE_KEY_PARTS: Final[tuple[str, ...]] = (
    "authorization",
    "cookie",
    "set-cookie",
    "secret",
    "token",
    "password",
    "api_key",
    "access_key",
    "refresh_token",
    "source_token",
    "headers",
    "raw_body",
    "body",
    "env",
    "environment",
)
_SENSITIVE_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]+"),
    re.compile(r"(?i)\b(?:authorization|cookie)\s*:\s*\S+"),
    re.compile(
        r"(?i)\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY)[A-Z0-9_]*\s*=\s*\S+"
    ),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]+\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+\b"),
)


def sanitize_output(
    surface: OutputSurface,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Return a reviewed surface payload with unknown and sensitive material removed."""

    allowed = _ALLOWED_TOP_LEVEL_KEYS[surface]
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise RedactionError("output contains fields outside the reviewed allowlist.")
    sanitized: dict[str, object] = {}
    for key, value in payload.items():
        if _is_sensitive_key(key):
            continue
        sanitized_value = _sanitize_value(value)
        if sanitized_value is _OMIT:
            continue
        sanitized[key] = sanitized_value
    return sanitized


def flatten_summary(summary: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    """Convert a sanitized summary mapping into deterministic audit pairs."""

    flattened: list[tuple[str, str]] = []
    _flatten_value("", summary, flattened)
    return tuple(sorted(flattened))


_OMIT = object()


def _sanitize_value(value: object) -> object:
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for key, inner_value in value.items():
            if not isinstance(key, str) or _is_sensitive_key(key):
                continue
            cleaned = _sanitize_value(inner_value)
            if cleaned is _OMIT:
                continue
            sanitized[key] = cleaned
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sanitized_items = [
            item for raw in value if (item := _sanitize_value(raw)) is not _OMIT
        ]
        return sanitized_items
    if isinstance(value, (bytes, bytearray)):
        raise RedactionError("output must contain JSON-like values only.")
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _sanitize_text(value)
    raise RedactionError("output must contain JSON-like values only.")


def _sanitize_text(value: str) -> str:
    text = value
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    return any(
        part in lowered or part.replace("_", "") in compact
        for part in _SENSITIVE_KEY_PARTS
    )


def _flatten_value(
    prefix: str,
    value: object,
    flattened: list[tuple[str, str]],
) -> None:
    if isinstance(value, Mapping):
        for key in sorted(value):
            next_prefix = key if not prefix else f"{prefix}.{key}"
            _flatten_value(next_prefix, value[key], flattened)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            next_prefix = f"{prefix}[{index}]"
            _flatten_value(next_prefix, item, flattened)
        return
    flattened.append((prefix, "" if value is None else str(value)))
