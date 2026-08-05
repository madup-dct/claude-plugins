from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta

from mim_control_plane.domain.models import (
    DeploymentPlan,
    DeploymentPlanId,
    UserId,
    WorkloadId,
)
from mim_control_plane.domain.plans import hash_plan_material
from mim_control_plane.domain.states import PlanState
from mim_control_plane.security.redaction import (
    OutputSurface,
    RedactionError,
    sanitize_output,
)
from mim_control_plane.services.audit import AuditRecord, build_audit_record

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


@dataclasses.dataclass(frozen=True, slots=True)
class ExplosiveSecret:
    token: str = "unpatterned-top-secret"

    def __repr__(self) -> str:
        raise AssertionError("repr must not be called for unexpected redaction values")


def issued_plan() -> DeploymentPlan:
    material = {
        "repository": {
            "owner": "madupmarketing",
            "name": "sample-app",
            "sha": "a" * 40,
        },
        "workload": {"kind": "streamlit", "name": "sample-app"},
    }
    return DeploymentPlan(
        id=DeploymentPlanId("plan-1"),
        actor_id=UserId("usr-1"),
        workload_id=WorkloadId("wrk-1"),
        action="deploy",
        material_hash=hash_plan_material(
            material,
            action="deploy",
            policy_version="policy-v1",
        ),
        policy_version="policy-v1",
        state=PlanState.ISSUED,
        expires_at=NOW + timedelta(minutes=15),
        created_at=NOW,
        updated_at=NOW,
        sanitized_summary=(("repository", "madupmarketing/sample-app"),),
    )


class RedactionAndAuditTests(unittest.TestCase):
    def test_sensitive_headers_cookies_env_and_tokens_are_removed_for_all_surfaces(
        self,
    ) -> None:
        payload = {
            "message": "Authorization: Bearer super-secret-token",
            "summary": {
                "repository": "madupmarketing/sample-app",
                "sha": "a" * 40,
                "Authorization": "Bearer top-secret",
                "cookie": "session=abc",
                "env": {
                    "OPENAI_API_KEY": "sk-secret",
                    "SAFE_VALUE": "keep-me",
                },
                "source_token": "ghp_1234567890",
                "details": "GITHUB_TOKEN=token-value",
            },
            "plan_hash": "hash-1",
        }

        for surface in OutputSurface:
            with self.subTest(surface=surface):
                sanitized = sanitize_output(surface, payload)
                rendered = repr(sanitized)
                self.assertIn("madupmarketing/sample-app", rendered)
                self.assertIn("hash-1", rendered)
                self.assertNotIn("super-secret-token", rendered)
                self.assertNotIn("top-secret", rendered)
                self.assertNotIn("session=abc", rendered)
                self.assertNotIn("sk-secret", rendered)
                self.assertNotIn("token-value", rendered)
                self.assertNotIn("source_token", rendered)
                self.assertNotIn("Authorization", rendered)
                self.assertNotIn("cookie", rendered)
                self.assertNotIn("env", rendered)

    def test_unknown_output_field_is_rejected_by_allowlist_schema(self) -> None:
        with self.assertRaises(RedactionError) as context:
            sanitize_output(
                OutputSurface.API,
                {
                    "message": "safe",
                    "summary": {"repository": "madupmarketing/sample-app"},
                    "evilInjectedKey": {"Authorization": "Bearer leaked"},
                },
            )

        message = str(context.exception)
        self.assertIn("reviewed allowlist", message)
        self.assertNotIn("evilInjectedKey", message)

    def test_redaction_rejects_custom_objects_and_bytes_without_calling_repr(
        self,
    ) -> None:
        with self.assertRaises(RedactionError) as object_context:
            sanitize_output(
                OutputSurface.API,
                {
                    "message": "safe",
                    "summary": {"repository": ExplosiveSecret()},
                },
            )
        self.assertIn("JSON-like", str(object_context.exception))

        for raw_value in (b"secret-bytes", bytearray(b"secret-bytearray")):
            with self.subTest(raw_value=type(raw_value).__name__):
                with self.assertRaises(RedactionError) as bytes_context:
                    sanitize_output(
                        OutputSurface.API,
                        {
                            "message": "safe",
                            "summary": {"repository": raw_value},
                        },
                    )
                self.assertIn("JSON-like", str(bytes_context.exception))

    def test_redaction_strips_camelcase_sensitive_keys(self) -> None:
        payload = {
            "message": "safe",
            "summary": {
                "repository": "madupmarketing/sample-app",
                "apiKey": "key-should-go",
                "accessToken": "token-should-go",
                "refreshToken": "refresh-should-go",
            },
        }

        sanitized = sanitize_output(OutputSurface.API, payload)
        rendered = repr(sanitized)
        self.assertIn("madupmarketing/sample-app", rendered)
        self.assertNotIn("apiKey", rendered)
        self.assertNotIn("accessToken", rendered)
        self.assertNotIn("refreshToken", rendered)
        self.assertNotIn("key-should-go", rendered)
        self.assertNotIn("token-should-go", rendered)
        self.assertNotIn("refresh-should-go", rendered)

    def test_audit_record_keeps_plan_hash_and_sanitized_summary_without_body(
        self,
    ) -> None:
        record = build_audit_record(
            event_id="audit-1",
            actor_id=UserId("usr-1"),
            action="deploy",
            target_ref="wrk-1",
            policy_decision="allowed",
            correlation_id="corr-1",
            outcome="queued",
            occurred_at=NOW,
            plan=issued_plan(),
            output={
                "summary": {
                    "repository": "madupmarketing/sample-app",
                    "status": "queued",
                    "headers": {"authorization": "Bearer leaked"},
                    "body": {"cookie": "session=secret"},
                },
                "message": "Bearer leaked",
            },
        )

        self.assertIsInstance(record, AuditRecord)
        self.assertEqual(record.plan_hash, issued_plan().material_hash)
        self.assertIn(
            ("repository", "madupmarketing/sample-app"),
            record.sanitized_summary,
        )
        self.assertIn(("status", "queued"), record.sanitized_summary)
        rendered = repr(record)
        self.assertNotIn("leaked", rendered)
        self.assertNotIn("cookie", rendered)
        self.assertNotIn("authorization", rendered.lower())
        self.assertFalse(any(key == "body" for key, _ in record.sanitized_summary))


if __name__ == "__main__":
    unittest.main()
