"""Cloud Run runtime adapter for verified private workload deploys."""

from __future__ import annotations

from hashlib import sha256
from typing import Any
from urllib.parse import urlparse

from google.api_core.exceptions import NotFound
from google.cloud import run_v2
from google.iam.v1 import (  # type: ignore[import-untyped]
    iam_policy_pb2,  # type: ignore[import-untyped]
    policy_pb2,  # type: ignore[import-untyped]
)
from google.protobuf.duration_pb2 import Duration  # type: ignore[import-untyped]

from mim_control_plane.adapters.iap_access import IapAccessPolicyManager
from mim_control_plane.config import REGION as CONFIG_REGION
from mim_control_plane.config import _validate_project_id
from mim_control_plane.domain.models import UserId, WorkloadId
from mim_control_plane.ports.execution import (
    ExecutionPlaneError,
    RuntimePort,
    RuntimeServiceRoute,
)
from mim_control_plane.services.render import DesiredStateTarget, VerifiedDesiredState
from mim_control_plane.services.runtime_identity import runtime_identity_spec
from mim_control_plane.services.runtime_naming import (
    app_gateway_invoker_member,
    cloud_run_job_name,
    cloud_run_parent,
    cloud_run_service_name,
    normalize_reviewed_breakglass_members,
    schedule_gateway_invoker_member,
    workload_resource_suffix,
)

_IMAGE_REPOSITORY = "mim/workloads"
_DIGEST_LENGTH = 64
_MIM_MANAGED_BY = "mim-control-plane"
_SERVICE_CONCURRENCY = 20
_SERVICE_LAUNCHES = frozenset(
    {
        (
            ("./node_modules/.bin/next",),
            ("start", "--hostname", "0.0.0.0", "--port", "8080"),
        ),
        (
            ("streamlit",),
            (
                "run",
                "app.py",
                "--server.address",
                "0.0.0.0",
                "--server.port",
                "8080",
            ),
        ),
    }
)
_JOB_LAUNCH = (("python",), ("main.py",))


