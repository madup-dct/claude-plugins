from __future__ import annotations

import dataclasses
import sys
import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias, cast

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.cloud_run_job_dispatch import (  # noqa: E402
    CloudRunJobDispatcher,
)
from mim_control_plane.domain.models import ScheduleId, WorkloadId  # noqa: E402
from mim_control_plane.ports.schedule import (  # noqa: E402
    ScheduledRunReceipt,
    ScheduledRunRequest,
    ScheduleExecutionError,
)

PROJECT_ID = "mim-prod-123456"
REGION = "asia-northeast3"
NOW = datetime(2026, 8, 4, 1, 0, 0, tzinfo=UTC)
LedgerRecord: TypeAlias = dict[str, str | None]


@dataclass(frozen=True, slots=True)
class FakeDuration:
    seconds: int


@dataclass(frozen=True, slots=True)
class FakeEnvVar:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class FakeResources:
    limits: dict[str, str]
    cpu_idle: bool
    startup_cpu_boost: bool = False


@dataclass(frozen=True, slots=True)
class FakeContainer:
    image: str
    command: tuple[str, ...]
    args: tuple[str, ...]
    resources: FakeResources
    env: tuple[FakeEnvVar, ...] = ()
    ports: tuple[str, ...] = ()
    volume_mounts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FakeVpcAccess:
    connector: str = ""


@dataclass(frozen=True, slots=True)
class FakeTaskTemplate:
    service_account: str
    max_retries: int
    timeout: FakeDuration
    containers: tuple[FakeContainer, ...]
    volumes: tuple[str, ...] = ()
    vpc_access: FakeVpcAccess = field(default_factory=FakeVpcAccess)


@dataclass(frozen=True, slots=True)
class FakeExecutionTemplate:
    task_count: int
    parallelism: int
    template: FakeTaskTemplate


@dataclass(frozen=True, slots=True)
class FakeJob:
    name: str
    labels: dict[str, str]
    template: FakeExecutionTemplate


@dataclass(frozen=True, slots=True)
class FakeExecution:
    name: str
    job: str
    labels: dict[str, str]
    task_count: int
    parallelism: int
    template: FakeTaskTemplate


class FakeRunOperation:
    def __init__(self, execution: FakeExecution) -> None:
        self.execution = execution
        self.timeout_calls: list[float | None] = []

    def result(self, timeout: float | None = None) -> FakeExecution:
        self.timeout_calls.append(timeout)
        return self.execution


class FakeJobsClient:
    def __init__(self, *, job: FakeJob, execution: FakeExecution) -> None:
        self.job = job
        self.execution = execution
        self.run_error: Exception | None = None
        self.get_calls: list[object] = []
        self.run_calls: list[object] = []

    def get_job(self, request: object) -> FakeJob:
        self.get_calls.append(request)
        return self.job

    def run_job(self, request: object) -> FakeRunOperation:
        self.run_calls.append(request)
        if self.run_error is not None:
            raise self.run_error
        return FakeRunOperation(self.execution)


class FakeExecutionsClient:
    def __init__(self) -> None:
        self.list_calls: list[object] = []
        self.executions_by_parent: dict[str, tuple[FakeExecution, ...]] = {}

    def list_executions(self, request: object) -> tuple[FakeExecution, ...]:
        self.list_calls.append(request)
        parent = getattr(request, "parent")
        return self.executions_by_parent.get(parent, ())


