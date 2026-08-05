"""Private app-gateway authorization route."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from mim_control_plane.http_body import (
    preflight_bounded_http_body,
    read_bounded_http_body,
)
from mim_control_plane.security.google_machine_identity import MachineRequestDenied
from mim_control_plane.services.app_gateway_authorization import (
    AppAuthorizationDecision,
    AppAuthorizationRequest,
    AppGatewayAuthorizationDenied,
)

_MAX_REQUEST_BYTES = 4096
_GENERIC_MACHINE_DENIED = "Machine request was denied."
_GENERIC_APP_DENIED = "App request was denied."


class AppAuthorizationPort(Protocol):
    def authorize(
        self,
        request: AppAuthorizationRequest,
    ) -> AppAuthorizationDecision: ...


def mount_app_authorization_route(
    *,
    app: FastAPI,
    authenticator: object,
    expected_service_account_email: str,
    authorization_service: AppAuthorizationPort,
) -> None:
    @app.post("/v1/apps/authorize")
    async def authorize_app(request: Request) -> JSONResponse:
        preflight_bounded_http_body(
            request,
            max_bytes=_MAX_REQUEST_BYTES,
        )
        try:
            header_pairs = _header_pairs(request)
            _authenticate_machine_request(
                authenticator=authenticator,
                headers=header_pairs,
                expected_service_account_email=expected_service_account_email,
            )
            body = await read_bounded_http_body(
                request,
                max_bytes=_MAX_REQUEST_BYTES,
            )
            payload = _parse_exact_json(
                body,
                expected_keys=frozenset(
                    {
                        "schema",
                        "public_host",
                        "method",
                        "request_target",
                        "access_subject",
                        "access_email",
                        "edge_request_id",
                        "edge_timestamp",
                        "edge_body_sha256",
                    }
                ),
            )
            decision = authorization_service.authorize(
                AppAuthorizationRequest(
                    schema=_require_text(payload["schema"]),
                    public_host=_require_text(payload["public_host"]),
                    method=_require_text(payload["method"]),
                    request_target=_require_text(payload["request_target"]),
                    access_subject=_require_text(payload["access_subject"]),
                    access_email=_require_text(payload["access_email"]),
                    edge_request_id=_require_text(payload["edge_request_id"]),
                    edge_timestamp=_require_int(payload["edge_timestamp"]),
                    edge_body_sha256=_require_text(payload["edge_body_sha256"]),
                )
            )
        except MachineRequestDenied:
            raise HTTPException(
                status_code=403,
                detail=_GENERIC_MACHINE_DENIED,
            ) from None
        except (AppGatewayAuthorizationDenied, ValueError):
            raise HTTPException(status_code=404, detail=_GENERIC_APP_DENIED) from None
        return JSONResponse(
            {
                "schema": decision.schema,
                "public_host": decision.public_host,
                "workload_id": decision.workload_id,
                "upstream_url": decision.upstream_url,
                "upstream_audience": decision.upstream_audience,
                "expires_at": decision.expires_at.astimezone(UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        )


def _authenticate_machine_request(
    *,
    authenticator: object,
    headers: tuple[tuple[str, str], ...],
    expected_service_account_email: str,
) -> None:
    authenticate = getattr(authenticator, "authenticate", None)
    if not callable(authenticate):
        raise TypeError("machine authenticator is invalid")
    authenticate(
        headers,
        expected_service_account_email=expected_service_account_email,
    )


def _header_pairs(request: Request) -> tuple[tuple[str, str], ...]:
    raw_headers = request.scope.get("headers")
    if not isinstance(raw_headers, list):
        raise MachineRequestDenied(_GENERIC_MACHINE_DENIED)
    pairs: list[tuple[str, str]] = []
    for item in raw_headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            raise MachineRequestDenied(_GENERIC_MACHINE_DENIED)
        pairs.append((item[0].decode("latin-1"), item[1].decode("latin-1")))
    return tuple(pairs)


def _parse_exact_json(
    body: bytes,
    *,
    expected_keys: frozenset[str],
) -> Mapping[str, object]:
    if type(body) is not bytes or len(body) > _MAX_REQUEST_BYTES:
        raise ValueError("request body is invalid")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body is invalid") from exc
    if not isinstance(payload, dict) or frozenset(payload) != expected_keys:
        raise ValueError("request body is invalid")
    return payload


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("request body is invalid")
        result[key] = value
    return result


def _require_text(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("field is invalid")
    return value.strip()


def _require_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("field is invalid")
    return value
