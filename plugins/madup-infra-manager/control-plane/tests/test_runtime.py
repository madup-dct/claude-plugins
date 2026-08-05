from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest import mock

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
TEMPLATE_BOOTSTRAP_PATH = (
    TEST_ROOT.parent.parent
    / "infra"
    / "runtime-bootstrap"
    / "bootstrap-input.template.json"
)
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane import config  # noqa: E402
from mim_control_plane.runtime import (  # noqa: E402
    ProductionDependencies,
    PublicRuntimeParts,
    RuntimeEnvironment,
    RuntimeMode,
    _build_central_identity_gateway,
    _build_private_deploy_worker,
    _build_public_runtime_parts,
    _build_schedule_gateway_runtime_app,
    _FirestoreScheduleDispatchLedger,
    _runtime_id,
    _runtime_lease_token,
    build_runtime_app,
    load_runtime_environment,
)

NOW = datetime(2026, 8, 4, 3, 0, 0, tzinfo=UTC)
BOOTSTRAP_SECRET_VERSION = (
    "projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7"
)
EDGE_SECRET_VERSION = (
    "projects/mim-prod-123456/secrets/mim-edge-origin-v1/versions/1"
)
APP_EDGE_SECRET_VERSION = (
    "projects/mim-prod-123456/secrets/"
    "mim-app-gateway-origin-v1/versions/5"
)
APP_EDGE_PREVIOUS_SECRET_VERSION = (
    "projects/mim-prod-123456/secrets/"
    "mim-app-gateway-origin-v0/versions/4"
)
SIGNING_SECRET_VERSION = (
    "projects/mim-prod-123456/secrets/mim-desired-state-signing/versions/2"
)
WEBHOOK_SECRET_VERSION = (
    "projects/mim-prod-123456/secrets/mim-github-webhook/versions/3"
)
APP_KEY_SECRET_VERSION = (
    "projects/mim-prod-123456/secrets/mim-github-app-key/versions/4"
)
PROJECT_NUMBER = "123456789012"


def bootstrap_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "mim-prod-123456",
        "project_number": PROJECT_NUMBER,
        "organization_id": "123456789012",
        "billing_account_id": "ABCDEF-123456-7890AB",
        "operator_email": "mim@madup.com",
        "cloudflare_issuer": "https://madup.cloudflareaccess.com",
        "cloudflare_audience": "cf-aud-1234567890",
        "app_cloudflare_issuer": "https://madup.cloudflareaccess.com",
        "app_cloudflare_audience": "cf-app-aud-1234567890",
        "public_host_suffix": "madup.app",
        "region": "asia-northeast3",
        "directory_required_group_email": "mim-users@madup.com",
        "admin_members": [
            "group:mim-admins@madup.com",
            "user:mim@madup.com",
        ],
        "directory": {
            "admin_subject": "directory.admin@madup.com",
            "service_account_email": (
                "mim-identity-sync@mim-prod-123456."
                "iam.gserviceaccount.com"
            ),
        },
        "slack": {"required_scopes": ["chat:write", "commands"]},
        "origin_hmac_keys": [
            {
                "key_id": "edge-current",
                "secret_version": EDGE_SECRET_VERSION,
            }
        ],
        "app_origin_hmac_keys": [
            {
                "key_id": "app-current",
                "secret_version": APP_EDGE_SECRET_VERSION,
            },
            {
                "key_id": "app-previous",
                "secret_version": APP_EDGE_PREVIOUS_SECRET_VERSION,
            },
        ],
        "desired_state_signing_key_id": "deploy-key-202608",
        "desired_state_signing_secret_version": SIGNING_SECRET_VERSION,
        "github_webhook_secret_version": WEBHOOK_SECRET_VERSION,
        "github_app": {
            "app_id": "123456",
            "private_key_secret_version": APP_KEY_SECRET_VERSION,
            "installation_id": 303,
            "allowed_repository_ids": [101],
            "bindings": [
                {
                    "repository_numeric_id": 101,
                    "owner": "madupmarketing",
                    "name": "sample-app",
                    "installation_id": 303,
                    "repository_resource": (
                        "projects/mim-prod-123456/locations/"
                        "asia-northeast3/connections/mim-github/"
                        "repositories/sample-app"
                    ),
                }
            ],
        },
        "build": {
            "builder_image": (
                "asia-northeast3-docker.pkg.dev/mim-prod-123456/"
                "mim-platform/mim-builder@sha256:" + ("c" * 64)
            ),
            "build_service_account": (
                "projects/mim-prod-123456/serviceAccounts/"
                "mim-build@mim-prod-123456.iam.gserviceaccount.com"
            ),
        },
        "deploy_worker": {
            "url": (
                "https://mim-deploy-worker-123456789012."
                "asia-northeast3.run.app/internal/deploy"
            ),
            "audience": (
                "https://mim-deploy-worker-123456789012."
                "asia-northeast3.run.app"
            ),
            "service_account_email": (
                "mim-deploy-worker@mim-prod-123456."
                "iam.gserviceaccount.com"
            ),
        },
        "app_gateway": {
            "url": (
                "https://mim-app-gateway-123456789012."
                "asia-northeast3.run.app"
            ),
            "audience": (
                "https://mim-app-gateway-123456789012."
                "asia-northeast3.run.app"
            ),
            "service_account_email": (
                "mim-app-gateway@mim-prod-123456."
                "iam.gserviceaccount.com"
            ),
        },
        "app_authorization": {
            "url": (
                "https://mim-schedule-gateway-123456789012."
                "asia-northeast3.run.app/v1/apps/authorize"
            ),
            "audience": (
                "https://mim-schedule-gateway-123456789012."
                "asia-northeast3.run.app"
            ),
            "service_account_email": (
                "mim-schedule-gateway@mim-prod-123456."
                "iam.gserviceaccount.com"
            ),
        },
        "schedule_gateway": {
            "url": (
                "https://mim-schedule-gateway-123456789012."
                "asia-northeast3.run.app/v1/schedules/execute"
            ),
            "audience": (
                "https://mim-schedule-gateway-123456789012."
                "asia-northeast3.run.app"
            ),
            "service_account_email": (
                "mim-schedule-gateway@mim-prod-123456."
                "iam.gserviceaccount.com"
            ),
        },
    }


