"""Authenticated browser handoff for raw secret submission."""

from __future__ import annotations

import base64
import hashlib
import html
from typing import Protocol
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from mim_control_plane.api import (
    _mutation_gate_from_environment,
    _path_with_query,
    _require_exact_bool,
    authorize_api_request,
    build_authentication_request,
)
from mim_control_plane.domain.central_identity import ActionIntent, ActionName
from mim_control_plane.http_body import read_bounded_http_body
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.services.central_identity import (
    CentralIdentityDenied,
    CentralIdentityGateway,
)

_DEFAULT_HANDOFF_PATH = "/v1/secrets/handoff"
_INVALID_REQUEST = "Invalid request."
_GENERIC_FORBIDDEN = "Identity is not authorized for MIM."
_GENERIC_SECRET_DENIED = "Secret request was denied."
_SAME_ORIGIN_HEADER = "X-MIM-Secret-Handoff"
_SAME_ORIGIN_VALUE = "same-origin"
_MAX_SECRET_BYTES = 16 * 1024
_HANDOFF_QUERY_KEYS = ("plan_id", "plan_hash", "idempotency_key")
_APPLY_RESULT_KEYS = frozenset(
    {
        "action",
        "operation_id",
        "secret_id",
        "mode",
        "active_version",
        "rotation_state",
        "retiring_version",
        "attached_workload_ids",
        "replayed",
    }
)


class SecretManagementPort(Protocol):
    def apply_secret_plan(
        self,
        *,
        principal: AuthenticatedPrincipal,
        plan_id: str,
        plan_hash: str,
        idempotency_key: str,
        payload: bytes | None = None,
    ) -> dict[str, object]: ...


def build_secret_router(
    *,
    gateway: CentralIdentityGateway,
    secret_management: SecretManagementPort | None = None,
    mutations_enabled: bool | None = None,
    handoff_path: str = _DEFAULT_HANDOFF_PATH,
) -> APIRouter:
    enable_mutations = (
        _mutation_gate_from_environment()
        if mutations_enabled is None
        else _require_exact_bool(mutations_enabled)
    )
    normalized_path = _require_handoff_path(handoff_path)
    if enable_mutations and secret_management is None:
        raise ValueError("Secret mutation dependencies must be configured together.")
    router = APIRouter()

    @router.get(normalized_path, response_class=HTMLResponse)
    async def get_secret_handoff(request: Request) -> HTMLResponse:
        if not enable_mutations or secret_management is None:
            raise HTTPException(status_code=404, detail="Not found.")
        params = _parse_handoff_query(request)
        try:
            await authorize_api_request(
                gateway=gateway,
                request=request,
                action=ActionName.DEPLOY_WORKLOAD,
                resource_id_factory=lambda _principal: (
                    f"deployment-plan:{params['plan_id']}"
                ),
            )
        except HTTPException as exc:
            if exc.status_code == 403:
                raise HTTPException(
                    status_code=403,
                    detail=_GENERIC_FORBIDDEN,
                ) from None
            raise
        return HTMLResponse(
            _render_secret_handoff_html(params),
            headers=_security_headers(_secret_handoff_script()),
        )

    @router.post(normalized_path)
    async def post_secret_handoff(request: Request) -> JSONResponse:
        if not enable_mutations or secret_management is None:
            raise HTTPException(status_code=404, detail="Not found.")
        params = _parse_handoff_query(request)
        _require_handoff_header(request)
        _require_content_headers(request)
        body = await read_bounded_http_body(
            request,
            max_bytes=_MAX_SECRET_BYTES,
        )
        if not body:
            raise HTTPException(status_code=400, detail=_INVALID_REQUEST)
        try:
            authorized = gateway.authorize_browser_for(
                authentication_request=build_authentication_request(
                    method=request.method,
                    path=_path_with_query(request),
                    body=body,
                    headers=_raw_auth_headers_without_handoff(request),
                ),
                intent_factory=lambda principal: ActionIntent(
                    action=ActionName.DEPLOY_WORKLOAD,
                    resource_id=f"deployment-plan:{params['plan_id']}",
                ),
            )
        except (ValueError, CentralIdentityDenied):
            raise HTTPException(status_code=403, detail=_GENERIC_FORBIDDEN) from None
        try:
            result = _require_canonical_apply_result(
                secret_management.apply_secret_plan(
                    principal=authorized.principal,
                    plan_id=params["plan_id"],
                    plan_hash=params["plan_hash"],
                    idempotency_key=params["idempotency_key"],
                    payload=body,
                )
            )
        except (PermissionError, ValueError):
            raise HTTPException(
                status_code=409,
                detail=_GENERIC_SECRET_DENIED,
            ) from None
        return JSONResponse(result, headers=_post_headers())

    return router


