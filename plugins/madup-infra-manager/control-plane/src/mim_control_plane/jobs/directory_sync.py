"""Private Cloud Run Job entrypoint for authoritative Directory sync."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, TextIO, cast

from google.auth.credentials import Credentials

from mim_control_plane import runtime
from mim_control_plane.adapters.firestore_directory import (
    FirestoreDirectoryIdentityRepository,
    FirestoreDirectorySyncLease,
)
from mim_control_plane.adapters.firestore_store import FirestoreStore
from mim_control_plane.adapters.google_directory import (
    GoogleDirectoryProvider,
    ImpersonatedDirectoryTokenProvider,
)
from mim_control_plane.config import (
    IDENTITY_MAX_STALENESS_MINUTES,
    DirectoryRuntimeSettings,
    Settings,
)
from mim_control_plane.domain.models import MaintenanceJobStatus
from mim_control_plane.jobs.maintenance_common import (
    MaintenanceStatusStore,
    failure_metadata,
    generate_run_id,
    summarize_counts,
)
from mim_control_plane.ports.directory import (
    DirectorySyncLease,
    DirectorySyncLeaseClaim,
)
from mim_control_plane.workers.identity_sync import (
    DirectoryIdentitySyncResult,
    DirectoryIdentitySyncWorker,
)

_EVENT = "mim.directory_identity_sync"
_MAX_COLLECTION_DURATION = timedelta(minutes=5)
_LEASE_DURATION = timedelta(minutes=10)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class _DirectorySyncWorker(Protocol):
    def run(self, *, now: datetime) -> DirectoryIdentitySyncResult: ...


_WorkerBuilder = Callable[..., _DirectorySyncWorker]
_LeaseBuilder = Callable[[Mapping[str, object] | object], DirectorySyncLease]
_StatusStoreBuilder = Callable[[Mapping[str, object] | object], MaintenanceStatusStore]


def build_directory_sync_worker(
    source: Mapping[str, object] | object,
    *,
    clock: Callable[[], datetime] = _utcnow,
    credentials_loader: Callable[[], object] | None = None,
    token_provider_factory: Callable[..., object] = (
        ImpersonatedDirectoryTokenProvider
    ),
    directory_provider_factory: Callable[..., object] = GoogleDirectoryProvider,
    repository_factory: Callable[..., object] = (FirestoreDirectoryIdentityRepository),
    worker_factory: Callable[..., object] = DirectoryIdentitySyncWorker,
) -> _DirectorySyncWorker:
    """Compose the private worker only from central runtime configuration."""

    settings = _resolve_settings(source)
    directory_settings = _resolve_directory_settings(source)
    shared_credentials_loader = (
        runtime.ProductionDependencies().metadata_credentials_loader
        if credentials_loader is None
        else credentials_loader
    )
    token_provider = token_provider_factory(
        settings=directory_settings,
        source_credentials_loader=shared_credentials_loader,
    )
    directory = directory_provider_factory(
        settings=directory_settings,
        token_provider=token_provider,
        clock=clock,
    )
    repository = repository_factory(
        settings=settings,
        credentials_loader=shared_credentials_loader,
    )
    return cast(
        _DirectorySyncWorker,
        worker_factory(
            directory=directory,
            repository=repository,
            required_group=directory_settings.directory_required_group_label,
            max_snapshot_age=timedelta(minutes=IDENTITY_MAX_STALENESS_MINUTES),
            max_collection_duration=_MAX_COLLECTION_DURATION,
            clock=clock,
        ),
    )


def build_directory_sync_lease(
    source: Mapping[str, object] | object,
    *,
    credentials_loader: Callable[[], object] | None = None,
    lease_factory: Callable[..., object] = FirestoreDirectorySyncLease,
) -> DirectorySyncLease:
    """Compose the overlap lease from the same central configuration."""

    settings = _resolve_settings(source)
    directory_settings = _resolve_directory_settings(source)
    shared_credentials_loader = (
        runtime.ProductionDependencies().metadata_credentials_loader
        if credentials_loader is None
        else credentials_loader
    )
    return cast(
        DirectorySyncLease,
        lease_factory(
            settings=settings,
            required_group=directory_settings.directory_required_group_label,
            credentials_loader=shared_credentials_loader,
        ),
    )


def build_directory_sync_status_store(
    source: Mapping[str, object] | object,
    *,
    credentials_loader: Callable[[], Credentials] | None = None,
    store_factory: Callable[..., object] = FirestoreStore,
) -> MaintenanceStatusStore:
    settings = _resolve_settings(source)
    default_loader = cast(
        Callable[[], Credentials],
        runtime.ProductionDependencies().metadata_credentials_loader,
    )
    shared_credentials_loader = (
        default_loader if credentials_loader is None else credentials_loader
    )
    return cast(
        MaintenanceStatusStore,
        store_factory(
            settings=settings,
            credentials_loader=shared_credentials_loader,
        ),
    )


def _write_event(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    stream.write("\n")


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, object] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    *,
    clock: Callable[[], datetime] = _utcnow,
    dependencies: runtime.ProductionDependencies | None = None,
    runtime_loader: Callable[
        [Mapping[str, str], runtime.ProductionDependencies],
        runtime.RuntimeEnvironment,
    ]
    | None = None,
    worker_builder: Callable[..., _DirectorySyncWorker] | None = None,
    lease_builder: Callable[..., DirectorySyncLease] | None = None,
    status_store_builder: _StatusStoreBuilder | None = None,
) -> int:
    """Run only from centrally configured job infrastructure."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    if arguments:
        _write_event(
            errors,
            {"event": _EVENT, "status": "invalid_invocation"},
        )
        return 2
    mapping = cast(dict[str, str], dict(os.environ if environ is None else environ))
    base_dependencies = (
        runtime.ProductionDependencies() if dependencies is None else dependencies
    )
    claim: DirectorySyncLeaseClaim | None = None
    lease: DirectorySyncLease | None = None
    status_store: MaintenanceStatusStore | None = None
    started_status: MaintenanceJobStatus | None = None
    run_id: str | None = None
    try:
        credentials = cast(Credentials, base_dependencies.metadata_credentials_loader())
        runtime_dependencies = runtime.ProductionDependencies(
            metadata_credentials_loader=lambda: credentials,
            bootstrap_secret_loader=base_dependencies.bootstrap_secret_loader,
            clock=base_dependencies.clock,
        )
        load_runtime = cast(
            Callable[
                [Mapping[str, str], runtime.ProductionDependencies],
                runtime.RuntimeEnvironment,
            ],
            (
                runtime.load_runtime_environment
                if runtime_loader is None
                else runtime_loader
            ),
        )
        build_worker_fn: _WorkerBuilder
        if worker_builder is None:
            def build_worker(
                source: Mapping[str, object] | object,
                *,
                clock: Callable[[], datetime],
            ) -> _DirectorySyncWorker:
                return build_directory_sync_worker(
                    source,
                    clock=clock,
                    credentials_loader=lambda: credentials,
                )
            build_worker_fn = build_worker
        else:
            build_worker_fn = cast(_WorkerBuilder, worker_builder)
        build_lease_fn: _LeaseBuilder
        if lease_builder is None:
            def build_lease(
                source: Mapping[str, object] | object,
            ) -> DirectorySyncLease:
                return build_directory_sync_lease(
                    source,
                    credentials_loader=lambda: credentials,
                )
            build_lease_fn = build_lease
        else:
            build_lease_fn = cast(_LeaseBuilder, lease_builder)
        build_status_store_fn: _StatusStoreBuilder
        if status_store_builder is None:
            def build_status_store(
                source: Mapping[str, object] | object,
            ) -> MaintenanceStatusStore:
                return build_directory_sync_status_store(
                    source,
                    credentials_loader=lambda: credentials,
                )
            build_status_store_fn = build_status_store
        else:
            build_status_store_fn = status_store_builder
        now = _require_job_time(clock())
        runtime_env = load_runtime(mapping, runtime_dependencies)
        if runtime_env.mode is not runtime.RuntimeMode.IDENTITY_SYNC:
            raise ValueError("directory sync runtime mode is invalid.")
        if runtime_env.mutations_enabled is not True:
            raise ValueError("directory sync mutations must be enabled.")
        lease = build_lease_fn(runtime_env.bootstrap)
        claim = lease.try_acquire(now=now, duration=_LEASE_DURATION)
        if claim is None:
            _write_event(output, {"event": _EVENT, "status": "skipped_overlap"})
            return 0
        status_store = build_status_store_fn(runtime_env.bootstrap)
        run_id = generate_run_id()
        started_at = _require_job_time(clock())
        started_status = cast(
            MaintenanceJobStatus,
            status_store.record_maintenance_job_started(
                job_name="identity-sync",
                run_id=run_id,
                started_at=started_at,
            ),
        )
        worker = build_worker_fn(runtime_env.bootstrap, clock=clock)
        result = worker.run(now=started_at)
        if not isinstance(result, DirectoryIdentitySyncResult):
            raise ValueError("directory sync result is invalid.")
        if started_status is None or status_store is None or run_id is None:
            raise ValueError("directory sync status state is invalid.")
        finished_at = _require_job_time(clock())
        status_store.record_maintenance_job_terminal(
            job_name="identity-sync",
            run_id=run_id,
            expected_version=started_status.version,
            finished_at=finished_at,
            outcome="completed",
            summary=summarize_counts(
                active_users=result.active_users,
                ignored_directory_users=result.ignored_directory_users,
                locked_user_count=len(result.locked_user_ids),
                offboarded_users=result.offboarded_users,
                processed_users=result.processed_users,
                suspended_users=result.suspended_users,
                updated_users=result.updated_users,
            ),
        )
        try:
            lease.release(claim)
        finally:
            claim = None
        _write_event(output, _success_event(result))
        return 0
    except Exception as exc:
        if lease is not None and claim is not None:
            try:
                lease.release(claim)
            except Exception:
                pass
        if (
            started_status is not None
            and status_store is not None
            and run_id is not None
        ):
            try:
                failure_code, failure_class = failure_metadata(exc)
                status_store.record_maintenance_job_terminal(
                    job_name="identity-sync",
                    run_id=run_id,
                    expected_version=started_status.version,
                    finished_at=_terminal_status_time(
                        clock=clock,
                        fallback=started_status.started_at,
                    ),
                    outcome="failed",
                    summary=(),
                    failure_code=failure_code,
                    failure_class=failure_class,
                )
            except Exception:
                pass
        _write_event(errors, {"event": _EVENT, "status": "failed"})
        return 1


