"""Authenticated HTTP boundary for the MIM MCP surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mim_control_plane.api import _path_with_query, build_authentication_request
from mim_control_plane.dashboard import (
    ADMIN_OVERVIEW_RESOURCE_ID,
    dashboard_resource_id,
)
from mim_control_plane.domain.central_identity import ActionIntent, ActionName
from mim_control_plane.domain.states import UserRole
from mim_control_plane.http_body import (
    preflight_bounded_http_body,
    read_bounded_http_body,
)
from mim_control_plane.mcp import _PREAUTHORIZED_BROWSER_SCOPE_KEY
from mim_control_plane.services.central_identity import (
    CentralIdentityDenied,
    CentralIdentityGateway,
)

_GENERIC_FORBIDDEN = "Identity is not authorized for MIM."
_MAX_MCP_REQUEST_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class _BufferedBody:
    body: bytes

    def receive(self) -> Any:
        sent = False

        async def _receive() -> dict[str, object]:
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": self.body, "more_body": False}

        return _receive


class AuthenticatedMcpHttpApp:
    def __init__(
        self,
        *,
        gateway: CentralIdentityGateway,
        inner_app: Any,
        max_body_bytes: int,
    ) -> None:
        if type(max_body_bytes) is not int or max_body_bytes < 1:
            raise ValueError("max_body_bytes must be a positive integer.")
        self._gateway = gateway
        self._inner_app = inner_app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict[str, object], receive: Any, send: Any) -> None:
        try:
            state = _copy_scope_state(scope)
            incoming_request = Request(scope, receive)
            preflight_bounded_http_body(
                incoming_request,
                max_bytes=self._max_body_bytes,
            )
            build_authentication_request(
                method=incoming_request.method,
                path=_path_with_query(incoming_request),
                body=b"",
                headers=_raw_headers(scope),
            )
            body = await read_bounded_http_body(
                incoming_request,
                max_bytes=self._max_body_bytes,
            )
            request = Request(scope, _BufferedBody(body).receive())
            authorized = self._gateway.authorize_browser_for(
                authentication_request=build_authentication_request(
                    method=request.method,
                    path=_path_with_query(request),
                    body=body,
                    headers=_raw_headers(scope),
                ),
                intent_factory=lambda principal: ActionIntent(
                    action=ActionName.VIEW_DASHBOARD,
                    resource_id=(
                        ADMIN_OVERVIEW_RESOURCE_ID
                        if principal.role is UserRole.ADMIN
                        else dashboard_resource_id(principal.user_id)
                    ),
                ),
            )
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
            )
            await response(scope, _BufferedBody(b"").receive(), send)
            return
        except (CentralIdentityDenied, PermissionError, ValueError):
            response = JSONResponse(
                {"detail": _GENERIC_FORBIDDEN},
                status_code=403,
            )
            await response(scope, _BufferedBody(b"").receive(), send)
            return

        state[_PREAUTHORIZED_BROWSER_SCOPE_KEY] = authorized
        delegated_scope = dict(scope)
        delegated_scope["state"] = state
        await self._inner_app(
            delegated_scope,
            _BufferedBody(body).receive(),
            send,
        )


def build_mcp_http_app(
    *,
    fastmcp: FastMCP,
    gateway: CentralIdentityGateway,
    max_body_bytes: int = _MAX_MCP_REQUEST_BYTES,
) -> Starlette:
    if fastmcp.settings.streamable_http_path != "/mcp":
        raise ValueError("MCP HTTP path must remain exact /mcp.")
    if fastmcp.settings.stateless_http is not True:
        raise ValueError("MCP HTTP must run in stateless mode.")
    if fastmcp.settings.json_response is not True:
        raise ValueError("MCP HTTP must return exact JSON responses.")

    inner_app = fastmcp.streamable_http_app()
    route = _streamable_route(inner_app, path=fastmcp.settings.streamable_http_path)
    wrapped = AuthenticatedMcpHttpApp(
        gateway=gateway,
        inner_app=route.endpoint,
        max_body_bytes=max_body_bytes,
    )
    return Starlette(
        debug=inner_app.debug,
        routes=[Route(route.path, endpoint=wrapped)],
        middleware=inner_app.user_middleware,
        lifespan=inner_app.router.lifespan_context,
    )


def _streamable_route(app: Starlette, *, path: str) -> Route:
    routes = [
        route
        for route in app.routes
        if isinstance(route, Route) and route.path == path
    ]
    if len(routes) != 1:
        raise ValueError("MCP HTTP route must expose exactly one /mcp endpoint.")
    return routes[0]


def _raw_headers(scope: dict[str, object]) -> tuple[tuple[str, str], ...]:
    headers = scope.get("headers")
    if not isinstance(headers, list):
        raise ValueError("Identity is not authorized for MIM.")
    pairs: list[tuple[str, str]] = []
    for item in headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            raise ValueError("Identity is not authorized for MIM.")
        pairs.append((item[0].decode("latin-1"), item[1].decode("latin-1")))
    return tuple(pairs)


def _copy_scope_state(scope: dict[str, object]) -> dict[str, object]:
    state_value = scope.get("state")
    if state_value is None:
        return {}
    if not isinstance(state_value, Mapping):
        raise ValueError("Identity is not authorized for MIM.")
    return dict(state_value.items())
