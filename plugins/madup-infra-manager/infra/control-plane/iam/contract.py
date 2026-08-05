#!/usr/bin/env python3
"""Canonical central IAM contract for MIM control-plane shell workflows."""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

REGION = "asia-northeast3"
DATASET_ID = "mim_billing_export"
SECRET_PREFIX = "mim-sec-"
WORKLOAD_ACCOUNT_PREFIXES = ("mim-wrk-", "mim-workload-")


def _project_id() -> str:
    value = os.environ.get("MIM_PROJECT_ID", "")
    if not value:
        raise SystemExit("MIM_PROJECT_ID is required")
    return value


def _operator_email() -> str:
    value = os.environ.get("MIM_OPERATOR_EMAIL", "")
    if not value:
        raise SystemExit("MIM_OPERATOR_EMAIL is required")
    return value


def _project_number() -> str:
    value = os.environ.get("MIM_PROJECT_NUMBER", "")
    if not re.fullmatch(r"[0-9]+", value):
        raise SystemExit("MIM_PROJECT_NUMBER is required")
    return value


def _service_account(name: str) -> str:
    project_id = _project_id()
    return f"serviceAccount:{name}@{project_id}.iam.gserviceaccount.com"


def _service_account_email(name: str) -> str:
    project_id = _project_id()
    return f"{name}@{project_id}.iam.gserviceaccount.com"


def _service_account_members(*names: str) -> list[str]:
    return [_service_account(name) for name in names]


def _secret_resource(secret_id: str) -> str:
    return f"projects/{_project_id()}/secrets/{secret_id}"


def _condition(title: str, expression: str) -> dict[str, str]:
    return {"title": title, "expression": expression}


def _run_condition() -> dict[str, str]:
    project_id = _project_id()
    expression = (
        f'resource.name.startsWith("projects/{project_id}/locations/{REGION}/services/mim-svc-") || '
        f'resource.name.startsWith("projects/{project_id}/locations/{REGION}/jobs/mim-job-")'
    )
    return _condition("mim-dynamic-run", expression)


def _job_condition() -> dict[str, str]:
    project_id = _project_id()
    expression = (
        f'resource.name.startsWith("projects/{project_id}/locations/{REGION}/jobs/mim-job-")'
    )
    return _condition("mim-dynamic-jobs", expression)


def _secret_condition() -> dict[str, str]:
    project_id = _project_id()
    expression = (
        f'resource.name.startsWith("projects/{project_id}/secrets/{SECRET_PREFIX}")'
    )
    return _condition("mim-managed-secrets", expression)


def _workload_service_account_condition() -> dict[str, str]:
    project_id = _project_id()
    expression = (
        'resource.type == "iam.googleapis.com/ServiceAccount" && '
        f'resource.name.startsWith("projects/{project_id}/serviceAccounts/mim-wrk-")'
    )
    return _condition("mim-workload-service-accounts", expression)


def _release_run_condition() -> dict[str, str]:
    project_id = _project_id()
    expression = (
        f'resource.name == "projects/{project_id}/locations/{REGION}/services/mim-control-plane" || '
        f'resource.name == "projects/{project_id}/locations/{REGION}/services/mim-app-gateway" || '
        f'resource.name == "projects/{project_id}/locations/{REGION}/services/mim-deploy-worker" || '
        f'resource.name == "projects/{project_id}/locations/{REGION}/services/mim-schedule-gateway" || '
        f'resource.name == "projects/{project_id}/locations/{REGION}/jobs/mim-identity-sync" || '
        f'resource.name == "projects/{project_id}/locations/{REGION}/jobs/mim-lifecycle" || '
        f'resource.name == "projects/{project_id}/locations/{REGION}/jobs/mim-usage-ingest"'
    )
    return _condition("mim-release-runtimes", expression)


def _maintenance_fixed_jobs_condition() -> dict[str, str]:
    project_id = _project_id()
    expression = (
        f'resource.name == "projects/{project_id}/locations/{REGION}/jobs/mim-identity-sync" || '
        f'resource.name == "projects/{project_id}/locations/{REGION}/jobs/mim-lifecycle" || '
        f'resource.name == "projects/{project_id}/locations/{REGION}/jobs/mim-usage-ingest"'
    )
    return _condition("mim-fixed-maintenance-jobs", expression)


