# ruff: noqa: E402, E501

from __future__ import annotations

import importlib
import io
import json
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = TEST_ROOT.parent / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.config import Settings
from mim_control_plane.domain.models import MaintenanceJobStatus
from mim_control_plane.runtime import RuntimeEnvironment, RuntimeMode
from tests.fakes import build_startup_mapping

NOW = datetime(2026, 8, 4, 1, 0, 0, tzinfo=UTC)
LEASED_AT = NOW
STARTED_AT = NOW + timedelta(seconds=5)
FINISHED_AT = NOW + timedelta(minutes=2)


class LifecycleJobTests(unittest.TestCase):
    def test_main_rejects_cli_arguments_before_building_dependencies(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.lifecycle")
        built = False

        def forbidden_builder(*args: object, **kwargs: object) -> object:
            nonlocal built
            built = True
            raise AssertionError("sweep must not be built")

        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = job.main(
            argv=("--user=person@madup.com",),
            environ={},
            stdout=stdout,
            stderr=stderr,
            sweep_builder=forbidden_builder,
        )

        self.assertEqual(exit_code, 2)
        self.assertFalse(built)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"event": "mim.lifecycle_maintenance", "status": "invalid_invocation"},
        )
        self.assertNotIn("person@madup.com", stderr.getvalue())

    def test_build_sweep_accepts_runtime_bootstrap_like_objects(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.lifecycle")
        settings = Settings.from_mapping(build_startup_mapping())
        credentials = object()
        source = BootstrapLike(
            public_settings=settings,
            admin_members=("group:mim-admins@madup.com", "user:operator.test@madup.com"),
            breakglass_members=("group:security-review@madup.com",),
            project_number="123456789012",
            desired_state_signing_secret_version=(
                "projects/123456789012/secrets/runtime-bootstrap/versions/7"
            ),
        )
        captured: dict[str, object] = {}
        expected = object()

        def store_factory(**kwargs: object) -> object:
            captured["store_settings"] = settings
            captured["store_credentials_loader"] = kwargs["credentials_loader"]
            return "store"

        def lifecycle_worker_builder(**kwargs: object) -> object:
            captured["lifecycle"] = kwargs
            return "lifecycle-worker"

        def hold_resolver_builder(**kwargs: object) -> object:
            captured["holds"] = kwargs
            return "hold-resolver"

        def sweep_factory(**kwargs: object) -> object:
            captured["sweep"] = kwargs
            return expected

        built = job.build_lifecycle_sweep(
            source,
            metadata_credentials_loader=lambda: credentials,
            store_factory=store_factory,
            lifecycle_worker_builder=lifecycle_worker_builder,
            hold_resolver_builder=hold_resolver_builder,
            sweep_factory=sweep_factory,
        )

        self.assertIs(built, expected)
        self.assertIs(captured["store_settings"], settings)
        self.assertIs(captured["store_credentials_loader"](), credentials)
        self.assertEqual(
            captured["lifecycle"],
            {
                "clock": job.utcnow,
                "credentials": credentials,
                "settings": settings,
                "source": source,
                "store": "store",
            },
        )
        self.assertEqual(
            captured["holds"],
            {
                "credentials": credentials,
                "settings": settings,
                "source": source,
                "store": "store",
            },
        )
        self.assertEqual(
            captured["sweep"],
            {
                "store": "store",
                "lifecycle": "lifecycle-worker",
                "hold_resolver": "hold-resolver",
            },
        )

    def test_production_worker_threads_breakglass_members_to_compute_manager(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.lifecycle")
        settings = Settings.from_mapping(build_startup_mapping())
        source = BootstrapLike(
            public_settings=settings,
            admin_members=("group:mim-admins@madup.com", "user:operator.test@madup.com"),
            breakglass_members=(
                "group:security-review@madup.com",
                "user:reviewer@madup.com",
            ),
            project_number="123456789012",
            desired_state_signing_secret_version=(
                "projects/123456789012/secrets/runtime-bootstrap/versions/7"
            ),
        )
        captured: dict[str, object] = {}

        with (
            mock.patch(
                "google.cloud.firestore_v1.Client",
                return_value="slack-client",
            ),
            mock.patch(
                "google.cloud.scheduler_v1.CloudSchedulerClient",
                return_value="scheduler-client",
            ),
            mock.patch(
                "google.cloud.run_v2.ServicesClient",
                return_value="services-client",
            ),
            mock.patch(
                "google.cloud.run_v2.JobsClient",
                return_value="jobs-client",
            ),
            mock.patch(
                "google.cloud.secretmanager_v1.SecretManagerServiceClient",
                return_value="secret-client",
            ),
            mock.patch(
                "mim_control_plane.adapters.google_rest.build_authorized_session",
                return_value="authorized-session",
            ),
            mock.patch(
                "mim_control_plane.adapters.firestore_slack_oauth.FirestoreSlackOAuthRepository",
                side_effect=lambda **kwargs: ("repo", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.lifecycle_effects.LifecycleAuditSessionGate",
                side_effect=lambda **kwargs: ("sessions", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.lifecycle_effects.LifecycleIapAccessManager",
                side_effect=lambda **kwargs: ("access", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.lifecycle_effects.LifecycleSecretBindingManager",
                side_effect=lambda **kwargs: ("secret_bindings", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.lifecycle_effects.LifecycleSlackGrantManager",
                side_effect=lambda **kwargs: ("slack_grants", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.lifecycle_effects.LifecycleAuditNotifier",
                side_effect=lambda **kwargs: ("notifier", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.lifecycle_effects.LifecycleAuditTransferManager",
                side_effect=lambda **kwargs: ("transfer", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.lifecycle_effects.LifecycleScheduleManager",
                side_effect=lambda **kwargs: ("schedules", kwargs),
            ),
            mock.patch(
                "mim_control_plane.adapters.lifecycle_effects.LifecycleComputeManager",
                side_effect=lambda **kwargs: ("compute", kwargs),
            ),
            mock.patch(
                "mim_control_plane.workers.lifecycle.LifecycleWorker",
                side_effect=lambda **kwargs: captured.setdefault("worker", kwargs),
            ),
        ):
            built = job._build_production_lifecycle_worker(
                settings=settings,
                source=source,
                store="store",
                credentials=object(),
                clock=job.utcnow,
            )

        worker_kwargs = captured["worker"]
        self.assertIs(built, worker_kwargs)
        self.assertEqual(
            worker_kwargs["compute"],  # type: ignore[index]
            (
                "compute",
                {
                    "store": "store",
                    "services_client": "services-client",
                    "jobs_client": "jobs-client",
                    "scheduler_client": "scheduler-client",
                    "project_number": source.project_number,
                    "reviewed_breakglass_members": source.breakglass_members,
                },
            ),
        )

    def test_main_emits_success_after_leased_run(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.lifecycle")
        common = importlib.import_module("mim_control_plane.jobs.maintenance_common")
        claim = common.OverlapLeaseClaim(
            token="private-lease-token",
            expires_at=NOW + timedelta(minutes=10),
        )
        calls: list[tuple[str, object]] = []
        persisted: list[tuple[str, object]] = []

        class Sweep:
            def run(self, *, now: datetime) -> object:
                calls.append(("run", now))
                return job.MaintenanceSweepResult(
                    processed_users=2,
                    failed_users=0,
                    replayed_users=1,
                    replayed_actions=1,
                    executed_actions=1,
                    noop_actions=1,
                    cancelled_actions=0,
                )

        class Lease:
            def try_acquire(self, *, now: datetime, duration: timedelta) -> object:
                calls.append(("acquire", (now, duration)))
                return claim

            def release(self, supplied: object) -> None:
                calls.append(("release", supplied))

        class StatusStore:
            def record_maintenance_job_started(self, **kwargs: object) -> MaintenanceJobStatus:
                persisted.append(("started", kwargs))
                return MaintenanceJobStatus(
                    job_name="lifecycle",
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
                    job_name="lifecycle",
                    run_id="run-1",
                    started_at=NOW,
                    finished_at=NOW,
                    succeeded_at=NOW,
                    failed_at=None,
                    outcome="completed",
                    summary=(("processed_users", 2),),
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
            sweep_builder=lambda source: Sweep(),
            lease_builder=lambda source: Lease(),
            status_store_builder=lambda source: StatusStore(),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "cancelled_actions": 0,
                "event": "mim.lifecycle_maintenance",
                "executed_actions": 1,
                "failed_users": 0,
                "noop_actions": 1,
                "processed_users": 2,
                "replayed_actions": 1,
                "replayed_users": 1,
                "status": "completed",
            },
        )
        self.assertNotIn("private-lease-token", stdout.getvalue())
        self.assertEqual(persisted[0][1]["job_name"], "lifecycle")
        self.assertEqual(persisted[1][1]["outcome"], "completed")
        self.assertEqual(persisted[0][1]["started_at"], STARTED_AT)
        self.assertEqual(persisted[1][1]["finished_at"], FINISHED_AT)
        self.assertEqual(
            [name for name, _ in calls],
            ["acquire", "run", "release"],
        )
        self.assertEqual(calls[0][1], (LEASED_AT, timedelta(minutes=10)))
        self.assertEqual(calls[1][1], STARTED_AT)

    def test_main_returns_generic_failure_when_metadata_loader_raises(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.lifecycle")
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = job.main(
            argv=(),
            environ=runtime_environ(),
            stdout=stdout,
            stderr=stderr,
            dependencies=job.runtime.ProductionDependencies(
                metadata_credentials_loader=lambda: (_ for _ in ()).throw(
                    RuntimeError("leaked bootstrap secret")
                )
            ),
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"event": "mim.lifecycle_maintenance", "status": "failed"},
        )
        self.assertNotIn("leaked bootstrap secret", stderr.getvalue())

    def test_main_returns_failure_for_isolated_user_errors_and_redacts_exceptions(
        self,
    ) -> None:
        job = importlib.import_module("mim_control_plane.jobs.lifecycle")
        common = importlib.import_module("mim_control_plane.jobs.maintenance_common")
        claim = common.OverlapLeaseClaim(
            token="private-lease-token",
            expires_at=NOW + timedelta(minutes=10),
        )

        class Lease:
            def try_acquire(self, *, now: datetime, duration: timedelta) -> object:
                del now, duration
                return claim

            def release(self, supplied: object) -> None:
                self.released = supplied

        stdout = io.StringIO()
        stderr = io.StringIO()
        persisted: list[dict[str, object]] = []
        times = iter((LEASED_AT, STARTED_AT, FINISHED_AT))

        def clock() -> datetime:
            return next(times)

        class StatusStore(_NoopStatusStore):
            def record_maintenance_job_terminal(
                self,
                **kwargs: object,
            ) -> MaintenanceJobStatus:
                persisted.append(dict(kwargs))
                return super().record_maintenance_job_terminal(**kwargs)

        class Sweep:
            def run(self, *, now: datetime) -> object:
                del now
                return job.MaintenanceSweepResult(
                    processed_users=1,
                    failed_users=1,
                    replayed_users=0,
                    replayed_actions=0,
                    executed_actions=0,
                    noop_actions=0,
                    cancelled_actions=0,
                )

        exit_code = job.main(
            argv=(),
            environ=runtime_environ(),
            stdout=stdout,
            stderr=stderr,
            clock=clock,
            runtime_loader=lambda mapping, dependencies: runtime_environment(),
            sweep_builder=lambda source: Sweep(),
            lease_builder=lambda source: Lease(),
            status_store_builder=lambda source: StatusStore(),
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "cancelled_actions": 0,
                "event": "mim.lifecycle_maintenance",
                "executed_actions": 0,
                "failed_users": 1,
                "noop_actions": 0,
                "processed_users": 1,
                "replayed_actions": 0,
                "replayed_users": 0,
                "status": "failed",
            },
        )
        self.assertEqual(persisted[0]["outcome"], "failed")
        self.assertEqual(persisted[0]["finished_at"], FINISHED_AT)
        self.assertEqual(persisted[0]["failure_class"], "partial_user_failure")

    def test_overlapping_skip_does_not_clobber_active_latest_status(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.lifecycle")
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

        class ActiveSweep:
            def run(self, *, now: datetime) -> object:
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
                    sweep_builder=lambda source: OverlapSweep(),
                    lease_builder=lambda source: lease,
                    status_store_builder=lambda source: store,
                )
                outputs["overlap_exit"] = overlap_exit
                outputs["overlap_stdout"] = json.loads(overlap_stdout.getvalue())
                outputs["overlap_stderr"] = overlap_stderr.getvalue()
                return job.MaintenanceSweepResult(
                    processed_users=1,
                    failed_users=0,
                    replayed_users=0,
                    replayed_actions=0,
                    executed_actions=1,
                    noop_actions=0,
                    cancelled_actions=0,
                )

        class OverlapSweep:
            def run(self, *, now: datetime) -> object:
                raise AssertionError(f"overlap sweep must not run at {now!r}")

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
                sweep_builder=lambda source: ActiveSweep(),
                lease_builder=lambda source: lease,
                status_store_builder=lambda source: store,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(outputs["overlap_exit"], 0)
        self.assertEqual(
            outputs["overlap_stdout"],
            {"event": "mim.lifecycle_maintenance", "status": "skipped_overlap"},
        )
        self.assertEqual(outputs["overlap_stderr"], "")
        self.assertEqual(outputs["active_started_at"], STARTED_AT)
        status = store.get_maintenance_job_status("lifecycle")
        self.assertEqual(status.run_id, "run-active")
        self.assertEqual(status.outcome, "completed")
        self.assertEqual(status.started_at, STARTED_AT)
        self.assertEqual(status.finished_at, FINISHED_AT)
        self.assertEqual(status.version, 2)

    def test_main_requires_lifecycle_mode_and_mutations(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.lifecycle")
        built = False

        def forbidden_builder(*args: object, **kwargs: object) -> object:
            nonlocal built
            built = True
            raise AssertionError("sweep must not be built")

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
                    sweep_builder=forbidden_builder,
                )
                self.assertEqual(exit_code, 1)
                self.assertFalse(built)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    json.loads(stderr.getvalue()),
                    {"event": "mim.lifecycle_maintenance", "status": "failed"},
                )


def runtime_environ() -> dict[str, str]:
    return {
        "MIM_RUNTIME_MODE": "lifecycle",
        "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION": (
            "projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7"
        ),
        "MIM_ENABLE_MUTATIONS": "true",
    }


def runtime_environment(
    *,
    mode: RuntimeMode = RuntimeMode.LIFECYCLE,
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
            breakglass_members=(),
            project_number="123456789012",
            desired_state_signing_secret_version=(
                "projects/123456789012/secrets/runtime-bootstrap/versions/7"
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class BootstrapLike:
    public_settings: Settings
    admin_members: tuple[str, ...]
    breakglass_members: tuple[str, ...]
    project_number: str
    desired_state_signing_secret_version: str


class _NoopStatusStore:
    def record_maintenance_job_started(self, **kwargs: object) -> MaintenanceJobStatus:
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

    def record_maintenance_job_terminal(self, **kwargs: object) -> MaintenanceJobStatus:
        outcome = str(kwargs["outcome"])
        finished_at = kwargs["finished_at"]  # type: ignore[assignment]
        return MaintenanceJobStatus(
            job_name=str(kwargs["job_name"]),
            run_id=str(kwargs["run_id"]),
            started_at=NOW,
            finished_at=finished_at,
            succeeded_at=finished_at if outcome == "completed" else None,
            failed_at=finished_at if outcome == "failed" else None,
            outcome=outcome,
            summary=kwargs["summary"],  # type: ignore[arg-type]
            failure_code=kwargs.get("failure_code"),  # type: ignore[arg-type]
            failure_class=kwargs.get("failure_class"),  # type: ignore[arg-type]
            version=2,
        )


if __name__ == "__main__":
    unittest.main()
