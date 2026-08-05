from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient

from mim_control_plane.adapters.fake_identity import FakeActionPolicyAuthorizer
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.api import build_api_app
from mim_control_plane.config import PER_USER_SERVICE_LIMIT, PILOT_MAX_IDENTITIES
from mim_control_plane.domain.models import (
    AppHostnameBinding,
    AppHostnameBindingState,
    Operation,
    OperationId,
    OriginRequestId,
    RepositoryAdmissionId,
    UsageEntry,
    UsageEntryId,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    OperationState,
    UsageConfidence,
    UserRole,
    UserState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.security.authorization import IdentityPolicy
from mim_control_plane.security.identity import IdentityAuthenticator, IdentityClaims
from mim_control_plane.security.origin import (
    OriginHmacVerifier,
    OriginRequest,
    sign_origin_request,
)
from mim_control_plane.services.app_hostname import (
    AppHostnameBindingService,
    build_app_hostname,
    workload_hash_suffix,
)
from mim_control_plane.services.central_identity import CentralIdentityGateway

NOW = datetime(2026, 8, 4, 3, 0, 0, tzinfo=UTC)
ISSUER = "https://tenant.cloudflareaccess.com"
AUDIENCE = "audience-1"
GROUP = "mim-users"
ORIGIN_KEY = b"d" * 32


class CountingMemoryStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.latest_operation_reads = 0
        self.hostname_binding_reads = 0

    def get_latest_workload_operation(
        self,
        *,
        owner_id: UserId,
        workload_id: WorkloadId,
    ) -> Operation | None:
        self.latest_operation_reads += 1
        return super().get_latest_workload_operation(
            owner_id=owner_id,
            workload_id=workload_id,
        )

    def get_app_hostname_binding(self, public_host: str) -> AppHostnameBinding:
        self.hostname_binding_reads += 1
        return super().get_app_hostname_binding(public_host)

    def reset_dashboard_read_counts(self) -> None:
        self.latest_operation_reads = 0
        self.hostname_binding_reads = 0


class StaticJwtVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self._claims = claims

    def verify(self, token: str) -> IdentityClaims:
        del token
        return self._claims


def build_gateway(
    store: MemoryStore,
    *,
    subject: str,
    email: str,
) -> CentralIdentityGateway:
    policy = IdentityPolicy(
        store=store,
        issuer=ISSUER,
        audience=AUDIENCE,
        company_domain="madup.com",
        required_group=GROUP,
        max_staleness=timedelta(minutes=60),
        clock=lambda: NOW,
    )
    return CentralIdentityGateway(
        browser_authenticator=IdentityAuthenticator(
            origin_verifier=OriginHmacVerifier(
                keys={"edge-key": ORIGIN_KEY},
                store=store,
                clock=lambda: NOW,
                window=timedelta(seconds=60),
            ),
            jwt_verifier=StaticJwtVerifier(
                IdentityClaims(
                    subject=subject,
                    email=email,
                    issuer=ISSUER,
                    audience=(AUDIENCE,),
                    issued_at=NOW - timedelta(minutes=1),
                    expires_at=NOW + timedelta(minutes=10),
                )
            ),
            identity_policy=policy,
        ),
        identity_policy=policy,
        shared_install_directory=cast(Any, object()),
        identity_link_directory=cast(Any, object()),
        action_authorizer=FakeActionPolicyAuthorizer(),
        required_slack_scopes=frozenset({"commands"}),
        clock=lambda: NOW,
    )


def signed_headers(*, path: str, request_id: str) -> dict[str, str]:
    unsigned = OriginRequest(
        method="GET",
        path=path,
        body=b"",
        timestamp=NOW,
        request_id=OriginRequestId(request_id),
        public_host="mim.madup.app",
        destination_class="control-plane",
        key_id="edge-key",
        signature=None,
    )
    return {
        "X-MIM-Origin-Key-Id": "edge-key",
        "X-MIM-Origin-Timestamp": str(int(NOW.timestamp())),
        "X-MIM-Origin-Request-Id": request_id,
        "X-MIM-Origin-Public-Host": "mim.madup.app",
        "X-MIM-Origin-Destination-Class": "control-plane",
        "X-MIM-Origin-Signature": sign_origin_request(unsigned, key=ORIGIN_KEY),
        "Cf-Access-Jwt-Assertion": "opaque",
    }


class DashboardViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = CountingMemoryStore()
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
        self.store.create_user(
            User(
                id=UserId("adm-1"),
                email="admin@madup.com",
                role=UserRole.ADMIN,
                state=UserState.ACTIVE,
                groups=frozenset({GROUP}),
                identity_synced_at=NOW - timedelta(minutes=5),
                created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(minutes=5),
            )
        )
        self.store.create_user(
            User(
                id=UserId("usr-zero"),
                email="zero@madup.com",
                role=UserRole.USER,
                state=UserState.SUSPENDED,
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
                kind=WorkloadKind.NEXTJS,
                state=WorkloadState.ACTIVE,
                source_sha="a" * 40,
                desired_manifest_hash="manifest-1",
                created_at=NOW - timedelta(days=2),
                updated_at=NOW - timedelta(hours=2),
            )
        )
        self.store.append_usage_entry(
            UsageEntry(
                id=UsageEntryId("use-1"),
                owner_id=UserId("usr-1"),
                workload_id=WorkloadId("wrk-1"),
                service_category="cloud_run",
                estimated_cost_krw=400,
                finalized_cost_krw=400,
                confidence=UsageConfidence.FINALIZED,
                collected_at=NOW - timedelta(hours=2),
            )
        )
        self.store.record_maintenance_job_started(
            job_name="identity-sync",
            run_id="run-identity",
            started_at=NOW - timedelta(minutes=20),
        )
        lifecycle_started = self.store.record_maintenance_job_started(
            job_name="lifecycle",
            run_id="run-lifecycle",
            started_at=NOW - timedelta(minutes=5),
        )
        self.store.record_maintenance_job_terminal(
            job_name="lifecycle",
            run_id="run-lifecycle",
            expected_version=lifecycle_started.version,
            finished_at=NOW - timedelta(minutes=4),
            outcome="completed",
            summary=(("processed_users", 1),),
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
            finished_at=NOW - timedelta(hours=3) + timedelta(minutes=5),
            outcome="failed",
            summary=(("billing_appended_entries", 0),),
            failure_code="runtime_error",
            failure_class="internal",
        )
        self.store.append_usage_entry(
            UsageEntry(
                id=UsageEntryId("use-shared"),
                owner_id=None,
                workload_id=None,
                service_category="load_balancer",
                estimated_cost_krw=300,
                finalized_cost_krw=300,
                confidence=UsageConfidence.FINALIZED,
                collected_at=NOW - timedelta(hours=2),
            )
        )

    def build_client(self, *, subject: str, email: str) -> TestClient:
        return TestClient(
            build_api_app(
                store=self.store,
                gateway=build_gateway(self.store, subject=subject, email=email),
                clock=lambda: NOW,
            )
        )

    def create_binding(
        self,
        *,
        workload_id: str = "wrk-1",
        state: AppHostnameBindingState = AppHostnameBindingState.ACTIVE,
    ) -> AppHostnameBinding:
        workload = self.store.get_workload(WorkloadId(workload_id))
        binding = AppHostnameBindingService(store=self.store).create_active_binding(
            workload=workload,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix(workload_id)}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix(workload_id)}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW - timedelta(minutes=10),
        )
        if state is AppHostnameBindingState.ACTIVE:
            return binding
        return self.store.save_app_hostname_binding(
            binding.transition_state(state, at=NOW - timedelta(minutes=5)),
            expected_version=binding.version,
        )

    def create_operation(
        self,
        *,
        operation_id: str,
        actor_id: str,
        workload_id: str,
        state: OperationState,
        updated_at: datetime,
        sanitized_failure: str | None = None,
    ) -> Operation:
        return self.store.create_operation_once(
            Operation(
                id=OperationId(operation_id),
                actor_id=UserId(actor_id),
                workload_id=WorkloadId(workload_id),
                action="deploy",
                idempotency_key=f"idem-{operation_id}",
                request_hash=f"request-{operation_id}",
                state=state,
                created_at=updated_at,
                updated_at=updated_at,
                sanitized_failure=sanitized_failure,
            )
        )

    def test_dashboard_user_and_admin_views_differ(self) -> None:
        self.create_binding()
        user_client = self.build_client(subject="usr-1", email="person@madup.com")
        admin_client = self.build_client(subject="adm-1", email="admin@madup.com")

        user_response = user_client.get(
            "/dashboard",
            headers=signed_headers(path="/dashboard", request_id="req-user-dashboard"),
        )
        admin_response = admin_client.get(
            "/dashboard",
            headers=signed_headers(path="/dashboard", request_id="req-admin-dashboard"),
        )
        self.assertEqual(user_response.status_code, 200)
        self.assertEqual(admin_response.status_code, 200)
        user_html = user_response.text
        admin_html = admin_response.text
        self.assertIn("Madup Infra Manager", user_html)
        self.assertIn("User Console", user_html)
        self.assertNotIn("Admin Overview", user_html)
        self.assertIn("Operational Status", user_html)
        self.assertNotIn("Secure infrastructure setup is in progress.", user_html)
        self.assertIn("Estimated Cost", user_html)
        self.assertIn("Finalized Cost", user_html)
        self.assertIn("Activity Metrics", user_html)
        self.assertNotIn("Maintenance Jobs", user_html)
        self.assertNotIn("zero@madup.com", user_html)
        self.assertIn("Admin Overview", admin_html)
        self.assertIn("Maintenance Jobs", admin_html)
        self.assertIn("identity-sync", admin_html)
        self.assertIn("usage-ingest", admin_html)
        self.assertIn("Stale", admin_html)
        self.assertIn("Platform Shared Cost", admin_html)
        self.assertIn("Dashboard Visits 30d", admin_html)
        self.assertIn("Authorization Denials 30d", admin_html)
        self.assertIn("External edge metrics are unavailable", admin_html)
        self.assertIn("zero@madup.com", admin_html)
        self.assertIn("SUSPENDED", admin_html)
        self.assertNotIn("RuntimeError", admin_html)
        self.assertNotIn("Access Seats", admin_html)
        self.assertNotIn("Worker Requests", admin_html)

    def test_workload_api_projection_exposes_only_sanitized_latest_status_fields(
        self,
    ) -> None:
        current = self.store.get_workload(WorkloadId("wrk-1"))
        self.store.save_workload(
            replace(
                current,
                last_activity_at=NOW - timedelta(minutes=11),
                last_healthy_image_digest="sha256:" + "a" * 64,
                updated_at=NOW - timedelta(minutes=11),
                version=current.version + 1,
            ),
            expected_version=current.version,
        )
        binding = self.create_binding()
        self.create_operation(
            operation_id="op-own-old",
            actor_id="usr-1",
            workload_id="wrk-1",
            state=OperationState.QUEUED,
            updated_at=NOW - timedelta(minutes=20),
        )
        self.create_operation(
            operation_id="op-own-latest",
            actor_id="system:github-auto-deploy",
            workload_id="wrk-1",
            state=OperationState.FAILED,
            updated_at=NOW - timedelta(minutes=9),
            sanitized_failure="deploy_unhealthy",
        )
        client = self.build_client(subject="usr-1", email="person@madup.com")

        response = client.get(
            "/v1/workloads",
            headers=signed_headers(path="/v1/workloads", request_id="req-workloads"),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scope"], "user")
        self.assertEqual(len(payload["workloads"]), 1)
        row = payload["workloads"][0]
        self.assertEqual(row["id"], "wrk-1")
        self.assertEqual(row["public_host"], binding.public_host)
        self.assertEqual(row["public_binding_state"], "active")
        self.assertEqual(
            row["last_activity_at"],
            (NOW - timedelta(minutes=11)).isoformat(),
        )
        self.assertEqual(row["latest_operation_state"], "failed")
        self.assertEqual(row["latest_operation_failure_code"], "deploy_unhealthy")
        self.assertEqual(row["last_healthy_state"], "healthy")
        self.assertEqual(row["last_healthy_digest_status"], "recorded")
        self.assertNotIn("last_healthy_image_digest", row)
        self.assertNotIn("latest_operation_id", row)
        self.assertNotIn("result_summary", row)
        self.assertNotIn("service_resource", row)
        self.assertNotIn("upstream_url", row)
        self.assertNotIn("upstream_audience", row)
        self.assertNotIn("edge-key", response.text)
        self.assertNotIn("sha256:" + "a" * 64, response.text)
        self.assertNotIn("abcdefg-an.a.run.app", response.text)

    def test_memory_latest_operation_uses_created_at_then_id_for_ties(self) -> None:
        for operation_id, created_at in (
            ("op-9", NOW - timedelta(minutes=2)),
            ("op-7", NOW - timedelta(minutes=1)),
            ("op-8", NOW - timedelta(minutes=1)),
        ):
            self.store.create_operation_once(
                Operation(
                    id=OperationId(operation_id),
                    actor_id=UserId("usr-1"),
                    workload_id=WorkloadId("wrk-1"),
                    action="deploy",
                    idempotency_key=f"idem-{operation_id}",
                    request_hash=f"request-{operation_id}",
                    state=OperationState.QUEUED,
                    created_at=created_at,
                    updated_at=NOW,
                )
            )

        latest = self.store.get_latest_workload_operation(
            owner_id=UserId("usr-1"),
            workload_id=WorkloadId("wrk-1"),
        )

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.id, OperationId("op-8"))

    def test_workload_api_admin_projection_keeps_latest_operation_scoped_per_workload(
        self,
    ) -> None:
        self.store.create_user(
            User(
                id=UserId("usr-2"),
                email="other@madup.com",
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
                id=WorkloadId("wrk-2"),
                owner_id=UserId("usr-2"),
                repository_admission_id=RepositoryAdmissionId("adm-2"),
                name="Beta",
                kind=WorkloadKind.NEXTJS,
                state=WorkloadState.ACTIVE,
                source_sha="b" * 40,
                desired_manifest_hash="manifest-2",
                created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(hours=1),
                last_healthy_image_digest="sha256:" + "b" * 64,
            )
        )
        self.create_operation(
            operation_id="op-alpha",
            actor_id="adm-1",
            workload_id="wrk-1",
            state=OperationState.SUCCEEDED,
            updated_at=NOW - timedelta(minutes=8),
        )
        self.create_operation(
            operation_id="op-beta",
            actor_id="adm-1",
            workload_id="wrk-2",
            state=OperationState.QUARANTINED,
            updated_at=NOW - timedelta(minutes=7),
            sanitized_failure="deploy_denied",
        )
        client = self.build_client(subject="adm-1", email="admin@madup.com")

        response = client.get(
            "/v1/workloads",
            headers=signed_headers(
                path="/v1/workloads",
                request_id="req-admin-workloads",
            ),
        )

        self.assertEqual(response.status_code, 200)
        rows = {item["id"]: item for item in response.json()["workloads"]}
        self.assertEqual(rows["wrk-1"]["latest_operation_state"], "succeeded")
        self.assertIsNone(rows["wrk-1"]["latest_operation_failure_code"])
        self.assertEqual(rows["wrk-2"]["latest_operation_state"], "quarantined")
        self.assertEqual(
            rows["wrk-2"]["latest_operation_failure_code"],
            "deploy_denied",
        )
        self.assertEqual(rows["wrk-1"]["last_healthy_digest_status"], "missing")
        self.assertEqual(rows["wrk-2"]["last_healthy_digest_status"], "recorded")

    def test_workload_api_replaces_unapproved_failure_material(self) -> None:
        private_origin = "https://mim-svc-alpha-abcdefg-an.a.run" + ".app"
        private_resource = (
            "projects/prod/locations/asia-northeast3/services/mim-svc-alpha"
        )
        private_digest = "sha256:" + "d" * 64
        private_operation_id = "op-private-internal"
        self.create_operation(
            operation_id="op-public-projection",
            actor_id="usr-1",
            workload_id="wrk-1",
            state=OperationState.FAILED,
            updated_at=NOW - timedelta(minutes=1),
            sanitized_failure=(
                f"{private_operation_id} {private_origin} {private_resource} "
                f"audience={private_origin} {private_digest} key=edge-key"
            ),
        )
        client = self.build_client(subject="usr-1", email="person@madup.com")

        response = client.get(
            "/v1/workloads",
            headers=signed_headers(
                path="/v1/workloads",
                request_id="req-workload-failure-redaction",
            ),
        )

        self.assertEqual(response.status_code, 200)
        row = response.json()["workloads"][0]
        self.assertEqual(
            row["latest_operation_failure_code"],
            "operation_failed",
        )
        for private_value in (
            private_origin,
            private_resource,
            private_digest,
            private_operation_id,
            "edge-key",
        ):
            self.assertNotIn(private_value, response.text)

    def test_workload_api_bounds_operational_detail_reads_to_service_quota(
        self,
    ) -> None:
        for index in (2, 3):
            self.store.create_workload(
                Workload(
                    id=WorkloadId(f"wrk-{index}"),
                    owner_id=UserId("usr-1"),
                    repository_admission_id=RepositoryAdmissionId(f"adm-{index}"),
                    name=f"Workload {index}",
                    kind=WorkloadKind.NEXTJS,
                    state=WorkloadState.ACTIVE,
                    source_sha=str(index) * 40,
                    desired_manifest_hash=f"manifest-{index}",
                    created_at=NOW - timedelta(hours=4 - index),
                    updated_at=NOW - timedelta(hours=1),
                )
            )
        self.store.reset_dashboard_read_counts()
        client = self.build_client(subject="usr-1", email="person@madup.com")

        response = client.get(
            "/v1/workloads",
            headers=signed_headers(
                path="/v1/workloads",
                request_id="req-workload-detail-bound",
            ),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload.get("operational_detail_limit_per_owner"),
            PER_USER_SERVICE_LIMIT,
        )
        self.assertEqual(
            payload.get("operational_detail_limit_total"),
            PILOT_MAX_IDENTITIES * PER_USER_SERVICE_LIMIT,
        )
        self.assertTrue(payload.get("operational_details_truncated"))
        self.assertEqual(self.store.latest_operation_reads, PER_USER_SERVICE_LIMIT)
        self.assertEqual(self.store.hostname_binding_reads, PER_USER_SERVICE_LIMIT)
        availability = [
            row.get("operational_details_available") for row in payload["workloads"]
        ]
        self.assertEqual(availability, [True, True, False])

    def test_dashboard_shows_clickable_safe_host_only_for_exact_active_binding(
        self,
    ) -> None:
        binding = self.create_binding()
        client = self.build_client(subject="usr-1", email="person@madup.com")

        response = client.get(
            "/dashboard",
            headers=signed_headers(path="/dashboard", request_id="req-active-binding"),
        )

        self.assertEqual(response.status_code, 200)
        rendered = response.text
        expected_link = f"https://{binding.public_host}"
        self.assertIn(expected_link, rendered)
        self.assertIn(binding.public_host, rendered)
        self.assertNotIn(binding.upstream_url, rendered)
        self.assertNotIn(binding.service_resource, rendered)
        self.assertNotIn(binding.upstream_audience, rendered)
        self.assertNotIn("edge-key", rendered)

    def test_dashboard_displays_binding_state_without_active_link_for_disabled_binding(
        self,
    ) -> None:
        binding = self.create_binding(state=AppHostnameBindingState.DISABLED)
        client = self.build_client(subject="usr-1", email="person@madup.com")

        response = client.get(
            "/dashboard",
            headers=signed_headers(
                path="/dashboard",
                request_id="req-disabled-binding",
            ),
        )

        self.assertEqual(response.status_code, 200)
        rendered = response.text
        self.assertIn(binding.public_host, rendered)
        self.assertIn("disabled", rendered)
        self.assertNotIn(f"https://{binding.public_host}", rendered)
        self.assertNotIn(binding.upstream_url, rendered)

    def test_dashboard_hides_conflicting_binding_material(self) -> None:
        self.store.create_user(
            User(
                id=UserId("usr-2"),
                email="other@madup.com",
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
                id=WorkloadId("wrk-2"),
                owner_id=UserId("usr-2"),
                repository_admission_id=RepositoryAdmissionId("adm-2"),
                name="Beta",
                kind=WorkloadKind.NEXTJS,
                state=WorkloadState.ACTIVE,
                source_sha="b" * 40,
                desired_manifest_hash="manifest-2",
                created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(hours=1),
            )
        )
        self.store.create_app_hostname_binding(
            AppHostnameBinding(
                public_host=build_app_hostname("Alpha", "wrk-1"),
                workload_id=WorkloadId("wrk-2"),
                owner_id=UserId("usr-2"),
                workload_kind=WorkloadKind.NEXTJS,
                service_resource=(
                    "projects/mim-prod-123456/locations/asia-northeast3/"
                    f"services/mim-svc-{workload_hash_suffix('wrk-2')}"
                ),
                upstream_url=(
                    f"https://mim-svc-{workload_hash_suffix('wrk-2')}"
                    "-abcdefg-an.a.run.app"
                ),
                upstream_audience=(
                    f"https://mim-svc-{workload_hash_suffix('wrk-2')}"
                    "-abcdefg-an.a.run.app"
                ),
                state=AppHostnameBindingState.ACTIVE,
                created_at=NOW - timedelta(minutes=20),
                updated_at=NOW - timedelta(minutes=20),
            )
        )
        client = self.build_client(subject="usr-1", email="person@madup.com")

        response = client.get(
            "/dashboard",
            headers=signed_headers(
                path="/dashboard",
                request_id="req-conflict-binding",
            ),
        )

        self.assertEqual(response.status_code, 200)
        rendered = response.text
        self.assertNotIn(build_app_hostname("Alpha", "wrk-1"), rendered)
        self.assertNotIn("abcdefg-an.a.run.app", rendered)

    def test_dashboard_static_assets_are_same_origin_only(self) -> None:
        client = self.build_client(subject="usr-1", email="person@madup.com")
        js_response = client.get(
            "/static/dashboard.js",
            headers=signed_headers(path="/static/dashboard.js", request_id="req-js"),
        )
        css_response = client.get(
            "/static/dashboard.css",
            headers=signed_headers(path="/static/dashboard.css", request_id="req-css"),
        )
        self.assertEqual(js_response.status_code, 200)
        self.assertEqual(css_response.status_code, 200)
        self.assertIn("fetch('/v1/workloads'", js_response.text)
        self.assertIn("fetch('/v1/usage'", js_response.text)
        self.assertNotIn("http://", js_response.text)
        self.assertNotIn("https://", js_response.text)

    def test_memory_store_filtered_lists_are_deterministic(self) -> None:
        owned_workloads = self.store.list_workloads(owner_id=UserId("usr-1"))
        all_workloads = self.store.list_workloads()
        self.assertEqual([item.id for item in owned_workloads], [WorkloadId("wrk-1")])
        self.assertEqual([item.id for item in all_workloads], [WorkloadId("wrk-1")])

    def test_usage_view_excludes_previous_month_costs_from_current_policy(self) -> None:
        self.store.append_usage_entry(
            UsageEntry(
                id=UsageEntryId("use-prev-user"),
                owner_id=UserId("usr-1"),
                workload_id=WorkloadId("wrk-1"),
                service_category="cloud_run",
                estimated_cost_krw=900,
                finalized_cost_krw=900,
                confidence=UsageConfidence.FINALIZED,
                collected_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
            )
        )
        self.store.append_usage_entry(
            UsageEntry(
                id=UsageEntryId("use-prev-shared"),
                owner_id=None,
                workload_id=None,
                service_category="load_balancer",
                estimated_cost_krw=9_900,
                finalized_cost_krw=9_900,
                confidence=UsageConfidence.FINALIZED,
                collected_at=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            )
        )
        user_client = self.build_client(subject="usr-1", email="person@madup.com")
        admin_client = self.build_client(subject="adm-1", email="admin@madup.com")

        user_response = user_client.get(
            "/v1/usage",
            headers=signed_headers(path="/v1/usage", request_id="req-user-usage"),
        )
        admin_response = admin_client.get(
            "/v1/usage",
            headers=signed_headers(path="/v1/usage", request_id="req-admin-usage"),
        )

        self.assertEqual(user_response.status_code, 200)
        self.assertEqual(admin_response.status_code, 200)
        user_payload = user_response.json()
        admin_payload = admin_response.json()
        self.assertEqual(
            user_payload["costs"]["user_direct"]["policy_krw"],
            400,
        )
        self.assertEqual(
            user_payload["costs"]["user_direct"]["percent"],
            40,
        )
        self.assertFalse(user_payload["cost_policy"]["warn"])
        self.assertEqual(
            admin_payload["costs"]["organization"]["estimated_krw"],
            700,
        )
        self.assertEqual(
            admin_payload["costs"]["platform_shared"]["estimated_krw"],
            300,
        )
        admin_users = {
            item["user_id"]: item for item in admin_payload["users"]
        }
        self.assertEqual(admin_users["usr-1"]["policy_krw"], 400)