class CloudRunRuntimePort(RuntimePort):
    def __init__(
        self,
        *,
        project_id: str,
        project_number: str,
        region: str,
        services_client: Any | None = None,
        jobs_client: Any | None = None,
        revisions_client: Any | None = None,
        iap_access_policy_manager: IapAccessPolicyManager | None = None,
        reviewed_breakglass_members: tuple[str, ...] = (),
    ) -> None:
        self._project_id = _validate_project_id(_require_text(project_id, "project_id"))
        self._project_number = _require_numeric_project_number(project_number)
        self._region = _require_text(region, "region")
        if self._region != CONFIG_REGION:
            raise ValueError("Cloud Run adapter region must match configured REGION.")
        if services_client is None or jobs_client is None or revisions_client is None:
            raise ValueError(
                "Cloud Run adapter requires explicit injected official clients."
            )
        self._services = services_client
        self._jobs = jobs_client
        self._revisions = revisions_client
        self._iap_access_policy_manager = iap_access_policy_manager
        self._reviewed_breakglass_members = normalize_reviewed_breakglass_members(
            reviewed_breakglass_members
        )
        self._gateway_invoker_members = (
            app_gateway_invoker_member(self._project_id),
            *self._reviewed_breakglass_members,
        )

    def apply(self, desired_state: VerifiedDesiredState) -> None:
        payload = _validated_payload(
            desired_state,
            project_id=self._project_id,
            region=self._region,
        )
        if payload.target is DesiredStateTarget.CLOUD_RUN_SERVICE:
            self._apply_service(payload)
            return
        if payload.target is DesiredStateTarget.CLOUD_RUN_JOB:
            self._apply_job(payload)
            return
        raise ExecutionPlaneError("unsupported Cloud Run target.")

    def verify_health(self, desired_state: VerifiedDesiredState) -> bool:
        try:
            payload = _validated_payload(
                desired_state,
                project_id=self._project_id,
                region=self._region,
            )
            if payload.target is DesiredStateTarget.CLOUD_RUN_SERVICE:
                service = self._services.get_service(
                    name=cloud_run_service_name(
                        project_id=self._project_id,
                        region=self._region,
                        workload_id=payload.workload_id,
                    )
                )
                if not _service_matches_payload(service, payload):
                    return False
                policy = self._services.get_iam_policy(
                    iam_policy_pb2.GetIamPolicyRequest(resource=service.name)
                )
                if not _policy_has_exact_run_invoker(
                    policy,
                    members=self._gateway_invoker_members,
                ):
                    return False
                if _validated_service_route(service.name, service.uri) is None:
                    return False
                revision_name = _revision_name(
                    service_name=service.name,
                    latest_ready_revision=service.latest_ready_revision,
                )
                revision = self._revisions.get_revision(name=revision_name)
                return _revision_matches_payload(revision, payload)
            if payload.target is DesiredStateTarget.CLOUD_RUN_JOB:
                job = self._jobs.get_job(
                    name=cloud_run_job_name(
                        project_id=self._project_id,
                        region=self._region,
                        workload_id=payload.workload_id,
                    )
                )
                if not _job_matches_payload(job, payload):
                    return False
                policy = self._jobs.get_iam_policy(
                    iam_policy_pb2.GetIamPolicyRequest(resource=job.name)
                )
                return _policy_has_exact_run_invoker(
                    policy,
                    members=(schedule_gateway_invoker_member(self._project_id),),
                )
            return False
        except Exception:
            return False

    def readback_service_route(
        self,
        desired_state: VerifiedDesiredState,
    ) -> RuntimeServiceRoute | None:
        payload = _validated_payload(
            desired_state,
            project_id=self._project_id,
            region=self._region,
        )
        if payload.target is not DesiredStateTarget.CLOUD_RUN_SERVICE:
            return None
        resource_name = cloud_run_service_name(
            project_id=self._project_id,
            region=self._region,
            workload_id=payload.workload_id,
        )
        service = self._services.get_service(name=resource_name)
        if not _service_matches_payload(service, payload):
            raise ExecutionPlaneError("Cloud Run service route readback was denied.")
        policy = self._services.get_iam_policy(
            iam_policy_pb2.GetIamPolicyRequest(resource=resource_name)
        )
        if not _policy_has_exact_run_invoker(
            policy,
            members=self._gateway_invoker_members,
        ):
            raise ExecutionPlaneError("Cloud Run service route readback was denied.")
        uri = _validated_service_route(resource_name, service.uri)
        if uri is None:
            raise ExecutionPlaneError("Cloud Run service route readback was denied.")
        return RuntimeServiceRoute(resource_name=resource_name, uri=uri)

    def rollback(
        self,
        *,
        workload_id: WorkloadId,
        workload_owner_id: UserId,
        image_digest: str,
    ) -> None:
        if type(workload_id) is not str or not workload_id.strip():
            raise ExecutionPlaneError("rollback requires exact workload id.")
        if (
            type(workload_owner_id) is not str
            or not workload_owner_id.strip()
            or workload_owner_id.strip() != workload_owner_id
        ):
            raise ExecutionPlaneError("rollback requires exact workload owner id.")
        image_uri = _image_uri(
            project_id=self._project_id,
            region=self._region,
            image_digest=image_digest,
        )
        service_name = cloud_run_service_name(
            project_id=self._project_id,
            region=self._region,
            workload_id=str(workload_id),
        )
        try:
            service = self._services.get_service(name=service_name)
        except NotFound:
            service = None
        if service is not None:
            policy = self._services.get_iam_policy(
                iam_policy_pb2.GetIamPolicyRequest(resource=service_name)
            )
            _require_safe_service_boundary(
                service,
                policy=policy,
                project_id=self._project_id,
                region=self._region,
                workload_id=str(workload_id),
                workload_owner_id=str(workload_owner_id),
                gateway_invoker_members=self._gateway_invoker_members,
            )
            updated = run_v2.Service(service)
            _set_service_image(updated, image_uri)
            self._services.update_service(service=updated).result()
            return
        job_name = cloud_run_job_name(
            project_id=self._project_id,
            region=self._region,
            workload_id=str(workload_id),
        )
        try:
            job = self._jobs.get_job(name=job_name)
        except NotFound as exc:
            raise ExecutionPlaneError(
                "Cloud Run rollback target was not found."
            ) from exc
        job_policy = self._jobs.get_iam_policy(
            iam_policy_pb2.GetIamPolicyRequest(resource=job_name)
        )
        _require_safe_job_boundary(
            job,
            policy=job_policy,
            project_id=self._project_id,
            region=self._region,
            workload_id=str(workload_id),
            workload_owner_id=str(workload_owner_id),
        )
        updated_job = run_v2.Job(job)
        _set_job_image(updated_job, image_uri)
        self._jobs.update_job(job=updated_job).result()

    def _apply_service(self, payload: Any) -> None:
        name = cloud_run_service_name(
            project_id=self._project_id,
            region=self._region,
            workload_id=payload.workload_id,
        )
        service = _service_resource(name=name, payload=payload)
        try:
            self._services.get_service(name=name)
        except NotFound:
            self._services.create_service(
                parent=_parent(self._project_id, self._region),
                service=service,
                service_id=name.rsplit("/", 1)[1],
            ).result()
        else:
            self._services.update_service(service=service).result()
        self._reconcile_service_invoker_policy(name)

    def _apply_job(self, payload: Any) -> None:
        name = cloud_run_job_name(
            project_id=self._project_id,
            region=self._region,
            workload_id=payload.workload_id,
        )
        job = _job_resource(name=name, payload=payload)
        try:
            self._jobs.get_job(name=name)
        except NotFound:
            self._jobs.create_job(
                parent=_parent(self._project_id, self._region),
                job=job,
                job_id=name.rsplit("/", 1)[1],
            ).result()
        else:
            self._jobs.update_job(job=job).result()
        self._reconcile_job_invoker_policy(name)

    def _reconcile_service_invoker_policy(self, service_name: str) -> None:
        self._reconcile_invoker_policy(
            client=self._services,
            resource_name=service_name,
            members=self._gateway_invoker_members,
        )

    def _reconcile_job_invoker_policy(self, job_name: str) -> None:
        self._reconcile_invoker_policy(
            client=self._jobs,
            resource_name=job_name,
            members=(schedule_gateway_invoker_member(self._project_id),),
        )

    def _reconcile_invoker_policy(
        self,
        *,
        client: Any,
        resource_name: str,
        members: tuple[str, ...],
    ) -> None:
        current = client.get_iam_policy(
            iam_policy_pb2.GetIamPolicyRequest(resource=resource_name)
        )
        desired = _authoritative_invoker_policy(
            current,
            members=members,
        )
        written = client.set_iam_policy(
            iam_policy_pb2.SetIamPolicyRequest(
                resource=resource_name,
                policy=desired,
            )
        )
        if not _policy_has_exact_run_invoker(written, members=members):
            raise ExecutionPlaneError(
                "Cloud Run invoker IAM could not be reconciled."
            )
        observed = client.get_iam_policy(
            iam_policy_pb2.GetIamPolicyRequest(resource=resource_name)
        )
        if not _policy_has_exact_run_invoker(observed, members=members):
            raise ExecutionPlaneError(
                "Cloud Run invoker IAM could not be reconciled."
            )


