"""Strict IAP access-policy adapter for private Cloud Run services."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from mim_control_plane.config import COMPANY_DOMAIN, _validate_project_id
from mim_control_plane.config import REGION as CONFIG_REGION
from mim_control_plane.domain.models import User, UserId
from mim_control_plane.domain.states import UserState
from mim_control_plane.ports.store import Store

_ACCESSOR_ROLE = "roles/iap.httpsResourceAccessor"
_GOOGLEAPIS_URL = "https://iap.googleapis.com/v1"
_POLICY_VERSION = 3
_SERVICE_NAME_PATTERN = re.compile(
    r"^projects/(?P<project_id>[^/]+)/locations/(?P<region>[^/]+)/services/"
    r"(?P<service_id>[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?)$"
)
_MEMBER_PATTERN = re.compile(
    rf"^(user|group):[a-z0-9._%+-]+@{re.escape(COMPANY_DOMAIN)}$"
)
_GENERIC_ENSURE_ERROR = "IAP access policy reconciliation failed."


class _Response(Protocol):
    status_code: int

    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class _HttpSession(Protocol):
    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object],
        timeout: float,
    ) -> _Response: ...


class IapAccessPolicyManager(Protocol):
    """Cloud Run-facing contract for exact per-service IAP access policy."""

    def ensure_exact_access(
        self,
        service_name: str,
        workload_owner_id: str,
    ) -> None: ...

    def verify_exact_access(
        self,
        service_name: str,
        workload_owner_id: str,
    ) -> bool: ...


class StoreIapPrincipalResolver:
    """Resolve the signed owner ID through the central identity store only."""

    def __init__(self, *, store: Store, admin_members: Sequence[str]) -> None:
        if store is None or not callable(getattr(store, "get_user", None)):
            raise ValueError("IAP principal resolver requires an identity store.")
        self._store = store
        self._admin_members = _normalize_members(
            admin_members,
            field_name="admin_members",
        )

    def __call__(self, *, workload_owner_id: str) -> Mapping[str, object]:
        owner_id = _require_exact_text(workload_owner_id, "workload_owner_id")
        try:
            user = self._store.get_user(UserId(owner_id))
        except Exception:
            raise ValueError("workload owner identity is unavailable.") from None
        if type(user) is not User or str(user.id) != owner_id:
            raise ValueError("workload owner identity is unavailable.")
        if user.state is not UserState.ACTIVE:
            raise ValueError("workload owner identity is unavailable.")
        owner_member = _normalize_member(
            f"user:{user.email}",
            field_name="owner_member",
        )
        if owner_member in self._admin_members:
            raise ValueError("owner member must not repeat in admin_members.")
        return {
            "owner_member": owner_member,
            "admin_members": self._admin_members,
        }


@dataclass(frozen=True, slots=True)
class _ResolvedPrincipals:
    owner_member: str
    admin_members: tuple[str, ...]

    @property
    def all_members(self) -> tuple[str, ...]:
        return (self.owner_member, *self.admin_members)


class IapAccessAdapter:
    def __init__(
        self,
        *,
        project_id: str,
        project_number: str,
        region: str,
        session: _HttpSession,
        principal_resolver: Callable[..., Mapping[str, object]],
        timeout: float = 10.0,
    ) -> None:
        self._project_id = _validate_project_id(
            _require_exact_text(project_id, "project_id")
        )
        self._project_number = _require_project_number(project_number)
        self._region = _require_exact_text(region, "region")
        if self._region != CONFIG_REGION:
            raise ValueError("IAP access adapter region must match configured REGION.")
        if session is None or not callable(getattr(session, "post", None)):
            raise ValueError(
                "IAP access adapter requires an injected HTTP session."
            )
        if principal_resolver is None or not callable(principal_resolver):
            raise ValueError(
                "IAP access adapter requires an injected principal resolver."
            )
        self._session = session
        self._principal_resolver = principal_resolver
        self._timeout = _require_timeout(timeout)

    def ensure_exact_access(self, service_name: str, workload_owner_id: str) -> None:
        try:
            resource = self._resource_name(service_name)
            principals = self._resolve_principals(workload_owner_id)
            current_policy = self._get_policy(resource)
            desired_policy = _authoritative_policy(current_policy, principals)
            _require_exact_policy(desired_policy, principals)
            set_policy = self._set_policy(resource, desired_policy)
            _require_exact_policy(set_policy, principals)
            _require_semantic_policy_match(
                expected_policy=desired_policy,
                observed_policy=set_policy,
            )
            readback_policy = self._get_policy(resource)
            _require_exact_policy(readback_policy, principals)
            _require_semantic_policy_match(
                expected_policy=desired_policy,
                observed_policy=readback_policy,
            )
            if readback_policy["etag"] != set_policy["etag"]:
                raise ValueError("policy readback etag drifted")
        except Exception:
            raise RuntimeError(_GENERIC_ENSURE_ERROR) from None

    def verify_exact_access(self, service_name: str, workload_owner_id: str) -> bool:
        try:
            resource = self._resource_name(service_name)
            principals = self._resolve_principals(workload_owner_id)
            policy = self._get_policy(resource)
            _require_exact_policy(policy, principals)
            return True
        except Exception:
            return False

    def _resource_name(self, service_name: str) -> str:
        normalized_service_name = _require_exact_text(service_name, "service_name")
        match = _SERVICE_NAME_PATTERN.fullmatch(normalized_service_name)
        if match is None:
            raise ValueError("service_name must be a Cloud Run service resource name.")
        if match.group("project_id") != self._project_id:
            raise ValueError("service_name project must match the configured project.")
        if match.group("region") != self._region:
            raise ValueError("service_name region must match the configured region.")
        service_id = match.group("service_id")
        return (
            f"projects/{self._project_number}/iap_web/cloud_run-{self._region}/"
            f"services/{service_id}"
        )

    def _resolve_principals(self, workload_owner_id: str) -> _ResolvedPrincipals:
        resolved = self._principal_resolver(
            workload_owner_id=_require_exact_text(
                workload_owner_id,
                "workload_owner_id",
            )
        )
        return _normalize_principals(resolved)

    def _get_policy(self, resource_name: str) -> dict[str, object]:
        return self._post_json(
            f"{_GOOGLEAPIS_URL}/{resource_name}:getIamPolicy",
            {"options": {"requestedPolicyVersion": _POLICY_VERSION}},
        )

    def _set_policy(
        self,
        resource_name: str,
        policy: Mapping[str, object],
    ) -> dict[str, object]:
        return self._post_json(
            f"{_GOOGLEAPIS_URL}/{resource_name}:setIamPolicy",
            {"policy": copy.deepcopy(dict(policy))},
        )

    def _post_json(
        self,
        url: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        response = self._session.post(url, json=payload, timeout=self._timeout)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, Mapping):
            raise ValueError("IAP policy response must be a JSON object.")
        return copy.deepcopy(dict(body))


def _authoritative_policy(
    current_policy: Mapping[str, object],
    principals: _ResolvedPrincipals,
) -> dict[str, object]:
    etag, version = _require_policy_header(current_policy)
    return {
        "version": version,
        "etag": etag,
        "bindings": [
            {
                "role": _ACCESSOR_ROLE,
                "members": list(principals.all_members),
            }
        ],
    }


def _require_exact_policy(
    policy: Mapping[str, object],
    principals: _ResolvedPrincipals,
) -> None:
    if set(policy.keys()) != {"version", "etag", "bindings"}:
        raise ValueError("policy must contain only version, etag, and bindings.")
    _require_policy_header(policy)
    bindings = _require_bindings(policy)
    if len(bindings) != 1:
        raise ValueError("policy must contain exactly one binding.")
    binding = bindings[0]
    if binding.get("role") != _ACCESSOR_ROLE:
        raise ValueError("policy must contain only the accessor binding.")
    if set(binding.keys()) != {"role", "members"}:
        raise ValueError("accessor binding must not be conditional.")
    members = binding.get("members")
    if not isinstance(members, list) or any(
        not isinstance(item, str) for item in members
    ):
        raise ValueError("accessor members must be a string list.")
    if len(set(members)) != len(members):
        raise ValueError("accessor binding members must not repeat.")
    if frozenset(members) != frozenset(principals.all_members):
        raise ValueError("accessor binding members drifted.")


def _require_semantic_policy_match(
    *,
    expected_policy: Mapping[str, object],
    observed_policy: Mapping[str, object],
) -> None:
    expected_etag = expected_policy.get("etag")
    observed_etag = observed_policy.get("etag")
    if not isinstance(expected_etag, str) or not expected_etag:
        raise ValueError("expected policy etag must be non-empty.")
    if not isinstance(observed_etag, str) or not observed_etag:
        raise ValueError("observed policy etag must be non-empty.")
    if _canonical_policy(expected_policy) != _canonical_policy(observed_policy):
        raise ValueError("policy drifted semantically.")


def _canonical_policy(policy: Mapping[str, object]) -> dict[str, object]:
    canonical = _copy_policy(policy)
    canonical.pop("etag", None)
    bindings = _require_bindings(canonical)
    if len(bindings) != 1:
        raise ValueError("policy must contain exactly one binding.")
    members = bindings[0].get("members")
    if not isinstance(members, list) or any(
        not isinstance(member, str) for member in members
    ):
        raise ValueError("accessor members must be a string list.")
    canonical["bindings"] = [
        {
            "role": _ACCESSOR_ROLE,
            "members": sorted(members),
        }
    ]
    return canonical


def _require_policy_header(policy: Mapping[str, object]) -> tuple[str, int]:
    etag = policy.get("etag")
    if not isinstance(etag, str) or not etag:
        raise ValueError("policy etag must be present and non-empty.")
    version = policy.get("version")
    if type(version) is not int or version not in {1, _POLICY_VERSION}:
        raise ValueError("policy version must be 1 or 3.")
    return etag, version


def _copy_policy(policy: Mapping[str, object]) -> dict[str, object]:
    copied = copy.deepcopy(dict(policy))
    if not isinstance(copied, dict):
        raise ValueError("policy must be a JSON object.")
    return copied


def _require_bindings(policy: Mapping[str, object]) -> list[dict[str, object]]:
    raw_bindings = policy.get("bindings")
    if not isinstance(raw_bindings, list):
        raise ValueError("policy bindings must be a list.")
    bindings: list[dict[str, object]] = []
    for raw_binding in raw_bindings:
        if not isinstance(raw_binding, Mapping):
            raise ValueError("policy binding must be an object.")
        bindings.append(copy.deepcopy(dict(raw_binding)))
    return bindings


def _normalize_principals(resolved: Mapping[str, object]) -> _ResolvedPrincipals:
    if set(resolved.keys()) != {"owner_member", "admin_members"}:
        raise ValueError("resolver must return only owner_member and admin_members.")
    owner_member = _normalize_member(
        resolved["owner_member"],
        field_name="owner_member",
    )
    admin_members = _normalize_members(
        resolved["admin_members"],
        field_name="admin_members",
    )
    if owner_member in admin_members:
        raise ValueError("owner member must not repeat in admin_members.")
    if len(set((owner_member, *admin_members))) != 1 + len(admin_members):
        raise ValueError("members must be unique.")
    return _ResolvedPrincipals(
        owner_member=owner_member,
        admin_members=admin_members,
    )


def _normalize_members(value: object, *, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a non-empty sequence.")
    normalized = tuple(
        _normalize_member(member, field_name=field_name) for member in value
    )
    if not normalized:
        raise ValueError(f"{field_name} must contain at least one admin member.")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicate members.")
    return normalized


def _normalize_member(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    normalized = _require_exact_text(value, field_name).casefold()
    if not normalized or _MEMBER_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be an exact company user/group member.")
    return normalized


def _require_project_number(value: str) -> str:
    project_number = _require_exact_text(value, "project_number")
    if (
        not project_number.isdigit()
        or int(project_number) <= 0
        or (len(project_number) > 1 and project_number.startswith("0"))
    ):
        raise ValueError("project_number must be a non-zero numeric string.")
    return project_number


def _require_exact_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a non-empty string.")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be exact non-empty text.")
    return value


def _require_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError("timeout must be a positive number.")
    return float(value)
