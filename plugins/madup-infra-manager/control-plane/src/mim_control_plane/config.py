"""Startup configuration for the MADUP infra-manager control plane.

This module accepts only explicit operator-managed startup inputs. Stable
product policy values are fixed in code and cannot be overridden through
additional MIM_* keys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Final, Mapping, cast
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when required startup configuration is missing or invalid."""


REGION: Final[str] = "asia-northeast3"
GITHUB_OWNER: Final[str] = "madupmarketing"
COMPANY_DOMAIN: Final[str] = "madup.com"
PUBLIC_HOSTNAME: Final[str] = "mim.madup.app"
PUBLIC_ORIGIN: Final[str] = f"https://{PUBLIC_HOSTNAME}"
MCP_URL: Final[str] = f"{PUBLIC_ORIGIN}/mcp"
APP_HOST_SUFFIX: Final[str] = "madup.app"
TIMEZONE: Final[str] = "Asia/Seoul"
IDENTITY_MAX_STALENESS_MINUTES: Final[int] = 60
PLAN_EXPIRY_MINUTES: Final[int] = 15
ORIGIN_HMAC_WINDOW_SECONDS: Final[int] = 60
PER_USER_SERVICE_LIMIT: Final[int] = 2
PER_USER_SCHEDULE_LIMIT: Final[int] = 3
DEFAULT_SECRET_LIMIT: Final[int] = 5
HARD_SECRET_LIMIT: Final[int] = 10
SERVICE_CPU: Final[int] = 1
SERVICE_MEMORY_MIB: Final[int] = 512
SERVICE_MIN_INSTANCES: Final[int] = 0
SERVICE_MAX_INSTANCES: Final[int] = 1
TARGET_MONTHLY_BUDGET_KRW: Final[int] = 1000
ADMIN_BUDGET_CEILING_KRW: Final[int] = 10000
TRANSFER_GRACE_DAYS: Final[int] = 7
INACTIVITY_WARNING_DAYS: Final[int] = 23
CLEANUP_DAYS: Final[int] = 30
FINAL_IMAGE_RETENTION_DAYS: Final[int] = 30
PILOT_MAX_IDENTITIES: Final[int] = 50
FIRESTORE_DATABASE_ID: Final[str] = "(default)"

_PROJECT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$",
)
_ORGANIZATION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[1-9][0-9]{5,19}$")
_BILLING_ACCOUNT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$",
)
_OPERATOR_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9._%+-]+@madup\.com$",
)
_SERVICE_ACCOUNT_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@"
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$",
)
_CLOUDFLARE_ISSUER_HOST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9-]+\.cloudflareaccess\.com$",
)
_CLOUDFLARE_AUDIENCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9._-]{8,128}$",
)
_PLACEHOLDER_MARKERS: Final[tuple[str, ...]] = (
    "changeme",
    "example",
    "placeholder",
    "replace-me",
    "your-",
)
_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "MIM_PROJECT_ID",
        "MIM_ORGANIZATION_ID",
        "MIM_BILLING_ACCOUNT_ID",
        "MIM_OPERATOR_EMAIL",
        "MIM_CLOUDFLARE_ISSUER",
        "MIM_CLOUDFLARE_AUDIENCE",
    },
)
_DIRECTORY_RUNTIME_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "MIM_DIRECTORY_ADMIN_SUBJECT",
        "MIM_DIRECTORY_SERVICE_ACCOUNT_EMAIL",
        "MIM_DIRECTORY_REQUIRED_GROUP_EMAIL",
    },
)
_DIRECTORY_RUNTIME_BASE_KEYS: Final[frozenset[str]] = frozenset(
    {"MIM_OPERATOR_EMAIL"},
)
_DIRECTORY_RUNTIME_LOADER_REQUIRED_KEYS: Final[frozenset[str]] = (
    _DIRECTORY_RUNTIME_BASE_KEYS | _DIRECTORY_RUNTIME_REQUIRED_KEYS
)
_PUBLIC_SUPPORTED_KEYS: Final[frozenset[str]] = (
    _REQUIRED_KEYS | _DIRECTORY_RUNTIME_REQUIRED_KEYS
)
_DIRECTORY_RUNTIME_SUPPORTED_KEYS: Final[frozenset[str]] = (
    _REQUIRED_KEYS | _DIRECTORY_RUNTIME_REQUIRED_KEYS
)


