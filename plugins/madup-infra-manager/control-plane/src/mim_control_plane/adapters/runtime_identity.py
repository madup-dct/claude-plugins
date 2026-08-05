"""Strict keyless per-workload service-account provisioning."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from google.api_core.exceptions import AlreadyExists, NotFound
from google.iam.v1 import policy_pb2  # type: ignore[import-untyped]

from mim_control_plane.config import _validate_project_id
from mim_control_plane.domain.models import WorkloadId
from mim_control_plane.ports.execution import ExecutionPlaneError, RuntimeIdentityPort
from mim_control_plane.services.runtime_identity import runtime_identity_spec

_ACT_AS_ROLE = "roles/iam.serviceAccountUser"
_POLICY_VERSION = 3
_GENERIC_ERROR = "Runtime identity reconciliation failed."


class RuntimeIdentityAdapter(RuntimeIdentityPort):
    """Create or verify the only identity shape accepted by workload runtimes."""

    def __init__(
        self,
        *,
        project_id: str,
        iam_admin_client: Any,
        resource_manager_client: Any,
        retry_sleeper: Callable[[float], None] = time.sleep,
        read_attempts: int = 7,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        self._project_id = _validate_project_id(
            _require_exact_text(project_id, "project_id")
        )
        _require_methods(
            iam_admin_client,
            (
                "get_service_account",
                "create_service_account",
                "list_service_account_keys",
                "get_iam_policy",
                "set_iam_policy",
            ),
            "IAM Admin client",
        )
        _require_methods(
            resource_manager_client,
            ("get_iam_policy",),
            "Resource Manager client",
        )
        if not callable(retry_sleeper):
            raise ValueError("retry_sleeper must be callable.")
        if type(read_attempts) is not int or not 1 <= read_attempts <= 10:
            raise ValueError("read_attempts must be an integer from 1 through 10.")
        if (
            isinstance(retry_delay_seconds, bool)
            or not isinstance(retry_delay_seconds, int | float)
            or not 0 <= retry_delay_seconds <= 5
        ):
            raise ValueError("retry_delay_seconds must be between 0 and 5.")
        self._iam = iam_admin_client
        self._resources = resource_manager_client
        self._retry_sleeper = retry_sleeper
        self._read_attempts = read_attempts
        self._retry_delay_seconds = float(retry_delay_seconds)
        self._deployer_member = (
            "serviceAccount:"
            f"mim-deploy-worker@{self._project_id}.iam.gserviceaccount.com"
        )

    def ensure_exact(self, workload_id: WorkloadId) -> str:
        try:
            if type(workload_id) is not str:
                raise ValueError("workload_id must be exact text.")
            spec = runtime_identity_spec(
                project_id=self._project_id,
                workload_id=_require_exact_text(workload_id, "workload_id"),
            )
            account = self._get_or_create(spec)
            _require_exact_account(account, spec=spec, project_id=self._project_id)
            self._require_no_project_roles(
                email=spec.email,
                unique_id=_require_account_text(account, "unique_id"),
            )
            self._require_no_user_managed_keys(spec.name)
            self._reconcile_act_as_policy(spec.name)
            return spec.email
        except ExecutionPlaneError:
            raise
        except Exception:
            raise ExecutionPlaneError(_GENERIC_ERROR) from None

    def _get_or_create(self, spec: Any) -> object:
        try:
            return self._iam.get_service_account({"name": spec.name})
        except NotFound:
            pass
        try:
            self._iam.create_service_account(
                {
                    "name": f"projects/{self._project_id}",
                    "account_id": spec.account_id,
                    "service_account": {
                        "display_name": spec.display_name,
                        "description": spec.description,
                    },
                }
            )
        except AlreadyExists:
            pass
        return self._read_after_create(spec.name)

    def _read_after_create(self, name: str) -> object:
        for attempt in range(self._read_attempts):
            try:
                return self._iam.get_service_account({"name": name})
            except NotFound:
                if attempt + 1 == self._read_attempts:
                    raise
                delay = min(
                    self._retry_delay_seconds * (2**attempt),
                    5.0,
                )
                self._retry_sleeper(delay)
        raise AssertionError("unreachable service-account read loop")

    def _require_no_project_roles(self, *, email: str, unique_id: str) -> None:
        policy = self._resources.get_iam_policy(
            {
                "resource": f"projects/{self._project_id}",
                "options": {"requested_policy_version": _POLICY_VERSION},
            }
        )
        _require_policy(policy, "project IAM policy")
        prohibited = {
            f"serviceAccount:{email}",
            "allUsers",
            "allAuthenticatedUsers",
        }
        for binding in policy.bindings:
            for member in binding.members:
                if member in prohibited or (
                    member.startswith("principal://")
                    and member.endswith(f"/serviceAccounts/{unique_id}")
                ):
                    raise ExecutionPlaneError(
                        "Runtime identity must not hold a project IAM role."
                    )

    def _require_no_user_managed_keys(self, name: str) -> None:
        response = self._iam.list_service_account_keys(
            {
                "name": name,
                "key_types": ["USER_MANAGED"],
            }
        )
        keys = _field(response, "keys")
        if isinstance(keys, str) or not isinstance(keys, Sequence):
            raise ValueError("service-account keys response must contain a sequence.")
        if keys:
            raise ExecutionPlaneError(
                "Runtime identity must not have user-managed keys."
            )

    def _reconcile_act_as_policy(self, name: str) -> None:
        current = self._iam.get_iam_policy(
            {
                "resource": name,
                "options": {"requested_policy_version": _POLICY_VERSION},
            }
        )
        _require_policy(current, "service-account IAM policy")
        desired = policy_pb2.Policy(version=current.version, etag=current.etag)
        desired.bindings.add(
            role=_ACT_AS_ROLE,
            members=(self._deployer_member,),
        )
        written = self._iam.set_iam_policy(
            {
                "resource": name,
                "policy": desired,
            }
        )
        if not _has_exact_act_as_policy(written, member=self._deployer_member):
            raise ExecutionPlaneError(_GENERIC_ERROR)
        observed = self._iam.get_iam_policy(
            {
                "resource": name,
                "options": {"requested_policy_version": _POLICY_VERSION},
            }
        )
        if not _has_exact_act_as_policy(observed, member=self._deployer_member):
            raise ExecutionPlaneError(_GENERIC_ERROR)
        if written.etag and observed.etag != written.etag:
            raise ExecutionPlaneError(_GENERIC_ERROR)


def _require_exact_account(account: object, *, spec: Any, project_id: str) -> None:
    expected = {
        "name": spec.name,
        "project_id": project_id,
        "email": spec.email,
        "display_name": spec.display_name,
        "description": spec.description,
    }
    for field_name, expected_value in expected.items():
        if _require_account_text(account, field_name) != expected_value:
            raise ExecutionPlaneError("Runtime identity resource drifted.")
    if _field(account, "disabled") is not False:
        raise ExecutionPlaneError("Runtime identity resource drifted.")
    unique_id = _require_account_text(account, "unique_id")
    if not unique_id.isdigit() or int(unique_id) <= 0:
        raise ExecutionPlaneError("Runtime identity resource drifted.")


def _require_account_text(account: object, field_name: str) -> str:
    value = _field(account, field_name)
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"service account {field_name} must be exact text.")
    return value


def _field(value: object, field_name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(field_name)
    return getattr(value, field_name, None)


def _require_policy(value: object, field_name: str) -> policy_pb2.Policy:
    if not isinstance(value, policy_pb2.Policy):
        raise ValueError(f"{field_name} must be a Policy.")
    return value


def _has_exact_act_as_policy(value: object, *, member: str) -> bool:
    if not isinstance(value, policy_pb2.Policy):
        return False
    if len(value.bindings) != 1 or value.audit_configs:
        return False
    binding = value.bindings[0]
    return (
        binding.role == _ACT_AS_ROLE
        and list(binding.members) == [member]
        and not binding.HasField("condition")
    )


def _require_methods(value: object, methods: Sequence[str], label: str) -> None:
    if value is None or any(
        not callable(getattr(value, method, None)) for method in methods
    ):
        raise ValueError(f"{label} must be explicitly injected.")


def _require_exact_text(value: str, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} must be exact non-empty text.")
    return value