def _validated_payload(
    desired_state: VerifiedDesiredState,
    *,
    project_id: str,
    region: str,
) -> Any:
    if type(desired_state) is not VerifiedDesiredState:
        raise ExecutionPlaneError("runtime requires verified desired state.")
    payload = desired_state.envelope.payload
    if payload.project_id != project_id or payload.region != region:
        raise ExecutionPlaneError("Cloud Run target project/region drifted.")
    if payload.service_min_instances != 0 or payload.service_max_instances != 1:
        raise ExecutionPlaneError("Cloud Run instance bounds must stay at min=0 max=1.")
    expected_service_account = _runtime_service_account(
        project_id=project_id,
        workload_id=payload.workload_id,
    )
    if payload.runtime_service_account != expected_service_account:
        raise ExecutionPlaneError("runtime identity must be per-workload and exact.")
    if payload.image_uri != _image_uri(
        project_id=project_id,
        region=region,
        image_digest=_extract_digest(payload.image_uri),
    ):
        raise ExecutionPlaneError("runtime image must use an exact immutable digest.")
    if payload.target is DesiredStateTarget.CLOUD_RUN_JOB:
        if (
            payload.job_task_count != 1
            or payload.job_parallelism != 1
            or payload.job_retry_count != 1
            or payload.job_timeout_seconds != 300
        ):
            raise ExecutionPlaneError("Cloud Run Job bounds drifted.")
    return payload