@dataclass(frozen=True, slots=True)
class Settings:
    """Centrally managed startup configuration for the control-plane service."""

    project_id: str
    organization_id: str
    billing_account_id: str
    operator_email: str
    cloudflare_issuer: str
    cloudflare_audience: str
    region: str = field(init=False, default=REGION)
    github_owner: str = field(init=False, default=GITHUB_OWNER)
    company_domain: str = field(init=False, default=COMPANY_DOMAIN)
    public_hostname: str = field(init=False, default=PUBLIC_HOSTNAME)
    public_origin: str = field(init=False, default=PUBLIC_ORIGIN)
    mcp_url: str = field(init=False, default=MCP_URL)
    app_host_suffix: str = field(init=False, default=APP_HOST_SUFFIX)
    timezone: str = field(init=False, default=TIMEZONE)
    identity_max_staleness_minutes: int = field(
        init=False,
        default=IDENTITY_MAX_STALENESS_MINUTES,
    )
    plan_expiry_minutes: int = field(init=False, default=PLAN_EXPIRY_MINUTES)
    origin_hmac_window_seconds: int = field(
        init=False,
        default=ORIGIN_HMAC_WINDOW_SECONDS,
    )
    per_user_service_limit: int = field(init=False, default=PER_USER_SERVICE_LIMIT)
    per_user_schedule_limit: int = field(init=False, default=PER_USER_SCHEDULE_LIMIT)
    default_secret_limit: int = field(init=False, default=DEFAULT_SECRET_LIMIT)
    hard_secret_limit: int = field(init=False, default=HARD_SECRET_LIMIT)
    service_cpu: int = field(init=False, default=SERVICE_CPU)
    service_memory_mib: int = field(init=False, default=SERVICE_MEMORY_MIB)
    service_min_instances: int = field(init=False, default=SERVICE_MIN_INSTANCES)
    service_max_instances: int = field(init=False, default=SERVICE_MAX_INSTANCES)
    target_monthly_budget_krw: int = field(
        init=False,
        default=TARGET_MONTHLY_BUDGET_KRW,
    )
    admin_budget_ceiling_krw: int = field(
        init=False,
        default=ADMIN_BUDGET_CEILING_KRW,
    )
    transfer_grace_days: int = field(init=False, default=TRANSFER_GRACE_DAYS)
    inactivity_warning_days: int = field(
        init=False,
        default=INACTIVITY_WARNING_DAYS,
    )
    cleanup_days: int = field(init=False, default=CLEANUP_DAYS)
    final_image_retention_days: int = field(
        init=False,
        default=FINAL_IMAGE_RETENTION_DAYS,
    )
    pilot_max_identities: int = field(init=False, default=PILOT_MAX_IDENTITIES)
    firestore_database_id: str = field(init=False, default=FIRESTORE_DATABASE_ID)

    def __post_init__(self) -> None:
        _set_validated_operator_input(
            self,
            "project_id",
            "MIM_PROJECT_ID",
            _validate_project_id,
        )
        _set_validated_operator_input(
            self,
            "organization_id",
            "MIM_ORGANIZATION_ID",
            _validate_organization_id,
        )
        _set_validated_operator_input(
            self,
            "billing_account_id",
            "MIM_BILLING_ACCOUNT_ID",
            _validate_billing_account_id,
        )
        _set_validated_operator_input(
            self,
            "operator_email",
            "MIM_OPERATOR_EMAIL",
            _validate_operator_email,
        )
        _set_validated_operator_input(
            self,
            "cloudflare_issuer",
            "MIM_CLOUDFLARE_ISSUER",
            _validate_cloudflare_issuer,
        )
        _set_validated_operator_input(
            self,
            "cloudflare_audience",
            "MIM_CLOUDFLARE_AUDIENCE",
            _validate_cloudflare_audience,
        )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> "Settings":
        """Load operator-managed startup configuration from a supplied mapping."""

        _reject_unknown_mim_keys(mapping, supported_keys=_PUBLIC_SUPPORTED_KEYS)
        _require_keys_present(mapping)

        return cls(
            project_id=cast(str, mapping["MIM_PROJECT_ID"]),
            organization_id=cast(str, mapping["MIM_ORGANIZATION_ID"]),
            billing_account_id=cast(str, mapping["MIM_BILLING_ACCOUNT_ID"]),
            operator_email=cast(str, mapping["MIM_OPERATOR_EMAIL"]),
            cloudflare_issuer=cast(str, mapping["MIM_CLOUDFLARE_ISSUER"]),
            cloudflare_audience=cast(str, mapping["MIM_CLOUDFLARE_AUDIENCE"]),
        )


