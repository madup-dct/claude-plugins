"""Authenticated API surface for reviewed MIM control-plane actions."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from mim_control_plane.adapters.github import (
    MAX_WEBHOOK_BODY_BYTES as _MAX_GITHUB_WEBHOOK_BODY_BYTES,
)
from mim_control_plane.dashboard import (
    ADMIN_OVERVIEW_RESOURCE_ID,
    ControlPlaneReadService,
    ReadNotFound,
    dashboard_resource_id,
    failure_resource_id,
    operation_resource_id,
    usage_resource_id,
)
from mim_control_plane.domain.central_identity import ActionIntent, ActionName
from mim_control_plane.domain.models import OriginRequestId
from mim_control_plane.domain.states import ActivityOutcome, ActivitySurface, UserRole
from mim_control_plane.http_body import (
    preflight_bounded_http_body,
    read_bounded_http_body,
)
from mim_control_plane.jobs.maintenance_common import hash_browser_request_id
from mim_control_plane.ports.store import Store
from mim_control_plane.security.identity import (
    AuthenticatedPrincipal,
    AuthenticationRequest,
)
from mim_control_plane.security.origin import (
    OriginDenied,
    OriginHmacVerifier,
    OriginRequest,
)
from mim_control_plane.services.central_identity import (
    AuthorizedAction,
    CentralIdentityDenied,
    CentralIdentityGateway,
)
from mim_control_plane.services.deployments import DeploymentDenied, DeploymentService
from mim_control_plane.services.schedule_management import (
    ScheduleDenied,
    ScheduleManagementService,
)
from mim_control_plane.services.usage import ActivityAction, ingest_activity_event

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_GENERIC_FORBIDDEN = "Identity is not authorized for MIM."
_GENERIC_DEPLOYMENT_DENIED = "Deployment request was denied."
_GENERIC_SCHEDULE_DENIED = "Schedule request was denied."
_MAX_DEPLOY_REQUEST_BYTES = 4096
_MAX_CONTROL_REQUEST_BYTES = 16 * 1024
_REQUIRED_BROWSER_AUTH_HEADERS = (
    "x-mim-origin-key-id",
    "x-mim-origin-timestamp",
    "x-mim-origin-request-id",
    "x-mim-origin-public-host",
    "x-mim-origin-destination-class",
    "x-mim-origin-signature",
    "cf-access-jwt-assertion",
)
_FORBIDDEN_BROWSER_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "forwarded",
        "proxy-authorization",
        "set-cookie",
        "true-client-ip",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-proto",
        "x-real-ip",
    }
)


def build_api_app(
    *,
    store: Store,
    gateway: CentralIdentityGateway,
    clock: Callable[[], datetime],
    deployment_service: DeploymentService | None = None,
    github_origin_verifier: OriginHmacVerifier | None = None,
    mutations_enabled: bool | None = None,
    schedule_management: ScheduleManagementService | None = None,
    readiness_check: Callable[[], None] | None = None,
) -> FastAPI:
    enable_mutations = (
        _mutation_gate_from_environment()
        if mutations_enabled is None
        else _require_exact_bool(mutations_enabled)
    )
    if enable_mutations and (
        deployment_service is None or github_origin_verifier is None
    ):
        raise ValueError(
            "Deployment mutation dependencies must be configured together."
        )
    if (deployment_service is None) != (github_origin_verifier is None):
        raise ValueError(
            "Deployment mutation dependencies must be configured together."
        )
    service = ControlPlaneReadService(
        store=store,
        clock=clock,
        deployment_planner=deployment_service,
    )
    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.VIEW_DASHBOARD,
            resource_id_factory=lambda principal: (
                ADMIN_OVERVIEW_RESOURCE_ID
                if principal.role is UserRole.ADMIN
                else dashboard_resource_id(principal.user_id)
            ),
        )
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.VIEW_DASHBOARD,
            resource_id_factory=lambda principal: (
                ADMIN_OVERVIEW_RESOURCE_ID
                if principal.role is UserRole.ADMIN
                else dashboard_resource_id(principal.user_id)
            ),
        )
        if readiness_check is not None:
            try:
                readiness_check()
            except Exception:
                return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/static/{asset_name}")
    async def static_asset(asset_name: str, request: Request) -> FileResponse:
        await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.VIEW_DASHBOARD,
            resource_id_factory=lambda principal: (
                ADMIN_OVERVIEW_RESOURCE_ID
                if principal.role is UserRole.ADMIN
                else dashboard_resource_id(principal.user_id)
            ),
        )
        if asset_name not in {"dashboard.css", "dashboard.js"}:
            raise HTTPException(status_code=404, detail="Not found.")
        return FileResponse(_STATIC_DIR / asset_name)

    @app.get("/v1/workloads")
    async def list_workloads(request: Request) -> JSONResponse:
        authorized = await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.VIEW_DASHBOARD,
            resource_id_factory=lambda principal: (
                ADMIN_OVERVIEW_RESOURCE_ID
                if principal.role is UserRole.ADMIN
                else dashboard_resource_id(principal.user_id)
            ),
        )
        return JSONResponse(service.list_workloads(principal=authorized.principal))

    @app.get("/v1/operations/{operation_id}")
    async def get_operation(operation_id: str, request: Request) -> JSONResponse:
        authorized = await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.VIEW_DASHBOARD,
            resource_id_factory=lambda _principal: operation_resource_id(operation_id),
        )
        try:
            payload = service.get_operation(
                principal=authorized.principal,
                operation_id=operation_id,
            )
        except ReadNotFound:
            raise HTTPException(status_code=404, detail="Not found.") from None
        return JSONResponse(payload)

    @app.get("/v1/usage")
    async def get_usage(request: Request) -> JSONResponse:
        authorized = await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.VIEW_USAGE,
            resource_id_factory=lambda principal: (
                ADMIN_OVERVIEW_RESOURCE_ID
                if principal.role is UserRole.ADMIN
                else usage_resource_id(principal.user_id)
            ),
            admin_action=ActionName.ADMIN_USAGE_OVERVIEW,
        )
        return JSONResponse(service.get_usage(principal=authorized.principal))

    @app.get("/v1/plan/deploy")
    async def plan_deploy(
        request: Request,
        workload_id: str | None = None,
    ) -> JSONResponse:
        authorized = await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.DEPLOY_WORKLOAD,
            resource_id_factory=lambda principal: (
                f"workload:{workload_id}"
                if workload_id is not None
                else (
                    ADMIN_OVERVIEW_RESOURCE_ID
                    if principal.role is UserRole.ADMIN
                    else dashboard_resource_id(principal.user_id)
                )
            ),
        )
        try:
            payload = service.plan_deploy(
                principal=authorized.principal,
                workload_id=workload_id,
            )
        except DeploymentDenied:
            raise HTTPException(
                status_code=409,
                detail=_GENERIC_DEPLOYMENT_DENIED,
            ) from None
        return JSONResponse(payload)

    @app.post("/v1/deployments")
    async def deploy_from_plan(request: Request) -> JSONResponse:
        if not enable_mutations:
            raise HTTPException(status_code=404, detail="Not found.")
        assert deployment_service is not None
        preflight_bounded_http_body(
            request,
            max_bytes=_MAX_DEPLOY_REQUEST_BYTES,
        )
        _require_browser_authenticated_request_headers(request)
        body = await read_bounded_http_body(
            request,
            max_bytes=_MAX_DEPLOY_REQUEST_BYTES,
        )
        try:
            payload = _parse_deployment_request(body)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request.") from None
        authorized = await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.DEPLOY_WORKLOAD,
            resource_id_factory=lambda _principal: (
                f"deployment-plan:{payload['plan_id']}"
            ),
            authenticated_body=body,
        )
        try:
            result = deployment_service.deploy_from_plan(
                principal=authorized.principal,
                plan_id=payload["plan_id"],
                plan_hash=payload["plan_hash"],
                idempotency_key=payload["idempotency_key"],
                correlation_id=payload["correlation_id"],
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request.") from None
        except DeploymentDenied:
            raise HTTPException(
                status_code=409,
                detail=_GENERIC_DEPLOYMENT_DENIED,
            ) from None
        return JSONResponse(result, status_code=202)

    @app.post("/v1/webhooks/github")
    async def github_webhook(request: Request) -> JSONResponse:
        if not enable_mutations:
            raise HTTPException(status_code=404, detail="Not found.")
        assert deployment_service is not None
        assert github_origin_verifier is not None
        try:
            github_headers = _exact_github_headers(request)
            body = await read_bounded_http_body(
                request,
                max_bytes=_MAX_GITHUB_WEBHOOK_BODY_BYTES,
            )
            _authorize_github_machine_origin(
                request=request,
                body=body,
                verifier=github_origin_verifier,
            )
            result = deployment_service.deploy_from_github_webhook(
                body=body,
                signature_header=github_headers["x-hub-signature-256"],
                event_name=github_headers["x-github-event"],
                delivery_id=github_headers["x-github-delivery"],
            )
        except (DeploymentDenied, OriginDenied, ValueError):
            raise HTTPException(status_code=403, detail="Webhook denied.") from None
        return JSONResponse(result, status_code=202)

    @app.get("/v1/plan/schedule")
    async def plan_schedule(
        request: Request,
        workload_id: str | None = None,
    ) -> JSONResponse:
        if workload_id is None:
            raise HTTPException(status_code=400, detail="Invalid request.")
        if schedule_management is None:
            raise HTTPException(
                status_code=503,
                detail="Schedule dependencies are not configured.",
            )
        authorized = await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.MANAGE_SCHEDULE,
            resource_id_factory=lambda _principal: f"workload:{workload_id}",
        )
        try:
            payload = schedule_management.plan_schedule(
                principal=authorized.principal,
                workload_id=workload_id,
            )
        except ScheduleDenied:
            raise HTTPException(
                status_code=409,
                detail=_GENERIC_SCHEDULE_DENIED,
            ) from None
        return JSONResponse(payload)

    @app.post("/v1/schedules")
    async def create_schedule_from_plan(request: Request) -> JSONResponse:
        if not enable_mutations:
            raise HTTPException(status_code=404, detail="Not found.")
        if schedule_management is None:
            raise HTTPException(
                status_code=503,
                detail="Schedule dependencies are not configured.",
            )
        preflight_bounded_http_body(
            request,
            max_bytes=_MAX_DEPLOY_REQUEST_BYTES,
        )
        _require_browser_authenticated_request_headers(request)
        body = await read_bounded_http_body(
            request,
            max_bytes=_MAX_DEPLOY_REQUEST_BYTES,
        )
        try:
            payload = _parse_schedule_creation_request(body)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request.") from None
        authorized = await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.MANAGE_SCHEDULE,
            resource_id_factory=lambda _principal: (
                f"deployment-plan:{payload['plan_id']}"
            ),
            authenticated_body=body,
        )
        try:
            result = schedule_management.create_schedule_from_plan(
                principal=authorized.principal,
                plan_id=payload["plan_id"],
                plan_hash=payload["plan_hash"],
                idempotency_key=payload["idempotency_key"],
            )
        except ScheduleDenied:
            raise HTTPException(
                status_code=409,
                detail=_GENERIC_SCHEDULE_DENIED,
            ) from None
        return JSONResponse(result, status_code=202)

    @app.post("/v1/schedules/{schedule_id}/pause")
    async def pause_schedule(schedule_id: str, request: Request) -> JSONResponse:
        return await _change_schedule_state(
            request=request,
            schedule_id=schedule_id,
            action="pause",
        )

    @app.post("/v1/schedules/{schedule_id}/resume")
    async def resume_schedule(schedule_id: str, request: Request) -> JSONResponse:
        return await _change_schedule_state(
            request=request,
            schedule_id=schedule_id,
            action="resume",
        )

    async def _change_schedule_state(
        *,
        request: Request,
        schedule_id: str,
        action: str,
    ) -> JSONResponse:
        if not enable_mutations:
            raise HTTPException(status_code=404, detail="Not found.")
        if schedule_management is None:
            raise HTTPException(
                status_code=503,
                detail="Schedule dependencies are not configured.",
            )
        authorized = await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.MANAGE_SCHEDULE,
            resource_id_factory=lambda _principal: f"schedule:{schedule_id}",
        )
        try:
            if action == "pause":
                result = schedule_management.pause_schedule(
                    principal=authorized.principal,
                    schedule_id=schedule_id,
                )
            elif action == "resume":
                result = schedule_management.resume_schedule(
                    principal=authorized.principal,
                    schedule_id=schedule_id,
                )
            else:  # pragma: no cover - closed internal call surface
                raise ValueError("invalid schedule action")
        except ScheduleDenied:
            raise HTTPException(
                status_code=409,
                detail=_GENERIC_SCHEDULE_DENIED,
            ) from None
        return JSONResponse(result)

    @app.get("/v1/failures/{operation_id}")
    async def get_failure(operation_id: str, request: Request) -> JSONResponse:
        authorized = await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.VIEW_DASHBOARD,
            resource_id_factory=lambda _principal: failure_resource_id(operation_id),
        )
        try:
            payload = service.get_failure(
                principal=authorized.principal,
                operation_id=operation_id,
            )
        except ReadNotFound:
            raise HTTPException(status_code=404, detail="Not found.") from None
        return JSONResponse(payload)

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        authorized = await authorize_api_request(
            gateway=gateway,
            request=request,
            action=ActionName.VIEW_DASHBOARD,
            resource_id_factory=lambda principal: (
                ADMIN_OVERVIEW_RESOURCE_ID
                if principal.role is UserRole.ADMIN
                else dashboard_resource_id(principal.user_id)
            ),
        )
        _record_dashboard_activity_event(
            store=store,
            principal=authorized.principal,
            request=request,
            now=clock(),
        )
        page = service.render_dashboard(principal=authorized.principal)
        return HTMLResponse(page.html)

    return app


async def authorize_api_request(
    *,
    gateway: CentralIdentityGateway,
    request: Request,
    action: ActionName,
    resource_id_factory: Callable[[AuthenticatedPrincipal], str],
    admin_action: ActionName | None = None,
    authenticated_body: bytes | None = None,
) -> AuthorizedAction:
    try:
        body = (
            await read_bounded_http_body(
                request,
                max_bytes=_MAX_CONTROL_REQUEST_BYTES,
            )
            if authenticated_body is None
            else _require_authenticated_body(authenticated_body)
        )
        auth_request = _build_authentication_request(
            method=request.method,
            path=_path_with_query(request),
            body=body,
            headers=_raw_request_headers(request),
        )
        return gateway.authorize_browser_for(
            authentication_request=auth_request,
            intent_factory=lambda principal: ActionIntent(
                action=(
                    admin_action
                    if admin_action is not None and principal.role is UserRole.ADMIN
                    else action
                ),
                resource_id=resource_id_factory(principal),
            ),
        )
    except (PermissionError, ValueError, CentralIdentityDenied):
        raise HTTPException(status_code=403, detail=_GENERIC_FORBIDDEN) from None


def _record_dashboard_activity_event(
    *,
    store: Store,
    principal: AuthenticatedPrincipal,
    request: Request,
    now: datetime,
) -> None:
    request_id = request.headers.get("X-MIM-Origin-Request-Id")
    if not request_id:
        return
    opaque = hash_browser_request_id(request_id)
    try:
        store.append_activity_event(
            ingest_activity_event(
                event_id=opaque,
                trusted_user_id=principal.user_id,
                trusted_correlation_id=f"corr-{opaque[:59]}",
                trusted_occurred_at=now,
                observed_at=now,
                payload={
                    "surface": ActivitySurface.DASHBOARD.value,
                    "action": ActivityAction.VIEW_DASHBOARD.value,
                    "target_ref": (
                        "dashboard/admin-overview"
                        if principal.role is UserRole.ADMIN
                        else f"dashboard/{principal.user_id}"
                    ),
                    "outcome": ActivityOutcome.SUCCEEDED.value,
                    "latency_ms": 0,
                },
            )
        )
    except Exception:
        return


def _require_authenticated_body(value: object) -> bytes:
    if type(value) is not bytes:
        raise ValueError("Identity is not authorized for MIM.")
    return value


def _require_browser_authenticated_request_headers(request: Request) -> None:
    try:
        _build_authentication_request(
            method=request.method,
            path=_path_with_query(request),
            body=b"",
            headers=_raw_request_headers(request),
        )
    except ValueError:
        raise HTTPException(status_code=403, detail=_GENERIC_FORBIDDEN) from None


def build_authentication_request(
    *,
    method: str,
    path: str,
    body: bytes,
    headers: Iterable[tuple[str, str]],
) -> AuthenticationRequest:
    indexed = _index_headers(headers)
    return AuthenticationRequest(
        origin=OriginRequest(
            method=_require_method(method),
            path=_require_path(path),
            body=body,
            timestamp=_parse_origin_timestamp(indexed["x-mim-origin-timestamp"][0]),
            request_id=OriginRequestId(indexed["x-mim-origin-request-id"][0]),
            public_host=_require_control_plane_public_host(
                indexed["x-mim-origin-public-host"][0]
            ),
            destination_class=_require_control_plane_destination_class(
                indexed["x-mim-origin-destination-class"][0]
            ),
            key_id=indexed["x-mim-origin-key-id"][0],
            signature=indexed["x-mim-origin-signature"][0],
        ),
        headers=(("Cf-Access-Jwt-Assertion", indexed["cf-access-jwt-assertion"][0]),),
    )


def _build_authentication_request(
    *,
    method: str,
    path: str,
    body: bytes,
    headers: Iterable[tuple[str, str]],
) -> AuthenticationRequest:
    return build_authentication_request(
        method=method,
        path=path,
        body=body,
        headers=headers,
    )


def _path_with_query(request: Request) -> str:
    path = str(request.scope["path"])
    query = request.scope["query_string"].decode("ascii")
    return path if not query else f"{path}?{query}"


def _index_headers(headers: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    indexed: dict[str, list[str]] = {}
    for name, value in headers:
        normalized_name = name.strip().casefold()
        if normalized_name in _FORBIDDEN_BROWSER_HEADERS:
            raise ValueError("Identity is not authorized for MIM.")
        if normalized_name.startswith("cf-access-") and (
            normalized_name != "cf-access-jwt-assertion"
        ):
            raise ValueError("Identity is not authorized for MIM.")
        if normalized_name.startswith("x-mim-") and (
            normalized_name not in _REQUIRED_BROWSER_AUTH_HEADERS
        ):
            raise ValueError("Identity is not authorized for MIM.")
        indexed.setdefault(normalized_name, []).append(value)
    for name in _REQUIRED_BROWSER_AUTH_HEADERS:
        values = indexed.get(name)
        if values is None or len(values) != 1:
            raise ValueError("Identity is not authorized for MIM.")
        if not isinstance(values[0], str) or not values[0].strip():
            raise ValueError("Identity is not authorized for MIM.")
        values[0] = values[0].strip()
    return indexed


def _raw_request_headers(request: Request) -> tuple[tuple[str, str], ...]:
    raw_headers = request.scope.get("headers")
    if not isinstance(raw_headers, list):
        raise ValueError("Identity is not authorized for MIM.")
    pairs: list[tuple[str, str]] = []
    for item in raw_headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            raise ValueError("Identity is not authorized for MIM.")
        pairs.append((item[0].decode("latin-1"), item[1].decode("latin-1")))
    return tuple(pairs)


def _require_method(method: str) -> str:
    text = method.strip().upper()
    if not text:
        raise ValueError("Identity is not authorized for MIM.")
    return text


def _require_path(path: str) -> str:
    text = path.strip()
    if not text.startswith("/"):
        raise ValueError("Identity is not authorized for MIM.")
    return text


def _parse_origin_timestamp(raw: str) -> datetime:
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ValueError("Identity is not authorized for MIM.") from exc
    return datetime.fromtimestamp(value, tz=UTC)


def _require_control_plane_public_host(raw: str) -> str:
    if raw != "mim.madup.app":
        raise ValueError("Identity is not authorized for MIM.")
    return raw


def _require_control_plane_destination_class(raw: str) -> str:
    if raw != "control-plane":
        raise ValueError("Identity is not authorized for MIM.")
    return raw


def _mutation_gate_from_environment() -> bool:
    value = os.environ.get("MIM_ENABLE_MUTATIONS")
    if value is None or value == "false":
        return False
    if value == "true":
        return True
    raise ValueError("MIM_ENABLE_MUTATIONS must be exact true or false.")


def _require_exact_bool(value: bool) -> bool:
    if type(value) is not bool:
        raise ValueError("mutations_enabled must be an exact bool.")
    return value


def _parse_deployment_request(body: bytes) -> dict[str, str]:
    payload = _parse_deployment_body(body)
    expected = frozenset(
        {"plan_id", "plan_hash", "idempotency_key", "correlation_id"}
    )
    if not isinstance(payload, dict) or frozenset(payload) != expected:
        raise ValueError("invalid deployment request")
    result: dict[str, str] = {}
    for key in expected:
        value = payload[key]
        if type(value) is not str or not value:
            raise ValueError("invalid deployment request")
        result[key] = value
    return result


def _parse_schedule_creation_request(body: bytes) -> dict[str, str]:
    payload = _parse_deployment_body(body)
    expected = frozenset({"plan_id", "plan_hash", "idempotency_key"})
    if frozenset(payload) != expected:
        raise ValueError("invalid schedule creation request")
    result: dict[str, str] = {}
    for key in expected:
        value = payload[key]
        if type(value) is not str or not value:
            raise ValueError("invalid schedule creation request")
        result[key] = value
    return result


def _parse_deployment_body(body: bytes) -> dict[str, object]:
    if type(body) is not bytes or len(body) > _MAX_DEPLOY_REQUEST_BYTES:
        raise ValueError("invalid deployment request")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid deployment request") from None
    if not isinstance(payload, dict):
        raise ValueError("invalid deployment request")
    return payload


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid deployment request")
        result[key] = value
    return result


def _authorize_github_machine_origin(
    *,
    request: Request,
    body: bytes,
    verifier: OriginHmacVerifier,
) -> None:
    indexed = _machine_origin_headers(request)
    verifier.verify(
        OriginRequest(
            method=_require_method(request.method),
            path=_path_with_query(request),
            body=body,
            timestamp=_parse_origin_timestamp(
                indexed["x-mim-origin-timestamp"]
            ),
            request_id=OriginRequestId(indexed["x-mim-origin-request-id"]),
            public_host=_require_control_plane_public_host(
                indexed["x-mim-origin-public-host"]
            ),
            destination_class=_require_control_plane_destination_class(
                indexed["x-mim-origin-destination-class"]
            ),
            key_id=indexed["x-mim-origin-key-id"],
            signature=indexed["x-mim-origin-signature"],
        )
    )
    if request.headers.getlist("Cf-Access-Jwt-Assertion"):
        raise OriginDenied("Origin request was denied.")


def _machine_origin_headers(request: Request) -> dict[str, str]:
    names = (
        "x-mim-origin-key-id",
        "x-mim-origin-timestamp",
        "x-mim-origin-request-id",
        "x-mim-origin-public-host",
        "x-mim-origin-destination-class",
        "x-mim-origin-signature",
    )
    result: dict[str, str] = {}
    for name in names:
        values = request.headers.getlist(name)
        if len(values) != 1 or not values[0].strip():
            raise OriginDenied("Origin request was denied.")
        result[name] = values[0].strip()
    return result


def _exact_github_headers(request: Request) -> dict[str, str]:
    names = (
        "x-hub-signature-256",
        "x-github-event",
        "x-github-delivery",
    )
    result: dict[str, str] = {}
    for name in names:
        values = request.headers.getlist(name)
        if len(values) != 1 or not values[0].strip():
            raise ValueError("GitHub webhook headers are invalid.")
        result[name] = values[0].strip()
    return result
