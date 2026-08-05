from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.dashboard import ControlPlaneReadService
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
from mim_control_plane.mcp import build_mcp_server
from mim_control_plane.mcp_http import AuthenticatedMcpHttpApp, build_mcp_http_app
from mim_control_plane.security.identity import IdentityClaims
from mim_control_plane.security.origin import OriginRequest, sign_origin_request
from tests.test_mcp_contract import build_gateway

NOW = datetime(2026, 8, 4, 2, 0, 0, tzinfo=UTC)
ORIGIN_KEY = b"m" * 32


class McpHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(
            User(
                id=UserId("usr-1"),
                email="person@madup.com",
                role=UserRole.USER,
                state=UserState.ACTIVE,
                groups=frozenset({"mim-users"}),
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
        self.gateway, self.verifier = build_gateway(
            store=self.store,
            claims=IdentityClaims(
                subject="usr-1",
                email="person@madup.com",
                issuer="https://tenant.cloudflareaccess.com",
                audience=("audience-1",),
                issued_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=10),
            ),
        )
        self.server = build_mcp_server(
            service=ControlPlaneReadService(store=self.store, clock=lambda: NOW),
            gateway=self.gateway,
        )

    def test_http_surface_exposes_exact_top_level_mcp_route(self) -> None:
        app = build_mcp_http_app(fastmcp=self.server, gateway=self.gateway)

        self.assertEqual([route.path for route in app.routes], ["/mcp"])
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            response = client.post("/mcp/mcp")
        self.assertEqual(response.status_code, 404)

    def test_http_surface_requires_authenticated_headers_for_initialize(self) -> None:
        app = build_mcp_http_app(fastmcp=self.server, gateway=self.gateway)

        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            response = client.post(
                "/mcp",
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                json=_initialize_payload("1"),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "Identity is not authorized for MIM."},
        )
        self.assertEqual(self.verifier.calls, 0)

    def test_http_surface_rejects_invalid_headers_before_streaming_body(self) -> None:
        app = build_mcp_http_app(fastmcp=self.server, gateway=self.gateway)
        body_reader = AsyncMock(side_effect=AssertionError("body must not be read"))

        with (
            patch(
                "mim_control_plane.mcp_http.read_bounded_http_body",
                body_reader,
            ),
            TestClient(app, base_url="http://127.0.0.1:8000") as client,
        ):
            response = client.post(
                "/mcp",
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                content=b"{}",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "Identity is not authorized for MIM."},
        )
        body_reader.assert_not_awaited()

    def test_http_surface_denies_duplicate_header_and_oversized_body_generically(
        self,
    ) -> None:
        app = build_mcp_http_app(
            fastmcp=self.server,
            gateway=self.gateway,
            max_body_bytes=32,
        )

        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            duplicate_body = b"{}"
            duplicate = client.post(
                "/mcp",
                headers=[
                    ("accept", "application/json"),
                    ("content-type", "application/json"),
                    ("cf-access-jwt-assertion", "opaque"),
                    ("cf-access-jwt-assertion", "duplicate"),
                    *_signed_header_items(
                        body=duplicate_body,
                        request_id="dup",
                    )[:-1],
                ],
                content=duplicate_body,
            )
            large_body = json.dumps(_initialize_payload("big")).encode("utf-8")
            oversized = client.post(
                "/mcp",
                headers=dict(
                    [
                        ("accept", "application/json"),
                        ("content-type", "application/json"),
                        *_signed_header_items(
                            body=large_body,
                            request_id="big",
                        ),
                    ]
                ),
                content=large_body,
            )

        self.assertEqual(duplicate.status_code, 403)
        self.assertEqual(
            duplicate.json(),
            {"detail": "Identity is not authorized for MIM."},
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(
            oversized.json(),
            {"detail": "Payload too large."},
        )

    def test_http_surface_authorizes_initialize_list_and_tool_call_once_per_request(
        self,
    ) -> None:
        app = build_mcp_http_app(fastmcp=self.server, gateway=self.gateway)

        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            initialize = client.post(
                "/mcp",
                headers=dict(
                    [
                        ("accept", "application/json"),
                        ("content-type", "application/json"),
                        *_signed_header_items(
                            body=json.dumps(_initialize_payload("init")).encode("utf-8"),
                            request_id="init",
                        ),
                    ]
                ),
                content=json.dumps(_initialize_payload("init")).encode("utf-8"),
            )
            tools_list = client.post(
                "/mcp",
                headers=dict(
                    [
                        ("accept", "application/json"),
                        ("content-type", "application/json"),
                        ("mcp-protocol-version", "2025-06-18"),
                        *_signed_header_items(
                            body=json.dumps(_tools_list_payload("list")).encode("utf-8"),
                            request_id="list",
                        ),
                    ]
                ),
                content=json.dumps(_tools_list_payload("list")).encode("utf-8"),
            )
            call_body = json.dumps(_tool_call_payload("call")).encode("utf-8")
            tool_call = client.post(
                "/mcp",
                headers=dict(
                    [
                        ("accept", "application/json"),
                        ("content-type", "application/json"),
                        ("mcp-protocol-version", "2025-06-18"),
                        *_signed_header_items(
                            body=call_body,
                            request_id="call",
                        ),
                    ]
                ),
                content=call_body,
            )

        self.assertEqual(initialize.status_code, 200)
        self.assertEqual(initialize.json()["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(tools_list.status_code, 200)
        self.assertEqual(
            [tool["name"] for tool in tools_list.json()["result"]["tools"]],
            [
                "explain_failure",
                "get_operation",
                "get_usage",
                "list_workloads",
                "plan_deploy",
            ],
        )
        self.assertEqual(tool_call.status_code, 200)
        self.assertIn("Alpha", tool_call.text)
        self.assertEqual(self.verifier.calls, 3)

    def test_http_surface_rejects_replayed_request_id(self) -> None:
        app = build_mcp_http_app(fastmcp=self.server, gateway=self.gateway)
        body = json.dumps(_tools_list_payload("replay")).encode("utf-8")
        headers = dict(
            [
                ("accept", "application/json"),
                ("content-type", "application/json"),
                ("mcp-protocol-version", "2025-06-18"),
                *_signed_header_items(body=body, request_id="replay"),
            ]
        )

        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            first = client.post("/mcp", headers=headers, content=body)
            second = client.post("/mcp", headers=headers, content=body)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 403)
        self.assertEqual(
            second.json(),
            {"detail": "Identity is not authorized for MIM."},
        )

    def test_http_surface_denies_non_mapping_scope_state_generically(self) -> None:
        body = json.dumps(_initialize_payload("bad-state")).encode("utf-8")
        sent: list[dict[str, object]] = []
        delivered = False

        async def receive() -> dict[str, object]:
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        async def inner_app(*_args: object) -> None:
            self.fail("inner MCP app must not receive a malformed ASGI state")

        app = AuthenticatedMcpHttpApp(
            gateway=self.gateway,
            inner_app=inner_app,
            max_body_bytes=64 * 1024,
        )
        raw_headers = [
            (name.encode("ascii"), value.encode("utf-8"))
            for name, value in [
                ("accept", "application/json"),
                ("content-type", "application/json"),
                *_signed_header_items(body=body, request_id="bad-state"),
            ]
        ]
        scope: dict[str, object] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "root_path": "",
            "headers": raw_headers,
            "state": object(),
        }

        asyncio.run(app(scope, receive, send))

        self.assertEqual(sent[0]["type"], "http.response.start")
        self.assertEqual(sent[0]["status"], 403)
        self.assertIn(b"Identity is not authorized for MIM", sent[1]["body"])


def _initialize_payload(request_id: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }


def _tools_list_payload(request_id: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/list",
        "params": {},
    }


def _tool_call_payload(request_id: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "list_workloads", "arguments": {}},
    }


def _signed_header_items(
    *,
    body: bytes,
    request_id: str,
) -> list[tuple[str, str]]:
    unsigned = OriginRequest(
        method="POST",
        path="/mcp",
        body=body,
        timestamp=NOW,
        request_id=OriginRequestId(request_id),
        public_host="mim.madup.app",
        destination_class="control-plane",
        key_id="edge-key",
        signature=None,
    )
    signature = sign_origin_request(unsigned, key=ORIGIN_KEY)
    return [
        ("x-mim-origin-key-id", "edge-key"),
        ("x-mim-origin-timestamp", str(int(NOW.timestamp()))),
        ("x-mim-origin-request-id", request_id),
        ("x-mim-origin-public-host", "mim.madup.app"),
        ("x-mim-origin-destination-class", "control-plane"),
        ("x-mim-origin-signature", signature),
        ("cf-access-jwt-assertion", "opaque"),
    ]