def _resolve_settings(source: Mapping[str, object] | object) -> Settings:
    if isinstance(source, Mapping):
        return Settings.from_mapping(source)
    settings = getattr(source, "public_settings", None)
    if isinstance(settings, Settings):
        return settings
    raise ValueError("directory sync settings source is invalid.")


def _resolve_directory_settings(
    source: Mapping[str, object] | object,
) -> DirectoryRuntimeSettings:
    if isinstance(source, Mapping):
        return DirectoryRuntimeSettings.from_mapping(source)
    directory_settings = getattr(source, "directory_runtime_settings", None)
    if isinstance(directory_settings, DirectoryRuntimeSettings):
        return directory_settings
    raise ValueError("directory sync settings source is invalid.")


def _require_job_time(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError("directory sync job time is invalid.")
    return value


def _terminal_status_time(
    *,
    clock: Callable[[], datetime],
    fallback: datetime,
) -> datetime:
    try:
        return _require_job_time(clock())
    except Exception:
        return fallback


def _success_event(result: DirectoryIdentitySyncResult) -> dict[str, object]:
    return {
        "active_users": result.active_users,
        "event": _EVENT,
        "ignored_directory_users": result.ignored_directory_users,
        "locked_user_count": len(result.locked_user_ids),
        "offboarded_users": result.offboarded_users,
        "processed_users": result.processed_users,
        "replayed": result.replayed,
        "snapshot_id": result.snapshot_id,
        "status": "completed",
        "suspended_users": result.suspended_users,
        "updated_users": result.updated_users,
    }


if __name__ == "__main__":
    raise SystemExit(main(environ=os.environ))
