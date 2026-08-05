"""Tests for control-plane startup configuration loading.

Settings.from_mapping accepts only explicit operator startup inputs. It must
not infer cloud identifiers from ambient state or permit policy overrides
through extra MIM_* environment keys.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_fakes = importlib.import_module("fakes")
_config = importlib.import_module("mim_control_plane.config")

FAKE_STARTUP_CONFIG = _fakes.FAKE_STARTUP_CONFIG
FAKE_DIRECTORY_RUNTIME_CONFIG = _fakes.FAKE_DIRECTORY_RUNTIME_CONFIG
build_startup_mapping = _fakes.build_startup_mapping
build_directory_runtime_mapping = _fakes.build_directory_runtime_mapping
ConfigError = _config.ConfigError
DirectoryRuntimeSettings = _config.DirectoryRuntimeSettings
Settings = _config.Settings


class SettingsFromMappingTest(unittest.TestCase):
    def test_public_settings_allow_supported_private_directory_runtime_keys(
        self,
    ) -> None:
        settings = Settings.from_mapping(build_directory_runtime_mapping())

        self.assertEqual(settings.project_id, FAKE_STARTUP_CONFIG["MIM_PROJECT_ID"])
        self.assertEqual(
            settings.operator_email,
            FAKE_STARTUP_CONFIG["MIM_OPERATOR_EMAIL"],
        )

    def test_loads_required_operator_inputs_and_fixed_platform_policy(self) -> None:
        settings = Settings.from_mapping(build_startup_mapping())

        self.assertEqual(settings.project_id, FAKE_STARTUP_CONFIG["MIM_PROJECT_ID"])
        self.assertEqual(
            settings.organization_id,
            FAKE_STARTUP_CONFIG["MIM_ORGANIZATION_ID"],
        )
        self.assertEqual(
            settings.billing_account_id,
            FAKE_STARTUP_CONFIG["MIM_BILLING_ACCOUNT_ID"],
        )
        self.assertEqual(
            settings.operator_email,
            FAKE_STARTUP_CONFIG["MIM_OPERATOR_EMAIL"],
        )
        self.assertEqual(
            settings.cloudflare_issuer,
            FAKE_STARTUP_CONFIG["MIM_CLOUDFLARE_ISSUER"],
        )
        self.assertEqual(
            settings.cloudflare_audience,
            FAKE_STARTUP_CONFIG["MIM_CLOUDFLARE_AUDIENCE"],
        )
        self.assertEqual(settings.region, "asia-northeast3")
        self.assertEqual(settings.github_owner, "madupmarketing")
        self.assertEqual(settings.company_domain, "madup.com")
        self.assertEqual(settings.public_origin, "https://mim.madup.app")
        self.assertEqual(settings.mcp_url, "https://mim.madup.app/mcp")
        self.assertEqual(settings.app_host_suffix, "madup.app")
        self.assertEqual(settings.timezone, "Asia/Seoul")
        self.assertEqual(settings.identity_max_staleness_minutes, 60)
        self.assertEqual(settings.plan_expiry_minutes, 15)
        self.assertEqual(settings.origin_hmac_window_seconds, 60)
        self.assertEqual(settings.per_user_service_limit, 2)
        self.assertEqual(settings.per_user_schedule_limit, 3)
        self.assertEqual(settings.default_secret_limit, 5)
        self.assertEqual(settings.hard_secret_limit, 10)
        self.assertEqual(settings.service_cpu, 1)
        self.assertEqual(settings.service_memory_mib, 512)
        self.assertEqual(settings.service_min_instances, 0)
        self.assertEqual(settings.service_max_instances, 1)
        self.assertEqual(settings.target_monthly_budget_krw, 1000)
        self.assertEqual(settings.admin_budget_ceiling_krw, 10000)
        self.assertEqual(settings.transfer_grace_days, 7)
        self.assertEqual(settings.inactivity_warning_days, 23)
        self.assertEqual(settings.cleanup_days, 30)
        self.assertEqual(settings.final_image_retention_days, 30)
        self.assertEqual(settings.pilot_max_identities, 50)
        self.assertEqual(settings.firestore_database_id, "(default)")

    def test_settings_are_immutable(self) -> None:
        settings = Settings.from_mapping(build_startup_mapping())

        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.project_id = "mutated-project"

    def test_direct_constructor_only_accepts_operator_specific_inputs(self) -> None:
        signature = inspect.signature(Settings)
        expected_init_fields = (
            "project_id",
            "organization_id",
            "billing_account_id",
            "operator_email",
            "cloudflare_issuer",
            "cloudflare_audience",
        )
        self.assertEqual(tuple(signature.parameters), expected_init_fields)

        dataclass_fields = dataclasses.fields(Settings)
        self.assertEqual(
            tuple(field.name for field in dataclass_fields if field.init),
            expected_init_fields,
        )
        self.assertTrue(
            all(not field.init for field in dataclass_fields[6:]),
        )

        with self.assertRaises(TypeError):
            Settings(
                project_id=FAKE_STARTUP_CONFIG["MIM_PROJECT_ID"],
                organization_id=FAKE_STARTUP_CONFIG["MIM_ORGANIZATION_ID"],
                billing_account_id=FAKE_STARTUP_CONFIG["MIM_BILLING_ACCOUNT_ID"],
                operator_email=FAKE_STARTUP_CONFIG["MIM_OPERATOR_EMAIL"],
                cloudflare_issuer=FAKE_STARTUP_CONFIG["MIM_CLOUDFLARE_ISSUER"],
                cloudflare_audience=FAKE_STARTUP_CONFIG["MIM_CLOUDFLARE_AUDIENCE"],
                region="europe-west1",
            )

        with self.assertRaises(TypeError):
            Settings(
                project_id=FAKE_STARTUP_CONFIG["MIM_PROJECT_ID"],
                organization_id=FAKE_STARTUP_CONFIG["MIM_ORGANIZATION_ID"],
                billing_account_id=FAKE_STARTUP_CONFIG["MIM_BILLING_ACCOUNT_ID"],
                operator_email=FAKE_STARTUP_CONFIG["MIM_OPERATOR_EMAIL"],
                cloudflare_issuer=FAKE_STARTUP_CONFIG["MIM_CLOUDFLARE_ISSUER"],
                cloudflare_audience=FAKE_STARTUP_CONFIG["MIM_CLOUDFLARE_AUDIENCE"],
                service_max_instances=2,
            )

    def test_direct_constructor_validates_and_redacts_operator_specific_inputs(
        self,
    ) -> None:
        cases = (
            ("project_id", "example-project", "MIM_PROJECT_ID"),
            ("organization_id", "org-placeholder", "MIM_ORGANIZATION_ID"),
            ("billing_account_id", "billing-account", "MIM_BILLING_ACCOUNT_ID"),
            ("operator_email", "operator@example.com", "MIM_OPERATOR_EMAIL"),
            ("cloudflare_issuer", "https://madup.example.com", "MIM_CLOUDFLARE_ISSUER"),
            ("cloudflare_audience", "audience with spaces", "MIM_CLOUDFLARE_AUDIENCE"),
            ("project_id", 12345, "MIM_PROJECT_ID"),
            ("organization_id", 123456789012, "MIM_ORGANIZATION_ID"),
            ("billing_account_id", 7, "MIM_BILLING_ACCOUNT_ID"),
            ("operator_email", object(), "MIM_OPERATOR_EMAIL"),
            ("cloudflare_issuer", object(), "MIM_CLOUDFLARE_ISSUER"),
            ("cloudflare_audience", object(), "MIM_CLOUDFLARE_AUDIENCE"),
        )

        for field_name, bad_value, expected_key in cases:
            with self.subTest(field_name=field_name, bad_value=bad_value):
                kwargs = {
                    "project_id": FAKE_STARTUP_CONFIG["MIM_PROJECT_ID"],
                    "organization_id": FAKE_STARTUP_CONFIG["MIM_ORGANIZATION_ID"],
                    "billing_account_id": FAKE_STARTUP_CONFIG["MIM_BILLING_ACCOUNT_ID"],
                    "operator_email": FAKE_STARTUP_CONFIG["MIM_OPERATOR_EMAIL"],
                    "cloudflare_issuer": FAKE_STARTUP_CONFIG[
                        "MIM_CLOUDFLARE_ISSUER"
                    ],
                    "cloudflare_audience": FAKE_STARTUP_CONFIG[
                        "MIM_CLOUDFLARE_AUDIENCE"
                    ],
                }
                kwargs[field_name] = bad_value

                with self.assertRaises(ConfigError) as context:
                    Settings(**kwargs)

                message = str(context.exception)
                self.assertIn(expected_key, message)
                self.assertNotIn(str(bad_value), message)

    def test_direct_constructor_normalizes_validated_cloudflare_issuer(self) -> None:
        settings = Settings(
            project_id=FAKE_STARTUP_CONFIG["MIM_PROJECT_ID"],
            organization_id=FAKE_STARTUP_CONFIG["MIM_ORGANIZATION_ID"],
            billing_account_id=FAKE_STARTUP_CONFIG["MIM_BILLING_ACCOUNT_ID"],
            operator_email=FAKE_STARTUP_CONFIG["MIM_OPERATOR_EMAIL"],
            cloudflare_issuer="https://MADUP.cloudflareaccess.com/",
            cloudflare_audience=FAKE_STARTUP_CONFIG["MIM_CLOUDFLARE_AUDIENCE"],
        )

        self.assertEqual(settings.cloudflare_issuer, "https://madup.cloudflareaccess.com")

    def test_ignores_unrelated_non_mim_keys(self) -> None:
        mapping = build_startup_mapping(
            PATH="/usr/bin",
            GOOGLE_CLOUD_PROJECT="ambient-project",
        )

        settings = Settings.from_mapping(mapping)

        self.assertEqual(settings.project_id, FAKE_STARTUP_CONFIG["MIM_PROJECT_ID"])

    def test_requires_only_the_operator_startup_keys(self) -> None:
        required_keys = (
            "MIM_PROJECT_ID",
            "MIM_ORGANIZATION_ID",
            "MIM_BILLING_ACCOUNT_ID",
            "MIM_OPERATOR_EMAIL",
            "MIM_CLOUDFLARE_ISSUER",
            "MIM_CLOUDFLARE_AUDIENCE",
        )

        for key in required_keys:
            with self.subTest(key=key):
                mapping = build_startup_mapping()
                mapping.pop(key)
                with self.assertRaisesRegex(ConfigError, key):
                    Settings.from_mapping(mapping)

    def test_rejects_unknown_legacy_and_override_mim_keys(self) -> None:
        cases = (
            "MIM_UNSUPPORTED_FLAG",
            "MIM_GCP_PROJECT_ID",
            "MIM_GCP_REGION",
            "MIM_DEFAULT_ACTIVE_SERVICES",
        )

        for key in cases:
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigError, key):
                    Settings.from_mapping(build_startup_mapping(**{key: "1"}))

    def test_rejects_placeholder_or_malformed_operator_identifiers(self) -> None:
        cases = (
            ("MIM_PROJECT_ID", "example-project", "MIM_PROJECT_ID"),
            ("MIM_PROJECT_ID", "UPPERCASE", "MIM_PROJECT_ID"),
            ("MIM_ORGANIZATION_ID", "org-placeholder", "MIM_ORGANIZATION_ID"),
            (
                "MIM_BILLING_ACCOUNT_ID",
                "billing-account",
                "MIM_BILLING_ACCOUNT_ID",
            ),
            ("MIM_OPERATOR_EMAIL", "operator@example.com", "MIM_OPERATOR_EMAIL"),
        )

        for key, value, expected_message in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ConfigError, expected_message):
                    Settings.from_mapping(build_startup_mapping(**{key: value}))

    def test_mapping_rejects_non_string_operator_inputs(self) -> None:
        cases = (
            ("MIM_PROJECT_ID", 12345),
            ("MIM_ORGANIZATION_ID", 123456789012),
            ("MIM_BILLING_ACCOUNT_ID", 7),
            ("MIM_OPERATOR_EMAIL", object()),
            ("MIM_CLOUDFLARE_ISSUER", object()),
            ("MIM_CLOUDFLARE_AUDIENCE", object()),
        )

        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigError) as context:
                    Settings.from_mapping(build_startup_mapping(**{key: value}))

                message = str(context.exception)
                self.assertIn(key, message)
                self.assertNotIn(str(value), message)

    def test_rejects_non_https_or_unsafe_cloudflare_issuer(self) -> None:
        issuers = (
            "http://madup.cloudflareaccess.com",
            "https://madup.example.com",
            "https://example.cloudflareaccess.com/path",
            "https://placeholder.cloudflareaccess.com",
            "https://madup.cloudflareaccess.com:443",
            "https://madup.cloudflareaccess.com:8443",
            "https://madup.cloudflareaccess.com:abc",
        )

        for issuer in issuers:
            with self.subTest(issuer=issuer):
                with self.assertRaisesRegex(ConfigError, "MIM_CLOUDFLARE_ISSUER"):
                    Settings.from_mapping(
                        build_startup_mapping(MIM_CLOUDFLARE_ISSUER=issuer),
                    )

    def test_rejects_malformed_cloudflare_audience(self) -> None:
        audiences = ("", "   ", "audience with spaces", "audience/with/slash")

        for audience in audiences:
            with self.subTest(audience=audience):
                with self.assertRaisesRegex(
                    ConfigError,
                    "MIM_CLOUDFLARE_AUDIENCE",
                ):
                    Settings.from_mapping(
                        build_startup_mapping(MIM_CLOUDFLARE_AUDIENCE=audience),
                    )

    def test_does_not_infer_ambient_gcloud_defaults(self) -> None:
        mapping = build_startup_mapping()
        mapping.pop("MIM_PROJECT_ID")

        with mock.patch.dict(
            os.environ,
            {
                "GOOGLE_CLOUD_PROJECT": "ambient-project",
                "CLOUDSDK_CORE_PROJECT": "ambient-project",
                "GOOGLE_CLOUD_QUOTA_PROJECT": "ambient-billing-project",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ConfigError, "MIM_PROJECT_ID"):
                Settings.from_mapping(mapping)

    def test_config_errors_redact_supplied_values(self) -> None:
        supplied_email = "operator@example.com"

        with self.assertRaises(ConfigError) as context:
            Settings.from_mapping(build_startup_mapping(MIM_OPERATOR_EMAIL=supplied_email))

        message = str(context.exception)
        self.assertIn("MIM_OPERATOR_EMAIL", message)
        self.assertNotIn(supplied_email, message)


class DirectoryRuntimeSettingsFromMappingTest(unittest.TestCase):
    def test_requires_only_operator_and_directory_runtime_inputs(self) -> None:
        settings = DirectoryRuntimeSettings.from_mapping(
            {
                "MIM_OPERATOR_EMAIL": FAKE_STARTUP_CONFIG["MIM_OPERATOR_EMAIL"],
                "MIM_DIRECTORY_ADMIN_SUBJECT": FAKE_DIRECTORY_RUNTIME_CONFIG[
                    "MIM_DIRECTORY_ADMIN_SUBJECT"
                ],
                "MIM_DIRECTORY_SERVICE_ACCOUNT_EMAIL": FAKE_DIRECTORY_RUNTIME_CONFIG[
                    "MIM_DIRECTORY_SERVICE_ACCOUNT_EMAIL"
                ],
                "MIM_DIRECTORY_REQUIRED_GROUP_EMAIL": FAKE_DIRECTORY_RUNTIME_CONFIG[
                    "MIM_DIRECTORY_REQUIRED_GROUP_EMAIL"
                ],
            }
        )

        self.assertEqual(
            settings.directory_admin_subject,
            FAKE_DIRECTORY_RUNTIME_CONFIG["MIM_DIRECTORY_ADMIN_SUBJECT"],
        )

    def test_loads_private_directory_runtime_inputs(self) -> None:
        settings = DirectoryRuntimeSettings.from_mapping(
            build_directory_runtime_mapping(),
        )

        self.assertEqual(
            settings.operator_email,
            FAKE_STARTUP_CONFIG["MIM_OPERATOR_EMAIL"],
        )
        self.assertEqual(
            settings.directory_admin_subject,
            FAKE_DIRECTORY_RUNTIME_CONFIG["MIM_DIRECTORY_ADMIN_SUBJECT"],
        )
        self.assertEqual(
            settings.directory_service_account_email,
            FAKE_DIRECTORY_RUNTIME_CONFIG["MIM_DIRECTORY_SERVICE_ACCOUNT_EMAIL"],
        )
        self.assertEqual(
            settings.directory_required_group_email,
            FAKE_DIRECTORY_RUNTIME_CONFIG["MIM_DIRECTORY_REQUIRED_GROUP_EMAIL"],
        )

    def test_settings_are_immutable(self) -> None:
        settings = DirectoryRuntimeSettings.from_mapping(
            build_directory_runtime_mapping(),
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.directory_admin_subject = "mutated@madup.com"

    def test_rejects_unknown_directory_runtime_key_and_redacts_supplied_value(
        self,
    ) -> None:
        with self.assertRaises(ConfigError) as context:
            DirectoryRuntimeSettings.from_mapping(
                build_directory_runtime_mapping(
                    MIM_DIRECTORY_UNKNOWN="leak-me",
                )
            )

        message = str(context.exception)
        self.assertIn("MIM_DIRECTORY_UNKNOWN", message)
        self.assertNotIn("leak-me", message)

    def test_requires_directory_admin_subject_distinct_from_operator_email(
        self,
    ) -> None:
        with self.assertRaises(ConfigError) as context:
            DirectoryRuntimeSettings.from_mapping(
                build_directory_runtime_mapping(
                    MIM_DIRECTORY_ADMIN_SUBJECT="Operator.Test@madup.com",
                )
            )

        message = str(context.exception)
        self.assertIn("MIM_DIRECTORY_ADMIN_SUBJECT", message)
        self.assertNotIn("Operator.Test@madup.com", message)

    def test_rejects_placeholder_or_non_madup_directory_values(self) -> None:
        cases = (
            ("MIM_DIRECTORY_ADMIN_SUBJECT", "admin@example.com"),
            ("MIM_DIRECTORY_REQUIRED_GROUP_EMAIL", "mim-users@example.com"),
            (
                "MIM_DIRECTORY_SERVICE_ACCOUNT_EMAIL",
                "service-account@example.com",
            ),
            ("MIM_DIRECTORY_SERVICE_ACCOUNT_EMAIL", "example-service-account"),
        )

        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigError) as context:
                    DirectoryRuntimeSettings.from_mapping(
                        build_directory_runtime_mapping(**{key: value}),
                    )

                message = str(context.exception)
                self.assertIn(key, message)
                self.assertNotIn(str(value), message)

    def test_repr_redacts_sensitive_directory_runtime_values(self) -> None:
        settings = DirectoryRuntimeSettings.from_mapping(
            build_directory_runtime_mapping(),
        )

        rendered = repr(settings)

        self.assertIn("DirectoryRuntimeSettings(", rendered)
        self.assertNotIn("madup.com", rendered)
        self.assertNotIn("gserviceaccount.com", rendered)
        self.assertNotIn("directory_admin_subject", rendered)


if __name__ == "__main__":
    unittest.main()