class FakeDispatchLedger:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], LedgerRecord] = {}
        self.claim_calls: list[tuple[str, str, str]] = []
        self.mark_succeeded_calls: list[tuple[str, str, str]] = []
        self.mark_ambiguous_calls: list[tuple[str, str, str]] = []

    def get(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
    ) -> LedgerRecord | None:
        return self.records.get((schedule_id, tick_at.isoformat()))

    def claim(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
    ) -> LedgerRecord:
        key = (schedule_id, tick_at.isoformat())
        self.claim_calls.append((schedule_id, tick_at.isoformat(), stable_token))
        existing = self.records.get(key)
        if existing is not None:
            return existing
        claimed: LedgerRecord = {
            "state": "claimed",
            "stable_token": stable_token,
            "run_reference": None,
        }
        self.records[key] = claimed
        return claimed

    def mark_succeeded(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
        run_reference: str,
    ) -> LedgerRecord:
        key = (schedule_id, tick_at.isoformat())
        self.mark_succeeded_calls.append(
            (schedule_id, tick_at.isoformat(), run_reference)
        )
        record: LedgerRecord = {
            "state": "succeeded",
            "stable_token": stable_token,
            "run_reference": run_reference,
        }
        self.records[key] = record
        return record

    def mark_ambiguous(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
    ) -> LedgerRecord:
        key = (schedule_id, tick_at.isoformat())
        self.mark_ambiguous_calls.append(
            (schedule_id, tick_at.isoformat(), stable_token)
        )
        record = self.records.get(key) or {"run_reference": None}
        updated: LedgerRecord = {
            "state": "ambiguous",
            "stable_token": stable_token,
            "run_reference": record.get("run_reference"),
        }
        self.records[key] = updated
        return updated


class ClaimMismatchLedger(FakeDispatchLedger):
    def __init__(self, *, returned_state: str) -> None:
        super().__init__()
        self.returned_state = returned_state

    def claim(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
    ) -> LedgerRecord:
        super().claim(
            schedule_id=schedule_id,
            tick_at=tick_at,
            stable_token=stable_token,
        )
        return {
            "state": self.returned_state,
            "stable_token": "f" * 64,
            "run_reference": None,
        }


class MarkAmbiguousMismatchLedger(FakeDispatchLedger):
    def mark_ambiguous(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
    ) -> LedgerRecord:
        super().mark_ambiguous(
            schedule_id=schedule_id,
            tick_at=tick_at,
            stable_token=stable_token,
        )
        return {
            "state": "ambiguous",
            "stable_token": "e" * 64,
            "run_reference": None,
        }


class MarkSucceededMismatchLedger(FakeDispatchLedger):
    def mark_succeeded(
        self,
        *,
        schedule_id: str,
        tick_at: datetime,
        stable_token: str,
        run_reference: str,
    ) -> LedgerRecord:
        super().mark_succeeded(
            schedule_id=schedule_id,
            tick_at=tick_at,
            stable_token=stable_token,
            run_reference=run_reference,
        )
        return {
            "state": "succeeded",
            "stable_token": "d" * 64,
            "run_reference": run_reference,
        }


def workload_job_name(workload_id: str) -> str:
    import hashlib

    suffix = hashlib.sha256(workload_id.encode("utf-8")).hexdigest()[:12]
    return f"projects/{PROJECT_ID}/locations/{REGION}/jobs/mim-job-{suffix}"


def workload_hash(workload_id: str) -> str:
    import hashlib

    return hashlib.sha256(workload_id.encode("utf-8")).hexdigest()[:12]


def service_account_for(workload_id: str) -> str:
    return f"mim-wrk-{workload_hash(workload_id)}@{PROJECT_ID}.iam.gserviceaccount.com"


def image_for(workload_id: str) -> str:
    return (
        f"{REGION}-docker.pkg.dev/{PROJECT_ID}/mim/workloads@sha256:"
        f"{workload_hash(workload_id) * 5 + workload_hash(workload_id)[:4]}"
    )[: len(f"{REGION}-docker.pkg.dev/{PROJECT_ID}/mim/workloads@sha256:") + 64]


def boundary_labels(workload_id: str) -> dict[str, str]:
    return {
        "managed-by": "mim-control-plane",
        "workload-hash": workload_hash(workload_id),
    }


def job_container(
    *,
    workload_id: str = "wrk-1",
    env: tuple[FakeEnvVar, ...] = (),
) -> FakeContainer:
    return FakeContainer(
        image=image_for(workload_id),
        command=("python",),
        args=("main.py",),
        resources=FakeResources(
            limits={"cpu": "1", "memory": "512Mi"},
            cpu_idle=False,
        ),
        env=env,
    )


