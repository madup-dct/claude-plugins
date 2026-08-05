"""Artifact Registry tag retention for MIM-managed workload images."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mim_control_plane.config import REGION as CONFIG_REGION
from mim_control_plane.ports.execution import ArtifactRegistryPort, ExecutionPlaneError
from mim_control_plane.services.render import ARTIFACT_IMAGE_NAME, ARTIFACT_REPOSITORY

_CENTRAL_PROJECT_ID = "mim-prod-123456"
_DIGEST_CHARS = frozenset("0123456789abcdef")
_FAILED = "Artifact Registry retention failed."


class ArtifactRegistryAdapterError(ExecutionPlaneError):
    """Raised when Artifact Registry state cannot be proven safe."""


class ArtifactRegistryClient(Protocol):
    def get_tag(self, *, name: str) -> object: ...
    def create_tag(self, *, parent: str, tag: object, tag_id: str) -> object: ...
    def delete_tag(self, *, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _ManagedTag:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class ArtifactRegistryAdapter(ArtifactRegistryPort):
    client: ArtifactRegistryClient
    project_id: str
    region: str

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or self.project_id != _CENTRAL_PROJECT_ID:
            raise ValueError("Artifact Registry project is invalid.")
        if type(self.region) is not str or self.region != CONFIG_REGION:
            raise ValueError("Artifact Registry region is invalid.")
        if not all(
            callable(getattr(self.client, method, None))
            for method in ("get_tag", "create_tag", "delete_tag")
        ):
            raise ValueError("Artifact Registry client must expose exact tag methods.")

    def retain(self, image_digest: str) -> str:
        digest = _require_digest(image_digest)
        desired_name = self._tag_name(digest)
        desired_version = self._version_name(digest)
        try:
            existing = self.client.get_tag(name=desired_name)
        except Exception as exc:
            if _is_missing(exc):
                return self._create_or_replay(digest)
            raise ArtifactRegistryAdapterError(_FAILED) from None
        current_name, current_version = _coerce_tag(existing)
        if current_name == desired_name and current_version == desired_version:
            return digest
        try:
            self.client.delete_tag(name=desired_name)
            return self._create_or_replay(digest)
        except Exception:
            raise ArtifactRegistryAdapterError(_FAILED) from None

    def _create_or_replay(self, digest: str) -> str:
        desired_name = self._tag_name(digest)
        desired_version = self._version_name(digest)
        tag = _ManagedTag(name=desired_name, version=desired_version)
        try:
            created = self.client.create_tag(
                parent=self._package_parent,
                tag=tag,
                tag_id=f"sha256-{digest}",
            )
        except Exception:
            try:
                created = self.client.get_tag(name=desired_name)
            except Exception:
                raise ArtifactRegistryAdapterError(_FAILED) from None
        created_name, created_version = _coerce_tag(created)
        if created_name != desired_name or created_version != desired_version:
            raise ArtifactRegistryAdapterError(_FAILED)
        return digest

    @property
    def _package_parent(self) -> str:
        return (
            f"projects/{self.project_id}/locations/{self.region}/repositories/"
            f"{ARTIFACT_REPOSITORY}/packages/{ARTIFACT_IMAGE_NAME}"
        )

    def _tag_name(self, digest: str) -> str:
        return f"{self._package_parent}/tags/sha256-{digest}"

    def _version_name(self, digest: str) -> str:
        return f"{self._package_parent}/versions/sha256:{digest}"


def _require_digest(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in _DIGEST_CHARS for char in value)
    ):
        raise ArtifactRegistryAdapterError(_FAILED)
    return value


def _coerce_tag(value: object) -> tuple[str, str]:
    name = getattr(value, "name", None)
    version = getattr(value, "version", None)
    if type(name) is not str or type(version) is not str:
        raise ArtifactRegistryAdapterError(_FAILED)
    return name, version


def _is_missing(exc: Exception) -> bool:
    return isinstance(exc, LookupError) or type(exc).__name__ in {
        "NotFound",
        "ResourceNotFound",
    }
