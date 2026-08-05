"""Immutable application-hostname binding policy."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

from mim_control_plane.domain.models import (
    AppHostnameBinding,
    AppHostnameBindingState,
    Workload,
)
from mim_control_plane.domain.states import WorkloadKind
from mim_control_plane.ports.store import AlreadyExists, IdempotencyConflict, Store

APP_HOST_SUFFIX = "madup.app"
APP_HOST_REGION = "asia-northeast3"
_RESERVED_LABELS = frozenset(
    {"admin", "api", "app", "mail", "mim", "pop", "smtp", "www"}
)
_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SERVICE_RESOURCE_PATTERN = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/"
    + APP_HOST_REGION
    + r"/services/mim-svc-[0-9a-f]{12}$"
)


def workload_hash_suffix(workload_id: str) -> str:
    return hashlib.sha256(workload_id.encode("utf-8")).hexdigest()[:12]


def build_app_hostname(workload_name: str, workload_id: str) -> str:
    base = _slug_base(workload_name)
    suffix = workload_hash_suffix(workload_id)
    max_base = 63 - len(suffix) - 1
    trimmed = base[:max_base].rstrip("-") or "site"
    if trimmed in _RESERVED_LABELS:
        trimmed = "site"
    return f"{trimmed}-{suffix}.{APP_HOST_SUFFIX}"


def validate_app_public_host(public_host: str) -> str:
    if type(public_host) is not str or public_host != public_host.strip().casefold():
        raise ValueError("public_host must be exact lower-case text.")
    label, dot, suffix = public_host.partition(".")
    if not dot or suffix != APP_HOST_SUFFIX or "." in label:
        raise ValueError("public_host must be a first-level *.madup.app host.")
    if label in _RESERVED_LABELS or _LABEL_PATTERN.fullmatch(label) is None:
        raise ValueError("public_host must be a reviewed application hostname.")
    return public_host


def validate_service_resource(*, service_resource: str, workload_id: str) -> str:
    if _SERVICE_RESOURCE_PATTERN.fullmatch(service_resource) is None:
        raise ValueError("service_resource must be the exact Seoul Cloud Run service.")
    expected_suffix = workload_hash_suffix(workload_id)
    if not service_resource.endswith(f"/services/mim-svc-{expected_suffix}"):
        raise ValueError("service_resource must match the workload hash suffix.")
    return service_resource


def validate_service_uri(
    *,
    service_uri: str,
    workload_id: str,
    service_resource: str,
) -> str:
    if type(service_uri) is not str or service_uri != service_uri.strip():
        raise ValueError("service_uri must be exact text.")
    parsed = urlsplit(service_uri)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("service_uri must be credential-free HTTPS.")
    if parsed.port is not None or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("service_uri must not contain an explicit port or path.")
    hostname = parsed.hostname or ""
    service_name = _service_name_from_resource(service_resource)
    expected_prefix = f"mim-svc-{workload_hash_suffix(workload_id)}-"
    if (
        not hostname.endswith(".run.app")
        or not hostname.startswith(f"{service_name}-")
        or not hostname.startswith(expected_prefix)
    ):
        raise ValueError("service_uri must be the exact reviewed run.app origin.")
    return service_uri


class AppHostnameBindingService:
    def __init__(self, *, store: Store) -> None:
        self._store = store

    def create_active_binding(
        self,
        *,
        workload: Workload,
        service_resource: str,
        service_uri: str,
        now: datetime,
    ) -> AppHostnameBinding:
        if workload.kind not in (WorkloadKind.NEXTJS, WorkloadKind.STREAMLIT):
            raise ValueError("only web workloads can receive hostname bindings.")
        validated_service_resource = validate_service_resource(
            service_resource=service_resource,
            workload_id=str(workload.id),
        )
        binding = AppHostnameBinding(
            public_host=build_app_hostname(str(workload.name), str(workload.id)),
            workload_id=workload.id,
            owner_id=workload.owner_id,
            workload_kind=workload.kind,
            service_resource=validated_service_resource,
            upstream_url=validate_service_uri(
                service_uri=service_uri,
                workload_id=str(workload.id),
                service_resource=validated_service_resource,
            ),
            upstream_audience=validate_service_uri(
                service_uri=service_uri,
                workload_id=str(workload.id),
                service_resource=validated_service_resource,
            ),
            state=AppHostnameBindingState.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        try:
            return self._store.create_app_hostname_binding(binding)
        except (AlreadyExists, IdempotencyConflict):
            existing = self._store.get_app_hostname_binding(binding.public_host)
            if _same_binding_identity(existing, binding):
                if existing.state is AppHostnameBindingState.ACTIVE:
                    return existing
                if existing.state is AppHostnameBindingState.DISABLED:
                    reactivated = existing.transition_state(
                        AppHostnameBindingState.ACTIVE,
                        at=now,
                    )
                    return self._store.save_app_hostname_binding(
                        reactivated,
                        expected_version=existing.version,
                    )
                if existing.state is AppHostnameBindingState.RETIRED:
                    raise IdempotencyConflict(
                        "retired app hostname bindings must never be reused."
                    ) from None
                return existing
            raise IdempotencyConflict(
                "app hostname binding material conflicts."
            ) from None

    def transition_binding(
        self,
        *,
        public_host: str,
        target_state: AppHostnameBindingState,
        now: datetime,
    ) -> AppHostnameBinding:
        current = self._store.get_app_hostname_binding(
            validate_app_public_host(public_host)
        )
        updated = current.transition_state(target_state, at=now)
        return self._store.save_app_hostname_binding(
            updated,
            expected_version=current.version,
        )


def _slug_base(value: str) -> str:
    if type(value) is not str or not value.strip():
        return "site"
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    collapsed = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return collapsed or "site"


def _service_name_from_resource(service_resource: str) -> str:
    return service_resource.rsplit("/", 1)[-1]


def _same_binding_identity(
    existing: AppHostnameBinding,
    candidate: AppHostnameBinding,
) -> bool:
    return (
        existing.public_host == candidate.public_host
        and existing.workload_id == candidate.workload_id
        and existing.owner_id == candidate.owner_id
        and existing.workload_kind == candidate.workload_kind
        and existing.service_resource == candidate.service_resource
        and existing.upstream_url == candidate.upstream_url
        and existing.upstream_audience == candidate.upstream_audience
    )
