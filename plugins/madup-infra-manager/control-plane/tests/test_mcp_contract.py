from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence, cast

from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.context import RequestContext
from starlette.requests import Request

from mim_control_plane.adapters.fake_identity import (
    FakeActionPolicyAuthorizer,
    FakeIdentityRegistry,
)
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.dashboard import ControlPlaneReadService
from mim_control_plane.domain.central_identity import ActionName
from mim_control_plane.domain.models import (
    Operation,
    OperationId,
    OriginRequestId,
    RepositoryAdmissionId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    OperationState,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.mcp import (
    _authorize_mcp_context,
    authorize_mcp_request,
    build_mcp_server,
)
from mim_control_plane.security.authorization import IdentityPolicy
from mim_control_plane.security.identity import IdentityAuthenticator, IdentityClaims
from mim_control_plane.security.origin import (
    OriginHmacVerifier,
    OriginRequest,
    sign_origin_request,
)
from mim_control_plane.services.central_identity import CentralIdentityGateway
from mim_control_plane.services.deployments import DeploymentService
from mim_control_plane.services.schedule_management import ScheduleManagementService

NOW = datetime(2026, 8, 4, 2, 0, 0, tzinfo=UTC)
ISSUER = "https://tenant.cloudflareaccess.com"
AUDIENCE = "audience-1"
GROUP = "mim-users"
ORIGIN_KEY = b"m" * 32


class CountingJwtVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self._claims = claims
        self.calls = 0

    def verify(self, token: str) -> IdentityClaims:
        del token
        self.calls += 1
        return self._claims


def build_gateway(
    *,
    store: MemoryStore,
    claims: IdentityClaims,
    authorizer: FakeActionPolicyAuthorizer | None = None,
) -> tuple[CentralIdentityGateway, CountingJwtVerifier]:
    verifier = CountingJwtVerifier(claims)
    policy = IdentityPolicy(
        store=store,
        issuer=ISSUER,
        audience=AUDIENCE,
        company_domain="madup.com",
        required_group=GROUP,
        max_staleness=timedelta(minutes=60),
        clock=lambda: NOW,
    )
    gateway = CentralIdentityGateway(
        browser_authenticator=IdentityAuthenticator(
            origin_verifier=OriginHmacVerifier(
                keys={"edge-key": ORIGIN_KEY},
                store=store,
                clock=lambda: NOW,
                window=timedelta(seconds=60),
            ),
            jwt_verifier=verifier,
            identity_policy=policy,
        ),
        identity_policy=policy,
        shared_install_directory=FakeIdentityRegistry(),
        identity_link_directory=FakeIdentityRegistry(),
        action_authorizer=(
            FakeActionPolicyAuthorizer() if authorizer is None else authorizer
        ),
        required_slack_scopes=frozenset({"commands"}),
        clock=lambda: NOW,
    )
    return gateway, verifier


def signed_headers(
    *, method: str, path: str, request_id: str
) -> tuple[tuple[str, str], ...]:
    unsigned = OriginRequest(
        method=method,
        path=path,
        body=b"",
        timestamp=NOW,
        request_id=OriginRequestId(request_id),
        public_host="mim.madup.app",
        destination_class="control-plane",
        key_id="edge-key",
        signature=None,
    )
    signature = sign_origin_request(unsigned, key=ORIGIN_KEY)
    return (
        ("X-MIM-Origin-Key-Id", "edge-key"),
        ("X-MIM-Origin-Timestamp", str(int(NOW.timestamp()))),
        ("X-MIM-Origin-Request-Id", request_id),
        ("X-MIM-Origin-Public-Host", "mim.madup.app"),
        ("X-MIM-Origin-Destination-Class", "control-plane"),
        ("X-MIM-Origin-Signature", signature),
        ("Cf-Access-Jwt-Assertion", "opaque"),
    )


class McpContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(
            User(
                id=UserId("usr-1"),
                email="person@madup.com",
                role=UserRole.USER,
                state=UserState.ACTIVE,
                groups=frozenset({GROUP}),
                identity_synced_at=NOW - timedelta(minutes=5),
                created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(minutes=5),
            )
        )
        self.store.create_workload(
            Workload(
                id=WorkloadId("wrk-1"),
                owner_id=UserId("usr-1"),
                repository_admission_id=RepositoryAdmissionId("adm-1"),
                name="Alpha",
                kind=WorkloadKind.STREAMLIT,
                state=WorkloadState.ACTIVE,
                source_sha="a" * 40,
                desired_manifest_hash="manifest-1",
                created_at=NOW - timedelta(days=2),
                updated_at=NOW - timedelta(hours=4),
            )
        )
        self.store.create_operation_once(
            Operation(
                id=OperationId("op-1"),
                actor_id=UserId("usr-1"),
                workload_id=WorkloadId("wrk-1"),
                action="deploy_workload",
                idempotency_key="idem-1",
                request_hash="hash-1",
                state=OperationState.FAILED,
                created_at=NOW - timedelta(hours=3),
                updated_at=NOW - timedelta(hours=1),
                sanitized_failure="Bearer secret-token 127.0.0.1",
            )
        )

    def test_authorize_mcp_request_checks_origin_before_jwt(self) -> None:
        gateway, verifier = build_gateway(
            store=self.store,
            claims=IdentityClaims(
                subject="usr-1",
                email="person@madup.com",
                issuer=ISSUER,
                audience=(AUDIENCE,),
                issued_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=10),
            ),
        )
        with self.assertRaises(PermissionError):
            authorize_mcp_request(
                gateway=gateway,
                method="POST",
                path="/mcp",
                body=b"{}",
                headers=(
                    ("X-MIM-Origin-Key-Id", "edge-key"),
                    ("X-MIM-Origin-Timestamp", str(int(NOW.timestamp()))),
                    ("X-MIM-Origin-Request-Id", "req-1"),
                    ("X-MIM-Origin-Public-Host", "mim.madup.app"),
                    ("X-MIM-Origin-Destination-Class", "control-plane"),
                    ("X-MIM-Origin-Signature", "0" * 64),
                    ("Cf-Access-Jwt-Assertion", "opaque"),
                ),
            )
        self.assertEqual(verifier.calls, 0)

    def test_server_exposes_exact_read_only_tools(self) -> None:
        service = ControlPlaneReadService(store=self.store, clock=lambda: NOW)
        server = build_mcp_server(
            service=service,
            gateway=build_gateway(
                store=self.store,
                claims=IdentityClaims(
                    subject="usr-1",
                    email="person@madup.com",
                    issuer=ISSUER,
                    audience=(AUDIENCE,),
                    issued_at=NOW - timedelta(minutes=1),
                    expires_at=NOW + timedelta(minutes=10),
                ),
            )[0],
        )

        async def run() -> Sequence[types.Tool]:
            return await server.list_tools()

        tools = asyncio.run(run())
        self.assertEqual(
            [tool.name for tool in tools],
            [
                "explain_failure",
                "get_operation",
                "get_usage",
                "list_workloads",
                "plan_deploy",
            ],
        )
        for tool in tools:
            self.assertNotIn("user_id", tool.inputSchema.get("properties", {}))
            self.assertNotIn("role", tool.inputSchema.get("properties", {}))
            annotations = tool.annotations
            if annotations is not None:
                self.assertTrue(annotations.readOnlyHint)
                self.assertFalse(bool(annotations.destructiveHint))

    def test_plan_schedule_tool_uses_exact_workload_scope(self) -> None:
        class StubScheduleService:
            def plan_schedule(self, **_kwargs: object) -> dict[str, object]:
                return {"status": "ready"}

        authorizer = FakeActionPolicyAuthorizer()
        authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.MANAGE_SCHEDULE.value,
            resource_id="dashboard:usr-1",
            reason_code="legacy_schedule_scope_denied",
        )
        gateway, _ = build_gateway(
            store=self.store,
            claims=IdentityClaims(
                subject="usr-1",
                email="person@madup.com",
                issuer=ISSUER,
                audience=(AUDIENCE,),
                issued_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=10),
            ),
            authorizer=authorizer,
        )
        server = build_mcp_server(
            service=ControlPlaneReadService(store=self.store, clock=lambda: NOW),
            gateway=gateway,
            schedule_management=cast(
                ScheduleManagementService,
                StubScheduleService(),
            ),
        )
        ctx = Context(
            request_context=RequestContext(
                request_id="req-plan-schedule-tool",
                meta=None,
                session=cast(Any, object()),
                lifespan_context=None,
                request=_request(
                    method="POST",
                    path="/mcp",
                    body=b"",
                    headers=dict(
                        signed_headers(
                            method="POST",
                            path="/mcp",
                            request_id="req-plan-schedule-tool",
                        )
                    ),
                ),
            ),
            fastmcp=server,
        )
        plan_schedule_tool = server._tool_manager.get_tool("plan_schedule")
        self.assertIsNotNone(plan_schedule_tool)
        assert plan_schedule_tool is not None

        result = asyncio.run(
            plan_schedule_tool.fn(
                workload_id="wrk-1",
                ctx=ctx,
            )
        )
        self.assertEqual(result, {"status": "ready"})
        self.assertEqual(authorizer.calls[-1].intent.action, ActionName.MANAGE_SCHEDULE)
        self.assertEqual(authorizer.calls[-1].intent.resource_id, "workload:wrk-1")

    def test_enabled_server_adds_only_reviewed_plan_bound_mutation_tools(
        self,
    ) -> None:
        class StubDeploymentService:
            def deploy_from_plan(self, **_kwargs: object) -> dict[str, object]:
                return {"status": "queued"}

        class StubSecretService:
            def __init__(self) -> None:
                self.plan_write_calls: list[dict[str, object]] = []
                self.plan_attach_calls: list[dict[str, object]] = []
                self.apply_calls: list[dict[str, object]] = []

            def plan_secret_write(self, **kwargs: object) -> dict[str, object]:
                self.plan_write_calls.append(kwargs)
                return {
                    "action": "plan_secret_write",
                    "plan_id": "plan-secret-1",
                    "plan_hash": "a" * 64,
                    "mode": "create",
                    "secret_id": "sec-1",
                    "secret_name": "meta-api",
                    "workload_ids": ("wrk-1", "wrk-2"),
                }

            def plan_secret_attach(self, **kwargs: object) -> dict[str, object]:
                self.plan_attach_calls.append(kwargs)
                return {
                    "action": "plan_secret_attach",
                    "plan_id": "plan-secret-2",
                    "plan_hash": "b" * 64,
                    "mode": "attach",
                    "secret_id": "sec-1",
                    "workload_ids": ("wrk-1", "wrk-2"),
                }

            def apply_secret_plan(self, **kwargs: object) -> dict[str, object]:
                self.apply_calls.append(kwargs)
                return {
                    "action": "apply_secret_plan",
                    "operation_id": "op-secret-1",
                    "secret_id": "sec-1",
                    "mode": "attach",
                    "active_version": 3,
                    "rotation_state": "stable",
                    "retiring_version": None,
                    "attached_workload_ids": ("wrk-1", "wrk-2"),
                    "replayed": False,
                }

        class StubScheduleService:
            def plan_schedule(self, **_kwargs: object) -> dict[str, object]:
                return {"status": "ready"}

            def create_schedule_from_plan(
                self, **_kwargs: object
            ) -> dict[str, object]:
                return {"state": "enabled"}

            def pause_schedule(self, **_kwargs: object) -> dict[str, object]:
                return {"state": "paused"}

            def resume_schedule(self, **_kwargs: object) -> dict[str, object]:
                return {"state": "enabled"}

        service = ControlPlaneReadService(store=self.store, clock=lambda: NOW)
        secret_service = StubSecretService()
        authorizer = FakeActionPolicyAuthorizer()
        authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.DEPLOY_WORKLOAD.value,
            resource_id="dashboard:usr-1",
            reason_code="legacy_deploy_scope_denied",
        )
        server = build_mcp_server(
            service=service,
            gateway=build_gateway(
                store=self.store,
                claims=IdentityClaims(
                    subject="usr-1",
                    email="person@madup.com",
                    issuer=ISSUER,
                    audience=(AUDIENCE,),
                    issued_at=NOW - timedelta(minutes=1),
                    expires_at=NOW + timedelta(minutes=10),
                ),
                authorizer=authorizer,
            )[0],
            deployment_service=cast(
                DeploymentService,
                StubDeploymentService(),
            ),
            secret_management=cast(Any, secret_service),
            schedule_management=cast(
                ScheduleManagementService,
                StubScheduleService(),
            ),
            mutations_enabled=True,
            secret_handoff_idempotency_factory=lambda: "handoff-idem-1",
        )

        async def run() -> Sequence[types.Tool]:
            return await server.list_tools()

        tools = asyncio.run(run())
        self.assertEqual(
            [tool.name for tool in tools],
            [
                "explain_failure",
                "get_operation",
                "get_usage",
                "list_workloads",
                "plan_deploy",
                "plan_secret_write",
                "plan_secret_attach",
                "plan_schedule",
                "deploy_from_plan",
                "attach_secret_from_plan",
                "create_schedule_from_plan",
                "pause_schedule",
                "resume_schedule",
            ],
        )
        deploy_tool = next(tool for tool in tools if tool.name == "deploy_from_plan")
        self.assertEqual(
            frozenset(deploy_tool.inputSchema["properties"]),
            frozenset(
                {
                    "plan_id",
                    "plan_hash",
                    "idempotency_key",
                    "correlation_id",
                }
            ),
        )
        annotations = deploy_tool.annotations
        self.assertIsNotNone(annotations)
        assert annotations is not None
        self.assertFalse(bool(annotations.readOnlyHint))
        self.assertTrue(annotations.destructiveHint)
        self.assertTrue(annotations.idempotentHint)
        self.assertFalse(bool(annotations.openWorldHint))

        plan_secret_write = next(
            tool for tool in tools if tool.name == "plan_secret_write"
        )
        self.assertEqual(
            frozenset(plan_secret_write.inputSchema["properties"]),
            frozenset({"secret_name", "integration_type", "workload_ids"}),
        )
        plan_secret_attach = next(
            tool for tool in tools if tool.name == "plan_secret_attach"
        )
        self.assertEqual(
            frozenset(plan_secret_attach.inputSchema["properties"]),
            frozenset({"secret_id", "workload_ids"}),
        )
        attach_secret = next(
            tool for tool in tools if tool.name == "attach_secret_from_plan"
        )
        self.assertEqual(
            frozenset(attach_secret.inputSchema["properties"]),
            frozenset({"plan_id", "plan_hash", "idempotency_key"}),
        )
        self.assertNotIn("payload", attach_secret.inputSchema["properties"])
        self.assertTrue(
            bool(
                attach_secret.annotations
                and attach_secret.annotations.destructiveHint
            )
        )
        self.assertFalse(
            bool(
                plan_secret_write.annotations
                and plan_secret_write.annotations.destructiveHint
            )
        )

        ctx = Context(
            request_context=RequestContext(
                request_id="req-secret-tool",
                meta=None,
                session=cast(Any, object()),
                lifespan_context=None,
                request=_request(
                    method="POST",
                    path="/mcp",
                    body=b"",
                    headers=dict(
                        signed_headers(
                            method="POST",
                            path="/mcp",
                            request_id="req-secret-tool",
                        )
                    ),
                ),
            ),
            fastmcp=server,
        )
        plan_secret_write_tool = server._tool_manager.get_tool("plan_secret_write")
        self.assertIsNotNone(plan_secret_write_tool)
        assert plan_secret_write_tool is not None
        planned_secret = asyncio.run(
            plan_secret_write_tool.fn(
                secret_name="meta-api",
                integration_type="meta_ads",
                workload_ids=("wrk-1", "wrk-2"),
                ctx=ctx,
            )
        )
        self.assertEqual(planned_secret["plan_id"], "plan-secret-1")
        self.assertEqual(
            planned_secret["handoff_path"],
            (
                "/v1/secrets/handoff?"
                "plan_id=plan-secret-1"
                f"&plan_hash={'a' * 64}"
                "&idempotency_key=handoff-idem-1"
            ),
        )
        self.assertNotIn("payload", str(secret_service.plan_write_calls[0]))
        self.assertEqual(
            [call.intent.resource_id for call in authorizer.calls[-2:]],
            ["workload:wrk-1", "workload:wrk-2"],
        )

        attach_secret_tool = server._tool_manager.get_tool("attach_secret_from_plan")
        self.assertIsNotNone(attach_secret_tool)
        assert attach_secret_tool is not None
        attach_ctx = Context(
            request_context=RequestContext(
                request_id="req-secret-attach-tool",
                meta=None,
                session=cast(Any, object()),
                lifespan_context=None,
                request=_request(
                    method="POST",
                    path="/mcp",
                    body=b"",
                    headers=dict(
                        signed_headers(
                            method="POST",
                            path="/mcp",
                            request_id="req-secret-attach-tool",
                        )
                    ),
                ),
            ),
            fastmcp=server,
        )
        attach_result = asyncio.run(
            attach_secret_tool.fn(
                plan_id="plan-secret-2",
                plan_hash="b" * 64,
                idempotency_key="attach-idem-1",
                ctx=attach_ctx,
            )
        )
        self.assertEqual(attach_result["mode"], "attach")
        self.assertIsNone(secret_service.apply_calls[0]["payload"])
        self.assertEqual(
            authorizer.calls[-1].intent.resource_id,
            "deployment-plan:plan-secret-2",
        )

        plan_schedule = next(tool for tool in tools if tool.name == "plan_schedule")
        self.assertEqual(
            frozenset(plan_schedule.inputSchema["properties"]),
            frozenset({"workload_id"}),
        )
        create_schedule = next(
            tool for tool in tools if tool.name == "create_schedule_from_plan"
        )
        self.assertEqual(
            frozenset(create_schedule.inputSchema["properties"]),
            frozenset({"plan_id", "plan_hash", "idempotency_key"}),
        )
        for name in ("pause_schedule", "resume_schedule"):
            schedule_tool = next(tool for tool in tools if tool.name == name)
            self.assertEqual(
                frozenset(schedule_tool.inputSchema["properties"]),
                frozenset({"schedule_id"}),
            )

    def test_enabled_server_requires_secret_mutation_dependency(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Mutation dependencies must be configured together.",
        ):
            build_mcp_server(
                service=ControlPlaneReadService(store=self.store, clock=lambda: NOW),
                gateway=build_gateway(
                    store=self.store,
                    claims=IdentityClaims(
                        subject="usr-1",
                        email="person@madup.com",
                        issuer=ISSUER,
                        audience=(AUDIENCE,),
                        issued_at=NOW - timedelta(minutes=1),
                        expires_at=NOW + timedelta(minutes=10),
                    ),
                )[0],
                deployment_service=cast(DeploymentService, object()),
                mutations_enabled=True,
            )

    def test_authorize_mcp_context_uses_request_headers_and_body(self) -> None:
        gateway, _ = build_gateway(
            store=self.store,
            claims=IdentityClaims(
                subject="usr-1",
                email="person@madup.com",
                issuer=ISSUER,
                audience=(AUDIENCE,),
                issued_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=10),
            ),
        )
        body = b"{\"method\":\"tools/call\"}"
        headers = dict(
            signed_headers(method="POST", path="/mcp", request_id="req-ctx-good")
        )
        unsigned = OriginRequest(
            method="POST",
            path="/mcp",
            body=body,
            timestamp=NOW,
            request_id=OriginRequestId("req-ctx-good"),
            public_host="mim.madup.app",
            destination_class="control-plane",
            key_id="edge-key",
            signature=None,
        )
        headers["X-MIM-Origin-Signature"] = sign_origin_request(
            unsigned,
            key=ORIGIN_KEY,
        )
        ctx = Context(
            request_context=RequestContext(
                request_id="req-ctx",
                meta=None,
                session=cast(Any, object()),
                lifespan_context=None,
                request=_request(
                    method="POST",
                    path="/mcp",
                    body=body,
                    headers=headers,
                ),
            ),
            fastmcp=FastMCP("test"),
        )
        authorized = asyncio.run(
            _authorize_mcp_context(
                gateway=gateway,
                ctx=ctx,
                action=ActionName.VIEW_DASHBOARD,
                resource_id_factory=lambda principal: f"dashboard:{principal.user_id}",
            )
        )
        self.assertEqual(authorized.principal.user_id, UserId("usr-1"))

    def test_tool_runtime_returns_redacted_failure(self) -> None:
        service = ControlPlaneReadService(store=self.store, clock=lambda: NOW)
        principal = authorize_mcp_request(
            gateway=build_gateway(
                store=self.store,
                claims=IdentityClaims(
                    subject="usr-1",
                    email="person@madup.com",
                    issuer=ISSUER,
                    audience=(AUDIENCE,),
                    issued_at=NOW - timedelta(minutes=1),
                    expires_at=NOW + timedelta(minutes=10),
                ),
            )[0],
            method="POST",
            path="/mcp",
            body=b"",
            headers=signed_headers(method="POST", path="/mcp", request_id="req-2"),
        )
        failure = service.get_failure(
            principal=principal.principal, operation_id="op-1"
        )
        rendered = str(failure)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("127.0.0.1", rendered)


def _request(
    *,
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
    allow_direct_auth: bool = True,
) -> Request:
    header_pairs = [
        (key.lower().encode("ascii"), value.encode("utf-8"))
        for key, value in headers.items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": header_pairs,
        "state": {},
        "_mim_allow_direct_mcp_auth": allow_direct_auth,
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)