def _service_resource(*, name: str, payload: Any) -> run_v2.Service:
    return run_v2.Service(
        name=name,
        labels=_payload_labels(payload),
        ingress=run_v2.IngressTraffic.INGRESS_TRAFFIC_ALL,
        iap_enabled=False,
        invoker_iam_disabled=False,
        template=run_v2.RevisionTemplate(
            service_account=payload.runtime_service_account,
            scaling=run_v2.RevisionScaling(
                min_instance_count=payload.service_min_instances,
                max_instance_count=payload.service_max_instances,
            ),
            timeout=Duration(seconds=payload.service_timeout_seconds),
            max_instance_request_concurrency=payload.service_concurrency,
            containers=[_service_container(payload)],
        ),
        traffic=[
            run_v2.TrafficTarget(
                percent=100,
                type_=(
                    run_v2.TrafficTargetAllocationType
                    .TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST
                ),
            )
        ],
    )


def _job_resource(*, name: str, payload: Any) -> run_v2.Job:
    return run_v2.Job(
        name=name,
        labels=_payload_labels(payload),
        template=run_v2.ExecutionTemplate(
            task_count=payload.job_task_count,
            parallelism=payload.job_parallelism,
            template=run_v2.TaskTemplate(
                service_account=payload.runtime_service_account,
                max_retries=payload.job_retry_count,
                timeout=Duration(seconds=payload.job_timeout_seconds),
                containers=[_job_container(payload)],
            ),
        ),
    )


def _service_container(payload: Any) -> run_v2.Container:
    return run_v2.Container(
        image=payload.image_uri,
        command=[payload.launch_command[0]],
        args=list(payload.launch_command[1:]),
        env=_service_secret_env_vars(payload),
        resources=_service_resources(payload),
    )


def _job_container(payload: Any) -> run_v2.Container:
    return run_v2.Container(
        image=payload.image_uri,
        command=[payload.launch_command[0]],
        args=list(payload.launch_command[1:]),
        resources=_job_resources(payload),
    )


def _service_resources(payload: Any) -> run_v2.ResourceRequirements:
    return run_v2.ResourceRequirements(
        limits={"cpu": str(payload.cpu), "memory": f"{payload.memory_mib}Mi"},
        cpu_idle=not payload.request_cpu_always_allocated,
        startup_cpu_boost=False,
    )


def _job_resources(payload: Any) -> run_v2.ResourceRequirements:
    return run_v2.ResourceRequirements(
        limits={"cpu": str(payload.cpu), "memory": f"{payload.memory_mib}Mi"},
        cpu_idle=False,
        startup_cpu_boost=False,
    )


def _service_matches_payload(service: run_v2.Service, payload: Any) -> bool:
    if not service.name or service.reconciling:
        return False
    if dict(service.labels) != _payload_labels(payload):
        return False
    if service.ingress is not run_v2.IngressTraffic.INGRESS_TRAFFIC_ALL:
        return False
    if service.iap_enabled or service.invoker_iam_disabled:
        return False
    if service.generation != service.observed_generation:
        return False
    if (
        service.terminal_condition.state
        is not run_v2.Condition.State.CONDITION_SUCCEEDED
    ):
        return False
    if not service.latest_ready_revision:
        return False
    if service.template.volumes:
        return False
    if service.template.vpc_access.connector:
        return False
    if not _traffic_is_latest_100(service.traffic):
        return False
    if service.template.service_account != payload.runtime_service_account:
        return False
    if service.template.scaling.min_instance_count != payload.service_min_instances:
        return False
    if service.template.scaling.max_instance_count != payload.service_max_instances:
        return False
    if service.template.max_instance_request_concurrency != payload.service_concurrency:
        return False
    if service.template.timeout.seconds != payload.service_timeout_seconds:
        return False
    return _service_containers_match_payload(service.template.containers, payload)


def _revision_matches_payload(revision: run_v2.Revision, payload: Any) -> bool:
    if revision.generation != revision.observed_generation:
        return False
    if revision.volumes:
        return False
    if revision.vpc_access.connector:
        return False
    if revision.service_account != payload.runtime_service_account:
        return False
    if not any(
        condition.state is run_v2.Condition.State.CONDITION_SUCCEEDED
        for condition in revision.conditions
    ):
        return False
    return _service_containers_match_payload(revision.containers, payload)


