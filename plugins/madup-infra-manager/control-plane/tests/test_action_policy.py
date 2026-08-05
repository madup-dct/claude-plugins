from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from mim_control_plane.adapters.action_policy import ClosedActionPolicyAuthorizer
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.central_identity import ActionIntent, ActionName
from mim_control_plane.domain.models import (
    DeploymentPlan,
    DeploymentPlanId,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    PlanState,
    ScheduleState,
    UserRole,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.store import StoreError
from mim_control_plane.security.identity import AuthenticatedPrincipal

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)


def principal(
    *,
    user_id: str = "usr-1",
    email: str = "person@madup.com",
    role: UserRole = UserRole.USER,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=UserId(user_id),
        email=email,
        role=role,
    )


def workload(
    *,
    workload_id: str = "wrk-1",
    owner_id: str = "usr-1",
) -> Workload:
    return Workload(
        id=WorkloadId(workload_id),
        owner_id=UserId(owner_id),
        repository_admission_id=RepositoryAdmissionId("adm-1"),
        name="sample-app",
        kind=WorkloadKind.NEXTJS,
        state=WorkloadState.ACTIVE,
        source_sha="a" * 40,
        desired_manifest_hash="b" * 64,
        created_at=NOW,
        updated_at=NOW,
    )


def schedule(
    *,
    schedule_id: str = "sch-1",
    workload_id: str = "wrk-1",
    owner_id: str = "usr-1",
) -> Schedule:
    return Schedule(
        id=ScheduleId(schedule_id),
        owner_id=UserId(owner_id),
        workload_id=WorkloadId(workload_id),
        cron="0 * * * *",
        timezone="Asia/Seoul",
        state=ScheduleState.ENABLED,
        created_at=NOW,
        updated_at=NOW,
    )


def deployment_plan(
    *,
    plan_id: str = "plan-1",
    workload_id: str = "wrk-1",
    actor_id: str = "usr-1",
) -> DeploymentPlan:
    return DeploymentPlan(
        id=DeploymentPlanId(plan_id),
        actor_id=UserId(actor_id),
        workload_id=WorkloadId(workload_id),
        action="deploy_workload",
        material_hash="c" * 64,
        policy_version="2026-08-04",
        state=PlanState.ISSUED,
        expires_at=NOW + timedelta(hours=1),
        created_at=NOW,
        updated_at=NOW,
    )


class StoreUnavailable(StoreError):
    pass


class UnavailableMemoryStore(MemoryStore):
    def get_workload(self, workload_id: WorkloadId) -> Workload:
        raise StoreUnavailable("store unavailable")

    def get_schedule(self, schedule_id: ScheduleId) -> Schedule:
        raise StoreUnavailable("store unavailable")

    def get_deployment_plan(self, plan_id: DeploymentPlanId) -> DeploymentPlan:
        raise StoreUnavailable("store unavailable")


class ClosedActionPolicyAuthorizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_workload(workload())
        self.store.create_workload(workload(workload_id="wrk-2", owner_id="usr-2"))
        self.store.create_schedule(schedule())
        self.store.create_schedule(
            schedule(schedule_id="sch-2", workload_id="wrk-2", owner_id="usr-2")
        )
        self.store.create_deployment_plan(deployment_plan())
        self.store.create_deployment_plan(
            deployment_plan(plan_id="plan-2", workload_id="wrk-2", actor_id="usr-2")
        )
        self.authorizer = ClosedActionPolicyAuthorizer(store=self.store)

    def test_allows_known_non_admin_actions_on_exact_surfaces(self) -> None:
        cases = (
            ("browser", ActionName.VIEW_DASHBOARD, "dashboard:usr-1"),
            ("slack", ActionName.VIEW_USAGE, "usage:usr-1"),
            ("browser", ActionName.DEPLOY_WORKLOAD, "dashboard:usr-1"),
            ("browser", ActionName.MANAGE_SCHEDULE, "workload:wrk-1"),
            ("slack", ActionName.MANAGE_SCHEDULE, "schedule:sch-1"),
            ("browser", ActionName.VIEW_DASHBOARD, "operation:op-1"),
            ("slack", ActionName.VIEW_DASHBOARD, "failure:op-1"),
            ("browser", ActionName.DEPLOY_WORKLOAD, "workload:wrk-1"),
            ("browser", ActionName.DEPLOY_WORKLOAD, "deployment-plan:plan-1"),
            ("slack", ActionName.MANAGE_SCHEDULE, "deployment-plan:plan-1"),
        )
        for surface, action, resource_id in cases:
            with self.subTest(surface=surface, action=action, resource_id=resource_id):
                decision = self.authorizer.authorize(
                    principal=principal(),
                    intent=ActionIntent(action=action, resource_id=resource_id),
                    surface=surface,
                )
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reason_code, "allowed")
                self.assertIsNone(decision.audit_message)

    def test_denies_cross_user_mutation_resources_based_on_persisted_ownership(
        self,
    ) -> None:
        cases = (
            (ActionName.DEPLOY_WORKLOAD, "workload:wrk-2"),
            (ActionName.MANAGE_SCHEDULE, "schedule:sch-2"),
            (ActionName.DEPLOY_WORKLOAD, "deployment-plan:plan-2"),
            (ActionName.MANAGE_SCHEDULE, "deployment-plan:plan-2"),
        )
        for action, resource_id in cases:
            with self.subTest(action=action, resource_id=resource_id):
                decision = self.authorizer.authorize(
                    principal=principal(),
                    intent=ActionIntent(action=action, resource_id=resource_id),
                    surface="browser",
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason_code, "principal_scope_mismatch")
                self.assertEqual(
                    decision.audit_message,
                    "resource is outside the authenticated scope",
                )

    def test_denies_missing_or_unavailable_mutation_resources_fail_closed(self) -> None:
        unavailable_authorizer = ClosedActionPolicyAuthorizer(
            store=UnavailableMemoryStore()
        )
        cases = (
            (self.authorizer, ActionName.DEPLOY_WORKLOAD, "workload:missing"),
            (self.authorizer, ActionName.MANAGE_SCHEDULE, "schedule:missing"),
            (
                self.authorizer,
                ActionName.DEPLOY_WORKLOAD,
                "deployment-plan:missing",
            ),
            (unavailable_authorizer, ActionName.DEPLOY_WORKLOAD, "workload:wrk-1"),
            (unavailable_authorizer, ActionName.MANAGE_SCHEDULE, "schedule:sch-1"),
            (
                unavailable_authorizer,
                ActionName.DEPLOY_WORKLOAD,
                "deployment-plan:plan-1",
            ),
        )
        for authorizer, action, resource_id in cases:
            with self.subTest(action=action, resource_id=resource_id):
                decision = authorizer.authorize(
                    principal=principal(),
                    intent=ActionIntent(action=action, resource_id=resource_id),
                    surface="slack",
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason_code, "principal_scope_mismatch")
                self.assertEqual(
                    decision.audit_message,
                    "resource is outside the authenticated scope",
                )

    def test_allows_admin_overview_for_admin_only(self) -> None:
        admin_decision = self.authorizer.authorize(
            principal=principal(
                user_id="admin-1",
                email="admin@madup.com",
                role=UserRole.ADMIN,
            ),
            intent=ActionIntent(
                action=ActionName.ADMIN_USAGE_OVERVIEW,
                resource_id="admin:overview",
            ),
            surface="browser",
        )
        self.assertTrue(admin_decision.allowed)
        self.assertEqual(admin_decision.reason_code, "allowed")

        denied = self.authorizer.authorize(
            principal=principal(),
            intent=ActionIntent(
                action=ActionName.ADMIN_USAGE_OVERVIEW,
                resource_id="admin:overview",
            ),
            surface="browser",
        )
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason_code, "admin_required")
        self.assertEqual(denied.audit_message, "administrator role is required")

    def test_allows_admin_actions_on_reviewed_mutation_kinds(self) -> None:
        admin = principal(
            user_id="admin-1",
            email="admin@madup.com",
            role=UserRole.ADMIN,
        )
        cases = (
            (ActionName.DEPLOY_WORKLOAD, "workload:wrk-2"),
            (ActionName.MANAGE_SCHEDULE, "schedule:sch-2"),
            (ActionName.DEPLOY_WORKLOAD, "deployment-plan:plan-2"),
        )
        for action, resource_id in cases:
            with self.subTest(action=action, resource_id=resource_id):
                decision = self.authorizer.authorize(
                    principal=admin,
                    intent=ActionIntent(action=action, resource_id=resource_id),
                    surface="slack",
                )
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reason_code, "allowed")

    def test_denies_malformed_inputs_and_scope_mismatches_without_echoing_values(
        self,
    ) -> None:
        cases = (
            (
                object(),
                ActionIntent(
                    action=ActionName.VIEW_DASHBOARD,
                    resource_id="dashboard:usr-1",
                ),
                "browser",
                "malformed_principal",
            ),
            (
                principal(),
                object(),
                "browser",
                "malformed_intent",
            ),
            (
                principal(),
                ActionIntent(
                    action=ActionName.VIEW_DASHBOARD,
                    resource_id="dashboard:usr-1",
                ),
                "Browser",
                "malformed_surface",
            ),
            (
                principal(),
                ActionIntent(
                    action=ActionName.MANAGE_SCHEDULE,
                    resource_id="dashboard:usr-1",
                ),
                "browser",
                "malformed_resource",
            ),
            (
                principal(),
                ActionIntent(
                    action=ActionName.VIEW_DASHBOARD,
                    resource_id="token:secret-user",
                ),
                "browser",
                "malformed_resource",
            ),
            (
                principal(
                    user_id="usr-1",
                    email="secret.person@madup.com",
                ),
                ActionIntent(
                    action=ActionName.VIEW_DASHBOARD,
                    resource_id="dashboard:usr-2",
                ),
                "slack",
                "principal_scope_mismatch",
            ),
        )

        for raw_principal, raw_intent, surface, expected_code in cases:
            with self.subTest(
                surface=surface,
                reason_code=expected_code,
            ):
                decision = self.authorizer.authorize(
                    principal=raw_principal,  # type: ignore[arg-type]
                    intent=raw_intent,  # type: ignore[arg-type]
                    surface=surface,
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason_code, expected_code)
                self.assertIsNotNone(decision.audit_message)
                audit_message = decision.audit_message or ""
                self.assertNotIn("secret.person@madup.com", audit_message)
                self.assertNotIn("dashboard:usr-2", decision.audit_message or "")
                self.assertNotIn("token:secret-user", decision.audit_message or "")


if __name__ == "__main__":
    unittest.main()
