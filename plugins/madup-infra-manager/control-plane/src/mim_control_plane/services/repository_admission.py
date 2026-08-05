"""Admission checks for immutable approved application repositories."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mim_control_plane.config import GITHUB_OWNER


class RepositoryAdmissionError(ValueError):
    """Raised when source repository metadata falls outside platform policy."""


_OWNER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class SelectedRepositoryPolicy:
    allowed_repository_ids: frozenset[int]
    installation_id: int
    owner: str = field(init=False, default=GITHUB_OWNER)

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_repository_ids, frozenset):
            raise RepositoryAdmissionError(
                "Selected repository IDs must be a frozenset."
            )
        if not self.allowed_repository_ids:
            raise RepositoryAdmissionError(
                "At least one selected repository ID is required."
            )
        for repository_id in self.allowed_repository_ids:
            if isinstance(repository_id, bool) or not isinstance(repository_id, int):
                raise RepositoryAdmissionError(
                    "Selected repository IDs must be positive integers."
                )
            if repository_id <= 0:
                raise RepositoryAdmissionError(
                    "Selected repository IDs must be positive integers."
                )
        if isinstance(self.installation_id, bool) or not isinstance(
            self.installation_id, int
        ):
            raise RepositoryAdmissionError(
                "Installation ID must be a positive integer."
            )
        if self.installation_id <= 0:
            raise RepositoryAdmissionError(
                "Installation ID must be a positive integer."
            )
        if self.owner != GITHUB_OWNER:
            raise RepositoryAdmissionError(
                "Repository owner must match platform policy."
            )


@dataclass(frozen=True, slots=True)
class RepositoryCandidate:
    repository_numeric_id: int
    owner: str
    name: str
    installation_id: int
    requested_ref: str
    resolved_sha: str
    is_fork: bool
    redirected_from: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.is_fork, bool):
            raise RepositoryAdmissionError("Fork state must be boolean.")
        if isinstance(self.repository_numeric_id, bool) or not isinstance(
            self.repository_numeric_id, int
        ):
            raise RepositoryAdmissionError("Repository ID must be positive.")
        if self.repository_numeric_id <= 0:
            raise RepositoryAdmissionError("Repository ID must be positive.")
        if isinstance(self.installation_id, bool) or not isinstance(
            self.installation_id, int
        ):
            raise RepositoryAdmissionError("Installation ID must be positive.")
        if self.installation_id <= 0:
            raise RepositoryAdmissionError("Installation ID must be positive.")
        if (
            not isinstance(self.owner, str)
            or _OWNER_PATTERN.fullmatch(self.owner) is None
        ):
            raise RepositoryAdmissionError("Repository owner is invalid.")
        if not isinstance(self.name, str) or _NAME_PATTERN.fullmatch(self.name) is None:
            raise RepositoryAdmissionError("Repository name is invalid.")
        if (
            not isinstance(self.requested_ref, str)
            or _SHA_PATTERN.fullmatch(self.requested_ref) is None
        ):
            raise RepositoryAdmissionError("Requested ref must be an exact SHA.")
        if self.requested_ref == "0" * 40:
            raise RepositoryAdmissionError("Requested ref must be a non-zero SHA.")
        if (
            not isinstance(self.resolved_sha, str)
            or _SHA_PATTERN.fullmatch(self.resolved_sha) is None
        ):
            raise RepositoryAdmissionError("Resolved SHA must be exact.")
        if self.resolved_sha == "0" * 40:
            raise RepositoryAdmissionError("Resolved SHA must be a non-zero SHA.")
        if self.redirected_from is not None and not isinstance(
            self.redirected_from, str
        ):
            raise RepositoryAdmissionError("Redirect source must be a string.")
        if self.redirected_from is not None and not self.redirected_from.strip():
            raise RepositoryAdmissionError("Redirect source must not be empty.")

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class AdmittedRepository:
    repository_numeric_id: int
    owner: str
    name: str
    installation_id: int
    sha: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


def admit_repository(
    policy: SelectedRepositoryPolicy,
    candidate: RepositoryCandidate,
) -> AdmittedRepository:
    if candidate.full_name == "madup-dct/claude-plugins":
        raise RepositoryAdmissionError("Platform repositories are not deployable.")
    if candidate.owner != policy.owner:
        raise RepositoryAdmissionError("Repository owner is not approved.")
    if candidate.is_fork:
        raise RepositoryAdmissionError("Fork repositories are not approved.")
    if candidate.redirected_from is not None:
        raise RepositoryAdmissionError("Redirected repositories are not approved.")
    if candidate.repository_numeric_id not in policy.allowed_repository_ids:
        raise RepositoryAdmissionError("Repository ID is not selected.")
    if candidate.installation_id != policy.installation_id:
        raise RepositoryAdmissionError("Installation ID is not approved.")
    if candidate.requested_ref != candidate.resolved_sha:
        raise RepositoryAdmissionError("Resolved SHA must match the reviewed ref.")
    return AdmittedRepository(
        repository_numeric_id=candidate.repository_numeric_id,
        owner=candidate.owner,
        name=candidate.name,
        installation_id=candidate.installation_id,
        sha=candidate.resolved_sha,
    )
