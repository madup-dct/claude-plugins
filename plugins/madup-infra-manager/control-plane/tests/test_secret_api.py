from __future__ import annotations

import unittest
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mim_control_plane.adapters.fake_identity import FakeActionPolicyAuthorizer
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.central_identity import ActionName
from mim_control_plane.domain.models import (
    RepositoryAdmissionId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.secret_api import build_secret_handoff_path, build_secret_router
from tests.test_api_readonly import NOW, build_gateway, claims, signed_headers


class StubSecretService:
    def __init__(self) -> None:
        self.apply_calls: list[dict[str, object]] = []
        self.replayed = False
        self.raise_denied = False
        self.include_unexpected_result = False

    def apply_secret_plan(self, **kwargs: object) -> dict[str, object]:
        self.apply_calls.append(kwargs)
        if self.raise_denied:
            raise PermissionError("super-secret-token")
        replayed = self.replayed
        self.replayed = True
        result = {
            "action": "apply_secret_plan",
            "operation_id": "op-secret-1",
            "secret_id": "sec-1",
            "mode": "create",
            "active_version": 1,
            "rotation_state": "stable",
            "retiring_version": None,
            "attached_workload_ids": ("wrk-1",),
            "replayed": replayed,
        }
        if self.include_unexpected_result:
            result["raw_value"] = "super-secret-token"
        return result


class SecretApiTests(unittest.TestCase):
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
                repository_admission_id=RepositoryAdmissionId("repo-1"),
                name="secret-target",
                kind=WorkloadKind.STREAMLIT,
                state=WorkloadState.ACTIVE,
                source_sha="a" * 40,
                desired_manifest_hash="manifest-1",
                created_at=NOW - timedelta(days=1),
                updated_at=NOW,
            )
        )

    def client(
        self,
        *,
        enabled: bool,
        authorizer: FakeActionPolicyAuthorizer | None = None,
        secret_service: StubSecretService | None = None,
    ) -> tuple[TestClient, FakeActionPolicyAuthorizer, StubSecretService]:
        policy_authorizer = (
            FakeActionPolicyAuthorizer() if authorizer is None else authorizer
        )
        service = StubSecretService() if secret_service is None else secret_service
        gateway, _ = build_gateway(
            store=self.store,
            claims=claims(subject="usr-1", email="person@madup.com"),
            authorizer=policy_authorizer,
        )
        app = FastAPI()
        app.include_router(
            build_secret_router(
                gateway=gateway,
                secret_management=service,
                mutations_enabled=enabled,
            )
        )
        return TestClient(app), policy_authorizer, service

    def test_enabled_router_requires_secret_service_dependency(self) -> None:
        gateway, _ = build_gateway(
            store=self.store,
            claims=claims(subject="usr-1", email="person@madup.com"),
            authorizer=FakeActionPolicyAuthorizer(),
        )
        with self.assertRaisesRegex(
            ValueError,
            "Secret mutation dependencies must be configured together.",
        ):
            build_secret_router(gateway=gateway, mutations_enabled=True)

    def test_handoff_form_requires_exact_plan_scope_and_returns_locked_html(
        self,
    ) -> None:
        authorizer = FakeActionPolicyAuthorizer()
        authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.DEPLOY_WORKLOAD.value,
            resource_id="dashboard:usr-1",
            reason_code="legacy_dashboard_scope_denied",
        )
        client, authorizer, _ = self.client(enabled=True, authorizer=authorizer)
        path = build_secret_handoff_path(
            plan_id="plan-1",
            plan_hash="a" * 64,
            idempotency_key="idem-1",
        )
        response = client.get(
            path,
            headers=signed_headers(
                method="GET",
                path=path,
                request_id="req-secret-form",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('type="password"', response.text)
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("http://", response.text)
        self.assertNotIn("https://", response.text)
        self.assertEqual(authorizer.calls[-1].intent.action, ActionName.DEPLOY_WORKLOAD)
        self.assertEqual(
            authorizer.calls[-1].intent.resource_id,
            "deployment-plan:plan-1",
        )

    def test_handoff_post_submits_raw_bytes_without_echo_and_replays(self) -> None:
        client, _, service = self.client(enabled=True)
        path = build_secret_handoff_path(
            plan_id="plan-1",
            plan_hash="a" * 64,
            idempotency_key="idem-1",
        )
        body = b"super-secret-token"
        headers = dict(
            signed_headers(
                method="POST",
                path=path,
                body=body,
                request_id="req-secret-submit-1",
            )
        ) | {
            "Content-Type": "application/octet-stream",
            "X-MIM-Secret-Handoff": "same-origin",
        }
        first = client.post(path, content=body, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["replayed"])
        self.assertEqual(service.apply_calls[0]["payload"], body)
        self.assertNotIn("super-secret-token", first.text)
        self.assertNotIn("payload_sha256", first.text)
        self.assertEqual(first.headers["Cache-Control"], "no-store")

        replay_headers = dict(
            signed_headers(
                method="POST",
                path=path,
                body=body,
                request_id="req-secret-submit-2",
            )
        ) | {
            "Content-Type": "application/octet-stream",
            "X-MIM-Secret-Handoff": "same-origin",
        }
        replay = client.post(path, content=body, headers=replay_headers)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])

    def test_handoff_post_rejects_invalid_headers_query_and_auth(self) -> None:
        client, _, _ = self.client(enabled=True)
        valid_path = build_secret_handoff_path(
            plan_id="plan-1",
            plan_hash="a" * 64,
            idempotency_key="idem-1",
        )
        body = b"x"

        missing_same_origin = client.post(
            valid_path,
            content=body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path=valid_path,
                    body=body,
                    request_id="req-secret-missing-header",
                )
            )
            | {"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(missing_same_origin.status_code, 400)

        duplicate_query_path = (
            "/v1/secrets/handoff?"
            f"plan_id=plan-1&plan_id=plan-2&plan_hash={'a' * 64}&idempotency_key=idem-1"
        )
        duplicate_query = client.post(
            duplicate_query_path,
            content=body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path=duplicate_query_path,
                    body=body,
                    request_id="req-secret-dup-query",
                )
            )
            | {
                "Content-Type": "application/octet-stream",
                "X-MIM-Secret-Handoff": "same-origin",
            },
        )
        self.assertEqual(duplicate_query.status_code, 400)

        oversized_body = b"x" * (16 * 1024 + 1)
        oversized = client.post(
            valid_path,
            content=oversized_body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path=valid_path,
                    body=oversized_body,
                    request_id="req-secret-oversize",
                )
            )
            | {
                "Content-Type": "application/octet-stream",
                "X-MIM-Secret-Handoff": "same-origin",
            },
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.json(), {"detail": "Payload too large."})

        bad_auth = client.post(
            valid_path,
            content=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-MIM-Origin-Key-Id": "edge-key",
                "X-MIM-Origin-Timestamp": str(int(NOW.timestamp())),
                "X-MIM-Origin-Request-Id": "req-secret-bad-auth",
                "X-MIM-Origin-Signature": "0" * 64,
                "Cf-Access-Jwt-Assertion": "opaque-token",
                "X-MIM-Secret-Handoff": "same-origin",
            },
        )
        self.assertEqual(bad_auth.status_code, 403)

    def test_handoff_post_rejects_wrong_content_headers_and_redacts_denials(
        self,
    ) -> None:
        service = StubSecretService()
        client, _, service = self.client(enabled=True, secret_service=service)
        path = build_secret_handoff_path(
            plan_id="plan-1",
            plan_hash="a" * 64,
            idempotency_key="idem-1",
        )
        body = b"super-secret-token"

        wrong_content_type = client.post(
            path,
            content=body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path=path,
                    body=body,
                    request_id="req-secret-content-type",
                )
            )
            | {
                "Content-Type": "text/plain",
                "X-MIM-Secret-Handoff": "same-origin",
            },
        )
        self.assertEqual(wrong_content_type.status_code, 400)

        encoded = client.post(
            path,
            content=body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path=path,
                    body=body,
                    request_id="req-secret-encoding",
                )
            )
            | {
                "Content-Type": "application/octet-stream",
                "Content-Encoding": "gzip",
                "X-MIM-Secret-Handoff": "same-origin",
            },
        )
        self.assertEqual(encoded.status_code, 400)

        service.raise_denied = True
        denied = client.post(
            path,
            content=body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path=path,
                    body=body,
                    request_id="req-secret-denied",
                )
            )
            | {
                "Content-Type": "application/octet-stream",
                "X-MIM-Secret-Handoff": "same-origin",
            },
        )
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json(), {"detail": "Secret request was denied."})
        self.assertNotIn("super-secret-token", denied.text)

    def test_handoff_post_rejects_noncanonical_service_result(self) -> None:
        service = StubSecretService()
        service.include_unexpected_result = True
        client, _, _ = self.client(enabled=True, secret_service=service)
        path = build_secret_handoff_path(
            plan_id="plan-1",
            plan_hash="a" * 64,
            idempotency_key="idem-1",
        )
        body = b"super-secret-token"

        response = client.post(
            path,
            content=body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path=path,
                    body=body,
                    request_id="req-secret-result-boundary",
                )
            )
            | {
                "Content-Type": "application/octet-stream",
                "X-MIM-Secret-Handoff": "same-origin",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Secret request was denied."})
        self.assertNotIn("super-secret-token", response.text)

    def test_handoff_post_rejects_nonexact_same_origin_header(self) -> None:
        client, _, service = self.client(enabled=True)
        path = build_secret_handoff_path(
            plan_id="plan-1",
            plan_hash="a" * 64,
            idempotency_key="idem-1",
        )
        body = b"x"

        response = client.post(
            path,
            content=body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path=path,
                    body=body,
                    request_id="req-secret-header-whitespace",
                )
            )
            | {
                "Content-Type": "application/octet-stream",
                "X-MIM-Secret-Handoff": " same-origin ",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(service.apply_calls, [])
