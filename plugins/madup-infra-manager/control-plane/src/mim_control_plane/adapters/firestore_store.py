"""Firestore-backed control-plane store for production deployments."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha256
from types import UnionType
from typing import (
    Any,
    Protocol,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.config import Settings
from mim_control_plane.domain.models import (
    ActivityEvent,
    AppHostnameBinding,
    AuditEvent,
    DailyUsageAggregate,
    DeploymentPlan,
    DeploymentPlanId,
    LifecycleAction,
    LifecycleActionId,
    MaintenanceJobStatus,
    Operation,
    OperationId,
    OrgCostGuard,
    OriginRequestClaim,
    RepositoryAdmission,
    RepositoryAdmissionId,
    Schedule,
    ScheduleId,
    SecretId,
    SecretMetadata,
    UsageEntry,
    User,
    UserId,
    Workload,
    WorkloadId,
)
from mim_control_plane.domain.states import (
    PlanState,
    SecretMutationState,
    UserRole,
    UserState,
)
from mim_control_plane.ports.execution import (
    QueuedDeployTask,
    SecretAttachmentReference,
    TaskConflictError,
    TaskNotFoundError,
)
from mim_control_plane.ports.store import (
    AlreadyExists,
    GitHubAutoDeployResult,
    IdempotencyConflict,
    InvariantViolation,
    NotFound,
    ReplayDetected,
    Store,
    StoreError,
    VersionConflict,
)

_SCHEMA_VERSION = 1
_DOCUMENT_ID_PREFIX = b"mim:firestore-store:v1\x00"
_USERS = "users"
_REPOSITORY_ADMISSIONS = "repository_admissions"
_WORKLOADS = "workloads"
_APP_HOSTNAME_BINDINGS = "app_hostname_bindings"
_DEPLOYMENT_PLANS = "deployment_plans"
_OPERATIONS = "operations"
_SCHEDULES = "schedules"
_SECRETS = "secrets"
_USAGE_ENTRIES = "usage_entries"
_ORG_COST_GUARDS = "org_cost_guards"
_ACTIVITY_EVENTS = "activity_events"
_AUDIT_EVENTS = "audit_events"
_DAILY_USAGE_AGGREGATES = "daily_usage_aggregates"
_LIFECYCLE_ACTIONS = "lifecycle_actions"
_MAINTENANCE_JOB_STATUSES = "maintenance_job_statuses"
_ORIGIN_REQUEST_CLAIMS = "origin_request_claims"
_GITHUB_DELIVERY_CLAIMS = "github_delivery_claims"
_OPERATION_IDEMPOTENCY = "operation_idempotency"
_DEPLOY_TASKS = "deploy_tasks"
_DEPLOY_TASK_OPERATION_INDEX = "deploy_task_operation_index"
_DEPLOY_TASK_IDEMPOTENCY_INDEX = "deploy_task_idempotency_index"
_DEPLOY_TASK_MAX_SECRET_ATTACHMENTS = 128
_DEPLOY_TASK_MAX_FILES = 128
_DEPLOY_TASK_MAX_SNAPSHOT_BYTES = 1024 * 1024


class _DocumentSnapshot(Protocol):
    id: str
    exists: bool

    def to_dict(self) -> dict[str, object] | None: ...


class _DocumentReference(Protocol):
    id: str

    def get(self, *, transaction: object | None = None) -> _DocumentSnapshot: ...

    def set(self, data: dict[str, object]) -> None: ...

    def create(self, data: dict[str, object]) -> None: ...


class _Collection(Protocol):
    def document(self, document_id: str) -> _DocumentReference: ...

    def where(self, field_name: str, op_string: str, value: object) -> _Query: ...

    def stream(self) -> Iterable[_DocumentSnapshot]: ...


class _Query(Protocol):
    def where(self, field_name: str, op_string: str, value: object) -> _Query: ...

    def order_by(self, field_name: str, direction: object = ...) -> _Query: ...

    def limit(self, count: int) -> _Query: ...

    def stream(self) -> Iterable[_DocumentSnapshot]: ...


class _FirestoreClient(Protocol):
    def collection(self, name: str) -> _Collection: ...


def _google_auth_compute_engine_credentials_factory() -> object:
    from google.auth import compute_engine

    return compute_engine.Credentials()


def _compute_metadata_credentials() -> object:
    return _google_auth_compute_engine_credentials_factory()


def _firestore_client_factory(
    *,
    project: str,
    database: str,
    credentials: object,
) -> object:
    from google.cloud import firestore_v1  # type: ignore[import-untyped]

    return firestore_v1.Client(
        project=project,
        database=database,
        credentials=credentials,
    )


def _run_firestore_transaction(
    client: object,
    operation: Callable[[object], object],
) -> object:
    from google.cloud import firestore_v1

    transaction_factory = getattr(client, "transaction")
    transaction = transaction_factory(max_attempts=5)
    return firestore_v1.transactional(operation)(transaction)


def _firestore_descending() -> object:
    try:
        from google.cloud import firestore_v1  # type: ignore[import-untyped]

        return firestore_v1.Query.DESCENDING
    except Exception:  # pragma: no cover - test fallback
        return "DESCENDING"


def _document_id(*, kind: str, logical_id: str) -> str:
    if not isinstance(kind, str) or not kind.strip():
        raise InvariantViolation("Firestore document kind is invalid.")
    if not isinstance(logical_id, str) or not logical_id.strip():
        raise InvariantViolation("Firestore logical ID is invalid.")
    digest = sha256()
    digest.update(_DOCUMENT_ID_PREFIX)
    digest.update(kind.encode("ascii"))
    digest.update(b"\x00")
    digest.update(logical_id.encode("utf-8"))
    return digest.hexdigest()


def _store_failure() -> StoreError:
    return StoreError("Firestore store operation failed.")


def _require_exact_mapping(
    value: object,
    *,
    fields: frozenset[str],
) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != fields:
        raise InvariantViolation("Firestore store data is invalid.")
    return value


def _asdict(value: object) -> dict[str, object]:
    return cast(dict[str, object], asdict(cast(Any, value)))


def _serialize_value(value: object) -> object:
    if is_dataclass(value):
        return {
            field: _serialize_value(field_value)
            for field, field_value in _asdict(value).items()
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, frozenset):
        return sorted((_serialize_value(item) for item in value), key=str)
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    return value


def _serialize_record(record: object) -> dict[str, object]:
    if not is_dataclass(record):
        raise InvariantViolation("Firestore store record is invalid.")
    payload = _asdict(record)
    payload = {key: _serialize_value(value) for key, value in payload.items()}
    payload["schema_version"] = _SCHEMA_VERSION
    return payload


def _serialize_user(user: User) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "id": str(user.id),
        "email": user.email,
        "role": user.role.value,
        "state": user.state.value,
        "groups": sorted(user.groups),
        "identity_synced_at": user.identity_synced_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "version": user.version,
    }


def _deserialize_user(snapshot: _DocumentSnapshot) -> User:
    try:
        if snapshot.exists is not True:
            raise ValueError
        data = _require_exact_mapping(
            snapshot.to_dict(),
            fields=frozenset(
                {
                    "schema_version",
                    "id",
                    "email",
                    "role",
                    "state",
                    "groups",
                    "identity_synced_at",
                    "created_at",
                    "updated_at",
                    "version",
                }
            ),
        )
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != _SCHEMA_VERSION
        ):
            raise ValueError
        groups = data["groups"]
        if not isinstance(groups, list) or any(
            not isinstance(group, str) or not group.strip() for group in groups
        ):
            raise ValueError
        return User(
            id=UserId(cast(str, data["id"])),
            email=cast(str, data["email"]),
            role=UserRole(cast(str, data["role"])),
            state=UserState(cast(str, data["state"])),
            groups=frozenset(groups),
            identity_synced_at=cast(datetime, data["identity_synced_at"]),
            created_at=cast(datetime, data["created_at"]),
            updated_at=cast(datetime, data["updated_at"]),
            version=cast(int, data["version"]),
        )
    except (KeyError, TypeError, ValueError, InvariantViolation):
        raise _store_failure() from None


RecordT = TypeVar("RecordT")


@dataclass(frozen=True, slots=True)
class _RecordSpec:
    kind: str
    collection: str
    record_type: type[Any]
    memory_attr: str
    logical_id_from_record: Callable[[object], str]
    memory_key: Callable[[object], object]
    sort_key: Callable[[object], tuple[object, ...]] | None = None
    deserializer: Callable[[_DocumentSnapshot], object] | None = None


def _snapshot_to_records(
    snapshots: Iterable[_DocumentSnapshot],
    *,
    deserialize: Callable[[_DocumentSnapshot], RecordT],
) -> tuple[RecordT, ...]:
    records = []
    for snapshot in snapshots:
        records.append(deserialize(snapshot))
    return tuple(records)


def _daily_usage_logical_id(day: date, user_id: UserId | None) -> str:
    return f"{day.isoformat()}::{user_id}"


def _decode_value(value: object, annotation: object) -> object:
    if hasattr(annotation, "__supertype__"):
        supertype = getattr(annotation, "__supertype__")
        return cast(Callable[[object], object], annotation)(
            _decode_value(value, supertype)
        )
    if annotation is datetime:
        if not isinstance(value, datetime):
            raise InvariantViolation("Firestore store data is invalid.")
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise InvariantViolation("Firestore store data is invalid.")
        return value
    if annotation is date:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if type(value) is str:
            try:
                return date.fromisoformat(value)
            except ValueError as exc:  # pragma: no cover - invalid payload branch
                raise InvariantViolation("Firestore store data is invalid.") from exc
        raise InvariantViolation("Firestore store data is invalid.")
    if annotation is str:
        if type(value) is not str:
            raise InvariantViolation("Firestore store data is invalid.")
        return value
    if annotation is int:
        if type(value) is not int:
            raise InvariantViolation("Firestore store data is invalid.")
        return value
    if annotation is bool:
        if type(value) is not bool:
            raise InvariantViolation("Firestore store data is invalid.")
        return value
    if annotation is type(None):
        if value is not None:
            raise InvariantViolation("Firestore store data is invalid.")
        return None
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        args = get_args(annotation)
        if value is None:
            if type(None) in args:
                return None
            raise InvariantViolation("Firestore store data is invalid.")
        for candidate in args:
            if candidate is type(None):
                continue
            try:
                return _decode_value(value, candidate)
            except InvariantViolation:
                continue
        raise InvariantViolation("Firestore store data is invalid.")
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise InvariantViolation("Firestore store data is invalid.")
        args = get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode_value(item, args[0]) for item in value)
        if len(args) != len(value):
            raise InvariantViolation("Firestore store data is invalid.")
        return tuple(
            _decode_value(item, item_type)
            for item, item_type in zip(value, args, strict=True)
        )
    if origin is frozenset:
        if not isinstance(value, (list, tuple)):
            raise InvariantViolation("Firestore store data is invalid.")
        (item_type,) = get_args(annotation)
        return frozenset(_decode_value(item, item_type) for item in value)
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            if type(value) is not str:
                raise InvariantViolation("Firestore store data is invalid.")
            return annotation(value)
        if not isinstance(value, annotation):
            raise InvariantViolation("Firestore store data is invalid.")
        return value
    raise InvariantViolation("Firestore store data is invalid.")


def _deserialize_record(
    snapshot: _DocumentSnapshot,
    *,
    spec: _RecordSpec,
) -> object:
    if spec.deserializer is not None:
        return spec.deserializer(snapshot)
    try:
        if snapshot.exists is not True:
            raise ValueError
        field_names = frozenset(
            {"schema_version", *(field.name for field in fields(spec.record_type))}
        )
        data = _require_exact_mapping(snapshot.to_dict(), fields=field_names)
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != _SCHEMA_VERSION
        ):
            raise ValueError
        hints = get_type_hints(spec.record_type)
        values = {
            field.name: _decode_value(data[field.name], hints[field.name])
            for field in fields(spec.record_type)
        }
        record = spec.record_type(**values)
        expected_id = _document_id(
            kind=spec.kind,
            logical_id=spec.logical_id_from_record(record),
        )
        if snapshot.id != expected_id:
            raise ValueError
        return record
    except (KeyError, TypeError, ValueError, InvariantViolation):
        raise _store_failure() from None


def _deserialize_secret_record(snapshot: _DocumentSnapshot) -> SecretMetadata:
    try:
        if snapshot.exists is not True:
            raise ValueError
        raw = snapshot.to_dict()
        if not isinstance(raw, dict):
            raise ValueError
        data = dict(raw)
        data.setdefault("retiring_version", None)
        data.setdefault("retirement_not_before", None)
        data.setdefault("mutation_state", SecretMutationState.IDLE.value)
        data.setdefault("mutation_idempotency_key", None)
        data.setdefault("pending_workload_ids", None)
        data.setdefault("pending_payload_sha256", None)
        field_names = frozenset(
            {"schema_version", *(field.name for field in fields(SecretMetadata))}
        )
        data = _require_exact_mapping(data, fields=field_names)
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != _SCHEMA_VERSION
        ):
            raise ValueError
        hints = get_type_hints(SecretMetadata)
        values = {
            field.name: _decode_value(data[field.name], hints[field.name])
            for field in fields(SecretMetadata)
        }
        record = SecretMetadata(
            id=cast(SecretId, values["id"]),
            owner_id=cast(UserId, values["owner_id"]),
            name=cast(str, values["name"]),
            integration_type=cast(str, values["integration_type"]),
            attached_workload_ids=cast(
                tuple[WorkloadId, ...],
                values["attached_workload_ids"],
            ),
            active_version=cast(int, values["active_version"]),
            rotation_state=cast(Any, values["rotation_state"]),
            lifecycle_state=cast(Any, values["lifecycle_state"]),
            created_at=cast(datetime, values["created_at"]),
            updated_at=cast(datetime, values["updated_at"]),
            retiring_version=cast(int | None, values["retiring_version"]),
            retirement_not_before=cast(
                datetime | None,
                values["retirement_not_before"],
            ),
            mutation_state=cast(Any, values["mutation_state"]),
            mutation_idempotency_key=cast(
                str | None,
                values["mutation_idempotency_key"],
            ),
            pending_workload_ids=cast(
                tuple[WorkloadId, ...] | None,
                values["pending_workload_ids"],
            ),
            pending_payload_sha256=cast(
                str | None,
                values["pending_payload_sha256"],
            ),
            version=cast(int, values["version"]),
        )
        expected_id = _document_id(kind="secret", logical_id=str(record.id))
        if snapshot.id != expected_id:
            raise ValueError
        return record
    except (KeyError, TypeError, ValueError, InvariantViolation):
        raise _store_failure() from None


def _deserialize_operation_record(snapshot: _DocumentSnapshot) -> Operation:
    try:
        if snapshot.exists is not True:
            raise ValueError
        raw = snapshot.to_dict()
        if not isinstance(raw, dict):
            raise ValueError
        data = dict(raw)
        data.setdefault("result_summary", ())
        data.setdefault("workload_owner_id", None)
        field_names = frozenset(
            {
                "schema_version",
                "workload_owner_id",
                *(field.name for field in fields(Operation)),
            }
        )
        data = _require_exact_mapping(data, fields=field_names)
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != _SCHEMA_VERSION
        ):
            raise ValueError
        if data["workload_owner_id"] is not None and type(
            data["workload_owner_id"]
        ) is not str:
            raise ValueError
        hints = get_type_hints(Operation)
        values = {
            field.name: _decode_value(data[field.name], hints[field.name])
            for field in fields(Operation)
        }
        record = Operation(
            id=cast(OperationId, values["id"]),
            actor_id=cast(UserId, values["actor_id"]),
            workload_id=cast(WorkloadId | None, values["workload_id"]),
            action=cast(str, values["action"]),
            idempotency_key=cast(str, values["idempotency_key"]),
            request_hash=cast(str, values["request_hash"]),
            state=cast(Any, values["state"]),
            created_at=cast(datetime, values["created_at"]),
            updated_at=cast(datetime, values["updated_at"]),
            sanitized_failure=cast(str | None, values["sanitized_failure"]),
            result_summary=cast(tuple[tuple[str, str], ...], values["result_summary"]),
            version=cast(int, values["version"]),
        )
        expected_id = _document_id(kind="operation", logical_id=str(record.id))
        if snapshot.id != expected_id:
            raise ValueError
        return record
    except (KeyError, TypeError, ValueError, InvariantViolation):
        raise _store_failure() from None


_REPOSITORY_ADMISSION_SPEC = _RecordSpec(
    kind="repository_admission",
    collection=_REPOSITORY_ADMISSIONS,
    record_type=RepositoryAdmission,
    memory_attr="_repository_admissions",
    logical_id_from_record=lambda record: str(cast(RepositoryAdmission, record).id),
    memory_key=lambda record: cast(RepositoryAdmission, record).id,
)
_WORKLOAD_SPEC = _RecordSpec(
    kind="workload",
    collection=_WORKLOADS,
    record_type=Workload,
    memory_attr="_workloads",
    logical_id_from_record=lambda record: str(cast(Workload, record).id),
    memory_key=lambda record: cast(Workload, record).id,
    sort_key=lambda record: (
        cast(Workload, record).created_at,
        str(cast(Workload, record).id),
    ),
)
_APP_HOSTNAME_BINDING_SPEC = _RecordSpec(
    kind="app_hostname_binding",
    collection=_APP_HOSTNAME_BINDINGS,
    record_type=AppHostnameBinding,
    memory_attr="_app_hostname_bindings",
    logical_id_from_record=lambda record: cast(AppHostnameBinding, record).public_host,
    memory_key=lambda record: cast(AppHostnameBinding, record).public_host,
)
_DEPLOYMENT_PLAN_SPEC = _RecordSpec(
    kind="deployment_plan",
    collection=_DEPLOYMENT_PLANS,
    record_type=DeploymentPlan,
    memory_attr="_deployment_plans",
    logical_id_from_record=lambda record: str(cast(DeploymentPlan, record).id),
    memory_key=lambda record: cast(DeploymentPlan, record).id,
)
_OPERATION_SPEC = _RecordSpec(
    kind="operation",
    collection=_OPERATIONS,
    record_type=Operation,
    memory_attr="_operations",
    logical_id_from_record=lambda record: str(cast(Operation, record).id),
    memory_key=lambda record: cast(Operation, record).id,
    deserializer=_deserialize_operation_record,
)
_SCHEDULE_SPEC = _RecordSpec(
    kind="schedule",
    collection=_SCHEDULES,
    record_type=Schedule,
    memory_attr="_schedules",
    logical_id_from_record=lambda record: str(cast(Schedule, record).id),
    memory_key=lambda record: cast(Schedule, record).id,
    sort_key=lambda record: (
        cast(Schedule, record).created_at,
        str(cast(Schedule, record).id),
    ),
)
_SECRET_SPEC = _RecordSpec(
    kind="secret",
    collection=_SECRETS,
    record_type=SecretMetadata,
    memory_attr="_secrets",
    logical_id_from_record=lambda record: str(cast(SecretMetadata, record).id),
    memory_key=lambda record: cast(SecretMetadata, record).id,
    sort_key=lambda record: (
        cast(SecretMetadata, record).created_at,
        str(cast(SecretMetadata, record).id),
    ),
    deserializer=_deserialize_secret_record,
)
_USAGE_SPEC = _RecordSpec(
    kind="usage_entry",
    collection=_USAGE_ENTRIES,
    record_type=UsageEntry,
    memory_attr="_usage_entries",
    logical_id_from_record=lambda record: str(cast(UsageEntry, record).id),
    memory_key=lambda record: cast(UsageEntry, record).id,
    sort_key=lambda record: (
        cast(UsageEntry, record).collected_at,
        str(cast(UsageEntry, record).id),
    ),
)
_ORG_COST_GUARD_SPEC = _RecordSpec(
    kind="org_cost_guard",
    collection=_ORG_COST_GUARDS,
    record_type=OrgCostGuard,
    memory_attr="_org_cost_guards",
    logical_id_from_record=lambda _record: "organization",
    memory_key=lambda _record: "organization",
)
_ACTIVITY_SPEC = _RecordSpec(
    kind="activity_event",
    collection=_ACTIVITY_EVENTS,
    record_type=ActivityEvent,
    memory_attr="_activity_events",
    logical_id_from_record=lambda record: str(cast(ActivityEvent, record).id),
    memory_key=lambda record: cast(ActivityEvent, record).id,
    sort_key=lambda record: (
        cast(ActivityEvent, record).occurred_at,
        str(cast(ActivityEvent, record).id),
    ),
)
_AUDIT_SPEC = _RecordSpec(
    kind="audit_event",
    collection=_AUDIT_EVENTS,
    record_type=AuditEvent,
    memory_attr="_audit_events",
    logical_id_from_record=lambda record: str(cast(AuditEvent, record).id),
    memory_key=lambda record: cast(AuditEvent, record).id,
    sort_key=lambda record: (
        cast(AuditEvent, record).occurred_at,
        str(cast(AuditEvent, record).id),
    ),
)
_DAILY_AGGREGATE_SPEC = _RecordSpec(
    kind="daily_usage_aggregate",
    collection=_DAILY_USAGE_AGGREGATES,
    record_type=DailyUsageAggregate,
    memory_attr="_daily_aggregates",
    logical_id_from_record=lambda record: _daily_usage_logical_id(
        cast(DailyUsageAggregate, record).day,
        cast(DailyUsageAggregate, record).user_id,
    ),
    memory_key=lambda record: (
        cast(DailyUsageAggregate, record).day,
        cast(DailyUsageAggregate, record).user_id,
    ),
)
_LIFECYCLE_SPEC = _RecordSpec(
    kind="lifecycle_action",
    collection=_LIFECYCLE_ACTIONS,
    record_type=LifecycleAction,
    memory_attr="_lifecycle_actions",
    logical_id_from_record=lambda record: str(cast(LifecycleAction, record).id),
    memory_key=lambda record: cast(LifecycleAction, record).id,
)
_MAINTENANCE_STATUS_SPEC = _RecordSpec(
    kind="maintenance_job_status",
    collection=_MAINTENANCE_JOB_STATUSES,
    record_type=MaintenanceJobStatus,
    memory_attr="_maintenance_job_statuses",
    logical_id_from_record=lambda record: cast(MaintenanceJobStatus, record).job_name,
    memory_key=lambda record: cast(MaintenanceJobStatus, record).job_name,
    sort_key=lambda record: (cast(MaintenanceJobStatus, record).job_name,),
)
_USER_SPEC = _RecordSpec(
    kind="user",
    collection=_USERS,
    record_type=User,
    memory_attr="_users",
    logical_id_from_record=lambda record: str(cast(User, record).id),
    memory_key=lambda record: cast(User, record).id,
    sort_key=lambda record: (
        cast(User, record).created_at,
        str(cast(User, record).id),
    ),
)


def _operation_claim_logical_id(actor_id: UserId, idempotency_key: str) -> str:
    return f"{actor_id}\x00{idempotency_key}"


def _serialize_operation_claim(
    *,
    operation: Operation,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "actor_id": str(operation.actor_id),
        "idempotency_key": operation.idempotency_key,
        "request_hash": operation.request_hash,
        "action": operation.action,
        "workload_id": (
            None if operation.workload_id is None else str(operation.workload_id)
        ),
        "operation_id": str(operation.id),
    }


def _deserialize_operation_claim(snapshot: _DocumentSnapshot) -> dict[str, object]:
    try:
        if snapshot.exists is not True:
            raise ValueError
        data = _require_exact_mapping(
            snapshot.to_dict(),
            fields=frozenset(
                {
                    "schema_version",
                    "actor_id",
                    "idempotency_key",
                    "request_hash",
                    "action",
                    "workload_id",
                    "operation_id",
                }
            ),
        )
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != _SCHEMA_VERSION
        ):
            raise ValueError
        return data
    except (KeyError, TypeError, ValueError, InvariantViolation):
        raise _store_failure() from None


def _serialize_github_delivery_claim(
    *,
    delivery_id: str,
    delivery_hash: str,
    source_ref: str,
    admission: RepositoryAdmission,
    workload: Workload,
    plan: DeploymentPlan,
    operation: Operation,
    task: QueuedDeployTask,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "delivery_hash": delivery_hash,
        "source_ref": source_ref,
        "admission_id": str(admission.id),
        "workload_id": str(workload.id),
        "plan_id": str(plan.id),
        "operation_id": str(operation.id),
        "task_material_hash": task.material_hash,
    }


def _deserialize_github_delivery_claim(
    snapshot: _DocumentSnapshot,
) -> dict[str, str]:
    try:
        fields = frozenset(
            {
                "schema_version",
                "delivery_id",
                "delivery_hash",
                "source_ref",
                "admission_id",
                "workload_id",
                "plan_id",
                "operation_id",
                "task_material_hash",
            }
        )
        data = _require_exact_mapping(snapshot.to_dict(), fields=fields)
        if (
            snapshot.exists is not True
            or type(data["schema_version"]) is not int
            or data["schema_version"] != _SCHEMA_VERSION
        ):
            raise ValueError
        result: dict[str, str] = {}
        for field_name in fields - {"schema_version"}:
            value = data[field_name]
            if type(value) is not str or not value:
                raise ValueError
            result[field_name] = value
        return result
    except (KeyError, TypeError, ValueError, InvariantViolation):
        raise _store_failure() from None


def _github_claim_matches(
    claim: dict[str, str],
    *,
    delivery_id: str,
    delivery_hash: str,
    source_ref: str,
    admission: RepositoryAdmission,
    workload: Workload,
    plan: DeploymentPlan,
    operation: Operation,
    task: QueuedDeployTask,
) -> bool:
    return claim == {
        "delivery_id": delivery_id,
        "delivery_hash": delivery_hash,
        "source_ref": source_ref,
        "admission_id": str(admission.id),
        "workload_id": str(workload.id),
        "plan_id": str(plan.id),
        "operation_id": str(operation.id),
        "task_material_hash": task.material_hash,
    }


def _validate_deploy_task_limits(task: QueuedDeployTask) -> None:
    if len(task.secret_attachments) > _DEPLOY_TASK_MAX_SECRET_ATTACHMENTS:
        raise ValueError("queued deploy task is too large.")
    if task.expected_snapshot_file_count > _DEPLOY_TASK_MAX_FILES:
        raise ValueError("queued deploy task is too large.")
    if task.expected_snapshot_byte_count > _DEPLOY_TASK_MAX_SNAPSHOT_BYTES:
        raise ValueError("queued deploy task is too large.")


def _serialize_deploy_task_metadata(task: QueuedDeployTask) -> dict[str, object]:
    _validate_deploy_task_limits(task)
    return {
        "schema_version": _SCHEMA_VERSION,
        "operation_id": str(task.operation_id),
        "expected_operation_version": task.expected_operation_version,
        "workload_id": str(task.workload_id),
        "expected_workload_version": task.expected_workload_version,
        "admission_id": str(task.admission_id),
        "expected_admission_version": task.expected_admission_version,
        "expected_source_sha": task.expected_source_sha,
        "idempotency_key": task.idempotency_key,
        "queued_at": task.queued_at.isoformat(),
        "secret_attachments": [
            {
                "secret_id": item.secret_id,
                "secret_version": item.secret_version,
                "metadata_version": item.metadata_version,
            }
            for item in task.secret_attachments
        ],
        "expected_snapshot_digest": task.expected_snapshot_digest,
        "expected_snapshot_file_count": task.expected_snapshot_file_count,
        "expected_snapshot_byte_count": task.expected_snapshot_byte_count,
        "material_hash": task.material_hash,
    }


def _deserialize_deploy_task_metadata(snapshot: _DocumentSnapshot) -> QueuedDeployTask:
    try:
        exact_fields = frozenset(
            {
                "schema_version",
                "operation_id",
                "expected_operation_version",
                "workload_id",
                "expected_workload_version",
                "admission_id",
                "expected_admission_version",
                "expected_source_sha",
                "idempotency_key",
                "queued_at",
                "secret_attachments",
                "expected_snapshot_digest",
                "expected_snapshot_file_count",
                "expected_snapshot_byte_count",
                "material_hash",
            }
        )
        if snapshot.exists is not True:
            raise ValueError
        data = _require_exact_mapping(snapshot.to_dict(), fields=exact_fields)
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != _SCHEMA_VERSION
            or type(data["operation_id"]) is not str
            or type(data["expected_operation_version"]) is not int
            or type(data["workload_id"]) is not str
            or type(data["expected_workload_version"]) is not int
            or type(data["admission_id"]) is not str
            or type(data["expected_admission_version"]) is not int
            or type(data["expected_source_sha"]) is not str
            or type(data["idempotency_key"]) is not str
            or type(data["queued_at"]) is not str
            or not isinstance(data["secret_attachments"], list)
            or type(data["expected_snapshot_digest"]) is not str
            or type(data["expected_snapshot_file_count"]) is not int
            or type(data["expected_snapshot_byte_count"]) is not int
            or type(data["material_hash"]) is not str
        ):
            raise ValueError
        if (
            len(cast(list[object], data["secret_attachments"]))
            > _DEPLOY_TASK_MAX_SECRET_ATTACHMENTS
        ):
            raise ValueError
        attachments = tuple(
            SecretAttachmentReference(
                secret_id=cast(str, _exact_attachment_mapping(item)["secret_id"]),
                secret_version=cast(
                    int,
                    _exact_attachment_mapping(item)["secret_version"],
                ),
                metadata_version=cast(
                    int,
                    _exact_attachment_mapping(item)["metadata_version"],
                ),
            )
            for item in cast(list[object], data["secret_attachments"])
        )
        queued_at = datetime.fromisoformat(cast(str, data["queued_at"]))
        if queued_at.tzinfo is None or queued_at.utcoffset() != UTC.utcoffset(
            queued_at
        ):
            raise ValueError
        task = QueuedDeployTask(
            operation_id=OperationId(cast(str, data["operation_id"])),
            expected_operation_version=cast(int, data["expected_operation_version"]),
            workload_id=WorkloadId(cast(str, data["workload_id"])),
            expected_workload_version=cast(int, data["expected_workload_version"]),
            admission_id=RepositoryAdmissionId(cast(str, data["admission_id"])),
            expected_admission_version=cast(int, data["expected_admission_version"]),
            expected_source_sha=cast(str, data["expected_source_sha"]),
            idempotency_key=cast(str, data["idempotency_key"]),
            queued_at=queued_at,
            secret_attachments=attachments,
            expected_snapshot_digest=cast(str, data["expected_snapshot_digest"]),
            expected_snapshot_file_count=cast(
                int, data["expected_snapshot_file_count"]
            ),
            expected_snapshot_byte_count=cast(
                int, data["expected_snapshot_byte_count"]
            ),
        )
        if task.material_hash != cast(str, data["material_hash"]):
            raise ValueError
        return task
    except Exception as exc:
        raise _store_failure() from exc


def _exact_attachment_mapping(value: object) -> dict[str, object]:
    mapping = _require_exact_mapping(
        value,
        fields=frozenset({"secret_id", "secret_version", "metadata_version"}),
    )
    if (
        type(mapping["secret_id"]) is not str
        or type(mapping["secret_version"]) is not int
        or type(mapping["metadata_version"]) is not int
    ):
        raise ValueError
    return mapping
def _serialize_deploy_task_index(*, material_hash: str) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "material_hash": material_hash,
    }


def _deserialize_deploy_task_index(snapshot: _DocumentSnapshot) -> str:
    try:
        if snapshot.exists is not True:
            raise ValueError
        data = _require_exact_mapping(
            snapshot.to_dict(),
            fields=frozenset({"schema_version", "material_hash"}),
        )
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != _SCHEMA_VERSION
            or type(data["material_hash"]) is not str
        ):
            raise ValueError
        return cast(str, data["material_hash"])
    except (KeyError, TypeError, ValueError, InvariantViolation):
        raise _store_failure() from None
class FirestoreStore(Store):
    def __init__(
        self,
        *,
        settings: Settings,
        credentials_loader: Callable[[], object] | None = None,
        client_factory: Callable[..., object] | None = None,
        transaction_runner: Callable[[object, Callable[[object], object]], object]
        | None = None,
    ) -> None:
        credentials = (credentials_loader or _compute_metadata_credentials)()
        factory = client_factory or _firestore_client_factory
        self._client: _FirestoreClient = cast(
            _FirestoreClient,
            factory(
                project=settings.project_id,
                database=settings.firestore_database_id,
                credentials=credentials,
            ),
        )
        self._transaction_runner = transaction_runner or _run_firestore_transaction
        self._memory = MemoryStore()

    def create_user(self, user: User) -> User:
        return cast(
            User,
            self._create_plain(_USER_SPEC, user, MemoryStore.create_user),
        )

    def get_user(self, user_id: UserId) -> User:
        return cast(User, self._get_plain(_USER_SPEC, str(user_id), "user"))

    def save_user(self, user: User, *, expected_version: int) -> User:
        return cast(
            User,
            self._save_plain(
                _USER_SPEC,
                user,
                expected_version=expected_version,
                save_method=MemoryStore.save_user,
                not_found_label="user",
            ),
        )

    def list_users(self) -> tuple[User, ...]:
        return cast(tuple[User, ...], self._list_plain(_USER_SPEC))

    def create_repository_admission(
        self, admission: RepositoryAdmission
    ) -> RepositoryAdmission:
        return cast(
            RepositoryAdmission,
            self._create_plain(
                _REPOSITORY_ADMISSION_SPEC,
                admission,
                MemoryStore.create_repository_admission,
            ),
        )

    def get_repository_admission(
        self, admission_id: RepositoryAdmissionId
    ) -> RepositoryAdmission:
        return cast(
            RepositoryAdmission,
            self._get_plain(
                _REPOSITORY_ADMISSION_SPEC,
                str(admission_id),
                "repository admission",
            ),
        )

    def save_repository_admission(
        self, admission: RepositoryAdmission, *, expected_version: int
    ) -> RepositoryAdmission:
        return cast(
            RepositoryAdmission,
            self._save_plain(
                _REPOSITORY_ADMISSION_SPEC,
                admission,
                expected_version=expected_version,
                save_method=MemoryStore.save_repository_admission,
                not_found_label="repository admission",
            ),
        )

    def create_workload(self, workload: Workload) -> Workload:
        return cast(
            Workload,
            self._create_plain(
                _WORKLOAD_SPEC,
                workload,
                MemoryStore.create_workload,
            ),
        )

    def get_workload(self, workload_id: WorkloadId) -> Workload:
        return cast(
            Workload,
            self._get_plain(_WORKLOAD_SPEC, str(workload_id), "workload"),
        )

    def save_workload(self, workload: Workload, *, expected_version: int) -> Workload:
        return cast(
            Workload,
            self._save_plain(
                _WORKLOAD_SPEC,
                workload,
                expected_version=expected_version,
                save_method=MemoryStore.save_workload,
                not_found_label="workload",
            ),
        )

    def list_workloads(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[Workload, ...]:
        if owner_id is None:
            return cast(
                tuple[Workload, ...],
                self._list_plain(_WORKLOAD_SPEC),
            )
        try:
            query = self._collection(_WORKLOAD_SPEC.collection).where(
                "owner_id",
                "==",
                str(owner_id),
            )
            workloads = _snapshot_to_records(
                query.stream(),
                deserialize=lambda snapshot: cast(
                    Workload,
                    _deserialize_record(snapshot, spec=_WORKLOAD_SPEC),
                ),
            )
            if _WORKLOAD_SPEC.sort_key is None:
                return cast(tuple[Workload, ...], workloads)
            return tuple(sorted(workloads, key=_WORKLOAD_SPEC.sort_key))
        except StoreError:
            raise
        except Exception as exc:  # pragma: no cover - defensive adapter guard
            raise _store_failure() from exc

    def create_app_hostname_binding(
        self,
        binding: AppHostnameBinding,
    ) -> AppHostnameBinding:
        return cast(
            AppHostnameBinding,
            self._create_plain(
                _APP_HOSTNAME_BINDING_SPEC,
                binding,
                MemoryStore.create_app_hostname_binding,
            ),
        )

    def get_app_hostname_binding(self, public_host: str) -> AppHostnameBinding:
        return cast(
            AppHostnameBinding,
            self._get_plain(
                _APP_HOSTNAME_BINDING_SPEC,
                public_host,
                "app hostname binding",
            ),
        )

    def save_app_hostname_binding(
        self,
        binding: AppHostnameBinding,
        *,
        expected_version: int,
    ) -> AppHostnameBinding:
        return cast(
            AppHostnameBinding,
            self._save_plain(
                _APP_HOSTNAME_BINDING_SPEC,
                binding,
                expected_version=expected_version,
                save_method=MemoryStore.save_app_hostname_binding,
                not_found_label="app hostname binding",
            ),
        )

    def create_deployment_plan(self, plan: DeploymentPlan) -> DeploymentPlan:
        return cast(
            DeploymentPlan,
            self._create_plain(
                _DEPLOYMENT_PLAN_SPEC,
                plan,
                MemoryStore.create_deployment_plan,
            ),
        )

    def get_deployment_plan(self, plan_id: DeploymentPlanId) -> DeploymentPlan:
        return cast(
            DeploymentPlan,
            self._get_plain(
                _DEPLOYMENT_PLAN_SPEC,
                str(plan_id),
                "deployment plan",
            ),
        )

    def save_deployment_plan(
        self, plan: DeploymentPlan, *, expected_version: int
    ) -> DeploymentPlan:
        return cast(
            DeploymentPlan,
            self._save_plain(
                _DEPLOYMENT_PLAN_SPEC,
                plan,
                expected_version=expected_version,
                save_method=MemoryStore.save_deployment_plan,
                not_found_label="deployment plan",
            ),
        )

    def consume_deployment_plan_with_operation(
        self,
        *,
        plan_id: DeploymentPlanId,
        actor_id: UserId,
        expected_material_hash: str,
        expected_action: str,
        policy_version: str,
        consumed_at: datetime,
        operation: Operation,
    ) -> tuple[DeploymentPlan, Operation]:
        def operation_body(_transaction: object) -> tuple[DeploymentPlan, Operation]:
            transaction = cast(Any, _transaction)
            store = MemoryStore()
            current_plan = cast(
                DeploymentPlan,
                self._get_plain_snapshot(
                    _DEPLOYMENT_PLAN_SPEC,
                    str(plan_id),
                    "deployment plan",
                    transaction=transaction,
                ),
            )
            store._deployment_plans[current_plan.id] = current_plan
            claim_ref = self._document(
                _OPERATION_IDEMPOTENCY,
                _document_id(
                    kind="operation_idempotency",
                    logical_id=_operation_claim_logical_id(
                        operation.actor_id,
                        operation.idempotency_key,
                    ),
                ),
            )
            claim_snapshot = claim_ref.get(transaction=transaction)
            if claim_snapshot.exists:
                claim = _deserialize_operation_claim(claim_snapshot)
                claim_key = (
                    UserId(cast(str, claim["actor_id"])),
                    cast(str, claim["idempotency_key"]),
                )
                claim_workload = claim["workload_id"]
                store._operation_idempotency[claim_key] = (
                    cast(str, claim["request_hash"]),
                    cast(str, claim["action"]),
                    None
                    if claim_workload is None
                    else WorkloadId(cast(str, claim_workload)),
                    OperationId(cast(str, claim["operation_id"])),
                )
                existing_operation = cast(
                    Operation,
                    self._get_plain_snapshot(
                        _OPERATION_SPEC,
                        cast(str, claim["operation_id"]),
                        "operation",
                        transaction=transaction,
                    ),
                )
                store._operations[existing_operation.id] = existing_operation
            consumed_plan, created_operation = (
                store.consume_deployment_plan_with_operation(
                    plan_id=plan_id,
                    actor_id=actor_id,
                    expected_material_hash=expected_material_hash,
                    expected_action=expected_action,
                    policy_version=policy_version,
                    consumed_at=consumed_at,
                    operation=operation,
                )
            )
            operation_payload = self._serialize_operation_for_write(
                created_operation,
                transaction=transaction,
            )
            transaction.set(
                self._document(
                    _DEPLOYMENT_PLANS,
                    _document_id(
                        kind="deployment_plan",
                        logical_id=str(consumed_plan.id),
                    ),
                ),
                _serialize_record(consumed_plan),
            )
            transaction.set(
                self._document(
                    _OPERATIONS,
                    _document_id(
                        kind="operation",
                        logical_id=str(created_operation.id),
                    ),
                ),
                operation_payload,
            )
            if not claim_snapshot.exists:
                transaction.create(
                    claim_ref, _serialize_operation_claim(operation=created_operation)
                )
            return consumed_plan, created_operation

        def recover() -> tuple[DeploymentPlan, Operation] | None:
            try:
                recovered_plan = self.get_deployment_plan(plan_id)
                recovered_operation = self.get_operation(operation.id)
            except (NotFound, StoreError):
                return None
            if (
                recovered_plan.state is PlanState.CONSUMED
                and recovered_operation == operation
            ):
                return recovered_plan, recovered_operation
            return None

        return self._run_atomic(operation_body, recover)

    def apply_github_auto_deploy_once(
        self,
        *,
        delivery_id: str,
        delivery_hash: str,
        source_ref: str,
        expected_workload_version: int,
        admission: RepositoryAdmission,
        workload: Workload,
        plan: DeploymentPlan,
        operation: Operation,
        task: QueuedDeployTask,
        consumed_at: datetime,
    ) -> GitHubAutoDeployResult:
        task_material_hash = task.material_hash
        delivery_ref = self._document(
            _GITHUB_DELIVERY_CLAIMS,
            _document_id(kind="github_delivery_claim", logical_id=delivery_id),
        )

        def operation_body(_transaction: object) -> GitHubAutoDeployResult:
            transaction = cast(Any, _transaction)
            delivery_snapshot = delivery_ref.get(transaction=transaction)
            if delivery_snapshot.exists:
                claim = _deserialize_github_delivery_claim(delivery_snapshot)
                if not _github_claim_matches(
                    claim,
                    delivery_id=delivery_id,
                    delivery_hash=delivery_hash,
                    source_ref=source_ref,
                    admission=admission,
                    workload=workload,
                    plan=plan,
                    operation=operation,
                    task=task,
                ):
                    raise ReplayDetected("GitHub delivery material changed.")
                persisted_admission = cast(
                    RepositoryAdmission,
                    self._get_plain_snapshot(
                        _REPOSITORY_ADMISSION_SPEC,
                        str(admission.id),
                        "repository admission",
                        transaction=transaction,
                    ),
                )
                persisted_workload = cast(
                    Workload,
                    self._get_plain_snapshot(
                        _WORKLOAD_SPEC,
                        str(workload.id),
                        "workload",
                        transaction=transaction,
                    ),
                )
                persisted_plan = cast(
                    DeploymentPlan,
                    self._get_plain_snapshot(
                        _DEPLOYMENT_PLAN_SPEC,
                        str(plan.id),
                        "deployment plan",
                        transaction=transaction,
                    ),
                )
                persisted_operation = cast(
                    Operation,
                    self._get_plain_snapshot(
                        _OPERATION_SPEC,
                        str(operation.id),
                        "operation",
                        transaction=transaction,
                    ),
                )
                persisted_task = self._get_deploy_task_by_material_hash(
                    task_material_hash,
                    transaction=transaction,
                )
                operation_index = self._document(
                    _DEPLOY_TASK_OPERATION_INDEX,
                    _document_id(
                        kind="deploy_task_operation_index",
                        logical_id=str(operation.id),
                    ),
                ).get(transaction=transaction)
                idempotency_index = self._document(
                    _DEPLOY_TASK_IDEMPOTENCY_INDEX,
                    _document_id(
                        kind="deploy_task_idempotency_index",
                        logical_id=task.idempotency_key,
                    ),
                ).get(transaction=transaction)
                if (
                    _deserialize_deploy_task_index(operation_index)
                    != task_material_hash
                    or _deserialize_deploy_task_index(idempotency_index)
                    != task_material_hash
                    or persisted_task.material_hash != task_material_hash
                ):
                    raise InvariantViolation("GitHub delivery outcome is incomplete.")
                return GitHubAutoDeployResult(
                    admission=persisted_admission,
                    workload=persisted_workload,
                    plan=persisted_plan,
                    operation=persisted_operation,
                    task=persisted_task,
                    replayed=True,
                )

            current_workload = cast(
                Workload,
                self._get_plain_snapshot(
                    _WORKLOAD_SPEC,
                    str(workload.id),
                    "workload",
                    transaction=transaction,
                ),
            )
            owner = cast(
                User,
                self._get_plain_snapshot(
                    _USER_SPEC,
                    str(current_workload.owner_id),
                    "user",
                    transaction=transaction,
                ),
            )
            current_admission = cast(
                RepositoryAdmission,
                self._get_plain_snapshot(
                    _REPOSITORY_ADMISSION_SPEC,
                    str(current_workload.repository_admission_id),
                    "repository admission",
                    transaction=transaction,
                ),
            )
            new_admission_ref = self._document(
                _REPOSITORY_ADMISSIONS,
                _document_id(
                    kind="repository_admission",
                    logical_id=str(admission.id),
                ),
            )
            plan_ref = self._document(
                _DEPLOYMENT_PLANS,
                _document_id(kind="deployment_plan", logical_id=str(plan.id)),
            )
            operation_ref = self._document(
                _OPERATIONS,
                _document_id(kind="operation", logical_id=str(operation.id)),
            )
            operation_claim_ref = self._document(
                _OPERATION_IDEMPOTENCY,
                _document_id(
                    kind="operation_idempotency",
                    logical_id=_operation_claim_logical_id(
                        operation.actor_id,
                        operation.idempotency_key,
                    ),
                ),
            )
            task_metadata_ref = self._document(
                _DEPLOY_TASKS,
                _document_id(
                    kind="deploy_task",
                    logical_id=task_material_hash,
                ),
            )
            task_operation_index_ref = self._document(
                _DEPLOY_TASK_OPERATION_INDEX,
                _document_id(
                    kind="deploy_task_operation_index",
                    logical_id=str(operation.id),
                ),
            )
            task_idempotency_index_ref = self._document(
                _DEPLOY_TASK_IDEMPOTENCY_INDEX,
                _document_id(
                    kind="deploy_task_idempotency_index",
                    logical_id=task.idempotency_key,
                ),
            )
            must_not_exist = (
                new_admission_ref,
                plan_ref,
                operation_ref,
                operation_claim_ref,
                task_metadata_ref,
                task_operation_index_ref,
                task_idempotency_index_ref,
            )
            if any(
                reference.get(transaction=transaction).exists
                for reference in must_not_exist
            ):
                raise InvariantViolation(
                    "GitHub delivery outcome already exists without its claim."
                )

            memory = MemoryStore()
            memory._users[owner.id] = owner
            memory._repository_admissions[current_admission.id] = current_admission
            memory._workloads[current_workload.id] = current_workload
            committed = memory.apply_github_auto_deploy_once(
                delivery_id=delivery_id,
                delivery_hash=delivery_hash,
                source_ref=source_ref,
                expected_workload_version=expected_workload_version,
                admission=admission,
                workload=workload,
                plan=plan,
                operation=operation,
                task=task,
                consumed_at=consumed_at,
            )
            operation_payload = self._serialize_operation_for_write(
                committed.operation,
                transaction=transaction,
            )
            transaction.create(
                delivery_ref,
                _serialize_github_delivery_claim(
                    delivery_id=delivery_id,
                    delivery_hash=delivery_hash,
                    source_ref=source_ref,
                    admission=admission,
                    workload=workload,
                    plan=plan,
                    operation=operation,
                    task=task,
                ),
            )
            transaction.create(
                new_admission_ref,
                _serialize_record(committed.admission),
            )
            transaction.set(
                self._document(
                    _WORKLOADS,
                    _document_id(kind="workload", logical_id=str(workload.id)),
                ),
                _serialize_record(committed.workload),
            )
            transaction.create(plan_ref, _serialize_record(committed.plan))
            transaction.create(
                operation_ref,
                operation_payload,
            )
            transaction.create(
                operation_claim_ref,
                _serialize_operation_claim(operation=committed.operation),
            )
            transaction.create(
                task_metadata_ref,
                _serialize_deploy_task_metadata(committed.task),
            )
            transaction.create(
                task_operation_index_ref,
                _serialize_deploy_task_index(material_hash=task_material_hash),
            )
            transaction.create(
                task_idempotency_index_ref,
                _serialize_deploy_task_index(material_hash=task_material_hash),
            )
            return committed

        def recover() -> GitHubAutoDeployResult | None:
            try:
                delivery_snapshot = delivery_ref.get()
                claim = _deserialize_github_delivery_claim(delivery_snapshot)
                if not _github_claim_matches(
                    claim,
                    delivery_id=delivery_id,
                    delivery_hash=delivery_hash,
                    source_ref=source_ref,
                    admission=admission,
                    workload=workload,
                    plan=plan,
                    operation=operation,
                    task=task,
                ):
                    return None
                persisted_task = self.get_deploy_task(operation.id)
                if persisted_task.material_hash != task_material_hash:
                    return None
                return GitHubAutoDeployResult(
                    admission=self.get_repository_admission(admission.id),
                    workload=self.get_workload(workload.id),
                    plan=self.get_deployment_plan(plan.id),
                    operation=self.get_operation(operation.id),
                    task=persisted_task,
                    replayed=True,
                )
            except (NotFound, StoreError, TaskNotFoundError):
                return None

        return self._run_atomic(operation_body, recover)

    def create_operation_once(self, operation: Operation) -> Operation:
        def operation_body(_transaction: object) -> Operation:
            transaction = cast(Any, _transaction)
            claim_ref = self._document(
                _OPERATION_IDEMPOTENCY,
                _document_id(
                    kind="operation_idempotency",
                    logical_id=_operation_claim_logical_id(
                        operation.actor_id,
                        operation.idempotency_key,
                    ),
                ),
            )
            claim_snapshot = claim_ref.get(transaction=transaction)
            if claim_snapshot.exists:
                claim = _deserialize_operation_claim(claim_snapshot)
                current_workload = claim["workload_id"]
                current_material = (
                    cast(str, claim["request_hash"]),
                    cast(str, claim["action"]),
                    None
                    if current_workload is None
                    else WorkloadId(cast(str, current_workload)),
                )
                next_material = (
                    operation.request_hash,
                    operation.action,
                    operation.workload_id,
                )
                if current_material != next_material:
                    raise IdempotencyConflict(
                        "Idempotency key was already used for different material."
                    )
                return cast(
                    Operation,
                    self._get_plain_snapshot(
                        _OPERATION_SPEC,
                        cast(str, claim["operation_id"]),
                        "operation",
                        transaction=transaction,
                    ),
                )
            operation_ref = self._document(
                _OPERATIONS,
                _document_id(kind="operation", logical_id=str(operation.id)),
            )
            if operation_ref.get(transaction=transaction).exists:
                raise AlreadyExists("operation already exists.")
            created = operation
            transaction.create(
                operation_ref,
                self._serialize_operation_for_write(
                    created,
                    transaction=transaction,
                ),
            )
            transaction.create(
                claim_ref,
                _serialize_operation_claim(operation=created),
            )
            return created

        def recover() -> Operation | None:
            try:
                recovered = self.get_operation(operation.id)
            except (NotFound, StoreError):
                return None
            if recovered == operation:
                return recovered
            return None

        return self._run_atomic(operation_body, recover)

    def consume_schedule_plan_with_operation(
        self,
        *,
        plan_id: DeploymentPlanId,
        actor_id: UserId,
        expected_material_hash: str,
        expected_action: str,
        policy_version: str,
        consumed_at: datetime,
        schedule: Schedule,
        operation: Operation,
    ) -> tuple[DeploymentPlan, Schedule, Operation]:
        plan_logical_id = str(plan_id)
        schedule_logical_id = str(schedule.id)

        def operation_body(
            _transaction: object,
        ) -> tuple[DeploymentPlan, Schedule, Operation]:
            transaction = cast(Any, _transaction)
            store = MemoryStore()
            plan_ref = self._document(
                _DEPLOYMENT_PLANS,
                _document_id(kind="deployment_plan", logical_id=plan_logical_id),
            )
            schedule_ref = self._document(
                _SCHEDULES,
                _document_id(kind="schedule", logical_id=schedule_logical_id),
            )
            operation_ref = self._document(
                _OPERATIONS,
                _document_id(kind="operation", logical_id=str(operation.id)),
            )
            claim_ref = self._document(
                _OPERATION_IDEMPOTENCY,
                _document_id(
                    kind="operation_idempotency",
                    logical_id=_operation_claim_logical_id(
                        operation.actor_id,
                        operation.idempotency_key,
                    ),
                ),
            )

            plan_snapshot = plan_ref.get(transaction=transaction)
            if plan_snapshot.exists is not True:
                raise NotFound("deployment plan was not found.")
            store._deployment_plans[plan_id] = cast(
                DeploymentPlan,
                _deserialize_record(plan_snapshot, spec=_DEPLOYMENT_PLAN_SPEC),
            )

            schedule_snapshot = schedule_ref.get(transaction=transaction)
            if schedule_snapshot.exists is True:
                store._schedules[schedule.id] = cast(
                    Schedule,
                    _deserialize_record(schedule_snapshot, spec=_SCHEDULE_SPEC),
                )

            operation_snapshot = operation_ref.get(transaction=transaction)
            if operation_snapshot.exists is True:
                store._operations[operation.id] = cast(
                    Operation,
                    _deserialize_record(operation_snapshot, spec=_OPERATION_SPEC),
                )

            claim_snapshot = claim_ref.get(transaction=transaction)
            if claim_snapshot.exists is True:
                claim = _deserialize_operation_claim(claim_snapshot)
                current_workload = claim["workload_id"]
                claimed_operation_id = OperationId(cast(str, claim["operation_id"]))
                store._operation_idempotency[(actor_id, operation.idempotency_key)] = (
                    cast(str, claim["request_hash"]),
                    cast(str, claim["action"]),
                    None
                    if current_workload is None
                    else WorkloadId(cast(str, current_workload)),
                    claimed_operation_id,
                )
                if claimed_operation_id != operation.id:
                    claimed_operation_ref = self._document(
                        _OPERATIONS,
                        _document_id(
                            kind="operation",
                            logical_id=str(claimed_operation_id),
                        ),
                    )
                    claimed_operation_snapshot = claimed_operation_ref.get(
                        transaction=transaction
                    )
                    if claimed_operation_snapshot.exists is not True:
                        raise InvariantViolation(
                            "schedule replay outcome is incomplete."
                        )
                    store._operations[claimed_operation_id] = cast(
                        Operation,
                        _deserialize_record(
                            claimed_operation_snapshot,
                            spec=_OPERATION_SPEC,
                        ),
                    )

            consumed_plan, saved_schedule, saved_operation = (
                store.consume_schedule_plan_with_operation(
                    plan_id=plan_id,
                    actor_id=actor_id,
                    expected_material_hash=expected_material_hash,
                    expected_action=expected_action,
                    policy_version=policy_version,
                    consumed_at=consumed_at,
                    schedule=schedule,
                    operation=operation,
                )
            )
            operation_payload = self._serialize_operation_for_write(
                saved_operation,
                transaction=transaction,
            )

            transaction.set(plan_ref, _serialize_record(consumed_plan))
            transaction.set(schedule_ref, _serialize_record(saved_schedule))
            transaction.set(
                operation_ref,
                operation_payload,
            )
            transaction.set(
                claim_ref,
                _serialize_operation_claim(operation=saved_operation),
            )
            return consumed_plan, saved_schedule, saved_operation

        def recover() -> tuple[DeploymentPlan, Schedule, Operation] | None:
            try:
                recovered_plan = self.get_deployment_plan(plan_id)
                recovered_schedule = self.get_schedule(schedule.id)
                recovered_operation = self.create_operation_once(operation)
            except (
                AlreadyExists,
                IdempotencyConflict,
                InvariantViolation,
                NotFound,
                StoreError,
                VersionConflict,
            ):
                return None
            if recovered_plan.state is not PlanState.CONSUMED:
                return None
            if recovered_schedule.id != schedule.id:
                return None
            return recovered_plan, recovered_schedule, recovered_operation

        return cast(
            tuple[DeploymentPlan, Schedule, Operation],
            self._run_atomic(operation_body, recover),
        )

    def get_operation(self, operation_id: OperationId) -> Operation:
        return cast(
            Operation,
            self._get_plain(_OPERATION_SPEC, str(operation_id), "operation"),
        )

    def get_latest_workload_operation(
        self,
        *,
        owner_id: UserId,
        workload_id: WorkloadId,
    ) -> Operation | None:
        try:
            # Production requires a collection-scope composite index over
            # workload_owner_id ASC, workload_id ASC, updated_at DESC,
            # created_at DESC, and id DESC. Legacy documents without the
            # owner field intentionally fail closed and require offline backfill.
            query = (
                self._collection(_OPERATION_SPEC.collection)
                .where("workload_owner_id", "==", str(owner_id))
                .where("workload_id", "==", str(workload_id))
                .order_by("updated_at", direction=_firestore_descending())
                .order_by("created_at", direction=_firestore_descending())
                .order_by("id", direction=_firestore_descending())
                .limit(1)
            )
            snapshots = tuple(query.stream())
            if not snapshots:
                return None
            if len(snapshots) != 1:
                raise InvariantViolation("latest operation scope is invalid.")
            snapshot = snapshots[0]
            raw = snapshot.to_dict()
            if (
                not isinstance(raw, dict)
                or raw.get("workload_owner_id") != str(owner_id)
                or raw.get("workload_id") != str(workload_id)
            ):
                raise InvariantViolation("latest operation scope is invalid.")
            operation = cast(
                Operation,
                _deserialize_record(snapshot, spec=_OPERATION_SPEC),
            )
            if operation.workload_id != workload_id:
                raise InvariantViolation("latest operation scope is invalid.")
            return operation
        except StoreError:
            raise
        except Exception as exc:  # pragma: no cover - defensive adapter guard
            raise _store_failure() from exc

    def save_operation(
        self,
        operation: Operation,
        *,
        expected_version: int,
    ) -> Operation:
        return cast(
            Operation,
            self._save_plain(
                _OPERATION_SPEC,
                operation,
                expected_version=expected_version,
                save_method=MemoryStore.save_operation,
                not_found_label="operation",
            ),
        )

    def create_schedule(self, schedule: Schedule) -> Schedule:
        return cast(
            Schedule,
            self._create_plain(
                _SCHEDULE_SPEC,
                schedule,
                MemoryStore.create_schedule,
            ),
        )

    def get_schedule(self, schedule_id: ScheduleId) -> Schedule:
        return cast(
            Schedule,
            self._get_plain(_SCHEDULE_SPEC, str(schedule_id), "schedule"),
        )

    def save_schedule(self, schedule: Schedule, *, expected_version: int) -> Schedule:
        return cast(
            Schedule,
            self._save_plain(
                _SCHEDULE_SPEC,
                schedule,
                expected_version=expected_version,
                save_method=MemoryStore.save_schedule,
                not_found_label="schedule",
            ),
        )

    def list_schedules(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[Schedule, ...]:
        if owner_id is None:
            return cast(tuple[Schedule, ...], self._list_plain(_SCHEDULE_SPEC))
        try:
            query = self._collection(_SCHEDULE_SPEC.collection).where(
                "owner_id",
                "==",
                str(owner_id),
            )
            schedules = _snapshot_to_records(
                query.stream(),
                deserialize=lambda snapshot: cast(
                    Schedule,
                    _deserialize_record(snapshot, spec=_SCHEDULE_SPEC),
                ),
            )
            if _SCHEDULE_SPEC.sort_key is None:
                return cast(tuple[Schedule, ...], schedules)
            return tuple(sorted(schedules, key=_SCHEDULE_SPEC.sort_key))
        except StoreError:
            raise
        except Exception as exc:  # pragma: no cover - defensive adapter guard
            raise _store_failure() from exc

    def acquire_schedule_lease(
        self,
        schedule_id: ScheduleId,
        *,
        expected_version: int,
        lease_token: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> Schedule:
        def run(transaction: object) -> Schedule:
            current = cast(
                Schedule,
                self._get_plain_snapshot(
                    _SCHEDULE_SPEC,
                    str(schedule_id),
                    "schedule",
                    transaction=cast(Any, transaction),
                ),
            )
            store = MemoryStore()
            store._schedules[current.id] = current
            leased = store.acquire_schedule_lease(
                schedule_id,
                expected_version=expected_version,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                now=now,
            )
            cast(Any, transaction).set(
                self._document(
                    _SCHEDULES,
                    _document_id(kind="schedule", logical_id=str(leased.id)),
                ),
                _serialize_record(leased),
            )
            return leased

        return cast(Schedule, self._transaction_runner(self._client, run))

    def complete_schedule_run(
        self,
        schedule_id: ScheduleId,
        *,
        expected_version: int,
        lease_token: str,
        succeeded: bool,
        completed_at: datetime,
    ) -> Schedule:
        def run(transaction: object) -> Schedule:
            current = cast(
                Schedule,
                self._get_plain_snapshot(
                    _SCHEDULE_SPEC,
                    str(schedule_id),
                    "schedule",
                    transaction=cast(Any, transaction),
                ),
            )
            store = MemoryStore()
            store._schedules[current.id] = current
            completed = store.complete_schedule_run(
                schedule_id,
                expected_version=expected_version,
                lease_token=lease_token,
                succeeded=succeeded,
                completed_at=completed_at,
            )
            cast(Any, transaction).set(
                self._document(
                    _SCHEDULES,
                    _document_id(kind="schedule", logical_id=str(completed.id)),
                ),
                _serialize_record(completed),
            )
            return completed

        return cast(Schedule, self._transaction_runner(self._client, run))

    def create_secret_metadata(self, secret: SecretMetadata) -> SecretMetadata:
        return cast(
            SecretMetadata,
            self._create_plain(
                _SECRET_SPEC,
                secret,
                MemoryStore.create_secret_metadata,
            ),
        )

    def get_secret_metadata(self, secret_id: SecretId) -> SecretMetadata:
        return cast(
            SecretMetadata,
            self._get_plain(_SECRET_SPEC, str(secret_id), "secret metadata"),
        )

    def save_secret_metadata(
        self, secret: SecretMetadata, *, expected_version: int
    ) -> SecretMetadata:
        return cast(
            SecretMetadata,
            self._save_plain(
                _SECRET_SPEC,
                secret,
                expected_version=expected_version,
                save_method=MemoryStore.save_secret_metadata,
                not_found_label="secret metadata",
            ),
        )

    def list_secret_metadata(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[SecretMetadata, ...]:
        if owner_id is None:
            return cast(tuple[SecretMetadata, ...], self._list_plain(_SECRET_SPEC))
        try:
            query = self._collection(_SECRET_SPEC.collection).where(
                "owner_id",
                "==",
                str(owner_id),
            )
            secrets = _snapshot_to_records(
                query.stream(),
                deserialize=lambda snapshot: cast(
                    SecretMetadata,
                    _deserialize_record(snapshot, spec=_SECRET_SPEC),
                ),
            )
            if _SECRET_SPEC.sort_key is None:
                return cast(tuple[SecretMetadata, ...], secrets)
            return tuple(sorted(secrets, key=_SECRET_SPEC.sort_key))
        except StoreError:
            raise
        except Exception as exc:  # pragma: no cover - defensive adapter guard
            raise _store_failure() from exc

    def append_usage_entry(self, entry: UsageEntry) -> UsageEntry:
        return cast(
            UsageEntry,
            self._create_plain(
                _USAGE_SPEC,
                entry,
                MemoryStore.append_usage_entry,
            ),
        )

    def upsert_usage_entry_monotonic(
        self,
        *,
        current: UsageEntry,
        updated: UsageEntry,
    ) -> UsageEntry:
        document_id = _document_id(kind=_USAGE_SPEC.kind, logical_id=str(current.id))

        def run(transaction: object) -> UsageEntry:
            reference = self._document(_USAGE_SPEC.collection, document_id)
            snapshot = reference.get(transaction=transaction)
            if snapshot.exists is not True:
                raise NotFound("usage entry was not found.")
            persisted = cast(
                UsageEntry,
                _deserialize_record(snapshot, spec=_USAGE_SPEC),
            )
            store = MemoryStore()
            store._usage_entries[persisted.id] = persisted
            saved = store.upsert_usage_entry_monotonic(
                current=current,
                updated=updated,
            )
            if saved != persisted:
                cast(Any, transaction).set(reference, _serialize_record(saved))
            return saved

        return cast(UsageEntry, self._transaction_runner(self._client, run))

    def list_usage_entries(
        self,
        *,
        owner_id: UserId | None = None,
    ) -> tuple[UsageEntry, ...]:
        if owner_id is None:
            return cast(tuple[UsageEntry, ...], self._list_plain(_USAGE_SPEC))
        try:
            query = self._collection(_USAGE_SPEC.collection).where(
                "owner_id",
                "==",
                str(owner_id),
            )
            entries = _snapshot_to_records(
                query.stream(),
                deserialize=lambda snapshot: cast(
                    UsageEntry,
                    _deserialize_record(snapshot, spec=_USAGE_SPEC),
                ),
            )
            if _USAGE_SPEC.sort_key is None:
                return cast(tuple[UsageEntry, ...], entries)
            return tuple(sorted(entries, key=_USAGE_SPEC.sort_key))
        except StoreError:
            raise
        except Exception as exc:  # pragma: no cover - defensive adapter guard
            raise _store_failure() from exc

    def create_org_cost_guard(self, guard: OrgCostGuard) -> OrgCostGuard:
        return cast(
            OrgCostGuard,
            self._create_plain(
                _ORG_COST_GUARD_SPEC,
                guard,
                MemoryStore.create_org_cost_guard,
            ),
        )

    def get_org_cost_guard(self) -> OrgCostGuard:
        return cast(
            OrgCostGuard,
            self._get_plain(
                _ORG_COST_GUARD_SPEC,
                "organization",
                "org cost guard",
            ),
        )

    def save_org_cost_guard(
        self,
        guard: OrgCostGuard,
        *,
        expected_version: int,
    ) -> OrgCostGuard:
        return cast(
            OrgCostGuard,
            self._save_plain(
                _ORG_COST_GUARD_SPEC,
                guard,
                expected_version=expected_version,
                save_method=MemoryStore.save_org_cost_guard,
                not_found_label="org cost guard",
            ),
        )

    def append_activity_event(self, event: ActivityEvent) -> ActivityEvent:
        return cast(
            ActivityEvent,
            self._create_plain(
                _ACTIVITY_SPEC,
                event,
                MemoryStore.append_activity_event,
            ),
        )

    def list_activity_events(
        self,
        *,
        user_id: UserId | None = None,
    ) -> tuple[ActivityEvent, ...]:
        events = cast(tuple[ActivityEvent, ...], self._list_plain(_ACTIVITY_SPEC))
        return tuple(
            event for event in events if user_id is None or event.user_id == user_id
        )

    def expire_activity_events(
        self,
        *,
        event_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not isinstance(event_ids, tuple):
            raise InvariantViolation("activity event IDs must be immutable.")
        normalized: list[str] = []
        seen: set[str] = set()
        for event_id in event_ids:
            if (
                not isinstance(event_id, str)
                or not event_id.strip()
                or event_id != event_id.strip()
            ):
                raise InvariantViolation("activity event ID is invalid.")
            if event_id in seen:
                raise InvariantViolation("activity event IDs must be unique.")
            seen.add(event_id)
            normalized.append(event_id)

        def run(transaction: object) -> tuple[str, ...]:
            tx = cast(Any, transaction)
            references = [
                (
                    event_id,
                    self._document(
                        _ACTIVITY_EVENTS,
                        _document_id(kind="activity_event", logical_id=event_id),
                    ),
                )
                for event_id in normalized
            ]
            snapshots = [
                (event_id, reference, reference.get(transaction=tx))
                for event_id, reference in references
            ]
            removed: list[str] = []
            for event_id, reference, snapshot in snapshots:
                if snapshot.exists:
                    tx.delete(reference)
                    removed.append(event_id)
            return tuple(removed)

        return cast(tuple[str, ...], self._transaction_runner(self._client, run))

    def create_daily_usage_aggregate(
        self, aggregate: DailyUsageAggregate
    ) -> DailyUsageAggregate:
        return cast(
            DailyUsageAggregate,
            self._create_plain(
                _DAILY_AGGREGATE_SPEC,
                aggregate,
                MemoryStore.create_daily_usage_aggregate,
            ),
        )

    def get_daily_usage_aggregate(
        self, day: date, user_id: UserId | None
    ) -> DailyUsageAggregate:
        return cast(
            DailyUsageAggregate,
            self._get_plain(
                _DAILY_AGGREGATE_SPEC,
                _daily_usage_logical_id(day, user_id),
                "daily usage aggregate",
            ),
        )

    def save_daily_usage_aggregate(
        self, aggregate: DailyUsageAggregate, *, expected_version: int
    ) -> DailyUsageAggregate:
        return cast(
            DailyUsageAggregate,
            self._save_plain(
                _DAILY_AGGREGATE_SPEC,
                aggregate,
                expected_version=expected_version,
                save_method=MemoryStore.save_daily_usage_aggregate,
                not_found_label="daily usage aggregate",
            ),
        )

    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        return cast(
            AuditEvent,
            self._create_plain(
                _AUDIT_SPEC,
                event,
                MemoryStore.append_audit_event,
            ),
        )

    def list_audit_events(self) -> tuple[AuditEvent, ...]:
        return cast(tuple[AuditEvent, ...], self._list_plain(_AUDIT_SPEC))

    def create_lifecycle_action(self, action: LifecycleAction) -> LifecycleAction:
        return cast(
            LifecycleAction,
            self._create_plain(
                _LIFECYCLE_SPEC,
                action,
                MemoryStore.create_lifecycle_action,
            ),
        )

    def get_lifecycle_action(self, action_id: LifecycleActionId) -> LifecycleAction:
        return cast(
            LifecycleAction,
            self._get_plain(_LIFECYCLE_SPEC, str(action_id), "lifecycle action"),
        )

    def save_lifecycle_action(
        self, action: LifecycleAction, *, expected_version: int
    ) -> LifecycleAction:
        return cast(
            LifecycleAction,
            self._save_plain(
                _LIFECYCLE_SPEC,
                action,
                expected_version=expected_version,
                save_method=MemoryStore.save_lifecycle_action,
                not_found_label="lifecycle action",
            ),
        )

    def get_maintenance_job_status(self, job_name: str) -> MaintenanceJobStatus:
        return cast(
            MaintenanceJobStatus,
            self._get_plain(
                _MAINTENANCE_STATUS_SPEC,
                job_name,
                "maintenance job status",
            ),
        )

    def list_maintenance_job_statuses(self) -> tuple[MaintenanceJobStatus, ...]:
        return cast(
            tuple[MaintenanceJobStatus, ...],
            self._list_plain(_MAINTENANCE_STATUS_SPEC),
        )

    def record_maintenance_job_started(
        self,
        *,
        job_name: str,
        run_id: str,
        started_at: datetime,
    ) -> MaintenanceJobStatus:
        document_id = _document_id(
            kind=_MAINTENANCE_STATUS_SPEC.kind,
            logical_id=job_name,
        )

        def run(transaction: object) -> MaintenanceJobStatus:
            reference = self._document(_MAINTENANCE_STATUS_SPEC.collection, document_id)
            snapshot = reference.get(transaction=transaction)
            store = MemoryStore()
            if snapshot.exists is True:
                current = _deserialize_record(snapshot, spec=_MAINTENANCE_STATUS_SPEC)
                getattr(store, _MAINTENANCE_STATUS_SPEC.memory_attr)[
                    _MAINTENANCE_STATUS_SPEC.memory_key(current)
                ] = current
            started = store.record_maintenance_job_started(
                job_name=job_name,
                run_id=run_id,
                started_at=started_at,
            )
            cast(Any, transaction).set(reference, _serialize_record(started))
            return started

        return cast(
            MaintenanceJobStatus,
            self._transaction_runner(self._client, run),
        )

    def record_maintenance_job_terminal(
        self,
        *,
        job_name: str,
        run_id: str,
        expected_version: int,
        finished_at: datetime,
        outcome: str,
        summary: tuple[tuple[str, int], ...],
        failure_code: str | None = None,
        failure_class: str | None = None,
    ) -> MaintenanceJobStatus:
        document_id = _document_id(
            kind=_MAINTENANCE_STATUS_SPEC.kind,
            logical_id=job_name,
        )

        def run(transaction: object) -> MaintenanceJobStatus:
            reference = self._document(_MAINTENANCE_STATUS_SPEC.collection, document_id)
            snapshot = reference.get(transaction=transaction)
            if snapshot.exists is not True:
                raise NotFound("maintenance job status was not found.")
            current = _deserialize_record(snapshot, spec=_MAINTENANCE_STATUS_SPEC)
            store = MemoryStore()
            getattr(store, _MAINTENANCE_STATUS_SPEC.memory_attr)[
                _MAINTENANCE_STATUS_SPEC.memory_key(current)
            ] = current
            terminal = store.record_maintenance_job_terminal(
                job_name=job_name,
                run_id=run_id,
                expected_version=expected_version,
                finished_at=finished_at,
                outcome=outcome,
                summary=summary,
                failure_code=failure_code,
                failure_class=failure_class,
            )
            cast(Any, transaction).set(reference, _serialize_record(terminal))
            return terminal

        return cast(
            MaintenanceJobStatus,
            self._transaction_runner(self._client, run),
        )

    def claim_origin_request(self, claim: OriginRequestClaim) -> None:
        document_id = _document_id(
            kind="origin_request_claim",
            logical_id=str(claim.request_id),
        )

        def run(transaction: object) -> None:
            reference = self._document(_ORIGIN_REQUEST_CLAIMS, document_id)
            snapshot = reference.get(transaction=transaction)
            if snapshot.exists:
                raise ReplayDetected("origin request ID was already claimed.")
            cast(Any, transaction).create(reference, _serialize_record(claim))

        self._transaction_runner(self._client, run)

    def create_deploy_task_once(
        self,
        task: QueuedDeployTask,
    ) -> tuple[QueuedDeployTask, bool]:
        _validate_deploy_task_limits(task)
        material_hash = task.material_hash
        metadata_id = _document_id(kind="deploy_task", logical_id=material_hash)
        operation_index_id = _document_id(
            kind="deploy_task_operation_index",
            logical_id=str(task.operation_id),
        )
        idempotency_index_id = _document_id(
            kind="deploy_task_idempotency_index",
            logical_id=task.idempotency_key,
        )

        def run(transaction: object) -> tuple[QueuedDeployTask, bool]:
            tx = cast(Any, transaction)
            operation_index_ref = self._document(
                _DEPLOY_TASK_OPERATION_INDEX,
                operation_index_id,
            )
            idempotency_index_ref = self._document(
                _DEPLOY_TASK_IDEMPOTENCY_INDEX,
                idempotency_index_id,
            )
            operation_index_snapshot = operation_index_ref.get(transaction=tx)
            idempotency_index_snapshot = idempotency_index_ref.get(transaction=tx)
            if operation_index_snapshot.exists != idempotency_index_snapshot.exists:
                raise InvariantViolation("deploy task indexes are inconsistent.")
            if operation_index_snapshot.exists and idempotency_index_snapshot.exists:
                operation_material_hash = _deserialize_deploy_task_index(
                    operation_index_snapshot
                )
                idempotency_material_hash = _deserialize_deploy_task_index(
                    idempotency_index_snapshot
                )
                if operation_material_hash != idempotency_material_hash:
                    raise InvariantViolation("deploy task indexes are inconsistent.")
                existing = self._get_deploy_task_by_material_hash(
                    operation_material_hash,
                    transaction=tx,
                )
                if existing.operation_id != task.operation_id:
                    raise InvariantViolation("deploy task metadata is inconsistent.")
                if existing.material_hash != task.material_hash:
                    raise TaskConflictError("queued deploy task material changed.")
                return existing, False

            metadata_ref = self._document(_DEPLOY_TASKS, metadata_id)
            tx.create(
                metadata_ref,
                _serialize_deploy_task_metadata(task),
            )
            tx.create(
                operation_index_ref,
                _serialize_deploy_task_index(material_hash=material_hash),
            )
            tx.create(
                idempotency_index_ref,
                _serialize_deploy_task_index(material_hash=material_hash),
            )
            return task, True

        def recover() -> tuple[QueuedDeployTask, bool] | None:
            try:
                recovered = self.get_deploy_task(task.operation_id)
            except TaskNotFoundError:
                return None
            if recovered.material_hash == task.material_hash:
                return recovered, False
            return None

        return self._run_atomic(run, recover)

    def get_deploy_task(
        self,
        operation_id: OperationId,
    ) -> QueuedDeployTask:
        operation_index_id = _document_id(
            kind="deploy_task_operation_index",
            logical_id=str(operation_id),
        )
        operation_index_snapshot = self._document(
            _DEPLOY_TASK_OPERATION_INDEX,
            operation_index_id,
        ).get()
        if operation_index_snapshot.exists is not True:
            raise TaskNotFoundError("queued deploy task was not found.")
        material_hash = _deserialize_deploy_task_index(operation_index_snapshot)
        task = self._get_deploy_task_by_material_hash(material_hash)
        idempotency_index_snapshot = self._document(
            _DEPLOY_TASK_IDEMPOTENCY_INDEX,
            _document_id(
                kind="deploy_task_idempotency_index",
                logical_id=task.idempotency_key,
            ),
        ).get()
        if idempotency_index_snapshot.exists is not True:
            raise _store_failure()
        if _deserialize_deploy_task_index(idempotency_index_snapshot) != material_hash:
            raise _store_failure()
        return task

    def _get_plain(
        self,
        spec: _RecordSpec,
        logical_id: str,
        label: str,
    ) -> object:
        snapshot = self._document(
            spec.collection,
            _document_id(kind=spec.kind, logical_id=logical_id),
        ).get()
        if snapshot.exists is not True:
            raise NotFound(f"{label} was not found.")
        return _deserialize_record(snapshot, spec=spec)

    def _get_plain_snapshot(
        self,
        spec: _RecordSpec,
        logical_id: str,
        label: str,
        *,
        transaction: object,
    ) -> object:
        snapshot = self._document(
            spec.collection,
            _document_id(kind=spec.kind, logical_id=logical_id),
        ).get(transaction=transaction)
        if snapshot.exists is not True:
            raise NotFound(f"{label} was not found.")
        return _deserialize_record(snapshot, spec=spec)

    def _list_plain(self, spec: _RecordSpec) -> tuple[object, ...]:
        try:
            records = _snapshot_to_records(
                self._client.collection(spec.collection).stream(),
                deserialize=lambda snapshot: _deserialize_record(snapshot, spec=spec),
            )
            if spec.sort_key is None:
                return tuple(records)
            return tuple(sorted(records, key=spec.sort_key))
        except StoreError:
            raise
        except Exception as exc:  # pragma: no cover - defensive adapter guard
            raise _store_failure() from exc

    def _create_plain(
        self,
        spec: _RecordSpec,
        record: object,
        create_method: Callable[..., object],
    ) -> object:
        document_id = _document_id(
            kind=spec.kind,
            logical_id=spec.logical_id_from_record(record),
        )

        def run(transaction: object) -> object:
            reference = self._document(spec.collection, document_id)
            snapshot = reference.get(transaction=transaction)
            if snapshot.exists:
                raise AlreadyExists(f"{spec.kind.replace('_', ' ')} already exists.")
            store = MemoryStore()
            created = create_method(store, record)
            cast(Any, transaction).create(
                reference,
                self._serialize_for_spec(
                    spec,
                    created,
                    transaction=cast(Any, transaction),
                ),
            )
            return created

        return cast(object, self._transaction_runner(self._client, run))

    def _get_deploy_task_by_material_hash(
        self,
        material_hash: str,
        *,
        transaction: object | None = None,
    ) -> QueuedDeployTask:
        metadata_snapshot = self._document(
            _DEPLOY_TASKS,
            _document_id(kind="deploy_task", logical_id=material_hash),
        ).get(transaction=transaction)
        task = _deserialize_deploy_task_metadata(metadata_snapshot)
        if task.material_hash != material_hash:
            raise _store_failure()
        return task

    def _save_plain(
        self,
        spec: _RecordSpec,
        record: object,
        *,
        expected_version: int,
        save_method: Callable[..., object],
        not_found_label: str,
    ) -> object:
        document_id = _document_id(
            kind=spec.kind,
            logical_id=spec.logical_id_from_record(record),
        )

        def run(transaction: object) -> object:
            reference = self._document(spec.collection, document_id)
            snapshot = reference.get(transaction=transaction)
            if snapshot.exists is not True:
                raise NotFound(f"{not_found_label} was not found.")
            current = _deserialize_record(snapshot, spec=spec)
            store = MemoryStore()
            getattr(store, spec.memory_attr)[spec.memory_key(current)] = current
            saved = save_method(store, record, expected_version=expected_version)
            cast(Any, transaction).set(
                reference,
                self._serialize_for_spec(
                    spec,
                    saved,
                    transaction=cast(Any, transaction),
                ),
            )
            return saved

        return cast(object, self._transaction_runner(self._client, run))

    def _collection(self, name: str) -> _Collection:
        return self._client.collection(name)

    def _document(self, collection: str, document_id: str) -> _DocumentReference:
        return self._collection(collection).document(document_id)

    def _serialize_for_spec(
        self,
        spec: _RecordSpec,
        record: object,
        *,
        transaction: object | None = None,
    ) -> dict[str, object]:
        if spec is _OPERATION_SPEC:
            return self._serialize_operation_for_write(
                cast(Operation, record),
                transaction=transaction,
            )
        return _serialize_record(record)

    def _serialize_operation_for_write(
        self,
        operation: Operation,
        *,
        transaction: object | None = None,
    ) -> dict[str, object]:
        payload = _serialize_record(operation)
        payload["workload_owner_id"] = self._workload_owner_id_for_operation(
            operation,
            transaction=transaction,
        )
        return payload

    def _workload_owner_id_for_operation(
        self,
        operation: Operation,
        *,
        transaction: object | None = None,
    ) -> str | None:
        if operation.workload_id is None:
            return None
        try:
            workload = cast(
                Workload,
                self._get_plain_snapshot(
                    _WORKLOAD_SPEC,
                    str(operation.workload_id),
                    "workload",
                    transaction=transaction,
                ),
            )
        except NotFound:
            return None
        return str(workload.owner_id)

    def _document_exists(self, collection: str, document_id: str) -> bool:
        return self._document(collection, document_id).get().exists is True

    def _write(
        self,
        collection: str,
        document_id: str,
        payload: dict[str, object],
    ) -> None:
        try:
            self._document(collection, document_id).set(payload)
        except Exception as exc:  # pragma: no cover - defensive adapter guard
            raise _store_failure() from exc

    def _run_atomic(
        self,
        operation: Callable[[object], RecordT],
        recover: Callable[[], RecordT | None],
    ) -> RecordT:
        try:
            return cast(
                RecordT,
                self._transaction_runner(
                    self._client,
                    operation,
                ),
            )
        except (
            AlreadyExists,
            IdempotencyConflict,
            InvariantViolation,
            NotFound,
            ReplayDetected,
            TaskConflictError,
            TaskNotFoundError,
            ValueError,
            VersionConflict,
        ):
            raise
        except Exception as exc:
            recovered = recover()
            if recovered is not None:
                return recovered
            raise _store_failure() from exc
