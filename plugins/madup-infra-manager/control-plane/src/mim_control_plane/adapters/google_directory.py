"""Keyless Google Workspace Directory adapter for central identity sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Callable, Protocol
from urllib.parse import quote

import httpx

from mim_control_plane.config import COMPANY_DOMAIN, DirectoryRuntimeSettings
from mim_control_plane.domain.directory_sync import (
    MAX_DIRECTORY_SNAPSHOT_USERS,
    DirectoryAuthoritativeSnapshot,
    DirectorySnapshotUser,
)
from mim_control_plane.ports.directory import (
    DIRECTORY_READONLY_SCOPES,
    DirectoryProvider,
    DirectoryProviderError,
)

_DIRECTORY_API_BASE_URL = "https://admin.googleapis.com"
_USERS_PAGE_SIZE = 500
_GROUP_MEMBERS_PAGE_SIZE = 200
_USER_TYPES_TO_IGNORE = frozenset({"GROUP", "CUSTOMER", "EXTERNAL"})


class DirectoryTokenProvider(Protocol):
    def get_token(self, *, now: datetime) -> str: ...


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _google_auth_compute_engine_credentials_factory(
) -> object:
    from google.auth import compute_engine

    return compute_engine.Credentials()


def _google_auth_compute_engine_source_credentials() -> object:
    return _google_auth_compute_engine_credentials_factory()


def _google_auth_request() -> object:
    from google.auth.transport.requests import Request

    return Request()


def _google_auth_impersonated_credentials(
    *,
    source_credentials: object,
    target_principal: str,
    target_scopes: tuple[str, ...],
    subject: str,
) -> object:
    from google.auth import impersonated_credentials

    return impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=target_principal,
        target_scopes=list(target_scopes),
        subject=subject,
    )


def _require_utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be UTC-aware.")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware.")
    return value


def _require_non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a non-empty string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be a non-empty string.")
    return normalized


def _require_mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} is invalid.")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be an exact bool.")
    return value


def _normalize_madup_email(value: object, field_name: str) -> str:
    email = _require_non_empty_text(value, field_name)
    local, separator, domain = email.partition("@")
    if not separator or not local or domain.casefold() != COMPANY_DOMAIN:
        raise ValueError(f"{field_name} must be a @madup.com address.")
    return f"{local}@{domain.casefold()}"


def _normalize_email(value: object, field_name: str) -> str:
    email = _require_non_empty_text(value, field_name)
    local, separator, domain = email.partition("@")
    if not separator or not local or not domain:
        raise ValueError(f"{field_name} must be an email address.")
    return f"{local}@{domain.casefold()}"


def _stable_snapshot_id(
    *,
    required_group: str,
    started_at: datetime,
    completed_at: datetime,
    users: tuple[DirectorySnapshotUser, ...],
) -> str:
    digest = sha256()
    digest.update(f"{required_group}\n".encode("utf-8"))
    digest.update(f"{started_at.isoformat()}\n".encode("utf-8"))
    digest.update(f"{completed_at.isoformat()}\n".encode("utf-8"))
    for user in users:
        digest.update(
            (
                f"{user.directory_user_id}\n"
                f"{user.email}\n"
                f"{int(user.active)}\n"
                f"{int(user.in_required_group)}\n"
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _response_json(response: httpx.Response, field_name: str) -> dict[str, object]:
    if response.status_code != 200:
        raise ValueError(f"{field_name} request failed.")
    return _require_mapping(response.json(), field_name)


def _next_page_token(payload: dict[str, object]) -> str | None:
    token = payload.get("nextPageToken")
    if token is None:
        return None
    return _require_non_empty_text(token, "nextPageToken")


@dataclass(frozen=True, slots=True)
class ImpersonatedDirectoryTokenProvider:
    """Refreshes Directory API access tokens without JSON service account keys."""

    settings: DirectoryRuntimeSettings = field(repr=False)
    source_credentials_loader: Callable[
        [],
        object,
    ] = field(
        default=_google_auth_compute_engine_source_credentials,
        repr=False,
    )
    credentials_factory: Callable[..., object] = field(
        default=_google_auth_impersonated_credentials,
        repr=False,
    )
    request_factory: Callable[[], object] = field(
        default=_google_auth_request,
        repr=False,
    )

    def get_token(self, *, now: datetime) -> str:
        _require_utc_datetime(now, "now")
        try:
            source_credentials = self.source_credentials_loader()
            credentials = self.credentials_factory(
                source_credentials=source_credentials,
                target_principal=self.settings.directory_service_account_email,
                target_scopes=DIRECTORY_READONLY_SCOPES,
                subject=self.settings.directory_admin_subject,
            )
            refresh = getattr(credentials, "refresh")
            refresh(self.request_factory())
            token = getattr(credentials, "token", None)
            return _require_non_empty_text(token, "Directory access token")
        except Exception:
            raise DirectoryProviderError("Directory snapshot failed.") from None

    def __repr__(self) -> str:
        return "ImpersonatedDirectoryTokenProvider(redacted=True)"


@dataclass(frozen=True, slots=True)
class GoogleDirectoryProvider(DirectoryProvider):
    settings: DirectoryRuntimeSettings = field(repr=False)
    token_provider: DirectoryTokenProvider = field(repr=False)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    clock: Callable[[], datetime] = field(default=_utcnow, repr=False)
    base_url: str = field(init=False, default=_DIRECTORY_API_BASE_URL, repr=False)
    timeout_seconds: float = 10.0

    def fetch_snapshot(
        self,
        *,
        required_group: str,
        now: datetime,
    ) -> DirectoryAuthoritativeSnapshot:
        request_now = _require_utc_datetime(now, "now")
        required_group = _require_non_empty_text(required_group, "required_group")
        if required_group != self.settings.directory_required_group_label:
            raise DirectoryProviderError("Directory snapshot failed.")
        try:
            observed_started_at = _require_utc_datetime(self.clock(), "clock")
            token = self.token_provider.get_token(now=request_now)
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            }
            with httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                group_id = self._resolve_required_group_id(client=client)
                users = self._collect_workspace_users(client=client)
                member_emails = self._collect_group_members(
                    client=client,
                    group_id=group_id,
                )
            observed_completed_at = _require_utc_datetime(self.clock(), "clock")
            if observed_completed_at < observed_started_at:
                raise ValueError("clock moved backwards.")
            snapshot_users = tuple(
                sorted(
                    (
                        DirectorySnapshotUser(
                            directory_user_id=user.directory_user_id,
                            email=user.email,
                            active=user.active,
                            in_required_group=user.email in member_emails,
                        )
                        for user in users
                    ),
                    key=lambda user: (user.email, user.directory_user_id),
                )
            )
            snapshot_id = _stable_snapshot_id(
                required_group=required_group,
                started_at=observed_started_at,
                completed_at=observed_completed_at,
                users=snapshot_users,
            )
            return DirectoryAuthoritativeSnapshot(
                snapshot_id=snapshot_id,
                required_group=required_group,
                started_at=observed_started_at,
                completed_at=observed_completed_at,
                users=snapshot_users,
            )
        except Exception:
            raise DirectoryProviderError("Directory snapshot failed.") from None

    def __repr__(self) -> str:
        return (
            "GoogleDirectoryProvider("
            f"base_url={self.base_url!r}, timeout_seconds={self.timeout_seconds!r})"
        )

    def _resolve_required_group_id(self, *, client: httpx.Client) -> str:
        group_email = self.settings.directory_required_group_email
        payload = _response_json(
            client.get(
                f"/admin/directory/v1/groups/{quote(group_email, safe='')}",
            ),
            "group",
        )
        resolved_email = _normalize_madup_email(payload.get("email"), "group.email")
        if resolved_email.casefold() != group_email.casefold():
            raise ValueError("group email drift is invalid.")
        return _require_non_empty_text(payload.get("id"), "group.id")

    def _collect_workspace_users(
        self,
        *,
        client: httpx.Client,
    ) -> tuple[_CollectedDirectoryUser, ...]:
        collected: list[_CollectedDirectoryUser] = []
        seen_ids: set[str] = set()
        seen_emails: set[str] = set()
        seen_tokens: set[str] = set()
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {
                "customer": "my_customer",
                "projection": "basic",
                "maxResults": _USERS_PAGE_SIZE,
            }
            if page_token is not None:
                params["pageToken"] = page_token
            payload = _response_json(
                client.get("/admin/directory/v1/users", params=params),
                "users",
            )
            users = payload.get("users", [])
            if not isinstance(users, list):
                raise ValueError("users payload is invalid.")
            for record in users:
                parsed = self._parse_workspace_user(record)
                if parsed.directory_user_id in seen_ids:
                    raise ValueError("duplicate directory user id.")
                if parsed.email in seen_emails:
                    raise ValueError("duplicate directory user email.")
                seen_ids.add(parsed.directory_user_id)
                seen_emails.add(parsed.email)
                collected.append(parsed)
                if len(collected) > MAX_DIRECTORY_SNAPSHOT_USERS:
                    raise ValueError("directory snapshot exceeded bounded size.")
            page_token = _next_page_token(payload)
            if page_token is None:
                return tuple(collected)
            if page_token in seen_tokens:
                raise ValueError("repeated user page token.")
            seen_tokens.add(page_token)

    def _collect_group_members(
        self,
        *,
        client: httpx.Client,
        group_id: str,
    ) -> frozenset[str]:
        member_emails: set[str] = set()
        seen_tokens: set[str] = set()
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {
                "includeDerivedMembership": "true",
                "maxResults": _GROUP_MEMBERS_PAGE_SIZE,
            }
            if page_token is not None:
                params["pageToken"] = page_token
            payload = _response_json(
                client.get(
                    f"/admin/directory/v1/groups/{quote(group_id, safe='')}/members",
                    params=params,
                ),
                "members",
            )
            members = payload.get("members", [])
            if not isinstance(members, list):
                raise ValueError("members payload is invalid.")
            for member in members:
                parsed = self._parse_group_member(member)
                if parsed is not None:
                    member_emails.add(parsed)
            page_token = _next_page_token(payload)
            if page_token is None:
                return frozenset(member_emails)
            if page_token in seen_tokens:
                raise ValueError("repeated member page token.")
            seen_tokens.add(page_token)

    def _parse_workspace_user(self, value: object) -> _CollectedDirectoryUser:
        record = _require_mapping(value, "directory user")
        directory_user_id = _require_non_empty_text(record.get("id"), "user.id")
        email = _normalize_madup_email(record.get("primaryEmail"), "user.primaryEmail")
        active = not (
            _require_bool(record.get("suspended"), "user.suspended")
            or _require_bool(record.get("archived"), "user.archived")
        )
        return _CollectedDirectoryUser(
            directory_user_id=directory_user_id,
            email=email.casefold(),
            active=active,
        )

    def _parse_group_member(self, value: object) -> str | None:
        record = _require_mapping(value, "group member")
        _require_non_empty_text(record.get("id"), "member.id")
        member_type = _require_non_empty_text(record.get("type"), "member.type").upper()
        if member_type == "USER":
            return _normalize_email(record.get("email"), "member.email").casefold()
        if member_type in _USER_TYPES_TO_IGNORE:
            email = record.get("email")
            if member_type == "EXTERNAL":
                _normalize_email(email, "member.email")
                return None
            if email is None:
                return None
            _require_non_empty_text(email, "member.email")
            return None
        raise ValueError("member type is invalid.")


__all__ = [
    "GoogleDirectoryProvider",
    "ImpersonatedDirectoryTokenProvider",
]


@dataclass(frozen=True, slots=True)
class _CollectedDirectoryUser:
    directory_user_id: str
    email: str
    active: bool
