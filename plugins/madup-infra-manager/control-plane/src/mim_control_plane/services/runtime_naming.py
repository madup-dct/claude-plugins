"""Authoritative names and IAM members for managed Cloud Run workloads."""

from __future__ import annotations

import hashlib
import re

from mim_control_plane.config import COMPANY_DOMAIN

_COMPANY_MEMBER_PATTERN = re.compile(
    rf"^(user|group):[a-z0-9._%+-]+@{re.escape(COMPANY_DOMAIN)}$"
)
_SECRET_ID_PATTERN = re.compile(r"^sec-[0-9a-f]{20}$")


def workload_resource_suffix(workload_id: str) -> str:
    if type(workload_id) is not str or not workload_id.strip():
        raise ValueError("workload_id must be exact.")
    return hashlib.sha256(workload_id.encode("utf-8")).hexdigest()[:12]


def cloud_run_parent(*, project_id: str, region: str) -> str:
    return f"projects/{project_id}/locations/{region}"


def cloud_run_service_name(*, project_id: str, region: str, workload_id: str) -> str:
    return (
        f"{cloud_run_parent(project_id=project_id, region=region)}/services/"
        f"mim-svc-{workload_resource_suffix(workload_id)}"
    )


def cloud_run_job_name(*, project_id: str, region: str, workload_id: str) -> str:
    return (
        f"{cloud_run_parent(project_id=project_id, region=region)}/jobs/"
        f"mim-job-{workload_resource_suffix(workload_id)}"
    )


def provider_secret_id(secret_id: str) -> str:
    if type(secret_id) is not str or _SECRET_ID_PATTERN.fullmatch(secret_id) is None:
        raise ValueError("secret_id must be exact.")
    return f"mim-{secret_id}"


def app_gateway_invoker_member(project_id: str) -> str:
    if type(project_id) is not str or not project_id.strip():
        raise ValueError("project_id must be exact.")
    return f"serviceAccount:mim-app-gateway@{project_id}.iam.gserviceaccount.com"


def schedule_gateway_invoker_member(project_id: str) -> str:
    if type(project_id) is not str or not project_id.strip():
        raise ValueError("project_id must be exact.")
    return (
        "serviceAccount:"
        f"mim-schedule-gateway@{project_id}.iam.gserviceaccount.com"
    )


def normalize_reviewed_breakglass_members(
    members: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    if isinstance(members, str):
        raise ValueError("breakglass members must be a sequence.")
    normalized: list[str] = []
    seen: set[str] = set()
    for member in members:
        if type(member) is not str:
            raise ValueError("breakglass members must be exact text.")
        lowered = member.casefold().strip()
        if (
            member != member.strip()
            or _COMPANY_MEMBER_PATTERN.fullmatch(lowered) is None
        ):
            raise ValueError("breakglass members must be exact company principals.")
        if lowered in seen:
            raise ValueError("breakglass members must be unique.")
        seen.add(lowered)
        normalized.append(lowered)
    return tuple(sorted(normalized))
