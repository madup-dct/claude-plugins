"""Canonical deployment-plan hashing and single-use consumption helpers."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any

from mim_control_plane.domain.models import (
    DeploymentPlan,
    DeploymentPlanId,
    Operation,
    UserId,
)
from mim_control_plane.domain.states import PlanState
from mim_control_plane.ports.store import Store


class PlanValidationError(ValueError):
    """Base class for deployment-plan validation failures."""


class PlanActorMismatch(PlanValidationError):
    """Raised when a caller does not match the reviewed plan actor."""


class PlanActionMismatch(PlanValidationError):
    """Raised when a request action does not match the reviewed plan."""


class PlanNormalizationError(PlanValidationError):
    """Raised when plan material cannot be normalized to safe canonical JSON."""


class PlanMaterialMismatch(PlanValidationError):
    """Raised when recomputed material differs from the reviewed plan."""


class PlanExpired(PlanValidationError):
    """Raised when a reviewed plan is already expired."""


class PlanStateMismatch(PlanValidationError):
    """Raised when a recovery path is used for the wrong plan state."""


def canonical_plan_payload(
    material: Mapping[str, object],
    *,
    action: str,
    policy_version: str,
) -> str:
    """Return the exact canonical JSON payload used for plan hashing."""

    return json.dumps(
        {
            "action": action,
            "material": _normalize_json_value(material),
            "policy_version": policy_version,
        },
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def hash_plan_material(
    material: Mapping[str, object],
    *,
    action: str,
    policy_version: str,
) -> str:
    """Hash the complete material plan together with its policy version."""

    payload = canonical_plan_payload(
        material,
        action=action,
        policy_version=policy_version,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def validate_plan_request(
    plan: DeploymentPlan,
    *,
    actor_id: UserId,
    material: Mapping[str, object],
    action: str,
    policy_version: str,
    at: datetime,
) -> None:
    """Fail closed if a caller or material does not match the reviewed plan."""

    _validate_plan_actor_and_action(plan, actor_id=actor_id, action=action)
    if at.tzinfo is None or at.utcoffset() != plan.expires_at.utcoffset():
        raise PlanExpired("deployment plan validation requires a UTC-aware timestamp.")
    if at >= plan.expires_at:
        raise PlanExpired("deployment plan has expired.")

    _validate_plan_material(
        plan,
        material=material,
        action=action,
        policy_version=policy_version,
    )


def validate_consumed_plan_repair(
    plan: DeploymentPlan,
    *,
    actor_id: UserId,
    material: Mapping[str, object],
    action: str,
    policy_version: str,
) -> None:
    """Validate immutable material for a previously claimed operation repair."""

    if plan.state is not PlanState.CONSUMED:
        raise PlanStateMismatch("deployment plan is not consumed.")
    _validate_plan_actor_and_action(plan, actor_id=actor_id, action=action)
    _validate_plan_material(
        plan,
        material=material,
        action=action,
        policy_version=policy_version,
    )


def _validate_plan_actor_and_action(
    plan: DeploymentPlan,
    *,
    actor_id: UserId,
    action: str,
) -> None:
    if plan.actor_id != actor_id:
        raise PlanActorMismatch("deployment plan actor does not match the caller.")
    if plan.action != action:
        raise PlanActionMismatch("deployment plan action does not match the caller.")


def _validate_plan_material(
    plan: DeploymentPlan,
    *,
    material: Mapping[str, object],
    action: str,
    policy_version: str,
) -> None:
    expected_hash = hash_plan_material(
        material,
        action=action,
        policy_version=policy_version,
    )
    if plan.policy_version != policy_version or plan.material_hash != expected_hash:
        raise PlanMaterialMismatch(
            "deployment plan material or policy version no longer matches review.",
        )


def consume_plan_with_operation(
    store: Store,
    *,
    plan_id: DeploymentPlanId,
    actor_id: UserId,
    material: Mapping[str, object],
    action: str,
    policy_version: str,
    operation: Operation,
    consumed_at: datetime,
) -> tuple[DeploymentPlan, Operation]:
    """Atomically consume a reviewed plan while creating the first mutation record."""

    plan = store.get_deployment_plan(plan_id)
    validate_plan_request(
        plan,
        actor_id=actor_id,
        material=material,
        action=action,
        policy_version=policy_version,
        at=consumed_at,
    )
    if operation.actor_id != actor_id:
        raise PlanActorMismatch("operation actor does not match the reviewed caller.")
    if operation.action != action:
        raise PlanActionMismatch(
            "operation action does not match the reviewed deployment plan."
        )
    return store.consume_deployment_plan_with_operation(
        plan_id=plan.id,
        actor_id=actor_id,
        expected_material_hash=plan.material_hash,
        expected_action=action,
        policy_version=policy_version,
        consumed_at=consumed_at,
        operation=operation,
    )


def _normalize_json_value(value: object) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, inner_value in value.items():
            if not isinstance(key, str):
                raise TypeError("plan material keys must be strings.")
            normalized[key] = _normalize_json_value(inner_value)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PlanNormalizationError(
                "plan material must use finite JSON number values only."
            )
        return value
    raise PlanNormalizationError("plan material must contain JSON-like values only.")
