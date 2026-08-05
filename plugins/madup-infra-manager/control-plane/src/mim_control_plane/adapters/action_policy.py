"""Closed-form central action policy for browser and Slack entrypoints."""

from __future__ import annotations

from typing import cast

from mim_control_plane.domain.central_identity import ActionIntent, ActionName
from mim_control_plane.domain.models import (
    DeploymentPlan,
    DeploymentPlanId,
    Schedule,
    ScheduleId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import UserRole
from mim_control_plane.ports.identity import ActionPolicyDecision
from mim_control_plane.ports.store import Store, StoreError
from mim_control_plane.security.identity import AuthenticatedPrincipal

_ALLOWED_SURFACES = frozenset({"browser", "slack"})
_ADMIN_OVERVIEW_RESOURCE = ("admin", "overview")
_RESOURCE_ACTIONS = {
    "dashboard": frozenset({ActionName.VIEW_DASHBOARD, ActionName.DEPLOY_WORKLOAD}),
    "usage": frozenset({ActionName.VIEW_USAGE}),
    "operation": frozenset({ActionName.VIEW_DASHBOARD}),
    "failure": frozenset({ActionName.VIEW_DASHBOARD}),
    "workload": frozenset(
        {ActionName.DEPLOY_WORKLOAD, ActionName.MANAGE_SCHEDULE}
    ),
    "schedule": frozenset({ActionName.MANAGE_SCHEDULE}),
    "deployment-plan": frozenset(
        {ActionName.DEPLOY_WORKLOAD, ActionName.MANAGE_SCHEDULE}
    ),
}
_ADMIN_OVERVIEW_ACTIONS = frozenset(
    {
        ActionName.VIEW_DASHBOARD,
        ActionName.DEPLOY_WORKLOAD,
        ActionName.ADMIN_USAGE_OVERVIEW,
    }
)


class ClosedActionPolicyAuthorizer:
    """Validate only the bounded action/resource shapes exposed by MIM."""

    def __init__(self, *, store: Store) -> None:
        self._store = store

    def authorize(
        self,
        *,
        principal: AuthenticatedPrincipal,
        intent: ActionIntent,
        surface: str,
    ) -> ActionPolicyDecision:
        if type(principal) is not AuthenticatedPrincipal:
            return _deny(
                reason_code="malformed_principal",
                audit_message="principal shape is invalid",
            )
        if not _principal_is_exact(principal):
            return _deny(
                reason_code="malformed_principal",
                audit_message="principal fields are invalid",
            )
        if type(intent) is not ActionIntent or type(intent.action) is not ActionName:
            return _deny(
                reason_code="malformed_intent",
                audit_message="action intent shape is invalid",
            )
        if surface not in _ALLOWED_SURFACES:
            return _deny(
                reason_code="malformed_surface",
                audit_message="surface is outside the reviewed entrypoints",
            )
        try:
            resource_kind, resource_value = _parse_resource(intent.resource_id)
        except ValueError:
            return _deny(
                reason_code="malformed_resource",
                audit_message="resource identifier is invalid",
            )

        if (resource_kind, resource_value) == _ADMIN_OVERVIEW_RESOURCE:
            return self._authorize_admin_overview(
                principal=principal,
                action=intent.action,
            )

        allowed_actions = _RESOURCE_ACTIONS.get(resource_kind)
        if allowed_actions is None or intent.action not in allowed_actions:
            return _deny(
                reason_code="malformed_resource",
                audit_message="resource is outside the reviewed policy",
            )
        if not self._resource_matches_authenticated_scope(
            principal=principal,
            resource_kind=resource_kind,
            resource_value=resource_value,
        ):
            return _deny(
                reason_code="principal_scope_mismatch",
                audit_message="resource is outside the authenticated scope",
            )
        return ActionPolicyDecision(allowed=True, reason_code="allowed")

    def _authorize_admin_overview(
        self,
        *,
        principal: AuthenticatedPrincipal,
        action: ActionName,
    ) -> ActionPolicyDecision:
        if action not in _ADMIN_OVERVIEW_ACTIONS:
            return _deny(
                reason_code="malformed_resource",
                audit_message="resource is outside the reviewed policy",
            )
        if principal.role is not UserRole.ADMIN:
            return _deny(
                reason_code="admin_required",
                audit_message="administrator role is required",
            )
        return ActionPolicyDecision(allowed=True, reason_code="allowed")

    def _resource_matches_authenticated_scope(
        self,
        *,
        principal: AuthenticatedPrincipal,
        resource_kind: str,
        resource_value: str,
    ) -> bool:
        if principal.role is UserRole.ADMIN:
            return True
        if resource_kind in {"dashboard", "usage"}:
            return resource_value == principal.user_id
        if resource_kind == "workload":
            return self._matches_workload_owner(
                principal=principal,
                workload_id=WorkloadId(resource_value),
            )
        if resource_kind == "schedule":
            return self._matches_schedule_owner(
                principal=principal,
                schedule_id=ScheduleId(resource_value),
            )
        if resource_kind == "deployment-plan":
            return self._matches_plan_actor(
                principal=principal,
                plan_id=DeploymentPlanId(resource_value),
            )
        return True

    def _matches_workload_owner(
        self,
        *,
        principal: AuthenticatedPrincipal,
        workload_id: WorkloadId,
    ) -> bool:
        try:
            workload = self._store.get_workload(workload_id)
        except (RuntimeError, StoreError, TypeError, ValueError):
            return False
        return type(workload) is Workload and workload.owner_id == principal.user_id

    def _matches_schedule_owner(
        self,
        *,
        principal: AuthenticatedPrincipal,
        schedule_id: ScheduleId,
    ) -> bool:
        try:
            schedule = self._store.get_schedule(schedule_id)
        except (RuntimeError, StoreError, TypeError, ValueError):
            return False
        return type(schedule) is Schedule and schedule.owner_id == principal.user_id

    def _matches_plan_actor(
        self,
        *,
        principal: AuthenticatedPrincipal,
        plan_id: DeploymentPlanId,
    ) -> bool:
        try:
            plan = self._store.get_deployment_plan(plan_id)
        except (RuntimeError, StoreError, TypeError, ValueError):
            return False
        return type(plan) is DeploymentPlan and plan.actor_id == principal.user_id


def _principal_is_exact(principal: AuthenticatedPrincipal) -> bool:
    return (
        _is_exact_text(principal.user_id)
        and _is_exact_text(principal.email)
        and type(principal.role) is UserRole
    )


def _parse_resource(resource_id: object) -> tuple[str, str]:
    if not _is_exact_text(resource_id):
        raise ValueError("resource is invalid")
    normalized = cast(str, resource_id)
    kind, separator, value = normalized.partition(":")
    if not separator or not _is_exact_text(kind) or not _is_exact_text(value):
        raise ValueError("resource is invalid")
    if ":" in value:
        raise ValueError("resource is invalid")
    return kind, value


def _is_exact_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _deny(*, reason_code: str, audit_message: str) -> ActionPolicyDecision:
    return ActionPolicyDecision(
        allowed=False,
        reason_code=reason_code,
        audit_message=audit_message,
    )
