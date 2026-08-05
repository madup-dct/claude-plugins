from __future__ import annotations

import os
import re
import stat
import unittest
from dataclasses import dataclass
from pathlib import Path

BASE_URL = "https://mim.madup.app"
PROJECT_ID = "mim-prod-123456"
REGION = "asia-northeast3"
CONTROL_PLANE_SERVICE = "mim-control-plane"
DEPLOY_WORKER_SERVICE = "mim-deploy-worker"
SCHEDULE_GATEWAY_SERVICE = "mim-schedule-gateway"
CONTROL_PLANE_RUN_APP_PATTERN = re.compile(
    r"^https://mim-control-plane-[0-9]{12}\.asia-northeast3\.run\.app$"
)
USER_WORKLOAD_RUN_APP_PATTERN = re.compile(
    r"^https://mim-user-app-[0-9]{12}\.asia-northeast3\.run\.app(?:/[a-z0-9/_-]+)?$"
)
SLACK_REDIRECT_PATH = "/slack/oauth/callback"
SLACK_REDIRECT_URI = f"{BASE_URL}{SLACK_REDIRECT_PATH}"
SLACK_REQUIRED_SCOPES = ("chat:write", "commands")

PROJECT_NUMBER_PATTERN = re.compile(r"^[1-9][0-9]{11}$")
RUN_APP_AUDIENCE_PATTERN = re.compile(
    r"^https://mim-(?:deploy-worker|schedule-gateway)-[0-9]{12}"
    r"\.asia-northeast3\.run\.app(?:/[a-z0-9/_-]+)?$"
)
RUNTIME_SERVICE_ACCOUNT_PATTERN = re.compile(
    r"^mim-(?:control-plane|deploy-worker|schedule-gateway)"
    r"@mim-prod-123456\.iam\.gserviceaccount\.com$"
)
USER_EMAIL_PATTERN = re.compile(r"^[a-z0-9._%+-]+@madup\.com$")
SLACK_TEAM_ID_PATTERN = re.compile(r"^T[A-Z0-9]{8,}$")
SLACK_ENTERPRISE_ID_PATTERN = re.compile(r"^E[A-Z0-9]{8,}$")
STATE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,256}$")


@dataclass(frozen=True, slots=True)
class Probe:
    name: str
    method: str
    url: str
    expected_status: int
    surface: str


def staging_canaries_required() -> bool:
    return os.environ.get("MIM_REQUIRE_STAGING_CANARIES") == "true"


def require_env(
    name: str,
    *,
    exact: str | None = None,
    pattern: re.Pattern[str] | None = None,
) -> str:
    value = os.environ.get(name, "")
    if not value:
        if staging_canaries_required():
            raise AssertionError(
                f"{name} is required when MIM_REQUIRE_STAGING_CANARIES=true"
            )
        raise unittest.SkipTest(f"{name} is not configured for local canary discovery.")
    if exact is not None and value != exact:
        raise AssertionError(f"{name} must match the exact approved value.")
    if pattern is not None and not pattern.fullmatch(value):
        raise AssertionError(f"{name} does not match the approved format.")
    return value


def assert_no_secret_echo(
    test_case: unittest.TestCase,
    text: str,
    *secrets: str,
) -> None:
    for secret in secrets:
        if secret:
            test_case.assertNotIn(secret, text)


def assert_private_mode_0600(test_case: unittest.TestCase, path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    test_case.assertEqual(mode, 0o600)


def require_private_file_env(name: str) -> Path:
    raw_path = require_env(name)
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise AssertionError(f"{name} must point to a regular file.")
    if path.is_symlink():
        raise AssertionError(f"{name} must not point to a symlink.")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise AssertionError(f"{name} must point to a mode 0600 file.")
    return path
