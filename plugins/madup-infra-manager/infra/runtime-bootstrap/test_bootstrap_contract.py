from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONTRACT = SCRIPT_DIR / "bootstrap_contract.py"
TEMPLATE = SCRIPT_DIR / "bootstrap-input.template.json"


def valid_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "mim-prod-123456",
        "project_number": "123456789012",
        "organization_id": "123456789012",
        "billing_account_id": "ABCDEF-123456-7890AB",
        "operator_email": "operator.test@madup.com",
        "cloudflare_issuer": "https://madup.cloudflareaccess.com",
        "cloudflare_audience": "cf-aud-1234567890",
        "app_cloudflare_issuer": "https://madup.cloudflareaccess.com",
        "app_cloudflare_audience": "cf-app-aud-1234567890",
        "public_host_suffix": "madup.app",
        "region": "asia-northeast3",
        "directory_required_group_email": "mim-users@madup.com",
        "admin_members": [
            "group:mim-admins@madup.com",
            "user:operator.test@madup.com",
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
                "secret_version": (
                    "projects/mim-prod-123456/secrets/"
                    "mim-edge-origin-v1/versions/1"
                ),
            }
        ],
        "app_origin_hmac_keys": [
            {
                "key_id": "app-current",
                "secret_version": (
                    "projects/mim-prod-123456/secrets/"
                    "mim-app-gateway-origin-v1/versions/5"
                ),
            },
            {
                "key_id": "app-previous",
                "secret_version": (
                    "projects/mim-prod-123456/secrets/"
                    "mim-app-gateway-origin-v0/versions/4"
                ),
            },
        ],
        "desired_state_signing_key_id": "deploy-key-202608",
        "desired_state_signing_secret_version": (
            "projects/mim-prod-123456/secrets/"
            "mim-desired-state-signing/versions/2"
        ),
        "github_webhook_secret_version": (
            "projects/mim-prod-123456/secrets/"
            "mim-github-webhook/versions/3"
        ),
        "github_app": {
            "app_id": "123456",
            "private_key_secret_version": (
                "projects/mim-prod-123456/secrets/"
                "mim-github-app-key/versions/4"
            ),
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


class BootstrapContractCliTests(unittest.TestCase):
    def run_validate(
        self,
        *,
        payload_text: str | None = None,
        payload_dict: dict[str, object] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.json"
            canonical_path = root / "canonical.json"
            summary_path = root / "summary.json"
            if payload_text is not None:
                input_path.write_text(payload_text, encoding="utf-8")
            else:
                input_path.write_text(
                    json.dumps(payload_dict or valid_payload(), indent=2) + "\n",
                    encoding="utf-8",
                )
            return subprocess.run(
                [
                    sys.executable,
                    str(CONTRACT),
                    "validate",
                    "--input",
                    str(input_path),
                    "--canonical-output",
                    str(canonical_path),
                    "--summary-output",
                    str(summary_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

    def run_validate_and_read_outputs(
        self,
        *,
        payload_text: str | None = None,
        payload_dict: dict[str, object] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object], dict[str, object]]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.json"
            canonical_path = root / "canonical.json"
            summary_path = root / "summary.json"
            if payload_text is not None:
                input_path.write_text(payload_text, encoding="utf-8")
            else:
                input_path.write_text(
                    json.dumps(payload_dict or valid_payload(), indent=2) + "\n",
                    encoding="utf-8",
                )
            result = subprocess.run(
                [
                    sys.executable,
                    str(CONTRACT),
                    "validate",
                    "--input",
                    str(input_path),
                    "--canonical-output",
                    str(canonical_path),
                    "--summary-output",
                    str(summary_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            return result, canonical, summary

    def test_accepts_valid_payload(self) -> None:
        result = self.run_validate(payload_dict=valid_payload())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_accepts_google_only_payload_without_slack(self) -> None:
        payload = valid_payload()
        payload.pop("slack")
        result, canonical, _summary = self.run_validate_and_read_outputs(
            payload_dict=payload
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("slack", canonical)

    def test_current_template_is_google_only_default(self) -> None:
        result, canonical, _summary = self.run_validate_and_read_outputs(
            payload_text=TEMPLATE.read_text(encoding="utf-8")
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("slack", canonical)

    def test_rejects_slack_object_with_non_exact_scopes(self) -> None:
        payload = valid_payload()
        payload["slack"] = {"required_scopes": ["commands", "users:read.email"]}
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("slack.required_scopes", result.stderr)

    def test_rejects_unknown_top_level_key(self) -> None:
        payload = valid_payload()
        payload["unexpected"] = "nope"
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported keys", result.stderr)

    def test_rejects_duplicate_json_keys(self) -> None:
        result = self.run_validate(
            payload_text='{"schema_version":1,"project_id":"mim-prod-123456","project_id":"dup"}'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate JSON keys", result.stderr)

    def test_rejects_latest_secret_ref(self) -> None:
        payload = valid_payload()
        payload["desired_state_signing_secret_version"] = (
            "projects/mim-prod-123456/secrets/"
            "mim-desired-state-signing/versions/latest"
        )
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full numeric Secret Manager version", result.stderr)

    def test_rejects_unsorted_admin_members(self) -> None:
        payload = valid_payload()
        payload["admin_members"] = [
            "user:operator.test@madup.com",
            "group:mim-admins@madup.com",
        ]
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sorted", result.stderr)

    def test_rejects_wrong_company_domain(self) -> None:
        payload = valid_payload()
        payload["admin_members"] = [
            "group:mim-admins@example.com",
            "user:operator.test@madup.com",
        ]
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("@madup.com", result.stderr)

    def test_rejects_non_digest_builder_image(self) -> None:
        payload = valid_payload()
        payload["build"]["builder_image"] = (
            "asia-northeast3-docker.pkg.dev/mim-prod-123456/"
            "mim-platform/mim-builder:latest"
        )
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("builder_image", result.stderr)

    def test_missing_breakglass_members_normalizes_to_empty_list(self) -> None:
        result, canonical, summary = self.run_validate_and_read_outputs(
            payload_dict=valid_payload()
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(canonical["breakglass_members"], [])
        self.assertEqual(summary["operator_email"], "operator.test@madup.com")
        self.assertEqual(
            set(summary),
            {"input_sha256", "operator_email", "project_id"},
        )

    def test_accepts_sorted_breakglass_members(self) -> None:
        payload = valid_payload()
        payload["breakglass_members"] = [
            "group:mim-breakglass@madup.com",
            "user:operator.test@madup.com",
        ]
        result, canonical, _summary = self.run_validate_and_read_outputs(
            payload_dict=payload
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(canonical["breakglass_members"], payload["breakglass_members"])

    def test_rejects_unsorted_breakglass_members(self) -> None:
        payload = valid_payload()
        payload["breakglass_members"] = [
            "user:operator.test@madup.com",
            "group:mim-breakglass@madup.com",
        ]
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("breakglass_members must be sorted", result.stderr)

    def test_rejects_invalid_explicit_breakglass_members(self) -> None:
        invalid_values = (
            [
                "group:mim-breakglass@madup.com",
                "group:mim-breakglass@madup.com",
            ],
            ["group:mim-breakglass@example.com"],
            "group:mim-breakglass@madup.com",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                payload = valid_payload()
                payload["breakglass_members"] = value
                result = self.run_validate(payload_dict=payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("breakglass_members", result.stderr)

    def test_rejects_non_selected_repo_binding(self) -> None:
        payload = valid_payload()
        payload["github_app"]["allowed_repository_ids"] = [999]
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("repository allowlist", result.stderr)

    def test_rejects_unsafe_service_url(self) -> None:
        payload = valid_payload()
        payload["deploy_worker"]["url"] = "https://evil.example.com/internal/deploy"
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deploy_worker", result.stderr)

    def test_rejects_wrong_project(self) -> None:
        payload = valid_payload()
        payload["project_id"] = "other-project-12345"
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("central MIM project", result.stderr)

    def test_rejects_wrong_public_host_suffix(self) -> None:
        payload = valid_payload()
        payload["public_host_suffix"] = "example.com"
        result = self.run_validate(payload_dict=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("public_host_suffix", result.stderr)


if __name__ == "__main__":
    unittest.main()
