"""Least-privilege GitHub App source ingestion boundaries."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Callable, NoReturn, Protocol
from urllib.parse import urlsplit

import httpx

from mim_control_plane.domain.models import RepositoryAdmission
from mim_control_plane.domain.states import RepositoryAdmissionState
from mim_control_plane.ports.source import SourceSnapshotPort
from mim_control_plane.services.classifier import (
    MAX_PATH_LENGTH,
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_FILES,
    MAX_SNAPSHOT_TOTAL_BYTES,
)
from mim_control_plane.services.repository_admission import (
    RepositoryCandidate,
    SelectedRepositoryPolicy,
    admit_repository,
)


class GitHubWebhookError(ValueError):
    """Raised when a GitHub webhook cannot be authenticated."""


class GitHubSourceError(RuntimeError):
    """Raised when centrally authenticated GitHub source access fails closed."""


class GitHubSourceUnavailableError(GitHubSourceError):
    """Raised when GitHub source access is transiently unavailable."""


class GitHubSourceIntegrityError(GitHubSourceError):
    """Raised when GitHub source trust or integrity validation fails."""


_SIGNATURE_PATTERN = re.compile(r"^sha256=[0-9a-f]{64}$")
MAX_WEBHOOK_BODY_BYTES = 1_048_576
_REF_PATTERN = re.compile(r"^refs/heads/[A-Za-z0-9._/-]{1,200}$")
_DELIVERY_ID_PATTERN = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_TOKEN_RESPONSE_MAX_BYTES = 65_536
_GITHUB_API_VERSION = "2022-11-28"
MAX_ARCHIVE_BYTES = 2_097_152
MAX_ARCHIVE_ENTRIES = 256
_ARCHIVE_CONTENT_TYPES = frozenset({"application/zip", "application/octet-stream"})
_SNAPSHOT_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def verify_github_webhook_signature(
    *,
    body: bytes,
    signature_header: str,
    webhook_secret: bytes,
) -> None:
    """Authenticate the exact delivery bytes using GitHub's SHA-256 contract."""

    if type(body) is not bytes or len(body) > MAX_WEBHOOK_BODY_BYTES:
        raise GitHubWebhookError("GitHub webhook authentication failed.")
    if type(webhook_secret) is not bytes or len(webhook_secret) < 32:
        raise GitHubWebhookError("GitHub webhook authentication failed.")
    if (
        type(signature_header) is not str
        or _SIGNATURE_PATTERN.fullmatch(signature_header) is None
    ):
        raise GitHubWebhookError("GitHub webhook authentication failed.")
    expected = "sha256=" + hmac.new(
        webhook_secret,
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature_header, expected):
        raise GitHubWebhookError("GitHub webhook authentication failed.")


@dataclass(frozen=True, slots=True)
class VerifiedGitHubPush:
    delivery_id: str
    repository_numeric_id: int
    owner: str
    name: str
    installation_id: int
    ref: str
    sha: str


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected positive integer")
    return value


def _exact_bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("expected boolean")
    return value


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{field_name} must be UTC-aware")
    return value


