from __future__ import annotations

import dataclasses
import json
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Callable
from unittest.mock import patch

from fastapi.testclient import TestClient

from mim_control_plane.adapters.fake_identity import FakeActionPolicyAuthorizer
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.api import build_api_app
from mim_control_plane.domain.central_identity import ActionIntent, ActionName
from mim_control_plane.domain.models import (
    ActivityEvent,
    ActivityEventId,
    Operation,
    OperationId,
    OriginRequestId,
    Schedule,
    ScheduleId,
    SecretId,
    SecretMetadata,
    UsageEntry,
    UsageEntryId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    ActivityOutcome,
    ActivitySurface,
    OperationState,
    ScheduleState,
    SecretLifecycleState,
    SecretRotationState,
    UsageConfidence,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.security.authorization import IdentityPolicy
from mim_control_plane.security.identity import (
    AuthenticatedPrincipal,
    IdentityAuthenticator,
    IdentityClaims,
)
from mim_control_plane.security.origin import (
    OriginHmacVerifier,
    OriginRequest,
    sign_origin_request,
)
from mim_control_plane.services.central_identity import (
    AuthorizedAction,
    CentralIdentityGateway,
    IdentitySurface,
)

NOW = datetime(2026, 8, 4, 1, 0, 0, tzinfo=UTC)
ISSUER = "https://tenant.cloudflareaccess.com"
AUDIENCE = "audience-1"
GROUP = "mim-users"
ORIGIN_KEY = b"o" * 32


class CountingJwtVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self._claims = claims
        self.calls = 0

    def verify(self, token: str) -> IdentityClaims:
        del token
        self.calls += 1
        return self._claims


class UnexpectedDeploymentService:
    def __init__(self) -> None:
        self.plan_called = False
        self.deploy_called = False
        self.deploy_kwargs: dict[str, object] | None = None

    def plan_deploy(self, **_kwargs: object) -> dict[str, object]:
        self.plan_called = True
        raise AssertionError("deployment planner should not run")

    def deploy_from_plan(self, **kwargs: object) -> dict[str, object]:
        self.deploy_called = True
        self.deploy_kwargs = dict(kwargs)
        raise AssertionError("deployment executor should not run")


class RecordingDeploymentService(UnexpectedDeploymentService):
    def deploy_from_plan(self, **kwargs: object) -> dict[str, object]:
        self.deploy_called = True
        self.deploy_kwargs = dict(kwargs)
        return {"queued": True}


def user(
    *,
    user_id: str,
    email: str,
    role: UserRole,
) -> User:
    return User(
        id=UserId(user_id),
        email=email,
        role=role,
        state=UserState.ACTIVE,
        groups=frozenset({GROUP}),
        identity_synced_at=NOW - timedelta(minutes=5),
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(minutes=5),
    )


def workload(
    *,
    workload_id: str,
    owner_id: str,
    name: str,
    state: WorkloadState = WorkloadState.ACTIVE,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id="adm-1",
        name=name,
        kind=WorkloadKind.NEXTJS,
        state=state,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-1",
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(hours=6),
        last_activity_at=NOW - timedelta(hours=1),
    )


def schedule(
    *,
    schedule_id: str,
    owner_id: str,
    workload_id: str,
    state: ScheduleState = ScheduleState.ENABLED,
) -> Schedule:
    return Schedule(
        id=ScheduleId(schedule_id),
        owner_id=UserId(owner_id),
        workload_id=WorkloadId(workload_id),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=state,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(hours=3),
        consecutive_failures=1,
        last_attempt_at=NOW - timedelta(hours=2),
        last_success_at=NOW - timedelta(hours=6),
    )


def secret(
    *,
    secret_id: str,
    owner_id: str,
    workload_ids: tuple[str, ...],
) -> SecretMetadata:
    return SecretMetadata(
        id=SecretId(secret_id),
        owner_id=UserId(owner_id),
        name="ads-api",
        integration_type="meta",
        attached_workload_ids=tuple(WorkloadId(item) for item in workload_ids),
        active_version=3,
        rotation_state=SecretRotationState.STABLE,
        lifecycle_state=SecretLifecycleState.ACTIVE,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(hours=5),
    )


def operation(
    *,
    operation_id: str,
    actor_id: str,
    workload_id: str,
    state: OperationState,
    failure: str | None = None,
) -> Operation:
    return Operation(
        id=OperationId(operation_id),
        actor_id=UserId(actor_id),
        workload_id=WorkloadId(workload_id),
        action="deploy_workload",
        idempotency_key=f"idem-{operation_id}",
        request_hash=f"hash-{operation_id}",
        state=state,
        created_at=NOW - timedelta(hours=8),
        updated_at=NOW - timedelta(hours=1),
        sanitized_failure=failure,
    )


def usage_entry(
    *,
    entry_id: str,
    owner_id: str | None,
    workload_id: str | None,
    estimated_cost_krw: int,
    finalized_cost_krw: int | None = None,
) -> UsageEntry:
    return UsageEntry(
        id=UsageEntryId(entry_id),
        owner_id=None if owner_id is None else UserId(owner_id),
        workload_id=None if workload_id is None else WorkloadId(workload_id),
        service_category="cloud_run",
        estimated_cost_krw=estimated_cost_krw,
        finalized_cost_krw=finalized_cost_krw,
        confidence=UsageConfidence.FINALIZED,
        collected_at=NOW - timedelta(hours=4),
    )


def activity(
    *,
    event_id: str,
    user_id: str,
    surface: ActivitySurface,
    action: str,
    outcome: ActivityOutcome = ActivityOutcome.SUCCEEDED,
) -> ActivityEvent:
    return ActivityEvent(
        id=ActivityEventId(event_id),
        user_id=UserId(user_id),
        surface=surface,
        action=action,
        target_ref="dashboard:usr-1",
        outcome=outcome,
        latency_bucket="lt_250ms",
        correlation_id=f"corr-{event_id}",
        occurred_at=NOW - timedelta(hours=2),
    )


def signed_headers(
    *,
    method: str,
    path: str,
    body: bytes = b"",
    request_id: str = "req-1",
    token: str = "opaque-token",
    public_host: str = "mim.madup.app",
) -> list[tuple[str, str]]:
    unsigned = OriginRequest(
        method=method,
        path=path,
        body=body,
        timestamp=NOW,
        request_id=OriginRequestId(request_id),
        public_host=public_host,
        destination_class="control-plane",
        key_id="edge-key",
        signature=None,
    )
    signed = dataclasses.replace(
        unsigned,
        signature=sign_origin_request(unsigned, key=ORIGIN_KEY),
    )
    return [
        ("X-MIM-Origin-Key-Id", signed.key_id),
        ("X-MIM-Origin-Timestamp", str(int(signed.timestamp.timestamp()))),
        ("X-MIM-Origin-Request-Id", str(signed.request_id)),
        ("X-MIM-Origin-Public-Host", signed.public_host),
        ("X-MIM-Origin-Destination-Class", signed.destination_class),
        ("X-MIM-Origin-Signature", signed.signature or ""),
        ("Cf-Access-Jwt-Assertion", token),
    ]


def build_gateway(
    *,
    store: MemoryStore,
    claims: IdentityClaims,
    authorizer: FakeActionPolicyAuthorizer,
) -> tuple[CentralIdentityGateway, CountingJwtVerifier]:
    jwt_verifier = CountingJwtVerifier(claims)
    authenticator = IdentityAuthenticator(
        origin_verifier=OriginHmacVerifier(
            keys={"edge-key": ORIGIN_KEY},
            store=store,
            clock=lambda: NOW,
            window=timedelta(seconds=60),
        ),
        jwt_verifier=jwt_verifier,
        identity_policy=IdentityPolicy(
            store=store,
            issuer=ISSUER,
            audience=AUDIENCE,
            company_domain="madup.com",
            required_group=GROUP,
            max_staleness=timedelta(minutes=60),
            clock=lambda: NOW,
        ),
    )
    gateway = CentralIdentityGateway(
        browser_authenticator=authenticator,
        identity_policy=IdentityPolicy(
            store=store,
            issuer=ISSUER,
            audience=AUDIENCE,
            company_domain="madup.com",
            required_group=GROUP,
            max_staleness=timedelta(minutes=60),
            clock=lambda: NOW,
        ),
        shared_install_directory=object(),
        identity_link_directory=object(),
        action_authorizer=authorizer,
        required_slack_scopes=frozenset({"commands"}),
        clock=lambda: NOW,
    )
    return gateway, jwt_verifier


def claims(*, subject: str, email: str) -> IdentityClaims:
    return IdentityClaims(
        subject=subject,
        email=email,
        issuer=ISSUER,
        audience=(AUDIENCE,),
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


class ApiReadonlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(
            user(user_id="usr-1", email="person@madup.com", role=UserRole.USER)
        )
        self.store.create_user(
            user(user_id="usr-2", email="other@madup.com", role=UserRole.USER)
        )
        self.store.create_user(
            user(user_id="adm-1", email="admin@madup.com", role=UserRole.ADMIN)
        )

        self.store.create_workload(
            workload(workload_id="wrk-1", owner_id="usr-1", name="Alpha")
        )
        self.store.create_workload(
            workload(workload_id="wrk-2", owner_id="usr-2", name="Bravo")
        )
        self.store.create_schedule(
            schedule(schedule_id="sch-1", owner_id="usr-1", workload_id="wrk-1")
        )
        self.store.create_schedule(
            schedule(schedule_id="sch-2", owner_id="usr-2", workload_id="wrk-2")
        )
        self.store.create_secret_metadata(
            secret(secret_id="sec-1", owner_id="usr-1", workload_ids=("wrk-1",))
        )
        self.store.create_secret_metadata(
            secret(secret_id="sec-2", owner_id="usr-2", workload_ids=("wrk-2",))
        )
        self.store.create_operation_once(
            operation(
                operation_id="op-own",
                actor_id="usr-1",
                workload_id="wrk-1",
                state=OperationState.SUCCEEDED,
            )
        )
        self.store.create_operation_once(
            operation(
                operation_id="op-other",
                actor_id="usr-2",
                workload_id="wrk-2",
                state=OperationState.FAILED,
                failure=(
                    "Bearer secret-token from Cookie: session=abc at 127.0.0.1 curl/8.0"
                ),
            )
        )
        self.store.append_usage_entry(
            usage_entry(
                entry_id="use-1",
                owner_id="usr-1",
                workload_id="wrk-1",
                estimated_cost_krw=400,
                finalized_cost_krw=400,
            )
        )
        self.store.append_usage_entry(
            usage_entry(
                entry_id="use-2",
                owner_id="usr-2",
                workload_id="wrk-2",
                estimated_cost_krw=200,
                finalized_cost_krw=200,
            )
        )
        self.store.append_usage_entry(
            usage_entry(
                entry_id="use-shared",
                owner_id=None,
                workload_id=None,
                estimated_cost_krw=300,
                finalized_cost_krw=300,
            )
        )
        self.store.append_activity_event(
            activity(
                event_id="evt-1",
                user_id="usr-1",
                surface=ActivitySurface.DASHBOARD,
                action="view_dashboard",
            )
        )
        self.store.append_activity_event(
            activity(
                event_id="evt-2",
                user_id="usr-2",
                surface=ActivitySurface.MCP,
                action="get_operation",
                outcome=ActivityOutcome.FAILED,
            )
        )
        self.store.record_maintenance_job_started(
            job_name="identity-sync",
            run_id="run-identity",
            started_at=NOW - timedelta(minutes=10),
        )
        usage_started = self.store.record_maintenance_job_started(
            job_name="usage-ingest",
            run_id="run-usage",
            started_at=NOW - timedelta(hours=3),
        )
        self.store.record_maintenance_job_terminal(
            job_name="usage-ingest",
            run_id="run-usage",
            expected_version=usage_started.version,
            finished_at=NOW - timedelta(hours=3) + timedelta(minutes=3),
            outcome="failed",
            summary=(("billing_appended_entries", 0),),
            failure_code="runtime_error",
            failure_class="internal",
        )

    def build_client(
        self,
        *,
        subject: str,
        email: str,
        authorizer: FakeActionPolicyAuthorizer | None = None,
        readiness_check: Callable[[], None] | None = None,
    ) -> tuple[TestClient, FakeActionPolicyAuthorizer, CountingJwtVerifier]:
        fake_authorizer = authorizer or FakeActionPolicyAuthorizer()
        gateway, jwt_verifier = build_gateway(
            store=self.store,
            claims=claims(subject=subject, email=email),
            authorizer=fake_authorizer,
        )
        client = TestClient(
            build_api_app(
                store=self.store,
                gateway=gateway,
                clock=lambda: NOW,
                readiness_check=readiness_check,
            )
        )
        return client, fake_authorizer, jwt_verifier

    def build_mutation_client(
        self,
        *,
        subject: str,
        email: str,
        deployment_service: object,
        authorizer: FakeActionPolicyAuthorizer | None = None,
    ) -> tuple[TestClient, FakeActionPolicyAuthorizer]:
        fake_authorizer = authorizer or FakeActionPolicyAuthorizer()
        gateway, _ = build_gateway(
            store=self.store,
            claims=claims(subject=subject, email=email),
            authorizer=fake_authorizer,
        )
        client = TestClient(
            build_api_app(
                store=self.store,
                gateway=gateway,
                clock=lambda: NOW,
                deployment_service=deployment_service,  # type: ignore[arg-type]
                github_origin_verifier=OriginHmacVerifier(
                    keys={"edge-key": ORIGIN_KEY},
                    store=self.store,
                    clock=lambda: NOW,
                    window=timedelta(seconds=60),
                ),
                mutations_enabled=True,
            )
        )
        return client, fake_authorizer

    def test_healthz_and_readyz_require_authenticated_origin_and_identity(self) -> None:
        client, _, _ = self.build_client(subject="usr-1", email="person@madup.com")
        for path in ("/healthz", "/readyz"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.json(),
                    {"detail": "Identity is not authorized for MIM."},
                )

    def test_healthz_uses_dashboard_authorization_and_returns_ok(self) -> None:
        client, authorizer, _ = self.build_client(
            subject="usr-1",
            email="person@madup.com",
        )

        response = client.get(
            "/healthz",
            headers=signed_headers(
                method="GET",
                path="/healthz",
                request_id="req-healthz",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(authorizer.calls[0].intent.action, ActionName.VIEW_DASHBOARD)
        self.assertEqual(authorizer.calls[0].intent.resource_id, "dashboard:usr-1")

    def test_readyz_uses_dashboard_authorization_and_returns_readiness_state(
        self,
    ) -> None:
        client, authorizer, _ = self.build_client(
            subject="usr-1",
            email="person@madup.com",
        )

        ready = client.get(
            "/readyz",
            headers=signed_headers(
                method="GET",
                path="/readyz",
                request_id="req-readyz",
            ),
        )

        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json(), {"status": "ready"})
        self.assertEqual(authorizer.calls[0].intent.action, ActionName.VIEW_DASHBOARD)
        self.assertEqual(authorizer.calls[0].intent.resource_id, "dashboard:usr-1")

        def fail_readiness() -> None:
            raise RuntimeError("dependency unavailable")

        failing_client, failing_authorizer, _ = self.build_client(
            subject="usr-1",
            email="person@madup.com",
            readiness_check=fail_readiness,
        )
        not_ready = failing_client.get(
            "/readyz",
            headers=signed_headers(
                method="GET",
                path="/readyz",
                request_id="req-readyz-fail",
            ),
        )

        self.assertEqual(not_ready.status_code, 503)
        self.assertEqual(not_ready.json(), {"status": "not_ready"})
        self.assertEqual(
            failing_authorizer.calls[0].intent.action,
            ActionName.VIEW_DASHBOARD,
        )
        self.assertEqual(
            failing_authorizer.calls[0].intent.resource_id,
            "dashboard:usr-1",
        )

    def test_default_documentation_routes_are_not_exposed(self) -> None:
        client, _, _ = self.build_client(subject="usr-1", email="person@madup.com")
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)

    def test_workloads_require_origin_before_jwt(self) -> None:
        client, _, jwt_verifier = self.build_client(
            subject="usr-1",
            email="person@madup.com",
        )

        denied = client.get(
            "/v1/workloads",
            headers={
                "X-MIM-Origin-Key-Id": "edge-key",
                "X-MIM-Origin-Timestamp": str(int(NOW.timestamp())),
                "X-MIM-Origin-Request-Id": "req-bad",
                "X-MIM-Origin-Signature": "0" * 64,
                "Cf-Access-Jwt-Assertion": "opaque-token",
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(jwt_verifier.calls, 0)

    def test_legacy_origin_proof_without_host_and_destination_fails_closed(
        self,
    ) -> None:
        client, _, jwt_verifier = self.build_client(
            subject="usr-1",
            email="person@madup.com",
        )
        response = client.get(
            "/v1/workloads",
            headers={
                "X-MIM-Origin-Key-Id": "edge-key",
                "X-MIM-Origin-Timestamp": str(int(NOW.timestamp())),
                "X-MIM-Origin-Request-Id": "req-legacy",
                "X-MIM-Origin-Signature": "0" * 64,
                "Cf-Access-Jwt-Assertion": "opaque-token",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(jwt_verifier.calls, 0)

    def test_user_scope_returns_only_owned_records_and_secret_metadata(self) -> None:
        client, authorizer, _ = self.build_client(
            subject="usr-1",
            email="person@madup.com",
        )
        response = client.get(
            "/v1/workloads",
            headers=signed_headers(
                method="GET", path="/v1/workloads", request_id="req-workloads"
            ),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scope"], "user")
        self.assertEqual([item["id"] for item in payload["workloads"]], ["wrk-1"])
        self.assertEqual([item["id"] for item in payload["schedules"]], ["sch-1"])
        self.assertEqual([item["id"] for item in payload["secrets"]], ["sec-1"])
        self.assertNotIn("secret_value", str(payload))
        self.assertNotIn("token", str(payload).casefold())
        self.assertEqual(authorizer.calls[0].intent.action, ActionName.VIEW_DASHBOARD)
        self.assertEqual(authorizer.calls[0].intent.resource_id, "dashboard:usr-1")

    def test_duplicate_identity_headers_fail_closed(self) -> None:
        client, _, _ = self.build_client(subject="usr-1", email="person@madup.com")
        response = client.get(
            "/v1/workloads",
            headers=signed_headers(
                method="GET", path="/v1/workloads", request_id="req-dup"
            )
            + [("Cf-Access-Jwt-Assertion", "duplicate-token")],
        )
        self.assertEqual(response.status_code, 403)

    def test_browser_surface_rejects_hidden_credential_and_spoofing_headers(
        self,
    ) -> None:
        denied_headers = (
            ("Authorization", "Bearer shadow-token"),
            ("Cookie", "CF_Authorization=shadow-cookie"),
            ("Proxy-Authorization", "Bearer shadow-proxy"),
            ("Cf-Access-Authenticated-User-Email", "person@madup.com"),
            ("X-MIM-Origin-Trace", "unexpected"),
            ("X-MIM-App-Proof", "unexpected"),
            ("X-Forwarded-For", "198.51.100.7"),
            ("Forwarded", "for=198.51.100.7;proto=https"),
        )
        for name, value in denied_headers:
            with self.subTest(name=name):
                client, _, jwt_verifier = self.build_client(
                    subject="usr-1",
                    email="person@madup.com",
                )
                response = client.get(
                    "/v1/workloads",
                    headers=signed_headers(
                        method="GET",
                        path="/v1/workloads",
                        request_id=f"req-hidden-{name.casefold()}",
                    )
                    + [(name, value)],
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(jwt_verifier.calls, 0)

    def test_cross_user_operation_and_failure_are_not_exposed(self) -> None:
        client, _, _ = self.build_client(subject="usr-1", email="person@madup.com")
        operation_response = client.get(
            "/v1/operations/op-other",
            headers=signed_headers(
                method="GET", path="/v1/operations/op-other", request_id="req-op-other"
            ),
        )
        failure_response = client.get(
            "/v1/failures/op-other",
            headers=signed_headers(
                method="GET", path="/v1/failures/op-other", request_id="req-fail-other"
            ),
        )
        self.assertEqual(operation_response.status_code, 404)
        self.assertEqual(failure_response.status_code, 404)

    def test_admin_usage_overview_includes_platform_bucket(self) -> None:
        client, authorizer, _ = self.build_client(
            subject="adm-1",
            email="admin@madup.com",
        )
        response = client.get(
            "/v1/usage",
            headers=signed_headers(
                method="GET", path="/v1/usage", request_id="req-usage-admin"
            ),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scope"], "admin")
        self.assertEqual(payload["costs"]["platform_shared"]["estimated_krw"], 300)
        self.assertEqual(payload["costs"]["organization"]["estimated_krw"], 900)
        self.assertEqual(
            sorted(item["user_id"] for item in payload["users"]),
            ["adm-1", "usr-1", "usr-2"],
        )
        self.assertIn("maintenance_jobs", payload)
        self.assertEqual(
            sorted(item["job_name"] for item in payload["maintenance_jobs"]),
            ["identity-sync", "lifecycle", "usage-ingest"],
        )
        maintenance = {
            item["job_name"]: item for item in payload["maintenance_jobs"]
        }
        self.assertEqual(maintenance["usage-ingest"]["failure_class"], "internal")
        self.assertNotIn("RuntimeError", str(payload))
        self.assertEqual(
            authorizer.calls[0].intent.action, ActionName.ADMIN_USAGE_OVERVIEW
        )
        self.assertEqual(authorizer.calls[0].intent.resource_id, "admin:overview")

    def test_dashboard_view_appends_hashed_safe_activity_event(self) -> None:
        client, _, _ = self.build_client(subject="usr-1", email="person@madup.com")

        response = client.get(
            "/dashboard",
            headers=signed_headers(
                method="GET",
                path="/dashboard",
                request_id="req-dashboard-view-1",
                token="opaque-browser-token",
            ),
        )

        self.assertEqual(response.status_code, 200)
        dashboard_events = [
            event
            for event in self.store.list_activity_events(user_id=UserId("usr-1"))
            if event.surface is ActivitySurface.DASHBOARD
            and event.action == "view_dashboard"
        ]
        self.assertEqual(len(dashboard_events), 2)
        latest = dashboard_events[-1]
        self.assertEqual(latest.outcome, ActivityOutcome.SUCCEEDED)
        self.assertEqual(latest.target_ref, "dashboard/usr-1")
        self.assertNotIn("req-dashboard-view-1", str(latest))
        self.assertNotIn("opaque-browser-token", str(latest))

    def test_dashboard_view_ignores_activity_append_failures(self) -> None:
        class FailingDashboardStore(MemoryStore):
            def append_activity_event(self, event: ActivityEvent) -> ActivityEvent:
                if event.surface is ActivitySurface.DASHBOARD:
                    raise RuntimeError("unsafe request details")
                return super().append_activity_event(event)

        failing_store = FailingDashboardStore()
        for attr in (
            "_users",
            "_workloads",
            "_schedules",
            "_secrets",
            "_operations",
            "_usage_entries",
            "_activity_events",
            "_maintenance_job_statuses",
            "_origin_claims",
        ):
            setattr(failing_store, attr, deepcopy(getattr(self.store, attr)))

        authorizer = FakeActionPolicyAuthorizer()
        gateway, _ = build_gateway(
            store=failing_store,
            claims=claims(subject="usr-1", email="person@madup.com"),
            authorizer=authorizer,
        )
        client = TestClient(
            build_api_app(
                store=failing_store,
                gateway=gateway,
                clock=lambda: NOW,
            )
        )

        response = client.get(
            "/dashboard",
            headers=signed_headers(
                method="GET",
                path="/dashboard",
                request_id="req-dashboard-write-fail",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("unsafe request details", response.text)

    def test_read_only_plan_surfaces_return_explicit_unavailable_results(self) -> None:
        client, _, _ = self.build_client(subject="usr-1", email="person@madup.com")
        deploy_response = client.get(
            "/v1/plan/deploy",
            headers=signed_headers(
                method="GET",
                path="/v1/plan/deploy",
                request_id="req-plan-deploy",
            ),
        )
        schedule_path = "/v1/plan/schedule?workload_id=wrk-script"
        schedule_response = client.get(
            schedule_path,
            headers=signed_headers(
                method="GET",
                path=schedule_path,
                request_id="req-plan-schedule",
            ),
        )
        self.assertEqual(deploy_response.status_code, 200)
        self.assertEqual(schedule_response.status_code, 503)
        self.assertEqual(deploy_response.json()["status"], "planning_unavailable")
        self.assertEqual(
            schedule_response.json()["detail"],
            "Schedule dependencies are not configured.",
        )

    def test_deploy_plan_authorizes_against_requested_workload_before_planning(
        self,
    ) -> None:
        authorizer = FakeActionPolicyAuthorizer()
        authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.DEPLOY_WORKLOAD.value,
            resource_id="workload:wrk-2",
            reason_code="principal_scope_mismatch",
            audit_message="resource is outside the authenticated scope",
        )
        planner = UnexpectedDeploymentService()
        client, _ = self.build_mutation_client(
            subject="usr-1",
            email="person@madup.com",
            deployment_service=planner,
            authorizer=authorizer,
        )
        path = "/v1/plan/deploy?workload_id=wrk-2"

        response = client.get(
            path,
            headers=signed_headers(
                method="GET",
                path=path,
                request_id="req-plan-deploy-cross-user",
            ),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(authorizer.calls[0].intent.action, ActionName.DEPLOY_WORKLOAD)
        self.assertEqual(authorizer.calls[0].intent.resource_id, "workload:wrk-2")
        self.assertFalse(planner.plan_called)

    def test_deploy_mutation_authorizes_against_plan_before_service_execution(
        self,
    ) -> None:
        authorizer = FakeActionPolicyAuthorizer()
        authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.DEPLOY_WORKLOAD.value,
            resource_id="deployment-plan:plan-2",
            reason_code="principal_scope_mismatch",
            audit_message="resource is outside the authenticated scope",
        )
        executor = UnexpectedDeploymentService()
        client, _ = self.build_mutation_client(
            subject="usr-1",
            email="person@madup.com",
            deployment_service=executor,
            authorizer=authorizer,
        )
        payload = {
            "correlation_id": "corr-cross-user",
            "idempotency_key": "idem-cross-user",
            "plan_hash": "a" * 64,
            "plan_id": "plan-2",
        }
        body = json.dumps(
            payload,
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
                    request_id="req-deploy-cross-user",
                )
            )
            | {"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(authorizer.calls[0].intent.action, ActionName.DEPLOY_WORKLOAD)
        self.assertEqual(
            authorizer.calls[0].intent.resource_id,
            "deployment-plan:plan-2",
        )
        self.assertFalse(executor.deploy_called)

    def test_deploy_mutation_parses_plan_id_before_authorization_and_reuses_body(
        self,
    ) -> None:
        deployment_service = RecordingDeploymentService()
        client, _ = self.build_mutation_client(
            subject="usr-1",
            email="person@madup.com",
            deployment_service=deployment_service,
        )
        payload = {
            "correlation_id": "corr-1",
            "idempotency_key": "idem-1",
            "plan_hash": "b" * 64,
            "plan_id": "plan-1",
        }
        body = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
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
                    action=ActionName.DEPLOY_WORKLOAD,
                    resource_id="deployment-plan:plan-1",
                ),
                surface=IdentitySurface.BROWSER,
            )

        with patch(
            "mim_control_plane.api.authorize_api_request",
            new=fake_authorize_api_request,
        ):
            response = client.post(
                "/v1/deployments",
                content=body,
                headers=dict(
                    signed_headers(
                        method="POST",
                        path="/v1/deployments",
                        body=body,
                        request_id="req-deploy-body",
                    )
                )
                | {"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["authenticated_body"], body)
        self.assertEqual(captured["resource_id"], "deployment-plan:plan-1")
        self.assertTrue(deployment_service.deploy_called)
        assert deployment_service.deploy_kwargs is not None
        self.assertEqual(deployment_service.deploy_kwargs["principal"], principal)
        self.assertEqual(
            {
                key: deployment_service.deploy_kwargs[key]
                for key in (
                    "correlation_id",
                    "idempotency_key",
                    "plan_hash",
                    "plan_id",
                )
            },
            payload,
        )

    def test_failure_response_is_redacted(self) -> None:
        client, _, _ = self.build_client(
            subject="adm-1",
            email="admin@madup.com",
        )
        response = client.get(
            "/v1/failures/op-other",
            headers=signed_headers(
                method="GET",
                path="/v1/failures/op-other",
                request_id="req-failure-admin",
            ),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rendered = str(payload)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("session=abc", rendered)
        self.assertNotIn("127.0.0.1", rendered)
        self.assertNotIn("curl/8.0", rendered)

    def test_static_assets_require_the_same_browser_auth(self) -> None:
        client, _, _ = self.build_client(subject="usr-1", email="person@madup.com")
        denied = client.get("/static/dashboard.js")
        allowed = client.get(
            "/static/dashboard.js",
            headers=signed_headers(
                method="GET",
                path="/static/dashboard.js",
                request_id="req-static-js",
            ),
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)

    def test_mutation_routes_are_absent(self) -> None:
        client, _, _ = self.build_client(subject="usr-1", email="person@madup.com")
        headers = dict(
            signed_headers(
                method="POST",
                path="/v1/workloads",
                body=b"{}",
                request_id="req-post",
            )
        )
        response = client.post("/v1/workloads", headers=headers, json={})
        self.assertIn(response.status_code, {404, 405})
