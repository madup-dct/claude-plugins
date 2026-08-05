"""Fail-closed bounded request-body reads for HTTP trust boundaries."""

from __future__ import annotations

from fastapi import HTTPException, Request

_INVALID_REQUEST = "Invalid request."
_PAYLOAD_TOO_LARGE = "Payload too large."


class InvalidRequestBody(ValueError):
    """Raised when the request framing or body stream is invalid."""


class RequestBodyTooLarge(InvalidRequestBody):
    """Raised when the declared or observed body exceeds its route limit."""


async def read_bounded_request_body(
    request: Request,
    *,
    max_bytes: int,
) -> bytes:
    """Read at most ``max_bytes`` while rejecting ambiguous HTTP framing."""

    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    declared_length = _declared_content_length(request)
    if declared_length is not None and declared_length > max_bytes:
        raise RequestBodyTooLarge("request body is too large")

    chunks: list[bytes] = []
    observed_length = 0
    try:
        async for chunk in request.stream():
            if type(chunk) is not bytes:
                raise InvalidRequestBody("request body is invalid")
            observed_length += len(chunk)
            if observed_length > max_bytes:
                raise RequestBodyTooLarge("request body is too large")
            chunks.append(chunk)
    except (InvalidRequestBody, RequestBodyTooLarge):
        raise
    except Exception:
        raise InvalidRequestBody("request body is invalid") from None

    if declared_length is not None and observed_length != declared_length:
        raise InvalidRequestBody("request body length does not match framing")
    return b"".join(chunks)


async def read_bounded_http_body(
    request: Request,
    *,
    max_bytes: int,
) -> bytes:
    """Read a bounded body and expose only generic HTTP framing errors."""

    try:
        return await read_bounded_request_body(request, max_bytes=max_bytes)
    except RequestBodyTooLarge:
        raise HTTPException(status_code=413, detail=_PAYLOAD_TOO_LARGE) from None
    except InvalidRequestBody:
        raise HTTPException(status_code=400, detail=_INVALID_REQUEST) from None


def preflight_bounded_http_body(
    request: Request,
    *,
    max_bytes: int,
) -> None:
    """Validate declared framing before any body bytes are read."""

    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    try:
        declared_length = _declared_content_length(request)
    except InvalidRequestBody:
        raise HTTPException(status_code=400, detail=_INVALID_REQUEST) from None
    if declared_length is not None and declared_length > max_bytes:
        raise HTTPException(status_code=413, detail=_PAYLOAD_TOO_LARGE)


def _declared_content_length(request: Request) -> int | None:
    raw_headers = request.scope.get("headers")
    if not isinstance(raw_headers, list):
        raise InvalidRequestBody("request headers are invalid")
    values: list[bytes] = []
    has_transfer_encoding = False
    for item in raw_headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or type(item[0]) is not bytes
            or type(item[1]) is not bytes
        ):
            raise InvalidRequestBody("request headers are invalid")
        if item[0].lower() == b"content-length":
            values.append(item[1])
        elif item[0].lower() == b"transfer-encoding":
            has_transfer_encoding = True
    if not values:
        return None
    if has_transfer_encoding:
        raise InvalidRequestBody("request body framing is ambiguous")
    if len(values) != 1:
        raise InvalidRequestBody("content length is ambiguous")
    raw_value = values[0]
    if not raw_value or any(byte < 48 or byte > 57 for byte in raw_value):
        raise InvalidRequestBody("content length is invalid")
    return int(raw_value)
