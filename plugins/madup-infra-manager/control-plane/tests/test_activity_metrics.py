from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, date, datetime, timedelta

from mim_control_plane.domain.models import (
    ActivityEvent,
    ActivityEventId,
    AuditEvent,
    AuditEventId,
    UserId,
)
from mim_control_plane.domain.states import ActivityOutcome, ActivitySurface
from mim_control_plane.services.usage import (
    ActivityAction,
    ActivityMetricsError,
    DailyActivityRollup,
    aggregate_daily_activity,
    compute_activity_metrics,
    ingest_activity_event,
    plan_activity_retention,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
DAY = date(2026, 8, 3)
USER = UserId("usr-1")
OTHER_USER = UserId("usr-2")


def event(
    *,
    event_id: str,
    user_id: UserId = USER,
    surface: ActivitySurface = ActivitySurface.MCP,
    action: str = ActivityAction.PLAN_DEPLOY,
    outcome: ActivityOutcome = ActivityOutcome.SUCCEEDED,
    occurred_at: datetime = NOW,
    latency_bucket: str = "lt_250ms",
    correlation_id: str = "corr-1",
    target_ref: str | None = "wrk-1",
) -> ActivityEvent:
    return ActivityEvent(
        id=ActivityEventId(event_id),
        user_id=user_id,
        surface=surface,
        action=action,
        target_ref=target_ref,
        outcome=outcome,
        latency_bucket=latency_bucket,
        correlation_id=correlation_id,
        occurred_at=occurred_at,
    )


class ActivityMetricsTests(unittest.TestCase):
    def test_ingest_activity_event_accepts_only_allowlisted_fields(self) -> None:
        ingested = ingest_activity_event(
            event_id="act-1",
            trusted_user_id=USER,
            trusted_correlation_id="corr-1",
            trusted_occurred_at=NOW,
            observed_at=NOW,
            payload={
                "surface": "mcp",
                "action": "plan_deploy",
                "target_ref": "wrk-1",
                "outcome": "succeeded",
                "latency_ms": 249,
            },
        )

        self.assertEqual(ingested.user_id, USER)
        self.assertEqual(ingested.surface, ActivitySurface.MCP)
        self.assertEqual(ingested.action, ActivityAction.PLAN_DEPLOY)
        self.assertEqual(ingested.latency_bucket, "lt_250ms")

    def test_ingest_activity_event_rejects_privacy_attack_fields_and_values(
        self,
    ) -> None:
        attack_payloads = (
            {"surface": "mcp", "action": "plan_deploy", "prompt": "ship this now"},
            {"surface": "mcp", "action": "plan_deploy", "request_body": "{}"},
            {"surface": "mcp", "action": "plan_deploy", "ip": "127.0.0.1"},
            {"surface": "mcp", "action": "plan_deploy", "user_agent": "curl/8.0"},
            {"surface": "mcp", "action": "plan_deploy", "cookie": "session=abc"},
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "occurred_at": NOW,
            },
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "authorization": "Bearer secret-token",
            },
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "env": {"OPENAI_API_KEY": "sk-secret"},
            },
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "target_ref": "Authorization: Bearer leaked",
            },
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "target_ref": "127.0.0.1",
            },
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "target_ref": "2001:db8::1",
            },
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "target_ref": "example.com:443",
            },
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "target_ref": "curl/8.0.1",
            },
        )

        for payload in attack_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ActivityMetricsError) as raised:
                    ingest_activity_event(
                        event_id="act-attack",
                        trusted_user_id=USER,
                        trusted_correlation_id="corr-1",
                        trusted_occurred_at=NOW,
                        observed_at=NOW,
                        payload=payload
                        | {
                            "outcome": "succeeded",
                            "latency_ms": 0,
                        },
                    )
                message = str(raised.exception)
                self.assertIn("unsupported or unsafe", message)
                self.assertNotIn("Authorization", message)
                self.assertNotIn("secret-token", message)
                self.assertNotIn("OPENAI_API_KEY", message)

    def test_ingest_activity_event_rejects_unknown_taxonomy_and_bad_latency(
        self,
    ) -> None:
        invalid_payloads = (
            {"surface": "browser", "action": "plan_deploy", "outcome": "succeeded"},
            {"surface": "mcp", "action": "freeform_chat", "outcome": "succeeded"},
            {"surface": "mcp", "action": "plan_deploy", "outcome": "partial"},
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "outcome": "succeeded",
                "correlation_id": "corr-1",
            },
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "outcome": "succeeded",
                "latency_ms": -1,
            },
            {
                "surface": "mcp",
                "action": "plan_deploy",
                "outcome": "succeeded",
                "latency_ms": True,
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ActivityMetricsError):
                    ingest_activity_event(
                        event_id="act-invalid",
                        trusted_user_id=USER,
                        trusted_correlation_id="corr-1",
                        trusted_occurred_at=NOW,
                        observed_at=NOW,
                        payload=payload,
                    )

    def test_ingest_activity_event_rejects_untrusted_correlation_id_values(
        self,
    ) -> None:
        for correlation_id in (
            "corr",
            "trace-1",
            "corr-127.0.0.1",
            "corr-example.com",
            "corr-curl/8.0.1",
            "127.0.0.1",
            "2001:db8::1",
            "example.com:443",
            "curl/8.0.1",
            "corr-ghp_secret",
            "corr-sk-secret",
            "corr-api_key",
            "corr-authorization",
            "corr-cookie",
            "x" * 65,
        ):
            with self.subTest(correlation_id=correlation_id):
                with self.assertRaises(ActivityMetricsError) as raised:
                    ingest_activity_event(
                        event_id="act-corr",
                        trusted_user_id=USER,
                        trusted_correlation_id=correlation_id,
                        trusted_occurred_at=NOW,
                        observed_at=NOW,
                        payload={
                            "surface": "mcp",
                            "action": "plan_deploy",
                            "target_ref": "wrk-1",
                            "outcome": "succeeded",
                            "latency_ms": 0,
                        },
                    )
                self.assertNotIn(correlation_id, str(raised.exception))

    def test_ingest_activity_event_rejects_future_occurred_at(self) -> None:
        with self.assertRaises(ActivityMetricsError):
            ingest_activity_event(
                event_id="act-future",
                trusted_user_id=USER,
                trusted_correlation_id="corr-1",
                trusted_occurred_at=NOW + timedelta(seconds=1),
                observed_at=NOW,
                payload={
                    "surface": "mcp",
                    "action": "plan_deploy",
                    "target_ref": "wrk-1",
                    "outcome": "succeeded",
                    "latency_ms": 0,
                },
            )

    def test_latency_buckets_and_user_binding_are_deterministic(self) -> None:
        payloads = (
            (0, "lt_250ms"),
            (249, "lt_250ms"),
            (250, "lt_1000ms"),
            (999, "lt_1000ms"),
            (1000, "lt_5000ms"),
            (4999, "lt_5000ms"),
            (5000, "gte_5000ms"),
        )
        for latency_ms, expected_bucket in payloads:
            with self.subTest(latency_ms=latency_ms):
                ingested = ingest_activity_event(
                    event_id=f"act-{latency_ms}",
                    trusted_user_id=USER,
                    trusted_correlation_id=f"corr-{latency_ms}",
                    trusted_occurred_at=NOW,
                    observed_at=NOW,
                    payload={
                        "surface": "dashboard",
                        "action": "view_dashboard",
                        "target_ref": "dash-home",
                        "outcome": "succeeded",
                        "latency_ms": latency_ms,
                    },
                )
                self.assertEqual(ingested.user_id, USER)
                self.assertEqual(ingested.latency_bucket, expected_bucket)

    def test_activity_metrics_cover_24h_7d_30d_edges_and_unique_visitors(self) -> None:
        events = (
            event(
                event_id="dash-now",
                surface=ActivitySurface.DASHBOARD,
                action=ActivityAction.VIEW_DASHBOARD,
                occurred_at=NOW,
            ),
            event(
                event_id="dash-24h-edge",
                surface=ActivitySurface.DASHBOARD,
                action=ActivityAction.VIEW_DASHBOARD,
                occurred_at=NOW - timedelta(hours=24),
                correlation_id="corr-24h",
            ),
            event(
                event_id="dash-24h-out",
                surface=ActivitySurface.DASHBOARD,
                action=ActivityAction.VIEW_DASHBOARD,
                occurred_at=NOW - timedelta(hours=24, microseconds=1),
                correlation_id="corr-24h-out",
            ),
            event(
                event_id="mcp-7d-edge",
                user_id=OTHER_USER,
                occurred_at=NOW - timedelta(days=7),
                correlation_id="corr-7d",
            ),
            event(
                event_id="deploy-30d-edge",
                user_id=OTHER_USER,
                action=ActivityAction.DEPLOY_EXECUTION,
                occurred_at=NOW - timedelta(days=30),
                correlation_id="corr-30d",
            ),
            event(
                event_id="schedule-30d-out",
                user_id=OTHER_USER,
                action=ActivityAction.SCHEDULE_RUN,
                occurred_at=NOW - timedelta(days=30, microseconds=1),
                correlation_id="corr-30d-out",
            ),
            event(
                event_id="deny-now",
                action=ActivityAction.PLAN_DEPLOY,
                outcome=ActivityOutcome.DENIED,
                correlation_id="corr-deny",
            ),
            event(
                event_id="fail-now",
                action=ActivityAction.DEPLOY_EXECUTION,
                outcome=ActivityOutcome.FAILED,
                correlation_id="corr-fail",
            ),
        )

        summary = compute_activity_metrics(events, now=NOW)

        self.assertEqual(summary.active_users_24h, 1)
        self.assertEqual(summary.active_users_7d, 2)
        self.assertEqual(summary.active_users_30d, 2)
        self.assertEqual(summary.dashboard_unique_visitors_24h, 1)
        self.assertEqual(summary.dashboard_unique_visitors_7d, 1)
        self.assertEqual(summary.dashboard_unique_visitors_30d, 1)
        self.assertEqual(summary.dashboard_visits_24h, 2)
        self.assertEqual(summary.mcp_actions_30d, 4)
        self.assertEqual(summary.deployments_30d, 2)
        self.assertEqual(summary.schedule_runs_30d, 0)
        self.assertEqual(summary.successes_30d, 5)
        self.assertEqual(summary.failures_30d, 1)
        self.assertEqual(summary.denials_30d, 1)

    def test_activity_metrics_can_be_scoped_per_user(self) -> None:
        events = (
            event(event_id="u1-now", user_id=USER),
            event(
                event_id="u2-now",
                user_id=OTHER_USER,
                action=ActivityAction.DEPLOY_EXECUTION,
            ),
        )

        summary = compute_activity_metrics(events, now=NOW, user_id=USER)

        self.assertEqual(summary.active_users_24h, 1)
        self.assertEqual(summary.active_users_30d, 1)
        self.assertEqual(summary.mcp_actions_30d, 1)
        self.assertEqual(summary.deployments_30d, 0)

    def test_scoped_metrics_ignore_other_user_future_poison_rows(self) -> None:
        events = (
            event(event_id="u1-now", user_id=USER, occurred_at=NOW),
            event(
                event_id="u2-future",
                user_id=OTHER_USER,
                occurred_at=NOW + timedelta(seconds=1),
                correlation_id="corr-u2-future",
            ),
        )

        summary = compute_activity_metrics(events, now=NOW, user_id=USER)

        self.assertEqual(summary.active_users_24h, 1)
        self.assertEqual(summary.mcp_actions_30d, 1)

    def test_duplicate_activity_ids_fail_closed_for_metrics(self) -> None:
        duplicate_a = event(
            event_id="dup-1",
            user_id=USER,
            correlation_id="corr-dup-a",
        )
        duplicate_b = event(
            event_id="dup-1",
            user_id=USER,
            correlation_id="corr-dup-b",
        )

        with self.assertRaises(ActivityMetricsError):
            compute_activity_metrics((duplicate_a, duplicate_b), now=NOW)

    def test_daily_rollup_uses_half_open_utc_boundaries(self) -> None:
        events = (
            event(
                event_id="day-start",
                surface=ActivitySurface.DASHBOARD,
                action=ActivityAction.VIEW_DASHBOARD,
                occurred_at=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
                correlation_id="corr-start",
            ),
            event(
                event_id="day-end-open",
                action=ActivityAction.SCHEDULE_RUN,
                occurred_at=datetime(2026, 8, 4, 0, 0, tzinfo=UTC),
                correlation_id="corr-end",
            ),
            event(
                event_id="day-denied",
                action=ActivityAction.PLAN_DEPLOY,
                outcome=ActivityOutcome.DENIED,
                occurred_at=datetime(2026, 8, 3, 23, 59, 59, tzinfo=UTC),
                correlation_id="corr-denied",
            ),
        )

        rollup = aggregate_daily_activity(
            events,
            day=DAY,
            now=datetime(2026, 8, 4, 0, 0, tzinfo=UTC),
        )

        self.assertIsInstance(rollup, DailyActivityRollup)
        self.assertTrue(dataclasses.is_dataclass(rollup.organization))
        self.assertEqual(rollup.organization.day, DAY)
        self.assertEqual(rollup.organization.active_users, 1)
        self.assertEqual(rollup.organization.dashboard_visits, 1)
        self.assertEqual(rollup.organization.schedule_executions, 0)
        self.assertEqual(rollup.organization.policy_denials, 1)
        self.assertEqual(rollup.organization.successes, 1)
        self.assertEqual(rollup.organization.failures, 0)
        self.assertEqual(rollup.by_user[USER].dashboard_visits, 1)
        self.assertEqual(rollup.by_user[USER].policy_denials, 1)

    def test_future_and_naive_events_fail_closed(self) -> None:
        future_event = event(
            event_id="future",
            occurred_at=NOW + timedelta(seconds=1),
            correlation_id="corr-future",
        )
        with self.assertRaisesRegex(ValueError, "UTC-aware"):
            event(
                event_id="naive",
                occurred_at=datetime(2026, 8, 3, 11, 59, 59),
                correlation_id="corr-naive",
            )

        with self.subTest(candidate=future_event.id):
            with self.assertRaises(ActivityMetricsError):
                compute_activity_metrics((future_event,), now=NOW)
            with self.assertRaises(ActivityMetricsError):
                aggregate_daily_activity((future_event,), day=DAY, now=NOW)

    def test_activity_retention_uses_a_separate_30_day_partition_plan(self) -> None:
        stale_event = event(
            event_id="stale",
            occurred_at=NOW - timedelta(days=30, microseconds=1),
            correlation_id="corr-stale",
        )
        cutoff_event = event(
            event_id="cutoff",
            occurred_at=NOW - timedelta(days=30),
            correlation_id="corr-cutoff",
        )
        fresh_event = event(
            event_id="fresh",
            occurred_at=NOW - timedelta(days=29),
            correlation_id="corr-fresh",
        )

        plan = plan_activity_retention(
            (stale_event, cutoff_event, fresh_event),
            now=NOW,
        )

        self.assertEqual(plan.cutoff, NOW - timedelta(days=30))
        self.assertEqual(plan.expired_event_ids, (ActivityEventId("stale"),))
        self.assertEqual(
            plan.keep_event_ids,
            (ActivityEventId("cutoff"), ActivityEventId("fresh")),
        )

    def test_activity_retention_sorts_ids_independent_of_input_order(self) -> None:
        stale_b = event(
            event_id="stale-b",
            occurred_at=NOW - timedelta(days=31),
            correlation_id="corr-stale-b",
        )
        stale_a = event(
            event_id="stale-a",
            occurred_at=NOW - timedelta(days=31),
            correlation_id="corr-stale-a",
        )
        keep_b = event(
            event_id="keep-b",
            occurred_at=NOW - timedelta(days=29),
            correlation_id="corr-keep-b",
        )
        keep_a = event(
            event_id="keep-a",
            occurred_at=NOW - timedelta(days=29),
            correlation_id="corr-keep-a",
        )

        plan = plan_activity_retention(
            (keep_b, stale_b, keep_a, stale_a),
            now=NOW,
        )

        self.assertEqual(
            plan.expired_event_ids,
            (ActivityEventId("stale-a"), ActivityEventId("stale-b")),
        )
        self.assertEqual(
            plan.keep_event_ids,
            (ActivityEventId("keep-a"), ActivityEventId("keep-b")),
        )

    def test_duplicate_activity_ids_fail_closed_for_retention(self) -> None:
        duplicate_a = event(
            event_id="dup-retain",
            occurred_at=NOW - timedelta(days=31),
            correlation_id="corr-retain-a",
        )
        duplicate_b = event(
            event_id="dup-retain",
            occurred_at=NOW - timedelta(days=29),
            correlation_id="corr-retain-b",
        )

        with self.assertRaises(ActivityMetricsError):
            plan_activity_retention((duplicate_a, duplicate_b), now=NOW)

    def test_activity_retention_never_accepts_audit_events(self) -> None:
        audit_event = AuditEvent(
            id=AuditEventId("audit-1"),
            actor_id=USER,
            action="deploy",
            target_ref="wrk-1",
            policy_decision="allowed",
            before_ref=None,
            after_ref="op-1",
            correlation_id="corr-audit",
            outcome="succeeded",
            occurred_at=NOW,
        )

        with self.assertRaises(ActivityMetricsError):
            plan_activity_retention((audit_event,), now=NOW)


if __name__ == "__main__":
    unittest.main()
