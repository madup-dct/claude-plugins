from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient

from mim_control_plane.adapters.fake_identity import FakeActionPolicyAuthorizer
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.api import build_api_app
from mim_control_plane.config import TARGET_MONTHLY_BUDGET_KRW
from mim_control_plane.domain.models import (
    AppHostnameBindingState,
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
from mim_control_plane.services.app_gateway_authorization import (
    AppGatewayAuthorizationDenied,
    AppGatewayAuthorizationService,
)
from mim_control_plane.services.app_hostname import (
    AppHostnameBindingService,
    workload_hash_suffix,
)
from mim_control_plane.services.central_identity import CentralIdentityGateway

NOW = datetime(2026, 8, 5, 6, 0, 0, tzinfo=UTC)
ISSUER = "https://tenant.cloudflareaccess.com"
AUDIENCE = "audience-1"
GROUP = "mim-users"
ORIGIN_KEY = b"d" * 32


class StaticJwtVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self._claims = claims

    def verify(self, token: str) -> IdentityClaims:
        del token
        return self._claims


def build_gateway(
    store: MemoryStore,
    *,
    subject: str = "usr-1",
    email: str = "person@madup.com",
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


def signed_headers(*, path: str) -> dict[str, str]:
    request_id = f"req-{path.strip('/').replace('/', '-') or 'root'}"
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
        "X-MIM-Origin-Request-Id": str(unsigned.request_id),
        "X-MIM-Origin-Public-Host": "mim.madup.app",
        "X-MIM-Origin-Destination-Class": "control-plane",
        "X-MIM-Origin-Signature": sign_origin_request(unsigned, key=ORIGIN_KEY),
        "Cf-Access-Jwt-Assertion": "opaque",
    }


class DashboardAppSurfaceContractTests(unittest.TestCase):
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
        self.workload = self.store.create_workload(
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
        self.binding = AppHostnameBindingService(
            store=self.store
        ).create_active_binding(
            workload=self.workload,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW - timedelta(minutes=10),
        )
        self.policy = IdentityPolicy(
            store=self.store,
            issuer=ISSUER,
            audience=AUDIENCE,
            company_domain="madup.com",
            required_group=GROUP,
            max_staleness=timedelta(minutes=60),
            clock=lambda: NOW,
        )
        self.authorizer = AppGatewayAuthorizationService(
            store=self.store,
            identity_policy=self.policy,
            clock=lambda: NOW,
        )

    def build_client(self) -> TestClient:
        return TestClient(
            build_api_app(
                store=self.store,
                gateway=build_gateway(self.store),
                clock=lambda: NOW,
            )
        )

    def request(self, **overrides: object) -> object:
        payload: dict[str, object] = {
            "schema": "mim.app-authorization.v1",
            "public_host": self.binding.public_host,
            "method": "GET",
            "request_target": "/",
            "access_subject": "usr-1",
            "access_email": "person@madup.com",
            "edge_request_id": "req-1",
            "edge_timestamp": int(NOW.timestamp()),
            "edge_body_sha256": "a" * 64,
        }
        payload.update(overrides)
        return SimpleNamespace(**payload)

    def test_gateway_denials_stay_generic_for_invalid_or_unavailable_hosts(
        self,
    ) -> None:
        generic = r"^App request was denied\.$"
        invalid_cases = (
            self.request(
                edge_request_id="req-missing",
                public_host="missing-51f8fa1fcb2d.madup.app",
            ),
            self.request(edge_request_id="req-reserved", public_host="mim.madup.app"),
            self.request(edge_request_id="req-apex", public_host="madup.app"),
            self.request(
                edge_request_id="req-nested",
                public_host="nested.alpha.madup.app",
            ),
            self.request(
                edge_request_id="req-cross-owner",
                access_subject="usr-2",
                access_email="other@madup.com",
            ),
        )
        for item in invalid_cases:
            with self.subTest(public_host=getattr(item, "public_host", "invalid")):
                with self.assertRaisesRegex(AppGatewayAuthorizationDenied, generic):
                    self.authorizer.authorize(item)  # type: ignore[arg-type]

        inactive_workload = self.store.get_workload(self.workload.id)
        self.store.save_workload(
            replace(
                inactive_workload,
                state=WorkloadState.PAUSED,
                updated_at=NOW - timedelta(minutes=5),
                version=inactive_workload.version + 1,
            ),
            expected_version=inactive_workload.version,
        )
        with self.assertRaisesRegex(AppGatewayAuthorizationDenied, generic):
            self.authorizer.authorize(
                self.request(
                    edge_request_id="req-inactive-workload",
                    public_host=self.binding.public_host,
                )
            )

        active_workload = self.store.get_workload(self.workload.id)
        self.store.save_workload(
            replace(
                active_workload,
                state=WorkloadState.ACTIVE,
                updated_at=NOW - timedelta(minutes=4),
                version=active_workload.version + 1,
            ),
            expected_version=active_workload.version,
        )
        self.store.append_usage_entry(
            UsageEntry(
                id=UsageEntryId("use-pause"),
                owner_id=UserId("usr-1"),
                workload_id=WorkloadId("wrk-1"),
                service_category="cloud_run",
                estimated_cost_krw=TARGET_MONTHLY_BUDGET_KRW,
                finalized_cost_krw=None,
                confidence=UsageConfidence.ESTIMATED,
                collected_at=NOW,
            )
        )
        with self.assertRaisesRegex(AppGatewayAuthorizationDenied, generic):
            self.authorizer.authorize(self.request(edge_request_id="req-cost-pause"))

        disabled = self.store.save_app_hostname_binding(
            self.binding.transition_state(
                AppHostnameBindingState.DISABLED,
                at=NOW - timedelta(minutes=3),
            ),
            expected_version=self.binding.version,
        )
        with self.assertRaisesRegex(AppGatewayAuthorizationDenied, generic):
            self.authorizer.authorize(
                self.request(
                    edge_request_id="req-disabled",
                    public_host=disabled.public_host,
                )
            )

        retired = self.store.save_app_hostname_binding(
            disabled.transition_state(
                AppHostnameBindingState.RETIRED,
                at=NOW - timedelta(minutes=2),
            ),
            expected_version=disabled.version,
        )
        with self.assertRaisesRegex(AppGatewayAuthorizationDenied, generic):
            self.authorizer.authorize(
                self.request(
                    edge_request_id="req-retired",
                    public_host=retired.public_host,
                )
            )

    def test_dashboard_and_workload_json_never_leak_private_routing_material(
        self,
    ) -> None:
        client = self.build_client()

        dashboard = client.get("/dashboard", headers=signed_headers(path="/dashboard"))
        workloads = client.get(
            "/v1/workloads",
            headers=signed_headers(path="/v1/workloads"),
        )

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(workloads.status_code, 200)
        for rendered in (dashboard.text, str(workloads.json())):
            self.assertNotIn(self.binding.upstream_url, rendered)
            self.assertNotIn(self.binding.service_resource, rendered)
            self.assertNotIn(self.binding.upstream_audience, rendered)
            self.assertNotIn("mim-prod-123456", rendered)
            self.assertNotIn("audience-1", rendered)
            self.assertNotIn("serviceAccount:", rendered)


if __name__ == "__main__":
    unittest.main()