def _fixed_secret_specs() -> list[dict[str, object]]:
    return [
        {
            "secret_id": "mim-runtime-bootstrap",
            "optional": False,
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": _service_account_members(
                        "mim-control-plane",
                        "mim-deploy-worker",
                        "mim-schedule-gateway",
                        "mim-maintenance",
                    ),
                },
                {
                    "role": "roles/secretmanager.secretVersionAdder",
                    "members": _service_account_members("mim-release"),
                },
            ],
        },
        {
            "secret_id": "mim-edge-origin-v1",
            "optional": False,
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": _service_account_members("mim-control-plane"),
                }
            ],
        },
        {
            "secret_id": "mim-app-gateway-origin-v1",
            "optional": False,
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": _service_account_members("mim-app-gateway"),
                }
            ],
        },
        {
            "secret_id": "mim-app-gateway-origin-v0",
            "optional": True,
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": _service_account_members("mim-app-gateway"),
                }
            ],
        },
        {
            "secret_id": "mim-desired-state-signing",
            "optional": False,
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": _service_account_members(
                        "mim-control-plane",
                        "mim-deploy-worker",
                    ),
                }
            ],
        },
        {
            "secret_id": "mim-github-webhook",
            "optional": False,
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": _service_account_members("mim-control-plane"),
                }
            ],
        },
        {
            "secret_id": "mim-github-app-key",
            "optional": False,
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": _service_account_members(
                        "mim-control-plane",
                        "mim-deploy-worker",
                    ),
                }
            ],
        },
    ]