def build_secret_handoff_path(
    *,
    plan_id: str,
    plan_hash: str,
    idempotency_key: str,
    handoff_path: str = _DEFAULT_HANDOFF_PATH,
) -> str:
    query = urlencode(
        (
            ("plan_id", plan_id),
            ("plan_hash", plan_hash),
            ("idempotency_key", idempotency_key),
        )
    )
    return f"{_require_handoff_path(handoff_path)}?{query}"


def _parse_handoff_query(request: Request) -> dict[str, str]:
    raw_query = request.scope["query_string"].decode("ascii")
    try:
        items = parse_qsl(
            raw_query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail=_INVALID_REQUEST) from None
    if len(items) != len(_HANDOFF_QUERY_KEYS):
        raise HTTPException(status_code=400, detail=_INVALID_REQUEST)
    values: dict[str, str] = {}
    for key, value in items:
        if key not in _HANDOFF_QUERY_KEYS or key in values:
            raise HTTPException(status_code=400, detail=_INVALID_REQUEST)
        if not value:
            raise HTTPException(status_code=400, detail=_INVALID_REQUEST)
        values[key] = value
    if tuple(values) != _HANDOFF_QUERY_KEYS:
        raise HTTPException(status_code=400, detail=_INVALID_REQUEST)
    return values


def _require_handoff_header(request: Request) -> None:
    values = request.headers.getlist(_SAME_ORIGIN_HEADER)
    if len(values) != 1 or values[0] != _SAME_ORIGIN_VALUE:
        raise HTTPException(status_code=400, detail=_INVALID_REQUEST)


def _raw_auth_headers_without_handoff(request: Request) -> tuple[tuple[str, str], ...]:
    raw_headers = request.scope.get("headers")
    if not isinstance(raw_headers, list):
        raise ValueError("Identity is not authorized for MIM.")
    pairs: list[tuple[str, str]] = []
    for item in raw_headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, bytes) for part in item)
        ):
            raise ValueError("Identity is not authorized for MIM.")
        name = item[0].decode("ascii")
        if name.casefold() == _SAME_ORIGIN_HEADER.casefold():
            continue
        pairs.append((name, item[1].decode("ascii")))
    return tuple(pairs)


