from __future__ import annotations

import stat
import unittest
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = TEST_ROOT.parents[1]
SMOKE_SCRIPT = PLUGIN_ROOT / "infra" / "release" / "smoke_test.sh"
VERIFY_SCRIPT = PLUGIN_ROOT / "infra" / "release" / "verify.sh"


class StagingCanaryContractTests(unittest.TestCase):
    def test_smoke_script_exists_and_is_owner_executable(self) -> None:
        self.assertTrue(SMOKE_SCRIPT.exists())
        self.assertTrue(SMOKE_SCRIPT.stat().st_mode & stat.S_IXUSR)

    def test_smoke_uses_cookie_files_without_client_access_jwt(
        self,
    ) -> None:
        content = SMOKE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("--read-only", content)
        self.assertIn("--public-app", content)
        self.assertIn("MIM_STAGING_CF_AUTHORIZATION_FILE", content)
        self.assertIn("CF_Authorization=", content)
        self.assertNotIn("Cf-Access-Jwt-Assertion", content)
        self.assertNotIn("location", content)
        self.assertIn("MIM_STAGING_CONTROL_PLANE_RUN_APP_URL", content)

    def test_smoke_separates_browser_and_direct_origin_checks(
        self,
    ) -> None:
        content = SMOKE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("/healthz", content)
        self.assertIn("/readyz", content)
        self.assertIn("tools/list", content)
        self.assertIn("direct-origin-without-worker-hmac-denied", content)
        self.assertIn("direct-origin-readyz-without-worker-hmac-denied", content)
        self.assertNotIn("plan_deploy", content)
        self.assertNotIn("tools/call", content)

    def test_smoke_pins_direct_origins_to_the_reviewed_project_number(self) -> None:
        content = SMOKE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("MIM_STAGING_PROJECT_NUMBER", content)
        self.assertIn(
            'deterministic_run_app_origin "mim-control-plane" "${project_number}"',
            content,
        )
        self.assertIn(
            'deterministic_run_app_origin "mim-app-gateway" "${project_number}"',
            content,
        )
        self.assertIn(
            "require_exact_env MIM_STAGING_CONTROL_PLANE_RUN_APP_URL",
            content,
        )
        self.assertIn("require_exact_env MIM_STAGING_APP_GATEWAY_RUN_APP_URL", content)
        self.assertIn("mim-svc-[0-9a-f]{12}-${project_number}", content)

    def test_private_workload_canary_requires_cloud_run_iam_denial_evidence(
        self,
    ) -> None:
        content = SMOKE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("require_cloud_run_iam_denial", content)
        self.assertIn("Google Frontend", content)
        self.assertIn("www-authenticate", content.lower())
        self.assertIn("Your client does not have permission", content)
        self.assertIn("The request was not authenticated", content)
        self.assertIn('-- "${url}"', content)

    def test_verify_script_runs_staging_contracts_locally_then_live_audit_and_smokes(
        self,
    ) -> None:
        verify_content = VERIFY_SCRIPT.read_text(encoding="utf-8")
        smoke_content = SMOKE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("run_staging_contracts", verify_content)
        self.assertIn("run_iam_policy_diff", verify_content)
        self.assertIn("run_authenticated_readonly_smoke", verify_content)
        self.assertIn("run_public_app_live", verify_content)
        self.assertIn("public app live canary prerequisites missing", verify_content)
        self.assertIn("direct-origin-denial", verify_content)
        self.assertIn("runtime-iam-canary", verify_content)
        self.assertIn('"401,403"', smoke_content)
        self.assertNotIn('"401,403,404"', smoke_content)
        self.assertIn("grep -Fqx 'Request denied.'", smoke_content)
        self.assertIn("app-gateway-direct-anonymous-denied", smoke_content)


if __name__ == "__main__":
    unittest.main()