def _job_matches_payload(job: run_v2.Job, payload: Any) -> bool:
    if not job.name or job.reconciling:
        return False
    if dict(job.labels) != _payload_labels(payload):
        return False
    if job.generation != job.observed_generation:
        return False
    if job.terminal_condition.state is not run_v2.Condition.State.CONDITION_SUCCEEDED:
        return False
    if job.template.task_count != payload.job_task_count:
        return False
    if job.template.parallelism != payload.job_parallelism:
        return False
    task = job.template.template
    if task.volumes:
        return False
    if task.vpc_access.connector:
        return False
    if task.service_account != payload.runtime_service_account:
        return False
    if task.max_retries != payload.job_retry_count:
        return False
    if task.timeout.seconds != payload.job_timeout_seconds:
        return False
    return _job_containers_match_payload(task.containers, payload)


def _set_service_image(service: run_v2.Service, image_uri: str) -> None:
    if not service.template.containers:
        raise ExecutionPlaneError(
            "Cloud Run service is missing its container template."
        )
    service.template.containers[0].image = image_uri


def _set_job_image(job: run_v2.Job, image_uri: str) -> None:
    if not job.template.template.containers:
        raise ExecutionPlaneError("Cloud Run job is missing its container template.")
    job.template.template.containers[0].image = image_uri


def _payload_labels(payload: Any) -> dict[str, str]:
    return dict(payload.labels)


def _service_secret_env_vars(payload: Any) -> list[run_v2.EnvVar]:
    env_vars: list[run_v2.EnvVar] = []
    for attachment in payload.secret_attachments:
        env_vars.append(
            run_v2.EnvVar(
                name=attachment.env_name,
                value_source=run_v2.EnvVarSource(
                    secret_key_ref=run_v2.SecretKeySelector(
                        secret=attachment.secret_name,
                        version=attachment.secret_version,
                    )
                ),
            )
        )
    return env_vars


def _service_containers_match_payload(containers: Any, payload: Any) -> bool:
    if len(containers) != 1:
        return False
    container = containers[0]
    if container.image != payload.image_uri:
        return False
    if tuple(container.command) != (payload.launch_command[0],):
        return False
    if tuple(container.args) != tuple(payload.launch_command[1:]):
        return False
    if dict(container.resources.limits) != {
        "cpu": str(payload.cpu),
        "memory": f"{payload.memory_mib}Mi",
    }:
        return False
    if container.resources.cpu_idle is not (not payload.request_cpu_always_allocated):
        return False
    if container.resources.startup_cpu_boost:
        return False
    if container.ports or container.volume_mounts:
        return False
    if not _container_env_matches_secret_attachments(
        container.env,
        payload.secret_attachments,
    ):
        return False
    return True


def _job_containers_match_payload(containers: Any, payload: Any) -> bool:
    if len(containers) != 1:
        return False
    container = containers[0]
    if container.image != payload.image_uri:
        return False
    if tuple(container.command) != (payload.launch_command[0],):
        return False
    if tuple(container.args) != tuple(payload.launch_command[1:]):
        return False
    if dict(container.resources.limits) != {
        "cpu": str(payload.cpu),
        "memory": f"{payload.memory_mib}Mi",
    }:
        return False
    if container.resources.cpu_idle:
        return False
    if container.resources.startup_cpu_boost:
        return False
    if container.env or container.ports or container.volume_mounts:
        return False
    return True


def _container_env_matches_secret_attachments(
    env_vars: Any,
    attachments: Any,
) -> bool:
    if len(env_vars) != len(attachments):
        return False
    observed: list[tuple[str, str, str]] = []
    for env_var in env_vars:
        if getattr(env_var, "value", ""):
            return False
        source = getattr(env_var, "value_source", None)
        secret_ref = getattr(source, "secret_key_ref", None)
        if secret_ref is None:
            return False
        observed.append(
            (
                str(env_var.name),
                str(secret_ref.secret),
                str(secret_ref.version),
            )
        )
    expected = [
        (attachment.env_name, attachment.secret_name, attachment.secret_version)
        for attachment in attachments
    ]
    return observed == expected