def _require_canonical_apply_result(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != _APPLY_RESULT_KEYS:
        raise ValueError("Secret result was denied.")
    if value.get("action") != "apply_secret_plan":
        raise ValueError("Secret result was denied.")
    if value.get("mode") not in {"attach", "create", "rotate"}:
        raise ValueError("Secret result was denied.")
    if type(value.get("replayed")) is not bool:
        raise ValueError("Secret result was denied.")
    if type(value.get("active_version")) is not int:
        raise ValueError("Secret result was denied.")
    retiring_version = value.get("retiring_version")
    if retiring_version is not None and type(retiring_version) is not int:
        raise ValueError("Secret result was denied.")
    workload_ids = value.get("attached_workload_ids")
    if not isinstance(workload_ids, tuple) or any(
        type(item) is not str or not item for item in workload_ids
    ):
        raise ValueError("Secret result was denied.")
    for field_name in ("operation_id", "secret_id", "rotation_state"):
        field = value.get(field_name)
        if type(field) is not str or not field:
            raise ValueError("Secret result was denied.")
    return dict(value)


def _require_content_headers(request: Request) -> None:
    content_type = request.headers.getlist("Content-Type")
    if len(content_type) != 1:
        raise HTTPException(status_code=400, detail=_INVALID_REQUEST)
    if content_type[0].strip().casefold() != "application/octet-stream":
        raise HTTPException(status_code=400, detail=_INVALID_REQUEST)
    if request.headers.getlist("Content-Encoding"):
        raise HTTPException(status_code=400, detail=_INVALID_REQUEST)


def _require_handoff_path(path: str) -> str:
    text = path.strip()
    if not text.startswith("/") or "?" in text:
        raise ValueError("handoff_path must be an absolute query-free path.")
    return text


def _render_secret_handoff_html(params: dict[str, str]) -> str:
    script = _secret_handoff_script()
    plan_id = html.escape(params["plan_id"], quote=True)
    plan_hash = html.escape(params["plan_hash"], quote=True)
    idempotency_key = html.escape(params["idempotency_key"], quote=True)
    fields = "".join(
        [
            '<label for="plan_id">plan_id</label>',
            (
                '<input id="plan_id" name="plan_id" type="text" readonly '
                f'value="{plan_id}" />'
            ),
            '<label for="plan_hash">plan_hash</label>',
            (
                '<input id="plan_hash" name="plan_hash" type="text" readonly '
                f'value="{plan_hash}" />'
            ),
            '<label for="idempotency_key">idempotency_key</label>',
            (
                '<input id="idempotency_key" name="idempotency_key" type="text" '
                f'readonly value="{idempotency_key}" />'
            ),
        ]
    )
    return "".join(
        [
            "<!doctype html>",
            "<html lang=\"en\">",
            "<head>",
            "<meta charset=\"utf-8\" />",
            (
                "<meta name=\"viewport\" "
                "content=\"width=device-width, initial-scale=1\" />"
            ),
            "<meta name=\"referrer\" content=\"no-referrer\" />",
            "<title>MIM Secret Handoff</title>",
            "</head>",
            "<body>",
            "<main>",
            "<h1>MIM Secret Handoff</h1>",
            "<p>Submit the raw secret once for the reviewed MIM plan.</p>",
            (
                "<form id=\"secret-form\" method=\"post\" action=\""
                f"{html.escape(_path_for_html(params), quote=True)}"
                "\">"
            ),
            fields,
            "<label for=\"secret-value\">secret_value</label>",
            (
                "<input id=\"secret-value\" name=\"secret_value\" "
                "type=\"password\" autocomplete=\"off\" />"
            ),
            "<button type=\"submit\">Submit</button>",
            "</form>",
            "<pre id=\"status\" aria-live=\"polite\"></pre>",
            f"<script>{script}</script>",
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def _path_for_html(params: dict[str, str]) -> str:
    return build_secret_handoff_path(
        plan_id=params["plan_id"],
        plan_hash=params["plan_hash"],
        idempotency_key=params["idempotency_key"],
    )


def _secret_handoff_script() -> str:
    return "".join(
        [
            "(() => {",
            "const form = document.getElementById('secret-form');",
            "const field = document.getElementById('secret-value');",
            "const status = document.getElementById('status');",
            "form.addEventListener('submit', async (event) => {",
            "event.preventDefault();",
            "if (!(field instanceof HTMLInputElement) || field.value.length === 0) {",
            "status.textContent = 'Secret is required.';",
            "return;",
            "}",
            "status.textContent = 'Submitting...';",
            (
                "const response = await fetch("
                "window.location.pathname + window.location.search, {"
            ),
            "method: 'POST',",
            "headers: {",
            f"'{_SAME_ORIGIN_HEADER}': '{_SAME_ORIGIN_VALUE}',",
            "'Content-Type': 'application/octet-stream'",
            "},",
            "body: new TextEncoder().encode(field.value),",
            "cache: 'no-store',",
            "credentials: 'same-origin'",
            "});",
            "const payload = await response.json();",
            "status.textContent = JSON.stringify(payload);",
            "field.value = '';",
            "});",
            "})();",
        ]
    )


def _security_headers(script: str) -> dict[str, str]:
    digest = hashlib.sha256(script.encode("utf-8")).digest()
    script_hash = base64.b64encode(digest).decode("ascii")
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "form-action 'self'; "
            "connect-src 'self'; "
            f"script-src 'sha256-{script_hash}'"
        ),
        "X-Content-Type-Options": "nosniff",
    }


def _post_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
