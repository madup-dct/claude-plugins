"""Regional Cloud Build adapter for immutable, admitted GitHub source."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Protocol

from google.cloud.devtools import cloudbuild_v1
from google.protobuf import duration_pb2  # type: ignore[import-untyped]

from mim_control_plane.config import GITHUB_OWNER, REGION
from mim_control_plane.ports.execution import BuildPort, BuildRequest
from mim_control_plane.services.build_template import build_template_for
from mim_control_plane.services.render import (
    ARTIFACT_IMAGE_NAME,
    ARTIFACT_REPOSITORY,
)


class CloudBuildError(RuntimeError):
    """Raised when a build cannot be proven to match its trusted request."""


_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_RESOURCE_SEGMENT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_REPOSITORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUILD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class BuildOperation(Protocol):
    def result(self, *, timeout: float) -> cloudbuild_v1.Build: ...


class CloudBuildClient(Protocol):
    def create_build(
        self,
        *,
        request: cloudbuild_v1.CreateBuildRequest,
    ) -> BuildOperation: ...


@dataclass(frozen=True, slots=True)
class ConnectedRepositoryBinding:
    repository_numeric_id: int
    owner: str
    name: str
    installation_id: int
    repository_resource: str

    def __post_init__(self) -> None:
        if (
            type(self.repository_numeric_id) is not int
            or self.repository_numeric_id < 1
            or type(self.installation_id) is not int
            or self.installation_id < 1
        ):
            raise ValueError("repository binding IDs must be positive integers")
        if self.owner != GITHUB_OWNER:
            raise ValueError("repository binding owner is not approved")
        if (
            type(self.name) is not str
            or _REPOSITORY_NAME_PATTERN.fullmatch(self.name) is None
        ):
            raise ValueError("repository binding name is invalid")
        if type(self.repository_resource) is not str:
            raise ValueError("connected repository resource is invalid")


@dataclass(frozen=True, slots=True)
class CloudBuildAdapter(BuildPort):
    project_id: str
    region: str
    build_service_account: str
    builder_image: str
    bindings: tuple[ConnectedRepositoryBinding, ...] = field(repr=False)
    client: CloudBuildClient = field(repr=False)
    operation_timeout_seconds: float = 900.0

    def __post_init__(self) -> None:
        if (
            type(self.project_id) is not str
            or _PROJECT_ID_PATTERN.fullmatch(self.project_id) is None
        ):
            raise ValueError("Cloud Build project is invalid")
        if self.region != REGION:
            raise ValueError("Cloud Build region is not approved")
        self._validate_service_account()
        self._validate_builder_image()
        if type(self.bindings) is not tuple or not self.bindings:
            raise ValueError("at least one connected repository binding is required")
        repository_ids: set[int] = set()
        resources: set[str] = set()
        for binding in self.bindings:
            if type(binding) is not ConnectedRepositoryBinding:
                raise ValueError("connected repository bindings must be exact")
            self._validate_repository_resource(binding.repository_resource)
            if (
                binding.repository_numeric_id in repository_ids
                or binding.repository_resource in resources
            ):
                raise ValueError("connected repository bindings must be unique")
            repository_ids.add(binding.repository_numeric_id)
            resources.add(binding.repository_resource)
        if not isinstance(self.operation_timeout_seconds, float) or not (
            1.0 <= self.operation_timeout_seconds <= 3600.0
        ):
            raise ValueError("Cloud Build operation timeout is invalid")

    def build(self, request: BuildRequest) -> str:
        try:
            if type(request) is not BuildRequest:
                raise ValueError("build request must be exact")
            if request.source.owner != GITHUB_OWNER:
                raise ValueError("build source owner is not approved")
            if request.template != build_template_for(request.classification):
                raise ValueError("build template is not trusted")
            binding = self._binding_for(request)
            output_image = self._output_image_for(request)
            submitted = cloudbuild_v1.Build(
                source=cloudbuild_v1.Source(
                    connected_repository=cloudbuild_v1.ConnectedRepository(
                        repository=binding.repository_resource,
                        revision=request.source_sha,
                    )
                ),
                steps=(
                    cloudbuild_v1.BuildStep(
                        name=self.builder_image,
                        args=(
                            "--kind",
                            request.classification.kind.value,
                            "--runtime",
                            request.template.runtime,
                            "--entrypoint",
                            request.classification.entrypoint,
                            "--source-sha",
                            request.source_sha,
                            "--destination",
                            output_image,
                        ),
                    ),
                ),
                images=(output_image,),
                options=cloudbuild_v1.BuildOptions(
                    logging=cloudbuild_v1.BuildOptions.LoggingMode.CLOUD_LOGGING_ONLY,
                ),
                service_account=self.build_service_account,
                timeout=duration_pb2.Duration(
                    seconds=int(self.operation_timeout_seconds)
                ),
                queue_ttl=duration_pb2.Duration(seconds=300),
                tags=("mim-user-build",),
            )
            create_request = cloudbuild_v1.CreateBuildRequest(
                parent=f"projects/{self.project_id}/locations/{self.region}",
                build=submitted,
            )
            operation = self.client.create_build(request=create_request)
            completed = operation.result(timeout=self.operation_timeout_seconds)
            return self._verified_digest(
                completed=completed,
                binding=binding,
                output_image=output_image,
                source_sha=request.source_sha,
            )
        except Exception:
            raise CloudBuildError("Cloud Build execution failed.") from None

    def _binding_for(self, request: BuildRequest) -> ConnectedRepositoryBinding:
        matches = tuple(
            binding
            for binding in self.bindings
            if binding.repository_numeric_id == request.source.repository_numeric_id
        )
        if len(matches) != 1:
            raise ValueError("build source has no exact binding")
        binding = matches[0]
        if (
            binding.owner != request.source.owner
            or binding.name != request.source.name
            or binding.installation_id != request.source.installation_id
        ):
            raise ValueError("build source does not match its binding")
        return binding

    def _output_image_for(self, request: BuildRequest) -> str:
        workload_key = hashlib.sha256(
            str(request.workload_id).encode("utf-8")
        ).hexdigest()[:12]
        operation_key = hashlib.sha256(
            str(request.operation_id).encode("utf-8")
        ).hexdigest()[:12]
        tag = (
            f"w-{workload_key}-sha-{request.source_sha[:12]}-op-{operation_key}"
        )
        return (
            f"{self.region}-docker.pkg.dev/{self.project_id}/"
            f"{ARTIFACT_REPOSITORY}/{ARTIFACT_IMAGE_NAME}:{tag}"
        )

    def _verified_digest(
        self,
        *,
        completed: cloudbuild_v1.Build,
        binding: ConnectedRepositoryBinding,
        output_image: str,
        source_sha: str,
    ) -> str:
        if type(completed) is not cloudbuild_v1.Build:
            raise ValueError("Cloud Build result is invalid")
        if completed.status != cloudbuild_v1.Build.Status.SUCCESS:
            raise ValueError("Cloud Build did not succeed")
        if completed.project_id != self.project_id:
            raise ValueError("Cloud Build result project changed")
        build_prefix = f"projects/{self.project_id}/locations/{self.region}/builds/"
        build_id = completed.name.removeprefix(build_prefix)
        if (
            not completed.name.startswith(build_prefix)
            or _BUILD_ID_PATTERN.fullmatch(build_id) is None
        ):
            raise ValueError("Cloud Build result name is invalid")
        resolved = completed.source_provenance.resolved_connected_repository
        if (
            resolved.repository != binding.repository_resource
            or resolved.revision != source_sha
        ):
            raise ValueError("Cloud Build resolved source changed")
        images = tuple(completed.results.images)
        if len(images) != 1 or images[0].name != output_image:
            raise ValueError("Cloud Build image result is ambiguous")
        digest = images[0].digest
        if _SHA256_DIGEST_PATTERN.fullmatch(digest) is None:
            raise ValueError("Cloud Build image digest is invalid")
        return digest.removeprefix("sha256:")

    def _validate_repository_resource(self, resource: str) -> None:
        prefix = f"projects/{self.project_id}/locations/{self.region}/connections/"
        if not resource.startswith(prefix):
            raise ValueError("connected repository is outside the build region")
        remainder = resource.removeprefix(prefix)
        parts = remainder.split("/")
        if (
            len(parts) != 3
            or parts[1] != "repositories"
            or _RESOURCE_SEGMENT_PATTERN.fullmatch(parts[0]) is None
            or _RESOURCE_SEGMENT_PATTERN.fullmatch(parts[2]) is None
        ):
            raise ValueError("connected repository resource is invalid")

    def _validate_service_account(self) -> None:
        expected_prefix = f"projects/{self.project_id}/serviceAccounts/"
        if not self.build_service_account.startswith(expected_prefix):
            raise ValueError("build service account is outside the build project")
        email = self.build_service_account.removeprefix(expected_prefix)
        if not email.endswith(f"@{self.project_id}.iam.gserviceaccount.com"):
            raise ValueError("build service account is invalid")
        local_part = email.split("@", 1)[0]
        if local_part != "mim-build":
            raise ValueError("build service account is not dedicated")

    def _validate_builder_image(self) -> None:
        prefix = (
            f"{self.region}-docker.pkg.dev/{self.project_id}/"
            "mim-platform/"
        )
        if not self.builder_image.startswith(prefix):
            raise ValueError("builder image is outside the platform repository")
        image_and_digest = self.builder_image.removeprefix(prefix)
        image_name, separator, digest = image_and_digest.partition("@")
        if (
            not separator
            or _RESOURCE_SEGMENT_PATTERN.fullmatch(image_name) is None
            or _SHA256_DIGEST_PATTERN.fullmatch(digest) is None
        ):
            raise ValueError("builder image must be pinned by digest")
