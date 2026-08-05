"""Read-only dashboard and API projections for the MIM control plane."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Any, Callable, Iterable, Protocol, cast

from mim_control_plane.config import (
    PER_USER_SERVICE_LIMIT,
    PILOT_MAX_IDENTITIES,
    PUBLIC_ORIGIN,
)
from mim_control_plane.domain.models import Operation, OperationId, UserId
from mim_control_plane.domain.states import (
    SecretLifecycleState,
    UserRole,
    WorkloadState,
)
from mim_control_plane.ports.store import AUTO_DEPLOY_ACTOR_ID, NotFound, Store
from mim_control_plane.security.authorization import (
    AccessDenied,
    require_owner_or_admin,
)
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.services.app_hostname import build_app_hostname
from mim_control_plane.services.quota import (
    ResourceInventory,
    evaluate_cost_policy,
    evaluate_resource_policy,
)
from mim_control_plane.services.schedules import require_utc_datetime
from mim_control_plane.services.usage import (
    build_cost_snapshot,
    build_usage_ledger,
    compute_activity_metrics,
    usage_entries_for_utc_month,
)

_IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_PATTERN = re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,}[0-9A-Fa-f]{1,4}\b")
_COOKIE_PATTERN = re.compile(r"(?i)\bcookie\s*:\s*\S+")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]+")
_SECRET_TOKEN_PATTERN = re.compile(
    r"(?i)\b(?:token|secret|api[_-]?key|password)\b[^\s,;:]*"
)
_USER_AGENT_PATTERN = re.compile(r"\b[a-z]+/\d[\w.-]*\b", re.IGNORECASE)
_PROMPT_PATTERN = re.compile(r"(?i)\bprompt\b")
_RECORDED_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_APPROVED_OPERATION_FAILURE_CODES = frozenset(
    {
        "deploy_denied",
        "build_failed",
        "deploy_failed",
        "deploy_unhealthy",
    }
)
_GENERIC_OPERATION_FAILURE_CODE = "operation_failed"
_DASHBOARD_WORKLOAD_DETAIL_LIMIT = PILOT_MAX_IDENTITIES * PER_USER_SERVICE_LIMIT
_MAINTENANCE_JOB_ORDER = ("identity-sync", "lifecycle", "usage-ingest")
_MAINTENANCE_STALE_WINDOWS = {
    "identity-sync": timedelta(minutes=30),
    "lifecycle": timedelta(minutes=30),
    "usage-ingest": timedelta(hours=2),
}


class ReadAccessDenied(PermissionError):
    """Raised when a caller can authenticate but not view the resource."""


class ReadNotFound(LookupError):
    """Raised when a scoped read result should not be exposed."""


@dataclass(frozen=True, slots=True)
class DashboardPage:
    title: str
    html: str


def dashboard_resource_id(user_id: UserId) -> str:
    return f"dashboard:{user_id}"


def usage_resource_id(user_id: UserId) -> str:
    return f"usage:{user_id}"


def operation_resource_id(operation_id: str) -> str:
    return f"operation:{operation_id}"


def workload_resource_id(workload_id: str) -> str:
    return f"workload:{workload_id}"


def failure_resource_id(operation_id: str) -> str:
    return f"failure:{operation_id}"


ADMIN_OVERVIEW_RESOURCE_ID = "admin:overview"


class DeploymentPlanner(Protocol):
    def plan_deploy(
        self,
        *,
        principal: AuthenticatedPrincipal,
        workload_id: str,
    ) -> dict[str, object]: ...


class ControlPlaneReadService:
    """Build reviewed read models for the dashboard, API, and MCP surfaces."""

    def __init__(
        self,
        *,
        store: Store,
        clock: Callable[[], datetime],
        deployment_planner: DeploymentPlanner | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._deployment_planner = deployment_planner

    def list_workloads(self, *, principal: AuthenticatedPrincipal) -> dict[str, Any]:
        owner_id = None if principal.role is UserRole.ADMIN else principal.user_id
        workloads = self._store.list_workloads(owner_id=owner_id)
        schedules = self._store.list_schedules(owner_id=owner_id)
        secrets = self._store.list_secret_metadata(owner_id=owner_id)
        detail_counts: dict[UserId, int] = {}
        detail_total = 0
        details_truncated = False
        serialized_workloads: list[dict[str, object]] = []
        for workload in workloads:
            include_operational_details = False
            if workload.state is not WorkloadState.ARCHIVED:
                owner_detail_count = detail_counts.get(workload.owner_id, 0)
                include_operational_details = (
                    owner_detail_count < PER_USER_SERVICE_LIMIT
                    and detail_total < _DASHBOARD_WORKLOAD_DETAIL_LIMIT
                )
                if include_operational_details:
                    detail_counts[workload.owner_id] = owner_detail_count + 1
                    detail_total += 1
                else:
                    details_truncated = True
            serialized_workloads.append(
                self._serialize_workload(
                    workload,
                    include_operational_details=include_operational_details,
                )
            )
        return {
            "action": "list_workloads",
            "scope": "admin" if principal.role is UserRole.ADMIN else "user",
            "operational_detail_limit_per_owner": PER_USER_SERVICE_LIMIT,
            "operational_detail_limit_total": _DASHBOARD_WORKLOAD_DETAIL_LIMIT,
            "operational_details_truncated": details_truncated,
            "workloads": serialized_workloads,
            "schedules": [self._serialize_schedule(item) for item in schedules],
            "secrets": [self._serialize_secret(item) for item in secrets],
        }

    def get_operation(
        self,
        *,
        principal: AuthenticatedPrincipal,
        operation_id: str,
    ) -> dict[str, Any]:
        operation = self._scoped_operation(
            principal=principal,
            operation_id=OperationId(operation_id),
        )
        return {
            "action": "get_operation",
            "operation": {
                "id": str(operation.id),
                "resource_id": operation_resource_id(str(operation.id)),
                "workload_id": None
                if operation.workload_id is None
                else str(operation.workload_id),
                "state": operation.state.value,
                "action_name": operation.action,
                "created_at": operation.created_at.isoformat(),
                "updated_at": operation.updated_at.isoformat(),
            },
        }

    def get_failure(
        self,
        *,
        principal: AuthenticatedPrincipal,
        operation_id: str,
    ) -> dict[str, Any]:
        operation = self._scoped_operation(
            principal=principal,
            operation_id=OperationId(operation_id),
        )
        message = "No failure recorded."
        if operation.sanitized_failure is not None:
            message = _project_operation_failure_code(operation.sanitized_failure)
        return {
            "action": "explain_failure",
            "failure": {
                "id": str(operation.id),
                "resource_id": failure_resource_id(str(operation.id)),
                "status": operation.state.value,
                "message": message,
            },
        }

    def get_usage(self, *, principal: AuthenticatedPrincipal) -> dict[str, Any]:
        current_now = self._now()
        if principal.role is UserRole.ADMIN:
            entries = usage_entries_for_utc_month(
                self._store.list_usage_entries(),
                now=current_now,
            )
            activity_events = self._store.list_activity_events()
            ledger = build_usage_ledger(entries)
            organization = {
                "estimated_krw": sum(
                    entry.estimated_cost_krw for entry in ledger.entries
                ),
                "finalized_krw": sum(
                    entry.finalized_cost_krw or 0 for entry in ledger.entries
                ),
            }
            platform_shared = {
                "estimated_krw": sum(
                    entry.estimated_cost_krw for entry in ledger.shared_entries
                ),
                "finalized_krw": sum(
                    entry.finalized_cost_krw or 0 for entry in ledger.shared_entries
                ),
            }
            users = []
            for user in self._store.list_users():
                user_ledger = build_usage_ledger(
                    usage_entries_for_utc_month(
                        self._store.list_usage_entries(owner_id=user.id),
                        now=current_now,
                    )
                )
                snapshot = build_cost_snapshot(user_ledger, user_id=user.id)
                inventory = self._inventory(owner_id=user.id)
                resource_policy = evaluate_resource_policy(inventory)
                workloads = self._store.list_workloads(owner_id=user.id)
                schedules = self._store.list_schedules(owner_id=user.id)
                users.append(
                    {
                        "user_id": str(user.id),
                        "email": _sanitize_text(user.email),
                        "state": user.state.value,
                        "estimated_krw": snapshot.user_direct_estimated_krw,
                        "finalized_krw": snapshot.user_direct_finalized_krw,
                        "policy_krw": snapshot.user_policy_krw,
                        "cost_percent": snapshot.user_percent,
                        "inventory": {
                            "active_services": inventory.active_services,
                            "active_schedules": inventory.active_schedules,
                            "active_secrets": inventory.active_secrets,
                        },
                        "resource_policy": {
                            "service_limit_reached": (
                                resource_policy.service_limit_reached
                            ),
                            "schedule_limit_reached": (
                                resource_policy.schedule_limit_reached
                            ),
                            "secret_limit_reached": (
                                resource_policy.secret_limit_reached
                            ),
                            "secret_limit": resource_policy.secret_limit,
                        },
                        "health": {
                            "workloads": self._workload_health(workloads),
                            "schedules": self._schedule_health(schedules),
                            "lifecycle_state": user.state.value,
                        },
                    }
                )
            return {
                "action": "get_usage",
                "scope": "admin",
                "costs": {
                    "organization": organization,
                    "platform_shared": platform_shared,
                },
                "metrics": self._serialize_metrics(
                    compute_activity_metrics(activity_events, now=current_now)
                ),
                "users": users,
                "maintenance_jobs": self._maintenance_jobs(now=current_now),
            }

        viewer = self._store.get_user(principal.user_id)
        owner_entries = usage_entries_for_utc_month(
            self._store.list_usage_entries(owner_id=principal.user_id),
            now=current_now,
        )
        ledger = build_usage_ledger(owner_entries)
        snapshot = build_cost_snapshot(ledger, user_id=principal.user_id)
        activity_events = self._store.list_activity_events(user_id=principal.user_id)
        inventory = self._inventory(owner_id=principal.user_id)
        resource_policy = evaluate_resource_policy(inventory)
        cost_policy = evaluate_cost_policy(snapshot=snapshot)
        workloads = self._store.list_workloads(owner_id=principal.user_id)
        schedules = self._store.list_schedules(owner_id=principal.user_id)
        return {
            "action": "get_usage",
            "scope": "user",
            "viewer_id": str(principal.user_id),
            "user": {
                "user_id": str(viewer.id),
                "email": _sanitize_text(viewer.email),
                "state": viewer.state.value,
            },
            "costs": {
                "user_direct": {
                    "estimated_krw": snapshot.user_direct_estimated_krw,
                    "finalized_krw": snapshot.user_direct_finalized_krw,
                    "policy_krw": snapshot.user_policy_krw,
                    "percent": snapshot.user_percent,
                }
            },
            "metrics": self._serialize_metrics(
                compute_activity_metrics(
                    activity_events,
                    now=current_now,
                    user_id=principal.user_id,
                )
            ),
            "inventory": {
                "active_services": inventory.active_services,
                "active_schedules": inventory.active_schedules,
                "active_secrets": inventory.active_secrets,
            },
            "health": {
                "workloads": self._workload_health(workloads),
                "schedules": self._schedule_health(schedules),
                "lifecycle_state": viewer.state.value,
            },
            "resource_policy": {
                "service_limit_reached": resource_policy.service_limit_reached,
                "schedule_limit_reached": resource_policy.schedule_limit_reached,
                "secret_limit_reached": resource_policy.secret_limit_reached,
                "secret_limit": resource_policy.secret_limit,
                "reason_codes": list(resource_policy.reason_codes),
            },
            "cost_policy": {
                "warn": cost_policy.warn,
                "block_new": cost_policy.block_new,
                "pause": cost_policy.pause,
                "emergency_stop": cost_policy.emergency_stop,
                "reason_codes": list(cost_policy.reason_codes),
            },
        }

    def _now(self) -> datetime:
        return require_utc_datetime(self._clock(), label="dashboard clock")

    def plan_deploy(
        self,
        *,
        principal: AuthenticatedPrincipal,
        workload_id: str | None = None,
    ) -> dict[str, Any]:
        if self._deployment_planner is None:
            return self._planning_unavailable(
                principal=principal,
                action="plan_deploy",
            )
        if workload_id is None:
            return {
                "action": "plan_deploy",
                "status": "input_required",
                "reason_code": "workload_id_required",
            }
        return self._deployment_planner.plan_deploy(
            principal=principal,
            workload_id=workload_id,
        )

    def plan_schedule(self, *, principal: AuthenticatedPrincipal) -> dict[str, Any]:
        return self._planning_unavailable(principal=principal, action="plan_schedule")

    def render_dashboard(self, *, principal: AuthenticatedPrincipal) -> DashboardPage:
        workloads = self.list_workloads(principal=principal)
        usage = self.get_usage(principal=principal)
        workload_items = cast(list[dict[str, Any]], workloads["workloads"])
        schedule_items = cast(list[dict[str, Any]], workloads["schedules"])
        secret_items = cast(list[dict[str, Any]], workloads["secrets"])
        usage_costs = cast(dict[str, Any], usage["costs"])
        usage_metrics = cast(dict[str, Any], usage["metrics"])
        scope = "Admin Overview" if principal.role is UserRole.ADMIN else "User Console"
        sections = [
            "<section class='hero'>"
            "<p class='eyebrow'>Madup Infra Manager</p>"
            f"<h1>{escape(scope)}</h1>"
            "<p class='lede'>"
            "Operational status for centrally managed infrastructure."
            "</p>"
            "</section>",
        ]
        if usage["scope"] == "admin":
            admin_users = cast(list[dict[str, Any]], usage["users"])
            maintenance_jobs = cast(list[dict[str, Any]], usage["maintenance_jobs"])
            sections.append(
                self._table_html(
                    "Maintenance Jobs",
                    ("Job", "Outcome", "Stale", "Started", "Finished", "Summary"),
                    (
                        (
                            item["job_name"],
                            item["outcome"],
                            "Stale" if item["stale"] else "Healthy",
                            item["started_at"] or "-",
                            item["finished_at"] or "-",
                            self._summary_text(cast(dict[str, int], item["summary"])),
                        )
                        for item in maintenance_jobs
                    ),
                )
            )
            sections.append(
                self._table_html(
                    "Operational Status",
                    ("User", "State", "Cost %", "Inventory", "Health"),
                    (
                        (
                            item["email"],
                            str(item["state"]).upper(),
                            f"{item['cost_percent']}%",
                            self._inventory_text(
                                cast(dict[str, int], item["inventory"])
                            ),
                            self._health_text(cast(dict[str, str], item["health"])),
                        )
                        for item in admin_users
                    ),
                )
            )
        else:
            viewer = cast(dict[str, Any], usage["user"])
            health = cast(dict[str, str], usage["health"])
            inventory = cast(dict[str, int], usage["inventory"])
            user_direct = cast(dict[str, Any], usage_costs["user_direct"])
            sections.append(
                "<section class='panel'>"
                "<h2>Operational Status</h2>"
                f"<p>User: {escape(str(viewer['email']))}</p>"
                f"<p>State: {escape(str(viewer['state']).upper())}</p>"
                f"<p>Current Month Cost: {escape(str(user_direct['percent']))}%</p>"
                f"<p>Inventory: {escape(self._inventory_text(inventory))}</p>"
                f"<p>Health: {escape(self._health_text(health))}</p>"
                "</section>"
            )
        sections.extend(
            [
                self._workloads_table_html(workload_items),
                self._table_html(
                    "Schedules",
                    ("ID", "Cron", "Timezone", "State"),
                    (
                        (
                            item["id"],
                            item["cron"],
                            item["timezone"],
                            item["state"],
                        )
                        for item in schedule_items
                    ),
                ),
                self._table_html(
                    "Secrets",
                    ("ID", "Name", "Integration", "Lifecycle"),
                    (
                        (
                            item["id"],
                            item["name"],
                            item["integration_type"],
                            item["lifecycle_state"],
                        )
                        for item in secret_items
                    ),
                ),
            ]
        )
        if usage["scope"] == "admin":
            platform_shared = cast(dict[str, Any], usage_costs["platform_shared"])
            organization = cast(dict[str, Any], usage_costs["organization"])
            sections.append(
                "<section class='panel'>"
                "<h2>Platform Shared Cost</h2>"
                f"<p>Estimated Cost: {platform_shared['estimated_krw']} KRW</p>"
                f"<p>Finalized Cost: {platform_shared['finalized_krw']} KRW</p>"
                "<p>Organization Estimated Cost: "
                f"{organization['estimated_krw']} KRW</p>"
                "</section>"
            )
        else:
            sections.append(
                "<section class='panel'>"
                "<h2>Quota</h2>"
                f"<p>Estimated Cost: {user_direct['estimated_krw']} KRW</p>"
                f"<p>Finalized Cost: {user_direct['finalized_krw']} KRW</p>"
                "</section>"
            )
        sections.append(
            "<section class='panel'>"
            "<h2>Activity Metrics</h2>"
            f"<p>Dashboard Visits 30d: {usage_metrics['dashboard_visits_30d']}</p>"
            f"<p>MCP Actions 30d: {usage_metrics['mcp_actions_30d']}</p>"
            f"<p>Failures 30d: {usage_metrics['failures_30d']}</p>"
            f"<p>Authorization Denials 30d: {usage_metrics['denials_30d']}</p>"
            "<p>External edge metrics are unavailable pending reviewed telemetry "
            "ingestion.</p>"
            "</section>"
        )
        html = (
            "<!doctype html><html lang='ko'><head>"
            "<meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Madup Infra Manager</title>"
            "<link rel='stylesheet' href='/static/dashboard.css'>"
            "</head><body>"
            "<main class='shell'>" + "".join(sections) + "</main>"
            "<script src='/static/dashboard.js' defer></script>"
            "</body></html>"
        )
        return DashboardPage(title="Madup Infra Manager", html=html)

    def _planning_unavailable(
        self,
        *,
        principal: AuthenticatedPrincipal,
        action: str,
    ) -> dict[str, Any]:
        return {
            "action": action,
            "scope": "admin" if principal.role is UserRole.ADMIN else "user",
            "status": "planning_unavailable",
            "reason_code": "immutable_source_snapshot_required",
            "message": (
                "Reviewed immutable source acquisition is not wired into this slice."
            ),
            "missing": [
                "selected_repository_candidate",
                "reviewed_snapshot_bytes",
                "resolved_commit_sha",
            ],
        }

    def _inventory(self, *, owner_id: UserId) -> ResourceInventory:
        workloads = self._store.list_workloads(owner_id=owner_id)
        schedules = self._store.list_schedules(owner_id=owner_id)
        secrets = self._store.list_secret_metadata(owner_id=owner_id)
        active_services = sum(
            1 for item in workloads if item.state is not WorkloadState.ARCHIVED
        )
        active_schedules = sum(
            1 for item in schedules if item.state.value != "archived"
        )
        active_secrets = sum(
            1
            for item in secrets
            if item.lifecycle_state is not SecretLifecycleState.DESTROYED
        )
        return ResourceInventory(
            active_services=active_services,
            active_schedules=active_schedules,
            active_secrets=active_secrets,
        )

    def _maintenance_jobs(self, *, now: datetime) -> list[dict[str, object]]:
        current = {
            status.job_name: status
            for status in self._store.list_maintenance_job_statuses()
        }
        rows: list[dict[str, object]] = []
        for job_name in _MAINTENANCE_JOB_ORDER:
            status = current.get(job_name)
            if status is None:
                rows.append(
                    {
                        "job_name": job_name,
                        "outcome": "missing",
                        "started_at": None,
                        "finished_at": None,
                        "summary": {},
                        "failure_code": None,
                        "failure_class": None,
                        "stale": True,
                        "version": 0,
                    }
                )
                continue
            reference_at = status.finished_at or status.started_at
            rows.append(
                {
                    "job_name": status.job_name,
                    "outcome": status.outcome,
                    "started_at": status.started_at.isoformat(),
                    "finished_at": None
                    if status.finished_at is None
                    else status.finished_at.isoformat(),
                    "summary": {key: value for key, value in status.summary},
                    "failure_code": status.failure_code,
                    "failure_class": status.failure_class,
                    "stale": (
                        now - reference_at
                    ) > _MAINTENANCE_STALE_WINDOWS[job_name],
                    "version": status.version,
                }
            )
        return rows

    @staticmethod
    def _workload_health(workloads: tuple[Any, ...]) -> str:
        if any(
            workload.state in {WorkloadState.FAILED, WorkloadState.QUARANTINED}
            for workload in workloads
        ):
            return "attention"
        if any(workload.state is WorkloadState.PAUSED for workload in workloads):
            return "paused"
        if any(workload.state is WorkloadState.ACTIVE for workload in workloads):
            return "healthy"
        return "idle"

    @staticmethod
    def _schedule_health(schedules: tuple[Any, ...]) -> str:
        if not schedules:
            return "none"
        if any(
            schedule.state.value == "quarantined" or schedule.consecutive_failures > 0
            for schedule in schedules
        ):
            return "attention"
        return "healthy"

    def _scoped_operation(
        self,
        *,
        principal: AuthenticatedPrincipal,
        operation_id: OperationId,
    ) -> Operation:
        try:
            operation = self._store.get_operation(operation_id)
        except NotFound as exc:
            raise ReadNotFound("Operation was not found.") from exc
        try:
            if operation.actor_id == AUTO_DEPLOY_ACTOR_ID:
                if operation.workload_id is None:
                    raise ReadNotFound("Operation was not found.")
                workload = self._store.get_workload(operation.workload_id)
                require_owner_or_admin(principal, workload.owner_id)
            else:
                require_owner_or_admin(principal, operation.actor_id)
        except (AccessDenied, NotFound) as exc:
            raise ReadNotFound("Operation was not found.") from exc
        return operation

    def _serialize_metrics(self, summary: Any) -> dict[str, object]:
        return {
            "active_users_24h": summary.active_users_24h,
            "active_users_7d": summary.active_users_7d,
            "active_users_30d": summary.active_users_30d,
            "dashboard_visits_30d": summary.dashboard_visits_30d,
            "mcp_actions_30d": summary.mcp_actions_30d,
            "failures_30d": summary.failures_30d,
            "denials_30d": summary.denials_30d,
        }

    def _serialize_workload(
        self,
        workload: Any,
        *,
        include_operational_details: bool,
    ) -> dict[str, object]:
        latest_operation = None
        if include_operational_details:
            latest_operation = self._store.get_latest_workload_operation(
                owner_id=workload.owner_id,
                workload_id=workload.id,
            )
        payload: dict[str, object] = {
            "id": str(workload.id),
            "resource_id": workload_resource_id(str(workload.id)),
            "name": _sanitize_text(workload.name),
            "kind": workload.kind.value,
            "state": workload.state.value,
            "latest_operation_state": None
            if latest_operation is None
            else latest_operation.state.value,
            "latest_operation_failure_code": None
            if latest_operation is None or latest_operation.sanitized_failure is None
            else _project_operation_failure_code(latest_operation.sanitized_failure),
            "last_healthy_state": self._last_healthy_state(workload),
            "last_healthy_digest_status": self._last_healthy_digest_status(workload),
            "last_activity_at": None
            if workload.last_activity_at is None
            else workload.last_activity_at.isoformat(),
            "operational_details_available": include_operational_details,
        }
        if include_operational_details:
            payload.update(self._safe_binding_projection(workload))
        return payload

    @staticmethod
    def _last_healthy_state(workload: Any) -> str:
        if _has_recorded_healthy_digest(workload.last_healthy_image_digest):
            return "healthy"
        return "unavailable"

    @staticmethod
    def _last_healthy_digest_status(workload: Any) -> str:
        if _has_recorded_healthy_digest(workload.last_healthy_image_digest):
            return "recorded"
        return "missing"

    def _safe_binding_projection(self, workload: Any) -> dict[str, object]:
        try:
            expected_host = build_app_hostname(str(workload.name), str(workload.id))
            binding = self._store.get_app_hostname_binding(expected_host)
        except (NotFound, ValueError):
            return {}
        if (
            str(binding.public_host) != expected_host
            or binding.workload_id != workload.id
            or binding.owner_id != workload.owner_id
            or binding.workload_kind != workload.kind
        ):
            return {}
        payload: dict[str, object] = {
            "public_host": binding.public_host,
            "public_binding_state": binding.state.value,
        }
        if binding.state.value == "active":
            payload["public_url"] = f"https://{binding.public_host}"
        return payload

    def _serialize_schedule(self, schedule: Any) -> dict[str, object]:
        return {
            "id": str(schedule.id),
            "workload_id": str(schedule.workload_id),
            "cron": _sanitize_text(schedule.cron),
            "timezone": _sanitize_text(schedule.timezone),
            "state": schedule.state.value,
            "consecutive_failures": schedule.consecutive_failures,
            "last_attempt_at": None
            if schedule.last_attempt_at is None
            else schedule.last_attempt_at.isoformat(),
            "last_success_at": None
            if schedule.last_success_at is None
            else schedule.last_success_at.isoformat(),
        }

    def _serialize_secret(self, secret: Any) -> dict[str, object]:
        return {
            "id": str(secret.id),
            "name": _sanitize_text(secret.name),
            "integration_type": _sanitize_text(secret.integration_type),
            "attached_workload_ids": [
                str(item) for item in secret.attached_workload_ids
            ],
            "active_version": secret.active_version,
            "rotation_state": secret.rotation_state.value,
            "lifecycle_state": secret.lifecycle_state.value,
        }

    def _table_html(
        self,
        title: str,
        headers: tuple[str, ...],
        rows: Iterable[tuple[object, ...]],
    ) -> str:
        rendered_rows = "".join(
            "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        rendered_headers = "".join(f"<th>{escape(header)}</th>" for header in headers)
        return (
            "<section class='panel'>"
            f"<h2>{escape(title)}</h2>"
            "<table><thead><tr>"
            f"{rendered_headers}"
            "</tr></thead><tbody>"
            f"{rendered_rows}"
            "</tbody></table></section>"
        )

    def _workloads_table_html(self, workloads: list[dict[str, Any]]) -> str:
        rows = "".join(
            "<tr>"
            f"<td>{escape(str(item['id']))}</td>"
            f"<td>{escape(str(item['name']))}</td>"
            f"<td>{escape(str(item['kind']))}</td>"
            f"<td>{escape(str(item['state']))}</td>"
            f"<td>{escape(str(item.get('latest_operation_state') or '-'))}</td>"
            f"<td>{escape(str(item.get('latest_operation_failure_code') or '-'))}</td>"
            f"<td>{escape(str(item.get('last_healthy_state', '-')))}</td>"
            f"<td>{escape(str(item.get('last_healthy_digest_status', '-')))}</td>"
            f"<td>{escape(str(item.get('last_activity_at') or '-'))}</td>"
            f"<td>{self._workload_host_html(item)}</td>"
            f"<td>{escape(str(item.get('public_binding_state', '-')))}</td>"
            "</tr>"
            for item in workloads
        )
        return (
            "<section class='panel'>"
            "<h2>Workloads</h2>"
            "<table><thead><tr>"
            "<th>ID</th><th>Name</th><th>Kind</th><th>State</th>"
            "<th>Latest Operation</th><th>Failure Code</th>"
            "<th>Last Healthy</th><th>Digest</th><th>Last Activity</th>"
            "<th>App Host</th><th>Binding</th>"
            "</tr></thead><tbody>"
            f"{rows}"
            "</tbody></table></section>"
        )

    @staticmethod
    def _workload_host_html(item: dict[str, Any]) -> str:
        public_host = item.get("public_host")
        if not public_host:
            return "-"
        if item.get("public_url"):
            public_url = escape(str(item["public_url"]), quote=True)
            return (
                f"<a href='{public_url}' target='_blank' rel='noreferrer'>"
                f"{escape(str(public_host))}</a>"
            )
        return escape(str(public_host))

    @staticmethod
    def _summary_text(summary: dict[str, int]) -> str:
        if not summary:
            return "-"
        return ", ".join(f"{key}={value}" for key, value in summary.items())

    @staticmethod
    def _inventory_text(inventory: dict[str, int]) -> str:
        return (
            f"services={inventory['active_services']}, "
            f"schedules={inventory['active_schedules']}, "
            f"secrets={inventory['active_secrets']}"
        )

    @staticmethod
    def _health_text(health: dict[str, str]) -> str:
        return (
            f"workloads={health['workloads']}, "
            f"schedules={health['schedules']}, "
            f"lifecycle={health['lifecycle_state']}"
        )


def _sanitize_text(value: str) -> str:
    text = value
    for pattern in (
        _BEARER_PATTERN,
        _COOKIE_PATTERN,
        _IPV4_PATTERN,
        _IPV6_PATTERN,
        _USER_AGENT_PATTERN,
        _SECRET_TOKEN_PATTERN,
        _PROMPT_PATTERN,
    ):
        text = pattern.sub("[REDACTED]", text)
    return text.replace(PUBLIC_ORIGIN, "/")


def _project_operation_failure_code(value: str) -> str:
    if value in _APPROVED_OPERATION_FAILURE_CODES:
        return value
    return _GENERIC_OPERATION_FAILURE_CODE


def _has_recorded_healthy_digest(value: object) -> bool:
    return type(value) is str and _RECORDED_SHA256_PATTERN.fullmatch(value) is not None