def expected_contract() -> dict[str, object]:
    operator = _operator_email()
    secret_specs = _fixed_secret_specs()
    contract = {
        "managed_identities": {
            "control_plane": _service_account_email("mim-control-plane"),
            "app_gateway": _service_account_email("mim-app-gateway"),
            "deploy_worker": _service_account_email("mim-deploy-worker"),
            "build": _service_account_email("mim-build"),
            "schedule_gateway": _service_account_email("mim-schedule-gateway"),
            "maintenance": _service_account_email("mim-maintenance"),
            "identity_sync": _service_account_email("mim-identity-sync"),
            "release": _service_account_email("mim-release"),
        },
        "project_bindings": [
            {
                "role": "roles/datastore.user",
                "members": [
                    _service_account("mim-control-plane"),
                    _service_account("mim-deploy-worker"),
                    _service_account("mim-schedule-gateway"),
                    _service_account("mim-maintenance"),
                ],
            },
            {
                "role": "roles/cloudscheduler.admin",
                "members": [_service_account("mim-control-plane")],
            },
            {
                "role": "roles/cloudbuild.builds.editor",
                "members": [_service_account("mim-deploy-worker")],
            },
            {
                "role": "roles/iam.serviceAccountCreator",
                "members": [_service_account("mim-deploy-worker")],
            },
            {
                "role": "roles/iam.serviceAccountAdmin",
                "members": [_service_account("mim-deploy-worker")],
                "condition": _workload_service_account_condition(),
            },
            {
                "role": "roles/iam.securityReviewer",
                "members": [_service_account("mim-deploy-worker")],
                "condition": _workload_service_account_condition(),
            },
            {
                "role": "roles/run.admin",
                "members": [_service_account("mim-deploy-worker")],
                "condition": _run_condition(),
            },
            {
                "role": "roles/run.viewer",
                "members": [_service_account("mim-schedule-gateway")],
                "condition": _job_condition(),
            },
            {
                "role": "roles/run.jobsExecutorWithOverrides",
                "members": [_service_account("mim-schedule-gateway")],
                "condition": _job_condition(),
            },
            {
                "role": "roles/secretmanager.admin",
                "members": [_service_account("mim-control-plane")],
                "condition": _secret_condition(),
            },
            {
                "role": "roles/bigquery.jobUser",
                "members": [_service_account("mim-maintenance")],
            },
            {
                "role": "roles/run.jobsExecutor",
                "members": [_service_account("mim-maintenance")],
                "condition": _maintenance_fixed_jobs_condition(),
            },
            {
                "role": "roles/run.admin",
                "members": [_service_account("mim-release")],
                "condition": _release_run_condition(),
            },
            {
                "role": "roles/cloudscheduler.admin",
                "members": [_service_account("mim-release")],
            },
        ],
        "artifact_repository_bindings": [
            {
                "repository": "mim",
                "role": "roles/artifactregistry.writer",
                "members": [
                    _service_account("mim-deploy-worker"),
                    _service_account("mim-build"),
                ],
            }
        ],
        "secret_resource_bindings": {
            _secret_resource(spec["secret_id"]): spec["bindings"] for spec in secret_specs
        },
        "optional_secret_resources": [
            _secret_resource(spec["secret_id"])
            for spec in secret_specs
            if bool(spec["optional"])
        ],
        "service_account_bindings": {
            _service_account_email("mim-app-gateway"): [
                {
                    "role": "roles/iam.serviceAccountUser",
                    "members": [_service_account("mim-release")],
                }
            ],
            _service_account_email("mim-control-plane"): [
                {
                    "role": "roles/iam.serviceAccountUser",
                    "members": [_service_account("mim-release")],
                }
            ],
            _service_account_email("mim-build"): [
                {
                    "role": "roles/iam.serviceAccountUser",
                    "members": [_service_account("mim-deploy-worker")],
                }
            ],
            _service_account_email("mim-deploy-worker"): [
                {
                    "role": "roles/iam.serviceAccountUser",
                    "members": [_service_account("mim-release")],
                }
            ],
            _service_account_email("mim-identity-sync"): [
                {
                    "role": "roles/iam.serviceAccountTokenCreator",
                    "members": [_service_account("mim-maintenance")],
                }
            ],
            _service_account_email("mim-maintenance"): [
                {
                    "role": "roles/iam.serviceAccountUser",
                    "members": [_service_account("mim-release")],
                }
            ],
            _service_account_email("mim-release"): [
                {
                    "role": "roles/iam.serviceAccountTokenCreator",
                    "members": [f"user:{operator}"],
                }
            ],
            _service_account_email("mim-schedule-gateway"): [
                {
                    "role": "roles/iam.serviceAccountUser",
                    "members": [
                        _service_account("mim-control-plane"),
                        _service_account("mim-release"),
                    ],
                }
            ],
        },
        "limitations": [
            "roles/iam.serviceAccountCreator only grants create/get/list and does not safely express a workload-name create-time IAM condition in this shell contract.",
            "Cloud Scheduler is not documented as supporting resource.name IAM Conditions for Scheduler job resources in this use case. The shell contract therefore keeps roles/cloudscheduler.admin project-scoped in the dedicated central MIM project and relies on deterministic runtime names plus strict audit as compensating controls.",
            "The conditioned deploy-worker roles iam.serviceAccountAdmin and iam.securityReviewer narrow post-create management to mim-wrk-* service-account resources; creation itself still requires the separate unconditioned iam.serviceAccountCreator role on the project parent.",
            "Raw BigQuery billing export access is forbidden in this contract. Maintenance must not read mim_billing_export directly; a separate isolated sanitized mart/view is required before any BigQuery reader grant is added.",
        ],
        "forbidden_project_roles": [
            "roles/run.invoker",
            "roles/iap.httpsResourceAccessor",
            "roles/owner",
            "roles/editor",
            "roles/viewer",
            "roles/bigquery.admin",
            "roles/bigquery.dataEditor",
            "roles/bigquery.dataOwner",
            "roles/bigquery.dataViewer",
        ],
    }
    return contract


def _load_json(path_env: str) -> dict[str, object]:
    path = os.environ[path_env]
    return _load_json_path(path)


def _load_json_path(path: str) -> dict[str, object]:
    text = Path(path).read_text()
    decoder = json.JSONDecoder()
    data, _ = decoder.raw_decode((text or "{}").lstrip())
    if not isinstance(data, dict):
        raise ValueError(f"{path_env} must contain a JSON object")
    return data


def _condition_key(binding: dict[str, object]) -> tuple[str | None, str | None]:
    condition = binding.get("condition")
    if not isinstance(condition, dict):
        return (None, None)
    title = condition.get("title")
    expression = condition.get("expression")
    return (
        title if isinstance(title, str) else None,
        expression if isinstance(expression, str) else None,
    )