@dataclass(frozen=True, slots=True)
class DirectoryRuntimeSettings:
    """Centrally managed private runtime configuration for Directory sync."""

    operator_email: str
    directory_admin_subject: str
    directory_service_account_email: str
    directory_required_group_email: str
    directory_required_group_label: str = field(init=False)

    def __post_init__(self) -> None:
        _set_validated_operator_input(
            self,
            "operator_email",
            "MIM_OPERATOR_EMAIL",
            _validate_operator_email,
        )
        _set_validated_operator_input(
            self,
            "directory_admin_subject",
            "MIM_DIRECTORY_ADMIN_SUBJECT",
            _validate_directory_admin_subject,
        )
        _set_validated_operator_input(
            self,
            "directory_service_account_email",
            "MIM_DIRECTORY_SERVICE_ACCOUNT_EMAIL",
            _validate_directory_service_account_email,
        )
        _set_validated_operator_input(
            self,
            "directory_required_group_email",
            "MIM_DIRECTORY_REQUIRED_GROUP_EMAIL",
            _validate_directory_required_group_email,
        )
        object.__setattr__(
            self,
            "directory_required_group_label",
            self.directory_required_group_email.partition("@")[0].casefold(),
        )
        if self.directory_admin_subject.casefold() == self.operator_email.casefold():
            raise ConfigError(
                "MIM_DIRECTORY_ADMIN_SUBJECT must differ from MIM_OPERATOR_EMAIL.",
            )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> "DirectoryRuntimeSettings":
        _reject_unknown_mim_keys(
            mapping,
            supported_keys=_DIRECTORY_RUNTIME_SUPPORTED_KEYS,
        )
        _require_keys_present(
            mapping,
            required_keys=_DIRECTORY_RUNTIME_LOADER_REQUIRED_KEYS,
        )
        return cls(
            operator_email=cast(str, mapping["MIM_OPERATOR_EMAIL"]),
            directory_admin_subject=cast(str, mapping["MIM_DIRECTORY_ADMIN_SUBJECT"]),
            directory_service_account_email=cast(
                str,
                mapping["MIM_DIRECTORY_SERVICE_ACCOUNT_EMAIL"],
            ),
            directory_required_group_email=cast(
                str,
                mapping["MIM_DIRECTORY_REQUIRED_GROUP_EMAIL"],
            ),
        )

    def __repr__(self) -> str:
        return "DirectoryRuntimeSettings(redacted=True)"


def _reject_unknown_mim_keys(
    mapping: Mapping[str, object],
    *,
    supported_keys: frozenset[str],
) -> None:
    for key in mapping:
        if key.startswith("MIM_") and key not in supported_keys:
            raise ConfigError(f"{key} is not a supported startup configuration key.")


