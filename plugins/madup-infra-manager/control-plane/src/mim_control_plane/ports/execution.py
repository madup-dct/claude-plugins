"""Private execution-plane contracts for verified deploy workers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from typing import Mapping, Protocol

from mim_control_plane.domain.models import (
    OperationId,
    RepositoryAdmission,
    RepositoryAdmissionId,
    UserId,
    WorkloadId,
)
from mim_control_plane.domain.states import RepositoryAdmissionState
from mim_control_plane.services.build_template import BuildTemplate
from mim_control_plane.services.classifier import (
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_FILES,
    MAX_SNAPSHOT_TOTAL_BYTES,
    WorkloadClassification,
)
from mim_control_plane.services.render import (
    DesiredStateSecretAttachment,
    SignedDesiredStateEnvelope,
    VerifiedDesiredState,
)


class ExecutionPlaneError(RuntimeError):
    """Base class for deterministic private execution failures."""


class RetryableExecutionPlaneError(ExecutionPlaneError):
    """Raised when a worker should surface a sanitized retryable failure."""

    def __init__(self, sanitized_failure: str) -> None:
        super().__init__("Private execution should be retried.")
        _require_text(sanitized_failure, "sanitized_failure")
        self.sanitized_failure = sanitized_failure


class TaskConflictError(ExecutionPlaneError):
    """Raised when a create-once task key is reused for different material."""


class TaskNotFoundError(ExecutionPlaneError):
    """Raised when an expected queued task cannot be loaded."""


class ArtifactConflictError(ExecutionPlaneError):
    """Raised when a create-once desired-state artifact differs."""


class SecretMetadataDeniedError(ExecutionPlaneError):
    """Raised when secret metadata attachments fail closed validation."""


_SOURCE_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _require_text(value: str, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")


def _require_positive_int(value: int, field_name: str) -> None:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be UTC-aware.")


def _normalized_snapshot(
    snapshot: Mapping[str, bytes],
) -> tuple[tuple[str, bytes], ...]:
    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot must be a mapping.")
    normalized: dict[str, bytes] = {}
    total_bytes = 0
    for path, content in snapshot.items():
        _require_text(path, "snapshot path")
        if type(content) is not bytes:
            raise ValueError("snapshot contents must be bytes.")
        if len(content) > MAX_SNAPSHOT_FILE_BYTES:
            raise ValueError("snapshot contents must respect file limits.")
        total_bytes += len(content)
        if total_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
            raise ValueError("snapshot contents must respect total size limits.")
        normalized[path] = bytes(content)
    if len(normalized) > MAX_SNAPSHOT_FILES:
        raise ValueError("snapshot contents must respect file limits.")
    return tuple(sorted(normalized.items()))


def _stable_material_hash(parts: tuple[object, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(_canonical_hash_bytes(parts))
    return digest.hexdigest()


def _canonical_hash_bytes(value: object) -> bytes:
    if type(value) is bytes:
        return _frame(b"b", value)
    if type(value) is str:
        return _frame(b"s", value.encode("utf-8"))
    if type(value) is int:
        return _frame(b"i", str(value).encode("ascii"))
    if type(value) is datetime:
        return _frame(b"t", value.isoformat().encode("utf-8"))
    if type(value) is tuple:
        payload = b"".join(_canonical_hash_bytes(item) for item in value)
        return _frame(b"l", payload)
    if is_dataclass(value):
        payload = b"".join(
            _canonical_hash_bytes((field.name, getattr(value, field.name)))
            for field in fields(value)
        )
        return _frame(type(value).__name__.encode("utf-8"), payload)
    raise TypeError("Unsupported hash material.")


def _frame(tag: bytes, payload: bytes) -> bytes:
    return tag + b":" + str(len(payload)).encode("ascii") + b":" + payload


@dataclass(frozen=True, slots=True)
class SecretAttachmentReference:
    secret_id: str
    secret_version: int
    metadata_version: int

    def __post_init__(self) -> None:
        _require_text(self.secret_id, "secret_id")
        _require_positive_int(self.secret_version, "secret_version")
        _require_positive_int(self.metadata_version, "metadata_version")


@dataclass(frozen=True, slots=True)
class SnapshotAttestation:
    digest: str
    file_count: int
    byte_count: int

    def __post_init__(self) -> None:
        if (
            type(self.digest) is not str
            or len(self.digest) != 64
            or any(character not in "0123456789abcdef" for character in self.digest)
        ):
            raise ValueError("snapshot digest must be exact.")
        _require_positive_int(self.file_count, "file_count")
        _require_positive_int(self.byte_count, "byte_count")

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, bytes]) -> SnapshotAttestation:
        snapshot_items = _normalized_snapshot(snapshot)
        return cls(
            digest=_stable_material_hash(snapshot_items),
            file_count=len(snapshot_items),
            byte_count=sum(len(content) for _, content in snapshot_items),
        )


@dataclass(frozen=True, slots=True)
class QueuedDeployTask:
    operation_id: OperationId
    expected_operation_version: int
    workload_id: WorkloadId
    expected_workload_version: int
    admission_id: RepositoryAdmissionId
    expected_admission_version: int
    expected_source_sha: str
    idempotency_key: str
    queued_at: datetime
    secret_attachments: tuple[SecretAttachmentReference, ...] = ()
    expected_snapshot_digest: str = field(default="")
    expected_snapshot_file_count: int = field(default=1)
    expected_snapshot_byte_count: int = field(default=1)

    def __post_init__(self) -> None:
        _require_text(str(self.operation_id), "operation_id")
        _require_positive_int(
            self.expected_operation_version,
            "expected_operation_version",
        )
        _require_text(str(self.workload_id), "workload_id")
        _require_positive_int(
            self.expected_workload_version,
            "expected_workload_version",
        )
        _require_text(str(self.admission_id), "admission_id")
        _require_positive_int(
            self.expected_admission_version,
            "expected_admission_version",
        )
        _require_text(self.expected_source_sha, "expected_source_sha")
        _require_text(self.idempotency_key, "idempotency_key")
        _require_utc(self.queued_at, "queued_at")
        if type(self.secret_attachments) is not tuple:
            raise ValueError("secret_attachments must be a tuple.")
        for attachment in self.secret_attachments:
            if type(attachment) is not SecretAttachmentReference:
                raise ValueError("secret_attachments must use exact attachment refs.")
        SnapshotAttestation(
            digest=self.expected_snapshot_digest,
            file_count=self.expected_snapshot_file_count,
            byte_count=self.expected_snapshot_byte_count,
        )

    @property
    def snapshot_digest(self) -> str:
        return self.expected_snapshot_digest

    @property
    def material_hash(self) -> str:
        return _stable_material_hash(
            (
                self.operation_id,
                self.expected_operation_version,
                self.workload_id,
                self.expected_workload_version,
                self.admission_id,
                self.expected_admission_version,
                self.expected_source_sha,
                self.idempotency_key,
                self.secret_attachments,
                self.expected_snapshot_digest,
                self.expected_snapshot_file_count,
                self.expected_snapshot_byte_count,
            )
        )

    def __repr__(self) -> str:
        return (
            "QueuedDeployTask("
            f"operation_id={self.operation_id!r}, "
            f"workload_id={self.workload_id!r}, "
            f"admission_id={self.admission_id!r}, "
            f"queued_at={self.queued_at.isoformat()!r}, "
            f"expected_snapshot_file_count={self.expected_snapshot_file_count!r}, "
            f"expected_snapshot_byte_count={self.expected_snapshot_byte_count!r}, "
            f"expected_snapshot_digest={self.expected_snapshot_digest!r}, "
            f"secret_attachments={self.secret_attachments!r})"
        )

    @classmethod
    def from_snapshot(
        cls,
        *,
        operation_id: OperationId,
        expected_operation_version: int,
        workload_id: WorkloadId,
        expected_workload_version: int,
        admission_id: RepositoryAdmissionId,
        expected_admission_version: int,
        expected_source_sha: str,
        idempotency_key: str,
        queued_at: datetime,
        snapshot: Mapping[str, bytes],
        secret_attachments: tuple[SecretAttachmentReference, ...] = (),
    ) -> QueuedDeployTask:
        attestation = SnapshotAttestation.from_snapshot(snapshot)
        return cls(
            operation_id=operation_id,
            expected_operation_version=expected_operation_version,
            workload_id=workload_id,
            expected_workload_version=expected_workload_version,
            admission_id=admission_id,
            expected_admission_version=expected_admission_version,
            expected_source_sha=expected_source_sha,
            idempotency_key=idempotency_key,
            queued_at=queued_at,
            secret_attachments=secret_attachments,
            expected_snapshot_digest=attestation.digest,
            expected_snapshot_file_count=attestation.file_count,
            expected_snapshot_byte_count=attestation.byte_count,
        )


@dataclass(frozen=True, slots=True)
class AdmittedBuildSource:
    admission_id: RepositoryAdmissionId
    repository_numeric_id: int
    owner: str
    name: str
    installation_id: int
    sha: str

    def __post_init__(self) -> None:
        _require_text(str(self.admission_id), "admission_id")
        _require_positive_int(self.repository_numeric_id, "repository_numeric_id")
        _require_text(self.owner, "owner")
        _require_text(self.name, "name")
        _require_positive_int(self.installation_id, "installation_id")
        if (
            type(self.sha) is not str
            or _SOURCE_SHA_PATTERN.fullmatch(self.sha) is None
            or self.sha == "0" * 40
        ):
            raise ValueError("source SHA must be exact.")

    @classmethod
    def from_admission(
        cls,
        *,
        admission: RepositoryAdmission,
        task: QueuedDeployTask,
    ) -> AdmittedBuildSource:
        if type(admission) is not RepositoryAdmission:
            raise ValueError("admission must be exact.")
        if admission.state is not RepositoryAdmissionState.ADMITTED:
            raise ValueError("admission must be active.")
        if admission.id != task.admission_id:
            raise ValueError("admission ID must match the queued task.")
        if admission.version != task.expected_admission_version:
            raise ValueError("admission version must match the queued task.")
        if admission.admitted_sha != task.expected_source_sha:
            raise ValueError("admission SHA must match the queued task.")
        return cls(
            admission_id=admission.id,
            repository_numeric_id=admission.repository_numeric_id,
            owner=admission.owner,
            name=admission.name,
            installation_id=admission.installation_id,
            sha=admission.admitted_sha,
        )


@dataclass(frozen=True, slots=True)
class BuildRequest:
    operation_id: OperationId
    workload_id: WorkloadId
    source_sha: str
    source: AdmittedBuildSource
    classification: WorkloadClassification
    template: BuildTemplate
    expected_snapshot_digest: str
    expected_snapshot_file_count: int
    expected_snapshot_byte_count: int

    def __post_init__(self) -> None:
        _require_text(str(self.operation_id), "operation_id")
        _require_text(str(self.workload_id), "workload_id")
        if (
            type(self.source_sha) is not str
            or _SOURCE_SHA_PATTERN.fullmatch(self.source_sha) is None
            or self.source_sha == "0" * 40
        ):
            raise ValueError("source_sha must be exact.")
        if type(self.source) is not AdmittedBuildSource:
            raise ValueError("source must be exact.")
        if self.source.sha != self.source_sha:
            raise ValueError("source identity SHA must match source_sha.")
        if type(self.classification) is not WorkloadClassification:
            raise ValueError("classification must be exact.")
        if type(self.template) is not BuildTemplate:
            raise ValueError("template must be exact.")
        if self.template.kind is not self.classification.kind:
            raise ValueError("template kind must match classification kind.")
        SnapshotAttestation(
            digest=self.expected_snapshot_digest,
            file_count=self.expected_snapshot_file_count,
            byte_count=self.expected_snapshot_byte_count,
        )

    @property
    def snapshot_digest(self) -> str:
        return self.expected_snapshot_digest

    def __repr__(self) -> str:
        return (
            "BuildRequest("
            f"operation_id={self.operation_id!r}, "
            f"workload_id={self.workload_id!r}, "
            f"source_sha={self.source_sha!r}, "
            f"repository_numeric_id={self.source.repository_numeric_id!r}, "
            f"admission_id={self.source.admission_id!r}, "
            f"kind={self.classification.kind.value!r}, "
            f"runtime={self.template.runtime!r}, "
            f"expected_snapshot_file_count={self.expected_snapshot_file_count!r}, "
            f"expected_snapshot_byte_count={self.expected_snapshot_byte_count!r}, "
            f"snapshot_digest={self.snapshot_digest!r})"
        )

    @classmethod
    def from_task(
        cls,
        *,
        task: QueuedDeployTask,
        admission: RepositoryAdmission,
        classification: WorkloadClassification,
        template: BuildTemplate,
    ) -> BuildRequest:
        return cls(
            operation_id=task.operation_id,
            workload_id=task.workload_id,
            source_sha=task.expected_source_sha,
            source=AdmittedBuildSource.from_admission(
                admission=admission,
                task=task,
            ),
            classification=classification,
            template=template,
            expected_snapshot_digest=task.expected_snapshot_digest,
            expected_snapshot_file_count=task.expected_snapshot_file_count,
            expected_snapshot_byte_count=task.expected_snapshot_byte_count,
        )


@dataclass(frozen=True, slots=True)
class DeploymentQueueReceipt:
    task: QueuedDeployTask
    created: bool


@dataclass(frozen=True, slots=True)
class RuntimeServiceRoute:
    resource_name: str
    uri: str

    def __post_init__(self) -> None:
        _require_text(self.resource_name, "resource_name")
        _require_text(self.uri, "uri")


@dataclass(frozen=True, slots=True)
class PrivateDeployEnqueuer:
    """Public submission surface limited to a single closed queue port."""

    queue: DeploymentQueuePort

    def enqueue(
        self,
        *,
        operation_id: OperationId,
        expected_operation_version: int,
        workload_id: WorkloadId,
        expected_workload_version: int,
        admission_id: RepositoryAdmissionId,
        expected_admission_version: int,
        expected_source_sha: str,
        idempotency_key: str,
        queued_at: datetime,
        snapshot: Mapping[str, bytes],
        secret_attachments: tuple[SecretAttachmentReference, ...] = (),
    ) -> DeploymentQueueReceipt:
        task = QueuedDeployTask.from_snapshot(
            operation_id=operation_id,
            expected_operation_version=expected_operation_version,
            workload_id=workload_id,
            expected_workload_version=expected_workload_version,
            admission_id=admission_id,
            expected_admission_version=expected_admission_version,
            expected_source_sha=expected_source_sha,
            idempotency_key=idempotency_key,
            queued_at=queued_at,
            snapshot=snapshot,
            secret_attachments=secret_attachments,
        )
        return self.queue.enqueue(task)

    def enqueue_task(self, task: QueuedDeployTask) -> DeploymentQueueReceipt:
        if type(task) is not QueuedDeployTask:
            raise TypeError("queued deploy task must be exact.")
        return self.queue.enqueue(task)


class DeploymentQueuePort(Protocol):
    def enqueue(self, task: QueuedDeployTask) -> DeploymentQueueReceipt: ...
    def get(self, operation_id: OperationId) -> QueuedDeployTask: ...


class BuildPort(Protocol):
    def build(self, request: BuildRequest) -> str: ...


class ArtifactRegistryPort(Protocol):
    def retain(self, image_digest: str) -> str: ...


class DesiredStateArtifactPort(Protocol):
    def create_once(
        self,
        *,
        operation_id: OperationId,
        envelope: SignedDesiredStateEnvelope,
    ) -> SignedDesiredStateEnvelope: ...
    def get(self, operation_id: OperationId) -> SignedDesiredStateEnvelope: ...


class RuntimeIdentityPort(Protocol):
    """Provision and verify one keyless, roleless workload identity."""

    def ensure_exact(self, workload_id: WorkloadId) -> str: ...


class RuntimePort(Protocol):
    def apply(self, desired_state: VerifiedDesiredState) -> None: ...
    def verify_health(self, desired_state: VerifiedDesiredState) -> bool: ...
    def readback_service_route(
        self,
        desired_state: VerifiedDesiredState,
    ) -> RuntimeServiceRoute | None: ...
    def rollback(
        self,
        *,
        workload_id: WorkloadId,
        workload_owner_id: UserId,
        image_digest: str,
    ) -> None: ...


class SecretMetadataPort(Protocol):
    def resolve(
        self,
        *,
        workload_id: WorkloadId,
        attachments: tuple[SecretAttachmentReference, ...],
    ) -> tuple[DesiredStateSecretAttachment, ...]: ...
