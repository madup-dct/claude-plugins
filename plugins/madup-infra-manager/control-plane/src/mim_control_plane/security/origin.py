"""Fail-closed verification for Cloudflare Worker-to-origin requests."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

from mim_control_plane.domain.models import OriginRequestClaim, OriginRequestId
from mim_control_plane.ports.store import ReplayDetected, Store


class OriginDenied(PermissionError):
    """Raised when a request cannot prove trusted edge transit."""


_METHOD_PATTERN = re.compile(r"^[A-Z]{3,16}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_TARGET_PATTERN = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/?-]*$")
_CONTROL_PLANE_HOST = "mim.madup.app"
_DESTINATION_CLASS = "control-plane"


@dataclass(frozen=True, slots=True)
class OriginRequest:
    """Raw material signed by the edge Worker before origin forwarding."""

    method: str
    path: str
    body: bytes
    timestamp: datetime
    request_id: OriginRequestId
    public_host: str
    destination_class: str
    key_id: str
    signature: str | None


def body_sha256(body: bytes) -> str:
    """Return the lower-case digest bound into an origin signature."""

    if not isinstance(body, bytes):
        raise OriginDenied("Origin request was denied.")
    return hashlib.sha256(body).hexdigest()


def canonical_origin_message(request: OriginRequest) -> bytes:
    """Render the exact cross-language HMAC v2 message."""

    _validate_unsigned_request(request)
    fields = (
        "mim-origin-v2",
        request.destination_class,
        request.method,
        request.public_host,
        canonical_request_target(request.path).decode("ascii"),
        body_sha256(request.body),
        str(int(request.timestamp.timestamp())),
        str(request.request_id),
        request.key_id,
    )
    return "\n".join(fields).encode("utf-8")


def canonical_request_target(target: str) -> bytes:
    """Task 11 Worker parity contract for path + optional query bytes only."""

    if not isinstance(target, str) or not target:
        raise OriginDenied("Origin request was denied.")
    if target.startswith(("http://", "https://", "//")):
        raise OriginDenied("Origin request was denied.")
    if "\\" in target or "#" in target or "%" in target:
        raise OriginDenied("Origin request was denied.")
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in target):
        raise OriginDenied("Origin request was denied.")
    if target.count("?") > 1:
        raise OriginDenied("Origin request was denied.")
    if _REQUEST_TARGET_PATTERN.fullmatch(target) is None:
        raise OriginDenied("Origin request was denied.")
    return target.encode("ascii")


def sign_origin_request(request: OriginRequest, *, key: bytes) -> str:
    """Sign a canonical request for Worker fixtures and the JS contract test."""

    _validate_key(key)
    return hmac.new(key, canonical_origin_message(request), hashlib.sha256).hexdigest()


class OriginHmacVerifier:
    """Verify rotating Worker keys and atomically reject request replay."""

    def __init__(
        self,
        *,
        keys: Mapping[str, bytes],
        store: Store,
        clock: Callable[[], datetime],
        window: timedelta,
    ) -> None:
        if not keys:
            raise ValueError("At least one origin verification key is required.")
        if window <= timedelta(0):
            raise ValueError("Origin verification window must be positive.")
        copied: dict[str, bytes] = {}
        for key_id, key in keys.items():
            _validate_identifier(key_id)
            _validate_key(key)
            copied[key_id] = bytes(key)
        self._keys = MappingProxyType(copied)
        self._store = store
        self._clock = clock
        self._window = window

    def verify(self, request: OriginRequest) -> OriginRequestClaim:
        """Validate freshness and HMAC, then claim the request ID exactly once."""

        _validate_unsigned_request(request)
        signature = request.signature
        if signature is None or _SIGNATURE_PATTERN.fullmatch(signature) is None:
            raise OriginDenied("Origin request was denied.")

        now = self._clock()
        _require_utc(now)
        age = now - request.timestamp
        if age < timedelta(0) or age > self._window:
            raise OriginDenied("Origin request was denied.")

        key = self._keys.get(request.key_id)
        if key is None:
            raise OriginDenied("Origin request was denied.")
        expected = sign_origin_request(request, key=key)
        if not hmac.compare_digest(expected, signature):
            raise OriginDenied("Origin request was denied.")

        claim = OriginRequestClaim(
            request_id=request.request_id,
            body_hash=body_sha256(request.body),
            claimed_at=request.timestamp,
            expires_at=request.timestamp + self._window,
        )
        try:
            self._store.claim_origin_request(claim)
        except ReplayDetected:
            raise OriginDenied("Origin request was denied.") from None
        return claim


def _validate_unsigned_request(request: OriginRequest) -> None:
    if _METHOD_PATTERN.fullmatch(request.method) is None:
        raise OriginDenied("Origin request was denied.")
    canonical_request_target(request.path)
    if not isinstance(request.body, bytes):
        raise OriginDenied("Origin request was denied.")
    _require_utc(request.timestamp)
    if request.timestamp.microsecond != 0:
        raise OriginDenied("Origin request was denied.")
    _validate_identifier(str(request.request_id))
    _validate_public_host(request.public_host)
    _validate_destination_class(request.destination_class)
    _validate_identifier(request.key_id)


def _validate_identifier(value: str) -> None:
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise OriginDenied("Origin request was denied.")


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("Origin HMAC keys must contain at least 32 bytes.")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise OriginDenied("Origin request was denied.")


def _validate_public_host(value: str) -> None:
    if value != _CONTROL_PLANE_HOST:
        raise OriginDenied("Origin request was denied.")


def _validate_destination_class(value: str) -> None:
    if value != _DESTINATION_CLASS:
        raise OriginDenied("Origin request was denied.")
