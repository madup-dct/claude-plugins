"""Deterministic fake execution adapters for private deploy workers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from mim_control_plane.domain.models import (
    OperationId,
    SecretMetadata,
    UserId,
    WorkloadId,
)
from mim_control_plane.domain.states import SecretLifecycleState
from mim_control_plane.ports.execution import (
    ArtifactConflictError,
    ArtifactRegistryPort,
    BuildPort,
    BuildRequest,
    DeploymentQueuePort,
    DeploymentQueueReceipt,
    DesiredStateArtifactPort,
    ExecutionPlaneError,
    QueuedDeployTask,
    RuntimeIdentityPort,
    RuntimePort,
    RuntimeServiceRoute,
    SecretAttachmentReference,
    SecretMetadataDeniedError,
    SecretMetadataPort,
    TaskConflictError,
    TaskNotFoundError,
)
from mim_control_plane.ports.store import NotFound, Store
from mim_control_plane.services.render import (
    DesiredStateSecretAttachment,
    SignedDesiredStateEnvelope,
    VerifiedDesiredState,
)
from mim_control_plane.services.runtime_naming import cloud_run_service_name

_DIGEST_LENGTH = 64


@dataclass(frozen=True, slots=True)
class FakeBuildCall:
    operation_id: OperationId
    workload_id: WorkloadId
    source_sha: str
    runtime: str
    install_command: tuple[str, ...]
    build_command: tuple[str, ...]
    launch_command: tuple[str, ...]
    required_files: tuple[str, ...]
    snapshot_digest: str


class FakeDeploymentQueue(DeploymentQueuePort):
    def __init__(self) -> None:
        self._tasks_by_operation: dict[OperationId, QueuedDeployTask] = {}
        self._tasks_by_idempotency: dict[str, QueuedDeployTask] = {}

    def enqueue(self, task: QueuedDeployTask) -> DeploymentQueueReceipt:
        existing = self._tasks_by_idempotency.get(task.idempotency_key)
        if existing is not None:
            if existing.material_hash != task.material_hash:
                raise TaskConflictError("queued deploy task material changed.")
            return DeploymentQueueReceipt(task=existing, created=False)
        current = self._tasks_by_operation.get(task.operation_id)
        if current is not None:
            if current.material_hash != task.material_hash:
                raise TaskConflictError("operation already has different queued task.")
            return DeploymentQueueReceipt(task=current, created=False)
        self._tasks_by_operation[task.operation_id] = task
        self._tasks_by_idempotency[task.idempotency_key] = task
        return DeploymentQueueReceipt(task=task, created=True)

    def get(self, operation_id: OperationId) -> QueuedDeployTask:
        try:
            return self._tasks_by_operation[operation_id]
        except KeyError as exc:
            raise TaskNotFoundError("queued deploy task was not found.") from exc


class FakeBuildPort(BuildPort):
    def __init__(
        self,
        *,
        digest_override: str | None = None,
        on_build: Callable[[BuildRequest], None] | None = None,
        error: ExecutionPlaneError | None = None,
    ) -> None:
        self.digest_override = digest_override
        self.on_build = on_build
        self.error = error
        self.calls: list[FakeBuildCall] = []

    def build(self, request: BuildRequest) -> str:
        if type(request) is not BuildRequest:
            raise ExecutionPlaneError("build request must be exact.")
        if self.error is not None:
            raise self.error
        if self.on_build is not None:
            self.on_build(request)
        self.calls.append(
            FakeBuildCall(
                operation_id=request.operation_id,
                workload_id=request.workload_id,
                source_sha=request.source_sha,
                runtime=request.template.runtime,
                install_command=request.template.install_command,
                build_command=request.template.build_command,
                launch_command=request.template.launch_command,
                required_files=request.template.required_files,
                snapshot_digest=request.snapshot_digest,
            )
        )
        if self.digest_override is not None:
            return self.digest_override
        digest = hashlib.sha256()
        digest.update(str(request.operation_id).encode("utf-8"))
        digest.update(request.source_sha.encode("utf-8"))
        digest.update(request.snapshot_digest.encode("ascii"))
        digest.update(request.template.runtime.encode("utf-8"))
        for command in (
            request.template.install_command,
            request.template.build_command,
            request.template.launch_command,
            request.template.required_files,
        ):
            for token in command:
                digest.update(token.encode("utf-8"))
                digest.update(b"\0")
        return digest.hexdigest()


class FakeArtifactRegistryPort(ArtifactRegistryPort):
    def __init__(
        self,
        *,
        error: ExecutionPlaneError | None = None,
    ) -> None:
        self.error = error
        self.calls: list[str] = []

    def retain(self, image_digest: str) -> str:
        if self.error is not None:
            raise self.error
        if (
            type(image_digest) is not str
            or len(image_digest) != _DIGEST_LENGTH
            or image_digest.lower() != image_digest
            or any(char not in "0123456789abcdef" for char in image_digest)
        ):
            raise ExecutionPlaneError("image digest must be exact lowercase sha256.")
        self.calls.append(image_digest)
        return image_digest


class FakeDesiredStateArtifactPort(DesiredStateArtifactPort):
    def __init__(self, *, tamper_signature: bool = False) -> None:
        self.tamper_signature = tamper_signature
        self._envelopes: dict[OperationId, SignedDesiredStateEnvelope] = {}
        self.calls: list[OperationId] = []

    def create_once(
        self,
        *,
        operation_id: OperationId,
        envelope: SignedDesiredStateEnvelope,
    ) -> SignedDesiredStateEnvelope:
        self.calls.append(operation_id)
        stored = self._envelopes.get(operation_id)
        if stored is not None:
            if stored != envelope:
                raise ArtifactConflictError("desired state artifact already differs.")
            return stored
        persisted = envelope
        if self.tamper_signature:
            suffix = "0" if envelope.signature[-1] != "0" else "1"
            persisted = SignedDesiredStateEnvelope(
                schema_version=envelope.schema_version,
                key_id=envelope.key_id,
                audience=envelope.audience,
                issued_at=envelope.issued_at,
                expires_at=envelope.expires_at,
                payload=envelope.payload,
                signature=envelope.signature[:-1] + suffix,
            )
        self._envelopes[operation_id] = persisted
        return persisted

    def get(self, operation_id: OperationId) -> SignedDesiredStateEnvelope:
        try:
            return self._envelopes[operation_id]
        except KeyError as exc:
            raise TaskNotFoundError("desired state artifact was not found.") from exc


@dataclass(frozen=True, slots=True)
class FakeRuntimeApplyCall:
    image_uri: str
    snapshot_digest: str


@dataclass(frozen=True, slots=True)
class FakeRuntimeRollbackCall:
    workload_id: WorkloadId
    workload_owner_id: UserId
    image_digest: str


class FakeRuntimePort(RuntimePort):
    def __init__(
        self,
        *,
        healthy: bool = True,
        apply_error: ExecutionPlaneError | None = None,
        health_error: ExecutionPlaneError | None = None,
        rollback_error: ExecutionPlaneError | None = None,
    ) -> None:
        self.healthy = healthy
        self.apply_error = apply_error
        self.health_error = health_error
        self.rollback_error = rollback_error
        self.apply_calls: list[FakeRuntimeApplyCall] = []
        self.health_checks: list[str] = []
        self.rollback_calls: list[FakeRuntimeRollbackCall] = []

    def apply(self, desired_state: VerifiedDesiredState) -> None:
        if type(desired_state) is not VerifiedDesiredState:
            raise ExecutionPlaneError("runtime.apply requires verified desired state.")
        if self.apply_error is not None:
            raise self.apply_error
        self.apply_calls.append(
            FakeRuntimeApplyCall(
                image_uri=desired_state.envelope.payload.image_uri,
                snapshot_digest=desired_state.snapshot_digest,
            )
        )

    def verify_health(self, desired_state: VerifiedDesiredState) -> bool:
        if type(desired_state) is not VerifiedDesiredState:
            raise ExecutionPlaneError(
                "runtime.verify_health requires verified desired state."
            )
        if self.health_error is not None:
            raise self.health_error
        self.health_checks.append(desired_state.envelope.payload.image_uri)
        return self.healthy

    def readback_service_route(
        self,
        desired_state: VerifiedDesiredState,
    ) -> RuntimeServiceRoute | None:
        if type(desired_state) is not VerifiedDesiredState:
            raise ExecutionPlaneError(
                "runtime.readback_service_route requires verified desired state."
            )
        if not self.healthy:
            raise ExecutionPlaneError("runtime service route is unavailable.")
        if desired_state.envelope.payload.target.value != "cloud_run_service":
            return None
        resource_name = cloud_run_service_name(
            project_id=desired_state.envelope.payload.project_id,
            region=desired_state.envelope.payload.region,
            workload_id=desired_state.envelope.payload.workload_id,
        )
        return RuntimeServiceRoute(
            resource_name=resource_name,
            uri=f"https://{resource_name.rsplit('/', 1)[1]}-uc.a.run.app",
        )

    def rollback(
        self,
        *,
        workload_id: WorkloadId,
        workload_owner_id: UserId,
        image_digest: str,
    ) -> None:
        if self.rollback_error is not None:
            raise self.rollback_error
        self.rollback_calls.append(
            FakeRuntimeRollbackCall(
                workload_id=workload_id,
                workload_owner_id=workload_owner_id,
                image_digest=image_digest,
            )
        )


class FakeRuntimeIdentityPort(RuntimeIdentityPort):
    def __init__(
        self,
        *,
        email: str = "mim-wrk-test@madup-prod1.iam.gserviceaccount.com",
        error: ExecutionPlaneError | None = None,
        on_ensure: Callable[[WorkloadId], None] | None = None,
    ) -> None:
        self.email = email
        self.error = error
        self.on_ensure = on_ensure
        self.calls: list[WorkloadId] = []

    def ensure_exact(self, workload_id: WorkloadId) -> str:
        if type(workload_id) is not str or not workload_id.strip():
            raise ExecutionPlaneError("runtime identity requires exact workload id.")
        if self.error is not None:
            raise self.error
        if self.on_ensure is not None:
            self.on_ensure(workload_id)
        self.calls.append(workload_id)
        return self.email


@dataclass(frozen=True, slots=True)
class FakeSecretResolutionCall:
    workload_id: WorkloadId
    attachments: tuple[SecretAttachmentReference, ...]


class FakeSecretMetadataPort(SecretMetadataPort):
    def __init__(self, *, store: Store) -> None:
        self.store = store
        self.calls: list[FakeSecretResolutionCall] = []

    def resolve(
        self,
        *,
        workload_id: WorkloadId,
        attachments: tuple[SecretAttachmentReference, ...],
    ) -> tuple[DesiredStateSecretAttachment, ...]:
        self.calls.append(
            FakeSecretResolutionCall(
                workload_id=workload_id,
                attachments=attachments,
            )
        )
        resolved: list[DesiredStateSecretAttachment] = []
        for attachment in attachments:
            record = self._load_secret(attachment.secret_id)
            self._validate_secret(
                record, workload_id=workload_id, attachment=attachment
            )
            resolved.append(
                DesiredStateSecretAttachment(
                    secret_id=str(record.id),
                    secret_name=record.name,
                    secret_version=str(record.active_version),
                    env_name=(
                        f"MIM_SECRET_{record.name.upper().replace('-', '_')}"
                    ),
                )
            )
        return tuple(resolved)

    def _load_secret(self, secret_id: str) -> SecretMetadata:
        try:
            return self.store.get_secret_metadata(secret_id)  # type: ignore[arg-type]
        except NotFound as exc:
            raise SecretMetadataDeniedError(
                "secret metadata lookup was denied."
            ) from exc

    def _validate_secret(
        self,
        record: SecretMetadata,
        *,
        workload_id: WorkloadId,
        attachment: SecretAttachmentReference,
    ) -> None:
        if record.lifecycle_state is not SecretLifecycleState.ACTIVE:
            raise SecretMetadataDeniedError("secret metadata is not active.")
        if workload_id not in record.attached_workload_ids:
            raise SecretMetadataDeniedError("secret metadata is not attached.")
        if record.active_version != attachment.secret_version:
            raise SecretMetadataDeniedError("secret metadata version changed.")
        if record.version != attachment.metadata_version:
            raise SecretMetadataDeniedError("secret metadata record changed.")
