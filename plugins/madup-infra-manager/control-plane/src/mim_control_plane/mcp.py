"""Authenticated MCP server with mutations disabled unless explicitly enabled."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from typing import Protocol

from mcp import types
from mcp.server.fastmcp import Context, FastMCP

from mim_control_plane.api import (
    _mutation_gate_from_environment,
    _path_with_query,
    _require_exact_bool,
    build_authentication_request,
)
from mim_control_plane.dashboard import (
    ADMIN_OVERVIEW_RESOURCE_ID,
    ControlPlaneReadService,
    dashboard_resource_id,
    failure_resource_id,
    operation_resource_id,
    usage_resource_id,
)
from mim_control_plane.domain.central_identity import ActionIntent, ActionName
from mim_control_plane.domain.states import UserRole
from mim_control_plane.http_body import read_bounded_http_body
from mim_control_plane.secret_api import build_secret_handoff_path
from mim_control_plane.security.authorization import AccessDenied
from mim_control_plane.security.identity import AuthenticatedPrincipal
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

_PREAUTHORIZED_BROWSER_SCOPE_KEY = "_mim_preauthorized_browser"
_ALLOW_DIRECT_MCP_AUTH_SCOPE_KEY = "_mim_allow_direct_mcp_auth"
_MAX_MCP_REQUEST_BYTES = 64 * 1024


class SecretManagementPort(Protocol):
    def plan_secret_write(
        self,
        *,
        principal: AuthenticatedPrincipal,
        secret_name: str,
        integration_type: str,
        workload_ids: tuple[str, ...],
    ) -> dict[str, object]: ...

    def plan_secret_attach(
        self,
        *,
        principal: AuthenticatedPrincipal,
        secret_id: str,
        workload_ids: tuple[str, ...],
    ) -> dict[str, object]: ...

    def apply_secret_plan(
        self,
        *,
        principal: AuthenticatedPrincipal,
        plan_id: str,
        plan_hash: str,
        idempotency_key: str,
        payload: bytes | None = None,
    ) -> dict[str, object]: ...


def authorize_mcp_request(
    *,
    gateway: CentralIdentityGateway,
    method: str,
    path: str,
    body: bytes,
    headers: tuple[tuple[str, str], ...],
    action: ActionName = ActionName.VIEW_DASHBOARD,
    resource_id_factory: Callable[[AuthenticatedPrincipal], str] = lambda _principal: (
        ADMIN_OVERVIEW_RESOURCE_ID
    ),
    admin_action: ActionName | None = None,
) -> AuthorizedAction:
    auth_request = build_authentication_request(
        method=method,
        path=path,
        body=body,
        headers=headers,
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


def build_mcp_server(
    *,
    service: ControlPlaneReadService,
    gateway: CentralIdentityGateway,
    deployment_service: DeploymentService | None = None,
    schedule_management: ScheduleManagementService | None = None,
    secret_management: SecretManagementPort | None = None,
    mutations_enabled: bool | None = None,
    secret_handoff_idempotency_factory: Callable[[], str] | None = None,
    secret_handoff_path: str = "/v1/secrets/handoff",
) -> FastMCP:
    enable_mutations = (
        _mutation_gate_from_environment()
        if mutations_enabled is None
        else _require_exact_bool(mutations_enabled)
    )
    if enable_mutations and (
        deployment_service is None or secret_management is None
    ):
        raise ValueError("Mutation dependencies must be configured together.")
    handoff_idempotency_factory = (
        _default_secret_handoff_idempotency
        if secret_handoff_idempotency_factory is None
        else secret_handoff_idempotency_factory
    )
    server = FastMCP(
        "madup-infra-manager",
        json_response=True,
        stateless_http=True,
    )
    annotations = types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.tool(
        name="explain_failure",
        description=(
            "Explain a sanitized MIM operation failure without revealing secrets."
        ),
        annotations=annotations,
    )
    async def explain_failure(
        operation_id: str,
        ctx: Context,
    ) -> dict[str, object]:
        authorized = await _authorize_mcp_context(
            gateway=gateway,
            ctx=ctx,
            action=ActionName.VIEW_DASHBOARD,
            resource_id_factory=lambda _principal: failure_resource_id(operation_id),
        )
        return service.get_failure(
            principal=authorized.principal,
            operation_id=operation_id,
        )

    @server.tool(
        name="get_operation",
        description="Return the current status of a MIM operation.",
        annotations=annotations,
    )
    async def get_operation(
        operation_id: str,
        ctx: Context,
    ) -> dict[str, object]:
        authorized = await _authorize_mcp_context(
            gateway=gateway,
            ctx=ctx,
            action=ActionName.VIEW_DASHBOARD,
            resource_id_factory=lambda _principal: operation_resource_id(operation_id),
        )
        return service.get_operation(
            principal=authorized.principal,
            operation_id=operation_id,
        )

    @server.tool(
        name="get_usage",
        description="Return user or admin usage and quota views for MIM only.",
        annotations=annotations,
    )
    async def get_usage(ctx: Context) -> dict[str, object]:
        authorized = await _authorize_mcp_context(
            gateway=gateway,
            ctx=ctx,
            action=ActionName.VIEW_USAGE,
            resource_id_factory=lambda principal: (
                ADMIN_OVERVIEW_RESOURCE_ID
                if principal.role is UserRole.ADMIN
                else usage_resource_id(principal.user_id)
            ),
            admin_action=ActionName.ADMIN_USAGE_OVERVIEW,
        )
        return service.get_usage(principal=authorized.principal)

    @server.tool(
        name="list_workloads",
        description="List allowed workloads, schedules, and secret metadata for MIM.",
        annotations=annotations,
    )
    async def list_workloads(ctx: Context) -> dict[str, object]:
        authorized = await _authorize_mcp_context(
            gateway=gateway,
            ctx=ctx,
            action=ActionName.VIEW_DASHBOARD,
            resource_id_factory=lambda principal: (
                ADMIN_OVERVIEW_RESOURCE_ID
                if principal.role is UserRole.ADMIN
                else dashboard_resource_id(principal.user_id)
            ),
        )
        return service.list_workloads(principal=authorized.principal)

    @server.tool(
        name="plan_deploy",
        description="Return the current reviewed deploy-planning availability.",
        annotations=annotations,
    )
    async def plan_deploy(
        ctx: Context,
        workload_id: str | None = None,
    ) -> dict[str, object]:
        authorized = await _authorize_mcp_context(
            gateway=gateway,
            ctx=ctx,
            action=ActionName.DEPLOY_WORKLOAD,
            resource_id_factory=lambda principal: (
                ADMIN_OVERVIEW_RESOURCE_ID
                if principal.role is UserRole.ADMIN
                else dashboard_resource_id(principal.user_id)
            ),
        )
        return service.plan_deploy(
            principal=authorized.principal,
            workload_id=workload_id,
        )

    if secret_management is not None:

        @server.tool(
            name="plan_secret_write",
            description=(
                "Review a secret create-or-rotate plan for exact MIM workloads and "
                "return a browser handoff path for the raw secret value."
            ),
            annotations=annotations,
        )
        async def plan_secret_write(
            secret_name: str,
            integration_type: str,
            workload_ids: tuple[str, ...],
            ctx: Context,
        ) -> dict[str, object]:
            authorized = await _authorize_mcp_workloads(
                gateway=gateway,
                ctx=ctx,
                action=ActionName.DEPLOY_WORKLOAD,
                workload_ids=workload_ids,
            )
            try:
                result = secret_management.plan_secret_write(
                    principal=authorized.principal,
                    secret_name=secret_name,
                    integration_type=integration_type,
                    workload_ids=workload_ids,
                )
            except (AccessDenied, PermissionError, ValueError) as exc:
                raise PermissionError("Secret request was denied.") from exc
            return dict(result) | {
                "handoff_path": build_secret_handoff_path(
                    plan_id=str(result["plan_id"]),
                    plan_hash=str(result["plan_hash"]),
                    idempotency_key=handoff_idempotency_factory(),
                    handoff_path=secret_handoff_path,
                ),
            }

        @server.tool(
            name="plan_secret_attach",
            description=(
                "Review an exact secret attachment plan without accepting any raw "
                "secret value."
            ),
            annotations=annotations,
        )
        async def plan_secret_attach(
            secret_id: str,
            workload_ids: tuple[str, ...],
            ctx: Context,
        ) -> dict[str, object]:
            authorized = await _authorize_mcp_workloads(
                gateway=gateway,
                ctx=ctx,
                action=ActionName.DEPLOY_WORKLOAD,
                workload_ids=workload_ids,
            )
            try:
                return secret_management.plan_secret_attach(
                    principal=authorized.principal,
                    secret_id=secret_id,
                    workload_ids=workload_ids,
                )
            except (AccessDenied, PermissionError, ValueError) as exc:
                raise PermissionError("Secret request was denied.") from exc

    if schedule_management is not None:

        @server.tool(
            name="plan_schedule",
            description="Return the current reviewed schedule-planning availability.",
            annotations=annotations,
        )
        async def plan_schedule(
            workload_id: str,
            ctx: Context,
        ) -> dict[str, object]:
            authorized = await _authorize_mcp_context(
                gateway=gateway,
                ctx=ctx,
                action=ActionName.MANAGE_SCHEDULE,
                resource_id_factory=lambda _principal: f"workload:{workload_id}",
            )
            try:
                return schedule_management.plan_schedule(
                    principal=authorized.principal,
                    workload_id=workload_id,
                )
            except (ScheduleDenied, ValueError) as exc:
                raise PermissionError("Schedule request was denied.") from exc

    if enable_mutations:
        destructive_annotations = types.ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )

        @server.tool(
            name="deploy_from_plan",
            description=(
                "Consume one reviewed deployment plan and queue its exact immutable "
                "source for the private deploy worker."
            ),
            annotations=destructive_annotations,
        )
        async def deploy_from_plan(
            plan_id: str,
            plan_hash: str,
            idempotency_key: str,
            correlation_id: str,
            ctx: Context,
        ) -> dict[str, object]:
            authorized = await _authorize_mcp_context(
                gateway=gateway,
                ctx=ctx,
                action=ActionName.DEPLOY_WORKLOAD,
                resource_id_factory=lambda _principal: (
                    f"deployment-plan:{plan_id}"
                ),
            )
            assert deployment_service is not None
            try:
                return deployment_service.deploy_from_plan(
                    principal=authorized.principal,
                    plan_id=plan_id,
                    plan_hash=plan_hash,
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                )
            except (DeploymentDenied, ValueError) as exc:
                raise PermissionError("Deployment request was denied.") from exc

        assert secret_management is not None

        @server.tool(
            name="attach_secret_from_plan",
            description=(
                "Consume one reviewed secret-attachment plan without accepting any "
                "raw secret value."
            ),
            annotations=destructive_annotations,
        )
        async def attach_secret_from_plan(
            plan_id: str,
            plan_hash: str,
            idempotency_key: str,
            ctx: Context,
        ) -> dict[str, object]:
            authorized = await _authorize_mcp_context(
                gateway=gateway,
                ctx=ctx,
                action=ActionName.DEPLOY_WORKLOAD,
                resource_id_factory=lambda _principal: f"deployment-plan:{plan_id}",
            )
            try:
                return secret_management.apply_secret_plan(
                    principal=authorized.principal,
                    plan_id=plan_id,
                    plan_hash=plan_hash,
                    idempotency_key=idempotency_key,
                    payload=None,
                )
            except (AccessDenied, PermissionError, ValueError) as exc:
                raise PermissionError("Secret request was denied.") from exc

        if schedule_management is not None:

            @server.tool(
                name="create_schedule_from_plan",
                description=(
                    "Consume one reviewed schedule plan and create its exact "
                    "hourly Asia/Seoul schedule."
                ),
                annotations=destructive_annotations,
            )
            async def create_schedule_from_plan(
                plan_id: str,
                plan_hash: str,
                idempotency_key: str,
                ctx: Context,
            ) -> dict[str, object]:
                authorized = await _authorize_mcp_context(
                    gateway=gateway,
                    ctx=ctx,
                    action=ActionName.MANAGE_SCHEDULE,
                    resource_id_factory=lambda _principal: (
                        f"deployment-plan:{plan_id}"
                    ),
                )
                try:
                    return schedule_management.create_schedule_from_plan(
                        principal=authorized.principal,
                        plan_id=plan_id,
                        plan_hash=plan_hash,
                        idempotency_key=idempotency_key,
                    )
                except (ScheduleDenied, ValueError) as exc:
                    raise PermissionError("Schedule request was denied.") from exc

            @server.tool(
                name="pause_schedule",
                description="Pause one owned MIM schedule and its cloud trigger.",
                annotations=destructive_annotations,
            )
            async def pause_schedule(
                schedule_id: str,
                ctx: Context,
            ) -> dict[str, object]:
                authorized = await _authorize_mcp_context(
                    gateway=gateway,
                    ctx=ctx,
                    action=ActionName.MANAGE_SCHEDULE,
                    resource_id_factory=lambda _principal: f"schedule:{schedule_id}",
                )
                try:
                    return schedule_management.pause_schedule(
                        principal=authorized.principal,
                        schedule_id=schedule_id,
                    )
                except (ScheduleDenied, ValueError) as exc:
                    raise PermissionError("Schedule request was denied.") from exc

            @server.tool(
                name="resume_schedule",
                description=(
                    "Resume one owned MIM schedule after current quota and cost checks."
                ),
                annotations=destructive_annotations,
            )
            async def resume_schedule(
                schedule_id: str,
                ctx: Context,
            ) -> dict[str, object]:
                authorized = await _authorize_mcp_context(
                    gateway=gateway,
                    ctx=ctx,
                    action=ActionName.MANAGE_SCHEDULE,
                    resource_id_factory=lambda _principal: f"schedule:{schedule_id}",
                )
                try:
                    return schedule_management.resume_schedule(
                        principal=authorized.principal,
                        schedule_id=schedule_id,
                    )
                except (ScheduleDenied, ValueError) as exc:
                    raise PermissionError("Schedule request was denied.") from exc

    return server


async def _authorize_mcp_workloads(
    *,
    gateway: CentralIdentityGateway,
    ctx: Context,
    action: ActionName,
    workload_ids: tuple[str, ...],
) -> AuthorizedAction:
    if not workload_ids:
        raise PermissionError("Identity is not authorized for MIM.")
    authorized = await _authorize_mcp_context(
        gateway=gateway,
        ctx=ctx,
        action=action,
        resource_id_factory=lambda _principal: f"workload:{workload_ids[0]}",
    )
    for workload_id in workload_ids[1:]:
        try:
            def _intent_factory(
                _principal: AuthenticatedPrincipal,
                workload_id: str = workload_id,
            ) -> ActionIntent:
                return ActionIntent(
                    action=action,
                    resource_id=f"workload:{workload_id}",
                )

            gateway.authorize_authenticated_browser_for(
                authorized_browser=authorized,
                intent_factory=_intent_factory,
            )
        except CentralIdentityDenied as exc:
            raise PermissionError("Identity is not authorized for MIM.") from exc
    return authorized


async def _authorize_mcp_context(
    *,
    gateway: CentralIdentityGateway,
    ctx: Context,
    action: ActionName,
    resource_id_factory: Callable[[AuthenticatedPrincipal], str],
    admin_action: ActionName | None = None,
) -> AuthorizedAction:
    request = ctx.request_context.request
    if request is None:
        raise PermissionError("Identity is not authorized for MIM.")
    try:
        state = request.scope.get("state")
        if state is None:
            preauthorized = None
        elif isinstance(state, Mapping):
            preauthorized = state.get(_PREAUTHORIZED_BROWSER_SCOPE_KEY)
        else:
            raise PermissionError("Identity is not authorized for MIM.")
        if preauthorized is not None:
            return gateway.authorize_authenticated_browser_for(
                authorized_browser=preauthorized,
                intent_factory=lambda principal: ActionIntent(
                    action=(
                        admin_action
                        if admin_action is not None
                        and principal.role is UserRole.ADMIN
                        else action
                    ),
                    resource_id=resource_id_factory(principal),
                ),
            )
        if request.scope.get(_ALLOW_DIRECT_MCP_AUTH_SCOPE_KEY) is not True:
            raise PermissionError("Identity is not authorized for MIM.")
        headers = tuple(
            (name, value)
            for name in (
                "X-MIM-Origin-Key-Id",
                "X-MIM-Origin-Timestamp",
                "X-MIM-Origin-Request-Id",
                "X-MIM-Origin-Public-Host",
                "X-MIM-Origin-Destination-Class",
                "X-MIM-Origin-Signature",
                "Cf-Access-Jwt-Assertion",
            )
            for value in request.headers.getlist(name)
        )
        return authorize_mcp_request(
            gateway=gateway,
            method=request.method,
            path=_path_with_query(request),
            body=await read_bounded_http_body(
                request,
                max_bytes=_MAX_MCP_REQUEST_BYTES,
            ),
            headers=headers,
            action=action,
            resource_id_factory=resource_id_factory,
            admin_action=admin_action,
        )
    except (PermissionError, ValueError, CentralIdentityDenied) as exc:
        raise PermissionError("Identity is not authorized for MIM.") from exc


def _default_secret_handoff_idempotency() -> str:
    return f"secret-handoff-{secrets.token_hex(16)}"
