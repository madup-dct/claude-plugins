"""Pure quota and cost policy decisions for the MIM control plane."""

from __future__ import annotations

from dataclasses import dataclass

from mim_control_plane.config import (
    ADMIN_BUDGET_CEILING_KRW,
    DEFAULT_SECRET_LIMIT,
    HARD_SECRET_LIMIT,
    PER_USER_SCHEDULE_LIMIT,
    PER_USER_SERVICE_LIMIT,
    TARGET_MONTHLY_BUDGET_KRW,
)
from mim_control_plane.services.usage import CostSnapshot


class QuotaPolicyError(ValueError):
    """Raised when quota or cost policy inputs are invalid."""


@dataclass(frozen=True, slots=True)
class ResourceInventory:
    active_services: int
    active_schedules: int
    active_secrets: int
    approved_secret_limit: int | None = None

    def __post_init__(self) -> None:
        _require_non_negative_int(self.active_services, "active_services")
        _require_non_negative_int(self.active_schedules, "active_schedules")
        _require_non_negative_int(self.active_secrets, "active_secrets")
        if self.approved_secret_limit is not None:
            _require_non_negative_int(
                self.approved_secret_limit,
                "approved_secret_limit",
            )


@dataclass(frozen=True, slots=True)
class ResourcePolicyDecision:
    service_limit_reached: bool
    schedule_limit_reached: bool
    secret_limit_reached: bool
    secret_limit: int
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_negative_int(self.secret_limit, "secret_limit")
        if not isinstance(self.reason_codes, tuple):
            raise QuotaPolicyError("reason_codes must be immutable.")


@dataclass(frozen=True, slots=True)
class CostPolicyDecision:
    user_percent: int
    warn: bool
    block_new: bool
    pause: bool
    emergency_stop: bool
    projected_user_cost_krw: int
    org_policy_cost_krw: int
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_negative_int(self.user_percent, "user_percent")
        _require_non_negative_int(
            self.projected_user_cost_krw,
            "projected_user_cost_krw",
        )
        _require_non_negative_int(self.org_policy_cost_krw, "org_policy_cost_krw")
        if not isinstance(self.reason_codes, tuple):
            raise QuotaPolicyError("reason_codes must be immutable.")


def evaluate_resource_policy(inventory: ResourceInventory) -> ResourcePolicyDecision:
    """Evaluate current inventory against create-blocking fixed limits."""

    secret_limit = _resolved_secret_limit(inventory.approved_secret_limit)
    service_limit_reached = inventory.active_services >= PER_USER_SERVICE_LIMIT
    schedule_limit_reached = inventory.active_schedules >= PER_USER_SCHEDULE_LIMIT
    secret_limit_reached = inventory.active_secrets >= secret_limit
    reason_codes = tuple(
        reason_code
        for enabled, reason_code in (
            (service_limit_reached, "service_limit_reached"),
            (schedule_limit_reached, "schedule_limit_reached"),
            (secret_limit_reached, "secret_limit_reached"),
        )
        if enabled
    )
    return ResourcePolicyDecision(
        service_limit_reached=service_limit_reached,
        schedule_limit_reached=schedule_limit_reached,
        secret_limit_reached=secret_limit_reached,
        secret_limit=secret_limit,
        reason_codes=reason_codes,
    )


def evaluate_cost_policy(
    *,
    snapshot: CostSnapshot,
    lag_reservation_krw: int = 0,
    proposed_cost_krw: int = 0,
    org_projected_additional_krw: int = 0,
) -> CostPolicyDecision:
    """Evaluate user and org pressure with explicit projected additions only."""

    _require_non_negative_int(lag_reservation_krw, "lag_reservation_krw")
    _require_non_negative_int(proposed_cost_krw, "proposed_cost_krw")
    _require_non_negative_int(
        org_projected_additional_krw,
        "org_projected_additional_krw",
    )
    user_percent = (snapshot.user_policy_krw * 100) // TARGET_MONTHLY_BUDGET_KRW
    projected_user_cost = (
        snapshot.user_policy_krw + lag_reservation_krw + proposed_cost_krw
    )
    org_policy_cost = (
        snapshot.org_direct_policy_krw
        + snapshot.shared_policy_krw
        + lag_reservation_krw
        + proposed_cost_krw
        + org_projected_additional_krw
    )
    warn = user_percent >= 70
    block_new = user_percent >= 90
    pause = projected_user_cost >= TARGET_MONTHLY_BUDGET_KRW
    emergency_stop = org_policy_cost >= ADMIN_BUDGET_CEILING_KRW
    reason_codes = tuple(
        reason_code
        for enabled, reason_code in (
            (warn, "user_warn_threshold_reached"),
            (block_new, "user_block_new_threshold_reached"),
            (pause, "user_projected_limit_reached"),
            (emergency_stop, "org_emergency_ceiling_reached"),
        )
        if enabled
    )
    return CostPolicyDecision(
        user_percent=user_percent,
        warn=warn,
        block_new=block_new,
        pause=pause,
        emergency_stop=emergency_stop,
        projected_user_cost_krw=projected_user_cost,
        org_policy_cost_krw=org_policy_cost,
        reason_codes=reason_codes,
    )


def _resolved_secret_limit(approved_secret_limit: int | None) -> int:
    if approved_secret_limit is None:
        return DEFAULT_SECRET_LIMIT
    if approved_secret_limit < 1 or approved_secret_limit > HARD_SECRET_LIMIT:
        raise QuotaPolicyError(
            "approved_secret_limit must stay within the centrally approved range."
        )
    return approved_secret_limit


def _require_non_negative_int(value: object, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise QuotaPolicyError(f"{field_name} must be a non-negative integer.")


__all__ = [
    "CostPolicyDecision",
    "QuotaPolicyError",
    "ResourceInventory",
    "ResourcePolicyDecision",
    "evaluate_cost_policy",
    "evaluate_resource_policy",
]
