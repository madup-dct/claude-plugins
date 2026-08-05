"""Firestore desired-state artifact storage with immutable canonical bytes."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from mim_control_plane.config import REGION as CONFIG_REGION
from mim_control_plane.domain.models import OperationId
from mim_control_plane.domain.states import WorkloadKind
from mim_control_plane.ports.execution import (
    ArtifactConflictError,
    DesiredStateArtifactPort,
    TaskNotFoundError,
)
from mim_control_plane.services.render import (
    SCHEMA_VERSION,
    DesiredStateAuthMode,
    DesiredStateIngress,
    DesiredStatePayload,
    DesiredStateSecretAttachment,
    DesiredStateTarget,
    SignedDesiredStateEnvelope,
    canonical_unsigned_desired_state_bytes,
)

_CENTRAL_PROJECT_ID = "mim-prod-123456"
_COLLECTION = "desired_state_artifacts"
_DOCUMENT_PREFIX = b"mim:desired-state:v2\x00"
_FAILED = "Desired state artifact was denied."
_EXPECTED_FIELDS = frozenset(
    {
        "operation_id",
        "canonical_unsigned_b64",
        "canonical_unsigned_sha256",
        "envelope",
    }
)
_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "key_id",
        "audience",
        "issued_at",
        "expires_at",
        "payload",
        "signature",
    }
)


class _DocumentSnapshot(Protocol):
    exists: bool

    def to_dict(self) -> dict[str, object] | None: ...


class _DocumentReference(Protocol):
    def get(self) -> _DocumentSnapshot: ...
    def create(self, data: dict[str, object]) -> None: ...


class _Collection(Protocol):
    def document(self, document_id: str) -> _DocumentReference: ...


class FirestoreClient(Protocol):
    project: str

    def collection(self, name: str) -> _Collection: ...


@dataclass(frozen=True, slots=True)
class FirestoreDesiredStateArtifactPort(DesiredStateArtifactPort):
    client: FirestoreClient
    project_id: str
    region: str

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or self.project_id != _CENTRAL_PROJECT_ID:
            raise ValueError("desired-state project is invalid.")
        if type(self.region) is not str or self.region != CONFIG_REGION:
            raise ValueError("desired-state region is invalid.")
        if not callable(getattr(self.client, "collection", None)):
            raise ValueError("desired-state client is invalid.")
        self._require_exact_client_identity()

    def create_once(
        self,
        *,
        operation_id: OperationId,
        envelope: SignedDesiredStateEnvelope,
    ) -> SignedDesiredStateEnvelope:
        normalized_operation = _require_operation_id(operation_id)
        stored = self._record_for(operation_id=normalized_operation, envelope=envelope)
        reference = self._collection.document(_document_id(normalized_operation))
        try:
            reference.create(stored)
            return envelope
        except Exception:
            current = self._load(reference, expected_operation_id=normalized_operation)
            if current != envelope:
                raise ArtifactConflictError(_FAILED) from None
            return current

    def get(self, operation_id: OperationId) -> SignedDesiredStateEnvelope:
        normalized_operation = _require_operation_id(operation_id)
        reference = self._collection.document(_document_id(normalized_operation))
        return self._load(reference, expected_operation_id=normalized_operation)

    def _load(
        self,
        reference: _DocumentReference,
        *,
        expected_operation_id: str,
    ) -> SignedDesiredStateEnvelope:
        snapshot = reference.get()
        if not snapshot.exists:
            raise TaskNotFoundError("desired state artifact was not found.")
        raw = snapshot.to_dict()
        if not isinstance(raw, dict) or frozenset(raw) != _EXPECTED_FIELDS:
            raise ArtifactConflictError(_FAILED)
        operation_id = raw.get("operation_id")
        if operation_id != expected_operation_id:
            raise ArtifactConflictError(_FAILED)
        canonical_b64 = raw.get("canonical_unsigned_b64")
        canonical_hash = raw.get("canonical_unsigned_sha256")
        if type(canonical_b64) is not str or type(canonical_hash) is not str:
            raise ArtifactConflictError(_FAILED)
        try:
            stored_canonical = base64.b64decode(
                canonical_b64.encode("ascii"),
                validate=True,
            )
        except Exception:
            raise ArtifactConflictError(_FAILED) from None
        if hashlib.sha256(stored_canonical).hexdigest() != canonical_hash:
            raise ArtifactConflictError(_FAILED)
        envelope = _deserialize_envelope(raw.get("envelope"))
        recomputed = canonical_unsigned_desired_state_bytes(envelope)
        if stored_canonical != recomputed:
            raise ArtifactConflictError(_FAILED)
        return envelope

    def _record_for(
        self,
        *,
        operation_id: str,
        envelope: SignedDesiredStateEnvelope,
    ) -> dict[str, object]:
        canonical_unsigned = canonical_unsigned_desired_state_bytes(envelope)
        return {
            "operation_id": operation_id,
            "canonical_unsigned_b64": base64.b64encode(canonical_unsigned).decode(
                "ascii"
            ),
            "canonical_unsigned_sha256": hashlib.sha256(canonical_unsigned).hexdigest(),
            "envelope": _serialize_envelope(envelope),
        }

    @property
    def _collection(self) -> _Collection:
        self._require_exact_client_identity()
        return self.client.collection(_COLLECTION)

    def _canonical_unsigned_for_test(
        self,
        envelope: SignedDesiredStateEnvelope,
    ) -> bytes:
        return canonical_unsigned_desired_state_bytes(envelope)

    def _require_exact_client_identity(self) -> None:
        project = getattr(self.client, "project", None)
        if type(project) is not str or project != self.project_id:
            raise ValueError("desired-state client project is invalid.")
        database = getattr(self.client, "database", None)
        database_string = getattr(self.client, "database_string", None)
        if type(database) is str:
            if database != "(default)":
                raise ValueError("desired-state client database is invalid.")
            expected_database_string = (
                f"projects/{self.project_id}/databases/(default)"
            )
            if (
                database_string is not None
                and database_string != expected_database_string
            ):
                raise ValueError("desired-state client database is invalid.")
            return
        if (
            type(database_string) is not str
            or database_string != f"projects/{self.project_id}/databases/(default)"
        ):
            raise ValueError("desired-state client database is invalid.")


def _require_operation_id(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ArtifactConflictError(_FAILED)
    return value


def _document_id(operation_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(_DOCUMENT_PREFIX)
    digest.update(operation_id.encode("utf-8"))
    return digest.hexdigest()


def _serialize_envelope(envelope: SignedDesiredStateEnvelope) -> dict[str, object]:
    if (
        type(envelope) is not SignedDesiredStateEnvelope
        or envelope.schema_version != SCHEMA_VERSION
    ):
        raise ArtifactConflictError(_FAILED)
    return {
        "schema_version": envelope.schema_version,
        "key_id": envelope.key_id,
        "audience": envelope.audience,
        "issued_at": _serialize_datetime(envelope.issued_at),
        "expires_at": _serialize_datetime(envelope.expires_at),
        "payload": _serialize_payload(envelope.payload),
        "signature": envelope.signature,
    }


def _deserialize_envelope(raw: object) -> SignedDesiredStateEnvelope:
    if not isinstance(raw, dict) or frozenset(raw) != _ENVELOPE_FIELDS:
        raise ArtifactConflictError(_FAILED)
    schema_version = _require_text(raw.get("schema_version"))
    if schema_version != SCHEMA_VERSION:
        raise ArtifactConflictError(_FAILED)
    return SignedDesiredStateEnvelope(
        schema_version=schema_version,
        key_id=_require_text(raw.get("key_id")),
        audience=_require_text(raw.get("audience")),
        issued_at=_deserialize_datetime(raw.get("issued_at")),
        expires_at=_deserialize_datetime(raw.get("expires_at")),
        payload=_deserialize_payload(raw.get("payload")),
        signature=_require_text(raw.get("signature")),
    )


def _serialize_payload(payload: DesiredStatePayload) -> dict[str, object]:
    if type(payload) is not DesiredStatePayload:
        raise ArtifactConflictError(_FAILED)
    return {
        "repository_admission_id": payload.repository_admission_id,
        "repository_numeric_id": payload.repository_numeric_id,
        "repository_owner": payload.repository_owner,
        "repository_name": payload.repository_name,
        "admitted_sha": payload.admitted_sha,
        "admission_version": payload.admission_version,
        "workload_id": payload.workload_id,
        "workload_owner_id": payload.workload_owner_id,
        "workload_kind": payload.workload_kind.value,
        "workload_version": payload.workload_version,
        "source_sha": payload.source_sha,
        "desired_manifest_hash": payload.desired_manifest_hash,
        "snapshot_digest": payload.snapshot_digest,
        "project_id": payload.project_id,
        "region": payload.region,
        "target": payload.target.value,
        "image_uri": payload.image_uri,
        "runtime": payload.runtime,
        "entrypoint": payload.entrypoint,
        "install_command": list(payload.install_command),
        "build_command": list(payload.build_command),
        "launch_command": list(payload.launch_command),
        "required_files": list(payload.required_files),
        "runtime_service_account": payload.runtime_service_account,
        "cpu": payload.cpu,
        "memory_mib": payload.memory_mib,
        "service_min_instances": payload.service_min_instances,
        "service_max_instances": payload.service_max_instances,
        "request_cpu_always_allocated": payload.request_cpu_always_allocated,
        "service_concurrency": payload.service_concurrency,
        "service_timeout_seconds": payload.service_timeout_seconds,
        "job_task_count": payload.job_task_count,
        "job_parallelism": payload.job_parallelism,
        "job_retry_count": payload.job_retry_count,
        "job_timeout_seconds": payload.job_timeout_seconds,
        "auth_mode": payload.auth_mode.value,
        "ingress": payload.ingress.value,
        "allow_unauthenticated": payload.allow_unauthenticated,
        "custom_domain": payload.custom_domain,
        "labels": [[key, value] for key, value in payload.labels],
        "secret_attachments": [
            {
                "secret_id": attachment.secret_id,
                "secret_name": attachment.secret_name,
                "secret_version": attachment.secret_version,
                "env_name": attachment.env_name,
            }
            for attachment in payload.secret_attachments
        ],
        "schedule_cron": payload.schedule_cron,
        "schedule_timezone": payload.schedule_timezone,
    }


def _deserialize_payload(raw: object) -> DesiredStatePayload:
    if not isinstance(raw, dict):
        raise ArtifactConflictError(_FAILED)
    try:
        return DesiredStatePayload(
            repository_admission_id=_require_text(raw.get("repository_admission_id")),
            repository_numeric_id=_require_int(raw.get("repository_numeric_id")),
            repository_owner=_require_text(raw.get("repository_owner")),
            repository_name=_require_text(raw.get("repository_name")),
            admitted_sha=_require_text(raw.get("admitted_sha")),
            admission_version=_require_int(raw.get("admission_version")),
            workload_id=_require_text(raw.get("workload_id")),
            workload_owner_id=_require_text(raw.get("workload_owner_id")),
            workload_kind=WorkloadKind(_require_text(raw.get("workload_kind"))),
            workload_version=_require_int(raw.get("workload_version")),
            source_sha=_require_text(raw.get("source_sha")),
            desired_manifest_hash=_require_text(raw.get("desired_manifest_hash")),
            snapshot_digest=_require_text(raw.get("snapshot_digest")),
            project_id=_require_text(raw.get("project_id")),
            region=_require_text(raw.get("region")),
            target=DesiredStateTarget(_require_text(raw.get("target"))),
            image_uri=_require_text(raw.get("image_uri")),
            runtime=_require_text(raw.get("runtime")),
            entrypoint=_require_text(raw.get("entrypoint")),
            install_command=_require_str_tuple(raw.get("install_command")),
            build_command=_require_str_tuple(raw.get("build_command")),
            launch_command=_require_str_tuple(raw.get("launch_command")),
            required_files=_require_str_tuple(raw.get("required_files")),
            runtime_service_account=_require_text(raw.get("runtime_service_account")),
            cpu=_require_int(raw.get("cpu")),
            memory_mib=_require_int(raw.get("memory_mib")),
            service_min_instances=_require_int(raw.get("service_min_instances")),
            service_max_instances=_require_int(raw.get("service_max_instances")),
            request_cpu_always_allocated=_require_bool(
                raw.get("request_cpu_always_allocated")
            ),
            service_concurrency=_require_int(raw.get("service_concurrency")),
            service_timeout_seconds=_require_int(raw.get("service_timeout_seconds")),
            job_task_count=_require_optional_int(raw.get("job_task_count")),
            job_parallelism=_require_optional_int(raw.get("job_parallelism")),
            job_retry_count=_require_optional_int(raw.get("job_retry_count")),
            job_timeout_seconds=_require_optional_int(raw.get("job_timeout_seconds")),
            auth_mode=DesiredStateAuthMode(_require_text(raw.get("auth_mode"))),
            ingress=DesiredStateIngress(_require_text(raw.get("ingress"))),
            allow_unauthenticated=_require_bool(raw.get("allow_unauthenticated")),
            custom_domain=_require_bool(raw.get("custom_domain")),
            labels=_require_labels(raw.get("labels")),
            secret_attachments=_require_secret_attachments(
                raw.get("secret_attachments")
            ),
            schedule_cron=_require_optional_text(raw.get("schedule_cron")),
            schedule_timezone=_require_optional_text(raw.get("schedule_timezone")),
        )
    except Exception:
        raise ArtifactConflictError(_FAILED) from None


def _serialize_datetime(value: datetime) -> str:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ArtifactConflictError(_FAILED)
    return value.isoformat()


def _deserialize_datetime(value: object) -> datetime:
    if type(value) is not str:
        raise ArtifactConflictError(_FAILED)
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        raise ArtifactConflictError(_FAILED) from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ArtifactConflictError(_FAILED)
    return parsed


def _require_text(value: object) -> str:
    if type(value) is not str:
        raise ArtifactConflictError(_FAILED)
    return value


def _require_optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _require_text(value)


def _require_int(value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ArtifactConflictError(_FAILED)
    return value


def _require_optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _require_int(value)


def _require_bool(value: object) -> bool:
    if type(value) is not bool:
        raise ArtifactConflictError(_FAILED)
    return value


def _require_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise ArtifactConflictError(_FAILED)
    return tuple(cast(str, item) for item in value)


def _require_labels(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ArtifactConflictError(_FAILED)
    labels: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
        ):
            raise ArtifactConflictError(_FAILED)
        labels.append((item[0], item[1]))
    return tuple(labels)


def _require_secret_attachments(
    value: object,
) -> tuple[DesiredStateSecretAttachment, ...]:
    if not isinstance(value, list):
        raise ArtifactConflictError(_FAILED)
    attachments: list[DesiredStateSecretAttachment] = []
    for item in value:
        if not isinstance(item, dict):
            raise ArtifactConflictError(_FAILED)
        attachments.append(
            DesiredStateSecretAttachment(
                secret_id=_require_text(item.get("secret_id")),
                secret_name=_require_text(item.get("secret_name")),
                secret_version=_require_text(item.get("secret_version")),
                env_name=_require_text(item.get("env_name")),
            )
        )
    return tuple(attachments)
