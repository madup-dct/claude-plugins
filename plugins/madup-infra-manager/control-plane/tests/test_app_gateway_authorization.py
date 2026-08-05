from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.config import TARGET_MONTHLY_BUDGET_KRW
from mim_control_plane.domain.models import (
    AppHostnameBinding,
    AppHostnameBindingState,
    OrgCostGuard,
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
from mim_control_plane.ports.store import VersionConflict
from mim_control_plane.security.authorization import IdentityPolicy
from mim_control_plane.services.app_gateway_authorization import (
    AppAuthorizationRequest,
    AppGatewayAuthorizationDenied,
    AppGatewayAuthorizationService,
)
from mim_control_plane.services.app_hostname import (
    AppHostnameBindingService,
    workload_hash_suffix,
)

NOW = datetime(2026, 8, 5, 4, 0, 0, tzinfo=UTC)
GROUP = "mim-users"


def seed_org_cost_guard(
    store: MemoryStore,
    *,
    evaluated_at: datetime = NOW,
    emergency_stop: bool = False,
) -> None:
    store.create_org_cost_guard(
        OrgCostGuard(
            evaluated_at=evaluated_at,
            latest_usage_collected_at=evaluated_at,
            emergency_stop=emergency_stop,
            org_policy_cost_krw=0 if not emergency_stop else 11_000,
        )
    )


def user(
    *,
    user_id: str = "usr-1",
    email: str = "person@madup.com",
    role: UserRole = UserRole.USER,
    state: UserState = UserState.ACTIVE,
    groups: frozenset[str] = frozenset({GROUP}),
    synced_at: datetime = NOW - timedelta(minutes=5),
) -> User:
    return User(
        id=UserId(user_id),
        email=email,
        role=role,
        state=state,
        groups=groups,
        identity_synced_at=synced_at,
        created_at=NOW - timedelta(days=2),
        updated_at=synced_at,
    )


def workload(
    *,
    workload_id: str = "wrk-1",
    owner_id: str = "usr-1",
    kind: WorkloadKind = WorkloadKind.NEXTJS,
    state: WorkloadState = WorkloadState.ACTIVE,
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id=RepositoryAdmissionId("adm-1"),
        name="North Star",
        kind=kind,
        state=state,
        source_sha="a" * 40,
        desired_manifest_hash="manifest-1",
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(hours=1),
        last_activity_at=NOW - timedelta(minutes=30),
    )


def request(
    *,
    public_host: str,
    request_target: str = "/",
    access_subject: str = "usr-1",
    access_email: str = "person@madup.com",
    edge_request_id: str = "req-1",
    edge_timestamp: int | None = None,
    edge_body_sha256: str = "a" * 64,
) -> AppAuthorizationRequest:
    return AppAuthorizationRequest(
        schema="mim.app-authorization.v1",
        public_host=public_host,
        method="GET",
        request_target=request_target,
        access_subject=access_subject,
        access_email=access_email,
        edge_request_id=edge_request_id,
        edge_timestamp=(
            int(NOW.timestamp()) if edge_timestamp is None else edge_timestamp
        ),
        edge_body_sha256=edge_body_sha256,
    )


class RecordingUsageScopeStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.usage_owner_ids: list[UserId | None] = []

    def list_usage_entries(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[UsageEntry, ...]:
        self.usage_owner_ids.append(owner_id)
        return super().list_usage_entries(owner_id=owner_id)


class RecordingHeartbeatStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.saved_activity_at: list[datetime | None] = []
        self.fail_save = False
        self.conflict_refresh: Workload | None = None

    def save_workload(  # type: ignore[override]
        self,
        workload: Workload,
        *,
        expected_version: int,
    ) -> Workload:
        self.saved_activity_at.append(workload.last_activity_at)
        if self.fail_save:
            raise RuntimeError("synthetic heartbeat failure")
        if self.conflict_refresh is not None:
            refresh = self.conflict_refresh
            self.conflict_refresh = None
            current = self.get_workload(refresh.id)
            MemoryStore.save_workload(
                self,
                refresh,
                expected_version=current.version,
            )
            raise VersionConflict("synthetic heartbeat conflict")
        return super().save_workload(workload, expected_version=expected_version)


class AppGatewayAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(user())
        self.store.create_user(
            user(
                user_id="adm-1",
                email="admin@madup.com",
                role=UserRole.ADMIN,
            )
        )
        seed_org_cost_guard(self.store)
        self.workload = self.store.create_workload(workload())
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
            now=NOW,
        )
        self.policy = IdentityPolicy(
            store=self.store,
            issuer="https://tenant.cloudflareaccess.com",
            audience="audience-1",
            company_domain="madup.com",
            required_group=GROUP,
            max_staleness=timedelta(minutes=60),
            clock=lambda: NOW,
        )
        self.service = AppGatewayAuthorizationService(
            store=self.store,
            identity_policy=self.policy,
            clock=lambda: NOW,
        )

    def test_authorize_claims_replay_and_returns_exact_short_lived_routing(
        self,
    ) -> None:
        decision = self.service.authorize(request(public_host=self.binding.public_host))

        self.assertEqual(decision.schema, "mim.app-authorization.v1")
        self.assertEqual(decision.public_host, self.binding.public_host)
        self.assertEqual(decision.workload_id, self.workload.id)
        self.assertEqual(decision.upstream_url, self.binding.upstream_url)
        self.assertEqual(decision.upstream_audience, self.binding.upstream_audience)
        self.assertEqual(decision.expires_at, NOW + timedelta(seconds=30))

        with self.assertRaises(AppGatewayAuthorizationDenied):
            self.service.authorize(request(public_host=self.binding.public_host))

    def test_authorize_records_workload_activity_only_when_stale(self) -> None:
        store = RecordingHeartbeatStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        stale = store.create_workload(
            replace(
                workload(
                    state=WorkloadState.ACTIVE,
                ),
                updated_at=NOW - timedelta(hours=2),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=stale,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        service = AppGatewayAuthorizationService(
            store=store,
            identity_policy=IdentityPolicy(
                store=store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        service.authorize(
            request(
                public_host=binding.public_host,
                edge_request_id="req-heartbeat-stale",
            )
        )

        self.assertEqual(store.saved_activity_at, [NOW])
        self.assertEqual(store.get_workload(WorkloadId("wrk-1")).last_activity_at, NOW)

        fresh_store = RecordingHeartbeatStore()
        fresh_store.create_user(user())
        seed_org_cost_guard(fresh_store)
        fresh = fresh_store.create_workload(workload())
        fresh_binding = AppHostnameBindingService(
            store=fresh_store
        ).create_active_binding(
            workload=fresh,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        fresh_service = AppGatewayAuthorizationService(
            store=fresh_store,
            identity_policy=IdentityPolicy(
                store=fresh_store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        fresh_service.authorize(
            request(
                public_host=fresh_binding.public_host,
                edge_request_id="req-heartbeat-fresh",
            )
        )

        self.assertEqual(fresh_store.saved_activity_at, [])
        self.assertEqual(
            fresh_store.get_workload(WorkloadId("wrk-1")).last_activity_at,
            NOW - timedelta(minutes=30),
        )

    def test_authorize_heartbeat_runs_at_most_once_per_hour(self) -> None:
        store = RecordingHeartbeatStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        stale = store.create_workload(
            replace(
                workload(),
                updated_at=NOW - timedelta(hours=2),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=stale,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        current_time = [NOW]
        service = AppGatewayAuthorizationService(
            store=store,
            identity_policy=IdentityPolicy(
                store=store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(hours=3),
                clock=lambda: current_time[0],
            ),
            clock=lambda: current_time[0],
        )

        service.authorize(
            request(
                public_host=binding.public_host,
                edge_request_id="req-hourly-1",
                edge_timestamp=int(current_time[0].timestamp()),
            )
        )
        current_time[0] = NOW + timedelta(minutes=59)
        guard = store.get_org_cost_guard()
        store.save_org_cost_guard(
            replace(
                guard,
                evaluated_at=current_time[0],
                latest_usage_collected_at=current_time[0],
                version=guard.version + 1,
            ),
            expected_version=guard.version,
        )
        service.authorize(
            request(
                public_host=binding.public_host,
                edge_request_id="req-hourly-2",
                edge_timestamp=int(current_time[0].timestamp()),
            )
        )
        current_time[0] = NOW + timedelta(hours=1, minutes=1)
        guard = store.get_org_cost_guard()
        store.save_org_cost_guard(
            replace(
                guard,
                evaluated_at=current_time[0],
                latest_usage_collected_at=current_time[0],
                version=guard.version + 1,
            ),
            expected_version=guard.version,
        )
        service.authorize(
            request(
                public_host=binding.public_host,
                edge_request_id="req-hourly-3",
                edge_timestamp=int(current_time[0].timestamp()),
            )
        )

        self.assertEqual(
            store.saved_activity_at,
            [NOW, NOW + timedelta(hours=1, minutes=1)],
        )

    def test_authorize_heartbeat_conflict_uses_recent_competing_refresh(self) -> None:
        store = RecordingHeartbeatStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        current = store.create_workload(
            replace(
                workload(),
                updated_at=NOW - timedelta(hours=2),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        store.conflict_refresh = replace(
            current,
            updated_at=NOW - timedelta(minutes=5),
            last_activity_at=NOW - timedelta(minutes=5),
            version=current.version + 1,
        )
        binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=current,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        service = AppGatewayAuthorizationService(
            store=store,
            identity_policy=IdentityPolicy(
                store=store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        decision = service.authorize(
            request(
                public_host=binding.public_host,
                edge_request_id="req-heartbeat-conflict",
            )
        )

        self.assertEqual(decision.workload_id, "wrk-1")
        self.assertEqual(len(store.saved_activity_at), 1)
        self.assertEqual(
            store.get_workload(WorkloadId("wrk-1")).last_activity_at,
            NOW - timedelta(minutes=5),
        )
        self.assertEqual(store.list_audit_events(), ())

    def test_authorize_heartbeat_conflict_does_not_retry_into_paused_state(
        self,
    ) -> None:
        store = RecordingHeartbeatStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        current = store.create_workload(
            replace(
                workload(),
                updated_at=NOW - timedelta(hours=2),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        store.conflict_refresh = replace(
            current,
            state=WorkloadState.PAUSED,
            updated_at=NOW,
            version=current.version + 1,
        )
        binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=current,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        service = AppGatewayAuthorizationService(
            store=store,
            identity_policy=IdentityPolicy(
                store=store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        decision = service.authorize(
            request(
                public_host=binding.public_host,
                edge_request_id="req-heartbeat-paused-race",
            )
        )

        self.assertEqual(decision.workload_id, "wrk-1")
        self.assertEqual(store.saved_activity_at, [NOW])
        self.assertEqual(
            store.get_workload(WorkloadId("wrk-1")).state,
            WorkloadState.PAUSED,
        )
        self.assertEqual(
            store.get_workload(WorkloadId("wrk-1")).last_activity_at,
            NOW - timedelta(hours=2),
        )
        self.assertEqual(store.list_audit_events(), ())

    def test_authorize_heartbeat_failure_emits_sanitized_signal_without_denial(
        self,
    ) -> None:
        store = RecordingHeartbeatStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        stale = store.create_workload(
            replace(
                workload(),
                updated_at=NOW - timedelta(hours=2),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        store.fail_save = True
        binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=stale,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        service = AppGatewayAuthorizationService(
            store=store,
            identity_policy=IdentityPolicy(
                store=store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        decision = service.authorize(
            request(
                public_host=binding.public_host,
                edge_request_id="req-heartbeat-failure",
            )
        )

        self.assertEqual(decision.workload_id, "wrk-1")
        self.assertEqual(store.saved_activity_at, [NOW])
        self.assertEqual(
            store.get_workload(WorkloadId("wrk-1")).last_activity_at,
            NOW - timedelta(hours=2),
        )
        events = store.list_audit_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "heartbeat_write_failed")
        self.assertEqual(events[0].target_ref, "app_gateway")
        self.assertEqual(events[0].policy_decision, "best_effort_suppressed")
        signal_text = "|".join(
            (
                str(events[0].id),
                events[0].action,
                events[0].target_ref,
                events[0].policy_decision,
                events[0].correlation_id,
            )
        )
        for forbidden in ("usr-1", "wrk-1", "person@madup.com", "token"):
            self.assertNotIn(forbidden, signal_text)

    def test_authorize_simultaneous_heartbeat_failures_persist_distinct_signals(
        self,
    ) -> None:
        store = RecordingHeartbeatStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        first = store.create_workload(
            replace(
                workload(),
                updated_at=NOW - timedelta(hours=2),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        second = store.create_workload(
            replace(
                workload(workload_id="wrk-2"),
                updated_at=NOW - timedelta(hours=2),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        first_binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=first,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        second_binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=second,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-2')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-2')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        store.fail_save = True
        service = AppGatewayAuthorizationService(
            store=store,
            identity_policy=IdentityPolicy(
                store=store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        first_result = service.authorize(
            request(
                public_host=first_binding.public_host,
                edge_request_id="req-fail-1",
            )
        )
        second_result = service.authorize(
            request(
                public_host=second_binding.public_host,
                edge_request_id="req-fail-2",
            )
        )

        self.assertEqual(first_result.workload_id, "wrk-1")
        self.assertEqual(second_result.workload_id, "wrk-2")
        events = store.list_audit_events()
        self.assertEqual(len(events), 2)
        self.assertEqual({event.action for event in events}, {"heartbeat_write_failed"})
        self.assertEqual({event.target_ref for event in events}, {"app_gateway"})
        self.assertEqual(
            {event.policy_decision for event in events},
            {"best_effort_suppressed"},
        )
        self.assertEqual(len({event.id for event in events}), 2)
        self.assertEqual(len({event.correlation_id for event in events}), 2)
        for event in events:
            signal_text = "|".join(
                (
                    str(event.id),
                    event.action,
                    event.target_ref,
                    event.policy_decision,
                    event.correlation_id,
                )
            )
            for forbidden in ("wrk-1", "wrk-2", "req-fail-1", "req-fail-2"):
                self.assertNotIn(forbidden, signal_text)

    def test_authorize_uses_only_owner_scoped_usage_entries(self) -> None:
        store = RecordingUsageScopeStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        target = store.create_workload(workload())
        binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=target,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        service = AppGatewayAuthorizationService(
            store=store,
            identity_policy=IdentityPolicy(
                store=store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        decision = service.authorize(
            request(
                public_host=binding.public_host,
                edge_request_id="req-owner-only",
            )
        )

        self.assertEqual(decision.workload_id, "wrk-1")
        self.assertEqual(store.usage_owner_ids, [UserId("usr-1")])

    def test_admin_cross_owner_authorize_records_single_stale_heartbeat(
        self,
    ) -> None:
        store = RecordingHeartbeatStore()
        store.create_user(user())
        store.create_user(
            user(
                user_id="adm-1",
                email="admin@madup.com",
                role=UserRole.ADMIN,
            )
        )
        seed_org_cost_guard(store)
        stale = store.create_workload(
            replace(
                workload(),
                updated_at=NOW - timedelta(hours=2),
                last_activity_at=NOW - timedelta(hours=2),
            )
        )
        binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=stale,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        service = AppGatewayAuthorizationService(
            store=store,
            identity_policy=IdentityPolicy(
                store=store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        decision = service.authorize(
            request(
                public_host=binding.public_host,
                access_subject="adm-1",
                access_email="admin@madup.com",
                edge_request_id="req-admin-cross-owner",
            )
        )

        self.assertEqual(decision.workload_id, "wrk-1")
        self.assertEqual(decision.public_host, binding.public_host)
        self.assertEqual(store.saved_activity_at, [NOW])
        self.assertEqual(store.get_workload(WorkloadId("wrk-1")).last_activity_at, NOW)
        self.assertEqual(store.list_audit_events(), ())

    def test_authorize_fails_closed_when_org_guard_is_missing_before_usage_scan(
        self,
    ) -> None:
        store = RecordingUsageScopeStore()
        store.create_user(user())
        target = store.create_workload(workload())
        binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=target,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        service = AppGatewayAuthorizationService(
            store=store,
            identity_policy=IdentityPolicy(
                store=store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        with self.assertRaises(AppGatewayAuthorizationDenied):
            service.authorize(
                request(
                    public_host=binding.public_host,
                    edge_request_id="req-guard-missing",
                )
            )

        self.assertEqual(store.usage_owner_ids, [])

    def test_authorize_relies_on_disabled_binding_and_paused_workload_state_contract(
        self,
    ) -> None:
        disabled_store = RecordingUsageScopeStore()
        disabled_store.create_user(user())
        seed_org_cost_guard(disabled_store)
        current = disabled_store.create_workload(workload())
        binding = AppHostnameBindingService(
            store=disabled_store
        ).create_active_binding(
            workload=current,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        disabled_store.save_app_hostname_binding(
            binding.transition_state(
                AppHostnameBindingState.DISABLED,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=binding.version,
        )
        disabled_service = AppGatewayAuthorizationService(
            store=disabled_store,
            identity_policy=IdentityPolicy(
                store=disabled_store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )
        with self.assertRaises(AppGatewayAuthorizationDenied):
            disabled_service.authorize(
                request(
                    public_host=binding.public_host,
                    edge_request_id="req-disabled",
                )
            )
        self.assertEqual(disabled_store.usage_owner_ids, [])

        paused_store = RecordingUsageScopeStore()
        paused_store.create_user(user())
        seed_org_cost_guard(paused_store)
        active = paused_store.create_workload(workload())
        paused_binding = AppHostnameBindingService(
            store=paused_store
        ).create_active_binding(
            workload=active,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        paused_store.save_workload(
            active.transition_state(
                WorkloadState.PAUSED,
                at=NOW + timedelta(minutes=1),
            ),
            expected_version=active.version,
        )
        paused_service = AppGatewayAuthorizationService(
            store=paused_store,
            identity_policy=IdentityPolicy(
                store=paused_store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )
        with self.assertRaises(AppGatewayAuthorizationDenied):
            paused_service.authorize(
                request(
                    public_host=paused_binding.public_host,
                    edge_request_id="req-paused",
                )
            )
        self.assertEqual(paused_store.usage_owner_ids, [])

    def test_request_target_accepts_only_well_formed_percent_escapes(self) -> None:
        allowed = request(
            public_host=self.binding.public_host,
            request_target="/reports/%E2%9C%93?q=hello%20world",
            edge_request_id="req-percent-encoded",
        )
        self.assertEqual(
            allowed.request_target,
            "/reports/%E2%9C%93?q=hello%20world",
        )

        for malformed in ("/reports/%", "/reports/%2", "/reports/%GG"):
            with self.subTest(request_target=malformed):
                with self.assertRaisesRegex(ValueError, "request_target"):
                    request(
                        public_host=self.binding.public_host,
                        request_target=malformed,
                        edge_request_id="req-bad-percent",
                    )

    def test_authorize_denies_cross_owner_cost_paused_and_non_web_workloads(
        self,
    ) -> None:
        with self.assertRaises(AppGatewayAuthorizationDenied):
            self.service.authorize(
                request(
                    public_host=self.binding.public_host,
                    access_subject="usr-2",
                    access_email="other@madup.com",
                    edge_request_id="req-cross-owner",
                )
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
        with self.assertRaises(AppGatewayAuthorizationDenied):
            self.service.authorize(
                request(
                    public_host=self.binding.public_host,
                    edge_request_id="req-cost-pause",
                )
            )

        secondary_store = MemoryStore()
        secondary_store.create_user(user())
        seed_org_cost_guard(secondary_store)
        non_web = secondary_store.create_workload(
            workload(
                workload_id="wrk-job",
                kind=WorkloadKind.SCHEDULED_SCRIPT,
            )
        )
        non_web_binding = secondary_store.create_app_hostname_binding(
            AppHostnameBinding(
                public_host="job-runner-51f8fa1fcb2d.madup.app",
                workload_id=non_web.id,
                owner_id=non_web.owner_id,
                workload_kind=non_web.kind,
                service_resource=(
                    "projects/mim-prod-123456/locations/asia-northeast3/"
                    f"services/mim-svc-{workload_hash_suffix('wrk-job')}"
                ),
                upstream_url=(
                    f"https://mim-svc-{workload_hash_suffix('wrk-job')}"
                    "-abcdefg-an.a.run.app"
                ),
                upstream_audience=(
                    f"https://mim-svc-{workload_hash_suffix('wrk-job')}"
                    "-abcdefg-an.a.run.app"
                ),
                state=AppHostnameBindingState.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        denied_service = AppGatewayAuthorizationService(
            store=secondary_store,
            identity_policy=IdentityPolicy(
                store=secondary_store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )
        with self.assertRaises(AppGatewayAuthorizationDenied):
            denied_service.authorize(
                request(
                    public_host=non_web_binding.public_host,
                    edge_request_id="req-non-web",
                )
            )

    def test_denied_authorize_never_touches_workload_activity(self) -> None:
        store = RecordingHeartbeatStore()
        store.create_user(user())
        seed_org_cost_guard(store)
        current = store.create_workload(workload())
        binding = AppHostnameBindingService(store=store).create_active_binding(
            workload=current,
            service_resource=(
                "projects/mim-prod-123456/locations/asia-northeast3/"
                f"services/mim-svc-{workload_hash_suffix('wrk-1')}"
            ),
            service_uri=(
                f"https://mim-svc-{workload_hash_suffix('wrk-1')}"
                "-abcdefg-an.a.run.app"
            ),
            now=NOW,
        )
        store.save_workload(
            replace(
                current,
                state=WorkloadState.PAUSED,
                updated_at=NOW + timedelta(minutes=1),
                version=current.version + 1,
            ),
            expected_version=current.version,
        )
        store.saved_activity_at.clear()
        service = AppGatewayAuthorizationService(
            store=store,
            identity_policy=IdentityPolicy(
                store=store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        with self.assertRaises(AppGatewayAuthorizationDenied):
            service.authorize(
                request(
                    public_host=binding.public_host,
                    edge_request_id="req-denied-no-heartbeat",
                )
            )

        self.assertEqual(store.saved_activity_at, [])


if __name__ == "__main__":
    unittest.main()
