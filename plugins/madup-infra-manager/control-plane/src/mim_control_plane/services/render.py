"""Signed desired-state rendering for private deployment workers."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from mim_control_plane.config import (
    GITHUB_OWNER,
    PLAN_EXPIRY_MINUTES,
    REGION,
    SERVICE_CPU,
    SERVICE_MAX_INSTANCES,
    SERVICE_MEMORY_MIB,
    SERVICE_MIN_INSTANCES,
    TIMEZONE,
    _validate_project_id,
)
from mim_control_plane.domain.models import RepositoryAdmission, Workload
from mim_control_plane.domain.states import RepositoryAdmissionState, WorkloadKind
from mim_control_plane.services.build_template import build_template_for
from mim_control_plane.services.classifier import (
    WorkloadClassification,
    classify_snapshot,
)
from mim_control_plane.services.runtime_identity import runtime_identity_spec


class DesiredStateDenied(PermissionError):
    """Raised when desired-state material fails validation or verification."""


SCHEMA_VERSION = "mim-desired-state-v2"
PRIVATE_DEPLOY_WORKER_AUDIENCE = "private-deploy-worker-v1"
ARTIFACT_REPOSITORY = "mim"
ARTIFACT_IMAGE_NAME = "workloads"
SERVICE_CONCURRENCY = 20
NEXTJS_SERVICE_TIMEOUT_SECONDS = 300
STREAMLIT_SERVICE_TIMEOUT_SECONDS = 3600
JOB_TASK_COUNT = 1
JOB_PARALLELISM = 1
JOB_RETRY_COUNT = 1
JOB_TIMEOUT_SECONDS = 300
SERVICE_CPU_ALWAYS_ALLOCATED = False
_DENIED_MESSAGE = "Desired state was denied."
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_REPOSITORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LABEL_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_LABEL_VALUE_PATTERN = re.compile(r"^[a-z0-9-]{1,63}$")
_SECRET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SECRET_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SECRET_ENV_NAME_PATTERN = re.compile(r"^MIM_SECRET_[A-Z0-9_]{1,128}$")
_SECRET_VERSION_PATTERN = re.compile(r"^[1-9][0-9]{0,9}$")
_VALUE_LIKE_MARKERS = (
    "ghp_",
    "sk-",
    "api_key",
    "authorization",
    "cookie",
    "bearer",
    "access_token",
    "refresh_token",
)


def _require_non_empty_text(value: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(_DENIED_MESSAGE)


def _require_positive_int(value: int) -> None:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise ValueError(_DENIED_MESSAGE)


def _is_valid_commit_sha(value: str) -> bool:
    return _SHA1_PATTERN.fullmatch(value) is not None and value != "0" * 40


class DesiredStateTarget(StrEnum):
    CLOUD_RUN_SERVICE = "cloud_run_service"
    CLOUD_RUN_JOB = "cloud_run_job"


class DesiredStateAuthMode(StrEnum):
    GATEWAY_IAM = "gateway_iam"
    MACHINE_ONLY = "machine_only"


class DesiredStateIngress(StrEnum):
    PUBLIC_IAM = "public_iam"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class DesiredStateRenderContext:
    project_id: str
    key_id: str
    region: str = field(init=False, default=REGION)
    audience: str = field(init=False, default=PRIVATE_DEPLOY_WORKER_AUDIENCE)

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or type(self.key_id) is not str:
            raise ValueError(_DENIED_MESSAGE)
        _validate_project_id(self.project_id)
        if _IDENTIFIER_PATTERN.fullmatch(self.key_id) is None:
            raise ValueError(_DENIED_MESSAGE)


@dataclass(frozen=True, slots=True)
class DesiredStateSecretAttachment:
    secret_id: str
    secret_name: str
    secret_version: str
    env_name: str


@dataclass(frozen=True, slots=True)
class DesiredStatePayload:
    repository_admission_id: str
    repository_numeric_id: int
    repository_owner: str
    repository_name: str
    admitted_sha: str
    admission_version: int
    workload_id: str
    workload_owner_id: str
    workload_kind: WorkloadKind
    workload_version: int
    source_sha: str
    desired_manifest_hash: str
    snapshot_digest: str
    project_id: str
    region: str
    target: DesiredStateTarget
    image_uri: str
    runtime: str
    entrypoint: str
    install_command: tuple[str, ...]
    build_command: tuple[str, ...]
    launch_command: tuple[str, ...]
    required_files: tuple[str, ...]
    runtime_service_account: str
    cpu: int
    memory_mib: int
    service_min_instances: int
    service_max_instances: int
    request_cpu_always_allocated: bool
    service_concurrency: int
    service_timeout_seconds: int
    job_task_count: int | None
    job_parallelism: int | None
    job_retry_count: int | None
    job_timeout_seconds: int | None
    auth_mode: DesiredStateAuthMode
    ingress: DesiredStateIngress
    allow_unauthenticated: bool
    custom_domain: bool
    labels: tuple[tuple[str, str], ...]
    secret_attachments: tuple[DesiredStateSecretAttachment, ...] = ()
    schedule_cron: str | None = None
    schedule_timezone: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_text(self.repository_admission_id)
        _require_positive_int(self.repository_numeric_id)
        _require_non_empty_text(self.repository_owner)
        _require_non_empty_text(self.repository_name)
        if not _is_valid_repository_owner(self.repository_owner):
            raise ValueError(_DENIED_MESSAGE)
        if _REPOSITORY_NAME_PATTERN.fullmatch(self.repository_name) is None:
            raise ValueError(_DENIED_MESSAGE)
        if not _is_valid_commit_sha(self.admitted_sha):
            raise ValueError(_DENIED_MESSAGE)
        if not _is_valid_commit_sha(self.source_sha):
            raise ValueError(_DENIED_MESSAGE)
        _require_positive_int(self.admission_version)
        _require_non_empty_text(self.workload_id)
        _require_non_empty_text(self.workload_owner_id)
        if type(self.workload_kind) is not WorkloadKind:
            raise ValueError(_DENIED_MESSAGE)
        _require_positive_int(self.workload_version)
        _require_non_empty_text(self.desired_manifest_hash)
        if (
            type(self.snapshot_digest) is not str
            or _SNAPSHOT_DIGEST_PATTERN.fullmatch(self.snapshot_digest) is None
        ):
            raise ValueError(_DENIED_MESSAGE)
        if type(self.project_id) is not str or type(self.region) is not str:
            raise ValueError(_DENIED_MESSAGE)
        if type(self.target) is not DesiredStateTarget:
            raise ValueError(_DENIED_MESSAGE)
        if type(self.image_uri) is not str or type(self.runtime) is not str:
            raise ValueError(_DENIED_MESSAGE)
        if (
            type(self.entrypoint) is not str
            or type(self.runtime_service_account) is not str
        ):
            raise ValueError(_DENIED_MESSAGE)
        _require_str_tuple(self.install_command)
        _require_str_tuple(self.build_command)
        _require_str_tuple(self.launch_command)
        _require_str_tuple(self.required_files)
        _require_exact_int(self.cpu)
        _require_exact_int(self.memory_mib)
        _require_exact_int(self.service_min_instances)
        _require_exact_int(self.service_max_instances)
        if type(self.request_cpu_always_allocated) is not bool:
            raise ValueError(_DENIED_MESSAGE)
        _require_exact_int(self.service_concurrency)
        _require_exact_int(self.service_timeout_seconds)
        _require_optional_exact_int(self.job_task_count)
        _require_optional_exact_int(self.job_parallelism)
        _require_optional_exact_int(self.job_retry_count)
        _require_optional_exact_int(self.job_timeout_seconds)
        if type(self.auth_mode) is not DesiredStateAuthMode:
            raise ValueError(_DENIED_MESSAGE)
        if type(self.ingress) is not DesiredStateIngress:
            raise ValueError(_DENIED_MESSAGE)
        if (
            type(self.allow_unauthenticated) is not bool
            or type(self.custom_domain) is not bool
        ):
            raise ValueError(_DENIED_MESSAGE)
        if type(self.labels) is not tuple or type(self.secret_attachments) is not tuple:
            raise ValueError(_DENIED_MESSAGE)
        if self.schedule_cron is not None and type(self.schedule_cron) is not str:
            raise ValueError(_DENIED_MESSAGE)
        if (
            self.schedule_timezone is not None
            and type(self.schedule_timezone) is not str
        ):
            raise ValueError(_DENIED_MESSAGE)


@dataclass(frozen=True, slots=True)
class SignedDesiredStateEnvelope:
    schema_version: str
    key_id: str
    audience: str
    issued_at: datetime
    expires_at: datetime
    payload: DesiredStatePayload
    signature: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str:
            raise ValueError(_DENIED_MESSAGE)
        if type(self.key_id) is not str or type(self.audience) is not str:
            raise ValueError(_DENIED_MESSAGE)
        if type(self.payload) is not DesiredStatePayload:
            raise ValueError(_DENIED_MESSAGE)
        _require_utc_second(self.issued_at)
        _require_utc_second(self.expires_at)
        if (
            type(self.signature) is not str
            or _SIGNATURE_PATTERN.fullmatch(self.signature) is None
        ):
            raise ValueError(_DENIED_MESSAGE)


@dataclass(frozen=True, slots=True)
class VerifiedDesiredState:
    envelope: SignedDesiredStateEnvelope
    canonical_unsigned: bytes
    snapshot_digest: str


def render_signed_desired_state(
    *,
    workload: Workload,
    admission: RepositoryAdmission,
    snapshot: dict[str, bytes],
    image_digest: str,
    context: DesiredStateRenderContext,
    issued_at: datetime,
    signing_key: bytes,
    secret_attachments: tuple[DesiredStateSecretAttachment, ...] = (),
) -> SignedDesiredStateEnvelope:
    try:
        _validate_exact_input_types(
            workload=workload,
            admission=admission,
            snapshot=snapshot,
            context=context,
            issued_at=issued_at,
            signing_key=signing_key,
            secret_attachments=secret_attachments,
        )
        _validate_workload_and_admission(workload=workload, admission=admission)
        snapshot_copy = _copied_snapshot(snapshot)
        image_uri = _image_uri(project_id=context.project_id, image_digest=image_digest)
        attachments = _normalized_secret_attachments(secret_attachments)
        classified = classify_snapshot(snapshot_copy)
        if not isinstance(classified, WorkloadClassification):
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if type(classified) is not WorkloadClassification:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        snapshot_digest = _snapshot_digest(snapshot_copy)
        template = build_template_for(classified)
        if workload.kind is not classified.kind:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        runtime_service_account = _runtime_service_account(
            project_id=context.project_id,
            workload_id=str(workload.id),
        )
        target = (
            DesiredStateTarget.CLOUD_RUN_JOB
            if workload.kind is WorkloadKind.SCHEDULED_SCRIPT
            else DesiredStateTarget.CLOUD_RUN_SERVICE
        )
        payload = DesiredStatePayload(
            repository_admission_id=str(admission.id),
            repository_numeric_id=admission.repository_numeric_id,
            repository_owner=admission.owner,
            repository_name=admission.name,
            admitted_sha=admission.admitted_sha,
            admission_version=admission.version,
            workload_id=str(workload.id),
            workload_owner_id=str(workload.owner_id),
            workload_kind=workload.kind,
            workload_version=workload.version,
            source_sha=workload.source_sha,
            desired_manifest_hash=workload.desired_manifest_hash,
            snapshot_digest=snapshot_digest,
            project_id=context.project_id,
            region=context.region,
            target=target,
            image_uri=image_uri,
            runtime=template.runtime,
            entrypoint=classified.entrypoint,
            install_command=template.install_command,
            build_command=template.build_command,
            launch_command=template.launch_command,
            required_files=template.required_files,
            runtime_service_account=runtime_service_account,
            cpu=SERVICE_CPU,
            memory_mib=SERVICE_MEMORY_MIB,
            service_min_instances=SERVICE_MIN_INSTANCES,
            service_max_instances=SERVICE_MAX_INSTANCES,
            request_cpu_always_allocated=SERVICE_CPU_ALWAYS_ALLOCATED,
            service_concurrency=SERVICE_CONCURRENCY,
            service_timeout_seconds=_service_timeout_seconds(workload.kind),
            job_task_count=JOB_TASK_COUNT
            if target is DesiredStateTarget.CLOUD_RUN_JOB
            else None,
            job_parallelism=JOB_PARALLELISM
            if target is DesiredStateTarget.CLOUD_RUN_JOB
            else None,
            job_retry_count=JOB_RETRY_COUNT
            if target is DesiredStateTarget.CLOUD_RUN_JOB
            else None,
            job_timeout_seconds=JOB_TIMEOUT_SECONDS
            if target is DesiredStateTarget.CLOUD_RUN_JOB
            else None,
            auth_mode=(
                DesiredStateAuthMode.MACHINE_ONLY
                if target is DesiredStateTarget.CLOUD_RUN_JOB
                else DesiredStateAuthMode.GATEWAY_IAM
            ),
            ingress=(
                DesiredStateIngress.NONE
                if target is DesiredStateTarget.CLOUD_RUN_JOB
                else DesiredStateIngress.PUBLIC_IAM
            ),
            allow_unauthenticated=False,
            custom_domain=False,
            labels=_labels_for(workload=workload, admission=admission),
            secret_attachments=attachments,
            schedule_cron=template.schedule_cron,
            schedule_timezone=TIMEZONE if template.schedule_cron is not None else None,
        )
        expires_at = issued_at + timedelta(minutes=PLAN_EXPIRY_MINUTES)
        unsigned = SignedDesiredStateEnvelope(
            schema_version=SCHEMA_VERSION,
            key_id=context.key_id,
            audience=context.audience,
            issued_at=issued_at,
            expires_at=expires_at,
            payload=payload,
            signature="0" * 64,
        )
        signature = hmac.new(
            signing_key,
            canonical_unsigned_desired_state_bytes(unsigned),
            hashlib.sha256,
        ).hexdigest()
        return SignedDesiredStateEnvelope(
            schema_version=unsigned.schema_version,
            key_id=unsigned.key_id,
            audience=unsigned.audience,
            issued_at=unsigned.issued_at,
            expires_at=unsigned.expires_at,
            payload=unsigned.payload,
            signature=signature,
        )
    except DesiredStateDenied:
        raise
    except Exception:  # pragma: no cover - fail closed
        raise DesiredStateDenied(_DENIED_MESSAGE) from None


def verify_signed_desired_state(
    envelope: SignedDesiredStateEnvelope,
    *,
    context: DesiredStateRenderContext,
    signing_key: bytes,
    now: datetime,
) -> VerifiedDesiredState:
    try:
        if type(envelope) is not SignedDesiredStateEnvelope:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if type(context) is not DesiredStateRenderContext:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        _validate_signing_key(signing_key)
        _require_utc_second(now)
        if envelope.schema_version != SCHEMA_VERSION:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if envelope.key_id != context.key_id or envelope.audience != context.audience:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if envelope.issued_at > now or envelope.expires_at <= now:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if envelope.expires_at <= envelope.issued_at:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if envelope.expires_at - envelope.issued_at > timedelta(
            minutes=PLAN_EXPIRY_MINUTES
        ):
            raise DesiredStateDenied(_DENIED_MESSAGE)
        _validate_payload_policy(envelope.payload, context=context)
        expected = hmac.new(
            signing_key,
            canonical_unsigned_desired_state_bytes(envelope),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, envelope.signature):
            raise DesiredStateDenied(_DENIED_MESSAGE)
        return VerifiedDesiredState(
            envelope=envelope,
            canonical_unsigned=canonical_unsigned_desired_state_bytes(envelope),
            snapshot_digest=envelope.payload.snapshot_digest,
        )
    except DesiredStateDenied:
        raise
    except Exception:  # pragma: no cover - fail closed
        raise DesiredStateDenied(_DENIED_MESSAGE) from None


def canonical_unsigned_desired_state_bytes(
    envelope: SignedDesiredStateEnvelope,
) -> bytes:
    if type(envelope) is not SignedDesiredStateEnvelope:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    return _canonical_json_bytes(
        {
            "audience": envelope.audience,
            "expires_at": _isoformat_utc(envelope.expires_at),
            "issued_at": _isoformat_utc(envelope.issued_at),
            "key_id": envelope.key_id,
            "payload": _payload_to_json_ready(envelope.payload),
            "schema_version": envelope.schema_version,
        }
    )


def _payload_to_json_ready(payload: DesiredStatePayload) -> dict[str, Any]:
    if type(payload) is not DesiredStatePayload:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    return {
        "admission_version": payload.admission_version,
        "admitted_sha": payload.admitted_sha,
        "allow_unauthenticated": payload.allow_unauthenticated,
        "auth_mode": payload.auth_mode.value,
        "build_command": list(payload.build_command),
        "cpu": payload.cpu,
        "custom_domain": payload.custom_domain,
        "desired_manifest_hash": payload.desired_manifest_hash,
        "entrypoint": payload.entrypoint,
        "image_uri": payload.image_uri,
        "ingress": payload.ingress.value,
        "install_command": list(payload.install_command),
        "job_parallelism": payload.job_parallelism,
        "job_retry_count": payload.job_retry_count,
        "job_task_count": payload.job_task_count,
        "job_timeout_seconds": payload.job_timeout_seconds,
        "labels": {key: value for key, value in payload.labels},
        "launch_command": list(payload.launch_command),
        "memory_mib": payload.memory_mib,
        "project_id": payload.project_id,
        "region": payload.region,
        "repository_admission_id": payload.repository_admission_id,
        "repository_name": payload.repository_name,
        "repository_numeric_id": payload.repository_numeric_id,
        "repository_owner": payload.repository_owner,
        "request_cpu_always_allocated": payload.request_cpu_always_allocated,
        "required_files": list(payload.required_files),
        "runtime": payload.runtime,
        "runtime_service_account": payload.runtime_service_account,
        "schedule_cron": payload.schedule_cron,
        "schedule_timezone": payload.schedule_timezone,
        "secret_attachments": [
            {
                "secret_id": attachment.secret_id,
                "secret_name": attachment.secret_name,
                "secret_version": attachment.secret_version,
                "env_name": attachment.env_name,
            }
            for attachment in payload.secret_attachments
        ],
        "service_concurrency": payload.service_concurrency,
        "service_max_instances": payload.service_max_instances,
        "service_min_instances": payload.service_min_instances,
        "service_timeout_seconds": payload.service_timeout_seconds,
        "snapshot_digest": payload.snapshot_digest,
        "source_sha": payload.source_sha,
        "target": payload.target.value,
        "workload_id": payload.workload_id,
        "workload_kind": payload.workload_kind.value,
        "workload_owner_id": payload.workload_owner_id,
        "workload_version": payload.workload_version,
    }


def _validate_exact_input_types(
    *,
    workload: Workload,
    admission: RepositoryAdmission,
    snapshot: dict[str, bytes],
    context: DesiredStateRenderContext,
    issued_at: datetime,
    signing_key: bytes,
    secret_attachments: tuple[DesiredStateSecretAttachment, ...],
) -> None:
    if type(workload) is not Workload or type(admission) is not RepositoryAdmission:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if type(snapshot) is not dict:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if type(context) is not DesiredStateRenderContext:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    _require_utc_second(issued_at)
    _validate_signing_key(signing_key)
    if type(secret_attachments) is not tuple:
        raise DesiredStateDenied(_DENIED_MESSAGE)


def _validate_workload_and_admission(
    *,
    workload: Workload,
    admission: RepositoryAdmission,
) -> None:
    if admission.state is not RepositoryAdmissionState.ADMITTED:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if workload.repository_admission_id != admission.id:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if workload.source_sha != admission.admitted_sha:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if admission.owner != GITHUB_OWNER:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if _REPOSITORY_NAME_PATTERN.fullmatch(admission.name) is None:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if not _is_valid_commit_sha(admission.admitted_sha):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if not _is_valid_commit_sha(workload.source_sha):
        raise DesiredStateDenied(_DENIED_MESSAGE)


def _validate_payload_policy(
    payload: DesiredStatePayload,
    *,
    context: DesiredStateRenderContext,
) -> None:
    if type(payload) is not DesiredStatePayload:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.project_id != context.project_id or payload.region != context.region:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if not _is_valid_repository_owner(payload.repository_owner):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.repository_owner != GITHUB_OWNER:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if _REPOSITORY_NAME_PATTERN.fullmatch(payload.repository_name) is None:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if (
        not payload.repository_admission_id.strip()
        or not payload.workload_id.strip()
        or not payload.workload_owner_id.strip()
        or not payload.desired_manifest_hash.strip()
    ):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if type(payload.repository_numeric_id) is not int or isinstance(
        payload.repository_numeric_id,
        bool,
    ):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if type(payload.admission_version) is not int or isinstance(
        payload.admission_version,
        bool,
    ):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if type(payload.workload_version) is not int or isinstance(
        payload.workload_version,
        bool,
    ):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.repository_numeric_id < 1:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.admission_version < 1 or payload.workload_version < 1:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if not _is_valid_commit_sha(payload.admitted_sha):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if not _is_valid_commit_sha(payload.source_sha):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.source_sha != payload.admitted_sha:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if _SNAPSHOT_DIGEST_PATTERN.fullmatch(payload.snapshot_digest) is None:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.cpu != SERVICE_CPU or payload.memory_mib != SERVICE_MEMORY_MIB:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if (
        payload.service_min_instances != SERVICE_MIN_INSTANCES
        or payload.service_max_instances != SERVICE_MAX_INSTANCES
        or payload.service_concurrency != SERVICE_CONCURRENCY
        or payload.service_timeout_seconds != _service_timeout_seconds(
            payload.workload_kind
        )
        or payload.request_cpu_always_allocated is not SERVICE_CPU_ALWAYS_ALLOCATED
    ):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.allow_unauthenticated or payload.custom_domain:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    expected_service_account = _runtime_service_account(
        project_id=payload.project_id,
        workload_id=payload.workload_id,
    )
    if payload.runtime_service_account != expected_service_account:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    expected_labels = _labels_for_payload(payload)
    if payload.labels != expected_labels:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.image_uri != _image_uri(
        project_id=payload.project_id,
        image_digest=_extract_image_digest(payload.image_uri),
    ):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.secret_attachments != _normalized_secret_attachments(
        payload.secret_attachments
    ):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    classification = _classification_from_payload(payload)
    template = build_template_for(classification)
    if payload.runtime != template.runtime:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.install_command != template.install_command:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.build_command != template.build_command:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.launch_command != template.launch_command:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.required_files != template.required_files:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.schedule_cron != template.schedule_cron:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if payload.workload_kind is WorkloadKind.SCHEDULED_SCRIPT:
        if payload.target is not DesiredStateTarget.CLOUD_RUN_JOB:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if payload.auth_mode is not DesiredStateAuthMode.MACHINE_ONLY:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if payload.ingress is not DesiredStateIngress.NONE:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if payload.job_task_count != JOB_TASK_COUNT:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if payload.job_parallelism != JOB_PARALLELISM:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if payload.job_retry_count != JOB_RETRY_COUNT:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if payload.job_timeout_seconds != JOB_TIMEOUT_SECONDS:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if payload.schedule_timezone != TIMEZONE:
            raise DesiredStateDenied(_DENIED_MESSAGE)
    else:
        if payload.target is not DesiredStateTarget.CLOUD_RUN_SERVICE:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if payload.auth_mode is not DesiredStateAuthMode.GATEWAY_IAM:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if payload.ingress is not DesiredStateIngress.PUBLIC_IAM:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if any(
            value is not None
            for value in (
                payload.job_task_count,
                payload.job_parallelism,
                payload.job_retry_count,
                payload.job_timeout_seconds,
                payload.schedule_cron,
                payload.schedule_timezone,
            )
        ):
            raise DesiredStateDenied(_DENIED_MESSAGE)


def _classification_from_payload(payload: DesiredStatePayload):
    return WorkloadClassification(
        kind=payload.workload_kind,
        entrypoint=payload.entrypoint,
        schedule_cron=payload.schedule_cron,
    )


def _normalized_secret_attachments(
    attachments: tuple[DesiredStateSecretAttachment, ...],
) -> tuple[DesiredStateSecretAttachment, ...]:
    if type(attachments) is not tuple or len(attachments) > 5:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    validated: list[DesiredStateSecretAttachment] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_env_names: set[str] = set()
    for attachment in attachments:
        if type(attachment) is not DesiredStateSecretAttachment:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if _SECRET_ID_PATTERN.fullmatch(attachment.secret_id) is None:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if _SECRET_NAME_PATTERN.fullmatch(attachment.secret_name) is None:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if _SECRET_VERSION_PATTERN.fullmatch(attachment.secret_version) is None:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if _looks_value_like(attachment.secret_id) or _looks_value_like(
            attachment.secret_name
        ):
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if _SECRET_ENV_NAME_PATTERN.fullmatch(attachment.env_name) is None:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if attachment.secret_id in seen_ids or attachment.secret_name in seen_names:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if attachment.env_name in seen_env_names:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        seen_ids.add(attachment.secret_id)
        seen_names.add(attachment.secret_name)
        seen_env_names.add(attachment.env_name)
        validated.append(attachment)
    return tuple(
        sorted(
            validated,
            key=lambda attachment: (
                attachment.secret_id,
                attachment.secret_name,
                attachment.secret_version,
                attachment.env_name,
            ),
        )
    )


def _secret_attachment_env_name(secret_name: str) -> str:
    if (
        type(secret_name) is not str
        or _SECRET_NAME_PATTERN.fullmatch(secret_name) is None
    ):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    return f"MIM_SECRET_{secret_name.upper().replace('-', '_')}"


def _service_timeout_seconds(kind: WorkloadKind) -> int:
    if kind is WorkloadKind.STREAMLIT:
        return STREAMLIT_SERVICE_TIMEOUT_SECONDS
    if kind is WorkloadKind.NEXTJS:
        return NEXTJS_SERVICE_TIMEOUT_SECONDS
    return NEXTJS_SERVICE_TIMEOUT_SECONDS


def _image_uri(*, project_id: str, image_digest: str) -> str:
    if type(image_digest) is not str or _DIGEST_PATTERN.fullmatch(image_digest) is None:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    return (
        f"{REGION}-docker.pkg.dev/"
        f"{project_id}/{ARTIFACT_REPOSITORY}/{ARTIFACT_IMAGE_NAME}@sha256:{image_digest}"
    )


def _copied_snapshot(snapshot: dict[str, bytes]) -> dict[str, bytes]:
    copied: dict[str, bytes] = {}
    for path, content in snapshot.items():
        if type(path) is not str or type(content) is not bytes:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        copied[path] = content
    return copied


def _snapshot_digest(snapshot: dict[str, bytes]) -> str:
    hasher = hashlib.sha256()
    hasher.update(len(snapshot).to_bytes(8, "big"))
    for path in sorted(snapshot):
        path_bytes = path.encode("utf-8")
        content = snapshot[path]
        hasher.update(len(path_bytes).to_bytes(8, "big"))
        hasher.update(path_bytes)
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return f"sha256:{hasher.hexdigest()}"


def _extract_image_digest(image_uri: str) -> str:
    if type(image_uri) is not str:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    marker = "@sha256:"
    if marker not in image_uri:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    digest = image_uri.rsplit(marker, 1)[1]
    if _DIGEST_PATTERN.fullmatch(digest) is None:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    return digest


def _runtime_service_account(*, project_id: str, workload_id: str) -> str:
    try:
        service_account = runtime_identity_spec(
            project_id=project_id,
            workload_id=workload_id,
        ).email
    except ValueError:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    return service_account


def _labels_for(
    *,
    workload: Workload,
    admission: RepositoryAdmission,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            {
                "managed-by": "mim-control-plane",
                "owner-hash": _stable_hash(str(workload.owner_id)),
                "repo-hash": _stable_hash(str(admission.repository_numeric_id)),
                "workload-hash": _stable_hash(str(workload.id)),
                "workload-kind": workload.kind.value.replace("_", "-"),
            }.items()
        )
    )


def _labels_for_payload(payload: DesiredStatePayload) -> tuple[tuple[str, str], ...]:
    labels = tuple(
        sorted(
            {
                "managed-by": "mim-control-plane",
                "owner-hash": _stable_hash(payload.workload_owner_id),
                "repo-hash": _stable_hash(str(payload.repository_numeric_id)),
                "workload-hash": _stable_hash(payload.workload_id),
                "workload-kind": payload.workload_kind.value.replace("_", "-"),
            }.items()
        )
    )
    for key, value in labels:
        if _LABEL_KEY_PATTERN.fullmatch(key) is None:
            raise DesiredStateDenied(_DENIED_MESSAGE)
        if _LABEL_VALUE_PATTERN.fullmatch(value) is None:
            raise DesiredStateDenied(_DENIED_MESSAGE)
    return labels


def _is_valid_repository_owner(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?", value))


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _looks_value_like(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _VALUE_LIKE_MARKERS)


def _validate_signing_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise DesiredStateDenied(_DENIED_MESSAGE)


def _require_utc_second(value: datetime) -> None:
    if type(value) is not datetime:
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise DesiredStateDenied(_DENIED_MESSAGE)
    if value.microsecond != 0:
        raise DesiredStateDenied(_DENIED_MESSAGE)


def _require_str_tuple(value: tuple[str, ...]) -> None:
    if type(value) is not tuple:
        raise ValueError(_DENIED_MESSAGE)
    for item in value:
        if type(item) is not str:
            raise ValueError(_DENIED_MESSAGE)


def _require_exact_int(value: int) -> None:
    if type(value) is not int or isinstance(value, bool):
        raise ValueError(_DENIED_MESSAGE)


def _require_optional_exact_int(value: int | None) -> None:
    if value is None:
        return
    _require_exact_int(value)


def _isoformat_utc(value: datetime) -> str:
    _require_utc_second(value)
    return value.isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