def _env_vars_are_exact_secret_bindings(
    env_vars: Any,
    *,
    project_id: str,
) -> bool:
    seen_names: set[str] = set()
    for env_var in env_vars:
        if getattr(env_var, "value", ""):
            return False
        name = getattr(env_var, "name", "")
        if (
            type(name) is not str
            or not name.startswith("MIM_SECRET_")
            or name in seen_names
        ):
            return False
        seen_names.add(name)
        source = getattr(env_var, "value_source", None)
        secret_ref = getattr(source, "secret_key_ref", None)
        secret = getattr(secret_ref, "secret", "")
        version = getattr(secret_ref, "version", "")
        if (
            type(secret) is not str
            or not secret
            or "/" in secret
            or type(version) is not str
            or not version.isdigit()
            or version.startswith("0")
        ):
            return False
    return True


def _require_safe_service_boundary(
    service: run_v2.Service,
    *,
    policy: policy_pb2.Policy,
    project_id: str,
    region: str,
    workload_id: str,
    workload_owner_id: str,
    gateway_invoker_members: tuple[str, ...],
) -> None:
    expected_name = cloud_run_service_name(
        project_id=project_id,
        region=region,
        workload_id=workload_id,
    )
    if service.name != expected_name:
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if service.labels.get("managed-by") != _MIM_MANAGED_BY:
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if service.labels.get("workload-hash") != workload_resource_suffix(workload_id):
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if service.labels.get("owner-hash") != _owner_hash(workload_owner_id):
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if service.ingress is not run_v2.IngressTraffic.INGRESS_TRAFFIC_ALL:
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if service.iap_enabled or service.invoker_iam_disabled:
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if not _traffic_is_latest_100(service.traffic):
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if not _policy_has_exact_run_invoker(
        policy,
        members=gateway_invoker_members,
    ):
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if _validated_service_route(service.name, service.uri) is None:
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if service.template.volumes or service.template.vpc_access.connector:
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if service.template.service_account != _runtime_service_account(
        project_id=project_id,
        workload_id=workload_id,
    ):
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if service.template.scaling.min_instance_count != 0:
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if service.template.scaling.max_instance_count != 1:
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if service.template.max_instance_request_concurrency != _SERVICE_CONCURRENCY:
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    containers = service.template.containers
    if len(containers) != 1:
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if not _is_mim_image_uri(containers[0].image, project_id, region):
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")
    if not _service_boundary_container_is_exact(
        containers[0],
        timeout_seconds=service.template.timeout.seconds,
        project_id=project_id,
    ):
        raise ExecutionPlaneError("Cloud Run service boundary drifted.")


def _require_safe_job_boundary(
    job: run_v2.Job,
    *,
    policy: policy_pb2.Policy,
    project_id: str,
    region: str,
    workload_id: str,
    workload_owner_id: str,
) -> None:
    expected_name = cloud_run_job_name(
        project_id=project_id,
        region=region,
        workload_id=workload_id,
    )
    if job.name != expected_name:
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    if job.labels.get("managed-by") != _MIM_MANAGED_BY:
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    if job.labels.get("workload-hash") != workload_resource_suffix(workload_id):
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    if job.labels.get("owner-hash") != _owner_hash(workload_owner_id):
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    if not _policy_has_exact_run_invoker(
        policy,
        members=(schedule_gateway_invoker_member(project_id),),
    ):
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    if job.template.task_count != 1 or job.template.parallelism != 1:
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    task = job.template.template
    if task.volumes or task.vpc_access.connector:
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    if task.service_account != _runtime_service_account(
        project_id=project_id,
        workload_id=workload_id,
    ):
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    if task.max_retries != 1 or task.timeout.seconds != 300:
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    containers = task.containers
    if len(containers) != 1:
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    if not _is_mim_image_uri(containers[0].image, project_id, region):
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")
    if not _job_boundary_container_is_exact(containers[0]):
        raise ExecutionPlaneError("Cloud Run job boundary drifted.")


def _revision_name(*, service_name: str, latest_ready_revision: str) -> str:
    if "/revisions/" in latest_ready_revision:
        return latest_ready_revision
    return f"{service_name}/revisions/{latest_ready_revision}"