def _binding_key(binding: dict[str, object]) -> tuple[str, str | None, str | None]:
    role = binding.get("role")
    if not isinstance(role, str):
        raise ValueError("binding role must be text")
    title, expression = _condition_key(binding)
    return (role, title, expression)


def _aggregate_bindings(policy: dict[str, object]) -> dict[tuple[str, str | None, str | None], set[str]]:
    aggregated: dict[tuple[str, str | None, str | None], set[str]] = defaultdict(set)
    for binding in policy.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        key = _binding_key(binding)
        members = binding.get("members", [])
        if not isinstance(members, list):
            continue
        for member in members:
            if isinstance(member, str):
                aggregated[key].add(member)
    return aggregated


def _normalize_expected(bindings: list[dict[str, object]]) -> dict[tuple[str, str | None, str | None], set[str]]:
    expected: dict[tuple[str, str | None, str | None], set[str]] = defaultdict(set)
    for binding in bindings:
        expected[_binding_key(binding)].update(binding["members"])
    return expected


def _member_identity(member: str) -> str:
    if member.startswith("serviceAccount:"):
        return member.removeprefix("serviceAccount:").split("@", 1)[0]
    return member


def _allowed_service_agent(email: str) -> bool:
    project_number = _project_number()
    project_id = _project_id()
    allowed = {
        f"service-{project_number}@serverless-robot-prod.iam.gserviceaccount.com",
        f"{project_number}-compute@developer.gserviceaccount.com",
        f"{project_number}@cloudbuild.gserviceaccount.com",
        f"{project_number}@cloudservices.gserviceaccount.com",
        f"service-{project_number}@gs-project-accounts.iam.gserviceaccount.com",
        f"service-{project_number}@containerregistry.iam.gserviceaccount.com",
        f"{project_id}@appspot.gserviceaccount.com",
    }
    if email in allowed:
        return True
    return re.fullmatch(
        rf"service-{re.escape(project_number)}@gcp-sa-[a-z0-9-]+\.iam\.gserviceaccount\.com$",
        email,
    ) is not None


def _is_cross_project_service_account(member: str) -> bool:
    if not member.startswith("serviceAccount:"):
        return False
    email = member.removeprefix("serviceAccount:")
    if email.endswith(f"@{_project_id()}.iam.gserviceaccount.com"):
        return False
    return not _allowed_service_agent(email)


def _observed_identity_roles(aggregated: dict[tuple[str, str | None, str | None], set[str]]) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    managed = expected_contract()["managed_identities"]
    for identity_name, email in managed.items():
        member = f"serviceAccount:{email}"
        rows: list[dict[str, object]] = []
        for (role, title, expression), members in aggregated.items():
            if member in members:
                row: dict[str, object] = {"role": role}
                if title is not None and expression is not None:
                    row["condition"] = {"title": title, "expression": expression}
                rows.append(row)
        rows.sort(key=lambda item: item["role"])
        result[identity_name] = rows
    return result