def _require_keys_present(
    mapping: Mapping[str, object],
    *,
    required_keys: frozenset[str] = _REQUIRED_KEYS,
) -> None:
    for key in required_keys:
        if key not in mapping:
            raise ConfigError(f"Missing required startup configuration key: {key}")


def _normalize_string(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be provided as a string value.")
    return _normalize_non_empty_string(value, key)


def _normalize_non_empty_string(value: str, key: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ConfigError(f"{key} must not be empty.")
    return stripped


def _set_validated_operator_input(
    settings: object,
    field_name: str,
    key: str,
    validator: Callable[[str], str],
) -> None:
    normalized = validator(_normalize_string(getattr(settings, field_name), key))
    object.__setattr__(settings, field_name, normalized)


def _contains_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _validate_project_id(value: str) -> str:
    if _contains_placeholder(value) or _PROJECT_ID_PATTERN.fullmatch(value) is None:
        raise ConfigError(
            "MIM_PROJECT_ID must be an explicit operator-managed GCP project ID.",
        )
    return value


def _validate_organization_id(value: str) -> str:
    if (
        _contains_placeholder(value)
        or _ORGANIZATION_ID_PATTERN.fullmatch(value) is None
    ):
        raise ConfigError(
            "MIM_ORGANIZATION_ID must be an explicit operator-managed numeric org ID.",
        )
    return value


def _validate_billing_account_id(value: str) -> str:
    if (
        _contains_placeholder(value)
        or _BILLING_ACCOUNT_PATTERN.fullmatch(value) is None
    ):
        raise ConfigError(
            "MIM_BILLING_ACCOUNT_ID must be an explicit operator-managed billing ID.",
        )
    return value


def _validate_operator_email(value: str) -> str:
    if _contains_placeholder(value) or _OPERATOR_EMAIL_PATTERN.fullmatch(value) is None:
        raise ConfigError(
            "MIM_OPERATOR_EMAIL must be an operator-managed @madup.com address.",
        )
    return value


def _validate_directory_admin_subject(value: str) -> str:
    if _contains_placeholder(value) or _OPERATOR_EMAIL_PATTERN.fullmatch(value) is None:
        raise ConfigError(
            "MIM_DIRECTORY_ADMIN_SUBJECT must be a dedicated @madup.com address.",
        )
    return value


def _validate_directory_service_account_email(value: str) -> str:
    if (
        _contains_placeholder(value)
        or _SERVICE_ACCOUNT_EMAIL_PATTERN.fullmatch(value) is None
    ):
        raise ConfigError(
            "MIM_DIRECTORY_SERVICE_ACCOUNT_EMAIL must be a GCP service account email.",
        )
    return value


def _validate_directory_required_group_email(value: str) -> str:
    if _contains_placeholder(value) or _OPERATOR_EMAIL_PATTERN.fullmatch(value) is None:
        raise ConfigError(
            "MIM_DIRECTORY_REQUIRED_GROUP_EMAIL must be a configured "
            "@madup.com group email.",
        )
    return value


def _validate_cloudflare_issuer(value: str) -> str:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(
            "MIM_CLOUDFLARE_ISSUER must be a safe HTTPS issuer URL.",
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or port is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or _CLOUDFLARE_ISSUER_HOST_PATTERN.fullmatch(parsed.hostname) is None
        or _contains_placeholder(parsed.hostname)
    ):
        raise ConfigError("MIM_CLOUDFLARE_ISSUER must be a safe HTTPS issuer URL.")
    return f"https://{parsed.hostname}"


def _validate_cloudflare_audience(value: str) -> str:
    if (
        _contains_placeholder(value)
        or _CLOUDFLARE_AUDIENCE_PATTERN.fullmatch(value) is None
    ):
        raise ConfigError(
            "MIM_CLOUDFLARE_AUDIENCE must be a non-empty configured audience ID.",
        )
    return value
