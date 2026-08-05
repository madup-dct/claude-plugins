from __future__ import annotations

import unittest
from pathlib import Path

from tests.staging import (
    CONTROL_PLANE_SERVICE,
    DEPLOY_WORKER_SERVICE,
    PROJECT_ID,
    PROJECT_NUMBER_PATTERN,
    RUN_APP_AUDIENCE_PATTERN,
    RUNTIME_SERVICE_ACCOUNT_PATTERN,
    SCHEDULE_GATEWAY_SERVICE,
    require_env,
)

TEST_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = TEST_ROOT.parents[2]
AUDIT_SCRIPT = PLUGIN_ROOT / "infra" / "control-plane" / "audit_iam.sh"


def runtime_boundary_contract() -> tuple[dict[str, str], ...]:
    return (
        {
            "service": CONTROL_PLANE_SERVICE,
            "service_account": (
                f"{CONTROL_PLANE_SERVICE}@{PROJECT_ID}.iam.gserviceaccount.com"
            ),
            "ingress": "all",
            "auth_boundary": "cloudflare-access-and-worker-hmac",
            "min_instances": "0",
            "max_instances": "1",
        },
        {
            "service": DEPLOY_WORKER_SERVICE,
            "service_account": (
                f"{DEPLOY_WORKER_SERVICE}@{PROJECT_ID}.iam.gserviceaccount.com"
            ),
            "ingress": "internal",
            "auth_boundary": "exact-run-invoker-only",
            "audience": "https://mim-deploy-worker-123456789012.asia-northeast3.run.app",
        },
        {
            "service": SCHEDULE_GATEWAY_SERVICE,
            "service_account": (
                f"{SCHEDULE_GATEWAY_SERVICE}@{PROJECT_ID}.iam.gserviceaccount.com"
            ),
            "ingress": "internal",
            "auth_boundary": "exact-run-invoker-only",
            "audience": "https://mim-schedule-gateway-123456789012.asia-northeast3.run.app",
        },
    )


class RuntimeIamCanaryTests(unittest.TestCase):
    def test_required_mode_demands_exact_project_and_runtime_identity_inputs(
        self,
    ) -> None:
        self.assertEqual(
            require_env("MIM_STAGING_PROJECT_ID", exact=PROJECT_ID),
            PROJECT_ID,
        )
        project_number = require_env(
            "MIM_STAGING_PROJECT_NUMBER",
            pattern=PROJECT_NUMBER_PATTERN,
        )
        control_plane = require_env(
            "MIM_STAGING_CONTROL_PLANE_SA",
            pattern=RUNTIME_SERVICE_ACCOUNT_PATTERN,
        )
        deploy_worker = require_env(
            "MIM_STAGING_DEPLOY_WORKER_AUDIENCE",
            pattern=RUN_APP_AUDIENCE_PATTERN,
        )

        self.assertEqual(len(project_number), 12)
        self.assertIn(
            "@mim-prod-123456.iam.gserviceaccount.com",
            control_plane,
        )
        self.assertIn(".run.app", deploy_worker)

    def test_runtime_boundary_matches_cloudflare_and_private_services(
        self,
    ) -> None:
        matrix = runtime_boundary_contract()

        self.assertEqual(
            [item["service"] for item in matrix],
            [
                CONTROL_PLANE_SERVICE,
                DEPLOY_WORKER_SERVICE,
                SCHEDULE_GATEWAY_SERVICE,
            ],
        )
        self.assertEqual(matrix[0]["ingress"], "all")
        self.assertEqual(
            matrix[0]["auth_boundary"],
            "cloudflare-access-and-worker-hmac",
        )
        self.assertEqual(matrix[1]["ingress"], "internal")
        self.assertEqual(matrix[2]["ingress"], "internal")
        self.assertNotIn("iap", matrix[0]["auth_boundary"])
        self.assertRegex(matrix[1]["audience"], RUN_APP_AUDIENCE_PATTERN)
        self.assertRegex(matrix[2]["audience"], RUN_APP_AUDIENCE_PATTERN)

    def test_runtime_iam_canary_executes_the_reviewed_read_only_audit_script(
        self,
    ) -> None:
        content = AUDIT_SCRIPT.read_text(encoding="utf-8")

        self.assertTrue(AUDIT_SCRIPT.exists())
        self.assertIn("projects get-iam-policy", content)
        self.assertIn("run services describe", content)
        self.assertNotIn("add-iam-policy-binding", content)
        self.assertNotIn("set-iam-policy", content)
        self.assertNotIn("run services update", content)
        self.assertIn('--project="$MIM_PROJECT_ID"', content)
        self.assertIn('--region="$MIM_FIXED_REGION"', content)
        self.assertIn("Project-wide Cloud Run invoker bindings are forbidden", content)


if __name__ == "__main__":
    unittest.main()
