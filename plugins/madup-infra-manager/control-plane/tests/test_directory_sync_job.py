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
from mim_control_plane.config import DirectoryRuntimeSettings, Settings
from mim_control_plane.domain.models import MaintenanceJobStatus, UserId
from mim_control_plane.ports.directory import DirectorySyncLeaseClaim
from mim_control_plane.runtime import RuntimeEnvironment, RuntimeMode
from mim_control_plane.workers.identity_sync import DirectoryIdentitySyncResult
from tests.fakes import build_directory_runtime_mapping

NOW = datetime(2026, 8, 3, 4, 0, 0, tzinfo=UTC)
LEASED_AT = NOW
STARTED_AT = NOW + timedelta(seconds=5)
FINISHED_AT = NOW + timedelta(minutes=2)
FAILED_AT = NOW + timedelta(minutes=3)


class DirectorySyncJobTests(unittest.TestCase):
    def test_main_rejects_employee_operational_arguments_before_composition(
        self,
    ) -> None:
        job = importlib.import_module("mim_control_plane.jobs.directory_sync")
        built = False

        def forbidden_builder(*args: object, **kwargs: object) -> object:
            nonlocal built
            built = True
            raise AssertionError("worker must not be built")

        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = job.main(
            argv=("--project=sensitive-project",),
            environ={},
            stdout=stdout,
            stderr=stderr,
            worker_builder=forbidden_builder,
        )

        self.assertEqual(exit_code, 2)
        self.assertFalse(built)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "event": "mim.directory_identity_sync",
                "status": "invalid_invocation",
            },
        )
        self.assertNotIn("sensitive-project", stderr.getvalue())

    def test_build_worker_uses_only_central_settings_and_fixed_policy(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.directory_sync")
        source = BootstrapLike(
            public_settings=Settings.from_mapping(build_directory_runtime_mapping()),
            directory_runtime_settings=DirectoryRuntimeSettings.from_mapping(
                build_directory_runtime_mapping()
            ),
        )
        token_provider = object()
        directory_provider = object()
        repository = object()
        worker = object()
        captured: dict[str, object] = {}
        credentials = object()

        def clock() -> datetime:
            return NOW

        def token_provider_factory(**kwargs: object) -> object:
            captured["directory_settings"] = kwargs["settings"]
            captured["source_credentials_loader"] = kwargs["source_credentials_loader"]
            return token_provider

        def directory_provider_factory(**kwargs: object) -> object:
            captured["directory_provider"] = kwargs
            return directory_provider

        def repository_factory(**kwargs: object) -> object:
            captured["repository_credentials_loader"] = kwargs["credentials_loader"]
            captured["repository_settings"] = kwargs["settings"]
            return repository

        def worker_factory(**kwargs: object) -> object:
            captured["worker"] = kwargs
            return worker

        built = job.build_directory_sync_worker(
            source,
            clock=clock,
            credentials_loader=lambda: credentials,
            token_provider_factory=token_provider_factory,
            directory_provider_factory=directory_provider_factory,
            repository_factory=repository_factory,
            worker_factory=worker_factory,
        )

        self.assertIs(built, worker)
        self.assertIsInstance(
            captured["directory_settings"],
            DirectoryRuntimeSettings,
        )
        self.assertIsInstance(captured["repository_settings"], Settings)
        self.assertEqual(
            captured["repository_settings"].project_id,  # type: ignore[union-attr]
            "mim-prod-123456",
        )
        self.assertEqual(
            captured["repository_settings"].firestore_database_id,  # type: ignore[union-attr]
            "(default)",
        )
        self.assertEqual(
            captured["directory_provider"],
            {
                "settings": captured["directory_settings"],
                "token_provider": token_provider,
                "clock": clock,
            },
        )
        self.assertIs(captured["source_credentials_loader"](), credentials)
        self.assertIs(captured["repository_credentials_loader"](), credentials)
        self.assertEqual(
            captured["worker"],
            {
                "directory": directory_provider,
                "repository": repository,
                "required_group": "mim-users",
                "max_snapshot_age": timedelta(minutes=60),
                "max_collection_duration": timedelta(minutes=5),
                "clock": clock,
            },
        )

    def test_build_lease_uses_the_same_central_project_and_group(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.directory_sync")
        captured: dict[str, object] = {}
        expected = object()
        credentials = object()

        def lease_factory(**kwargs: object) -> object:
            captured.update(kwargs)
            return expected

        built = job.build_directory_sync_lease(
            BootstrapLike(
                public_settings=Settings.from_mapping(build_directory_runtime_mapping()),
                directory_runtime_settings=DirectoryRuntimeSettings.from_mapping(
                    build_directory_runtime_mapping()
                ),
            ),
            credentials_loader=lambda: credentials,
            lease_factory=lease_factory,
        )

        self.assertIs(built, expected)
        self.assertIsInstance(captured["settings"], Settings)
        self.assertEqual(
            captured["settings"].project_id,  # type: ignore[union-attr]
            "mim-prod-123456",
        )
        self.assertEqual(captured["required_group"], "mim-users")
        self.assertIs(captured["credentials_loader"](), credentials)

    def test_main_runs_under_a_lease_and_emits_aggregate_success_only(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.directory_sync")
        result = DirectoryIdentitySyncResult(
            snapshot_id="a" * 64,
            replayed=False,
            processed_users=3,
            updated_users=2,
            ignored_directory_users=1,
            active_users=1,
            suspended_users=1,
            offboarded_users=1,
            locked_user_ids=(UserId("usr-private"),),
        )
        claim = DirectorySyncLeaseClaim(
            token="private-lease-token",
            expires_at=NOW + timedelta(minutes=10),
        )
        calls: list[tuple[str, object]] = []
        persisted: list[tuple[str, object]] = []

        class Worker:
            def run(self, *, now: datetime) -> DirectoryIdentitySyncResult:
                calls.append(("run", now))
                return result

        class Lease:
            def try_acquire(
                self,
                *,
                now: datetime,
                duration: timedelta,
            ) -> DirectorySyncLeaseClaim:
                calls.append(("acquire", (now, duration)))
                return claim

            def release(self, supplied: DirectorySyncLeaseClaim) -> None:
                calls.append(("release", supplied))

        def worker_builder(mapping: object, *, clock: object) -> Worker:
            calls.append(("worker_mapping", mapping))
            calls.append(("worker_clock", clock))
            return Worker()

        def lease_builder(mapping: object) -> Lease:
            calls.append(("lease_mapping", mapping))
            return Lease()

        class StatusStore:
            def record_maintenance_job_started(
                self,
                *,
                job_name: str,
                run_id: str,
                started_at: datetime,
            ) -> MaintenanceJobStatus:
                persisted.append(("started", (job_name, run_id, started_at)))
                return MaintenanceJobStatus(
                    job_name=job_name,
                    run_id=run_id,
                    started_at=started_at,
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
                    job_name="identity-sync",
                    run_id="opaque-run-id",
                    started_at=NOW,
                    finished_at=NOW,
                    succeeded_at=NOW,
                    failed_at=None,
                    outcome="completed",
                    summary=(("processed_users", 3),),
                    failure_code=None,
                    failure_class=None,
                    version=2,
                )

        stdout = io.StringIO()
        stderr = io.StringIO()
        times = iter((LEASED_AT, STARTED_AT, FINISHED_AT))

        def clock() -> datetime:
            return next(times)

        with mock.patch.object(job, "generate_run_id", return_value="run-1"):
            exit_code = job.main(
                argv=(),
                environ=runtime_environ(),
                stdout=stdout,
                stderr=stderr,
                clock=clock,
                runtime_loader=lambda mapping, dependencies: runtime_environment(),
                worker_builder=worker_builder,
                lease_builder=lease_builder,
                status_store_builder=lambda mapping: StatusStore(),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "active_users": 1,
                "event": "mim.directory_identity_sync",
                "ignored_directory_users": 1,
                "locked_user_count": 1,
                "offboarded_users": 1,
                "processed_users": 3,
                "replayed": False,
                "snapshot_id": "a" * 64,
                "status": "completed",
                "suspended_users": 1,
                "updated_users": 2,
            },
        )
        self.assertNotIn("usr-private", stdout.getvalue())
        self.assertNotIn("private-lease-token", stdout.getvalue())
        self.assertEqual(persisted[0][0], "started")
        self.assertEqual(
            persisted[0][1],
            ("identity-sync", "run-1", STARTED_AT),
        )
        self.assertEqual(persisted[1][0], "terminal")
        self.assertEqual(persisted[1][1]["job_name"], "identity-sync")
        self.assertEqual(persisted[1][1]["outcome"], "completed")
        self.assertEqual(persisted[1][1]["finished_at"], FINISHED_AT)
        self.assertEqual(
            persisted[1][1]["summary"],
            (
                ("active_users", 1),
                ("ignored_directory_users", 1),
                ("locked_user_count", 1),
                ("offboarded_users", 1),
                ("processed_users", 3),
                ("suspended_users", 1),
                ("updated_users", 2),
            ),
        )
        self.assertEqual(
            [name for name, _ in calls],
            [
                "lease_mapping",
                "acquire",
                "worker_mapping",
                "worker_clock",
                "run",
                "release",
            ],
        )
        self.assertEqual(calls[1][1], (LEASED_AT, timedelta(minutes=10)))
        self.assertEqual(calls[4][1], STARTED_AT)

    def test_main_returns_generic_failure_when_metadata_loader_raises(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.directory_sync")
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = job.main(
            argv=(),
            environ=runtime_environ(),
            stdout=stdout,
            stderr=stderr,
            dependencies=job.runtime.ProductionDependencies(
                metadata_credentials_loader=lambda: (_ for _ in ()).throw(
                    RuntimeError("secret bootstrap detail")
                )
            ),
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"event": "mim.directory_identity_sync", "status": "failed"},
        )
        self.assertNotIn("secret bootstrap detail", stderr.getvalue())

    def test_main_skips_overlap_without_reading_directory(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.directory_sync")
        calls: list[str] = []

        class Worker:
            def run(self, *, now: datetime) -> DirectoryIdentitySyncResult:
                raise AssertionError(f"worker must not run at {now!r}")

        class Lease:
            def try_acquire(
                self,
                *,
                now: datetime,
                duration: timedelta,
            ) -> None:
                calls.append("acquire")
                return None

            def release(self, claim: DirectorySyncLeaseClaim) -> None:
                raise AssertionError(f"unowned claim released: {claim!r}")

        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = job.main(
            argv=(),
            environ=runtime_environ(),
            stdout=stdout,
            stderr=stderr,
            clock=lambda: NOW,
            runtime_loader=lambda mapping, dependencies: runtime_environment(),
            worker_builder=lambda mapping, *, clock: Worker(),
            lease_builder=lambda mapping: Lease(),
            status_store_builder=lambda mapping: (_ for _ in ()).throw(
                AssertionError("status store must not be built")
            ),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, ["acquire"])
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "event": "mim.directory_identity_sync",
                "status": "skipped_overlap",
            },
        )

    def test_main_releases_lease_and_redacts_every_failure(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.directory_sync")
        claim = DirectorySyncLeaseClaim(
            token="private-lease-token",
            expires_at=NOW + timedelta(minutes=10),
        )
        calls: list[str] = []

        class ExplosiveCustomError(RuntimeError):
            pass

        class Worker:
            def run(self, *, now: datetime) -> DirectoryIdentitySyncResult:
                calls.append("run")
                raise ExplosiveCustomError(
                    "person@madup.com secret-token mim-prod-123456"
                )

        class Lease:
            def try_acquire(
                self,
                *,
                now: datetime,
                duration: timedelta,
            ) -> DirectorySyncLeaseClaim:
                calls.append("acquire")
                return claim

            def release(self, supplied: DirectorySyncLeaseClaim) -> None:
                if supplied is not claim:
                    raise AssertionError("wrong claim released")
                calls.append("release")

        stdout = io.StringIO()
        stderr = io.StringIO()
        persisted: list[dict[str, object]] = []
        times = iter((LEASED_AT, STARTED_AT, FAILED_AT))

        def clock() -> datetime:
            return next(times)

        class StatusStore(_NoopStatusStore):
            def record_maintenance_job_terminal(
                self,
                **kwargs: object,
            ) -> MaintenanceJobStatus:
                persisted.append(dict(kwargs))
                return super().record_maintenance_job_terminal(**kwargs)

        exit_code = job.main(
            argv=(),
            environ=runtime_environ(),
            stdout=stdout,
            stderr=stderr,
            clock=clock,
            runtime_loader=lambda mapping, dependencies: runtime_environment(),
            worker_builder=lambda mapping, *, clock: Worker(),
            lease_builder=lambda mapping: Lease(),
            status_store_builder=lambda mapping: StatusStore(),
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(calls, ["acquire", "run", "release"])
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"event": "mim.directory_identity_sync", "status": "failed"},
        )
        self.assertEqual(persisted[0]["outcome"], "failed")
        self.assertEqual(persisted[0]["finished_at"], FAILED_AT)
        self.assertEqual(persisted[0]["failure_class"], "internal")
        self.assertNotEqual(
            persisted[0]["failure_class"],
            ExplosiveCustomError.__name__,
        )
        self.assertNotIn("person@madup.com", stderr.getvalue())
        self.assertNotIn("secret-token", stderr.getvalue())
        self.assertNotIn("mim-prod-123456", stderr.getvalue())

    def test_overlapping_skip_does_not_clobber_active_latest_status(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.directory_sync")
        store = MemoryStore()
        lease_claim = DirectorySyncLeaseClaim(
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

            def try_acquire(
                self,
                *,
                now: datetime,
                duration: timedelta,
            ) -> DirectorySyncLeaseClaim | None:
                del now, duration
                if self.active:
                    return None
                self.active = True
                return lease_claim

            def release(self, supplied: DirectorySyncLeaseClaim) -> None:
                self.active = False
                self.released = supplied

        lease = SharedLease()

        class OverlapWorker:
            def run(self, *, now: datetime) -> DirectoryIdentitySyncResult:
                raise AssertionError(f"overlap worker must not run at {now!r}")

        class ActiveWorker:
            def run(self, *, now: datetime) -> DirectoryIdentitySyncResult:
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
                    worker_builder=lambda mapping, *, clock: OverlapWorker(),
                    lease_builder=lambda mapping: lease,
                    status_store_builder=lambda mapping: store,
                )
                outputs["overlap_exit"] = overlap_exit
                outputs["overlap_stdout"] = json.loads(overlap_stdout.getvalue())
                outputs["overlap_stderr"] = overlap_stderr.getvalue()
                return DirectoryIdentitySyncResult(
                    snapshot_id="a" * 64,
                    replayed=False,
                    processed_users=1,
                    updated_users=1,
                    ignored_directory_users=0,
                    active_users=1,
                    suspended_users=0,
                    offboarded_users=0,
                    locked_user_ids=(),
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
                worker_builder=lambda mapping, *, clock: ActiveWorker(),
                lease_builder=lambda mapping: lease,
                status_store_builder=lambda mapping: store,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(outputs["overlap_exit"], 0)
        self.assertEqual(
            outputs["overlap_stdout"],
            {"event": "mim.directory_identity_sync", "status": "skipped_overlap"},
        )
        self.assertEqual(outputs["overlap_stderr"], "")
        self.assertEqual(outputs["active_started_at"], STARTED_AT)
        status = store.get_maintenance_job_status("identity-sync")
        self.assertEqual(status.run_id, "run-active")
        self.assertEqual(status.outcome, "completed")
        self.assertEqual(status.started_at, STARTED_AT)
        self.assertEqual(status.finished_at, FINISHED_AT)
        self.assertEqual(status.version, 2)

    def test_main_redacts_configuration_failures_before_cloud_access(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.directory_sync")
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = job.main(
            argv=(),
            environ={"MIM_PROJECT_ID": "sensitive-project"},
            stdout=stdout,
            stderr=stderr,
            clock=lambda: NOW,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"event": "mim.directory_identity_sync", "status": "failed"},
        )
        self.assertNotIn("sensitive-project", stderr.getvalue())

    def test_main_requires_identity_sync_mode_and_mutations(self) -> None:
        job = importlib.import_module("mim_control_plane.jobs.directory_sync")
        built = False

        def forbidden_builder(*args: object, **kwargs: object) -> object:
            nonlocal built
            built = True
            raise AssertionError("builder must not run")

        stdout = io.StringIO()
        stderr = io.StringIO()

        for runtime_env in (
            runtime_environment(mode=RuntimeMode.LIFECYCLE),
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
                    {"event": "mim.directory_identity_sync", "status": "failed"},
                )


def runtime_environ() -> dict[str, str]:
    return {
        "MIM_RUNTIME_MODE": "identity-sync",
        "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION": (
            "projects/mim-prod-123456/secrets/mim-runtime-bootstrap/versions/7"
        ),
        "MIM_ENABLE_MUTATIONS": "true",
    }


def runtime_environment(
    *,
    mode: RuntimeMode = RuntimeMode.IDENTITY_SYNC,
    mutations_enabled: bool = True,
) -> RuntimeEnvironment:
    return RuntimeEnvironment(
        mode=mode,
        bootstrap_secret_version=runtime_environ()[
            "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION"
        ],
        mutations_enabled=mutations_enabled,
        bootstrap=BootstrapLike(
            public_settings=Settings.from_mapping(build_directory_runtime_mapping()),
            directory_runtime_settings=DirectoryRuntimeSettings.from_mapping(
                build_directory_runtime_mapping()
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class BootstrapLike:
    public_settings: Settings
    directory_runtime_settings: DirectoryRuntimeSettings


class _NoopStatusStore:
    def record_maintenance_job_started(
        self,
        *,
        job_name: str,
        run_id: str,
        started_at: datetime,
    ) -> MaintenanceJobStatus:
        return MaintenanceJobStatus(
            job_name=job_name,
            run_id=run_id,
            started_at=started_at,
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
