"""Sanitized audit helpers for reviewed plan-bound mutations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from mim_control_plane.domain.models import (
    AuditEvent,
    AuditEventId,
    DeploymentPlan,
    UserId,
)
from mim_control_plane.security.redaction import (
    OutputSurface,
    flatten_summary,
    sanitize_output,
)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Persistable audit material with no raw request body or secret-bearing payload."""

    event: AuditEvent
    plan_hash: str | None
    sanitized_summary: tuple[tuple[str, str], ...]


def build_audit_record(
    *,
    event_id: str,
    actor_id: UserId | None,
    action: str,
    target_ref: str,
    policy_decision: str,
    correlation_id: str,
    outcome: str,
    occurred_at: datetime,
    plan: DeploymentPlan | None = None,
    output: Mapping[str, object] | None = None,
    before_ref: str | None = None,
    after_ref: str | None = None,
) -> AuditRecord:
    """Build the append-only audit payload from reviewed plan metadata only."""

    sanitized_output = (
        {} if output is None else sanitize_output(OutputSurface.AUDIT, output)
    )
    summary: dict[str, object] = dict(
        plan.sanitized_summary if plan is not None else ()
    )
    if "summary" in sanitized_output and isinstance(
        sanitized_output["summary"],
        Mapping,
    ):
        for key, value in sanitized_output["summary"].items():
            summary[key] = value

    event = AuditEvent(
        id=AuditEventId(event_id),
        actor_id=actor_id,
        action=action,
        target_ref=target_ref,
        policy_decision=policy_decision,
        before_ref=before_ref,
        after_ref=after_ref,
        correlation_id=correlation_id,
        outcome=outcome,
        occurred_at=occurred_at,
    )
    return AuditRecord(
        event=event,
        plan_hash=None if plan is None else plan.material_hash,
        sanitized_summary=flatten_summary(summary),
    )
