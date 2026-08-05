# ruff: noqa: E402, E501

from __future__ import annotations

import importlib
import io
import json
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from unittest import mock

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = TEST_ROOT.parent / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.config import Settings
from mim_control_plane.domain.models import (
    ActivityEvent,
    ActivityEventId,
    DailyUsageAggregate,
    MaintenanceJobStatus,
    User,
    UserId,
)
from mim_control_plane.domain.states import (
    ActivityOutcome,
    ActivitySurface,
    UserRole,
    UserState,
)
from mim_control_plane.runtime import RuntimeEnvironment, RuntimeMode
from mim_control_plane.workers.usage_ingest import (
    ActivityIngestResult,
    BillingCostRecord,
    BillingIngestResult,
    UsageIngestWorker,
)
from tests.fakes import build_startup_mapping

NOW = datetime(2026, 8, 4, 2, 0, 0, tzinfo=UTC)
LEASED_AT = NOW
STARTED_AT = NOW + timedelta(seconds=5)
FINISHED_AT = NOW + timedelta(minutes=2)
FAILED_AT = NOW + timedelta(minutes=3)


def user(*, user_id: str) -> User:
    return User(
        id=UserId(user_id),
        email=f"{user_id}@madup.com",
        role=UserRole.USER,
        state=UserState.ACTIVE,
        groups=frozenset({"mim-users"}),
        identity_synced_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=90),
        updated_at=NOW - timedelta(days=1),
        version=1,
    )


def activity(
    *,
    event_id: str,
    user_id: str = "usr-1",
    occurred_at: datetime,
) -> ActivityEvent:
    return ActivityEvent(
        id=ActivityEventId(event_id),
        user_id=UserId(user_id),
        surface=ActivitySurface.MCP,
        action="deploy_execution",
        target_ref="wrk-1",
        outcome=ActivityOutcome.SUCCEEDED,
        latency_bucket="lt_1s",
        correlation_id=f"corr-{event_id}",
        occurred_at=occurred_at,
    )


@dataclass(frozen=True, slots=True)
class EmptyBillingSource:
    def fetch_cost_records(self, *, now: datetime) -> tuple[BillingCostRecord, ...]:
        del now
        return ()


class NoopEnforcer:
    def enforce_user_policy(self, **kwargs: object) -> None:
        del kwargs

    def enforce_org_policy(self, **kwargs: object) -> None:
        del kwargs


