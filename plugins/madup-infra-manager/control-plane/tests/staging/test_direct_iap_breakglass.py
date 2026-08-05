from __future__ import annotations

import unittest

from tests.staging import (
    PROJECT_NUMBER_PATTERN,
    USER_EMAIL_PATTERN,
    USER_WORKLOAD_RUN_APP_PATTERN,
    require_env,
    require_private_file_env,
)


def direct_iap_probe_contract() -> dict[str, object]:
    return {
        "url_env": "MIM_STAGING_IAP_WORKLOAD_URL",
        "owner_email_env": "MIM_STAGING_WORKLOAD_OWNER_EMAIL",
        "admin_email_env": "MIM_STAGING_ADMIN_EMAIL",
        "owner_token_env": "MIM_STAGING_IAP_OWNER_ID_TOKEN_FILE",
        "admin_token_env": "MIM_STAGING_IAP_ADMIN_ID_TOKEN_FILE",
        "other_token_env": "MIM_STAGING_IAP_OTHER_ID_TOKEN_FILE",
        "anonymous_expected_statuses": (401, 403),
        "non_owner_expected_statuses": (401, 403),
        "owner_expected_status": 200,
        "admin_expected_status": 200,
    }


class DirectIapBreakglassCanaryTests(unittest.TestCase):
    def test_required_mode_demands_live_probe_inputs_instead_of_static_role_claims(
        self,
    ) -> None:
        owner = require_env(
            "MIM_STAGING_WORKLOAD_OWNER_EMAIL",
            pattern=USER_EMAIL_PATTERN,
        )
        admin = require_env("MIM_STAGING_ADMIN_EMAIL", pattern=USER_EMAIL_PATTERN)
        project_number = require_env(
            "MIM_STAGING_PROJECT_NUMBER",
            pattern=PROJECT_NUMBER_PATTERN,
        )
        workload_url = require_env(
            "MIM_STAGING_IAP_WORKLOAD_URL",
            pattern=USER_WORKLOAD_RUN_APP_PATTERN,
        )
        owner_token = require_private_file_env("MIM_STAGING_IAP_OWNER_ID_TOKEN_FILE")
        admin_token = require_private_file_env("MIM_STAGING_IAP_ADMIN_ID_TOKEN_FILE")
        other_token = require_private_file_env("MIM_STAGING_IAP_OTHER_ID_TOKEN_FILE")

        self.assertNotEqual(owner, admin)
        self.assertEqual(len(project_number), 12)
        self.assertRegex(workload_url, USER_WORKLOAD_RUN_APP_PATTERN)
        self.assertNotEqual(owner_token.read_text(encoding="utf-8").strip(), "")
        self.assertNotEqual(admin_token.read_text(encoding="utf-8").strip(), "")
        self.assertNotEqual(other_token.read_text(encoding="utf-8").strip(), "")

    def test_breakglass_requires_owner_admin_and_other_token_files(
        self,
    ) -> None:
        contract = direct_iap_probe_contract()

        self.assertEqual(contract["url_env"], "MIM_STAGING_IAP_WORKLOAD_URL")
        self.assertIn("TOKEN_FILE", contract["owner_token_env"])
        self.assertIn("TOKEN_FILE", contract["admin_token_env"])
        self.assertIn("TOKEN_FILE", contract["other_token_env"])
        self.assertEqual(contract["owner_expected_status"], 200)
        self.assertEqual(contract["admin_expected_status"], 200)
        self.assertIn(403, contract["anonymous_expected_statuses"])
        self.assertIn(403, contract["non_owner_expected_statuses"])

    def test_contract_never_routes_iap_breakglass_back_through_public_mim_origin(
        self,
    ) -> None:
        rendered = " ".join(
            str(value) for value in direct_iap_probe_contract().values()
        )

        self.assertNotIn("mim.madupai.com", rendered)
        self.assertNotIn("Cf-Access-Jwt-Assertion", rendered)
        self.assertNotIn("cookie", rendered.casefold())


if __name__ == "__main__":
    unittest.main()