def _runtime_service_account(*, project_id: str, workload_id: str) -> str:
    return runtime_identity_spec(
        project_id=project_id,
        workload_id=workload_id,
    ).email


def _owner_hash(owner_id: str) -> str:
    return sha256(owner_id.encode("utf-8")).hexdigest()[:12]


def _parent(project_id: str, region: str) -> str:
    return cloud_run_parent(project_id=project_id, region=region)


def _image_uri(*, project_id: str, region: str, image_digest: str) -> str:
    digest = _extract_digest(image_digest)
    return f"{region}-docker.pkg.dev/{project_id}/{_IMAGE_REPOSITORY}@sha256:{digest}"


def _is_mim_image_uri(value: str, project_id: str, region: str) -> bool:
    try:
        return value == _image_uri(
            project_id=project_id,
            region=region,
            image_digest=_extract_digest(value),
        )
    except ExecutionPlaneError:
        return False


def _require_numeric_project_number(value: str) -> str:
    if type(value) is not str or not value.isdigit() or value.startswith("0"):
        raise ValueError("project_number must be an exact numeric project number.")
    return value


def _authoritative_invoker_policy(
    current: policy_pb2.Policy,
    *,
    members: tuple[str, ...],
) -> policy_pb2.Policy:
    desired = policy_pb2.Policy(version=current.version, etag=current.etag)
    invoker = desired.bindings.add(role="roles/run.invoker")
    invoker.members.extend(members)
    return desired


def _policy_has_exact_run_invoker(
    policy: policy_pb2.Policy,
    *,
    members: tuple[str, ...],
) -> bool:
    if len(policy.bindings) != 1 or policy.audit_configs:
        return False
    binding = policy.bindings[0]
    return (
        binding.role == "roles/run.invoker"
        and not binding.HasField("condition")
        and list(binding.members) == list(members)
    )


def _traffic_is_latest_100(traffic: Any) -> bool:
    return (
        len(traffic) == 1
        and traffic[0].percent == 100
        and traffic[0].type_
        is run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST
    )


def _expected_service_timeout_seconds(container: run_v2.Container) -> int | None:
    launch = (tuple(container.command), tuple(container.args))
    if launch not in _SERVICE_LAUNCHES:
        return None
    if launch[0] == ("streamlit",):
        return 3600
    return 300


def _validated_service_route(service_name: str, service_uri: str) -> str | None:
    if type(service_uri) is not str or not service_uri:
        return None
    parsed = urlparse(service_uri)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return None
    hostname = parsed.hostname
    if type(hostname) is not str or not hostname.endswith(".run.app"):
        return None
    service_id = service_name.rsplit("/", 1)[1]
    if not hostname.startswith(f"{service_id}-"):
        return None
    return f"https://{hostname}"


def _service_boundary_container_is_exact(
    container: run_v2.Container,
    *,
    timeout_seconds: int,
    project_id: str,
) -> bool:
    expected_timeout = _expected_service_timeout_seconds(container)
    return (
        expected_timeout is not None
        and timeout_seconds == expected_timeout
        and not container.ports
        and not container.volume_mounts
        and dict(container.resources.limits) == {"cpu": "1", "memory": "512Mi"}
        and container.resources.cpu_idle
        and not container.resources.startup_cpu_boost
        and _env_vars_are_exact_secret_bindings(container.env, project_id=project_id)
    )


def _job_boundary_container_is_exact(container: run_v2.Container) -> bool:
    return (
        (tuple(container.command), tuple(container.args)) == _JOB_LAUNCH
        and not container.env
        and not container.ports
        and not container.volume_mounts
        and dict(container.resources.limits) == {"cpu": "1", "memory": "512Mi"}
        and not container.resources.cpu_idle
        and not container.resources.startup_cpu_boost
    )


def _extract_digest(value: str) -> str:
    if type(value) is not str:
        raise ExecutionPlaneError("image digest must be text.")
    digest = value
    marker = "@sha256:"
    if marker in value:
        digest = value.rsplit(marker, 1)[1]
    if (
        len(digest) != _DIGEST_LENGTH
        or digest.lower() != digest
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ExecutionPlaneError("image digest must be exact lowercase sha256.")
    return digest


def _require_text(value: str, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value
