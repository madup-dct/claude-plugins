"""Authorized REST helpers for Google APIs without extra client libraries."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlencode

from google.auth.transport.requests import AuthorizedSession

_IAM_BASE_URL = "https://iam.googleapis.com/v1"
_RESOURCE_MANAGER_BASE_URL = "https://cloudresourcemanager.googleapis.com/v1"
_ARTIFACT_REGISTRY_BASE_URL = "https://artifactregistry.googleapis.com/v1"


def build_authorized_session(*, credentials: object) -> AuthorizedSession:
    return AuthorizedSession(credentials)


@dataclass(frozen=True, slots=True)
class IamAdminRestClient:
    session: AuthorizedSession
    timeout: float = 10.0

    def get_service_account(self, request: dict[str, object]) -> Mapping[str, object]:
        name = _require_exact_text(request.get("name"), "name")
        response = self.session.get(
            f"{_IAM_BASE_URL}/{name}",
            timeout=self.timeout,
        )
        return _json_body(response)

    def create_service_account(
        self,
        request: dict[str, object],
    ) -> Mapping[str, object]:
        name = _require_exact_text(request.get("name"), "name")
        account_id = _require_exact_text(request.get("account_id"), "account_id")
        service_account = _require_mapping(
            request.get("service_account"),
            "service_account",
        )
        response = self.session.post(
            f"{_IAM_BASE_URL}/{name}/serviceAccounts",
            json={
                "accountId": account_id,
                "serviceAccount": {
                    "displayName": _require_exact_text(
                        service_account.get("display_name"),
                        "display_name",
                    ),
                    "description": _require_exact_text(
                        service_account.get("description"),
                        "description",
                    ),
                },
            },
            timeout=self.timeout,
        )
        return _json_body(response)

    def list_service_account_keys(
        self,
        request: dict[str, object],
    ) -> Mapping[str, object]:
        name = _require_exact_text(request.get("name"), "name")
        key_types = request.get("key_types")
        if not isinstance(key_types, list) or any(
            type(item) is not str for item in key_types
        ):
            raise ValueError("key_types must be an exact list of strings.")
        query = urlencode([("keyTypes", item) for item in key_types])
        response = self.session.get(
            f"{_IAM_BASE_URL}/{name}/keys?{query}",
            timeout=self.timeout,
        )
        return _json_body(response)

    def get_iam_policy(self, request: dict[str, object]) -> object:
        resource = _require_exact_text(request.get("resource"), "resource")
        options = _require_mapping(request.get("options"), "options")
        response = self.session.post(
            f"{_IAM_BASE_URL}/{resource}:getIamPolicy",
            json={
                "options": {
                    "requestedPolicyVersion": _require_int(
                        options.get("requested_policy_version"),
                        "requested_policy_version",
                    ),
                }
            },
            timeout=self.timeout,
        )
        return _policy_from_response(response)

    def set_iam_policy(self, request: dict[str, object]) -> object:
        resource = _require_exact_text(request.get("resource"), "resource")
        policy = request.get("policy")
        if policy is None:
            raise ValueError("policy must be an exact Policy.")
        message_to_dict = _message_to_dict()
        response = self.session.post(
            f"{_IAM_BASE_URL}/{resource}:setIamPolicy",
            json={"policy": message_to_dict(policy)},
            timeout=self.timeout,
        )
        return _policy_from_response(response)


@dataclass(frozen=True, slots=True)
class ResourceManagerRestClient:
    session: AuthorizedSession
    timeout: float = 10.0

    def get_iam_policy(self, request: dict[str, object]) -> object:
        resource = _require_exact_text(request.get("resource"), "resource")
        options = _require_mapping(request.get("options"), "options")
        response = self.session.post(
            f"{_RESOURCE_MANAGER_BASE_URL}/{resource}:getIamPolicy",
            json={
                "options": {
                    "requestedPolicyVersion": _require_int(
                        options.get("requested_policy_version"),
                        "requested_policy_version",
                    ),
                }
            },
            timeout=self.timeout,
        )
        return _policy_from_response(response)


@dataclass(frozen=True, slots=True)
class ArtifactRegistryRestClient:
    session: AuthorizedSession
    timeout: float = 10.0

    def get_tag(self, *, name: str) -> object:
        response = self.session.get(
            f"{_ARTIFACT_REGISTRY_BASE_URL}/{_require_exact_text(name, 'name')}",
            timeout=self.timeout,
        )
        return _object_from_response(response)

    def create_tag(self, *, parent: str, tag: object, tag_id: str) -> object:
        payload = {
            "name": _require_exact_text(getattr(tag, "name", None), "tag.name"),
            "version": _require_exact_text(
                getattr(tag, "version", None),
                "tag.version",
            ),
        }
        parent_name = _require_exact_text(parent, "parent")
        stable_tag_id = _require_exact_text(tag_id, "tag_id")
        response = self.session.post(
            f"{_ARTIFACT_REGISTRY_BASE_URL}/{parent_name}/tags?tagId={stable_tag_id}",
            json=payload,
            timeout=self.timeout,
        )
        return _object_from_response(response)

    def delete_tag(self, *, name: str) -> None:
        response = self.session.delete(
            f"{_ARTIFACT_REGISTRY_BASE_URL}/{_require_exact_text(name, 'name')}",
            timeout=self.timeout,
        )
        response.raise_for_status()


def _policy_from_response(response: Any) -> object:
    body = _json_body(response)
    policy = _new_policy()
    return _parse_dict(dict(body), policy)


def _object_from_response(response: Any) -> object:
    return SimpleNamespace(**_json_body(response))


def _json_body(response: Any) -> Mapping[str, object]:
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, Mapping):
        raise ValueError("response body must be a JSON object.")
    return dict(body)


def _require_exact_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} must be exact text.")
    return value


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping.")
    return value


def _require_int(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field_name} must be an exact int.")
    return value


def _message_to_dict() -> Any:
    module = importlib.import_module("google.protobuf.json_format")
    return getattr(module, "MessageToDict")


def _parse_dict(payload: dict[str, object], policy: object) -> object:
    module = importlib.import_module("google.protobuf.json_format")
    parser = getattr(module, "ParseDict")
    return parser(payload, policy)


def _new_policy() -> object:
    module = importlib.import_module("google.iam.v1.policy_pb2")
    policy_class = getattr(module, "Policy")
    return policy_class()
