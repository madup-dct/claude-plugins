"""Least-privilege Secret Manager metadata and version adapter."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import google_crc32c
from google.api_core.exceptions import AlreadyExists as GoogleAlreadyExists
from google.api_core.exceptions import NotFound as GoogleNotFound
from google.cloud import secretmanager_v1
from google.iam.v1 import iam_policy_pb2, policy_pb2  # type: ignore[import-untyped]

from mim_control_plane.domain.models import SecretId, SecretMetadata, WorkloadId
from mim_control_plane.domain.states import SecretLifecycleState
from mim_control_plane.ports.execution import (
    ExecutionPlaneError,
    SecretAttachmentReference,
    SecretMetadataDeniedError,
    SecretMetadataPort,
)
from mim_control_plane.ports.store import NotFound, Store
from mim_control_plane.services.render import (
    DesiredStateSecretAttachment,
    _secret_attachment_env_name,
)
from mim_control_plane.services.runtime_naming import provider_secret_id

_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_SECRET_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SERVICE_ACCOUNT_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@"
    r"([a-z][a-z0-9-]{4,28}[a-z0-9])\.iam\.gserviceaccount\.com$"
)
_VERSION_RESOURCE_PATTERN = re.compile(
    r"^projects/([a-z][a-z0-9-]{4,28}[a-z0-9])/"
    r"secrets/([a-z][a-z0-9-]{0,62})/versions/([1-9][0-9]{0,9})$"
)
_ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"
_METADATA_READER_ROLE = "roles/secretmanager.viewer"
_VERSION_MANAGER_ROLE = "roles/secretmanager.secretVersionManager"
_MAX_PAYLOAD_BYTES = 16 * 1024
_MAX_WORKLOAD_ATTACHMENTS = 5
_RETIREMENT_MINIMUM = timedelta(days=7)
_DENIED = "secret metadata lookup was denied."
_FAILED = "secret operation failed."
_MIM_MANAGED_LABELS = (("managed-by", "mim-control-plane"),)


@dataclass(frozen=True, slots=True)
class ManagedSecretMetadata:
    """Value-free metadata for an exact managed secret resource."""

    name: str
    created: bool
    labels: tuple[tuple[str, str], ...]


class SecretManagerAdapterError(ExecutionPlaneError):
    """Raised when a bounded Secret Manager operation fails closed."""


@dataclass(frozen=True, slots=True)
class SecretVersionMetadata:
    """Value-free metadata returned after a Secret Manager version write."""

    name: str
    version: int
    state: str
    checksum_verified: bool


@dataclass(frozen=True, slots=True)
class SecretVersionStateMetadata:
    """Value-free metadata returned after lifecycle actions on a version."""

    name: str
    version: int
    state: str


@dataclass(frozen=True, slots=True)
class ObservedSecretState:
    """Metadata-only snapshot used to recover exact managed-secret progress."""

    name: str
    exists: bool
    exact_bindings: bool
    enabled_versions: tuple[int, ...]
    disabled_versions: tuple[int, ...]
    destroyed_versions: tuple[int, ...]


class SecretManagerAdapter(SecretMetadataPort):
    """Resolve attachments without ever reading secret payloads.

    The adapter owns only exact secret resources in one configured project.  It
    deliberately has no method that calls ``access_secret_version``.
    """

    def __init__(
        self,
        *,
        client: Any,
        store: Store,
        project_id: str,
        version_manager_service_account: str,
    ) -> None:
        self._project_id = _require_project_id(project_id)
        self._version_manager_service_account = _require_service_account(
            version_manager_service_account,
            project_id=self._project_id,
        )
        if self._version_manager_service_account != (
            f"mim-control-plane@{self._project_id}.iam.gserviceaccount.com"
        ):
            raise ValueError("service account is invalid.")
        self._client = client
        self._store = store

    def __repr__(self) -> str:
        return f"SecretManagerAdapter(project_id={self._project_id!r})"

    def resolve(
        self,
        *,
        workload_id: WorkloadId,
        attachments: tuple[SecretAttachmentReference, ...],
    ) -> tuple[DesiredStateSecretAttachment, ...]:
        if type(workload_id) is not str or not workload_id.strip():
            raise SecretMetadataDeniedError(_DENIED)
        if type(attachments) is not tuple:
            raise SecretMetadataDeniedError(_DENIED)

        resolved: list[DesiredStateSecretAttachment] = []
        seen_secret_names: set[str] = set()
        for attachment in attachments:
            if type(attachment) is not SecretAttachmentReference:
                raise SecretMetadataDeniedError(_DENIED)
            record = self._load_record(attachment.secret_id)
            self._validate_record(
                record,
                workload_id=workload_id,
                attachment=attachment,
            )
            provider_name = provider_secret_id(str(record.id))
            env_name = _secret_attachment_env_name(_require_secret_name(record.name))
            if provider_name in seen_secret_names:
                raise SecretMetadataDeniedError(_DENIED)
            seen_secret_names.add(provider_name)
            resource = self._secret_resource(record.id)
            self._audit_metadata(
                resource=resource,
                expected_version=record.active_version,
                workload_ids=record.attached_workload_ids,
            )
            resolved.append(
                DesiredStateSecretAttachment(
                    secret_id=str(record.id),
                    secret_name=provider_name,
                    secret_version=str(record.active_version),
                    env_name=env_name,
                )
            )
        return tuple(resolved)

    def add_version(
        self,
        *,
        secret_id: SecretId,
        payload: bytes,
    ) -> SecretVersionMetadata:
        resource = self._secret_resource(secret_id)
        provider_name = provider_secret_id(str(secret_id))
        if (
            type(payload) is not bytes
            or not payload
            or len(payload) > _MAX_PAYLOAD_BYTES
        ):
            raise SecretManagerAdapterError(_FAILED)
        payload_copy = bytes(payload)
        checksum = int.from_bytes(
            google_crc32c.Checksum(payload_copy).digest(),
            "big",
        )
        request = secretmanager_v1.AddSecretVersionRequest(
            parent=resource,
            payload=secretmanager_v1.SecretPayload(
                data=payload_copy,
                data_crc32c=checksum,
            ),
        )
        try:
            created = self._client.add_secret_version(request)
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None
        try:
            version = _require_version_resource(
                created.name,
                project_id=self._project_id,
                secret_name=provider_name,
            )
            if (
                created.state is not secretmanager_v1.SecretVersion.State.ENABLED
                or created.client_specified_payload_checksum is not True
            ):
                raise SecretManagerAdapterError(_FAILED)
            return SecretVersionMetadata(
                name=created.name,
                version=version,
                state="enabled",
                checksum_verified=True,
            )
        except SecretManagerAdapterError:
            raise
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None

    def ensure_secret(
        self,
        *,
        secret_id: SecretId,
        workload_ids: tuple[WorkloadId, ...],
    ) -> ManagedSecretMetadata:
        resource = self._secret_resource(secret_id)
        _require_workload_ids(workload_ids)
        created = False
        try:
            secret = self._client.get_secret(
                secretmanager_v1.GetSecretRequest(name=resource)
            )
        except GoogleNotFound:
            secret = self._create_secret(resource=resource, secret_id=secret_id)
            created = True
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None
        self._require_exact_secret(secret, resource=resource)
        self.ensure_exact_bindings(
            secret_id=secret_id,
            workload_ids=workload_ids,
        )
        return ManagedSecretMetadata(
            name=resource,
            created=created,
            labels=_MIM_MANAGED_LABELS,
        )

    def rotate_secret(
        self,
        *,
        secret_id: SecretId,
        workload_ids: tuple[WorkloadId, ...],
        payload: bytes,
    ) -> SecretVersionMetadata:
        self.ensure_secret(
            secret_id=secret_id,
            workload_ids=workload_ids,
        )
        created = self.add_version(secret_id=secret_id, payload=payload)
        return self._require_enabled_version(
            created.name,
            secret_name=provider_secret_id(str(secret_id)),
        )

    def disable_old_version(
        self,
        *,
        secret_id: SecretId,
        version_name: str,
        active_version: int,
        retirement_not_before: datetime,
        now: datetime,
    ) -> SecretVersionStateMetadata:
        provider_name = provider_secret_id(str(secret_id))
        version = self._validate_retirement_target(
            secret_name=provider_name,
            version_name=version_name,
            active_version=active_version,
            retirement_not_before=retirement_not_before,
            now=now,
            require_future_window=True,
        )
        observed = self._get_version_state(
            version_name,
            secret_name=provider_name,
        )
        if observed.version != version:
            raise SecretManagerAdapterError(_FAILED)
        if observed.state == "disabled":
            return observed
        if observed.state != "enabled":
            raise SecretManagerAdapterError(_FAILED)
        try:
            self._client.disable_secret_version(
                secretmanager_v1.DisableSecretVersionRequest(name=version_name)
            )
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None
        return self._require_version_state(
            version_name,
            secret_name=provider_name,
            expected_state=secretmanager_v1.SecretVersion.State.DISABLED,
        )

    def destroy_old_version(
        self,
        *,
        secret_id: SecretId,
        version_name: str,
        active_version: int,
        retirement_not_before: datetime,
        now: datetime,
    ) -> SecretVersionStateMetadata:
        provider_name = provider_secret_id(str(secret_id))
        self._validate_retirement_target(
            secret_name=provider_name,
            version_name=version_name,
            active_version=active_version,
            retirement_not_before=retirement_not_before,
            now=now,
            require_future_window=False,
        )
        observed = self._get_version_state(
            version_name,
            secret_name=provider_name,
        )
        if observed.state == "destroyed":
            return observed
        if observed.state != "disabled":
            raise SecretManagerAdapterError(_FAILED)
        try:
            self._client.destroy_secret_version(
                secretmanager_v1.DestroySecretVersionRequest(name=version_name)
            )
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None
        return self._require_version_state(
            version_name,
            secret_name=provider_name,
            expected_state=secretmanager_v1.SecretVersion.State.DESTROYED,
        )

    def ensure_exact_bindings(
        self,
        *,
        secret_id: SecretId,
        workload_ids: tuple[WorkloadId, ...],
    ) -> None:
        resource = self._secret_resource(secret_id)
        expected = self._expected_managed_members(workload_ids)
        try:
            current = self._client.get_iam_policy(
                iam_policy_pb2.GetIamPolicyRequest(resource=resource)
            )
            proposed_bindings: list[policy_pb2.Binding] = []
            for role in sorted(expected):
                proposed_bindings.append(
                    policy_pb2.Binding(
                        role=role,
                        members=tuple(sorted(expected[role])),
                    )
                )
            proposed = policy_pb2.Policy(
                version=max(current.version, 3),
                etag=current.etag,
                bindings=tuple(proposed_bindings),
            )
            updated = self._client.set_iam_policy(
                iam_policy_pb2.SetIamPolicyRequest(
                    resource=resource,
                    policy=proposed,
                )
            )
            self._require_exact_managed_bindings(updated, expected=expected)
        except SecretManagerAdapterError:
            raise
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None

    def probe_secret(
        self,
        *,
        secret_id: SecretId,
        workload_ids: tuple[WorkloadId, ...],
    ) -> ObservedSecretState:
        resource = self._secret_resource(secret_id)
        provider_name = provider_secret_id(str(secret_id))
        expected = self._expected_managed_members(workload_ids)
        try:
            secret = self._client.get_secret(
                secretmanager_v1.GetSecretRequest(name=resource)
            )
        except GoogleNotFound:
            return ObservedSecretState(
                name=resource,
                exists=False,
                exact_bindings=False,
                enabled_versions=(),
                disabled_versions=(),
                destroyed_versions=(),
            )
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None
        self._require_exact_secret(secret, resource=resource)
        try:
            versions = tuple(
                self._client.list_secret_versions(
                    secretmanager_v1.ListSecretVersionsRequest(parent=resource)
                )
            )
            policy = self._client.get_iam_policy(
                iam_policy_pb2.GetIamPolicyRequest(resource=resource)
            )
            exact_bindings = True
            try:
                self._require_exact_managed_bindings(policy, expected=expected)
            except SecretMetadataDeniedError:
                exact_bindings = False
            enabled: list[int] = []
            disabled: list[int] = []
            destroyed: list[int] = []
            for version in versions:
                parsed = _require_version_resource(
                    version.name,
                    project_id=self._project_id,
                    secret_name=provider_name,
                )
                state_name = _version_state_name(version.state)
                if state_name == "enabled":
                    enabled.append(parsed)
                elif state_name == "disabled":
                    disabled.append(parsed)
                elif state_name == "destroyed":
                    destroyed.append(parsed)
                else:
                    raise SecretManagerAdapterError(_FAILED)
            return ObservedSecretState(
                name=resource,
                exists=True,
                exact_bindings=exact_bindings,
                enabled_versions=tuple(sorted(enabled)),
                disabled_versions=tuple(sorted(disabled)),
                destroyed_versions=tuple(sorted(destroyed)),
            )
        except SecretManagerAdapterError:
            raise
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None

    def _load_record(self, secret_id: str) -> SecretMetadata:
        try:
            return self._store.get_secret_metadata(SecretId(secret_id))
        except NotFound:
            raise SecretMetadataDeniedError(_DENIED) from None
        except Exception:
            raise SecretMetadataDeniedError(_DENIED) from None

    @staticmethod
    def _validate_record(
        record: SecretMetadata,
        *,
        workload_id: WorkloadId,
        attachment: SecretAttachmentReference,
    ) -> None:
        if type(record) is not SecretMetadata:
            raise SecretMetadataDeniedError(_DENIED)
        if record.lifecycle_state is not SecretLifecycleState.ACTIVE:
            raise SecretMetadataDeniedError(_DENIED)
        if workload_id not in record.attached_workload_ids:
            raise SecretMetadataDeniedError(_DENIED)
        if record.active_version != attachment.secret_version:
            raise SecretMetadataDeniedError(_DENIED)
        if record.version != attachment.metadata_version:
            raise SecretMetadataDeniedError(_DENIED)

    def _audit_metadata(
        self,
        *,
        resource: str,
        expected_version: int,
        workload_ids: tuple[WorkloadId, ...],
    ) -> None:
        try:
            secret = self._client.get_secret(
                secretmanager_v1.GetSecretRequest(name=resource)
            )
            if secret.name != resource:
                raise SecretMetadataDeniedError(_DENIED)
            enabled = tuple(
                self._client.list_secret_versions(
                    secretmanager_v1.ListSecretVersionsRequest(
                        parent=resource,
                        filter="state:ENABLED",
                    )
                )
            )
            if len(enabled) != 1:
                raise SecretMetadataDeniedError(_DENIED)
            version = enabled[0]
            parsed_version = _require_version_resource(
                version.name,
                project_id=self._project_id,
                secret_name=resource.rsplit("/", 1)[1],
            )
            if (
                version.state is not secretmanager_v1.SecretVersion.State.ENABLED
                or parsed_version != expected_version
                or version.client_specified_payload_checksum is not True
            ):
                raise SecretMetadataDeniedError(_DENIED)
            policy = self._client.get_iam_policy(
                iam_policy_pb2.GetIamPolicyRequest(resource=resource)
            )
            self._require_exact_managed_bindings(
                policy,
                expected=self._expected_managed_members(workload_ids),
            )
        except SecretMetadataDeniedError:
            raise
        except Exception:
            raise SecretMetadataDeniedError(_DENIED) from None

    def _expected_managed_members(
        self,
        workload_ids: tuple[WorkloadId, ...],
    ) -> dict[str, frozenset[str]]:
        normalized_ids = _require_workload_ids(workload_ids)
        runtimes = frozenset(
            "serviceAccount:"
            + _runtime_service_account(
                workload_id=workload_id,
                project_id=self._project_id,
            )
            for workload_id in normalized_ids
        )
        return {
            _ACCESSOR_ROLE: runtimes,
            _METADATA_READER_ROLE: frozenset(
                {
                    (
                        "serviceAccount:"
                        f"mim-deploy-worker@{self._project_id}.iam.gserviceaccount.com"
                    )
                }
            ),
            _VERSION_MANAGER_ROLE: frozenset(
                {f"serviceAccount:{self._version_manager_service_account}"}
            ),
        }

    @staticmethod
    def _require_exact_managed_bindings(
        policy: policy_pb2.Policy,
        *,
        expected: dict[str, frozenset[str]],
    ) -> None:
        found: dict[str, list[policy_pb2.Binding]] = {
            role: [] for role in expected
        }
        for binding in policy.bindings:
            if binding.role not in found:
                raise SecretMetadataDeniedError(_DENIED)
            found[binding.role].append(binding)
        for role, expected_members in expected.items():
            bindings = found[role]
            if len(bindings) != 1:
                raise SecretMetadataDeniedError(_DENIED)
            binding = bindings[0]
            if binding.HasField("condition"):
                raise SecretMetadataDeniedError(_DENIED)
            if frozenset(binding.members) != expected_members:
                raise SecretMetadataDeniedError(_DENIED)

    def _secret_resource(self, secret_id: SecretId) -> str:
        provider_name = provider_secret_id(str(secret_id))
        return f"projects/{self._project_id}/secrets/{provider_name}"

    def _create_secret(
        self,
        *,
        resource: str,
        secret_id: SecretId,
    ) -> secretmanager_v1.Secret:
        provider_name = provider_secret_id(str(secret_id))
        request = secretmanager_v1.CreateSecretRequest(
            parent=f"projects/{self._project_id}",
            secret_id=provider_name,
            secret=secretmanager_v1.Secret(
                replication=secretmanager_v1.Replication(
                    automatic=secretmanager_v1.Replication.Automatic()
                ),
                labels=dict(_MIM_MANAGED_LABELS),
            ),
        )
        try:
            return self._client.create_secret(request)
        except GoogleAlreadyExists:
            try:
                return self._client.get_secret(
                    secretmanager_v1.GetSecretRequest(name=resource)
                )
            except Exception:
                raise SecretManagerAdapterError(_FAILED) from None
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None

    def _require_exact_secret(
        self,
        secret: object,
        *,
        resource: str,
    ) -> None:
        if not isinstance(secret, secretmanager_v1.Secret):
            raise SecretManagerAdapterError(_FAILED)
        if secret.name != resource:
            raise SecretManagerAdapterError(_FAILED)
        if tuple(sorted(dict(secret.labels).items())) != _MIM_MANAGED_LABELS:
            raise SecretManagerAdapterError(_FAILED)
        if secret.version_aliases:
            raise SecretManagerAdapterError(_FAILED)
        if secret.replication._pb.WhichOneof("replication") != "automatic":
            raise SecretManagerAdapterError(_FAILED)

    def _validate_retirement_target(
        self,
        *,
        secret_name: str,
        version_name: str,
        active_version: int,
        retirement_not_before: datetime,
        now: datetime,
        require_future_window: bool,
    ) -> int:
        _require_positive_version(active_version)
        _require_utc_datetime(retirement_not_before)
        _require_utc_datetime(now)
        try:
            version = _require_version_resource(
                version_name,
                project_id=self._project_id,
                secret_name=_require_secret_name(secret_name),
            )
        except SecretMetadataDeniedError:
            raise SecretManagerAdapterError(_FAILED) from None
        if version == active_version:
            raise SecretManagerAdapterError(_FAILED)
        if require_future_window:
            if retirement_not_before - now < _RETIREMENT_MINIMUM:
                raise SecretManagerAdapterError(_FAILED)
        else:
            if now < retirement_not_before:
                raise SecretManagerAdapterError(_FAILED)
        return version

    def _require_enabled_version(
        self,
        version_name: str,
        *,
        secret_name: str,
    ) -> SecretVersionMetadata:
        try:
            observed = self._client.get_secret_version(
                secretmanager_v1.GetSecretVersionRequest(name=version_name)
            )
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None
        version = _require_version_resource(
            observed.name,
            project_id=self._project_id,
            secret_name=_require_secret_name(secret_name),
        )
        if (
            observed.state is not secretmanager_v1.SecretVersion.State.ENABLED
            or observed.client_specified_payload_checksum is not True
        ):
            raise SecretManagerAdapterError(_FAILED)
        return SecretVersionMetadata(
            name=observed.name,
            version=version,
            state="enabled",
            checksum_verified=True,
        )

    def _get_version_state(
        self,
        version_name: str,
        *,
        secret_name: str,
    ) -> SecretVersionStateMetadata:
        try:
            observed = self._client.get_secret_version(
                secretmanager_v1.GetSecretVersionRequest(name=version_name)
            )
        except Exception:
            raise SecretManagerAdapterError(_FAILED) from None
        version = _require_version_resource(
            observed.name,
            project_id=self._project_id,
            secret_name=_require_secret_name(secret_name),
        )
        return SecretVersionStateMetadata(
            name=observed.name,
            version=version,
            state=_version_state_name(observed.state),
        )

    def _require_version_state(
        self,
        version_name: str,
        *,
        secret_name: str,
        expected_state: int,
    ) -> SecretVersionStateMetadata:
        observed = self._get_version_state(
            version_name,
            secret_name=secret_name,
        )
        if observed.state != _version_state_name(expected_state):
            raise SecretManagerAdapterError(_FAILED)
        return observed


def _require_project_id(value: object) -> str:
    if type(value) is not str or _PROJECT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("project_id is invalid.")
    return value


def _require_secret_name(value: object) -> str:
    if type(value) is not str or _SECRET_NAME_PATTERN.fullmatch(value) is None:
        raise SecretMetadataDeniedError(_DENIED)
    return value


def _require_service_account(value: object, *, project_id: str) -> str:
    if type(value) is not str:
        raise ValueError("service account is invalid.")
    match = _SERVICE_ACCOUNT_PATTERN.fullmatch(value)
    if match is None or match.group(1) != project_id:
        raise ValueError("service account is invalid.")
    return value


def _runtime_service_account(*, workload_id: WorkloadId, project_id: str) -> str:
    digest = hashlib.sha256(str(workload_id).encode("utf-8")).hexdigest()[:12]
    account = f"mim-wrk-{digest}@{project_id}.iam.gserviceaccount.com"
    return _require_service_account(account, project_id=project_id)


def _require_workload_ids(
    workload_ids: object,
) -> tuple[WorkloadId, ...]:
    if (
        type(workload_ids) is not tuple
        or not workload_ids
        or len(workload_ids) > _MAX_WORKLOAD_ATTACHMENTS
    ):
        raise SecretMetadataDeniedError(_DENIED)
    normalized: list[WorkloadId] = []
    seen: set[str] = set()
    for workload_id in workload_ids:
        if type(workload_id) is not str or not workload_id.strip():
            raise SecretMetadataDeniedError(_DENIED)
        if workload_id in seen:
            raise SecretMetadataDeniedError(_DENIED)
        seen.add(workload_id)
        normalized.append(WorkloadId(workload_id))
    return tuple(normalized)


def _require_version_resource(
    value: object,
    *,
    project_id: str,
    secret_name: str,
) -> int:
    if type(value) is not str:
        raise SecretMetadataDeniedError(_DENIED)
    match = _VERSION_RESOURCE_PATTERN.fullmatch(value)
    if (
        match is None
        or match.group(1) != project_id
        or match.group(2) != secret_name
    ):
        raise SecretMetadataDeniedError(_DENIED)
    return int(match.group(3))


def _require_positive_version(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise SecretManagerAdapterError(_FAILED)
    return value


def _require_utc_datetime(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise SecretManagerAdapterError(_FAILED)
    if value.utcoffset() != UTC.utcoffset(value):
        raise SecretManagerAdapterError(_FAILED)
    return value


def _version_state_name(
    value: int,
) -> str:
    mapping = {
        secretmanager_v1.SecretVersion.State.ENABLED: "enabled",
        secretmanager_v1.SecretVersion.State.DISABLED: "disabled",
        secretmanager_v1.SecretVersion.State.DESTROYED: "destroyed",
    }
    try:
        return mapping[value]
    except KeyError:
        raise SecretManagerAdapterError(_FAILED) from None
