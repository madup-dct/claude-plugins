"""Cloud Run Job entrypoint for hourly billing ingest and activity rollups."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol, TextIO, cast

from google.auth.credentials import Credentials

from mim_control_plane import runtime
from mim_control_plane.adapters.firestore_store import FirestoreStore
from mim_control_plane.adapters.maintenance_state import FirestoreNamedOverlapLease
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
from mim_control_plane.workers.usage_ingest import (
    ActivityIngestResult,
    BillingIngestResult,
    UsageIngestWorker,
)

_EVENT = "mim.usage_ingest"
_LEASE_DURATION = timedelta(minutes=10)


class _UsageWorker(Protocol):
    def ingest_billing(self, *, now: datetime) -> BillingIngestResult: ...

    def rollup_persisted_activity(self, *, now: datetime) -> ActivityIngestResult: ...


_StatusStoreBuilder = Callable[[Mapping[str, object] | object], MaintenanceStatusStore]


class _UsageBootstrap(Protocol):
    public_settings: Settings
    admin_members: tuple[str, ...]
    project_number: str


def build_usage_worker(
    source: Mapping[str, object] | object,
    *,
    clock: Callable[[], datetime] = utcnow,
    metadata_credentials_loader: Callable[[], Credentials] | None = None,
    store_factory: Callable[..., object] = FirestoreStore,
    billing_builder: Callable[..., object] | None = None,
    enforcer_builder: Callable[..., object] | None = None,
    worker_factory: Callable[..., object] = UsageIngestWorker,
) -> _UsageWorker:
    settings = resolve_settings(source)
    bootstrap = _require_usage_bootstrap(source)
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
    billing: object
    if billing_builder is None:
        billing = _build_production_billing_source(
            settings=settings,
            source=bootstrap,
            store=store,
            credentials=credentials,
            clock=clock,
        )
    else:
        billing = billing_builder(
            settings=settings,
            source=source,
            store=store,
            credentials=credentials,
            clock=clock,
        )
    enforcer: object
    if enforcer_builder is None:
        enforcer = _build_production_cost_enforcer(
            settings=settings,
            source=bootstrap,
            store=store,
            credentials=credentials,
            clock=clock,
        )
    else:
        enforcer = enforcer_builder(
            settings=settings,
            source=source,
            store=store,
            credentials=credentials,
            clock=clock,
        )
    return cast(
        _UsageWorker,
        worker_factory(
            store=store,
            billing=billing,
            retention=store,
            enforcer=enforcer,
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
            lease_name="mim-usage-ingest",
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
    worker_builder: Callable[[Mapping[str, object] | object], _UsageWorker]
    | None = None,
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
        build_worker_fn: Callable[[Mapping[str, object] | object], _UsageWorker]
        if worker_builder is None:
            def build_worker(source: Mapping[str, object] | object) -> _UsageWorker:
                return build_usage_worker(
                    source,
                    clock=clock,
                    metadata_credentials_loader=lambda: credentials,
                )
            build_worker_fn = build_worker
        else:
            build_worker_fn = cast(
                Callable[[Mapping[str, object] | object], _UsageWorker],
                worker_builder,
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
        if runtime_env.mode is not runtime.RuntimeMode.USAGE_INGEST:
            raise ValueError("usage ingest runtime mode is invalid.")
        if runtime_env.mutations_enabled is not True:
            raise ValueError("usage ingest mutations must be enabled.")
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
                job_name="usage-ingest",
                run_id=run_id,
                started_at=started_at,
            ),
        )
        worker = build_worker_fn(runtime_env.bootstrap)
        billing = worker.ingest_billing(now=started_at)
        activity = worker.rollup_persisted_activity(now=started_at)
        if not isinstance(billing, BillingIngestResult) or not isinstance(
            activity,
            ActivityIngestResult,
        ):
            raise ValueError("usage ingest result is invalid.")
        if started_status is None or status_store is None or run_id is None:
            raise ValueError("usage ingest status state is invalid.")
        finished_at = require_job_time(clock())
        status_store.record_maintenance_job_terminal(
            job_name="usage-ingest",
            run_id=run_id,
            expected_version=started_status.version,
            finished_at=finished_at,
            outcome="completed",
            summary=summarize_counts(
                activity_rollup_days=len(activity.organization_rollups),
                billing_appended_entries=len(billing.appended_entry_ids),
                billing_ignored_entries=len(billing.ignored_entry_ids),
                billing_updated_entries=len(billing.updated_entry_ids),
                expired_activity_events=len(activity.expired_event_ids),
            ),
        )
        _release_quietly(lease, claim)
        claim = None
        write_event(
            output,
            {
                "activity_rollup_days": len(activity.organization_rollups),
                "billing_appended_entries": len(billing.appended_entry_ids),
                "billing_ignored_entries": len(billing.ignored_entry_ids),
                "billing_updated_entries": len(billing.updated_entry_ids),
                "event": _EVENT,
                "expired_activity_events": len(activity.expired_event_ids),
                "status": "completed",
            },
        )
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
                    job_name="usage-ingest",
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


def _build_production_billing_source(
    *,
    settings: object,
    source: _UsageBootstrap,
    store: Store,
    credentials: Credentials,
    clock: Callable[[], datetime],
) -> object:
    del settings, clock
    from google.cloud import bigquery

    from mim_control_plane.adapters.billing_export import BigQueryBillingExportAdapter

    bootstrap = _require_usage_bootstrap(source)
    return BigQueryBillingExportAdapter(
        client=bigquery.Client(
            project=bootstrap.public_settings.project_id,
            credentials=cast(Any, credentials),
        ),
        store=store,
        project_id=bootstrap.public_settings.project_id,
    )


def _build_production_cost_enforcer(
    *,
    settings: object,
    source: _UsageBootstrap,
    store: Store,
    credentials: Credentials,
    clock: Callable[[], datetime],
) -> object:
    del settings
    from google.cloud import scheduler_v1

    from mim_control_plane.adapters.cost_enforcement import CostEnforcementAdapter
    from mim_control_plane.adapters.google_rest import build_authorized_session
    from mim_control_plane.adapters.lifecycle_effects import (
        LifecycleIapAccessManager,
        LifecycleScheduleManager,
    )

    bootstrap = _require_usage_bootstrap(source)
    schedule_effects = LifecycleScheduleManager(
        store=store,
        client=scheduler_v1.CloudSchedulerClient(credentials=credentials),
        project_number=bootstrap.project_number,
    )
    workload_access = LifecycleIapAccessManager(
        store=store,
        session=build_authorized_session(credentials=credentials),
        project_number=bootstrap.project_number,
        admin_members=bootstrap.admin_members,
    )
    return CostEnforcementAdapter(
        store=store,
        workload_access=workload_access,
        schedule_effects=schedule_effects,
        project_id=bootstrap.public_settings.project_id,
        clock=clock,
    )


def _require_usage_bootstrap(source: object) -> _UsageBootstrap:
    required = ("admin_members", "project_number", "public_settings")
    for field_name in required:
        if not hasattr(source, field_name):
            raise ValueError("usage ingest bootstrap is invalid.")
    return cast(_UsageBootstrap, source)


if __name__ == "__main__":
    raise SystemExit(main(environ=os.environ))
