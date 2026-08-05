"""Synthetic startup configuration fixtures for control-plane tests."""

from __future__ import annotations

from typing import Any

FAKE_STARTUP_CONFIG: dict[str, str] = {
    "MIM_PROJECT_ID": "mim-prod-123456",
    "MIM_ORGANIZATION_ID": "123456789012",
    "MIM_BILLING_ACCOUNT_ID": "ABCDEF-123456-7890AB",
    "MIM_OPERATOR_EMAIL": "operator.test@madup.com",
    "MIM_CLOUDFLARE_ISSUER": "https://madup.cloudflareaccess.com",
    "MIM_CLOUDFLARE_AUDIENCE": "cf-aud-1234567890",
}

FAKE_DIRECTORY_RUNTIME_CONFIG: dict[str, str] = {
    "MIM_DIRECTORY_ADMIN_SUBJECT": "directory.admin@madup.com",
    "MIM_DIRECTORY_SERVICE_ACCOUNT_EMAIL": (
        "mim-directory-sync@mim-prod-123456.iam.gserviceaccount.com"
    ),
    "MIM_DIRECTORY_REQUIRED_GROUP_EMAIL": "mim-users@madup.com",
}


def build_startup_mapping(**overrides: Any) -> dict[str, str]:
    mapping = dict(FAKE_STARTUP_CONFIG)
    for key, value in overrides.items():
        mapping[key] = value
    return mapping


def build_directory_runtime_mapping(**overrides: Any) -> dict[str, str]:
    mapping = dict(FAKE_STARTUP_CONFIG)
    mapping.update(FAKE_DIRECTORY_RUNTIME_CONFIG)
    for key, value in overrides.items():
        mapping[key] = value
    return mapping
