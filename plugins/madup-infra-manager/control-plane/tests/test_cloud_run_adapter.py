from __future__ import annotations

import dataclasses
import hashlib
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from google.api_core.exceptions import NotFound
from google.cloud import run_v2
from google.iam.v1 import iam_policy_pb2, policy_pb2
from google.type import expr_pb2

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
FIXTURE_ROOT = TEST_ROOT / "fixtures" / "repos"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.adapters.cloud_run import CloudRunRuntimePort  # noqa: E402
from mim_control_plane.domain.models import (  # noqa: E402
    RepositoryAdmission,
    RepositoryAdmissionId,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (  # noqa: E402
    RepositoryAdmissionState,
    WorkloadKind,
    WorkloadState,
)
from mim_control_plane.ports.execution import (  # noqa: E402
    ExecutionPlaneError,
    RuntimeServiceRoute,
)
from mim_control_plane.services.render import (  # noqa: E402
    DesiredStateRenderContext,
    DesiredStateSecretAttachment,
    VerifiedDesiredState,
    render_signed_desired_state,
)
from mim_control_plane.services.runtime_naming import provider_secret_id  # noqa: E402

NOW = datetime(2026, 8, 4, 0, 0, 0, tzinfo=UTC)
KEY = b"k" * 32
PROJECT_ID = "madup-prod1"
PROJECT_NUMBER = "123456789012"
REGION = "asia-northeast3"
RUN_APP_SUFFIX = ".run" + ".app"
SECRET_ID = "sec-0123456789abcdefabcd"


@dataclass(frozen=True, slots=True)
class FakeOperation:
    response: object | None = None
    result_calls: int = 0

    def result(self) -> object | None:
        object.__setattr__(self, "result_calls", self.result_calls + 1)
        return self.response


class FakeServicesClient:
    def __init__(
        self,
        existing: dict[str, run_v2.Service] | None = None,
        iam_policies: dict[str, policy_pb2.Policy] | None = None,
        set_policy_response: policy_pb2.Policy | None = None,
    ) -> None:
        self.existing = dict(existing or {})
        self.iam_policies = dict(iam_policies or {})
        self.create_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []
        self.get_calls: list[str] = []
        self.get_iam_calls: list[str] = []
        self.set_iam_calls: list[policy_pb2.Policy] = []
        self.set_policy_response = set_policy_response

    def get_service(self, *, name: str, **_: object) -> run_v2.Service:
        self.get_calls.append(name)
        try:
            return self.existing[name]
        except KeyError as exc:
            raise NotFound("service not found") from exc

    def create_service(
        self,
        *,
        parent: str,
        service: run_v2.Service,
        service_id: str,
        **_: object,
    ) -> FakeOperation:
        self.create_calls.append(
            {"parent": parent, "service": service, "service_id": service_id}
        )
        created = run_v2.Service(service)
        created.name = f"{parent}/services/{service_id}"
        self.existing[created.name] = created
        return FakeOperation(response=created)

    def update_service(self, *, service: run_v2.Service, **_: object) -> FakeOperation:
        self.update_calls.append({"service": service})
        self.existing[service.name] = run_v2.Service(service)
        return FakeOperation(response=service)

    def get_iam_policy(
        self,
        request: iam_policy_pb2.GetIamPolicyRequest | dict[str, object] | None = None,
        **_: object,
    ) -> policy_pb2.Policy:
        resource = (
            request.resource
            if isinstance(request, iam_policy_pb2.GetIamPolicyRequest)
            else str((request or {}).get("resource"))
        )
        self.get_iam_calls.append(resource)
        return self.iam_policies.get(resource, policy_pb2.Policy())

    def set_iam_policy(
        self,
        request: iam_policy_pb2.SetIamPolicyRequest | dict[str, object] | None = None,
        **_: object,
    ) -> policy_pb2.Policy:
        if isinstance(request, iam_policy_pb2.SetIamPolicyRequest):
            resource = request.resource
            policy = policy_pb2.Policy()
            policy.CopyFrom(request.policy)
        else:
            raw_request = request or {}
            resource = str(raw_request.get("resource"))
            policy = policy_pb2.Policy()
            policy.CopyFrom(raw_request.get("policy"))
        self.set_iam_calls.append(policy)
        stored = policy_pb2.Policy()
        stored.CopyFrom(self.set_policy_response or policy)
        self.iam_policies[resource] = stored
        return stored


class FakeJobsClient:
    def __init__(
        self,
        existing: dict[str, run_v2.Job] | None = None,
        iam_policies: dict[str, policy_pb2.Policy] | None = None,
        set_policy_response: policy_pb2.Policy | None = None,
    ) -> None:
        self.existing = dict(existing or {})
        self.iam_policies = dict(iam_policies or {})
        self.create_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []
        self.get_calls: list[str] = []
        self.get_iam_calls: list[str] = []
        self.set_iam_calls: list[policy_pb2.Policy] = []
        self.set_policy_response = set_policy_response

    def get_job(self, *, name: str, **_: object) -> run_v2.Job:
        self.get_calls.append(name)
        try:
            return self.existing[name]
        except KeyError as exc:
            raise NotFound("job not found") from exc

    def create_job(
        self,
        *,
        parent: str,
        job: run_v2.Job,
        job_id: str,
        **_: object,
    ) -> FakeOperation:
        self.create_calls.append({"parent": parent, "job": job, "job_id": job_id})
        created = run_v2.Job(job)
        created.name = f"{parent}/jobs/{job_id}"
        self.existing[created.name] = created
        return FakeOperation(response=created)

    def update_job(self, *, job: run_v2.Job, **_: object) -> FakeOperation:
        self.update_calls.append({"job": job})
        self.existing[job.name] = run_v2.Job(job)
        return FakeOperation(response=job)

    def get_iam_policy(
        self,
        request: iam_policy_pb2.GetIamPolicyRequest | dict[str, object] | None = None,
        **_: object,
    ) -> policy_pb2.Policy:
        resource = (
            request.resource
            if isinstance(request, iam_policy_pb2.GetIamPolicyRequest)
            else str((request or {}).get("resource"))
        )
        self.get_iam_calls.append(resource)
        return self.iam_policies.get(resource, policy_pb2.Policy())

    def set_iam_policy(
        self,
        request: iam_policy_pb2.SetIamPolicyRequest | dict[str, object] | None = None,
        **_: object,
    ) -> policy_pb2.Policy:
        if isinstance(request, iam_policy_pb2.SetIamPolicyRequest):
            resource = request.resource
            policy = policy_pb2.Policy()
            policy.CopyFrom(request.policy)
        else:
            raw_request = request or {}
            resource = str(raw_request.get("resource"))
            policy = policy_pb2.Policy()
            policy.CopyFrom(raw_request.get("policy"))
        self.set_iam_calls.append(policy)
        stored = policy_pb2.Policy()
        stored.CopyFrom(self.set_policy_response or policy)
        self.iam_policies[resource] = stored
        return stored


class FakeRevisionsClient:
    def __init__(self, revisions: dict[str, run_v2.Revision] | None = None) -> None:
        self.revisions = dict(revisions or {})
        self.get_calls: list[str] = []

    def get_revision(self, *, name: str, **_: object) -> run_v2.Revision:
        self.get_calls.append(name)
        try:
            return self.revisions[name]
        except KeyError as exc:
            raise NotFound("revision not found") from exc


class ExplodingServicesClient:
    def get_service(self, *_: object, **__: object) -> run_v2.Service:
        raise RuntimeError("boom")


class FakeIapAccessPolicyManager:
    def __init__(
        self,
        *,
        allowed: bool = True,
        expected_owner_id: str | None = None,
    ) -> None:
        self.allowed = allowed
        self.expected_owner_id = expected_owner_id
        self.ensure_calls: list[tuple[str, str]] = []
        self.verify_calls: list[tuple[str, str]] = []

    def ensure_exact_access(
        self,
        service_name: str,
        workload_owner_id: str,
    ) -> None:
        self.ensure_calls.append((service_name, workload_owner_id))
        if not self.allowed or (
            self.expected_owner_id is not None
            and workload_owner_id != self.expected_owner_id
        ):
            raise RuntimeError("synthetic IAP access denial")

    def verify_exact_access(
        self,
        service_name: str,
        workload_owner_id: str,
    ) -> bool:
        self.verify_calls.append((service_name, workload_owner_id))
        return self.allowed and (
            self.expected_owner_id is None
            or workload_owner_id == self.expected_owner_id
        )


def admission() -> RepositoryAdmission:
    return RepositoryAdmission(
        id=RepositoryAdmissionId("repo-1"),
        repository_numeric_id=42,
        owner="madupmarketing",
        name="sample-app",
        installation_id=99,
        state=RepositoryAdmissionState.ADMITTED,
        admitted_sha="b" * 40,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(minutes=5),
        version=1,
    )


def workload(kind: WorkloadKind) -> Workload:
    return Workload(
        id=WorkloadId("wrk-1"),
        owner_id=UserId("usr-1"),
        repository_admission_id=RepositoryAdmissionId("repo-1"),
        name="sample-app",
        kind=kind,
        state=WorkloadState.ACTIVE,
        source_sha="b" * 40,
        desired_manifest_hash="manifest-hash-1",
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(minutes=2),
        last_activity_at=NOW - timedelta(minutes=2),
        last_healthy_image_digest="sha256:" + "a" * 64,
        version=1,
    )


def snapshot(kind: WorkloadKind) -> dict[str, bytes]:
    fixture_name = "nextjs" if kind is WorkloadKind.NEXTJS else "scheduled_script"
    fixture_dir = FIXTURE_ROOT / fixture_name
    loaded: dict[str, bytes] = {}
    for path in sorted(fixture_dir.rglob("*")):
        if path.is_file():
            loaded[path.relative_to(fixture_dir).as_posix()] = path.read_bytes()
    return loaded


def verified_state(kind: WorkloadKind) -> VerifiedDesiredState:
    envelope = render_signed_desired_state(
        workload=workload(kind),
        admission=admission(),
        snapshot=snapshot(kind),
        image_digest="a" * 64,
        context=DesiredStateRenderContext(project_id=PROJECT_ID, key_id="deploy-key-1"),
        issued_at=NOW,
        signing_key=KEY,
    )
    return VerifiedDesiredState(
        envelope=envelope,
        canonical_unsigned=b"canonical",
        snapshot_digest=envelope.payload.snapshot_digest,
    )


def service_desired_state() -> VerifiedDesiredState:
    return replace_payload(
        verified_state(WorkloadKind.NEXTJS),
        request_cpu_always_allocated=False,
    )


def replace_payload(
    desired_state: VerifiedDesiredState,
    **changes: Any,
) -> VerifiedDesiredState:
    payload = dataclasses.replace(desired_state.envelope.payload, **changes)
    envelope = dataclasses.replace(desired_state.envelope, payload=payload)
    return dataclasses.replace(desired_state, envelope=envelope)


def ready_condition() -> run_v2.Condition:
    return run_v2.Condition(state=run_v2.Condition.State.CONDITION_SUCCEEDED)


def workload_suffix(workload_id: str) -> str:
    return hashlib.sha256(workload_id.encode("utf-8")).hexdigest()[:12]


def service_name(workload_id: str) -> str:
    return (
        f"projects/{PROJECT_ID}/locations/{REGION}/services/"
        f"mim-svc-{workload_suffix(workload_id)}"
    )


def job_name(workload_id: str) -> str:
    return (
        f"projects/{PROJECT_ID}/locations/{REGION}/jobs/"
        f"mim-job-{workload_suffix(workload_id)}"
    )


def gateway_member() -> str:
    return f"serviceAccount:mim-app-gateway@{PROJECT_ID}.iam.gserviceaccount.com"


def reviewed_breakglass_members() -> tuple[str, ...]:
    return (
        "group:mim-admins@madup.com",
        "user:operator@madup.com",
    )


def iap_service_agent() -> str:
    return gateway_member().split(":", 1)[1]


def expected_labels(
    desired_state: VerifiedDesiredState,
) -> dict[str, str]:
    return dict(desired_state.envelope.payload.labels)


def service_invoker_policy(*, etag: bytes = b"etag-1") -> policy_pb2.Policy:
    policy = policy_pb2.Policy(etag=etag)
    binding = policy.bindings.add(role="roles/run.invoker")
    for member in (gateway_member(), *reviewed_breakglass_members()):
        binding.members.append(member)
    return policy


def iap_invoker_policy(*, etag: bytes = b"etag-1") -> policy_pb2.Policy:
    return service_invoker_policy(etag=etag)


def scheduler_gateway_member() -> str:
    return f"serviceAccount:mim-schedule-gateway@{PROJECT_ID}.iam.gserviceaccount.com"


def job_invoker_policy(*, etag: bytes = b"etag-1") -> policy_pb2.Policy:
    policy = policy_pb2.Policy(etag=etag)
    binding = policy.bindings.add(role="roles/run.invoker")
    binding.members.append(scheduler_gateway_member())
    return policy


def policy_with_extra_binding(
    policy: policy_pb2.Policy,
    *,
    role: str,
    member: str = "user:stale-admin@madup.com",
) -> policy_pb2.Policy:
    drifted = policy_pb2.Policy()
    drifted.CopyFrom(policy)
    drifted.bindings.add(role=role, members=(member,))
    return drifted


def make_runtime(
    *,
    services_client: FakeServicesClient | ExplodingServicesClient | None = None,
    jobs_client: FakeJobsClient | None = None,
    revisions_client: FakeRevisionsClient | None = None,
    iap_manager: FakeIapAccessPolicyManager | None = None,
    reviewed_members: tuple[str, ...] | None = None,
) -> CloudRunRuntimePort:
    return CloudRunRuntimePort(
        project_id=PROJECT_ID,
        project_number=PROJECT_NUMBER,
        region=REGION,
        services_client=services_client or FakeServicesClient(),
        jobs_client=jobs_client or FakeJobsClient(),
        revisions_client=revisions_client or FakeRevisionsClient(),
        iap_access_policy_manager=iap_manager or FakeIapAccessPolicyManager(),
        reviewed_breakglass_members=(
            reviewed_breakglass_members()
            if reviewed_members is None
            else reviewed_members
        ),
    )


def nextjs_container(
    desired_state: VerifiedDesiredState,
    *,
    command: list[str] | None = None,
    args: list[str] | None = None,
    cpu: str = "1",
    memory: str = "512Mi",
    cpu_idle: bool = True,
    startup_cpu_boost: bool = False,
    env: list[run_v2.EnvVar] | None = None,
    ports: list[run_v2.ContainerPort] | None = None,
    volume_mounts: list[run_v2.VolumeMount] | None = None,
) -> run_v2.Container:
    return run_v2.Container(
        image=desired_state.envelope.payload.image_uri,
        command=command or ["./node_modules/.bin/next"],
        args=args or ["start", "--hostname", "0.0.0.0", "--port", "8080"],
        env=env or [],
        ports=ports or [],
        volume_mounts=volume_mounts or [],
        resources=run_v2.ResourceRequirements(
            limits={"cpu": cpu, "memory": memory},
            cpu_idle=cpu_idle,
            startup_cpu_boost=startup_cpu_boost,
        ),
    )


def script_container(
    desired_state: VerifiedDesiredState,
    *,
    command: list[str] | None = None,
    args: list[str] | None = None,
    cpu: str = "1",
    memory: str = "512Mi",
    cpu_idle: bool = False,
    startup_cpu_boost: bool = False,
    env: list[run_v2.EnvVar] | None = None,
    ports: list[run_v2.ContainerPort] | None = None,
    volume_mounts: list[run_v2.VolumeMount] | None = None,
) -> run_v2.Container:
    return run_v2.Container(
        image=desired_state.envelope.payload.image_uri,
        command=command or ["python"],
        args=args or ["main.py"],
        env=env or [],
        ports=ports or [],
        volume_mounts=volume_mounts or [],
        resources=run_v2.ResourceRequirements(
            limits={"cpu": cpu, "memory": memory},
            cpu_idle=cpu_idle,
            startup_cpu_boost=startup_cpu_boost,
        ),
    )


def service_fixture(
    desired_state: VerifiedDesiredState,
    *,
    labels: dict[str, str] | None = None,
    ingress: run_v2.IngressTraffic = (
        run_v2.IngressTraffic.INGRESS_TRAFFIC_ALL
    ),
    service_account: str | None = None,
    min_instance_count: int = 0,
    max_instance_count: int = 1,
    concurrency: int = 20,
    timeout_seconds: int = 300,
    containers: list[run_v2.Container] | None = None,
    vpc_connector: str = "",
    volumes: list[run_v2.Volume] | None = None,
    traffic: list[run_v2.TrafficTarget] | None = None,
    iap_enabled: bool = False,
    invoker_iam_disabled: bool = False,
    uri: str = "https://mim-svc-5251ebcdff9f-uc.a" + RUN_APP_SUFFIX,
) -> run_v2.Service:
    return run_v2.Service(
        name=service_name("wrk-1"),
        uri=uri,
        generation=7,
        observed_generation=7,
        latest_ready_revision=f"{service_name('wrk-1')}/revisions/rev-0001",
        labels=labels or expected_labels(desired_state),
        ingress=ingress,
        iap_enabled=iap_enabled,
        invoker_iam_disabled=invoker_iam_disabled,
        template=run_v2.RevisionTemplate(
            service_account=(
                service_account
                or desired_state.envelope.payload.runtime_service_account
            ),
            scaling=run_v2.RevisionScaling(
                min_instance_count=min_instance_count,
                max_instance_count=max_instance_count,
            ),
            max_instance_request_concurrency=concurrency,
            timeout={"seconds": timeout_seconds},
            containers=containers or [nextjs_container(desired_state)],
            volumes=volumes or [],
            vpc_access=run_v2.VpcAccess(connector=vpc_connector),
        ),
        traffic=traffic
        or [
            run_v2.TrafficTarget(
                percent=100,
                type_=(
                    run_v2.TrafficTargetAllocationType
                    .TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST
                ),
            )
        ],
        terminal_condition=ready_condition(),
    )


def revision_fixture(
    desired_state: VerifiedDesiredState,
    *,
    containers: list[run_v2.Container] | None = None,
    volumes: list[run_v2.Volume] | None = None,
    vpc_connector: str = "",
) -> run_v2.Revision:
    return run_v2.Revision(
        name=f"{service_name('wrk-1')}/revisions/rev-0001",
        generation=9,
        observed_generation=9,
        service=service_name("wrk-1"),
        service_account=desired_state.envelope.payload.runtime_service_account,
        containers=containers or [nextjs_container(desired_state)],
        volumes=volumes or [],
        vpc_access=run_v2.VpcAccess(connector=vpc_connector),
        conditions=[ready_condition()],
    )


def job_fixture(
    desired_state: VerifiedDesiredState,
    *,
    labels: dict[str, str] | None = None,
    task_count: int = 1,
    parallelism: int = 1,
    service_account: str | None = None,
    max_retries: int = 1,
    timeout_seconds: int = 300,
    containers: list[run_v2.Container] | None = None,
    vpc_connector: str = "",
    volumes: list[run_v2.Volume] | None = None,
) -> run_v2.Job:
    return run_v2.Job(
        name=job_name("wrk-1"),
        generation=5,
        observed_generation=5,
        labels=labels or expected_labels(desired_state),
        terminal_condition=ready_condition(),
        template=run_v2.ExecutionTemplate(
            task_count=task_count,
            parallelism=parallelism,
            template=run_v2.TaskTemplate(
                service_account=(
                    service_account
                    or desired_state.envelope.payload.runtime_service_account
                ),
                max_retries=max_retries,
                timeout={"seconds": timeout_seconds},
                containers=containers or [script_container(desired_state)],
                volumes=volumes or [],
                vpc_access=run_v2.VpcAccess(connector=vpc_connector),
            ),
        ),
    )


class CloudRunRuntimePortTests(unittest.TestCase):
    def test_constructor_rejects_invalid_project_and_cross_region_before_clients(
        self,
    ) -> None:
        with (
            mock.patch.object(
                run_v2,
                "ServicesClient",
                side_effect=AssertionError("services client should not be built"),
            ),
            mock.patch.object(
                run_v2,
                "JobsClient",
                side_effect=AssertionError("jobs client should not be built"),
            ),
            mock.patch.object(
                run_v2,
                "RevisionsClient",
                side_effect=AssertionError("revisions client should not be built"),
            ),
        ):
            with self.assertRaises(ValueError):
                CloudRunRuntimePort(
                    project_id="Bad_Project",
                    project_number=PROJECT_NUMBER,
                    region=REGION,
                    services_client=FakeServicesClient(),
                    jobs_client=FakeJobsClient(),
                    revisions_client=FakeRevisionsClient(),
                    iap_access_policy_manager=FakeIapAccessPolicyManager(),
                )
            with self.assertRaises(ValueError):
                CloudRunRuntimePort(
                    project_id=PROJECT_ID,
                    project_number=PROJECT_NUMBER,
                    region="us-central1",
                    services_client=FakeServicesClient(),
                    jobs_client=FakeJobsClient(),
                    revisions_client=FakeRevisionsClient(),
                    iap_access_policy_manager=FakeIapAccessPolicyManager(),
                )

    def test_constructor_requires_all_clients_to_be_explicitly_injected(self) -> None:
        with (
            mock.patch.object(
                run_v2,
                "ServicesClient",
                side_effect=AssertionError(
                    "ambient services client lookup is forbidden"
                ),
            ),
            mock.patch.object(
                run_v2,
                "JobsClient",
                side_effect=AssertionError("ambient jobs client lookup is forbidden"),
            ),
            mock.patch.object(
                run_v2,
                "RevisionsClient",
                side_effect=AssertionError(
                    "ambient revisions client lookup is forbidden"
                ),
            ),
        ):
            with self.assertRaises(ValueError):
                CloudRunRuntimePort(
                    project_id=PROJECT_ID,
                    project_number=PROJECT_NUMBER,
                    region=REGION,
                    iap_access_policy_manager=FakeIapAccessPolicyManager(),
                )
            with self.assertRaises(ValueError):
                CloudRunRuntimePort(
                    project_id=PROJECT_ID,
                    project_number=PROJECT_NUMBER,
                    region=REGION,
                    services_client=FakeServicesClient(),
                    jobs_client=None,
                    revisions_client=FakeRevisionsClient(),
                    iap_access_policy_manager=FakeIapAccessPolicyManager(),
                )

    def test_constructor_requires_numeric_project_number_and_allows_missing_iap_manager(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            CloudRunRuntimePort(
                project_id=PROJECT_ID,
                project_number="not-numeric",
                region=REGION,
                services_client=FakeServicesClient(),
                jobs_client=FakeJobsClient(),
                revisions_client=FakeRevisionsClient(),
                iap_access_policy_manager=FakeIapAccessPolicyManager(),
            )
        runtime = CloudRunRuntimePort(
            project_id=PROJECT_ID,
            project_number=PROJECT_NUMBER,
            region=REGION,
            services_client=FakeServicesClient(),
            jobs_client=FakeJobsClient(),
            revisions_client=FakeRevisionsClient(),
            iap_access_policy_manager=None,
            reviewed_breakglass_members=reviewed_breakglass_members(),
        )

        self.assertIsInstance(runtime, CloudRunRuntimePort)

    def test_apply_service_creates_cloud_run_service_with_exact_policy(self) -> None:
        desired_state = service_desired_state()
        existing_policy = policy_pb2.Policy(version=3, etag=b"abc")
        existing_policy.bindings.add(
            role="roles/run.admin",
            members=("user:stale-admin@madup.com",),
            condition=expr_pb2.Expr(
                expression="request.time < timestamp('2030-01-01T00:00:00Z')"
            ),
        )
        existing_policy.bindings.add(
            role=f"projects/{PROJECT_ID}/roles/workloadRuntimeAdmin",
            members=("user:other-admin@madup.com",),
        )
        existing_policy.bindings.add(
            role="roles/run.invoker",
            members=("allUsers",),
        )
        existing_policy.audit_configs.add(service="allServices")
        services = FakeServicesClient(
            iam_policies={service_name("wrk-1"): existing_policy}
        )
        jobs = FakeJobsClient()
        revisions = FakeRevisionsClient()
        manager = FakeIapAccessPolicyManager()
        runtime = make_runtime(
            services_client=services,
            jobs_client=jobs,
            revisions_client=revisions,
            iap_manager=manager,
        )

        runtime.apply(desired_state)

        self.assertEqual(len(services.create_calls), 1)
        self.assertEqual(jobs.create_calls, [])
        call = services.create_calls[0]
        self.assertEqual(call["parent"], f"projects/{PROJECT_ID}/locations/{REGION}")
        self.assertTrue(str(call["service_id"]).startswith("mim-svc-"))
        service = call["service"]
        assert isinstance(service, run_v2.Service)
        self.assertEqual(
            service.ingress,
            run_v2.IngressTraffic.INGRESS_TRAFFIC_ALL,
        )
        self.assertFalse(service.iap_enabled)
        self.assertFalse(service.invoker_iam_disabled)
        self.assertEqual(dict(service.labels), expected_labels(desired_state))
        self.assertEqual(
            service.template.service_account,
            desired_state.envelope.payload.runtime_service_account,
        )
        self.assertEqual(service.template.scaling.min_instance_count, 0)
        self.assertEqual(service.template.scaling.max_instance_count, 1)
        self.assertEqual(len(service.template.containers), 1)
        self.assertEqual(
            service.template.max_instance_request_concurrency,
            desired_state.envelope.payload.service_concurrency,
        )
        self.assertEqual(service.template.timeout.seconds, 300)
        self.assertFalse(service.template.volumes)
        self.assertEqual(service.template.vpc_access.connector, "")
        container = service.template.containers[0]
        self.assertEqual(container.image, desired_state.envelope.payload.image_uri)
        self.assertEqual(tuple(container.command), ("./node_modules/.bin/next",))
        self.assertEqual(
            tuple(container.args),
            ("start", "--hostname", "0.0.0.0", "--port", "8080"),
        )
        self.assertEqual(container.resources.limits["cpu"], "1")
        self.assertEqual(container.resources.limits["memory"], "512Mi")
        self.assertTrue(container.resources.cpu_idle)
        self.assertFalse(container.resources.startup_cpu_boost)
        self.assertEqual(len(service.traffic), 1)
        self.assertEqual(service.traffic[0].percent, 100)
        self.assertEqual(
            service.traffic[0].type_,
            run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST,
        )
        self.assertEqual(
            services.get_iam_calls,
            [service_name("wrk-1"), service_name("wrk-1")],
        )
        self.assertEqual(len(services.set_iam_calls), 1)
        applied_policy = services.set_iam_calls[0]
        self.assertEqual(applied_policy.version, 3)
        self.assertEqual(applied_policy.etag, b"abc")
        self.assertEqual(len(applied_policy.bindings), 1)
        binding = applied_policy.bindings[0]
        self.assertEqual(binding.role, "roles/run.invoker")
        self.assertEqual(
            list(binding.members),
            [gateway_member(), *reviewed_breakglass_members()],
        )
        self.assertFalse(binding.HasField("condition"))
        self.assertEqual(list(applied_policy.audit_configs), [])
        self.assertEqual(manager.ensure_calls, [])
        self.assertEqual(manager.verify_calls, [])

    def test_apply_service_rejects_drifted_run_invoker_write_response(self) -> None:
        desired_state = service_desired_state()
        drifted = policy_pb2.Policy(etag=b"next")
        drifted.bindings.add(
            role="roles/run.invoker",
            members=(gateway_member(), *reviewed_breakglass_members(), "allUsers"),
        )
        services = FakeServicesClient(
            iam_policies={service_name("wrk-1"): policy_pb2.Policy(etag=b"abc")},
            set_policy_response=drifted,
        )
        runtime = make_runtime(
            services_client=services,
            jobs_client=FakeJobsClient(),
            revisions_client=FakeRevisionsClient(),
        )

        with self.assertRaises(ExecutionPlaneError):
            runtime.apply(desired_state)

        self.assertEqual(len(services.set_iam_calls), 1)

    def test_apply_service_ignores_iap_manager_for_gateway_iam_services(self) -> None:
        desired_state = service_desired_state()
        services = FakeServicesClient(
            iam_policies={service_name("wrk-1"): policy_pb2.Policy(etag=b"abc")}
        )
        runtime = make_runtime(
            services_client=services,
            jobs_client=FakeJobsClient(),
            revisions_client=FakeRevisionsClient(),
            iap_manager=FakeIapAccessPolicyManager(allowed=False),
        )

        runtime.apply(desired_state)

        self.assertEqual(len(services.set_iam_calls), 1)

    def test_apply_service_with_empty_breakglass_keeps_only_gateway_invoker(
        self,
    ) -> None:
        desired_state = service_desired_state()
        services = FakeServicesClient(
            iam_policies={service_name("wrk-1"): policy_pb2.Policy(etag=b"abc")}
        )
        runtime = make_runtime(
            services_client=services,
            jobs_client=FakeJobsClient(),
            revisions_client=FakeRevisionsClient(),
            reviewed_members=(),
        )

        runtime.apply(desired_state)

        applied_policy = services.set_iam_calls[0]
        binding = applied_policy.bindings[0]
        self.assertEqual(list(binding.members), [gateway_member()])

    def test_apply_job_creates_cloud_run_job_with_exact_policy(self) -> None:
        desired_state = verified_state(WorkloadKind.SCHEDULED_SCRIPT)
        services = FakeServicesClient()
        jobs = FakeJobsClient()
        manager = FakeIapAccessPolicyManager()
        runtime = make_runtime(
            services_client=services,
            jobs_client=jobs,
            revisions_client=FakeRevisionsClient(),
            iap_manager=manager,
        )

        runtime.apply(desired_state)

        self.assertEqual(services.create_calls, [])
        self.assertEqual(len(jobs.create_calls), 1)
        call = jobs.create_calls[0]
        self.assertEqual(call["parent"], f"projects/{PROJECT_ID}/locations/{REGION}")
        self.assertTrue(str(call["job_id"]).startswith("mim-job-"))
        job = call["job"]
        assert isinstance(job, run_v2.Job)
        self.assertEqual(dict(job.labels), expected_labels(desired_state))
        self.assertEqual(job.template.task_count, 1)
        self.assertEqual(job.template.parallelism, 1)
        task_template = job.template.template
        self.assertEqual(
            task_template.service_account,
            desired_state.envelope.payload.runtime_service_account,
        )
        self.assertEqual(task_template.timeout.seconds, 300)
        self.assertEqual(task_template.max_retries, 1)
        self.assertEqual(len(task_template.containers), 1)
        self.assertFalse(task_template.volumes)
        self.assertEqual(task_template.vpc_access.connector, "")
        container = task_template.containers[0]
        self.assertEqual(container.image, desired_state.envelope.payload.image_uri)
        self.assertEqual(tuple(container.command), ("python",))
        self.assertEqual(tuple(container.args), ("main.py",))
        self.assertEqual(container.resources.limits["cpu"], "1")
        self.assertEqual(container.resources.limits["memory"], "512Mi")
        self.assertEqual(
            jobs.get_iam_calls,
            [job_name("wrk-1"), job_name("wrk-1")],
        )
        self.assertEqual(len(jobs.set_iam_calls), 1)
        applied_policy = jobs.set_iam_calls[0]
        self.assertEqual(len(applied_policy.bindings), 1)
        binding = applied_policy.bindings[0]
        self.assertEqual(binding.role, "roles/run.invoker")
        self.assertEqual(list(binding.members), [scheduler_gateway_member()])
        self.assertFalse(binding.HasField("condition"))
        self.assertEqual(list(applied_policy.audit_configs), [])
        self.assertEqual(manager.ensure_calls, [])
        self.assertEqual(manager.verify_calls, [])

    def test_apply_job_rejects_drifted_invoker_write_response(self) -> None:
        desired_state = verified_state(WorkloadKind.SCHEDULED_SCRIPT)
        drifted = policy_pb2.Policy(etag=b"next")
        drifted.bindings.add(
            role="roles/run.invoker",
            members=(scheduler_gateway_member(), "allUsers"),
        )
        jobs = FakeJobsClient(
            iam_policies={job_name("wrk-1"): policy_pb2.Policy(etag=b"abc")},
            set_policy_response=drifted,
        )
        runtime = make_runtime(jobs_client=jobs)

        with self.assertRaises(ExecutionPlaneError):
            runtime.apply(desired_state)

        self.assertEqual(len(jobs.set_iam_calls), 1)

    def test_apply_rejects_project_region_digest_and_identity_drift(self) -> None:
        runtime = make_runtime()
        baseline = service_desired_state()
        drift_cases = (
            replace_payload(baseline, project_id="wrong-project"),
            replace_payload(baseline, region="us-central1"),
            replace_payload(
                baseline,
                image_uri=f"{REGION}-docker.pkg.dev/{PROJECT_ID}/mim/workloads:latest",
            ),
            replace_payload(
                baseline,
                runtime_service_account="developer@madup.com",
            ),
        )

        for desired_state in drift_cases:
            with self.subTest(payload=desired_state.envelope.payload):
                with self.assertRaises(ExecutionPlaneError):
                    runtime.apply(desired_state)

    def test_apply_service_renders_secret_backed_environment_variables(self) -> None:
        baseline = service_desired_state()
        desired_state = replace_payload(
            baseline,
            secret_attachments=(
                DesiredStateSecretAttachment(
                    secret_id=SECRET_ID,
                    secret_name=provider_secret_id(SECRET_ID),
                    secret_version="1",
                    env_name="MIM_SECRET_SLACK_BOT",
                ),
            ),
        )
        runtime = make_runtime()

        runtime.apply(desired_state)

        created = runtime._services.create_calls[0]["service"]  # type: ignore[attr-defined]
        container = created.template.containers[0]
        self.assertEqual(len(container.env), 1)
        self.assertEqual(container.env[0].name, "MIM_SECRET_SLACK_BOT")

    def test_verify_health_checks_service_generation_and_ready_revision(self) -> None:
        desired_state = service_desired_state()
        resource_name = service_name("wrk-1")
        revision_name = f"{resource_name}/revisions/rev-0001"
        services = FakeServicesClient(
            {resource_name: service_fixture(desired_state)},
            iam_policies={resource_name: service_invoker_policy()},
        )
        revisions = FakeRevisionsClient(
            {
                revision_name: revision_fixture(desired_state)
            }
        )
        manager = FakeIapAccessPolicyManager()
        runtime = make_runtime(
            services_client=services,
            jobs_client=FakeJobsClient(),
            revisions_client=revisions,
            iap_manager=manager,
        )

        self.assertTrue(runtime.verify_health(desired_state))
        self.assertEqual(
            runtime.readback_service_route(desired_state),
            RuntimeServiceRoute(
                resource_name=resource_name,
                uri="https://mim-svc-5251ebcdff9f-uc.a" + RUN_APP_SUFFIX,
            ),
        )
        unhealthy = replace_payload(
            desired_state,
            image_uri=(
                f"{REGION}-docker.pkg.dev/{PROJECT_ID}/mim/workloads@sha256:"
                f"{'b' * 64}"
            ),
        )
        self.assertFalse(runtime.verify_health(unhealthy))
        self.assertEqual(manager.ensure_calls, [])
        self.assertEqual(manager.verify_calls, [])

    def test_verify_health_rejects_admin_invokers_when_breakglass_is_empty(
        self,
    ) -> None:
        desired_state = service_desired_state()
        resource_name = service_name("wrk-1")
        revision_name = f"{resource_name}/revisions/rev-0001"
        services = FakeServicesClient(
            {resource_name: service_fixture(desired_state)},
            iam_policies={resource_name: service_invoker_policy()},
        )
        revisions = FakeRevisionsClient(
            {
                revision_name: revision_fixture(desired_state)
            }
        )
        runtime = make_runtime(
            services_client=services,
            jobs_client=FakeJobsClient(),
            revisions_client=revisions,
            reviewed_members=(),
        )

        self.assertFalse(runtime.verify_health(desired_state))

    def test_verify_health_returns_false_for_runtime_exceptions(self) -> None:
        runtime = make_runtime(
            services_client=ExplodingServicesClient(),
            jobs_client=FakeJobsClient(),
            revisions_client=FakeRevisionsClient(),
        )

        self.assertFalse(runtime.verify_health(service_desired_state()))

    def test_verify_health_rejects_service_material_drift(self) -> None:
        desired_state = service_desired_state()
        resource_name = service_name("wrk-1")
        revision_name = f"{resource_name}/revisions/rev-0001"
        drift_cases = (
            (
                service_fixture(desired_state, labels={"managed-by": "other"}),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(
                    desired_state,
                    ingress=run_v2.IngressTraffic.INGRESS_TRAFFIC_INTERNAL_ONLY,
                ),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(
                    desired_state,
                    containers=[
                        nextjs_container(desired_state),
                        run_v2.Container(image=desired_state.envelope.payload.image_uri),
                    ],
                ),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(
                    desired_state,
                    containers=[
                        nextjs_container(
                            desired_state,
                            command=["node"],
                            args=["server.js"],
                            cpu="2",
                            cpu_idle=True,
                            startup_cpu_boost=True,
                        )
                    ],
                ),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(
                    desired_state,
                    vpc_connector="projects/p/locations/r/connectors/c1",
                ),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(
                    desired_state,
                    traffic=[
                        run_v2.TrafficTarget(
                            percent=100,
                            type_=(
                                run_v2.TrafficTargetAllocationType
                                .TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION
                            ),
                        )
                    ],
                ),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(
                    desired_state,
                    traffic=[
                        run_v2.TrafficTarget(
                            percent=50,
                            type_=(
                                run_v2.TrafficTargetAllocationType
                                .TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST
                            ),
                        )
                    ],
                ),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(
                    desired_state,
                    containers=[
                        nextjs_container(
                            desired_state,
                            env=[run_v2.EnvVar(name="SECRET", value="x")],
                        )
                    ],
                ),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(
                    desired_state,
                    containers=[
                        nextjs_container(
                            desired_state,
                            ports=[run_v2.ContainerPort(container_port=9090)],
                        )
                    ],
                ),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(
                    desired_state,
                    containers=[
                        nextjs_container(
                            desired_state,
                            volume_mounts=[
                                run_v2.VolumeMount(name="cache", mount_path="/cache")
                            ],
                        )
                    ],
                    volumes=[run_v2.Volume(name="cache", empty_dir={})],
                ),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(desired_state),
                revision_fixture(
                    desired_state,
                    containers=[
                        nextjs_container(
                            desired_state,
                            env=[run_v2.EnvVar(name="SECRET", value="x")],
                        )
                    ],
                ),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(desired_state),
                revision_fixture(
                    desired_state,
                    containers=[
                        nextjs_container(
                            desired_state,
                            cpu="2",
                            cpu_idle=True,
                            startup_cpu_boost=True,
                        )
                    ],
                ),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(desired_state),
                revision_fixture(
                    desired_state,
                    volumes=[run_v2.Volume(name="cache", empty_dir={})],
                ),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(desired_state),
                revision_fixture(
                    desired_state,
                    containers=[
                        nextjs_container(
                            desired_state,
                            ports=[run_v2.ContainerPort(container_port=9090)],
                        )
                    ],
                ),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(desired_state),
                revision_fixture(
                    desired_state,
                    containers=[
                        nextjs_container(
                            desired_state,
                            volume_mounts=[
                                run_v2.VolumeMount(name="cache", mount_path="/cache")
                            ],
                        )
                    ],
                ),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(desired_state),
                revision_fixture(
                    desired_state,
                    vpc_connector="projects/p/locations/r/connectors/c1",
                ),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(desired_state, invoker_iam_disabled=True),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(desired_state, iap_enabled=True),
                revision_fixture(desired_state),
                iap_invoker_policy(),
                True,
            ),
            (
                service_fixture(desired_state),
                revision_fixture(desired_state),
                policy_pb2.Policy(),
                True,
            ),
            (
                service_fixture(desired_state),
                revision_fixture(desired_state),
                policy_pb2.Policy(
                    bindings=(
                        policy_pb2.Binding(
                            role="roles/run.invoker",
                            members=(f"serviceAccount:{iap_service_agent()}",),
                            condition=expr_pb2.Expr(expression="true"),
                        ),
                    )
                ),
                True,
            ),
            (
                service_fixture(desired_state),
                revision_fixture(desired_state),
                policy_with_extra_binding(
                    iap_invoker_policy(),
                    role="roles/run.admin",
                ),
                True,
            ),
            (
                service_fixture(desired_state),
                revision_fixture(desired_state),
                policy_with_extra_binding(
                    iap_invoker_policy(),
                    role=f"projects/{PROJECT_ID}/roles/workloadRuntimeAdmin",
                ),
                True,
            ),
        )

        for service, revision, policy, access_allowed in drift_cases:
            with self.subTest(service=service, revision=revision, policy=policy):
                runtime = make_runtime(
                    services_client=FakeServicesClient(
                        {resource_name: service},
                        iam_policies={resource_name: policy},
                    ),
                    jobs_client=FakeJobsClient(),
                    revisions_client=FakeRevisionsClient(
                        {revision_name: revision}
                    ),
                    iap_manager=FakeIapAccessPolicyManager(allowed=access_allowed),
                )
                self.assertFalse(runtime.verify_health(desired_state))

    def test_verify_health_accepts_job_with_exact_invoker_policy(self) -> None:
        desired_state = verified_state(WorkloadKind.SCHEDULED_SCRIPT)
        resource_name = job_name("wrk-1")
        jobs = FakeJobsClient(
            {resource_name: job_fixture(desired_state)},
            iam_policies={resource_name: job_invoker_policy()},
        )
        runtime = make_runtime(jobs_client=jobs)

        self.assertTrue(runtime.verify_health(desired_state))
        self.assertEqual(jobs.get_iam_calls, [resource_name])

    def test_verify_health_rejects_job_invoker_policy_drift(self) -> None:
        desired_state = verified_state(WorkloadKind.SCHEDULED_SCRIPT)
        resource_name = job_name("wrk-1")
        conditional = job_invoker_policy()
        conditional.bindings[0].condition.CopyFrom(
            expr_pb2.Expr(expression="true")
        )
        invalid_policies = (
            policy_pb2.Policy(),
            policy_pb2.Policy(
                bindings=(
                    policy_pb2.Binding(
                        role="roles/run.invoker",
                        members=("allUsers",),
                    ),
                )
            ),
            conditional,
            policy_with_extra_binding(
                job_invoker_policy(),
                role="roles/run.admin",
            ),
            policy_with_extra_binding(
                job_invoker_policy(),
                role=f"projects/{PROJECT_ID}/roles/jobRunnerAdmin",
            ),
        )

        for policy in invalid_policies:
            with self.subTest(policy=policy):
                jobs = FakeJobsClient(
                    {resource_name: job_fixture(desired_state)},
                    iam_policies={resource_name: policy},
                )
                runtime = make_runtime(jobs_client=jobs)

                self.assertFalse(runtime.verify_health(desired_state))

    def test_verify_health_rejects_job_material_drift(self) -> None:
        desired_state = verified_state(WorkloadKind.SCHEDULED_SCRIPT)
        resource_name = job_name("wrk-1")
        drift_jobs = (
            job_fixture(desired_state, labels={"managed-by": "other"}),
            job_fixture(desired_state, task_count=2),
            job_fixture(desired_state, service_account="wrong@example.com"),
            job_fixture(
                desired_state,
                containers=[
                    script_container(
                        desired_state,
                        cpu="2",
                        cpu_idle=True,
                        startup_cpu_boost=True,
                    )
                ],
            ),
            job_fixture(
                desired_state,
                containers=[
                    script_container(desired_state),
                    run_v2.Container(image=desired_state.envelope.payload.image_uri),
                ],
            ),
            job_fixture(
                desired_state,
                containers=[
                    script_container(
                        desired_state,
                        env=[run_v2.EnvVar(name="SECRET", value="x")],
                    )
                ],
            ),
            job_fixture(
                desired_state,
                containers=[
                    script_container(
                        desired_state,
                        ports=[run_v2.ContainerPort(container_port=9090)],
                    )
                ],
            ),
            job_fixture(
                desired_state,
                containers=[
                    script_container(
                        desired_state,
                        volume_mounts=[
                            run_v2.VolumeMount(name="cache", mount_path="/cache")
                        ],
                    )
                ],
                volumes=[run_v2.Volume(name="cache", empty_dir={})],
            ),
            job_fixture(
                desired_state,
                vpc_connector="projects/p/locations/r/connectors/c1",
            ),
        )

        for job in drift_jobs:
            with self.subTest(job=job):
                runtime = make_runtime(
                    services_client=FakeServicesClient(),
                    jobs_client=FakeJobsClient(
                        {resource_name: job},
                        iam_policies={resource_name: job_invoker_policy()},
                    ),
                    revisions_client=FakeRevisionsClient(),
                )
                self.assertFalse(runtime.verify_health(desired_state))

    def test_rollback_updates_existing_service_to_exact_digest(self) -> None:
        desired_state = service_desired_state()
        services = FakeServicesClient(
            {service_name("wrk-1"): service_fixture(desired_state)},
            iam_policies={service_name("wrk-1"): iap_invoker_policy()},
        )
        runtime = make_runtime(
            services_client=services,
            jobs_client=FakeJobsClient(),
            revisions_client=FakeRevisionsClient(),
        )

        runtime.rollback(
            workload_id=WorkloadId("wrk-1"),
            workload_owner_id=UserId("usr-1"),
            image_digest="b" * 64,
        )

        self.assertEqual(len(services.update_calls), 1)
        updated = services.update_calls[0]["service"]
        assert isinstance(updated, run_v2.Service)
        self.assertEqual(
            updated.template.containers[0].image,
            f"{REGION}-docker.pkg.dev/{PROJECT_ID}/mim/workloads@sha256:{'b' * 64}",
        )
    def test_rollback_service_rejects_wrong_or_missing_trusted_owner(self) -> None:
        desired_state = service_desired_state()
        for owner_id in (UserId("usr-2"), UserId("")):
            with self.subTest(owner_id=owner_id):
                services = FakeServicesClient(
                    {service_name("wrk-1"): service_fixture(desired_state)},
                    iam_policies={service_name("wrk-1"): iap_invoker_policy()},
                )
                runtime = make_runtime(
                    services_client=services,
                    jobs_client=FakeJobsClient(),
                    revisions_client=FakeRevisionsClient(),
                )

                with self.assertRaises(ExecutionPlaneError):
                    runtime.rollback(
                        workload_id=WorkloadId("wrk-1"),
                        workload_owner_id=owner_id,
                        image_digest="b" * 64,
                    )

                self.assertEqual(services.update_calls, [])

    def test_rollback_rejects_service_boundary_drift(self) -> None:
        desired_state = service_desired_state()
        drift_services = (
            service_fixture(desired_state, service_account="wrong@example.com"),
            service_fixture(desired_state, concurrency=5),
            service_fixture(desired_state, timeout_seconds=120),
            service_fixture(
                desired_state,
                containers=[nextjs_container(desired_state, cpu="2")],
            ),
            service_fixture(
                desired_state,
                containers=[nextjs_container(desired_state, cpu_idle=False)],
            ),
            service_fixture(
                desired_state,
                containers=[nextjs_container(desired_state, startup_cpu_boost=True)],
            ),
            service_fixture(
                desired_state,
                traffic=[
                    run_v2.TrafficTarget(
                        percent=100,
                        type_=(
                            run_v2.TrafficTargetAllocationType
                            .TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION
                        ),
                    )
                ],
            ),
            service_fixture(
                desired_state,
                containers=[
                    nextjs_container(
                        desired_state,
                        env=[run_v2.EnvVar(name="SECRET", value="x")],
                    )
                ],
            ),
            service_fixture(
                desired_state,
                containers=[
                    nextjs_container(
                        desired_state,
                        ports=[run_v2.ContainerPort(container_port=9090)],
                    )
                ],
            ),
            service_fixture(
                desired_state,
                containers=[
                    nextjs_container(
                        desired_state,
                        volume_mounts=[
                            run_v2.VolumeMount(name="cache", mount_path="/cache")
                        ],
                    )
                ],
                volumes=[run_v2.Volume(name="cache", empty_dir={})],
            ),
            service_fixture(
                desired_state,
                containers=[
                    nextjs_container(desired_state),
                    run_v2.Container(image=desired_state.envelope.payload.image_uri),
                ],
            ),
            service_fixture(
                desired_state,
                containers=[
                    nextjs_container(
                        desired_state,
                        command=["node"],
                        args=["server.js"],
                    )
                ],
            ),
            service_fixture(
                desired_state,
                containers=[nextjs_container(desired_state, args=["start"])],
            ),
            service_fixture(desired_state, iap_enabled=True),
            service_fixture(desired_state, invoker_iam_disabled=True),
        )

        for service in drift_services:
            services = FakeServicesClient(
                {service_name("wrk-1"): service},
                iam_policies={service_name("wrk-1"): iap_invoker_policy()},
            )
            runtime = make_runtime(
                services_client=services,
                jobs_client=FakeJobsClient(),
                revisions_client=FakeRevisionsClient(),
            )

            with self.subTest(service=service):
                with self.assertRaises(ExecutionPlaneError):
                    runtime.rollback(
                        workload_id=WorkloadId("wrk-1"),
                        workload_owner_id=UserId("usr-1"),
                        image_digest="b" * 64,
                    )
                self.assertEqual(services.update_calls, [])

    def test_rollback_rejects_public_or_extra_run_invokers(self) -> None:
        desired_state = service_desired_state()
        conditional = iap_invoker_policy()
        conditional.bindings[0].condition.CopyFrom(
            expr_pb2.Expr(expression="true")
        )
        invalid_policies = (
            policy_pb2.Policy(
                bindings=(
                    policy_pb2.Binding(
                        role="roles/run.invoker",
                        members=("allUsers",),
                    ),
                )
            ),
            policy_pb2.Policy(
                bindings=(
                    policy_pb2.Binding(
                        role="roles/run.invoker",
                        members=(
                            f"serviceAccount:{iap_service_agent()}",
                            "allAuthenticatedUsers",
                        ),
                    ),
                )
            ),
            conditional,
            policy_with_extra_binding(
                iap_invoker_policy(),
                role="roles/run.admin",
            ),
            policy_with_extra_binding(
                iap_invoker_policy(),
                role=f"projects/{PROJECT_ID}/roles/workloadRuntimeAdmin",
            ),
        )
        for policy in invalid_policies:
            with self.subTest(policy=policy):
                services = FakeServicesClient(
                    {service_name("wrk-1"): service_fixture(desired_state)},
                    iam_policies={service_name("wrk-1"): policy},
                )
                runtime = make_runtime(
                    services_client=services,
                    jobs_client=FakeJobsClient(),
                    revisions_client=FakeRevisionsClient(),
                )

                with self.assertRaises(ExecutionPlaneError):
                    runtime.rollback(
                        workload_id=WorkloadId("wrk-1"),
                        workload_owner_id=UserId("usr-1"),
                        image_digest="b" * 64,
                    )

                self.assertEqual(services.update_calls, [])

    def test_rollback_updates_existing_job_when_service_is_absent(self) -> None:
        desired_state = verified_state(WorkloadKind.SCHEDULED_SCRIPT)
        jobs = FakeJobsClient(
            {
                job_name("wrk-1"): job_fixture(desired_state)
            },
            iam_policies={job_name("wrk-1"): job_invoker_policy()},
        )
        manager = FakeIapAccessPolicyManager(allowed=False)
        runtime = make_runtime(
            services_client=FakeServicesClient(),
            jobs_client=jobs,
            revisions_client=FakeRevisionsClient(),
            iap_manager=manager,
        )

        runtime.rollback(
            workload_id=WorkloadId("wrk-1"),
            workload_owner_id=UserId("usr-1"),
            image_digest="c" * 64,
        )

        self.assertEqual(len(jobs.update_calls), 1)
        updated = jobs.update_calls[0]["job"]
        assert isinstance(updated, run_v2.Job)
        self.assertEqual(
            updated.template.template.containers[0].image,
            f"{REGION}-docker.pkg.dev/{PROJECT_ID}/mim/workloads@sha256:{'c' * 64}",
        )
        self.assertEqual(manager.ensure_calls, [])
        self.assertEqual(manager.verify_calls, [])

    def test_rollback_rejects_job_invoker_policy_drift(self) -> None:
        desired_state = verified_state(WorkloadKind.SCHEDULED_SCRIPT)
        resource_name = job_name("wrk-1")
        invalid_policies = (
            policy_pb2.Policy(),
            policy_with_extra_binding(
                job_invoker_policy(),
                role="roles/run.admin",
            ),
            policy_with_extra_binding(
                job_invoker_policy(),
                role=f"projects/{PROJECT_ID}/roles/jobRunnerAdmin",
            ),
        )

        for policy in invalid_policies:
            with self.subTest(policy=policy):
                jobs = FakeJobsClient(
                    {resource_name: job_fixture(desired_state)},
                    iam_policies={resource_name: policy},
                )
                runtime = make_runtime(jobs_client=jobs)

                with self.assertRaises(ExecutionPlaneError):
                    runtime.rollback(
                        workload_id=WorkloadId("wrk-1"),
                        workload_owner_id=UserId("usr-1"),
                        image_digest="c" * 64,
                    )

                self.assertEqual(jobs.update_calls, [])

    def test_rollback_rejects_job_boundary_drift(self) -> None:
        desired_state = verified_state(WorkloadKind.SCHEDULED_SCRIPT)
        drift_jobs = (
            job_fixture(desired_state, max_retries=0),
            job_fixture(
                desired_state,
                containers=[script_container(desired_state, cpu="2")],
            ),
            job_fixture(
                desired_state,
                containers=[script_container(desired_state, cpu_idle=True)],
            ),
            job_fixture(
                desired_state,
                containers=[script_container(desired_state, startup_cpu_boost=True)],
            ),
            job_fixture(
                desired_state,
                containers=[
                    script_container(
                        desired_state,
                        env=[run_v2.EnvVar(name="SECRET", value="x")],
                    )
                ],
            ),
            job_fixture(
                desired_state,
                containers=[
                    script_container(
                        desired_state,
                        ports=[run_v2.ContainerPort(container_port=9090)],
                    )
                ],
            ),
            job_fixture(
                desired_state,
                containers=[
                    script_container(
                        desired_state,
                        volume_mounts=[
                            run_v2.VolumeMount(name="cache", mount_path="/cache")
                        ],
                    )
                ],
                volumes=[run_v2.Volume(name="cache", empty_dir={})],
            ),
            job_fixture(
                desired_state,
                containers=[
                    script_container(desired_state),
                    run_v2.Container(image=desired_state.envelope.payload.image_uri),
                ],
            ),
            job_fixture(
                desired_state,
                containers=[
                    script_container(
                        desired_state,
                        command=["sh"],
                        args=["-c", "python main.py"],
                    )
                ],
            ),
            job_fixture(
                desired_state,
                containers=[script_container(desired_state, args=["other.py"])],
            ),
        )

        for job in drift_jobs:
            jobs = FakeJobsClient(
                {job_name("wrk-1"): job},
                iam_policies={job_name("wrk-1"): job_invoker_policy()},
            )
            runtime = make_runtime(
                services_client=FakeServicesClient(),
                jobs_client=jobs,
                revisions_client=FakeRevisionsClient(),
            )

            with self.subTest(job=job):
                with self.assertRaises(ExecutionPlaneError):
                    runtime.rollback(
                        workload_id=WorkloadId("wrk-1"),
                        workload_owner_id=UserId("usr-1"),
                        image_digest="c" * 64,
                    )
                self.assertEqual(jobs.update_calls, [])


if __name__ == "__main__":
    unittest.main()
