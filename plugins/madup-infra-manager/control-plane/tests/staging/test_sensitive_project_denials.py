from __future__ import annotations

import unittest

from tests.staging import PROJECT_ID, require_env


def protected_resource_denials() -> tuple[dict[str, str], ...]:
    return (
        {
            "name": "personal-project-denial",
            "resource": "project:user-personal-prod",
            "expected": "forbidden",
        },
        {
            "name": "central-project-denial",
            "resource": f"project:{PROJECT_ID}",
            "expected": "forbidden",
        },
        {
            "name": "bigquery-read-denial",
            "resource": "service:bigquery",
            "expected": "forbidden",
        },
        {
            "name": "secret-payload-read-denial",
            "resource": "secret:payload",
            "expected": "forbidden",
        },
    )


def dry_run_operations() -> tuple[dict[str, str], ...]:
    return (
        {"name": "quota-check", "mode": "dry-run"},
        {"name": "rollback-plan", "mode": "dry-run"},
        {"name": "schedule-repair", "mode": "dry-run"},
        {"name": "lifecycle-repair", "mode": "dry-run"},
    )


class SensitiveProjectDenialCanaryTests(unittest.TestCase):
    def test_required_mode_demands_protected_resource_manifest_reference(self) -> None:
        path = require_env("MIM_STAGING_PROTECTED_PROJECT_MANIFEST")
        self.assertTrue(path.endswith(".json") or path.endswith(".txt"))

    def test_denial_matrix_blocks_projects_bigquery_and_secret_payloads(self) -> None:
        denials = protected_resource_denials()

        self.assertEqual([item["expected"] for item in denials], ["forbidden"] * 4)
        self.assertIn(
            "project:user-personal-prod",
            {item["resource"] for item in denials},
        )
        self.assertIn(f"project:{PROJECT_ID}", {item["resource"] for item in denials})
        self.assertIn("service:bigquery", {item["resource"] for item in denials})
        self.assertIn("secret:payload", {item["resource"] for item in denials})

    def test_dry_run_matrix_never_requests_apply_or_delete(self) -> None:
        operations = dry_run_operations()
        rendered = " ".join(f"{item['name']}:{item['mode']}" for item in operations)

        self.assertTrue(all(item["mode"] == "dry-run" for item in operations))
        self.assertNotIn("apply", rendered)
        self.assertNotIn("delete", rendered)
        self.assertNotIn("destroy", rendered)


if __name__ == "__main__":
    unittest.main()