def environment(*, mode: str, mutations: str = "false", **extra: str) -> dict[str, str]:
    values = {
        "MIM_RUNTIME_MODE": mode,
        "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION": BOOTSTRAP_SECRET_VERSION,
        "MIM_ENABLE_MUTATIONS": mutations,
    }
    values.update(extra)
    return values


class RecordingBootstrapLoader:
    def __init__(self, *, payload: bytes | None = None) -> None:
        self.payload = payload or json.dumps(
            bootstrap_payload(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.calls: list[tuple[str, object]] = []

    def __call__(self, *, secret_version: str, credentials: object) -> bytes:
        self.calls.append((secret_version, credentials))
        return self.payload


class RuntimeEnvironmentLoadingTests(unittest.TestCase):
    def assert_invalid_bootstrap(
        self,
        payload: dict[str, object],
        expected_message: str,
    ) -> None:
        loader = RecordingBootstrapLoader(
            payload=json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        with self.assertRaisesRegex(config.ConfigError, expected_message):
            load_runtime_environment(
                environment(mode="control-plane"),
                dependencies=ProductionDependencies(
                    metadata_credentials_loader=object,
                    bootstrap_secret_loader=loader,
                ),
            )

    def test_loads_exact_runtime_environment_with_metadata_credentials_only(
        self,
    ) -> None:
        credentials = object()
        loader = RecordingBootstrapLoader()

        loaded = load_runtime_environment(
            environment(mode="control-plane"),
            dependencies=ProductionDependencies(
                metadata_credentials_loader=lambda: credentials,
                bootstrap_secret_loader=loader,
            ),
        )

        self.assertEqual(loaded.mode, RuntimeMode.CONTROL_PLANE)
        self.assertFalse(loaded.mutations_enabled)
        self.assertEqual(loaded.bootstrap.project_id, "mim-prod-123456")
        self.assertEqual(
            loaded.bootstrap.admin_members,
            ("group:mim-admins@madup.com", "user:mim@madup.com"),
        )
        self.assertEqual(
            loaded.bootstrap.directory_runtime_settings.directory_admin_subject,
            "directory.admin@madup.com",
        )
        self.assertEqual(loaded.bootstrap.public_host_suffix, "madup.app")
        self.assertEqual(
            loaded.bootstrap.app_cloudflare_audience,
            "cf-app-aud-1234567890",
        )
        self.assertEqual(
            tuple(key.key_id for key in loaded.bootstrap.app_origin_hmac_keys),
            ("app-current", "app-previous"),
        )
        self.assertEqual(
            loaded.bootstrap.app_gateway.service_account_email,
            (
                "mim-app-gateway@mim-prod-123456."
                "iam.gserviceaccount.com"
            ),
        )
        self.assertEqual(
            loaded.bootstrap.app_authorization.url,
            (
                "https://mim-schedule-gateway-123456789012."
                "asia-northeast3.run.app/v1/apps/authorize"
            ),
        )
        self.assertEqual(loader.calls, [(BOOTSTRAP_SECRET_VERSION, credentials)])

    def test_missing_breakglass_members_normalizes_empty_tuple(self) -> None:
        loaded = load_runtime_environment(
            environment(mode="control-plane"),
            dependencies=ProductionDependencies(
                metadata_credentials_loader=object,
                bootstrap_secret_loader=RecordingBootstrapLoader(),
            ),
        )

        self.assertEqual(loaded.bootstrap.breakglass_members, ())

    def test_missing_slack_configuration_disables_slack_runtime(self) -> None:
        payload = bootstrap_payload()
        payload.pop("slack")

        loaded = load_runtime_environment(
            environment(mode="control-plane"),
            dependencies=ProductionDependencies(
                metadata_credentials_loader=object,
                bootstrap_secret_loader=RecordingBootstrapLoader(
                    payload=json.dumps(
                        payload,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8"),
                ),
            ),
        )

        self.assertEqual(loaded.bootstrap.slack_required_scopes, ())

    def test_loads_current_template_bootstrap_with_empty_breakglass(self) -> None:
        loaded = load_runtime_environment(
            environment(mode="control-plane"),
            dependencies=ProductionDependencies(
                metadata_credentials_loader=object,
                bootstrap_secret_loader=RecordingBootstrapLoader(
                    payload=TEMPLATE_BOOTSTRAP_PATH.read_bytes(),
                ),
            ),
        )

        self.assertEqual(
            loaded.bootstrap.admin_members,
            ("group:mim-admins@madup.com", "user:operator.name@madup.com"),
        )
        self.assertEqual(loaded.bootstrap.breakglass_members, ())

    def test_loads_explicit_breakglass_members_exactly(self) -> None:
        payload = bootstrap_payload()
        payload["breakglass_members"] = [
            "group:security-review@madup.com",
            "user:reviewer@madup.com",
        ]

        loaded = load_runtime_environment(
            environment(mode="control-plane"),
            dependencies=ProductionDependencies(
                metadata_credentials_loader=object,
                bootstrap_secret_loader=RecordingBootstrapLoader(
                    payload=json.dumps(
                        payload,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8"),
                ),
            ),
        )

        self.assertEqual(
            loaded.bootstrap.breakglass_members,
            ("group:security-review@madup.com", "user:reviewer@madup.com"),
        )

    def test_rejects_invalid_breakglass_members(self) -> None:
        invalid_cases = (
            (
                [
                    "user:reviewer@madup.com",
                    "group:security-review@madup.com",
                ],
                "breakglass_members must be sorted.",
            ),
            (
                [
                    "group:security-review@madup.com",
                    "group:security-review@madup.com",
                ],
                "breakglass_members must be unique.",
            ),
            (
                ["user:reviewer@example.com"],
                "breakglass_members",
            ),
        )

        for members, expected_message in invalid_cases:
            with self.subTest(members=members):
                payload = bootstrap_payload()
                payload["breakglass_members"] = members
                self.assert_invalid_bootstrap(payload, expected_message)

    def test_central_identity_gateway_injects_runtime_store_into_action_authorizer(
        self,
    ) -> None:
        runtime_env = load_runtime_environment(
            environment(mode="control-plane"),
            dependencies=ProductionDependencies(
                metadata_credentials_loader=object,
                bootstrap_secret_loader=RecordingBootstrapLoader(),
            ),
        )
        calls: dict[str, object] = {}
        runtime_store = object()

        class FakeActionAuthorizer:
            def __init__(self, *, store: object) -> None:
                calls["authorizer_store"] = store

        class FakeIdentityPolicy:
            def __init__(self, **kwargs: object) -> None:
                calls["identity_policy_kwargs"] = kwargs

        class FakeIdentityAuthenticator:
            def __init__(self, **kwargs: object) -> None:
                calls["authenticator_kwargs"] = kwargs

        class FakeGateway:
            def __init__(self, **kwargs: object) -> None:
                calls["gateway_kwargs"] = kwargs

        with mock.patch(
            "mim_control_plane.runtime._build_origin_verifier",
            return_value="origin-verifier",
        ), mock.patch(
            "mim_control_plane.adapters.action_policy.ClosedActionPolicyAuthorizer",
            FakeActionAuthorizer,
        ), mock.patch(
            "mim_control_plane.adapters.slack_identity."
            "FirestoreSlackIdentityDirectory",
            side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
        ), mock.patch(
            "mim_control_plane.security.authorization.IdentityPolicy",
            FakeIdentityPolicy,
        ), mock.patch(
            "mim_control_plane.security.identity.IdentityAuthenticator",
            FakeIdentityAuthenticator,
        ), mock.patch(
            "mim_control_plane.security.identity.CloudflareJwtVerifier",
            side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
        ), mock.patch(
            "mim_control_plane.services.central_identity.CentralIdentityGateway",
            FakeGateway,
        ):
            _build_central_identity_gateway(
                store=runtime_store,
                bootstrap=runtime_env.bootstrap,
                credentials=object(),
                clock=lambda: NOW,
                slack_repository=object(),
            )

        self.assertIs(calls["authorizer_store"], runtime_store)

    def test_loads_closed_job_runtime_modes(self) -> None:
        for mode, expected in (
            ("identity-sync", RuntimeMode.IDENTITY_SYNC),
            ("lifecycle", RuntimeMode.LIFECYCLE),
            ("usage-ingest", RuntimeMode.USAGE_INGEST),
        ):
            with self.subTest(mode=mode):
                loaded = load_runtime_environment(
                    environment(mode=mode, mutations="true"),
                    dependencies=ProductionDependencies(
                        metadata_credentials_loader=object,
                        bootstrap_secret_loader=RecordingBootstrapLoader(),
                    ),
                )
                self.assertEqual(loaded.mode, expected)

    def test_rejects_unknown_top_level_bootstrap_key(self) -> None:
        payload = bootstrap_payload()
        payload["unexpected"] = "value"

        self.assert_invalid_bootstrap(payload, "unexpected")

    def test_rejects_unknown_runtime_environment_keys(self) -> None:
        with self.assertRaisesRegex(
            config.ConfigError,
            "MIM_CONTROL_PLANE_APP_FACTORY",
        ):
            load_runtime_environment(
                environment(
                    mode="control-plane",
                    MIM_CONTROL_PLANE_APP_FACTORY="tests.test_main:built_app",
                ),
                dependencies=ProductionDependencies(
                    metadata_credentials_loader=object,
                    bootstrap_secret_loader=RecordingBootstrapLoader(),
                ),
            )

    def test_rejects_duplicate_bootstrap_json_keys(self) -> None:
        duplicate_json = (
            b'{"schema_version":1,"project_id":"mim-prod-123456",'
            b'"project_id":"other"}'
        )
        with self.assertRaisesRegex(config.ConfigError, "bootstrap"):
            load_runtime_environment(
                environment(mode="control-plane"),
                dependencies=ProductionDependencies(
                    metadata_credentials_loader=object,
                    bootstrap_secret_loader=RecordingBootstrapLoader(
                        payload=duplicate_json
                    ),
                ),
            )

    def test_rejects_non_numeric_secret_references_and_human_adc_override(self) -> None:
        payload = bootstrap_payload()
        payload["desired_state_signing_secret_version"] = (
            "projects/mim-prod-123456/secrets/mim-desired-state-signing/versions/latest"
        )
        loader = RecordingBootstrapLoader(
            payload=json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        credentials = object()

        with self.assertRaisesRegex(
            config.ConfigError,
            "desired_state_signing_secret_version",
        ):
            load_runtime_environment(
                environment(
                    mode="control-plane",
                    GOOGLE_APPLICATION_CREDENTIALS="/tmp/human-adc.json",
                ),
                dependencies=ProductionDependencies(
                    metadata_credentials_loader=lambda: credentials,
                    bootstrap_secret_loader=loader,
                ),
            )

        self.assertEqual(loader.calls, [(BOOTSTRAP_SECRET_VERSION, credentials)])

    def test_rejects_missing_required_nested_keys(self) -> None:
        payload = bootstrap_payload()
        del cast(dict[str, object], payload["github_app"])["app_id"]

        self.assert_invalid_bootstrap(payload, "github_app")

    def test_rejects_non_exact_slack_scopes(self) -> None:
        payload = bootstrap_payload()
        cast(dict[str, object], payload["slack"])["required_scopes"] = [
            "commands",
            "users:read.email",
        ]

        self.assert_invalid_bootstrap(payload, "slack.required_scopes")

    def test_rejects_duplicate_repository_allowlist_ids(self) -> None:
        payload = bootstrap_payload()
        cast(dict[str, object], payload["github_app"])["allowed_repository_ids"] = [
            101,
            101,
        ]

        self.assert_invalid_bootstrap(payload, "allowed_repository_ids")

    def test_rejects_unsorted_or_missing_operator_admin_members(self) -> None:
        unsorted_payload = bootstrap_payload()
        unsorted_payload["admin_members"] = [
            "user:mim@madup.com",
            "group:mim-admins@madup.com",
        ]
        self.assert_invalid_bootstrap(unsorted_payload, "admin_members")

        missing_operator_payload = bootstrap_payload()
        missing_operator_payload["admin_members"] = ["group:mim-admins@madup.com"]
        self.assert_invalid_bootstrap(missing_operator_payload, "admin_members")

    def test_rejects_directory_runtime_service_account_drift(self) -> None:
        payload = bootstrap_payload()
        cast(dict[str, object], payload["directory"])["service_account_email"] = (
            "mim-directory-sync@mim-prod-123456.iam.gserviceaccount.com"
        )
        self.assert_invalid_bootstrap(payload, "directory.service_account_email")

    def test_rejects_binding_owner_installation_and_allowlist_drift(self) -> None:
        invalid_cases = (
            ("owner", "otherowner", "owner"),
            ("installation_id", 404, "installation_id"),
            ("repository_numeric_id", 202, "allowlist"),
        )

        for key, value, expected in invalid_cases:
            with self.subTest(key=key):
                payload = bootstrap_payload()
                binding = cast(
                    list[dict[str, object]],
                    cast(dict[str, object], payload["github_app"])["bindings"],
                )[0]
                binding[key] = value
                self.assert_invalid_bootstrap(payload, expected)

    def test_rejects_duplicate_binding_ids(self) -> None:
        payload = bootstrap_payload()
        binding = dict(
            cast(
                list[dict[str, object]],
                cast(dict[str, object], payload["github_app"])["bindings"],
            )[0]
        )
        cast(dict[str, object], payload["github_app"])["bindings"] = [
            binding,
            binding,
        ]

        self.assert_invalid_bootstrap(payload, "bindings")

    def test_rejects_non_central_build_binding_and_builder_material(self) -> None:
        payload = bootstrap_payload()
        binding = cast(
            list[dict[str, object]],
            cast(dict[str, object], payload["github_app"])["bindings"],
        )[0]
        binding["repository_resource"] = (
            "projects/other-project/locations/asia-northeast3/connections/"
            "mim-github/repositories/sample-app"
        )
        self.assert_invalid_bootstrap(payload, "repository_resource")

        payload = bootstrap_payload()
        cast(dict[str, object], payload["build"])["builder_image"] = (
            "asia-northeast3-docker.pkg.dev/mim-prod-123456/"
            "mim-platform/other-builder@sha256:" + ("c" * 64)
        )
        self.assert_invalid_bootstrap(payload, "builder_image")

        payload = bootstrap_payload()
        cast(dict[str, object], payload["build"])["build_service_account"] = (
            "projects/mim-prod-123456/serviceAccounts/"
            "builder@mim-prod-123456.iam.gserviceaccount.com"
        )
        self.assert_invalid_bootstrap(payload, "build_service_account")

    def test_rejects_non_deterministic_machine_origins(self) -> None:
        payload = bootstrap_payload()
        cast(dict[str, object], payload["deploy_worker"])["audience"] = (
            "https://mim-deploy-worker-999999999999.asia-northeast3.run.app"
        )
        self.assert_invalid_bootstrap(payload, "deploy_worker")

        payload = bootstrap_payload()
        cast(dict[str, object], payload["schedule_gateway"])["url"] = (
            "https://mim-schedule-gateway-123456789012."
            "us-central1.run.app/v1/schedules/execute"
        )
        self.assert_invalid_bootstrap(payload, "schedule_gateway")

    def test_rejects_app_edge_suffix_identity_and_route_drift(self) -> None:
        payload = bootstrap_payload()
        payload["public_host_suffix"] = "example.com"
        self.assert_invalid_bootstrap(payload, "public_host_suffix")

        payload = bootstrap_payload()
        cast(dict[str, object], payload["app_gateway"])[
            "service_account_email"
        ] = (
            "mim-app-gateway@other-project.iam.gserviceaccount.com"
        )
        self.assert_invalid_bootstrap(payload, "app_gateway")

        payload = bootstrap_payload()
        cast(dict[str, object], payload["app_authorization"])["url"] = (
            "https://mim-schedule-gateway-123456789012."
            "asia-northeast3.run.app/v1/schedules/execute"
        )
        self.assert_invalid_bootstrap(payload, "app_authorization")

    def test_rejects_invalid_app_edge_key_rotation_set(self) -> None:
        payload = bootstrap_payload()
        app_keys = cast(list[dict[str, object]], payload["app_origin_hmac_keys"])
        app_keys.append(
            {
                "key_id": "app-extra",
                "secret_version": (
                    "projects/mim-prod-123456/secrets/"
                    "mim-app-gateway-origin-extra/versions/3"
                ),
            }
        )
        self.assert_invalid_bootstrap(payload, "app_origin_hmac_keys")

        payload = bootstrap_payload()
        app_keys = cast(list[dict[str, object]], payload["app_origin_hmac_keys"])
        app_keys[1]["key_id"] = app_keys[0]["key_id"]
        self.assert_invalid_bootstrap(payload, "app_origin_hmac_keys")


def _public_parts() -> PublicRuntimeParts:
    api_app = FastAPI()

    @api_app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api_app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @api_app.get("/v1/workloads")
    async def workloads() -> dict[str, str]:
        return {"status": "api"}

    return PublicRuntimeParts(
        api_app=api_app,
        mcp_app=Starlette(
            routes=[
                Route(
                    "/mcp",
                    endpoint=lambda request: JSONResponse({"status": "mcp"}),
                    methods=["POST"],
                )
            ]
        ),
    )


def _route_paths(app: FastAPI) -> list[str]:
    return [
        route.path
        for route in app.routes
        if isinstance(route, BaseRoute) and hasattr(route, "path")
    ]


class RuntimeSelectionTests(unittest.TestCase):
    def test_private_deploy_worker_threads_breakglass_members_to_cloud_run_runtime(
        self,
    ) -> None:
        payload = bootstrap_payload()
        payload["breakglass_members"] = [
            "group:security-review@madup.com",
            "user:reviewer@madup.com",
        ]
        runtime_env = load_runtime_environment(
            environment(mode="deploy-worker"),
            dependencies=ProductionDependencies(
                metadata_credentials_loader=object,
                bootstrap_secret_loader=RecordingBootstrapLoader(
                    payload=json.dumps(
                        payload,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8"),
                ),
            ),
        )
        runtime_calls: dict[str, object] = {}
        worker_calls: dict[str, object] = {}

        def build_runtime_port(**kwargs: object) -> object:
            runtime_calls["kwargs"] = kwargs
            return "runtime-port"

        def build_worker(**kwargs: object) -> object:
            worker_calls["kwargs"] = kwargs
            return kwargs

        with (
            mock.patch(
                "google.cloud.firestore_v1.Client",
                return_value="artifacts-client",
            ),
            mock.patch(
                "google.cloud.devtools.cloudbuild_v1.CloudBuildClient",
                return_value="cloudbuild-client",
            ),
            mock.patch(
                "google.cloud.run_v2.ServicesClient",
                return_value="services-client",
            ),
            mock.patch(
                "google.cloud.run_v2.JobsClient",
                return_value="jobs-client",
            ),
            mock.patch(
                "google.cloud.run_v2.RevisionsClient",
                return_value="revisions-client",
            ),
            mock.patch(
                "google.cloud.secretmanager_v1.SecretManagerServiceClient",
                return_value="secret-client",
            ),
            mock.patch(
                "mim_control_plane.runtime._build_cloud_tasks_queue",
                return_value="queue",
            ),
            mock.patch(
                "mim_control_plane.runtime._build_github_source_port",
                return_value="source-port",
            ),
            mock.patch(
                "mim_control_plane.runtime._load_secret_bytes",
                return_value=b"signing-key",
            ),
            mock.patch(
                "mim_control_plane.adapters.google_rest.build_authorized_session",
                return_value="authorized-session",
            ),
            mock.patch(
                "mim_control_plane.adapters.artifact_registry.ArtifactRegistryAdapter",
                side_effect=lambda **kwargs: ("registry", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.cloud_build.CloudBuildAdapter",
                side_effect=lambda **kwargs: ("build", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.firestore_desired_state.FirestoreDesiredStateArtifactPort",
                side_effect=lambda **kwargs: ("artifacts", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.runtime_identity.RuntimeIdentityAdapter",
                side_effect=lambda **kwargs: ("runtime-identity", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.secret_manager.SecretManagerAdapter",
                side_effect=lambda **kwargs: ("secrets", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.cloud_run.CloudRunRuntimePort",
                side_effect=build_runtime_port,
            ),
            mock.patch(
                "mim_control_plane.workers.deploy.PrivateDeployWorker",
                side_effect=build_worker,
            ),
        ):
            built = _build_private_deploy_worker(
                bootstrap=runtime_env.bootstrap,
                store="store",
                credentials=object(),
            )

        self.assertIs(built, worker_calls["kwargs"])
        self.assertEqual(
            runtime_calls["kwargs"]["reviewed_breakglass_members"],  # type: ignore[index]
            ("group:security-review@madup.com", "user:reviewer@madup.com"),
        )
        self.assertEqual(
            worker_calls["kwargs"]["runtime"],  # type: ignore[index]
            "runtime-port",
        )

    def test_control_plane_mode_selects_only_public_builder_and_includes_exact_mcp_route(  # noqa: E501
        self,
    ) -> None:
        selected: list[str] = []

        def build_public(
            runtime_env: RuntimeEnvironment,
            _: ProductionDependencies,
        ) -> PublicRuntimeParts:
            selected.append(runtime_env.mode.value)
            return _public_parts()

        app = build_runtime_app(
            load_runtime_environment(
                environment(mode="control-plane"),
                dependencies=ProductionDependencies(
                    metadata_credentials_loader=object,
                    bootstrap_secret_loader=RecordingBootstrapLoader(),
                ),
            ),
            dependencies=ProductionDependencies(build_public_runtime_parts=build_public),
        )

        self.assertEqual(selected, ["control-plane"])
        self.assertIn("/mcp", _route_paths(app))
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            self.assertEqual(client.get("/v1/workloads").status_code, 200)
            self.assertEqual(client.post("/mcp/mcp").status_code, 404)

    def test_deploy_worker_mode_selects_only_machine_builder(self) -> None:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/internal/deploy")
        async def deploy() -> dict[str, str]:
            return {"status": "worker"}

        selected: list[str] = []

        def build_worker(
            runtime_env: RuntimeEnvironment,
            _deps: ProductionDependencies,
        ) -> FastAPI:
            selected.append(runtime_env.mode.value)
            return app

        built = build_runtime_app(
            load_runtime_environment(
                environment(mode="deploy-worker"),
                dependencies=ProductionDependencies(
                    metadata_credentials_loader=object,
                    bootstrap_secret_loader=RecordingBootstrapLoader(),
                ),
            ),
            dependencies=ProductionDependencies(
                build_deploy_worker_runtime_app=build_worker,
            ),
        )

        self.assertEqual(selected, ["deploy-worker"])
        self.assertNotIn("/mcp", _route_paths(built))
        self.assertEqual(_route_paths(built), ["/internal/deploy"])

    def test_schedule_gateway_mode_selects_only_machine_builder(self) -> None:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/v1/schedules/execute")
        async def execute() -> dict[str, str]:
            return {"status": "schedule"}

        selected: list[str] = []

        def build_schedule(
            runtime_env: RuntimeEnvironment,
            _deps: ProductionDependencies,
        ) -> FastAPI:
            selected.append(runtime_env.mode.value)
            return app

        built = build_runtime_app(
            load_runtime_environment(
                environment(mode="schedule-gateway"),
                dependencies=ProductionDependencies(
                    metadata_credentials_loader=object,
                    bootstrap_secret_loader=RecordingBootstrapLoader(),
                ),
            ),
            dependencies=ProductionDependencies(
                build_schedule_gateway_runtime_app=build_schedule,
            ),
        )

        self.assertEqual(selected, ["schedule-gateway"])
        self.assertNotIn("/mcp", _route_paths(built))
        self.assertEqual(_route_paths(built), ["/v1/schedules/execute"])

    def test_production_schedule_gateway_mounts_central_app_authorization(self) -> None:
        runtime_env = load_runtime_environment(
            environment(mode="schedule-gateway", mutations="true"),
            dependencies=ProductionDependencies(
                metadata_credentials_loader=object,
                bootstrap_secret_loader=RecordingBootstrapLoader(),
            ),
        )
        credentials = object()
        store = SimpleNamespace(list_users=lambda: ())
        captured: dict[str, object] = {}
        built_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        def capture_builder(**kwargs: object) -> FastAPI:
            captured.update(kwargs)
            return built_app

        with (
            mock.patch(
                "mim_control_plane.adapters.firestore_store.FirestoreStore",
                return_value=store,
            ),
            mock.patch(
                "mim_control_plane.runtime._build_schedule_management_service",
                return_value=object(),
            ),
            mock.patch(
                "mim_control_plane.machine_api.build_schedule_gateway_app",
                side_effect=capture_builder,
            ),
        ):
            result = _build_schedule_gateway_runtime_app(
                runtime_env,
                ProductionDependencies(
                    metadata_credentials_loader=lambda: credentials,
                    clock=lambda: NOW,
                ),
            )

        self.assertIs(result, built_app)
        self.assertEqual(
            captured["expected_app_service_account_email"],
            runtime_env.bootstrap.app_gateway.service_account_email,
        )
        authorization = cast(Any, captured["app_authorization"])
        self.assertEqual(
            authorization.__class__.__name__,
            "AppGatewayAuthorizationService",
        )
        identity_policy = authorization._identity_policy
        self.assertEqual(
            identity_policy._issuer,
            runtime_env.bootstrap.app_cloudflare_issuer,
        )
        self.assertEqual(
            identity_policy._audience,
            runtime_env.bootstrap.app_cloudflare_audience,
        )

    def test_public_builder_wires_secret_router_and_secret_management_into_mcp(
        self,
    ) -> None:
        runtime_env = load_runtime_environment(
            environment(mode="control-plane"),
            dependencies=ProductionDependencies(
                metadata_credentials_loader=object,
                bootstrap_secret_loader=RecordingBootstrapLoader(),
            ),
        )
        calls: dict[str, object] = {}

        class RecordingApp(FastAPI):
            def __init__(self) -> None:
                super().__init__(docs_url=None, redoc_url=None, openapi_url=None)
                self.included_paths: list[str] = []

            def include_router(self, router: APIRouter, **kwargs: object) -> None:
                self.included_paths.extend(
                    getattr(route, "path", "")
                    for route in router.routes
                    if getattr(route, "path", "")
                )
                super().include_router(router, **cast(Any, kwargs))

        class FakeStore:
            def __init__(self, **_: object) -> None:
                pass

            def list_users(self) -> tuple[object, ...]:
                return ()

        class FakeFirestoreClient:
            project = "mim-prod-123456"
            database = "(default)"

            def __init__(self, **_: object) -> None:
                pass

        secret_router = APIRouter()

        @secret_router.get("/v1/secrets/handoff")
        async def handoff() -> dict[str, str]:
            return {"status": "secret"}

        api_module = ModuleType("mim_control_plane.api")

        def fake_build_api_app(**kwargs: object) -> FastAPI:
            calls["api_kwargs"] = kwargs
            app = RecordingApp()
            calls["recording_app"] = app
            return app

        api_module.build_api_app = fake_build_api_app  # type: ignore[attr-defined]

        slack_repo_module = ModuleType(
            "mim_control_plane.adapters.firestore_slack_oauth"
        )
        slack_repo_module.FirestoreSlackOAuthRepository = (  # type: ignore[attr-defined]
            lambda **kwargs: SimpleNamespace(**kwargs)
        )

        store_module = ModuleType("mim_control_plane.adapters.firestore_store")
        store_module.FirestoreStore = FakeStore  # type: ignore[attr-defined]

        dashboard_module = ModuleType("mim_control_plane.dashboard")

        def fake_read_service(**kwargs: object) -> object:
            calls["dashboard_kwargs"] = kwargs
            return SimpleNamespace(**kwargs)

        dashboard_module.ControlPlaneReadService = fake_read_service  # type: ignore[attr-defined]

        mcp_module = ModuleType("mim_control_plane.mcp")

        def fake_build_mcp_server(**kwargs: object) -> object:
            calls["mcp_kwargs"] = kwargs
            return object()

        mcp_module.build_mcp_server = fake_build_mcp_server  # type: ignore[attr-defined]

        mcp_http_module = ModuleType("mim_control_plane.mcp_http")
        mcp_http_module.build_mcp_http_app = (  # type: ignore[attr-defined]
            lambda **_: Starlette(
                routes=[
                    Route(
                        "/mcp",
                        endpoint=lambda request: JSONResponse({"status": "mcp"}),
                        methods=["POST"],
                    )
                ]
            )
        )

        secret_api_module = ModuleType("mim_control_plane.secret_api")

        def fake_build_secret_router(**kwargs: object) -> APIRouter:
            calls["secret_router_kwargs"] = kwargs
            return secret_router

        secret_api_module.build_secret_router = fake_build_secret_router  # type: ignore[attr-defined]

        google_cloud_module = ModuleType("google.cloud")
        firestore_module = ModuleType("google.cloud.firestore_v1")
        firestore_module.Client = FakeFirestoreClient  # type: ignore[attr-defined]
        google_cloud_module.firestore_v1 = firestore_module  # type: ignore[attr-defined]

        with mock.patch.dict(
            sys.modules,
            {
                "google.cloud": google_cloud_module,
                "google.cloud.firestore_v1": firestore_module,
                "mim_control_plane.api": api_module,
                "mim_control_plane.adapters.firestore_slack_oauth": slack_repo_module,
                "mim_control_plane.adapters.firestore_store": store_module,
                "mim_control_plane.dashboard": dashboard_module,
                "mim_control_plane.mcp": mcp_module,
                "mim_control_plane.mcp_http": mcp_http_module,
                "mim_control_plane.secret_api": secret_api_module,
            },
        ):
            with mock.patch(
                "mim_control_plane.runtime._build_central_identity_gateway",
                return_value=object(),
            ), mock.patch(
                "mim_control_plane.runtime._build_deployment_service",
                return_value=object(),
            ), mock.patch(
                "mim_control_plane.runtime._build_schedule_management_service",
                return_value=object(),
            ), mock.patch(
                "mim_control_plane.runtime._build_secret_management_service",
                return_value="secret-service",
            ), mock.patch(
                "mim_control_plane.runtime._build_origin_verifier",
                return_value=object(),
            ):
                _build_public_runtime_parts(
                    runtime_env,
                    ProductionDependencies(
                        metadata_credentials_loader=object,
                        bootstrap_secret_loader=RecordingBootstrapLoader(),
                    ),
                )

        recording_app = cast(RecordingApp, calls["recording_app"])
        mcp_kwargs = cast(dict[str, object], calls["mcp_kwargs"])
        self.assertIn("/v1/secrets/handoff", recording_app.included_paths)
        self.assertEqual(mcp_kwargs["secret_management"], "secret-service")

    def test_public_runtime_skips_slack_repository_when_disabled(self) -> None:
        runtime_env = load_runtime_environment(
            environment(mode="control-plane"),
            dependencies=ProductionDependencies(
                metadata_credentials_loader=object,
                bootstrap_secret_loader=RecordingBootstrapLoader(
                    payload=json.dumps(
                        {
                            key: value
                            for key, value in bootstrap_payload().items()
                            if key != "slack"
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8"),
                ),
            ),
        )
        calls: dict[str, object] = {}

        class RecordingApp(FastAPI):
            pass

        class FakeStore:
            def __init__(self, **_: object) -> None:
                pass

            def list_users(self) -> tuple[object, ...]:
                return ()

        class ForbiddenFirestoreClient:
            def __init__(self, **_: object) -> None:
                raise AssertionError("slack firestore client should not be created")

        api_module = ModuleType("mim_control_plane.api")
        api_module.build_api_app = lambda **_: RecordingApp()  # type: ignore[attr-defined]
        store_module = ModuleType("mim_control_plane.adapters.firestore_store")
        store_module.FirestoreStore = FakeStore  # type: ignore[attr-defined]
        dashboard_module = ModuleType("mim_control_plane.dashboard")
        dashboard_module.ControlPlaneReadService = (  # type: ignore[attr-defined]
            lambda **kwargs: SimpleNamespace(**kwargs)
        )
        mcp_module = ModuleType("mim_control_plane.mcp")
        mcp_module.build_mcp_server = lambda **_: object()  # type: ignore[attr-defined]
        mcp_http_module = ModuleType("mim_control_plane.mcp_http")
        mcp_http_module.build_mcp_http_app = (  # type: ignore[attr-defined]
            lambda **_: Starlette(
                routes=[
                    Route(
                        "/mcp",
                        endpoint=lambda request: JSONResponse({"status": "mcp"}),
                        methods=["POST"],
                    )
                ]
            )
        )
        secret_api_module = ModuleType("mim_control_plane.secret_api")
        secret_api_module.build_secret_router = (  # type: ignore[attr-defined]
            lambda **_: APIRouter()
        )
        google_cloud_module = ModuleType("google.cloud")
        firestore_module = ModuleType("google.cloud.firestore_v1")
        firestore_module.Client = ForbiddenFirestoreClient  # type: ignore[attr-defined]
        google_cloud_module.firestore_v1 = firestore_module  # type: ignore[attr-defined]

        with mock.patch.dict(
            sys.modules,
            {
                "google.cloud": google_cloud_module,
                "google.cloud.firestore_v1": firestore_module,
                "mim_control_plane.api": api_module,
                "mim_control_plane.adapters.firestore_store": store_module,
                "mim_control_plane.dashboard": dashboard_module,
                "mim_control_plane.mcp": mcp_module,
                "mim_control_plane.mcp_http": mcp_http_module,
                "mim_control_plane.secret_api": secret_api_module,
            },
        ):
            with mock.patch(
                "mim_control_plane.runtime._build_central_identity_gateway",
                side_effect=lambda **kwargs: calls.setdefault(
                    "slack_repository", kwargs["slack_repository"]
                )
                or object(),
            ), mock.patch(
                "mim_control_plane.runtime._build_deployment_service",
                return_value=object(),
            ), mock.patch(
                "mim_control_plane.runtime._build_schedule_management_service",
                return_value=object(),
            ), mock.patch(
                "mim_control_plane.runtime._build_secret_management_service",
                return_value=object(),
            ), mock.patch(
                "mim_control_plane.runtime._build_origin_verifier",
                return_value=object(),
            ):
                _build_public_runtime_parts(
                    runtime_env,
                    ProductionDependencies(metadata_credentials_loader=object),
                )

        self.assertIsNone(calls["slack_repository"])


class FakeDocumentSnapshot:
    def __init__(self, payload: dict[str, object] | None) -> None:
        self._payload = payload
        self.exists = payload is not None

    def to_dict(self) -> dict[str, object] | None:
        return None if self._payload is None else dict(self._payload)


class FakeDocumentReference:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None

    def get(self) -> FakeDocumentSnapshot:
        return FakeDocumentSnapshot(self.payload)

    def create(self, data: dict[str, object]) -> None:
        if self.payload is not None:
            raise RuntimeError("exists")
        self.payload = dict(data)

    def set(self, data: dict[str, object]) -> None:
        self.payload = dict(data)


class FakeCollection:
    def __init__(self) -> None:
        self.documents: dict[str, FakeDocumentReference] = {}

    def document(self, document_id: str) -> FakeDocumentReference:
        return self.documents.setdefault(document_id, FakeDocumentReference())


class FakeFirestoreLedgerClient:
    def __init__(
        self,
        *,
        project: str = "mim-prod-123456",
        database: str = "(default)",
    ) -> None:
        self.project = project
        self.database = database
        self.collections: dict[str, FakeCollection] = {}

    def collection(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())


class ScheduleDispatchLedgerTests(unittest.TestCase):
    def test_rejects_non_central_firestore_client(self) -> None:
        with self.assertRaisesRegex(ValueError, "central project"):
            _FirestoreScheduleDispatchLedger(
                client=FakeFirestoreLedgerClient(project="other-project")
            )

        with self.assertRaisesRegex(ValueError, "default"):
            _FirestoreScheduleDispatchLedger(
                client=FakeFirestoreLedgerClient(database="custom")
            )

    def test_fails_closed_on_record_state_and_stable_token_drift(self) -> None:
        client = FakeFirestoreLedgerClient()
        ledger = _FirestoreScheduleDispatchLedger(client=client)
        tick_at = NOW

        claimed = cast(
            dict[str, object],
            ledger.claim(
                schedule_id="sch-1",
                tick_at=tick_at,
                stable_token="a" * 64,
            ),
        )
        self.assertEqual(claimed["state"], "claimed")

        document = client.collection("schedule_dispatch_ledger").document(
            next(iter(client.collection("schedule_dispatch_ledger").documents))
        )
        document.set(
            {
                "state": "wrong",
                "stable_token": "short",
                "run_reference": None,
            }
        )

        with self.assertRaisesRegex(ValueError, "ledger"):
            ledger.get(schedule_id="sch-1", tick_at=tick_at)


class RuntimeIdentifierTests(unittest.TestCase):
    def test_operation_ids_and_schedule_leases_use_cryptographic_randomness(
        self,
    ) -> None:
        with mock.patch(
            "mim_control_plane.runtime.secrets.token_hex",
            return_value="ab" * 12,
        ) as token_hex:
            self.assertEqual(_runtime_id("operation"), "operation-" + ("ab" * 12))
        token_hex.assert_called_once_with(12)

        with mock.patch(
            "mim_control_plane.runtime.secrets.token_urlsafe",
            return_value="lease-token-with-enough-entropy",
        ) as token_urlsafe:
            self.assertEqual(
                _runtime_lease_token(),
                "lease-token-with-enough-entropy",
            )
        token_urlsafe.assert_called_once_with(32)


if __name__ == "__main__":
    unittest.main()
