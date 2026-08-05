"""Cloud Run Job entrypoint for deterministic lifecycle maintenance sweeps."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol, TextIO, cast

from google.auth.credentials import Credentials

from mim_control_plane import runtime
from mim_control_plane.adapters.firestore_store import FirestoreStore
from mim_control_plane.adapters.maintenance_state import (
    FirestoreLifecycleHoldResolver,
    FirestoreNamedOverlapLease,
)
from mim_control_plane.config import Settings
from mim_control_plane.domain.models import MaintenanceJobStatus
from mim_control_plane.jobs.maintenance_common import (
    MaintenanceStatusStore,
    OverlapLease,
    OverlapLeaseClaim,
    failure_metadata,
    generate_run_id,
    require_job_time,
    resolve_settings,
    summarize_counts,
    utcnow,
    write_event,
)
from mim_control_plane.ports.store import Store
from mim_control_plane.workers.maintenance import (
    MaintenanceSweep,
    MaintenanceSweepResult,
)

_EVENT = "mim.lifecycle_maintenance"
_LEASE_DURATION = timedelta(minutes=10)


class _MaintenanceSweep(Protocol):
    def run(self, *, now: datetime) -> MaintenanceSweepResult: ...


_StatusStoreBuilder = Callable[[Mapping[str, object] | object], MaintenanceStatusStore]


class _LifecycleBootstrap(Protocol):
    public_settings: Settings
    admin_members: tuple[str, ...]
    breakglass_members: tuple[str, ...]
    project_number: str


def build_lifecycle_sweep(
    source: Mapping[str, object] | object,
    *,
    clock: Callable[[], datetime] = utcnow,
    metadata_credentials_loader: Callable[[], Credentials] | None = None,
    store_factory: Callable[..., object] = FirestoreStore,
    lifecycle_worker_builder: Callable[..., object] | None = None,
    hold_resolver_builder: Callable[..., object] | None = None,
    sweep_factory: Callable[..., object] = MaintenanceSweep,
) -> _MaintenanceSweep:
    settings = resolve_settings(source)
    bootstrap = _require_lifecycle_bootstrap(source)
    default_loader = cast(
        Callable[[], Credentials],
        runtime.ProductionDependencies().metadata_credentials_loader,
    )
    shared_credentials_loader = (
        default_loader
        if metadata_credentials_loader is None
        else metadata_credentials_loader
    )
    credentials = shared_credentials_loader()
    store = cast(
        Store,
        store_factory(settings=settings, credentials_loader=lambda: credentials),
    )
    lifecycle: object
    if lifecycle_worker_builder is None:
        lifecycle = _build_production_lifecycle_worker(
            settings=settings,
            source=bootstrap,
            store=store,
            credentials=credentials,
            clock=clock,
        )
    else:
        lifecycle = lifecycle_worker_builder(
            settings=settings,
            source=source,
            store=store,
            credentials=credentials,
            clock=clock,
        )
    hold_resolver: object
    if hold_resolver_builder is None:
        hold_resolver = FirestoreLifecycleHoldResolver(
            settings=settings,
            store=store,
            credentials_loader=lambda: credentials,
        )
    else:
        hold_resolver = hold_resolver_builder(
            settings=settings,
            source=source,
            store=store,
            credentials=credentials,
        )
    return cast(
        _MaintenanceSweep,
        sweep_factory(
            store=store,
            lifecycle=lifecycle,
            hold_resolver=hold_resolver,
        ),
    )


def build_overlap_lease(
    source: Mapping[str, object] | object,
    *,
    metadata_credentials_loader: Callable[[], Credentials] | None = None,
    lease_factory: Callable[..., object] | None = None,
) -> OverlapLease:
    settings = resolve_settings(source)
    default_loader = cast(
        Callable[[], Credentials],
        runtime.ProductionDependencies().metadata_credentials_loader,
    )
    shared_credentials_loader = (
        default_loader
        if metadata_credentials_loader is None
        else metadata_credentials_loader
    )
    if lease_factory is None:
        return FirestoreNamedOverlapLease(
            settings=settings,
            lease_name="mim-lifecycle-maintenance",
            credentials_loader=shared_credentials_loader,
        )
    return cast(
        OverlapLease,
        lease_factory(
            settings=settings,
            source=source,
            credentials_loader=shared_credentials_loader,
        ),
    )


def build_status_store(
    source: Mapping[str, object] | object,
    *,
    metadata_credentials_loader: Callable[[], Credentials] | None = None,
    store_factory: Callable[..., object] = FirestoreStore,
) -> MaintenanceStatusStore:
    settings = resolve_settings(source)
    default_loader = cast(
        Callable[[], Credentials],
        runtime.ProductionDependencies().metadata_credentials_loader,
    )
    shared_credentials_loader = (
        default_loader
        if metadata_credentials_loader is None
        else metadata_credentials_loader
    )
    return cast(
        MaintenanceStatusStore,
        store_factory(
            settings=settings,
            credentials_loader=shared_credentials_loader,
        ),
    )


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, object] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    *,
    clock: Callable[[], datetime] = utcnow,
    dependencies: runtime.ProductionDependencies | None = None,
    runtime_loader: Callable[
        [Mapping[str, str], runtime.ProductionDependencies],
        runtime.RuntimeEnvironment,
    ]
    | None = None,
    sweep_builder: (
        Callable[[Mapping[str, object] | object], _MaintenanceSweep] | None
    ) = None,
    lease_builder: Callable[[Mapping[str, object] | object], OverlapLease]
    | None = None,
    status_store_builder: _StatusStoreBuilder | None = None,
) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    if arguments:
        write_event(errors, {"event": _EVENT, "status": "invalid_invocation"})
        return 2
    source = cast(dict[str, str], dict(os.environ if environ is None else environ))
    base_dependencies = (
        runtime.ProductionDependencies() if dependencies is None else dependencies
    )
    lease: OverlapLease | None = None
    claim: OverlapLeaseClaim | None = None
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
        build_sweep_fn: Callable[[Mapping[str, object] | object], _MaintenanceSweep]
        if sweep_builder is None:
            def build_sweep(
                source: Mapping[str, object] | object,
            ) -> _MaintenanceSweep:
                return build_lifecycle_sweep(
                    source,
                    clock=clock,
                    metadata_credentials_loader=lambda: credentials,
                )
            build_sweep_fn = build_sweep
        else:
            build_sweep_fn = cast(
                Callable[[Mapping[str, object] | object], _MaintenanceSweep],
                sweep_builder,
            )
        build_lease_fn: Callable[[Mapping[str, object] | object], OverlapLease]
        if lease_builder is None:
            def build_lease(source: Mapping[str, object] | object) -> OverlapLease:
                return build_overlap_lease(
                    source,
                    metadata_credentials_loader=lambda: credentials,
                )
            build_lease_fn = build_lease
        else:
            build_lease_fn = cast(
                Callable[[Mapping[str, object] | object], OverlapLease],
                lease_builder,
            )
        build_status_store_fn: _StatusStoreBuilder
        if status_store_builder is None:
            def build_runtime_status_store(
                source: Mapping[str, object] | object,
            ) -> MaintenanceStatusStore:
                return build_status_store(
                    source,
                    metadata_credentials_loader=lambda: credentials,
                )
            build_status_store_fn = build_runtime_status_store
        else:
            build_status_store_fn = status_store_builder
        now = require_job_time(clock())
        runtime_env = load_runtime(source, runtime_dependencies)
        if runtime_env.mode is not runtime.RuntimeMode.LIFECYCLE:
            raise ValueError("lifecycle maintenance runtime mode is invalid.")
        if runtime_env.mutations_enabled is not True:
            raise ValueError("lifecycle maintenance mutations must be enabled.")
        lease = build_lease_fn(runtime_env.bootstrap)
        claim = lease.try_acquire(now=now, duration=_LEASE_DURATION)
        if claim is None:
            write_event(output, {"event": _EVENT, "status": "skipped_overlap"})
            return 0
        status_store = build_status_store_fn(runtime_env.bootstrap)
        run_id = generate_run_id()
        started_at = require_job_time(clock())
        started_status = cast(
            MaintenanceJobStatus,
            status_store.record_maintenance_job_started(
                job_name="lifecycle",
                run_id=run_id,
                started_at=started_at,
            ),
        )
        sweep = build_sweep_fn(runtime_env.bootstrap)
        result = sweep.run(now=started_at)
        if not isinstance(result, MaintenanceSweepResult):
            raise ValueError("lifecycle maintenance result is invalid.")
        if started_status is None or status_store is None or run_id is None:
            raise ValueError("lifecycle maintenance status state is invalid.")
        outcome = "failed" if result.failed_users > 0 else "completed"
        finished_at = require_job_time(clock())
        status_store.record_maintenance_job_terminal(
            job_name="lifecycle",
            run_id=run_id,
            expected_version=started_status.version,
            finished_at=finished_at,
            outcome=outcome,
            summary=summarize_counts(
                cancelled_actions=result.cancelled_actions,
                executed_actions=result.executed_actions,
                failed_users=result.failed_users,
                noop_actions=result.noop_actions,
                processed_users=result.processed_users,
                replayed_actions=result.replayed_actions,
                replayed_users=result.replayed_users,
            ),
            failure_code="user_failures" if outcome == "failed" else None,
            failure_class="partial_user_failure" if outcome == "failed" else None,
        )
        _release_quietly(lease, claim)
        claim = None
        payload = _event_payload(result)
        if result.failed_users > 0:
            write_event(errors, payload | {"status": "failed"})
            return 1
        write_event(output, payload | {"status": "completed"})
        return 0
    except Exception as exc:
        if lease is not None and claim is not None:
            _release_quietly(lease, claim)
        if (
            started_status is not None
            and status_store is not None
            and run_id is not None
        ):
            try:
                failure_code, failure_class = failure_metadata(exc)
                status_store.record_maintenance_job_terminal(
                    job_name="lifecycle",
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
        write_event(errors, {"event": _EVENT, "status": "failed"})
        return 1


def _build_production_lifecycle_worker(
    *,
    settings: object,
    source: _LifecycleBootstrap,
    store: Store,
    credentials: Credentials,
    clock: Callable[[], datetime],
) -> object:
    from google.cloud import firestore_v1, run_v2, scheduler_v1, secretmanager_v1

    from mim_control_plane.adapters.firestore_slack_oauth import (
        FirestoreSlackOAuthRepository,
    )
    from mim_control_plane.adapters.google_rest import build_authorized_session
    from mim_control_plane.adapters.lifecycle_effects import (
        LifecycleAuditNotifier,
        LifecycleAuditSessionGate,
        LifecycleAuditTransferManager,
        LifecycleComputeManager,
        LifecycleIapAccessManager,
        LifecycleScheduleManager,
        LifecycleSecretBindingManager,
        LifecycleSlackGrantManager,
    )
    from mim_control_plane.workers.lifecycle import LifecycleWorker

    bootstrap = _require_lifecycle_bootstrap(source)
    slack_client = firestore_v1.Client(
        project=bootstrap.public_settings.project_id,
        database=bootstrap.public_settings.firestore_database_id,
        credentials=credentials,
    )
    scheduler_client = scheduler_v1.CloudSchedulerClient(
        credentials=credentials
    )
    authorized_session = build_authorized_session(credentials=credentials)
    return LifecycleWorker(
        store=store,
        sessions=LifecycleAuditSessionGate(store=store, clock=clock),
        access=LifecycleIapAccessManager(
            store=store,
            session=authorized_session,
            project_number=bootstrap.project_number,
            admin_members=bootstrap.admin_members,
        ),
        secret_bindings=LifecycleSecretBindingManager(
            store=store,
            client=secretmanager_v1.SecretManagerServiceClient(
                credentials=credentials
            ),
        ),
        slack_grants=LifecycleSlackGrantManager(
            store=store,
            repository=FirestoreSlackOAuthRepository(client=cast(Any, slack_client)),
            clock=clock,
        ),
        notifier=LifecycleAuditNotifier(store=store, clock=clock),
        transfer=LifecycleAuditTransferManager(store=store, clock=clock),
        schedules=LifecycleScheduleManager(
            store=store,
            client=scheduler_client,
            project_number=bootstrap.project_number,
        ),
        compute=LifecycleComputeManager(
            store=store,
            services_client=run_v2.ServicesClient(
                credentials=credentials
            ),
            jobs_client=run_v2.JobsClient(credentials=credentials),
            scheduler_client=scheduler_client,
            project_number=bootstrap.project_number,
            reviewed_breakglass_members=bootstrap.breakglass_members,
        ),
    )


def _require_lifecycle_bootstrap(source: object) -> _LifecycleBootstrap:
    required = (
        "admin_members",
        "breakglass_members",
        "project_number",
        "public_settings",
    )
    for field_name in required:
        if not hasattr(source, field_name):
            raise ValueError("lifecycle maintenance bootstrap is invalid.")
    return cast(_LifecycleBootstrap, source)


def _release_quietly(lease: OverlapLease, claim: OverlapLeaseClaim) -> None:
    try:
        lease.release(claim)
    except Exception:
        pass


def _terminal_status_time(
    *,
    clock: Callable[[], datetime],
    fallback: datetime,
) -> datetime:
    try:
        return require_job_time(clock())
    except Exception:
        return fallback


def _event_payload(result: MaintenanceSweepResult) -> dict[str, object]:
    return {
        "cancelled_actions": result.cancelled_actions,
        "event": _EVENT,
        "executed_actions": result.executed_actions,
        "failed_users": result.failed_users,
        "noop_actions": result.noop_actions,
        "processed_users": result.processed_users,
        "replayed_actions": result.replayed_actions,
        "replayed_users": result.replayed_users,
    }


if __name__ == "__main__":
    raise SystemExit(main(environ=os.environ))