def sample_job(workload_id: str = "wrk-1") -> FakeJob:
    return FakeJob(
        name=workload_job_name(workload_id),
        labels=boundary_labels(workload_id),
        template=FakeExecutionTemplate(
            task_count=1,
            parallelism=1,
            template=FakeTaskTemplate(
                service_account=service_account_for(workload_id),
                max_retries=1,
                timeout=FakeDuration(seconds=300),
                containers=(job_container(workload_id=workload_id),),
            ),
        ),
    )


def execution_env_pairs(
    *,
    schedule_id: str,
    workload_id: str,
    tick_at: datetime,
    tick_token: str,
    lease_token: str = "lease-1",
) -> tuple[FakeEnvVar, ...]:
    return (
        FakeEnvVar(name="MIM_SCHEDULE_ID", value=schedule_id),
        FakeEnvVar(name="MIM_WORKLOAD_ID", value=workload_id),
        FakeEnvVar(name="MIM_LEASE_TOKEN", value=lease_token),
        FakeEnvVar(name="MIM_TICK_AT", value=tick_at.isoformat()),
        FakeEnvVar(name="MIM_TICK_TOKEN", value=tick_token),
    )


def sample_execution(
    *,
    schedule_id: str = "sch-1",
    workload_id: str = "wrk-1",
    tick_at: datetime = NOW,
    tick_token: str,
    lease_token: str = "lease-1",
    execution_id: str = "exec-1",
) -> FakeExecution:
    return FakeExecution(
        name=f"{workload_job_name(workload_id)}/executions/{execution_id}",
        job=workload_job_name(workload_id),
        labels=boundary_labels(workload_id),
        task_count=1,
        parallelism=1,
        template=FakeTaskTemplate(
            service_account=service_account_for(workload_id),
            max_retries=1,
            timeout=FakeDuration(seconds=300),
            containers=(
                job_container(
                    workload_id=workload_id,
                    env=execution_env_pairs(
                        schedule_id=schedule_id,
                        workload_id=workload_id,
                        tick_at=tick_at,
                        tick_token=tick_token,
                        lease_token=lease_token,
                    ),
                ),
            ),
        ),
    )


def dispatch_request(
    *,
    schedule_id: str = "sch-1",
    workload_id: str = "wrk-1",
    tick_at: datetime = NOW,
    lease_token: str = "lease-1",
) -> ScheduledRunRequest:
    return ScheduledRunRequest(
        schedule_id=ScheduleId(schedule_id),
        workload_id=WorkloadId(workload_id),
        tick_at=tick_at,
        lease_token=lease_token,
    )


def dispatcher(
    *,
    workload_id: str = "wrk-1",
    job: FakeJob | None = None,
    execution: FakeExecution | None = None,
    ledger: FakeDispatchLedger | None = None,
) -> tuple[
    CloudRunJobDispatcher,
    FakeJobsClient,
    FakeExecutionsClient,
    FakeDispatchLedger,
]:
    request = dispatch_request(workload_id=workload_id)
    tick_token = CloudRunJobDispatcher.stable_tick_token(request)
    fake_job = job or sample_job(workload_id)
    fake_execution = execution or sample_execution(
        schedule_id=str(request.schedule_id),
        workload_id=workload_id,
        tick_at=request.tick_at,
        tick_token=tick_token,
    )
    client = FakeJobsClient(job=fake_job, execution=fake_execution)
    executions_client = FakeExecutionsClient()
    fake_ledger = ledger or FakeDispatchLedger()
    return (
        CloudRunJobDispatcher(
            jobs_client=client,
            executions_client=executions_client,
            ledger=fake_ledger,
            project_id=PROJECT_ID,
            region=REGION,
        ),
        client,
        executions_client,
        fake_ledger,
    )


