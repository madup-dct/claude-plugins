from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import unittest
from datetime import timedelta
from types import MappingProxyType
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from mim_control_plane.adapters.fake_execution import FakeDeploymentQueue
from mim_control_plane.adapters.fake_identity import FakeActionPolicyAuthorizer
from mim_control_plane.adapters.github import (
    MAX_WEBHOOK_BODY_BYTES,
    GitHubSourceUnavailableError,
)
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.api import _mutation_gate_from_environment, build_api_app
from mim_control_plane.config import PLAN_EXPIRY_MINUTES
from mim_control_plane.domain.models import (
    OperationId,
    OrgCostGuard,
    OriginRequestId,
    RepositoryAdmission,
    RepositoryAdmissionId,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    RepositoryAdmissionState,
    UserRole,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.http_body import read_bounded_http_body
from mim_control_plane.ports.execution import PrivateDeployEnqueuer, TaskConflictError
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.security.origin import (
    OriginHmacVerifier,
    OriginRequest,
    sign_origin_request,
)
from mim_control_plane.services.deployments import DeploymentDenied, DeploymentService
from mim_control_plane.services.render import DesiredStateRenderContext
from mim_control_plane.services.repository_admission import SelectedRepositoryPolicy
from tests.test_api_readonly import (
    NOW,
    ORIGIN_KEY,
    build_gateway,
    claims,
    signed_headers,
    user,
)

WEBHOOK_SECRET = b"w" * 32


def seed_org_cost_guard(
    store: MemoryStore,
    *,
    evaluated_at,
) -> None:
    store.create_org_cost_guard(
        OrgCostGuard(
            evaluated_at=evaluated_at,
            latest_usage_collected_at=evaluated_at,
            emergency_stop=False,
            org_policy_cost_krw=0,
        )
    )


class _BoundedBodyRequest:
    def __init__(
        self,
        *,
        headers: tuple[tuple[bytes, bytes], ...],
        chunks: tuple[bytes, ...],
    ) -> None:
        self.scope = {"headers": list(headers)}
        self._chunks = chunks
        self.stream_calls = 0

    async def stream(self):
        self.stream_calls += 1
        for chunk in self._chunks:
            yield chunk


class BoundedBodyTests(unittest.TestCase):
    def test_rejects_oversized_declared_body_before_streaming(self) -> None:
        request = _BoundedBodyRequest(
            headers=((b"content-length", b"5"),),
            chunks=(b"hello",),
        )

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(read_bounded_http_body(request, max_bytes=4))

        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(request.stream_calls, 0)

    def test_rejects_invalid_content_length_before_streaming(self) -> None:
        request = _BoundedBodyRequest(
            headers=((b"content-length", b"4"), (b"content-length", b"4")),
            chunks=(b"body",),
        )

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(read_bounded_http_body(request, max_bytes=8))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(request.stream_calls, 0)

    def test_reads_chunked_body_within_limit(self) -> None:
        request = _BoundedBodyRequest(
            headers=(),
            chunks=(b"ab", b"cd"),
        )

        body = asyncio.run(read_bounded_http_body(request, max_bytes=4))

        self.assertEqual(body, b"abcd")
        self.assertEqual(request.stream_calls, 1)


class FixedSource:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_snapshot(
        self,
        admission: RepositoryAdmission,
    ) -> MappingProxyType[str, bytes]:
        del admission
        self.calls += 1
        return MappingProxyType(
            {
                "app.py": b"import streamlit as st\nst.write('ok')\n",
                "requirements.txt": b"streamlit==1.40.0\n",
            }
        )


class ReplayOutageSource(FixedSource):
    def __init__(self, *, fail_after_calls: int) -> None:
        super().__init__()
        self.fail_after_calls = fail_after_calls

    def fetch_snapshot(
        self,
        admission: RepositoryAdmission,
    ) -> MappingProxyType[str, bytes]:
        self.calls += 1
        if self.calls > self.fail_after_calls:
            raise GitHubSourceUnavailableError("source transport unavailable")
        del admission
        return MappingProxyType(
            {
                "app.py": b"import streamlit as st\nst.write('ok')\n",
                "requirements.txt": b"streamlit==1.40.0\n",
            }
        )


class RecordingUsageScopeStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.usage_owner_ids: list[UserId | None] = []

    def list_usage_entries(
        self,
        *,
        owner_id: UserId | None = None,
    ):
        self.usage_owner_ids.append(owner_id)
        return super().list_usage_entries(owner_id=owner_id)


class DeploymentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        seed_org_cost_guard(self.store, evaluated_at=NOW)
        owner = self.store.create_user(
            user(
                user_id="usr-1",
                email="person@madup.com",
                role=UserRole.USER,
            )
        )
        admission = self.store.create_repository_admission(
            RepositoryAdmission(
                id=RepositoryAdmissionId("repo-1"),
                repository_numeric_id=123,
                owner="madupmarketing",
                name="streamlit-app",
                installation_id=456,
                state=RepositoryAdmissionState.ADMITTED,
                admitted_sha="a" * 40,
                created_at=NOW - timedelta(days=1),
                updated_at=NOW,
            )
        )
        self.workload = self.store.create_workload(
            Workload(
                id=WorkloadId("wrk-1"),
                owner_id=owner.id,
                repository_admission_id=admission.id,
                name="streamlit-app",
                kind=WorkloadKind.STREAMLIT,
                state=WorkloadState.ACTIVE,
                source_sha=admission.admitted_sha,
                desired_manifest_hash="manifest-1",
                created_at=NOW - timedelta(days=1),
                updated_at=NOW,
                auto_deploy_enabled=True,
                auto_deploy_ref="refs/heads/main",
            )
        )
        self.queue = FakeDeploymentQueue()
        self.deployment_service = DeploymentService(
            store=self.store,
            source=FixedSource(),
            enqueuer=PrivateDeployEnqueuer(queue=self.queue),
            render_context=DesiredStateRenderContext(
                project_id="mim-prod-123456",
                key_id="deploy-key-1",
            ),
            signing_key=b"s" * 32,
            clock=lambda: NOW + timedelta(seconds=1),
            id_factory=lambda prefix: f"{prefix}-api",
            github_policy=SelectedRepositoryPolicy(
                allowed_repository_ids=frozenset({123}),
                installation_id=456,
            ),
            github_webhook_secret=WEBHOOK_SECRET,
        )

    def client(self, *, enabled: bool) -> TestClient:
        gateway, _ = build_gateway(
            store=self.store,
            claims=claims(subject="usr-1", email="person@madup.com"),
            authorizer=FakeActionPolicyAuthorizer(),
        )
        return TestClient(
            build_api_app(
                store=self.store,
                gateway=gateway,
                clock=lambda: NOW + timedelta(seconds=1),
                deployment_service=self.deployment_service,
                github_origin_verifier=OriginHmacVerifier(
                    keys={"edge-key": ORIGIN_KEY},
                    store=self.store,
                    clock=lambda: NOW,
                    window=timedelta(seconds=60),
                ),
                mutations_enabled=enabled,
            )
        )

    def test_mutation_environment_gate_is_exact_and_defaults_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_mutation_gate_from_environment())
        with patch.dict(os.environ, {"MIM_ENABLE_MUTATIONS": "false"}):
            self.assertFalse(_mutation_gate_from_environment())
        with patch.dict(os.environ, {"MIM_ENABLE_MUTATIONS": "true"}):
            self.assertTrue(_mutation_gate_from_environment())
        for invalid in ("TRUE", "1", "yes", " true"):
            with self.subTest(invalid=invalid):
                with patch.dict(
                    os.environ,
                    {"MIM_ENABLE_MUTATIONS": invalid},
                ):
                    with self.assertRaises(ValueError):
                        _mutation_gate_from_environment()

    def test_public_mutation_routes_reject_oversized_bodies_before_auth(self) -> None:
        client = self.client(enabled=True)

        deploy = client.post(
            "/v1/deployments",
            content=b"x" * 4097,
            headers={"Content-Type": "application/json"},
        )
        webhook = client.post(
            "/v1/webhooks/github",
            content=b"x" * (MAX_WEBHOOK_BODY_BYTES + 1),
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Delivery": "77777777-7777-7777-7777-777777777777",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": "v0=" + "0" * 64,
            },
        )

        self.assertEqual(deploy.status_code, 413)
        self.assertEqual(deploy.json(), {"detail": "Payload too large."})
        self.assertEqual(webhook.status_code, 413)
        self.assertEqual(webhook.json(), {"detail": "Payload too large."})

    def test_deploy_rejects_invalid_browser_auth_headers_before_body_read(self) -> None:
        client = self.client(enabled=True)
        body = (
            b'{"plan_id":"plan-1","plan_hash":"'
            + (b"a" * 64)
            + b'","idempotency_key":"idem-1","correlation_id":"corr-1"}'
        )
        headers = dict(
            signed_headers(
                method="POST",
                path="/v1/deployments",
                body=body,
                request_id="req-invalid-browser-auth",
            )
        )
        headers["x-mim-origin-public-host"] = "evil.madup.app"

        with patch(
            "mim_control_plane.api.read_bounded_http_body",
            side_effect=AssertionError("body reader should not run"),
        ):
            response = client.post(
                "/v1/deployments",
                content=body,
                headers=headers | {"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "Identity is not authorized for MIM."},
        )

    def test_manual_route_requires_real_plan_and_default_gate_is_closed(self) -> None:
        disabled = self.client(enabled=False)
        self.assertEqual(disabled.post("/v1/deployments", json={}).status_code, 404)

        client = self.client(enabled=True)
        plan_path = "/v1/plan/deploy?workload_id=wrk-1"
        reviewed = client.get(
            plan_path,
            headers=signed_headers(
                method="GET",
                path=plan_path,
                request_id="req-api-plan",
            ),
        )
        self.assertEqual(reviewed.status_code, 200)
        plan = reviewed.json()
        self.assertEqual(
            plan["material_summary"],
            {
                "repository_owner": "madupmarketing",
                "repository_name": "streamlit-app",
                "selected_ref": "refs/heads/main",
                "immutable_sha": "a" * 40,
                "source_root": ".",
                "workload_kind": "streamlit",
                "deployment_target": "cloud_run_service",
                "resource_impact": "upsert_cloud_run_service",
                "current_month_policy_cost_krw": "0",
                "monthly_budget_cap_krw": "1000",
                "service_quota_limit": "2",
                "schedule_quota_limit": "3",
            },
        )
        self.assertNotIn("snapshot_digest", plan["material_summary"])
        self.assertNotIn("run.app", str(plan["material_summary"]))
        request_payload = {
            "correlation_id": "corr-api",
            "idempotency_key": "manual-api",
            "plan_hash": plan["plan_hash"],
            "plan_id": plan["plan_id"],
        }
        body = json.dumps(
            request_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        response = client.post(
            "/v1/deployments",
            content=body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path="/v1/deployments",
                    body=body,
                    request_id="req-api-deploy",
                )
            )
            | {"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["queued"])

        direct_body = b'{"workload_id":"wrk-1"}'
        direct = client.post(
            "/v1/deployments",
            content=direct_body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path="/v1/deployments",
                    body=direct_body,
                    request_id="req-api-direct",
                )
            )
            | {"Content-Type": "application/json"},
        )
        self.assertEqual(direct.status_code, 400)

    def test_webhook_uses_origin_hmac_then_github_without_cf_assertion(self) -> None:
        body = _push_body()
        unsigned = OriginRequest(
            method="POST",
            path="/v1/webhooks/github",
            body=body,
            timestamp=NOW,
            request_id=OriginRequestId("req-webhook-good"),
            public_host="mim.madup.app",
            destination_class="control-plane",
            key_id="edge-key",
            signature=None,
        )
        origin_signature = sign_origin_request(unsigned, key=ORIGIN_KEY)
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Delivery": "66666666-6666-6666-6666-666666666666",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _github_signature(body),
            "X-MIM-Origin-Key-Id": "edge-key",
            "X-MIM-Origin-Request-Id": "req-webhook-good",
            "X-MIM-Origin-Public-Host": "mim.madup.app",
            "X-MIM-Origin-Destination-Class": "control-plane",
            "X-MIM-Origin-Signature": origin_signature,
            "X-MIM-Origin-Timestamp": str(int(NOW.timestamp())),
        }
        client = self.client(enabled=True)

        missing_origin = client.post(
            "/v1/webhooks/github",
            content=body,
            headers={
                key: value
                for key, value in headers.items()
                if not key.startswith("X-MIM-Origin")
            },
        )
        self.assertEqual(missing_origin.status_code, 403)

        legacy_origin = client.post(
            "/v1/webhooks/github",
            content=body,
            headers={
                key: value
                for key, value in headers.items()
                if key
                not in {
                    "X-MIM-Origin-Public-Host",
                    "X-MIM-Origin-Destination-Class",
                }
            },
        )
        self.assertEqual(legacy_origin.status_code, 403)

        invalid_origin_headers = dict(headers)
        invalid_origin_headers["X-MIM-Origin-Request-Id"] = "req-webhook-bad"
        invalid_origin = client.post(
            "/v1/webhooks/github",
            content=body,
            headers=invalid_origin_headers,
        )
        self.assertEqual(invalid_origin.status_code, 403)

        response = client.post(
            "/v1/webhooks/github",
            content=body,
            headers=headers,
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["queued"])

    def test_cf_user_assertion_never_substitutes_for_github_signature(self) -> None:
        body = _push_body()
        unsigned = OriginRequest(
            method="POST",
            path="/v1/webhooks/github",
            body=body,
            timestamp=NOW,
            request_id=OriginRequestId("req-webhook-cf"),
            public_host="mim.madup.app",
            destination_class="control-plane",
            key_id="edge-key",
            signature=None,
        )
        headers = {
            "Cf-Access-Jwt-Assertion": "valid-browser-token",
            "Content-Type": "application/json",
            "X-GitHub-Delivery": "77777777-7777-7777-7777-777777777777",
            "X-GitHub-Event": "push",
            "X-MIM-Origin-Key-Id": "edge-key",
            "X-MIM-Origin-Request-Id": "req-webhook-cf",
            "X-MIM-Origin-Public-Host": "mim.madup.app",
            "X-MIM-Origin-Destination-Class": "control-plane",
            "X-MIM-Origin-Signature": sign_origin_request(unsigned, key=ORIGIN_KEY),
            "X-MIM-Origin-Timestamp": str(int(NOW.timestamp())),
        }
        response = self.client(enabled=True).post(
            "/v1/webhooks/github",
            content=body,
            headers=headers,
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("token", response.text.casefold())

    def test_deployment_policy_decisions_use_owner_scoped_usage_entries(self) -> None:
        store = RecordingUsageScopeStore()
        seed_org_cost_guard(store, evaluated_at=NOW)
        owner = store.create_user(
            user(
                user_id="usr-1",
                email="person@madup.com",
                role=UserRole.USER,
            )
        )
        admission = store.create_repository_admission(
            RepositoryAdmission(
                id=RepositoryAdmissionId("repo-1"),
                repository_numeric_id=123,
                owner="madupmarketing",
                name="streamlit-app",
                installation_id=456,
                state=RepositoryAdmissionState.ADMITTED,
                admitted_sha="a" * 40,
                created_at=NOW - timedelta(days=1),
                updated_at=NOW,
            )
        )
        target = store.create_workload(
            Workload(
                id=WorkloadId("wrk-1"),
                owner_id=owner.id,
                repository_admission_id=admission.id,
                name="streamlit-app",
                kind=WorkloadKind.STREAMLIT,
                state=WorkloadState.ACTIVE,
                source_sha=admission.admitted_sha,
                desired_manifest_hash="manifest-1",
                created_at=NOW - timedelta(days=1),
                updated_at=NOW,
                auto_deploy_enabled=True,
                auto_deploy_ref="refs/heads/main",
            )
        )
        service = DeploymentService(
            store=store,
            source=FixedSource(),
            enqueuer=PrivateDeployEnqueuer(queue=FakeDeploymentQueue()),
            render_context=DesiredStateRenderContext(
                project_id="mim-prod-123456",
                key_id="deploy-key-1",
            ),
            signing_key=b"s" * 32,
            clock=lambda: NOW + timedelta(seconds=1),
            id_factory=lambda prefix: f"{prefix}-api",
            github_policy=SelectedRepositoryPolicy(
                allowed_repository_ids=frozenset({123}),
                installation_id=456,
            ),
            github_webhook_secret=WEBHOOK_SECRET,
        )

        service._policy_decisions(workload=target)

        self.assertEqual(store.usage_owner_ids, [UserId("usr-1")])

    def test_deployment_policy_decisions_fail_closed_when_org_guard_is_missing(
        self,
    ) -> None:
        store = RecordingUsageScopeStore()
        owner = store.create_user(
            user(
                user_id="usr-1",
                email="person@madup.com",
                role=UserRole.USER,
            )
        )
        admission = store.create_repository_admission(
            RepositoryAdmission(
                id=RepositoryAdmissionId("repo-1"),
                repository_numeric_id=123,
                owner="madupmarketing",
                name="streamlit-app",
                installation_id=456,
                state=RepositoryAdmissionState.ADMITTED,
                admitted_sha="a" * 40,
                created_at=NOW - timedelta(days=1),
                updated_at=NOW,
            )
        )
        target = store.create_workload(
            Workload(
                id=WorkloadId("wrk-1"),
                owner_id=owner.id,
                repository_admission_id=admission.id,
                name="streamlit-app",
                kind=WorkloadKind.STREAMLIT,
                state=WorkloadState.ACTIVE,
                source_sha=admission.admitted_sha,
                desired_manifest_hash="manifest-1",
                created_at=NOW - timedelta(days=1),
                updated_at=NOW,
                auto_deploy_enabled=True,
                auto_deploy_ref="refs/heads/main",
            )
        )
        service = DeploymentService(
            store=store,
            source=FixedSource(),
            enqueuer=PrivateDeployEnqueuer(queue=FakeDeploymentQueue()),
            render_context=DesiredStateRenderContext(
                project_id="mim-prod-123456",
                key_id="deploy-key-1",
            ),
            signing_key=b"s" * 32,
            clock=lambda: NOW + timedelta(seconds=1),
            id_factory=lambda prefix: f"{prefix}-guard-missing",
            github_policy=SelectedRepositoryPolicy(
                allowed_repository_ids=frozenset({123}),
                installation_id=456,
            ),
            github_webhook_secret=WEBHOOK_SECRET,
        )

        with self.assertRaises(DeploymentDenied):
            service._policy_decisions(workload=target)

        self.assertEqual(store.usage_owner_ids, [])

    def test_consumed_plan_replay_reuses_durable_task_without_refetch_on_outage(
        self,
    ) -> None:
        source = ReplayOutageSource(fail_after_calls=2)
        service = DeploymentService(
            store=self.store,
            source=source,
            enqueuer=PrivateDeployEnqueuer(queue=self.queue),
            render_context=DesiredStateRenderContext(
                project_id="mim-prod-123456",
                key_id="deploy-key-1",
            ),
            signing_key=b"s" * 32,
            clock=lambda: NOW + timedelta(seconds=1),
            id_factory=lambda prefix: f"{prefix}-replay",
            github_policy=SelectedRepositoryPolicy(
                allowed_repository_ids=frozenset({123}),
                installation_id=456,
            ),
            github_webhook_secret=WEBHOOK_SECRET,
        )
        principal = AuthenticatedPrincipal(
            user_id=UserId("usr-1"),
            email="person@madup.com",
            role=UserRole.USER,
        )
        plan = service.plan_deploy(principal=principal, workload_id="wrk-1")

        first = service.deploy_from_plan(
            principal=principal,
            plan_id=plan["plan_id"],
            plan_hash=plan["plan_hash"],
            idempotency_key="manual-replay",
            correlation_id="corr-replay",
        )

        second = service.deploy_from_plan(
            principal=principal,
            plan_id=plan["plan_id"],
            plan_hash=plan["plan_hash"],
            idempotency_key="manual-replay",
            correlation_id="corr-replay",
        )

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(second["operation_id"], first["operation_id"])
        self.assertEqual(second["state"], first["state"])
        self.assertEqual(source.calls, 2)

    def test_consumed_plan_replay_repairs_missing_durable_task(self) -> None:
        source = FixedSource()
        repair_now = [NOW + timedelta(seconds=1)]
        service = DeploymentService(
            store=self.store,
            source=source,
            enqueuer=PrivateDeployEnqueuer(queue=self.queue),
            render_context=DesiredStateRenderContext(
                project_id="mim-prod-123456",
                key_id="deploy-key-1",
            ),
            signing_key=b"s" * 32,
            clock=lambda: repair_now[0],
            id_factory=lambda prefix: f"{prefix}-repair",
            github_policy=SelectedRepositoryPolicy(
                allowed_repository_ids=frozenset({123}),
                installation_id=456,
            ),
            github_webhook_secret=WEBHOOK_SECRET,
        )
        principal = AuthenticatedPrincipal(
            user_id=UserId("usr-1"),
            email="person@madup.com",
            role=UserRole.USER,
        )
        plan = service.plan_deploy(principal=principal, workload_id="wrk-1")

        with patch.object(
            self.store,
            "create_deploy_task_once",
            side_effect=TaskConflictError("simulated task persistence failure"),
        ):
            with self.assertRaises(DeploymentDenied):
                service.deploy_from_plan(
                    principal=principal,
                    plan_id=plan["plan_id"],
                    plan_hash=plan["plan_hash"],
                    idempotency_key="manual-repair",
                    correlation_id="corr-repair",
                )

        repair_now[0] = NOW + timedelta(minutes=PLAN_EXPIRY_MINUTES + 2)
        repaired = service.deploy_from_plan(
            principal=principal,
            plan_id=plan["plan_id"],
            plan_hash=plan["plan_hash"],
            idempotency_key="manual-repair",
            correlation_id="corr-repair",
        )

        self.assertTrue(repaired["replayed"])
        self.assertTrue(repaired["queued"])
        self.assertEqual(repaired["operation_id"], "operation-repair")
        self.assertEqual(source.calls, 3)
        self.assertEqual(
            self.queue.get(OperationId("operation-repair")).idempotency_key,
            "manual-repair",
        )


def _push_body() -> bytes:
    sha = "b" * 40
    return json.dumps(
        {
            "after": sha,
            "deleted": False,
            "head_commit": {"id": sha},
            "installation": {"id": 456},
            "ref": "refs/heads/main",
            "repository": {
                "fork": False,
                "full_name": "madupmarketing/streamlit-app",
                "id": 123,
                "name": "streamlit-app",
                "owner": {"login": "madupmarketing"},
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _github_signature(body: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()


if __name__ == "__main__":
    unittest.main()
