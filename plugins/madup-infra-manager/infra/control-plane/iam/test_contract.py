#!/usr/bin/env python3
"""Focused regression tests for the fixed Secret Manager IAM contract."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("contract.py")
MODULE_SPEC = importlib.util.spec_from_file_location("mim_iam_contract", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
CONTRACT = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(CONTRACT)

PROJECT_ID = "mim-prod-123456"
PROJECT_NUMBER = "987654321012"
OPERATOR_EMAIL = "operator.test@madup.com"

SERVICE_ACCOUNT_ENV = {
    "mim-build": "MIM_IAM_BUILD_POLICY_FILE",
    "mim-control-plane": "MIM_IAM_CONTROL_PLANE_POLICY_FILE",
    "mim-app-gateway": "MIM_IAM_APP_GATEWAY_POLICY_FILE",
    "mim-deploy-worker": "MIM_IAM_DEPLOY_WORKER_POLICY_FILE",
    "mim-identity-sync": "MIM_IAM_IDENTITY_SYNC_POLICY_FILE",
    "mim-maintenance": "MIM_IAM_MAINTENANCE_POLICY_FILE",
    "mim-release": "MIM_IAM_RELEASE_POLICY_FILE",
    "mim-schedule-gateway": "MIM_IAM_SCHEDULE_GATEWAY_POLICY_FILE",
}


class ContractTest(unittest.TestCase):
    def _base_env(self) -> dict[str, str]:
        return {
            "MIM_PROJECT_ID": PROJECT_ID,
            "MIM_PROJECT_NUMBER": PROJECT_NUMBER,
            "MIM_OPERATOR_EMAIL": OPERATOR_EMAIL,
        }

    def _write_json(self, directory: Path, name: str, payload: dict[str, object]) -> str:
        path = directory / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def _configure_evaluate_env(
        self,
        directory: Path,
        *,
        include_optional_previous_secret: bool,
        optional_previous_secret_policy: dict[str, object] | None = None,
    ) -> None:
        expected = CONTRACT.expected_contract()
        os.environ["MIM_IAM_PROJECT_POLICY_FILE"] = self._write_json(
            directory,
            "project-iam.json",
            {"bindings": expected["project_bindings"]},
        )
        os.environ["MIM_IAM_ARTIFACT_REPOSITORY_POLICY_FILE"] = self._write_json(
            directory,
            "artifact-repository-iam.json",
            {"bindings": [expected["artifact_repository_bindings"][0]]},
        )
        os.environ["MIM_IAM_BILLING_DATASET_FILE"] = self._write_json(
            directory,
            "billing-export.json",
            {
                "datasetReference": {
                    "projectId": PROJECT_ID,
                    "datasetId": "mim_billing_export",
                },
                "access": [],
            },
        )

        for resource_email, bindings in expected["service_account_bindings"].items():
            local_name = resource_email.split("@", 1)[0]
            env_name = SERVICE_ACCOUNT_ENV[local_name]
            os.environ[env_name] = self._write_json(
                directory,
                f"{local_name}-iam.json",
                {"bindings": bindings},
            )

        optional_resources = set(expected["optional_secret_resources"])
        manifest_lines: list[str] = []
        for resource_name, bindings in expected["secret_resource_bindings"].items():
            if resource_name in optional_resources and not include_optional_previous_secret:
                continue
            secret_id = resource_name.split("/secrets/", 1)[1]
            payload = {"bindings": bindings}
            if resource_name in optional_resources and optional_previous_secret_policy is not None:
                payload = optional_previous_secret_policy
            policy_path = self._write_json(
                directory,
                f"{secret_id}-iam.json",
                payload,
            )
            manifest_lines.append(
                "\t".join(
                    [
                        secret_id,
                        "optional" if resource_name in optional_resources else "required",
                        policy_path,
                    ]
                )
            )
        manifest_path = directory / "secret-policies.tsv"
        manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
        os.environ["MIM_IAM_SECRET_POLICIES_TSV_FILE"] = str(manifest_path)

    def test_expected_contract_uses_fixed_secret_set_and_mim_sec_prefix(self) -> None:
        with patch.dict(os.environ, self._base_env(), clear=False):
            expected = CONTRACT.expected_contract()

        secret_admin_binding = next(
            binding
            for binding in expected["project_bindings"]
            if binding["role"] == "roles/secretmanager.admin"
        )
        self.assertEqual(
            secret_admin_binding["condition"]["expression"],
            'resource.name.startsWith("projects/mim-prod-123456/secrets/mim-sec-")',
        )
        self.assertEqual(
            set(expected["secret_resource_bindings"]),
            {
                f"projects/{PROJECT_ID}/secrets/mim-runtime-bootstrap",
                f"projects/{PROJECT_ID}/secrets/mim-edge-origin-v1",
                f"projects/{PROJECT_ID}/secrets/mim-app-gateway-origin-v1",
                f"projects/{PROJECT_ID}/secrets/mim-app-gateway-origin-v0",
                f"projects/{PROJECT_ID}/secrets/mim-desired-state-signing",
                f"projects/{PROJECT_ID}/secrets/mim-github-webhook",
                f"projects/{PROJECT_ID}/secrets/mim-github-app-key",
            },
        )
        self.assertEqual(
            expected["optional_secret_resources"],
            [f"projects/{PROJECT_ID}/secrets/mim-app-gateway-origin-v0"],
        )

    def test_evaluate_allows_missing_optional_previous_app_gateway_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict(os.environ, self._base_env(), clear=False):
                self._configure_evaluate_env(
                    Path(tmp_dir),
                    include_optional_previous_secret=False,
                )
                result = CONTRACT.evaluate()

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["actions"], [])
        self.assertNotIn(
            f"projects/{PROJECT_ID}/secrets/mim-app-gateway-origin-v0",
            result["observed"]["secret_resources"],
        )

    def test_evaluate_blocks_when_optional_previous_secret_exists_with_member_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict(os.environ, self._base_env(), clear=False):
                self._configure_evaluate_env(
                    Path(tmp_dir),
                    include_optional_previous_secret=True,
                    optional_previous_secret_policy={
                        "bindings": [
                            {
                                "role": "roles/secretmanager.secretAccessor",
                                "members": [
                                    f"serviceAccount:mim-app-gateway@{PROJECT_ID}.iam.gserviceaccount.com",
                                    f"serviceAccount:mim-control-plane@{PROJECT_ID}.iam.gserviceaccount.com",
                                ],
                            }
                        ]
                    },
                )
                result = CONTRACT.evaluate()

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            {
                "code": f"projects/{PROJECT_ID}/secrets/mim-app-gateway-origin-v0-secret-resource-members-drift",
                "message": (
                    f"Secret resource projects/{PROJECT_ID}/secrets/mim-app-gateway-origin-v0 "
                    "binding roles/secretmanager.secretAccessor members drifted"
                ),
            },
            result["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