class UsageIngestJobTests(unittest.TestCase):
    def test_rollup_persisted_activity_zeroes_stale_day_and_user_buckets_after_expiry(
        self,
    ) -> None:
        store = MemoryStore()
        store.create_user(user(user_id="usr-1"))
        store.create_user(user(user_id="usr-2"))
        partial_day = date(2026, 7, 5)
        expired_day = date(2026, 7, 4)
        previous_now = NOW - timedelta(days=1)
        worker = UsageIngestWorker(
            store=store,
            billing=EmptyBillingSource(),
            retention=store,
            enforcer=NoopEnforcer(),
        )

        store.append_activity_event(
            activity(
                event_id="act-partial-expired",
                occurred_at=datetime(2026, 7, 5, 1, 59, 59, tzinfo=UTC),
            )
        )
        store.append_activity_event(
            activity(
                event_id="act-partial-retained",
                user_id="usr-2",
                occurred_at=datetime(2026, 7, 5, 2, 0, 0, tzinfo=UTC),
            )
        )
        store.append_activity_event(
            activity(
                event_id="act-day-expired",
                occurred_at=datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC),
            )
        )

        first_result = worker.rollup_persisted_activity(now=previous_now)
        self.assertEqual(set(first_result.organization_rollups), {partial_day, expired_day})
        self.assertEqual(
            store.get_daily_usage_aggregate(partial_day, None).active_users,
            2,
        )
        self.assertEqual(
            store.get_daily_usage_aggregate(partial_day, UserId("usr-1")).successes,
            1,
        )
        self.assertEqual(
            store.get_daily_usage_aggregate(expired_day, None).deployments,
            1,
        )

        result = worker.rollup_persisted_activity(now=NOW)

        self.assertEqual(
            set(result.organization_rollups),
            {partial_day, expired_day},
        )
        self.assertEqual(
            store.get_daily_usage_aggregate(partial_day, None).active_users,
            1,
        )
        self.assertEqual(
            store.get_daily_usage_aggregate(partial_day, UserId("usr-1")).successes,
            0,
        )
        self.assertEqual(
            store.get_daily_usage_aggregate(partial_day, UserId("usr-2")).successes,
            1,
        )
        expired_org = store.get_daily_usage_aggregate(expired_day, None)
        self.assertEqual(expired_org.active_users, 0)
        self.assertEqual(expired_org.deployments, 0)
        expired_user = store.get_daily_usage_aggregate(expired_day, UserId("usr-1"))
        self.assertEqual(expired_user.successes, 0)
        self.assertEqual(expired_user.deployments, 0)
        self.assertEqual(
            result.user_rollups[partial_day][UserId("usr-1")].successes,
            0,
        )
        self.assertEqual(
            result.user_rollups[expired_day][UserId("usr-1")].active_users,
            0,
        )

    def test_rollup_persisted_activity_expires_only_exact_old_events_and_updates_aggregates(
        self,
    ) -> None:
        store = MemoryStore()
        store.create_user(user(user_id="usr-1"))
        boundary = NOW - timedelta(days=30)
        older = store.append_activity_event(
            activity(event_id="act-old", occurred_at=boundary - timedelta(seconds=1))
        )
        kept_boundary = store.append_activity_event(
            activity(event_id="act-boundary", occurred_at=boundary)
        )
        kept_recent = store.append_activity_event(
            activity(event_id="act-recent", occurred_at=NOW - timedelta(days=1))
        )
        worker = UsageIngestWorker(
            store=store,
            billing=EmptyBillingSource(),
            retention=store,
            enforcer=NoopEnforcer(),
        )

        result = worker.rollup_persisted_activity(now=NOW)

        self.assertEqual(result.appended_event_ids, ())
        self.assertEqual(result.expired_event_ids, ("act-old",))
        self.assertEqual(
            tuple(str(event.id) for event in store.list_activity_events()),
            ("act-boundary", "act-recent"),
        )
        self.assertEqual(
            set(result.organization_rollups), {boundary.date(), date(2026, 8, 3)}
        )
        self.assertEqual(
            store.get_daily_usage_aggregate(boundary.date(), None).deployments,
            1,
        )
        self.assertEqual(
            store.get_daily_usage_aggregate(
                date(2026, 8, 3), UserId("usr-1")
            ).successes,
            1,
        )
        self.assertIn(str(older.id), result.expired_event_ids)
        self.assertNotIn(
            str(older.id),
            tuple(str(event.id) for event in store.list_activity_events()),
        )
        self.assertIn(
            str(kept_boundary.id),
            tuple(str(event.id) for event in store.list_activity_events()),
        )
        self.assertIn(
            str(kept_recent.id),
            tuple(str(event.id) for event in store.list_activity_events()),
        )

    def test_main_runs_billing_then_rollup_and_emits_sanitized_success(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.usage_ingest")
        common = importlib.import_module("mim_control_plane.jobs.maintenance_common")
        claim = common.OverlapLeaseClaim(
            token="private-lease-token",
            expires_at=NOW + timedelta(minutes=10),
        )
        calls: list[str] = []
        persisted: list[tuple[str, object]] = []

        class Worker:
            def ingest_billing(self, *, now: datetime) -> BillingIngestResult:
                self.assertEqual if False else None
                calls.append(f"billing:{now.isoformat()}")
                return BillingIngestResult(
                    appended_entry_ids=("bill-1",),
                    updated_entry_ids=("bill-2",),
                    ignored_entry_ids=("bill-3",),
                    user_decisions=MappingProxyType({}),
                    organization_decision=type(
                        "Decision",
                        (),
                        {
                            "emergency_stop": False,
                            "user_percent": 0,
                        },
                    )(),
                )

            def rollup_persisted_activity(
                self, *, now: datetime
            ) -> ActivityIngestResult:
                calls.append(f"rollup:{now.isoformat()}")
                aggregate = DailyUsageAggregate(
                    day=NOW.date(),
                    user_id=None,
                    active_users=1,
                    dashboard_visits=0,
                    mcp_actions=1,
                    deployments=1,
                    schedule_executions=0,
                    successes=1,
                    failures=0,
                    policy_denials=0,
                    version=1,
                    updated_at=NOW,
                )
                return ActivityIngestResult(
                    appended_event_ids=(),
                    expired_event_ids=("act-expired",),
                    organization_rollups=MappingProxyType({NOW.date(): aggregate}),
                    user_rollups=MappingProxyType({}),
                )

        class Lease:
            def try_acquire(self, *, now: datetime, duration: timedelta) -> object:
                del now, duration
                return claim

            def release(self, supplied: object) -> None:
                self.claim = supplied

        class StatusStore:
            def record_maintenance_job_started(self, **kwargs: object) -> MaintenanceJobStatus:
                persisted.append(("started", kwargs))
                return MaintenanceJobStatus(
                    job_name="usage-ingest",
                    run_id="run-1",
                    started_at=NOW,
                    finished_at=None,
                    succeeded_at=None,
                    failed_at=None,
                    outcome="started",
                    summary=(),
                    failure_code=None,
                    failure_class=None,
                    version=1,
                )

            def record_maintenance_job_terminal(self, **kwargs: object) -> MaintenanceJobStatus:
                persisted.append(("terminal", kwargs))
                return MaintenanceJobStatus(
                    job_name="usage-ingest",
                    run_id="run-1",
                    started_at=NOW,
                    finished_at=NOW,
                    succeeded_at=NOW,
                    failed_at=None,
                    outcome="completed",
                    summary=(("activity_rollup_days", 1),),
                    failure_code=None,
                    failure_class=None,
                    version=2,
                )

        stdout = io.StringIO()
        stderr = io.StringIO()
        times = iter((LEASED_AT, STARTED_AT, FINISHED_AT))

        def clock() -> datetime:
            return next(times)

        exit_code = job.main(
            argv=(),
            environ=runtime_environ(),
            stdout=stdout,
            stderr=stderr,
            clock=clock,
            runtime_loader=lambda mapping, dependencies: runtime_environment(),
            worker_builder=lambda source: Worker(),
            lease_builder=lambda source: Lease(),
            status_store_builder=lambda source: StatusStore(),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            calls,
            [f"billing:{STARTED_AT.isoformat()}", f"rollup:{STARTED_AT.isoformat()}"],
        )
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "activity_rollup_days": 1,
                "billing_appended_entries": 1,
                "billing_ignored_entries": 1,
                "billing_updated_entries": 1,
                "event": "mim.usage_ingest",
                "expired_activity_events": 1,
                "status": "completed",
            },
        )
        self.assertNotIn("private-lease-token", stdout.getvalue())
        self.assertEqual(persisted[0][1]["job_name"], "usage-ingest")
        self.assertEqual(persisted[1][1]["outcome"], "completed")
        self.assertEqual(persisted[0][1]["started_at"], STARTED_AT)
        self.assertEqual(persisted[1][1]["finished_at"], FINISHED_AT)

    def test_skipped_overlap_keeps_page_green_when_skip_status_write_fails(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.usage_ingest")
        stdout = io.StringIO()
        stderr = io.StringIO()

        class Worker:
            def ingest_billing(self, *, now: datetime) -> BillingIngestResult:
                raise AssertionError(f"billing should not run at {now!r}")

            def rollup_persisted_activity(
                self, *, now: datetime
            ) -> ActivityIngestResult:
                raise AssertionError(f"rollup should not run at {now!r}")

        class Lease:
            def try_acquire(self, *, now: datetime, duration: timedelta) -> object:
                del now, duration
                return None

            def release(self, supplied: object) -> None:
                raise AssertionError(f"release should not run for {supplied!r}")

        exit_code = job.main(
            argv=(),
            environ=runtime_environ(),
            stdout=stdout,
            stderr=stderr,
            clock=lambda: NOW,
            runtime_loader=lambda mapping, dependencies: runtime_environment(),
            worker_builder=lambda source: Worker(),
            lease_builder=lambda source: Lease(),
            status_store_builder=lambda source: (_ for _ in ()).throw(
                AssertionError("status store must not be built")
            ),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"event": "mim.usage_ingest", "status": "skipped_overlap"},
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_main_returns_generic_failure_when_metadata_loader_raises(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.usage_ingest")
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = job.main(
            argv=(),
            environ=runtime_environ(),
            stdout=stdout,
            stderr=stderr,
            dependencies=job.runtime.ProductionDependencies(
                metadata_credentials_loader=lambda: (_ for _ in ()).throw(
                    RuntimeError("billing secret leaked")
                )
            ),
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"event": "mim.usage_ingest", "status": "failed"},
        )
        self.assertNotIn("billing secret leaked", stderr.getvalue())

    def test_main_records_failed_status_with_post_run_clock(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.usage_ingest")
        common = importlib.import_module("mim_control_plane.jobs.maintenance_common")
        claim = common.OverlapLeaseClaim(
            token="private-lease-token",
            expires_at=LEASED_AT + timedelta(minutes=10),
        )
        persisted: list[dict[str, object]] = []
        times = iter((LEASED_AT, STARTED_AT, FAILED_AT))

        def clock() -> datetime:
            return next(times)

        class Worker:
            def ingest_billing(self, *, now: datetime) -> BillingIngestResult:
                raise RuntimeError(f"billing must fail at {now.isoformat()}")

            def rollup_persisted_activity(
                self, *, now: datetime
            ) -> ActivityIngestResult:
                raise AssertionError(f"rollup must not run at {now!r}")

        class Lease:
            def try_acquire(self, *, now: datetime, duration: timedelta) -> object:
                del now, duration
                return claim

            def release(self, supplied: object) -> None:
                self.claim = supplied

        class StatusStore:
            def record_maintenance_job_started(
                self,
                **kwargs: object,
            ) -> MaintenanceJobStatus:
                return MaintenanceJobStatus(
                    job_name=str(kwargs["job_name"]),
                    run_id=str(kwargs["run_id"]),
                    started_at=kwargs["started_at"],  # type: ignore[arg-type]
                    finished_at=None,
                    succeeded_at=None,
                    failed_at=None,
                    outcome="started",
                    summary=(),
                    failure_code=None,
                    failure_class=None,
                    version=1,
                )

            def record_maintenance_job_terminal(
                self,
                **kwargs: object,
            ) -> MaintenanceJobStatus:
                persisted.append(dict(kwargs))
                outcome = str(kwargs["outcome"])
                finished_at = kwargs["finished_at"]  # type: ignore[assignment]
                return MaintenanceJobStatus(
                    job_name=str(kwargs["job_name"]),
                    run_id=str(kwargs["run_id"]),
                    started_at=STARTED_AT,
                    finished_at=finished_at,
                    succeeded_at=finished_at if outcome == "completed" else None,
                    failed_at=finished_at if outcome == "failed" else None,
                    outcome=outcome,
                    summary=kwargs["summary"],  # type: ignore[arg-type]
                    failure_code=kwargs.get("failure_code"),  # type: ignore[arg-type]
                    failure_class=kwargs.get("failure_class"),  # type: ignore[arg-type]
                    version=2,
                )

        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = job.main(
            argv=(),
            environ=runtime_environ(),
            stdout=stdout,
            stderr=stderr,
            clock=clock,
            runtime_loader=lambda mapping, dependencies: runtime_environment(),
            worker_builder=lambda source: Worker(),
            lease_builder=lambda source: Lease(),
            status_store_builder=lambda source: StatusStore(),
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"event": "mim.usage_ingest", "status": "failed"},
        )
        self.assertEqual(persisted[0]["outcome"], "failed")
        self.assertEqual(persisted[0]["finished_at"], FAILED_AT)

    def test_overlapping_skip_does_not_clobber_active_latest_status(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.usage_ingest")
        common = importlib.import_module("mim_control_plane.jobs.maintenance_common")
        store = MemoryStore()
        lease_claim = common.OverlapLeaseClaim(
            token="private-lease-token",
            expires_at=LEASED_AT + timedelta(minutes=10),
        )
        outputs: dict[str, object] = {}
        times = iter(
            (
                LEASED_AT,
                STARTED_AT,
                LEASED_AT + timedelta(seconds=30),
                FINISHED_AT,
            )
        )

        def clock() -> datetime:
            return next(times)

        class SharedLease:
            def __init__(self) -> None:
                self.active = False

            def try_acquire(self, *, now: datetime, duration: timedelta) -> object:
                del now, duration
                if self.active:
                    return None
                self.active = True
                return lease_claim

            def release(self, supplied: object) -> None:
                self.active = False
                self.released = supplied

        lease = SharedLease()

        class OverlapWorker:
            def ingest_billing(self, *, now: datetime) -> BillingIngestResult:
                raise AssertionError(f"overlap billing must not run at {now!r}")

            def rollup_persisted_activity(
                self, *, now: datetime
            ) -> ActivityIngestResult:
                raise AssertionError(f"overlap rollup must not run at {now!r}")

        class ActiveWorker:
            def ingest_billing(self, *, now: datetime) -> BillingIngestResult:
                outputs["active_started_at"] = now
                overlap_stdout = io.StringIO()
                overlap_stderr = io.StringIO()
                overlap_exit = job.main(
                    argv=(),
                    environ=runtime_environ(),
                    stdout=overlap_stdout,
                    stderr=overlap_stderr,
                    clock=clock,
                    runtime_loader=lambda mapping, dependencies: runtime_environment(),
                    worker_builder=lambda source: OverlapWorker(),
                    lease_builder=lambda source: lease,
                    status_store_builder=lambda source: store,
                )
                outputs["overlap_exit"] = overlap_exit
                outputs["overlap_stdout"] = json.loads(overlap_stdout.getvalue())
                outputs["overlap_stderr"] = overlap_stderr.getvalue()
                return BillingIngestResult(
                    appended_entry_ids=(),
                    updated_entry_ids=(),
                    ignored_entry_ids=(),
                    user_decisions=MappingProxyType({}),
                    organization_decision=type(
                        "Decision",
                        (),
                        {"emergency_stop": False, "user_percent": 0},
                    )(),
                )

            def rollup_persisted_activity(
                self, *, now: datetime
            ) -> ActivityIngestResult:
                aggregate = DailyUsageAggregate(
                    day=STARTED_AT.date(),
                    user_id=None,
                    active_users=0,
                    dashboard_visits=0,
                    mcp_actions=0,
                    deployments=0,
                    schedule_executions=0,
                    successes=0,
                    failures=0,
                    policy_denials=0,
                    version=1,
                    updated_at=STARTED_AT,
                )
                return ActivityIngestResult(
                    appended_event_ids=(),
                    expired_event_ids=(),
                    organization_rollups=MappingProxyType({STARTED_AT.date(): aggregate}),
                    user_rollups=MappingProxyType({}),
                )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            job,
            "generate_run_id",
            side_effect=("run-active", "run-overlap"),
        ):
            exit_code = job.main(
                argv=(),
                environ=runtime_environ(),
                stdout=stdout,
                stderr=stderr,
                clock=clock,
                runtime_loader=lambda mapping, dependencies: runtime_environment(),
                worker_builder=lambda source: ActiveWorker(),
                lease_builder=lambda source: lease,
                status_store_builder=lambda source: store,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(outputs["overlap_exit"], 0)
        self.assertEqual(
            outputs["overlap_stdout"],
            {"event": "mim.usage_ingest", "status": "skipped_overlap"},
        )
        self.assertEqual(outputs["overlap_stderr"], "")
        self.assertEqual(outputs["active_started_at"], STARTED_AT)
        status = store.get_maintenance_job_status("usage-ingest")
        self.assertEqual(status.run_id, "run-active")
        self.assertEqual(status.outcome, "completed")
        self.assertEqual(status.started_at, STARTED_AT)
        self.assertEqual(status.finished_at, FINISHED_AT)
        self.assertEqual(status.version, 2)

    def test_build_worker_accepts_runtime_bootstrap_like_objects(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.usage_ingest")
        settings = Settings.from_mapping(build_startup_mapping())
        credentials = object()
        source = BootstrapLike(
            public_settings=settings,
            admin_members=("group:mim-admins@madup.com", "user:operator.test@madup.com"),
            project_number="123456789012",
        )
        captured: dict[str, object] = {}
        expected = object()

        def store_factory(**kwargs: object) -> object:
            captured["store_settings"] = settings
            captured["store_credentials_loader"] = kwargs["credentials_loader"]
            return "store"

        def billing_builder(**kwargs: object) -> object:
            captured["billing"] = kwargs
            return "billing"

        def enforcer_builder(**kwargs: object) -> object:
            captured["enforcer"] = kwargs
            return "enforcer"

        def worker_factory(**kwargs: object) -> object:
            captured["worker"] = kwargs
            return expected

        built = job.build_usage_worker(
            source,
            clock=lambda: NOW,
            metadata_credentials_loader=lambda: credentials,
            store_factory=store_factory,
            billing_builder=billing_builder,
            enforcer_builder=enforcer_builder,
            worker_factory=worker_factory,
        )

        self.assertIs(built, expected)
        self.assertIs(captured["store_settings"], settings)
        self.assertIs(captured["store_credentials_loader"](), credentials)
        self.assertEqual(
            captured["billing"],
            {
                "clock": unittest.mock.ANY,
                "credentials": credentials,
                "settings": settings,
                "source": source,
                "store": "store",
            },
        )
        self.assertEqual(
            captured["enforcer"],
            {
                "clock": unittest.mock.ANY,
                "credentials": credentials,
                "settings": settings,
                "source": source,
                "store": "store",
            },
        )
        self.assertEqual(
            captured["worker"],
            {
                "store": "store",
                "billing": "billing",
                "retention": "store",
                "enforcer": "enforcer",
            },
        )

    def test_main_requires_usage_ingest_mode_and_mutations(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.usage_ingest")
        built = False

        def forbidden_builder(*args: object, **kwargs: object) -> object:
            nonlocal built
            built = True
            raise AssertionError("worker must not be built")

        stdout = io.StringIO()
        stderr = io.StringIO()
        for runtime_env in (
            runtime_environment(mode=RuntimeMode.IDENTITY_SYNC),
            runtime_environment(mutations_enabled=False),
        ):
            with self.subTest(runtime_env=runtime_env):
                stdout.seek(0)
                stdout.truncate(0)
                stderr.seek(0)
                stderr.truncate(0)
                built = False
                exit_code = job.main(
                    argv=(),
                    environ=runtime_environ(),
                    stdout=stdout,
                    stderr=stderr,
                    clock=lambda: NOW,
                    runtime_loader=lambda mapping, dependencies, env=runtime_env: env,
                    worker_builder=forbidden_builder,
                )
                self.assertEqual(exit_code, 1)
                self.assertFalse(built)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    json.loads(stderr.getvalue()),
                    {"event": "mim.usage_ingest", "status": "failed"},
                )


def runtime_environ() -> dict[str, str]:
    return {
        "MIM_RUNTIME_MODE": "usage-ingest",
        "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION": (
            "projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7"
        ),
        "MIM_ENABLE_MUTATIONS": "true",
    }


def runtime_environment(
    *,
    mode: RuntimeMode = RuntimeMode.USAGE_INGEST,
    mutations_enabled: bool = True,
) -> RuntimeEnvironment:
    return RuntimeEnvironment(
        mode=mode,
        bootstrap_secret_version=runtime_environ()[
            "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION"
        ],
        mutations_enabled=mutations_enabled,
        bootstrap=BootstrapLike(
            public_settings=Settings.from_mapping(build_startup_mapping()),
            admin_members=(
                "group:mim-admins@madup.com",
                "user:operator.test@madup.com",
            ),
            project_number="123456789012",
        ),
    )


@dataclass(frozen=True, slots=True)
class BootstrapLike:
    public_settings: Settings
    admin_members: tuple[str, ...]
    project_number: str


if __name__ == "__main__":
    unittest.main()