def _parse_github_datetime(value: object) -> datetime:
    if type(value) is not str or not value:
        raise ValueError("invalid timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return _utc_datetime(datetime.fromisoformat(normalized), "expires_at")


def _strict_response_object(response: httpx.Response) -> dict[str, object]:
    content = response.content
    if len(content) > _TOKEN_RESPONSE_MAX_BYTES:
        raise ValueError("response is too large")
    return _mapping(
        json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    )


def _raise_retryable_source_failure() -> NoReturn:
    raise GitHubSourceUnavailableError("GitHub source fetch failed.")


def _raise_integrity_source_failure() -> NoReturn:
    raise GitHubSourceIntegrityError("GitHub source fetch failed.")


def _raise_retryable_token_failure() -> NoReturn:
    raise GitHubSourceUnavailableError("GitHub installation token minting failed.")


def _raise_integrity_token_failure() -> NoReturn:
    raise GitHubSourceIntegrityError("GitHub installation token minting failed.")


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES


def _is_retryable_transport_error(error: Exception) -> bool:
    return isinstance(error, httpx.TransportError | httpx.TimeoutException)


class GitHubAppJwtProvider(Protocol):
    def get_app_jwt(self, *, now: datetime) -> str: ...


class InstallationTokenProvider(Protocol):
    def get_token(self, *, installation_id: int, now: datetime) -> str: ...


@dataclass(frozen=True, slots=True)
class GitHubAppInstallationTokenProvider:
    """Mints read-only, selected-repository installation tokens centrally."""

    policy: SelectedRepositoryPolicy = field(repr=False)
    app_jwt_provider: GitHubAppJwtProvider = field(repr=False)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if type(self.policy) is not SelectedRepositoryPolicy:
            raise ValueError("policy must be exact")
        if not isinstance(self.timeout_seconds, float) or not (
            0.0 < self.timeout_seconds <= 30.0
        ):
            raise ValueError("timeout_seconds is invalid")

    def get_token(self, *, installation_id: int, now: datetime) -> str:
        request_now = _utc_datetime(now, "now")
        if type(installation_id) is not int or (
            installation_id != self.policy.installation_id
        ):
            _raise_integrity_token_failure()
        try:
            app_jwt = self.app_jwt_provider.get_app_jwt(now=request_now)
            if (
                type(app_jwt) is not str
                or len(app_jwt) < 16
                or any(character.isspace() for character in app_jwt)
            ):
                raise ValueError("invalid app JWT")
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            }
            with httpx.Client(
                base_url="https://api.github.com",
                headers=headers,
                follow_redirects=False,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.post(
                    f"/app/installations/{installation_id}/access_tokens",
                    json={
                        "permissions": {"contents": "read"},
                        "repository_ids": sorted(self.policy.allowed_repository_ids),
                    },
                )
            if response.status_code != 201:
                if _is_retryable_http_status(response.status_code):
                    _raise_retryable_token_failure()
                raise ValueError("token endpoint failed")
            payload = _strict_response_object(response)
            token = payload.get("token")
            if (
                type(token) is not str
                or len(token) < 20
                or any(character.isspace() for character in token)
            ):
                raise ValueError("invalid token")
            expires_at = _parse_github_datetime(payload.get("expires_at"))
            if not (
                request_now + timedelta(seconds=60)
                < expires_at
                <= request_now + timedelta(minutes=65)
            ):
                raise ValueError("invalid token lifetime")
            if payload.get("repository_selection") != "selected":
                raise ValueError("token is not repository-selected")
            permissions = _mapping(payload.get("permissions"))
            if permissions != {"contents": "read", "metadata": "read"}:
                raise ValueError("token permissions are not exact")
            repositories = payload.get("repositories")
            if not isinstance(repositories, list):
                raise ValueError("token repository set is missing")
            repository_ids: set[int] = set()
            for repository_value in repositories:
                repository = _mapping(repository_value)
                repository_id = repository.get("id")
                if type(repository_id) is not int or repository_id <= 0:
                    raise ValueError("token repository ID is invalid")
                if repository_id in repository_ids:
                    raise ValueError("token repository IDs are duplicated")
                repository_ids.add(repository_id)
            if repository_ids != set(self.policy.allowed_repository_ids):
                raise ValueError("token repository set is not exact")
            return token
        except GitHubSourceUnavailableError:
            raise
        except GitHubSourceIntegrityError:
            raise
        except Exception as exc:
            if _is_retryable_transport_error(exc):
                _raise_retryable_token_failure()
            _raise_integrity_token_failure()


@dataclass(frozen=True, slots=True)
class GitHubSourceAdapter(SourceSnapshotPort):
    """Fetches an immutable selected-repository snapshot via a GitHub App."""

    policy: SelectedRepositoryPolicy = field(repr=False)
    token_provider: InstallationTokenProvider = field(repr=False)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(tz=UTC),
        repr=False,
    )
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if type(self.policy) is not SelectedRepositoryPolicy:
            raise ValueError("policy must be exact")
        if not isinstance(self.timeout_seconds, float) or not (
            0.0 < self.timeout_seconds <= 30.0
        ):
            raise ValueError("timeout_seconds is invalid")

    def fetch_snapshot(
        self,
        admission: RepositoryAdmission,
    ) -> MappingProxyType[str, bytes]:
        try:
            if type(admission) is not RepositoryAdmission:
                raise ValueError("admission must be exact")
            if admission.state is not RepositoryAdmissionState.ADMITTED:
                raise ValueError("admission is not active")
            preflight = RepositoryCandidate(
                repository_numeric_id=admission.repository_numeric_id,
                owner=admission.owner,
                name=admission.name,
                installation_id=admission.installation_id,
                requested_ref=admission.admitted_sha,
                resolved_sha=admission.admitted_sha,
                is_fork=False,
            )
            admit_repository(self.policy, preflight)
            now = _utc_datetime(self.clock(), "clock")
            token = self.token_provider.get_token(
                installation_id=admission.installation_id,
                now=now,
            )
            if (
                type(token) is not str
                or len(token) < 20
                or any(character.isspace() for character in token)
            ):
                raise ValueError("invalid installation token")
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            }
            repository_path = f"/repos/{admission.owner}/{admission.name}"
            with httpx.Client(
                base_url="https://api.github.com",
                headers=headers,
                follow_redirects=False,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                repository_response = client.get(repository_path)
                if _is_retryable_http_status(repository_response.status_code):
                    _raise_retryable_source_failure()
                repository = _github_api_object(repository_response)
                owner = _mapping(repository.get("owner"))
                owner_login = owner.get("login")
                repository_name = repository.get("name")
                if (
                    type(owner_login) is not str
                    or type(repository_name) is not str
                    or repository.get("full_name")
                    != f"{owner_login}/{repository_name}"
                ):
                    raise ValueError("repository identity is invalid")
                candidate = RepositoryCandidate(
                    repository_numeric_id=_positive_int(repository.get("id")),
                    owner=owner_login,
                    name=repository_name,
                    installation_id=admission.installation_id,
                    requested_ref=admission.admitted_sha,
                    resolved_sha=admission.admitted_sha,
                    is_fork=_exact_bool(repository.get("fork")),
                )
                fetched = admit_repository(self.policy, candidate)
                if (
                    fetched.repository_numeric_id != admission.repository_numeric_id
                    or fetched.owner != admission.owner
                    or fetched.name != admission.name
                ):
                    raise ValueError("repository metadata changed")
                commit_response = client.get(
                    f"{repository_path}/commits/{admission.admitted_sha}"
                )
                if _is_retryable_http_status(commit_response.status_code):
                    _raise_retryable_source_failure()
                commit = _github_api_object(commit_response)
                if commit.get("sha") != admission.admitted_sha:
                    raise ValueError("commit SHA changed")
                archive_response = client.get(
                    f"{repository_path}/zipball/{admission.admitted_sha}"
                )
                if _is_retryable_http_status(archive_response.status_code):
                    _raise_retryable_source_failure()
            if archive_response.status_code not in {302, 307}:
                raise ValueError("archive endpoint did not redirect")
            archive_url = _validate_codeload_url(
                archive_response.headers.get("Location"),
                admission=admission,
            )
            with httpx.Client(
                follow_redirects=False,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                with client.stream("GET", archive_url) as archive_download:
                    archive_content = _read_archive_response(archive_download)
            return MappingProxyType(_extract_snapshot(archive_content))
        except GitHubSourceUnavailableError:
            raise
        except GitHubSourceIntegrityError:
            raise
        except Exception as exc:
            if _is_retryable_transport_error(exc):
                _raise_retryable_source_failure()
            _raise_integrity_source_failure()


def _github_api_object(response: httpx.Response) -> dict[str, object]:
    if response.status_code != 200:
        raise ValueError("GitHub API request failed")
    return _strict_response_object(response)


def _validate_codeload_url(
    value: object,
    *,
    admission: RepositoryAdmission,
) -> str:
    if type(value) is not str:
        raise ValueError("archive redirect is missing")
    parsed = urlsplit(value)
    expected_path = (
        f"/{admission.owner}/{admission.name}/legacy.zip/{admission.admitted_sha}"
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname != "codeload.github.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("archive redirect is not approved")
    return value


def _extract_snapshot(content: bytes) -> dict[str, bytes]:
    if type(content) is not bytes or len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive is too large")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("archive has too many entries")
        file_infos = [info for info in infos if not info.is_dir()]
        if not file_infos or len(file_infos) > MAX_SNAPSHOT_FILES:
            raise ValueError("archive file count is invalid")
        roots: set[str] = set()
        accepted: list[tuple[str, zipfile.ZipInfo]] = []
        seen_paths: set[str] = set()
        declared_total = 0
        for info in file_infos:
            raw_path = info.filename
            if (
                not raw_path
                or raw_path.startswith("/")
                or "\\" in raw_path
                or "\x00" in raw_path
                or "/" not in raw_path
            ):
                raise ValueError("archive path is unsafe")
            root, relative_path = raw_path.split("/", 1)
            roots.add(root)
            if len(roots) != 1 or not root or root in {".", ".."}:
                raise ValueError("archive root is invalid")
            _validate_snapshot_path(relative_path)
            if relative_path in seen_paths:
                raise ValueError("archive path is duplicated")
            seen_paths.add(relative_path)
            if info.flag_bits & 0x1:
                raise ValueError("encrypted archive entries are forbidden")
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if file_type not in {0, stat.S_IFREG}:
                raise ValueError("special archive entries are forbidden")
            if info.file_size < 0 or info.file_size > MAX_SNAPSHOT_FILE_BYTES:
                raise ValueError("archive file is too large")
            declared_total += info.file_size
            if declared_total > MAX_SNAPSHOT_TOTAL_BYTES:
                raise ValueError("archive contents are too large")
            accepted.append((relative_path, info))
        snapshot: dict[str, bytes] = {}
        actual_total = 0
        for relative_path, info in sorted(accepted, key=lambda item: item[0]):
            with archive.open(info, "r") as source_file:
                file_content = source_file.read(MAX_SNAPSHOT_FILE_BYTES + 1)
            if len(file_content) != info.file_size:
                raise ValueError("archive file size changed")
            actual_total += len(file_content)
            if actual_total > MAX_SNAPSHOT_TOTAL_BYTES:
                raise ValueError("archive contents are too large")
            snapshot[relative_path] = file_content
        return snapshot


def _read_archive_response(response: httpx.Response) -> bytes:
    if _is_retryable_http_status(response.status_code):
        _raise_retryable_source_failure()
    if response.status_code != 200:
        raise ValueError("archive download failed")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
    if content_type not in _ARCHIVE_CONTENT_TYPES:
        raise ValueError("archive content type is invalid")
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdigit():
            raise ValueError("archive content length is invalid")
        if int(content_length) > MAX_ARCHIVE_BYTES:
            raise ValueError("archive is too large")
    chunks: list[bytes] = []
    total_bytes = 0
    for chunk in response.iter_bytes():
        total_bytes += len(chunk)
        if total_bytes > MAX_ARCHIVE_BYTES:
            raise ValueError("archive is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_snapshot_path(path: str) -> None:
    if (
        len(path) > MAX_PATH_LENGTH
        or _SNAPSHOT_PATH_PATTERN.fullmatch(path) is None
        or path.startswith("/")
        or path.startswith("./")
    ):
        raise ValueError("archive path is unsafe")
    parts = path.split("/")
    if any(
        part in {"", ".", ".."}
        or (part.startswith(".") and part != ".github")
        or part.startswith("-")
        or any(ord(character) < 0x21 for character in part)
        for part in parts
    ):
        raise ValueError("archive path is unsafe")


def verify_github_push(
    *,
    body: bytes,
    signature_header: str,
    webhook_secret: bytes,
    event_name: str,
    delivery_id: str,
    allowed_ref: str,
    policy: SelectedRepositoryPolicy,
) -> VerifiedGitHubPush:
    """Authenticate and narrow a push delivery to one selected repository/SHA."""

    verify_github_webhook_signature(
        body=body,
        signature_header=signature_header,
        webhook_secret=webhook_secret,
    )
    try:
        if (
            type(delivery_id) is not str
            or _DELIVERY_ID_PATTERN.fullmatch(delivery_id) is None
        ):
            raise ValueError("delivery ID is invalid")
        if event_name != "push":
            raise ValueError("wrong event")
        if (
            type(allowed_ref) is not str
            or _REF_PATTERN.fullmatch(allowed_ref) is None
            or ".." in allowed_ref
        ):
            raise ValueError("unsafe ref")
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
        payload = _mapping(payload)
        if payload.get("ref") != allowed_ref or payload.get("deleted") is not False:
            raise ValueError("push is not the approved branch head")
        sha = payload.get("after")
        if type(sha) is not str:
            raise ValueError("missing SHA")
        head_commit = _mapping(payload.get("head_commit"))
        if head_commit.get("id") != sha:
            raise ValueError("head commit does not match")
        repository = _mapping(payload.get("repository"))
        owner = _mapping(repository.get("owner"))
        installation = _mapping(payload.get("installation"))
        owner_login = owner.get("login")
        repository_name = repository.get("name")
        if type(owner_login) is not str or type(repository_name) is not str:
            raise ValueError("repository identity is missing")
        if repository.get("full_name") != f"{owner_login}/{repository_name}":
            raise ValueError("repository full name does not match")
        candidate = RepositoryCandidate(
            repository_numeric_id=_positive_int(repository.get("id")),
            owner=owner_login,
            name=repository_name,
            installation_id=_positive_int(installation.get("id")),
            requested_ref=sha,
            resolved_sha=sha,
            is_fork=_exact_bool(repository.get("fork")),
        )
        admitted = admit_repository(policy, candidate)
    except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise GitHubWebhookError("GitHub webhook validation failed.") from None
    return VerifiedGitHubPush(
        delivery_id=delivery_id.lower(),
        repository_numeric_id=admitted.repository_numeric_id,
        owner=admitted.owner,
        name=admitted.name,
        installation_id=admitted.installation_id,
        ref=allowed_ref,
        sha=admitted.sha,
    )
