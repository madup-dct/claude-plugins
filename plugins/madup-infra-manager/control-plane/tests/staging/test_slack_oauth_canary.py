from __future__ import annotations

import unittest

from tests.staging import (
    CODE_PATTERN,
    SLACK_ENTERPRISE_ID_PATTERN,
    SLACK_REDIRECT_URI,
    SLACK_REQUIRED_SCOPES,
    SLACK_TEAM_ID_PATTERN,
    STATE_PATTERN,
    USER_EMAIL_PATTERN,
    require_env,
)


def slack_oauth_contract() -> dict[str, object]:
    return {
        "redirect_uri": SLACK_REDIRECT_URI,
        "required_scopes": SLACK_REQUIRED_SCOPES,
        "google_identity_required": True,
        "credential_storage": "metadata-only",
        "callback_single_use": True,
    }


class SlackOauthCanaryTests(unittest.TestCase):
    def test_required_mode_demands_google_identity_and_exact_slack_tenant_inputs(
        self,
    ) -> None:
        installer = require_env(
            "MIM_STAGING_GOOGLE_USER_EMAIL",
            pattern=USER_EMAIL_PATTERN,
        )
        team_id = require_env(
            "MIM_STAGING_SLACK_TEAM_ID",
            pattern=SLACK_TEAM_ID_PATTERN,
        )
        enterprise_id = require_env(
            "MIM_STAGING_SLACK_ENTERPRISE_ID",
            pattern=SLACK_ENTERPRISE_ID_PATTERN,
        )
        state = require_env("MIM_STAGING_SLACK_STATE", pattern=STATE_PATTERN)
        code = require_env("MIM_STAGING_SLACK_CODE", pattern=CODE_PATTERN)

        self.assertTrue(installer.endswith("@madup.com"))
        self.assertTrue(team_id.startswith("T"))
        self.assertTrue(enterprise_id.startswith("E"))
        self.assertNotEqual(state, code)

    def test_slack_oauth_contract_requires_google_identity_and_metadata_only_storage(
        self,
    ) -> None:
        contract = slack_oauth_contract()

        self.assertEqual(contract["redirect_uri"], SLACK_REDIRECT_URI)
        self.assertEqual(contract["required_scopes"], SLACK_REQUIRED_SCOPES)
        self.assertTrue(contract["google_identity_required"])
        self.assertEqual(contract["credential_storage"], "metadata-only")
        self.assertTrue(contract["callback_single_use"])

    def test_contract_never_expands_slack_scope_or_redirect_surface(self) -> None:
        contract = slack_oauth_contract()
        rendered = " ".join(str(value) for value in contract.values())

        self.assertNotIn("users:read.email", rendered)
        self.assertNotIn("http://", rendered)
        self.assertNotIn("/oauth/v2/access", rendered)


if __name__ == "__main__":
    unittest.main()
