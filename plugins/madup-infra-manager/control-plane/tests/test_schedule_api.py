from __future__ import annotations

import json
import unittest
from datetime import timedelta
from typing import cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from mim_control_plane.adapters.fake_identity import FakeActionPolicyAuthorizer
from mim_control_plane.adapters.fake_schedule import (
    FakeScheduleControlPort,
    FakeScheduleRunDispatcher,
)
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.api import build_api_app
from mim_control_plane.domain.central_identity import ActionName
from mim_control_plane.domain.models import (
    OrgCostGuard,
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
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.security.origin import OriginHmacVerifier
from mim_control_plane.services.central_identity import (
    ActionIntent,
    AuthorizedAction,
    IdentitySurface,
)
from mim_control_plane.services.schedule_management import ScheduleManagementService
from tests.test_api_readonly import (
    NOW,
    ORIGIN_KEY,
    build_gateway,
    claims,
    signed_headers,
)


class _UnusedDeploymentService:
    pass


_USE_DEFAULT_SCHEDULE_SERVICE = object()


class ScheduleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_org_cost_guard(
            OrgCostGuard(
                evaluated_at=NOW,
                latest_usage_collected_at=NOW,
                emergency_stop=False,
                org_policy_cost_krw=0,
            )
        )
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
                id=WorkloadId("wrk-script"),
                owner_id=UserId("usr-1"),
                repository_admission_id=RepositoryAdmissionId("repo-1"),
                name="hourly-report",
                kind=WorkloadKind.SCHEDULED_SCRIPT,
                state=WorkloadState.ACTIVE,
                source_sha="a" * 40,
                desired_manifest_hash="manifest-1",
                created_at=NOW - timedelta(days=1),
                updated_at=NOW,
                last_activity_at=NOW - timedelta(hours=1),
            )
        )
        self.scheduler = FakeScheduleControlPort()
        self.schedule_service = ScheduleManagementService(
            store=self.store,
            scheduler=self.scheduler,
            dispatcher=FakeScheduleRunDispatcher(),
            clock=lambda: NOW,
            id_factory=self._id_factory,
            lease_token_factory=lambda: "lease-api-fixed",
        )

    def _id_factory(self, prefix: str) -> str:
        counters = getattr(self, "_counters", {})
        counters[prefix] = counters.get(prefix, 0) + 1
        self._counters = counters
        return f"{prefix}-api-{counters[prefix]}"

    def client(
        self,
        *,
        enabled: bool,
        authorizer: FakeActionPolicyAuthorizer | None = None,
        schedule_management: ScheduleManagementService | object = (
            _USE_DEFAULT_SCHEDULE_SERVICE
        ),
    ) -> tuple[TestClient, FakeActionPolicyAuthorizer]:
        policy_authorizer = (
            FakeActionPolicyAuthorizer() if authorizer is None else authorizer
        )
        gateway, _ = build_gateway(
            store=self.store,
            claims=claims(subject="usr-1", email="person@madup.com"),
            authorizer=policy_authorizer,
        )
        resolved_schedule_management = (
            self.schedule_service
            if schedule_management is _USE_DEFAULT_SCHEDULE_SERVICE
            else cast(ScheduleManagementService | None, schedule_management)
        )
        return (
            TestClient(
                build_api_app(
                    store=self.store,
                    gateway=gateway,
                    clock=lambda: NOW,
                    mutations_enabled=enabled,
                    deployment_service=(
                        _UnusedDeploymentService()  # type: ignore[arg-type]
                        if enabled
                        else None
                    ),
                    github_origin_verifier=(
                        OriginHmacVerifier(
                            keys={"edge-key": ORIGIN_KEY},
                            store=self.store,
                            clock=lambda: NOW,
                            window=timedelta(seconds=60),
                        )
                        if enabled
                        else None
                    ),
                    schedule_management=resolved_schedule_management,
                ),
            ),
            policy_authorizer,
        )

    def test_plan_create_pause_and_resume_use_the_schedule_service(self) -> None:
        client, _ = self.client(enabled=True)
        plan_path = "/v1/plan/schedule?workload_id=wrk-script"
        planned = client.get(
            plan_path,
            headers=signed_headers(
                method="GET",
                path=plan_path,
                request_id="req-schedule-plan",
            ),
        )
        self.assertEqual(planned.status_code, 200)
        plan = planned.json()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(
            plan["policy"],
            {"cron": "0 * * * *", "timezone": "Asia/Seoul"},
        )

        create_body = _body(
            {
                "idempotency_key": "schedule-api-1",
                "plan_hash": plan["plan_hash"],
                "plan_id": plan["plan_id"],
            }
        )
        created = client.post(
            "/v1/schedules",
            content=create_body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path="/v1/schedules",
                    body=create_body,
                    request_id="req-schedule-create",
                )
            )
            | {"Content-Type": "application/json"},
        )
        self.assertEqual(created.status_code, 202)
        schedule_id = created.json()["schedule_id"]

        for action, expected_state in (("pause", "paused"), ("resume", "enabled")):
            path = f"/v1/schedules/{schedule_id}/{action}"
            response = client.post(
                path,
                content=b"",
                headers=signed_headers(
                    method="POST",
                    path=path,
                    body=b"",
                    request_id=f"req-schedule-{action}",
                ),
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["state"], expected_state)

    def test_plan_schedule_requires_schedule_service_and_workload_scope(self) -> None:
        authorizer = FakeActionPolicyAuthorizer()
        authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.MANAGE_SCHEDULE.value,
            resource_id="dashboard:usr-1",
            reason_code="legacy_schedule_scope_denied",
        )
        client, authorizer = self.client(enabled=False, authorizer=authorizer)
        plan_path = "/v1/plan/schedule?workload_id=wrk-script"
        planned = client.get(
            plan_path,
            headers=signed_headers(
                method="GET",
                path=plan_path,
                request_id="req-schedule-plan-scope",
            ),
        )
        self.assertEqual(planned.status_code, 200)
        self.assertEqual(authorizer.calls[-1].intent.action, ActionName.MANAGE_SCHEDULE)
        self.assertEqual(authorizer.calls[-1].intent.resource_id, "workload:wrk-script")

        unavailable, _ = self.client(enabled=False, schedule_management=None)
        rejected = unavailable.get(
            plan_path,
            headers=signed_headers(
                method="GET",
                path=plan_path,
                request_id="req-schedule-plan-unavailable",
            ),
        )
        self.assertEqual(rejected.status_code, 503)
        self.assertEqual(
            rejected.json(),
            {"detail": "Schedule dependencies are not configured."},
        )

    def test_create_schedule_authorizes_exact_plan_scope(self) -> None:
        authorizer = FakeActionPolicyAuthorizer()
        authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.MANAGE_SCHEDULE.value,
            resource_id="dashboard:usr-1",
            reason_code="legacy_schedule_scope_denied",
        )
        client, authorizer = self.client(enabled=True, authorizer=authorizer)
        plan_path = "/v1/plan/schedule?workload_id=wrk-script"
        plan_response = client.get(
            plan_path,
            headers=signed_headers(
                method="GET",
                path=plan_path,
                request_id="req-schedule-plan-create-scope",
            ),
        )
        self.assertEqual(plan_response.status_code, 200)
        plan = plan_response.json()

        create_body = _body(
            {
                "idempotency_key": "schedule-api-plan-scope",
                "plan_hash": plan["plan_hash"],
                "plan_id": plan["plan_id"],
            }
        )
        created = client.post(
            "/v1/schedules",
            content=create_body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path="/v1/schedules",
                    body=create_body,
                    request_id="req-schedule-create-scope",
                )
            )
            | {"Content-Type": "application/json"},
        )
        self.assertEqual(created.status_code, 202)
        self.assertEqual(authorizer.calls[-1].intent.action, ActionName.MANAGE_SCHEDULE)
        self.assertEqual(
            authorizer.calls[-1].intent.resource_id,
            f"deployment-plan:{plan['plan_id']}",
        )

    def test_create_schedule_reuses_authenticated_body_for_authorization(self) -> None:
        client, _ = self.client(enabled=True)
        plan_path = "/v1/plan/schedule?workload_id=wrk-script"
        plan = client.get(
            plan_path,
            headers=signed_headers(
                method="GET",
                path=plan_path,
                request_id="req-schedule-plan-auth-body",
            ),
        ).json()
        body = _body(
            {
                "idempotency_key": "schedule-api-auth-body",
                "plan_hash": plan["plan_hash"],
                "plan_id": plan["plan_id"],
            }
        )
        principal = AuthenticatedPrincipal(
            user_id=UserId("usr-1"),
            email="person@madup.com",
            role=UserRole.USER,
        )
        captured: dict[str, object] = {}

        async def fake_authorize_api_request(**kwargs: object) -> AuthorizedAction:
            captured["authenticated_body"] = kwargs.get("authenticated_body")
            resource_id_factory = kwargs["resource_id_factory"]
            assert callable(resource_id_factory)
            captured["resource_id"] = resource_id_factory(principal)
            return AuthorizedAction(
                principal=principal,
                intent=ActionIntent(
                    action=ActionName.MANAGE_SCHEDULE,
                    resource_id=f"deployment-plan:{plan['plan_id']}",
                ),
                surface=IdentitySurface.BROWSER,
            )

        with patch(
            "mim_control_plane.api.authorize_api_request",
            new=fake_authorize_api_request,
        ):
            response = client.post(
                "/v1/schedules",
                content=body,
                headers=dict(
                    signed_headers(
                        method="POST",
                        path="/v1/schedules",
                        body=body,
                        request_id="req-schedule-auth-body",
                    )
                )
                | {"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["authenticated_body"], body)
        self.assertEqual(
            captured["resource_id"],
            f"deployment-plan:{plan['plan_id']}",
        )

    def test_schedule_mutations_default_closed_and_reject_extra_fields(self) -> None:
        disabled, _ = self.client(enabled=False)
        self.assertEqual(disabled.post("/v1/schedules", json={}).status_code, 404)

        authorizer = FakeActionPolicyAuthorizer()
        client, authorizer = self.client(enabled=True, authorizer=authorizer)
        body = _body(
            {
                "idempotency_key": "schedule-api-1",
                "plan_hash": "a" * 64,
                "plan_id": "plan-1",
                "workload_id": "wrk-script",
            }
        )
        response = client.post(
            "/v1/schedules",
            content=body,
            headers=dict(
                signed_headers(
                    method="POST",
                    path="/v1/schedules",
                    body=body,
                    request_id="req-schedule-extra",
                )
            )
            | {"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("workload_id", response.text)
        self.assertEqual(authorizer.calls, [])

    def test_create_schedule_rejects_oversized_body_before_auth(self) -> None:
        authorizer = FakeActionPolicyAuthorizer()
        client, authorizer = self.client(enabled=True, authorizer=authorizer)

        response = client.post(
            "/v1/schedules",
            content=b"x" * 4097,
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": "Payload too large."})
        self.assertEqual(authorizer.calls, [])

    def test_create_schedule_rejects_invalid_browser_auth_headers_before_body_read(
        self,
    ) -> None:
        client, authorizer = self.client(enabled=True)
        body = _body(
            {
                "idempotency_key": "schedule-api-invalid-browser-auth",
                "plan_hash": "a" * 64,
                "plan_id": "plan-1",
            }
        )
        headers = dict(
            signed_headers(
                method="POST",
                path="/v1/schedules",
                body=body,
                request_id="req-schedule-invalid-browser-auth",
            )
        )
        headers["x-mim-origin-public-host"] = "evil.madup.app"

        with patch(
            "mim_control_plane.api.read_bounded_http_body",
            side_effect=AssertionError("body reader should not run"),
        ):
            response = client.post(
                "/v1/schedules",
                content=body,
                headers=headers | {"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "Identity is not authorized for MIM."},
        )
        self.assertEqual(authorizer.calls, [])

    def test_mutations_enabled_require_deployment_dependencies_at_build_time(
        self,
    ) -> None:
        gateway, _ = build_gateway(
            store=self.store,
            claims=claims(subject="usr-1", email="person@madup.com"),
            authorizer=FakeActionPolicyAuthorizer(),
        )
        with self.assertRaisesRegex(
            ValueError,
            "Deployment mutation dependencies must be configured together.",
        ):
            build_api_app(
                store=self.store,
                gateway=gateway,
                clock=lambda: NOW,
                mutations_enabled=True,
                schedule_management=self.schedule_service,
            )


def _body(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