def _observed_service_account_policies(policies: dict[str, dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for resource_email, policy in policies.items():
        rows: list[dict[str, object]] = []
        for (role, title, expression), members in sorted(_aggregate_bindings(policy).items()):
            item: dict[str, object] = {"role": role, "members": sorted(members)}
            if title is not None and expression is not None:
                item["condition"] = {"title": title, "expression": expression}
            rows.append(item)
        result[resource_email] = rows
    return result


def _observed_artifact_repository_bindings(
    repository_policies: dict[str, dict[str, object]]
) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for repository, policy in repository_policies.items():
        rows: list[dict[str, object]] = []
        for (role, title, expression), members in sorted(_aggregate_bindings(policy).items()):
            item: dict[str, object] = {"role": role, "members": sorted(members)}
            if title is not None and expression is not None:
                item["condition"] = {"title": title, "expression": expression}
            rows.append(item)
        result[repository] = rows
    return result


def _observed_secret_resource_bindings(
    secret_policies: dict[str, dict[str, object]]
) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for resource_name, policy in secret_policies.items():
        rows: list[dict[str, object]] = []
        for (role, title, expression), members in sorted(_aggregate_bindings(policy).items()):
            item: dict[str, object] = {"role": role, "members": sorted(members)}
            if title is not None and expression is not None:
                item["condition"] = {"title": title, "expression": expression}
            rows.append(item)
        result[resource_name] = rows
    return result


def _load_secret_policies() -> dict[str, dict[str, object]]:
    manifest_path = os.environ.get("MIM_IAM_SECRET_POLICIES_TSV_FILE")
    secret_policies: dict[str, dict[str, object]] = {}
    if manifest_path:
        for raw_line in Path(manifest_path).read_text().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            secret_id, _secret_mode, policy_path = line.split("\t", 2)
            secret_policies[_secret_resource(secret_id)] = _load_json_path(policy_path)
        return secret_policies

    legacy_env_map = {
        "MIM_IAM_BOOTSTRAP_SECRET_POLICY_FILE": _secret_resource("mim-runtime-bootstrap"),
        "MIM_IAM_APP_GATEWAY_PROOF_SECRET_POLICY_FILE": _secret_resource(
            "mim-app-gateway-origin-v1"
        ),
    }
    for path_env, resource_name in legacy_env_map.items():
        path = os.environ.get(path_env)
        if path:
            secret_policies[resource_name] = _load_json_path(path)
    return secret_policies


def _policy_is_absent(policy: dict[str, object] | None) -> bool:
    if policy is None:
        return True
    return policy == {}


def _bigquery_dataset_state(dataset_policy: dict[str, object], forbidden_member: str) -> dict[str, object]:
    dataset = (
        dataset_policy.get("datasetReference")
        if isinstance(dataset_policy.get("datasetReference"), dict)
        else {}
    )
    project_id = dataset.get("projectId")
    dataset_id = dataset.get("datasetId")
    accesses = dataset_policy.get("access")
    forbidden = False
    if project_id == _project_id() and dataset_id == DATASET_ID and isinstance(accesses, list):
        for entry in accesses:
            if not isinstance(entry, dict):
                continue
            role = entry.get("role")
            if role != "READER":
                continue
            if entry.get("iamMember") == forbidden_member:
                forbidden = True
            if entry.get("userByEmail") == forbidden_member.removeprefix("serviceAccount:"):
                forbidden = True
    return {
        "dataset": DATASET_ID,
        "status": "forbidden" if forbidden else "clear",
    }


def evaluate() -> dict[str, object]:
    contract = expected_contract()
    project_policy = _load_json("MIM_IAM_PROJECT_POLICY_FILE")
    build_policy = _load_json("MIM_IAM_BUILD_POLICY_FILE")
    control_plane_policy = _load_json("MIM_IAM_CONTROL_PLANE_POLICY_FILE")
    app_gateway_policy = _load_json("MIM_IAM_APP_GATEWAY_POLICY_FILE")
    deploy_worker_policy = _load_json("MIM_IAM_DEPLOY_WORKER_POLICY_FILE")
    identity_sync_policy = _load_json("MIM_IAM_IDENTITY_SYNC_POLICY_FILE")
    maintenance_policy = _load_json("MIM_IAM_MAINTENANCE_POLICY_FILE")
    release_policy = _load_json("MIM_IAM_RELEASE_POLICY_FILE")
    schedule_gateway_policy = _load_json("MIM_IAM_SCHEDULE_GATEWAY_POLICY_FILE")
    artifact_repository_policy = _load_json("MIM_IAM_ARTIFACT_REPOSITORY_POLICY_FILE")
    dataset_policy = _load_json("MIM_IAM_BILLING_DATASET_FILE")
    secret_policies = _load_secret_policies()

    aggregated_project = _aggregate_bindings(project_policy)
    blockers: list[dict[str, str]] = []
    actions: list[dict[str, str]] = []

    for (role, _title, _expression), members in aggregated_project.items():
        if role in contract["forbidden_project_roles"] and members:
            if role == "roles/iap.httpsResourceAccessor":
                blockers.append(
                    {
                        "code": "project-wide-iap-accessor",
                        "message": "Project-wide IAP access bindings are forbidden",
                    }
                )
            elif role == "roles/run.invoker":
                blockers.append(
                    {
                        "code": "project-wide-run-invoker",
                        "message": "Project-wide Cloud Run invoker bindings are forbidden",
                    }
                )

        for member in members:
            if member in {"allUsers", "allAuthenticatedUsers"}:
                blockers.append(
                    {
                        "code": "broad-project-member",
                        "message": "Public project IAM members are forbidden",
                    }
                )
            if _is_cross_project_service_account(member):
                blockers.append(
                    {
                        "code": "cross-project-service-account",
                        "message": "Cross-project service account bindings are forbidden",
                    }
                )

    expected_project = _normalize_expected(contract["project_bindings"])
    managed_members = {
        f"serviceAccount:{email}" for email in contract["managed_identities"].values()
    }

    for identity_name, email in contract["managed_identities"].items():
        member = f"serviceAccount:{email}"
        expected_keys = {
            key for key, members in expected_project.items() if member in members
        }
        observed_keys = {
            key for key, members in aggregated_project.items() if member in members
        }
        unexpected = observed_keys - expected_keys
        missing = expected_keys - observed_keys
        if not expected_keys and observed_keys:
            role = sorted(unexpected)[0][0]
            blockers.append(
                {
                    "code": f"{identity_name}-unexpected-project-role",
                    "message": f"Managed identity {email.split('@', 1)[0]} must not hold project role {role}",
                }
            )
            continue
        if unexpected:
            role = sorted(unexpected)[0][0]
            blockers.append(
                {
                    "code": f"{identity_name}-unexpected-project-role",
                    "message": f"Managed identity {email.split('@', 1)[0]} must not hold project role {role}",
                }
            )
        for role, title, expression in sorted(missing):
            action: dict[str, str] = {
                "kind": "bind_project_role",
                "name": role,
                "member": member,
            }
            if title is not None and expression is not None:
                action["condition_title"] = title
                action["condition_expression"] = expression
            actions.append(action)
        for key in observed_keys & expected_keys:
            observed_members = aggregated_project.get(key, set())
            expected_members = expected_project.get(key, set())
            if observed_members != expected_members:
                unexpected_managed_members = sorted(
                    member
                    for member in (observed_members - expected_members)
                    if member in managed_members
                )
                if unexpected_managed_members:
                    extra_member = unexpected_managed_members[0]
                    blockers.append(
                        {
                            "code": f"{_member_identity(extra_member)}-unexpected-project-role",
                            "message": (
                                "Managed identity "
                                f"{_member_identity(extra_member)} must not hold "
                                f"project role {key[0]}"
                            ),
                        }
                    )
                    continue
                blockers.append(
                    {
                        "code": f"{identity_name}-project-members-drift",
                        "message": f"Managed identity binding {key[0]} members drifted",
                    }
                )

    for (role, _title, _expression), members in aggregated_project.items():
        for member in members:
            if not member.startswith("serviceAccount:"):
                continue
            local = _member_identity(member)
            if local.startswith(WORKLOAD_ACCOUNT_PREFIXES):
                blockers.append(
                    {
                        "code": "workload-project-role-drift",
                        "message": f"Managed workload identity {local} must not hold project role {role}",
                    }
                )

    sa_policies = {
        _service_account_email("mim-control-plane"): control_plane_policy,
        _service_account_email("mim-app-gateway"): app_gateway_policy,
        _service_account_email("mim-build"): build_policy,
        _service_account_email("mim-deploy-worker"): deploy_worker_policy,
        _service_account_email("mim-identity-sync"): identity_sync_policy,
        _service_account_email("mim-maintenance"): maintenance_policy,
        _service_account_email("mim-release"): release_policy,
        _service_account_email("mim-schedule-gateway"): schedule_gateway_policy,
    }
    for resource_email, expected_bindings in contract["service_account_bindings"].items():
        observed = _aggregate_bindings(sa_policies[resource_email])
        expected = _normalize_expected(expected_bindings)
        for (_role, _title, _expression), members in observed.items():
            for member in members:
                if member in {"allUsers", "allAuthenticatedUsers"}:
                    blockers.append(
                        {
                            "code": "broad-service-account-member",
                            "message": "Public service-account IAM members are forbidden",
                        }
                    )
                if _is_cross_project_service_account(member):
                    blockers.append(
                        {
                            "code": "cross-project-service-account",
                            "message": "Cross-project service account bindings are forbidden",
                        }
                    )
        unexpected = set(observed) - set(expected)
        missing = set(expected) - set(observed)
        if unexpected:
            role = sorted(unexpected)[0][0]
            blockers.append(
                {
                    "code": f"{resource_email}-service-account-drift",
                    "message": f"Managed identity {resource_email.split('@', 1)[0]} has unexpected IAM role {role}",
                }
            )
        for role, _title, _expression in sorted(missing):
            for member in sorted(expected[(role, _title, _expression)]):
                action = {
                    "kind": "bind_service_account_role",
                    "name": role,
                    "member": member,
                    "value": resource_email,
                }
                actions.append(action)
        for key in set(observed) & set(expected):
            if observed[key] != expected[key]:
                blockers.append(
                    {
                        "code": f"{resource_email}-service-account-members-drift",
                        "message": f"Managed identity {resource_email.split('@', 1)[0]} binding {key[0]} members drifted",
                    }
                )

    dataset_state = _bigquery_dataset_state(
        dataset_policy,
        forbidden_member=_service_account("mim-maintenance"),
    )
    if dataset_state["status"] == "forbidden":
        blockers.append(
            {
                "code": "raw-billing-export-access-forbidden",
                "message": (
                    "Maintenance must not hold raw mim_billing_export access; "
                    "use a dedicated billing-account export or isolated sanitized mart/view instead"
                ),
            }
        )

    repository_policies = {"mim": artifact_repository_policy}
    for item in contract["artifact_repository_bindings"]:
        repository = item["repository"]
        observed_repo = _aggregate_bindings(repository_policies[repository])
        expected_repo = _normalize_expected([item])
        unexpected = set(observed_repo) - set(expected_repo)
        missing = set(expected_repo) - set(observed_repo)
        if unexpected:
            role = sorted(unexpected)[0][0]
            blockers.append(
                {
                    "code": f"{repository}-artifact-repository-drift",
                    "message": f"Artifact Registry repository {repository} has unexpected IAM role {role}",
                }
            )
        for role, title, expression in sorted(missing):
            for member in sorted(expected_repo[(role, title, expression)]):
                actions.append(
                    {
                        "kind": "bind_artifact_repository_role",
                        "name": role,
                        "member": member,
                        "value": repository,
                    }
                )
        for key in set(observed_repo) & set(expected_repo):
            if observed_repo[key] != expected_repo[key]:
                blockers.append(
                    {
                        "code": f"{repository}-artifact-repository-members-drift",
                        "message": f"Artifact Registry repository {repository} binding {key[0]} members drifted",
                    }
                )

    optional_secret_resources = set(contract["optional_secret_resources"])
    for resource_name, expected_bindings in contract["secret_resource_bindings"].items():
        secret_policy = secret_policies.get(resource_name)
        if resource_name in optional_secret_resources and _policy_is_absent(secret_policy):
            continue
        observed_secret = _aggregate_bindings(secret_policy or {})
        expected_secret = _normalize_expected(expected_bindings)
        unexpected = set(observed_secret) - set(expected_secret)
        missing = set(expected_secret) - set(observed_secret)
        if unexpected:
            role = sorted(unexpected)[0][0]
            blockers.append(
                {
                    "code": f"{resource_name}-secret-resource-drift",
                    "message": f"Secret resource {resource_name} has unexpected IAM role {role}",
                }
            )
        for role, title, expression in sorted(missing):
            for member in sorted(expected_secret[(role, title, expression)]):
                actions.append(
                    {
                        "kind": "bind_secret_resource_role",
                        "name": role,
                        "member": member,
                        "value": resource_name,
                    }
                )
        for key in set(observed_secret) & set(expected_secret):
            if observed_secret[key] != expected_secret[key]:
                blockers.append(
                    {
                        "code": f"{resource_name}-secret-resource-members-drift",
                        "message": f"Secret resource {resource_name} binding {key[0]} members drifted",
                    }
                )

    observed = {
        "project": _observed_identity_roles(aggregated_project),
        "artifact_repository": _observed_artifact_repository_bindings(repository_policies),
        "secret_resources": _observed_secret_resource_bindings(secret_policies),
        "service_accounts": _observed_service_account_policies(sa_policies),
        "billing_export_dataset": dataset_state,
    }
    return {
        "status": "blocked" if blockers else "ready",
        "blockers": blockers,
        "actions": actions,
        "contract": contract,
        "observed": observed,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] != "evaluate":
        print("usage: contract.py evaluate", file=sys.stderr)
        return 2
    json.dump(evaluate(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