class CloudRunJobDispatcherTests(unittest.TestCase):
    def test_dispatch_runs_the_exact_mim_job_with_stable_tick_overrides(self) -> None:
        adapter, client, _executions, ledger = dispatcher()
        request = dispatch_request()

        receipt = adapter.dispatch(request)

        self.assertEqual(
            receipt,
            ScheduledRunReceipt(
                run_reference=f"{workload_job_name('wrk-1')}/executions/exec-1",
                created=True,
            ),
        )
        get_request = cast(Any, client.get_calls[0])
        self.assertEqual(get_request.name, workload_job_name("wrk-1"))
        run_request = cast(Any, client.run_calls[0])
        self.assertEqual(run_request.name, workload_job_name("wrk-1"))
        self.assertFalse(run_request.validate_only)
        overrides = run_request.overrides
        self.assertEqual(overrides.task_count, 1)
        env_pairs = {
            env.name: env.value for env in overrides.container_overrides[0].env
        }
        self.assertEqual(env_pairs["MIM_SCHEDULE_ID"], "sch-1")
        self.assertEqual(env_pairs["MIM_WORKLOAD_ID"], "wrk-1")
        self.assertEqual(env_pairs["MIM_LEASE_TOKEN"], "lease-1")
        self.assertEqual(env_pairs["MIM_TICK_AT"], NOW.isoformat())
        self.assertEqual(
            env_pairs["MIM_TICK_TOKEN"],
            CloudRunJobDispatcher.stable_tick_token(request),
        )
        self.assertEqual(
            ledger.records[("sch-1", NOW.isoformat())]["state"],
            "succeeded",
        )

    def test_dispatch_replays_completed_claim_without_rerunning_job(self) -> None:
        adapter, client, _executions, ledger = dispatcher()
        request = dispatch_request()
        token = CloudRunJobDispatcher.stable_tick_token(request)
        ledger.mark_succeeded(
            schedule_id="sch-1",
            tick_at=NOW,
            stable_token=token,
            run_reference=f"{workload_job_name('wrk-1')}/executions/existing",
        )

        receipt = adapter.dispatch(request)

        self.assertEqual(
            receipt,
            ScheduledRunReceipt(
                run_reference=f"{workload_job_name('wrk-1')}/executions/existing",
                created=False,
            ),
        )
        self.assertEqual(client.run_calls, [])

    def test_dispatch_denies_existing_record_with_mismatched_stable_token(self) -> None:
        request = dispatch_request()
        expected_token = CloudRunJobDispatcher.stable_tick_token(request)
        for state in ("claimed", "ambiguous", "succeeded"):
            with self.subTest(state=state):
                adapter, client, executions, ledger = dispatcher()
                ledger.records[("sch-1", NOW.isoformat())] = {
                    "state": state,
                    "stable_token": "0" * 64,
                    "run_reference": (
                        f"{workload_job_name('wrk-1')}/executions/existing"
                        if state == "succeeded"
                        else None
                    ),
                }

                with self.assertRaises(ScheduleExecutionError):
                    adapter.dispatch(request)

                self.assertNotEqual("0" * 64, expected_token)
                self.assertEqual(client.run_calls, [])
                self.assertEqual(executions.list_calls, [])
                self.assertEqual(ledger.mark_succeeded_calls, [])

    def test_dispatch_marks_ambiguous_then_reconciles_existing_execution(self) -> None:
        adapter, client, executions, ledger = dispatcher()
        request = dispatch_request()
        token = CloudRunJobDispatcher.stable_tick_token(request)
        client.run_error = RuntimeError("timeout")
        executions.executions_by_parent[workload_job_name("wrk-1")] = (
            sample_execution(
                schedule_id="sch-1",
                workload_id="wrk-1",
                tick_at=NOW,
                tick_token=token,
                execution_id="reconciled",
            ),
        )

        receipt = adapter.dispatch(request)

        self.assertEqual(
            receipt,
            ScheduledRunReceipt(
                run_reference=f"{workload_job_name('wrk-1')}/executions/reconciled",
                created=False,
            ),
        )
        self.assertEqual(
            ledger.mark_ambiguous_calls,
            [("sch-1", NOW.isoformat(), token)],
        )
        self.assertEqual(len(executions.list_calls), 1)
        self.assertEqual(len(ledger.mark_succeeded_calls), 1)

    def test_dispatch_denies_when_ambiguous_claim_cannot_be_reconciled(self) -> None:
        adapter, client, executions, ledger = dispatcher()
        request = dispatch_request()
        token = CloudRunJobDispatcher.stable_tick_token(request)
        ledger.mark_ambiguous(
            schedule_id="sch-1",
            tick_at=NOW,
            stable_token=token,
        )
        executions.executions_by_parent[workload_job_name("wrk-1")] = ()

        with self.assertRaises(ScheduleExecutionError):
            adapter.dispatch(request)

        self.assertEqual(client.run_calls, [])
        self.assertEqual(len(executions.list_calls), 1)
        self.assertEqual(ledger.mark_succeeded_calls, [])

    def test_dispatch_denies_claim_result_with_mismatched_stable_token(self) -> None:
        request = dispatch_request()
        for state in ("claimed", "ambiguous", "succeeded"):
            with self.subTest(state=state):
                ledger = ClaimMismatchLedger(returned_state=state)
                adapter, client, executions, _ledger = dispatcher(ledger=ledger)

                with self.assertRaises(ScheduleExecutionError):
                    adapter.dispatch(request)

                self.assertEqual(client.run_calls, [])
                self.assertEqual(executions.list_calls, [])
                self.assertEqual(ledger.mark_succeeded_calls, [])

    def test_dispatch_denies_mark_results_with_mismatched_stable_token(self) -> None:
        request = dispatch_request()
        token = CloudRunJobDispatcher.stable_tick_token(request)

        ambiguous_ledger = MarkAmbiguousMismatchLedger()
        adapter, client, executions, _ledger = dispatcher(ledger=ambiguous_ledger)
        client.run_error = RuntimeError("timeout")
        executions.executions_by_parent[workload_job_name("wrk-1")] = (
            sample_execution(
                schedule_id="sch-1",
                workload_id="wrk-1",
                tick_at=NOW,
                tick_token=token,
                execution_id="should-not-count",
            ),
        )
        with self.assertRaises(ScheduleExecutionError):
            adapter.dispatch(request)
        self.assertEqual(executions.list_calls, [])
        self.assertEqual(ambiguous_ledger.mark_succeeded_calls, [])

        success_ledger = MarkSucceededMismatchLedger()
        adapter, client, _executions, _ledger = dispatcher(ledger=success_ledger)
        with self.assertRaises(ScheduleExecutionError):
            adapter.dispatch(request)
        self.assertEqual(
            success_ledger.mark_succeeded_calls,
            [
                (
                    "sch-1",
                    NOW.isoformat(),
                    f"{workload_job_name('wrk-1')}/executions/exec-1",
                )
            ],
        )

    def test_reconciliation_denies_execution_boundary_drift_without_ledger_success(
        self,
    ) -> None:
        request = dispatch_request()
        token = CloudRunJobDispatcher.stable_tick_token(request)
        cases = {
            "image": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    containers=(
                        dataclasses.replace(
                            execution.template.containers[0],
                            image="gcr.io/other/image:latest",
                        ),
                    ),
                ),
            ),
            "command": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    containers=(
                        dataclasses.replace(
                            execution.template.containers[0],
                            command=("bash",),
                        ),
                    ),
                ),
            ),
            "args": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    containers=(
                        dataclasses.replace(
                            execution.template.containers[0],
                            args=("worker.py",),
                        ),
                    ),
                ),
            ),
            "resources": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    containers=(
                        dataclasses.replace(
                            execution.template.containers[0],
                            resources=FakeResources(
                                limits={"cpu": "2", "memory": "512Mi"},
                                cpu_idle=False,
                            ),
                        ),
                    ),
                ),
            ),
            "cpu_idle": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    containers=(
                        dataclasses.replace(
                            execution.template.containers[0],
                            resources=FakeResources(
                                limits={"cpu": "1", "memory": "512Mi"},
                                cpu_idle=True,
                            ),
                        ),
                    ),
                ),
            ),
            "startup_boost": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    containers=(
                        dataclasses.replace(
                            execution.template.containers[0],
                            resources=FakeResources(
                                limits={"cpu": "1", "memory": "512Mi"},
                                cpu_idle=False,
                                startup_cpu_boost=True,
                            ),
                        ),
                    ),
                ),
            ),
            "ports": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    containers=(
                        dataclasses.replace(
                            execution.template.containers[0],
                            ports=("8080",),
                        ),
                    ),
                ),
            ),
            "volume_mounts": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    containers=(
                        dataclasses.replace(
                            execution.template.containers[0],
                            volume_mounts=("/data",),
                        ),
                    ),
                ),
            ),
            "volumes": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    volumes=("scratch",),
                ),
            ),
            "vpc": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    vpc_access=FakeVpcAccess(connector="projects/p/locations/r/connectors/c"),
                ),
            ),
            "task_count": lambda execution: dataclasses.replace(
                execution,
                task_count=2,
            ),
            "parallelism": lambda execution: dataclasses.replace(
                execution,
                parallelism=2,
            ),
            "token_env": lambda execution: dataclasses.replace(
                execution,
                template=dataclasses.replace(
                    execution.template,
                    containers=(
                        dataclasses.replace(
                            execution.template.containers[0],
                            env=execution_env_pairs(
                                schedule_id="sch-1",
                                workload_id="wrk-1",
                                tick_at=NOW,
                                tick_token="0" * 64,
                            ),
                        ),
                    ),
                ),
            ),
        }

        for case_name, drift in cases.items():
            with self.subTest(case=case_name):
                adapter, client, executions, ledger = dispatcher()
                client.run_error = RuntimeError("timeout")
                executions.executions_by_parent[workload_job_name("wrk-1")] = (
                    drift(
                        sample_execution(
                            schedule_id="sch-1",
                            workload_id="wrk-1",
                            tick_at=NOW,
                            tick_token=token,
                            execution_id=f"drift-{case_name}",
                        )
                    ),
                )

                with self.assertRaises(ScheduleExecutionError):
                    adapter.dispatch(request)

                self.assertEqual(ledger.mark_succeeded_calls, [])

    def test_dispatch_rejects_job_boundary_drift_before_run(self) -> None:
        drifted = FakeJob(
            name=workload_job_name("wrk-1"),
            labels=boundary_labels("wrk-1"),
            template=FakeExecutionTemplate(
                task_count=1,
                parallelism=1,
                template=FakeTaskTemplate(
                    service_account=service_account_for("wrk-1"),
                    max_retries=1,
                    timeout=FakeDuration(seconds=300),
                    containers=(
                        FakeContainer(
                            image="gcr.io/other/image:latest",
                            command=("python",),
                            args=("main.py",),
                            resources=FakeResources(
                                limits={"cpu": "1", "memory": "512Mi"},
                                cpu_idle=False,
                            ),
                        ),
                    ),
                ),
            ),
        )
        adapter, client, _executions, _ledger = dispatcher(job=drifted)

        with self.assertRaises(ScheduleExecutionError):
            adapter.dispatch(dispatch_request())

        self.assertEqual(client.run_calls, [])

    def test_dispatch_rejects_non_central_project_boundary(self) -> None:
        with self.assertRaises(ValueError):
            CloudRunJobDispatcher(
                jobs_client=FakeJobsClient(
                    job=sample_job(),
                    execution=sample_execution(
                        tick_token=CloudRunJobDispatcher.stable_tick_token(
                            dispatch_request()
                        )
                    ),
                ),
                executions_client=FakeExecutionsClient(),
                ledger=FakeDispatchLedger(),
                project_id="other-project-12345",
                region=REGION,
            )
