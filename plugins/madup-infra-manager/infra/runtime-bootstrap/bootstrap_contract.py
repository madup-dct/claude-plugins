#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

CENTRAL_PROJECT_ID = "mim-prod-123456"
FIXED_REGION = "asia-northeast3"
COMPANY_DOMAIN = "madup.com"
GITHUB_OWNER = "madupmarketing"
BOOTSTRAP_SECRET_NAME = "mim-runtime-bootstrap"
MAX_BOOTSTRAP_BYTES = 64 * 1024
TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "project_id",
        "project_number",
        "organization_id",
        "billing_account_id",
        "operator_email",
        "cloudflare_issuer",
        "cloudflare_audience",
        "app_cloudflare_issuer",
        "app_cloudflare_audience",
        "public_host_suffix",
        "region",
        "directory_required_group_email",
        "admin_members",
        "breakglass_members",
        "directory",
        "slack",
        "origin_hmac_keys",
        "app_origin_hmac_keys",
        "desired_state_signing_key_id",
        "desired_state_signing_secret_version",
        "github_webhook_secret_version",
        "github_app",
        "build",
        "deploy_worker",
        "app_gateway",
        "app_authorization",
        "schedule_gateway",
    }
)
OPTIONAL_TOP_LEVEL_KEYS = frozenset({"breakglass_members", "slack"})
ORIGIN_KEY_FIELDS = frozenset({"key_id", "secret_version"})
SLACK_FIELDS = frozenset({"required_scopes"})
GITHUB_APP_FIELDS = frozenset(
    {
        "app_id",
        "private_key_secret_version",
        "installation_id",
        "allowed_repository_ids",
        "bindings",
    }
)
GITHUB_BINDING_FIELDS = frozenset(
    {
        "repository_numeric_id",
        "owner",
        "name",
        "installation_id",
        "repository_resource",
    }
)
BUILD_FIELDS = frozenset({"builder_image", "build_service_account"})
MACHINE_SERVICE_FIELDS = frozenset({"url", "audience", "service_account_email"})
DIRECTORY_FIELDS = frozenset({"admin_subject", "service_account_email"})
PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
ORGANIZATION_ID_PATTERN = re.compile(r"^[1-9][0-9]{5,19}$")
BILLING_ACCOUNT_PATTERN = re.compile(r"^[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$")
OPERATOR_EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@madup\.com$")
CLOUDFLARE_AUDIENCE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
SERVICE_ACCOUNT_EMAIL_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@"
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
)
SECRET_REF_PATTERN = re.compile(
    r"^projects/(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])/secrets/"
    r"(?P<secret>[a-z][a-z0-9-]{1,127})/versions/(?P<version>[1-9][0-9]*)$"
)
DIGEST_IMAGE_PATTERN = re.compile(
    r"^"
    + re.escape(FIXED_REGION)
    + r"-docker\.pkg\.dev/"
    + re.escape(CENTRAL_PROJECT_ID)
    + r"/(?P<repository>[a-z0-9._-]+)/(?P<image>[A-Za-z0-9._-]+)"
    + r"@sha256:[0-9a-f]{64}$"
)
SECRET_VERSION_NAME_PATTERN = re.compile(
    r"^projects/"
    + re.escape(CENTRAL_PROJECT_ID)
    + r"/secrets/"
    + re.escape(BOOTSTRAP_SECRET_NAME)
    + r"/versions/(?P<version>[1-9][0-9]*)$"
)


class ContractError(ValueError):
    pass


def fail(message: str) -> None:
    raise ContractError(message)


def load_unique_json(path: Path) -> Any:
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_BOOTSTRAP_BYTES:
        fail("Runtime bootstrap payload is invalid.")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except UnicodeDecodeError as exc:
        raise ContractError("Runtime bootstrap payload is invalid.") from exc
    except json.JSONDecodeError as exc:
        raise ContractError("Runtime bootstrap payload is invalid.") from exc


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("Runtime bootstrap contains duplicate JSON keys.")
        result[key] = value
    return result


def require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail(f"{field_name} must be a JSON object.")
    return value


def require_exact_mapping_keys(
    mapping: Mapping[str, Any],
    *,
    expected: frozenset[str],
    field_name: str,
) -> None:
    keys = frozenset(mapping.keys())
    if keys != expected:
        unexpected = sorted(keys - expected)
        missing = sorted(expected - keys)
        details = ", ".join(unexpected + missing)
        fail(f"{field_name} contains unsupported keys: {details}")


