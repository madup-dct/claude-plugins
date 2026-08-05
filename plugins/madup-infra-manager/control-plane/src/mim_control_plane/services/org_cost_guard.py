"""Fail-closed org-wide cost guard for hot-path authorization checks."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from mim_control_plane.domain.models import OrgCostGuard
from mim_control_plane.ports.store import AlreadyExists, NotFound
from mim_control_plane.services.quota import CostPolicyDecision

ORG_COST_GUARD_MAX_AGE = timedelta(hours=2)


class OrgCostGuardDenied(PermissionError):
    """Raised when the reviewed org-wide cost guard is unavailable or closed."""


class OrgCostGuardStore(Protocol):
    def create_org_cost_guard(self, guard: OrgCostGuard) -> OrgCostGuard: ...

    def get_org_cost_guard(self) -> OrgCostGuard: ...

    def save_org_cost_guard(
        self,
        guard: OrgCostGuard,
        *,
        expected_version: int,
    ) -> OrgCostGuard: ...


def persist_org_cost_guard(
    *,
    store: OrgCostGuardStore,
    evaluated_at: datetime,
    latest_usage_collected_at: datetime | None,
    decision: CostPolicyDecision,
) -> OrgCostGuard:
    candidate = OrgCostGuard(
        evaluated_at=evaluated_at,
        latest_usage_collected_at=latest_usage_collected_at,
        emergency_stop=decision.emergency_stop,
        org_policy_cost_krw=decision.org_policy_cost_krw,
    )
    try:
        current = store.get_org_cost_guard()
    except NotFound:
        try:
            return store.create_org_cost_guard(candidate)
        except AlreadyExists:
            current = store.get_org_cost_guard()
    if _same_guard_material(current, candidate):
        return current
    if current.evaluated_at > candidate.evaluated_at:
        raise ValueError("org cost guard evaluated_at must be monotonic.")
    if current.evaluated_at == candidate.evaluated_at:
        raise ValueError("org cost guard material conflicts at the same evaluated_at.")
    return store.save_org_cost_guard(
        OrgCostGuard(
            evaluated_at=candidate.evaluated_at,
            latest_usage_collected_at=candidate.latest_usage_collected_at,
            emergency_stop=candidate.emergency_stop,
            org_policy_cost_krw=candidate.org_policy_cost_krw,
            version=current.version + 1,
        ),
        expected_version=current.version,
    )


def _same_guard_material(current: OrgCostGuard, candidate: OrgCostGuard) -> bool:
    return (
        current.evaluated_at == candidate.evaluated_at
        and current.latest_usage_collected_at
        == candidate.latest_usage_collected_at
        and current.emergency_stop is candidate.emergency_stop
        and current.org_policy_cost_krw == candidate.org_policy_cost_krw
    )


def require_current_org_cost_guard(
    *,
    store: OrgCostGuardStore,
    now: datetime,
) -> OrgCostGuard:
    try:
        guard = store.get_org_cost_guard()
    except NotFound as exc:
        raise OrgCostGuardDenied("org cost guard is unavailable") from exc
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be UTC-aware")
    if guard.evaluated_at > now:
        raise OrgCostGuardDenied("org cost guard is invalid")
    if (
        guard.latest_usage_collected_at is not None
        and guard.latest_usage_collected_at > now
    ):
        raise OrgCostGuardDenied("org cost guard is invalid")
    if now - guard.evaluated_at > ORG_COST_GUARD_MAX_AGE:
        raise OrgCostGuardDenied("org cost guard is stale")
    if guard.emergency_stop:
        raise OrgCostGuardDenied("org cost guard is closed")
    return guard


__all__ = [
    "ORG_COST_GUARD_MAX_AGE",
    "OrgCostGuardDenied",
    "persist_org_cost_guard",
    "require_current_org_cost_guard",
]
