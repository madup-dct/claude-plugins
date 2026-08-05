from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mim_control_plane.app_gateway_api import (
    _parse_exact_json,
    mount_app_authorization_route,
)
from mim_control_plane.security.google_machine_identity import MachineRequestDenied
from mim_control_plane.services.app_gateway_authorization import (
    AppAuthorizationDecision,
    AppAuthorizationRequest,
    AppGatewayAuthorizationDenied,
)

NOW = datetime(2026, 8, 5, 5, 0, 0, tzinfo=UTC)
RUN_APP_SUFFIX = ".run" + ".app"
APP_SERVICE_ACCOUNT = (
    "mim-app-gateway@mim-prod-123456.iam.gserviceaccount.com"
)


class RecordingAuthenticator:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.calls: list[str] = []

    def authenticate(
        self,
        headers: tuple[tuple[str, str], ...],
        *,
        expected_service_account_email: str,
    ) -> None:
        del headers
        self.calls.append(expected_service_account_email)
        if self.deny:
            raise MachineRequestDenied("Machine request was denied.")


class FakeAuthorizationService:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.calls: list[AppAuthorizationRequest] = []

    def authorize(
        self,
        request: AppAuthorizationRequest,
    ) -> AppAuthorizationDecision:
        self.calls.append(request)
        if self.deny:
            raise AppGatewayAuthorizationDenied("App request was denied.")
        return AppAuthorizationDecision(
            schema="mim.app-authorization.v1",
            public_host=request.public_host,
            workload_id="wrk-1",
            upstream_url="https://mim-svc-bde131f06b2f-abcdefg-an.a"
            + RUN_APP_SUFFIX,
            upstream_audience="https://mim-svc-bde131f06b2f-abcdefg-an.a"
            + RUN_APP_SUFFIX,
            expires_at=NOW + timedelta(seconds=30),
        )


def payload() -> dict[str, object]:
    return {
        "schema": "mim.app-authorization.v1",
        "public_host": "north-star-bde131f06b2f.madup.app",
        "method": "GET",
        "request_target": "/",
        "access_subject": "usr-1",
        "access_email": "person@madup.com",
        "edge_request_id": "req-1",
        "edge_timestamp": int(NOW.timestamp()),
        "edge_body_sha256": "a" * 64,
    }


def headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer machine-token",
        "Content-Type": "application/json",
    }


class AppGatewayApiTests(unittest.TestCase):
    def build_client(
        self,
        *,
        deny_machine: bool = False,
        deny_service: bool = False,
    ) -> tuple[TestClient, RecordingAuthenticator, FakeAuthorizationService]:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        authenticator = RecordingAuthenticator(deny=deny_machine)
        authorization = FakeAuthorizationService(deny=deny_service)
        mount_app_authorization_route(
            app=app,
            authenticator=authenticator,
            expected_service_account_email=APP_SERVICE_ACCOUNT,
            authorization_service=authorization,
        )
        return TestClient(app), authenticator, authorization

    def test_route_returns_exact_decision_shape(self) -> None:
        client, authenticator, authorization = self.build_client()
        response = client.post("/v1/apps/authorize", json=payload(), headers=headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schema"], "mim.app-authorization.v1")
        self.assertEqual(response.json()["public_host"], payload()["public_host"])
        self.assertEqual(authenticator.calls, [APP_SERVICE_ACCOUNT])
        self.assertEqual(len(authorization.calls), 1)

    def test_route_maps_machine_and_policy_denials_without_leaking_details(
        self,
    ) -> None:
        denied_machine, _, _ = self.build_client(deny_machine=True)
        machine_response = denied_machine.post(
            "/v1/apps/authorize",
            json=payload(),
            headers=headers(),
        )
        self.assertEqual(machine_response.status_code, 403)
        self.assertEqual(
            machine_response.json(),
            {"detail": "Machine request was denied."},
        )

        denied_policy, _, _ = self.build_client(deny_service=True)
        policy_response = denied_policy.post(
            "/v1/apps/authorize",
            json=payload(),
            headers=headers(),
        )
        self.assertEqual(policy_response.status_code, 404)
        self.assertEqual(policy_response.json(), {"detail": "App request was denied."})

    def test_route_rejects_machine_identity_before_streaming_body(self) -> None:
        client, authenticator, authorization = self.build_client(deny_machine=True)
        body_reader = AsyncMock(side_effect=AssertionError("body must not be read"))

        with patch(
            "mim_control_plane.app_gateway_api.read_bounded_http_body",
            body_reader,
        ):
            response = client.post(
                "/v1/apps/authorize",
                content=b"{}",
                headers=headers(),
            )

        self.assertEqual(response.status_code, 403)
        body_reader.assert_not_awaited()
        self.assertEqual(authenticator.calls, [APP_SERVICE_ACCOUNT])
        self.assertEqual(authorization.calls, [])

    def test_route_rejects_duplicate_top_level_json_keys(self) -> None:
        client, _, authorization = self.build_client()

        response = client.post(
            "/v1/apps/authorize",
            content=(
                '{"schema":"mim.app-authorization.v1",'
                '"schema":"mim.app-authorization.v1",'
                '"public_host":"north-star-bde131f06b2f.madup.app",'
                '"method":"GET",'
                '"request_target":"/",'
                '"access_subject":"usr-1",'
                '"access_email":"person@madup.com",'
                '"edge_request_id":"req-1",'
                '"edge_timestamp":1754370000,'
                '"edge_body_sha256":"'
                + ("a" * 64)
                + '"}'
            ),
            headers=headers(),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "App request was denied."})
        self.assertEqual(authorization.calls, [])

    def test_route_rejects_oversized_body_before_machine_auth(self) -> None:
        client, authenticator, authorization = self.build_client()

        response = client.post(
            "/v1/apps/authorize",
            content=b"x" * 4097,
            headers=headers(),
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": "Payload too large."})
        self.assertEqual(authenticator.calls, [])
        self.assertEqual(authorization.calls, [])

    def test_parse_exact_json_rejects_duplicate_nested_object_keys(self) -> None:
        with self.assertRaises(ValueError):
            _parse_exact_json(
                (
                    b'{"schema":"mim.app-authorization.v1",'
                    b'"public_host":{"name":"north","name":"south"},'
                    b'"method":"GET",'
                    b'"request_target":"/",'
                    b'"access_subject":"usr-1",'
                    b'"access_email":"person@madup.com",'
                    b'"edge_request_id":"req-1",'
                    b'"edge_timestamp":1754370000,'
                    b'"edge_body_sha256":"'
                    + (b"a" * 64)
                    + b'"}'
                ),
                expected_keys=frozenset(payload()),
            )


if __name__ == "__main__":
    unittest.main()