def require_text(value: Any, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        fail(f"{field_name} must be exact non-empty text.")
    return value.strip()


def require_identifier(value: Any, field_name: str) -> str:
    text = require_text(value, field_name)
    if any(char.isspace() for char in text):
        fail(f"{field_name} must not contain whitespace.")
    return text


def require_numeric_string(value: Any, field_name: str) -> str:
    text = require_text(value, field_name)
    if not text.isdigit():
        fail(f"{field_name} must be numeric text.")
    return text


def require_project_number(value: Any) -> str:
    project_number = require_numeric_string(value, "project_number")
    if project_number.startswith("0"):
        fail("project_number must be a non-zero numeric string.")
    return project_number


def require_positive_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 1:
        fail(f"{field_name} must be a positive integer.")
    return value


def validate_project_id(value: Any) -> str:
    project_id = require_text(value, "project_id")
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        fail("project_id must be a valid GCP project ID.")
    if project_id != CENTRAL_PROJECT_ID:
        fail("project_id must match the central MIM project.")
    return project_id


def validate_organization_id(value: Any) -> str:
    organization_id = require_text(value, "organization_id")
    if not ORGANIZATION_ID_PATTERN.fullmatch(organization_id):
        fail("organization_id must be a canonical organization numeric string.")
    return organization_id


def validate_billing_account_id(value: Any) -> str:
    billing_account_id = require_text(value, "billing_account_id")
    if not BILLING_ACCOUNT_PATTERN.fullmatch(billing_account_id):
        fail("billing_account_id must be a canonical billing account ID.")
    return billing_account_id


def validate_operator_email(value: Any, *, field_name: str = "operator_email") -> str:
    operator_email = require_text(value, field_name).casefold()
    if not OPERATOR_EMAIL_PATTERN.fullmatch(operator_email):
        fail(f"{field_name} must be a @madup.com email.")
    return operator_email


def validate_directory_admin_subject(value: Any) -> str:
    admin_subject = require_text(value, "directory.admin_subject").casefold()
    if not OPERATOR_EMAIL_PATTERN.fullmatch(admin_subject):
        fail("directory.admin_subject must be a @madup.com email.")
    return admin_subject


def validate_directory_service_account_email(value: Any) -> str:
    service_account_email = require_text(
        value,
        "directory.service_account_email",
    ).casefold()
    if not SERVICE_ACCOUNT_EMAIL_PATTERN.fullmatch(service_account_email):
        fail("directory.service_account_email is invalid.")
    expected = f"mim-identity-sync@{CENTRAL_PROJECT_ID}.iam.gserviceaccount.com"
    if service_account_email != expected:
        fail("directory.service_account_email is invalid.")
    return service_account_email


def validate_directory_required_group_email(value: Any) -> str:
    group_email = require_text(value, "directory_required_group_email").casefold()
    if not group_email.endswith(f"@{COMPANY_DOMAIN}") or not group_email.startswith(
        "mim-"
    ):
        fail("directory_required_group_email must be a @madup.com group email.")
    return group_email


def validate_cloudflare_issuer(value: Any) -> str:
    issuer = require_text(value, "cloudflare_issuer")
    parsed = urlparse(issuer)
    if parsed.scheme != "https" or parsed.params or parsed.query or parsed.fragment:
        fail("cloudflare_issuer is invalid.")
    if parsed.hostname is None or not parsed.hostname.endswith(".cloudflareaccess.com"):
        fail("cloudflare_issuer is invalid.")
    if parsed.path not in ("", "/"):
        fail("cloudflare_issuer is invalid.")
    return f"https://{parsed.hostname}"


def validate_cloudflare_audience(value: Any) -> str:
    audience = require_text(value, "cloudflare_audience")
    if not CLOUDFLARE_AUDIENCE_PATTERN.fullmatch(audience):
        fail("cloudflare_audience is invalid.")
    return audience


def validate_public_host_suffix(value: Any) -> str:
    suffix = require_text(value, "public_host_suffix").casefold()
    if suffix != "madup.app":
        fail("public_host_suffix is invalid.")
    return suffix


def validate_region(value: Any) -> str:
    region = require_text(value, "region")
    if region != FIXED_REGION:
        fail("region is invalid.")
    return region


def require_company_member(value: Any, *, field_name: str) -> str:
    member = require_text(value, field_name).casefold()
    if not (member.startswith("user:") or member.startswith("group:")):
        fail(f"{field_name} must contain only @madup.com users/groups.")
    if not member.endswith(f"@{COMPANY_DOMAIN}"):
        fail(f"{field_name} must contain only @madup.com users/groups.")
    return member


def require_admin_members(value: Any, *, operator_email: str) -> list[str]:
    if not isinstance(value, list) or not value:
        fail("admin_members must be a non-empty list.")
    members = [require_company_member(item, field_name="admin_members") for item in value]
    if len(set(members)) != len(members):
        fail("admin_members must be unique.")
    if members != sorted(members):
        fail("admin_members must be sorted.")
    if f"user:{operator_email}" not in members:
        fail("admin_members must include the operator.")
    return members


def require_breakglass_members(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        fail("breakglass_members must contain only @madup.com users/groups.")
    members = [
        require_company_member(item, field_name="breakglass_members") for item in value
    ]
    if len(set(members)) != len(members):
        fail("breakglass_members must be unique.")
    if members != sorted(members):
        fail("breakglass_members must be sorted.")
    return members


def require_secret_version_ref(
    value: Any,
    *,
    field_name: str,
    project_id: str,
) -> str:
    text = require_text(value, field_name)
    match = SECRET_REF_PATTERN.fullmatch(text)
    if match is None or match.group("project") != project_id:
        fail(f"{field_name} must be a full numeric Secret Manager version.")
    return text


def require_origin_keys(
    value: Any,
    *,
    field_name: str,
    max_items: int | None = None,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        fail(f"{field_name} must be a non-empty list.")
    if max_items is not None and len(value) > max_items:
        fail(f"{field_name} must not contain more than {max_items} entries.")
    keys: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in value:
        mapping = require_mapping(item, field_name)
        require_exact_mapping_keys(
            mapping,
            expected=ORIGIN_KEY_FIELDS,
            field_name=field_name,
        )
        key_id = require_identifier(mapping.get("key_id"), f"{field_name}.key_id")
        if key_id in seen_ids:
            fail(f"{field_name}.key_id must be unique.")
        seen_ids.add(key_id)
        keys.append(
            {
                "key_id": key_id,
                "secret_version": require_secret_version_ref(
                    mapping.get("secret_version"),
                    field_name=f"{field_name}.secret_version",
                    project_id=CENTRAL_PROJECT_ID,
                ),
            }
        )
    return keys


def require_directory(value: Any) -> dict[str, str]:
    mapping = require_mapping(value, "directory")
    require_exact_mapping_keys(
        mapping,
        expected=DIRECTORY_FIELDS,
        field_name="directory",
    )
    admin_subject = validate_directory_admin_subject(mapping.get("admin_subject"))
    service_account_email = validate_directory_service_account_email(
        mapping.get("service_account_email")
    )
    return {
        "admin_subject": admin_subject,
        "service_account_email": service_account_email,
    }


def require_scopes(value: Any, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        fail(f"{field_name} must be a non-empty list.")
    scopes = [require_identifier(item, field_name) for item in value]
    if len(set(scopes)) != len(scopes):
        fail(f"{field_name} must be unique.")
    if set(scopes) != {"chat:write", "commands"}:
        fail(f"{field_name} must be exactly chat:write and commands.")
    return ["chat:write", "commands"]


def require_slack(value: Any) -> dict[str, Any]:
    mapping = require_mapping(value, "slack")
    require_exact_mapping_keys(mapping, expected=SLACK_FIELDS, field_name="slack")
    return {
        "required_scopes": require_scopes(
            mapping.get("required_scopes"),
            field_name="slack.required_scopes",
        )
    }


def require_repository_resource(value: Any, *, project_id: str) -> str:
    resource = require_text(value, "github_app.bindings.repository_resource")
    parts = resource.split("/")
    if (
        len(parts) != 8
        or parts[0] != "projects"
        or parts[1] != project_id
        or parts[2] != "locations"
        or parts[3] != FIXED_REGION
        or parts[4] != "connections"
        or parts[6] != "repositories"
        or not parts[5]
        or not parts[7]
    ):
        fail("github_app.bindings.repository_resource is invalid.")
    return resource


def require_github_app(value: Any, *, project_id: str) -> dict[str, Any]:
    mapping = require_mapping(value, "github_app")
    require_exact_mapping_keys(
        mapping,
        expected=GITHUB_APP_FIELDS,
        field_name="github_app",
    )
    installation_id = require_positive_int(
        mapping.get("installation_id"),
        "github_app.installation_id",
    )
    repository_ids = mapping.get("allowed_repository_ids")
    if not isinstance(repository_ids, list) or not repository_ids:
        fail("github_app.allowed_repository_ids must be a non-empty list.")
    allowlist: list[int] = []
    seen_allowlist_ids: set[int] = set()
    for item in repository_ids:
        repository_id = require_positive_int(item, "github_app.allowed_repository_ids")
        if repository_id in seen_allowlist_ids:
            fail("github_app.allowed_repository_ids must be unique.")
        seen_allowlist_ids.add(repository_id)
        allowlist.append(repository_id)

    bindings_value = mapping.get("bindings")
    if not isinstance(bindings_value, list) or not bindings_value:
        fail("github_app.bindings must be a non-empty list.")
    bindings: list[dict[str, Any]] = []
    seen_binding_ids: set[int] = set()
    seen_binding_resources: set[str] = set()
    for item in bindings_value:
        entry = require_mapping(item, "github_app.bindings")
        require_exact_mapping_keys(
            entry,
            expected=GITHUB_BINDING_FIELDS,
            field_name="github_app.bindings",
        )
        repository_numeric_id = require_positive_int(
            entry.get("repository_numeric_id"),
            "github_app.bindings.repository_numeric_id",
        )
        owner = require_text(entry.get("owner"), "github_app.bindings.owner")
        if owner != GITHUB_OWNER:
            fail("github_app.bindings.owner must be madupmarketing.")
        binding_installation_id = require_positive_int(
            entry.get("installation_id"),
            "github_app.bindings.installation_id",
        )
        if binding_installation_id != installation_id:
            fail(
                "github_app.bindings.installation_id must match "
                "github_app.installation_id."
            )
        repository_resource = require_repository_resource(
            entry.get("repository_resource"),
            project_id=project_id,
        )
        if repository_numeric_id in seen_binding_ids:
            fail("github_app.bindings.repository_numeric_id must be unique.")
        if repository_resource in seen_binding_resources:
            fail("github_app.bindings.repository_resource must be unique.")
        seen_binding_ids.add(repository_numeric_id)
        seen_binding_resources.add(repository_resource)
        bindings.append(
            {
                "repository_numeric_id": repository_numeric_id,
                "owner": owner,
                "name": require_text(entry.get("name"), "github_app.bindings.name"),
                "installation_id": binding_installation_id,
                "repository_resource": repository_resource,
            }
        )
    if seen_binding_ids != seen_allowlist_ids:
        fail(
            "github_app.bindings.repository_numeric_id must match "
            "the repository allowlist."
        )
    return {
        "app_id": require_numeric_string(mapping.get("app_id"), "github_app.app_id"),
        "private_key_secret_version": require_secret_version_ref(
            mapping.get("private_key_secret_version"),
            field_name="github_app.private_key_secret_version",
            project_id=project_id,
        ),
        "installation_id": installation_id,
        "allowed_repository_ids": allowlist,
        "bindings": bindings,
    }


def require_builder_image(value: Any) -> str:
    image = require_text(value, "build.builder_image")
    match = DIGEST_IMAGE_PATTERN.fullmatch(image)
    if match is None or match.group("repository") != "mim-platform":
        fail("build.builder_image is invalid.")
    return image


def require_build(value: Any) -> dict[str, str]:
    mapping = require_mapping(value, "build")
    require_exact_mapping_keys(mapping, expected=BUILD_FIELDS, field_name="build")
    build_service_account = require_text(
        mapping.get("build_service_account"),
        "build.build_service_account",
    )
    expected_service_account = (
        f"projects/{CENTRAL_PROJECT_ID}/serviceAccounts/"
        f"mim-build@{CENTRAL_PROJECT_ID}.iam.gserviceaccount.com"
    )
    if build_service_account != expected_service_account:
        fail("build.build_service_account is invalid.")
    return {
        "builder_image": require_builder_image(mapping.get("builder_image")),
        "build_service_account": build_service_account,
    }


def require_machine_service(
    value: Any,
    *,
    field_name: str,
    expected_service_account: str,
    expected_origin: str,
    expected_path: str,
) -> dict[str, str]:
    mapping = require_mapping(value, field_name)
    require_exact_mapping_keys(
        mapping,
        expected=MACHINE_SERVICE_FIELDS,
        field_name=field_name,
    )
    service_account = require_text(
        mapping.get("service_account_email"),
        f"{field_name}.service_account_email",
    ).casefold()
    if not SERVICE_ACCOUNT_EMAIL_PATTERN.fullmatch(service_account):
        fail(f"{field_name}.service_account_email is invalid.")
    if service_account != expected_service_account:
        fail(f"{field_name}.service_account_email is invalid.")
    audience = require_text(mapping.get("audience"), f"{field_name}.audience")
    url = require_text(mapping.get("url"), f"{field_name}.url")
    if audience != expected_origin or url != f"{expected_origin}{expected_path}":
        fail(f"{field_name} service URL or audience is invalid.")
    return {
        "url": url,
        "audience": audience,
        "service_account_email": service_account,
    }


def validate_bootstrap_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        fail("Runtime bootstrap payload is invalid.")
    keys = frozenset(payload.keys())
    unexpected = sorted(keys - TOP_LEVEL_KEYS)
    missing = sorted((TOP_LEVEL_KEYS - OPTIONAL_TOP_LEVEL_KEYS) - keys)
    if unexpected or missing:
        details = ", ".join(unexpected + missing)
        fail(f"Runtime bootstrap contains unsupported keys: {details}")
    schema_version = payload.get("schema_version")
    if schema_version != 1:
        fail("Runtime bootstrap schema_version is invalid.")
    project_id = validate_project_id(payload.get("project_id"))
    project_number = require_project_number(payload.get("project_number"))
    operator_email = validate_operator_email(payload.get("operator_email"))

    validated = {
        "schema_version": 1,
        "project_id": project_id,
        "project_number": project_number,
        "organization_id": validate_organization_id(payload.get("organization_id")),
        "billing_account_id": validate_billing_account_id(
            payload.get("billing_account_id")
        ),
        "operator_email": operator_email,
        "cloudflare_issuer": validate_cloudflare_issuer(
            payload.get("cloudflare_issuer")
        ),
        "cloudflare_audience": validate_cloudflare_audience(
            payload.get("cloudflare_audience")
        ),
        "app_cloudflare_issuer": validate_cloudflare_issuer(
            payload.get("app_cloudflare_issuer")
        ),
        "app_cloudflare_audience": validate_cloudflare_audience(
            payload.get("app_cloudflare_audience")
        ),
        "public_host_suffix": validate_public_host_suffix(
            payload.get("public_host_suffix")
        ),
        "region": validate_region(payload.get("region")),
        "directory_required_group_email": validate_directory_required_group_email(
            payload.get("directory_required_group_email")
        ),
        "admin_members": require_admin_members(
            payload.get("admin_members"),
            operator_email=operator_email,
        ),
        "breakglass_members": require_breakglass_members(
            payload.get("breakglass_members")
        ),
        "directory": require_directory(payload.get("directory")),
        "origin_hmac_keys": require_origin_keys(
            payload.get("origin_hmac_keys"),
            field_name="origin_hmac_keys",
        ),
        "app_origin_hmac_keys": require_origin_keys(
            payload.get("app_origin_hmac_keys"),
            field_name="app_origin_hmac_keys",
            max_items=2,
        ),
        "desired_state_signing_key_id": require_identifier(
            payload.get("desired_state_signing_key_id"),
            "desired_state_signing_key_id",
        ),
        "desired_state_signing_secret_version": require_secret_version_ref(
            payload.get("desired_state_signing_secret_version"),
            field_name="desired_state_signing_secret_version",
            project_id=project_id,
        ),
        "github_webhook_secret_version": require_secret_version_ref(
            payload.get("github_webhook_secret_version"),
            field_name="github_webhook_secret_version",
            project_id=project_id,
        ),
        "github_app": require_github_app(payload.get("github_app"), project_id=project_id),
        "build": require_build(payload.get("build")),
        "deploy_worker": require_machine_service(
            payload.get("deploy_worker"),
            field_name="deploy_worker",
            expected_service_account=(
                f"mim-deploy-worker@{project_id}.iam.gserviceaccount.com"
            ),
            expected_origin=(
                f"https://mim-deploy-worker-{project_number}.{FIXED_REGION}.run.app"
            ),
            expected_path="/internal/deploy",
        ),
        "app_gateway": require_machine_service(
            payload.get("app_gateway"),
            field_name="app_gateway",
            expected_service_account=(
                f"mim-app-gateway@{project_id}.iam.gserviceaccount.com"
            ),
            expected_origin=(
                f"https://mim-app-gateway-{project_number}.{FIXED_REGION}.run.app"
            ),
            expected_path="",
        ),
        "app_authorization": require_machine_service(
            payload.get("app_authorization"),
            field_name="app_authorization",
            expected_service_account=(
                f"mim-schedule-gateway@{project_id}.iam.gserviceaccount.com"
            ),
            expected_origin=(
                f"https://mim-schedule-gateway-{project_number}.{FIXED_REGION}.run.app"
            ),
            expected_path="/v1/apps/authorize",
        ),
        "schedule_gateway": require_machine_service(
            payload.get("schedule_gateway"),
            field_name="schedule_gateway",
            expected_service_account=(
                f"mim-schedule-gateway@{project_id}.iam.gserviceaccount.com"
            ),
            expected_origin=(
                f"https://mim-schedule-gateway-{project_number}.{FIXED_REGION}.run.app"
            ),
            expected_path="/v1/schedules/execute",
        ),
    }
    if "slack" in payload:
        validated["slack"] = require_slack(payload.get("slack"))
    return validated


def canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def normalize_secret_metadata(secret_payload: Any, versions_payload: Any) -> dict[str, Any]:
    secret = require_mapping(secret_payload, "secret")
    name = require_text(secret.get("name"), "secret.name")
    expected_secret_name = f"projects/{CENTRAL_PROJECT_ID}/secrets/{BOOTSTRAP_SECRET_NAME}"
    if name != expected_secret_name:
        fail("Secret metadata target is invalid.")
    if not isinstance(versions_payload, list):
        fail("Secret version metadata is invalid.")
    highest_enabled_version = 0
    latest_enabled_version: str | None = None
    for item in versions_payload:
        mapping = require_mapping(item, "versions")
        version_name = require_text(mapping.get("name"), "versions.name")
        match = SECRET_VERSION_NAME_PATTERN.fullmatch(version_name)
        if match is None:
            fail("Secret version metadata is invalid.")
        state = require_text(mapping.get("state"), "versions.state")
        if state == "ENABLED":
            version_number = int(match.group("version"))
            if version_number > highest_enabled_version:
                highest_enabled_version = version_number
                latest_enabled_version = version_name
    return {
        "secret": secret_payload,
        "versions": versions_payload,
        "latest_enabled_version": latest_enabled_version,
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_bytes(canonical_json_bytes(payload))


def command_validate(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    raw_bytes = input_path.read_bytes()
    payload = validate_bootstrap_payload(load_unique_json(input_path))
    write_json(Path(args.canonical_output), payload)
    summary = {
        "project_id": payload["project_id"],
        "operator_email": payload["operator_email"],
        "input_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }
    write_json(Path(args.summary_output), summary)
    return 0


def command_normalize_secret_metadata(args: argparse.Namespace) -> int:
    secret_payload = json.loads(Path(args.secret_json).read_text(encoding="utf-8"))
    versions_payload = json.loads(Path(args.versions_json).read_text(encoding="utf-8"))
    write_json(
        Path(args.output),
        normalize_secret_metadata(secret_payload, versions_payload),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--input", required=True)
    validate_parser.add_argument("--canonical-output", required=True)
    validate_parser.add_argument("--summary-output", required=True)
    validate_parser.set_defaults(func=command_validate)

    metadata_parser = subparsers.add_parser("normalize-secret-metadata")
    metadata_parser.add_argument("--secret-json", required=True)
    metadata_parser.add_argument("--versions-json", required=True)
    metadata_parser.add_argument("--output", required=True)
    metadata_parser.set_defaults(func=command_normalize_secret_metadata)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ContractError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
