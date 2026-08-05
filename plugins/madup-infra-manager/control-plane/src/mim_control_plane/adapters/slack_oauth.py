"""Production adapters for Slack OAuth HTTP exchange and credential vaulting."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import google_crc32c
import httpx

from mim_control_plane.domain.slack_oauth import SlackOAuthGrant
from mim_control_plane.ports.slack_oauth import (
    SlackOAuthCredentialVaultError,
    SlackOAuthProviderError,
)

_FAILED = "Slack OAuth adapter failed."
_OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"
_AUTH_REVOKE_URL = "https://slack.com/api/auth.revoke"
_APPS_UNINSTALL_URL = "https://slack.com/api/apps.uninstall"
_ADMIN_APPS_UNINSTALL_URL = "https://slack.com/api/admin.apps.uninstall"
_TOKEN_PAYLOAD_FIELDS = frozenset({"access_token"})
_MAX_TIMEOUT_SECONDS = 10.0
_SECRET_VERSION_PATTERN = re.compile(
    r"^projects/([a-z][a-z0-9-]{4,28}[a-z0-9])/"
    r"secrets/([a-z][a-z0-9-]{0,62})/versions/([1-9][0-9]{0,9})$"
)


class _SecretManagerClient(Protocol):
    def add_secret_version(self, request: object) -> object: ...

    def access_secret_version(self, request: object) -> object: ...

    def destroy_secret_version(self, request: object) -> object: ...


@dataclass(frozen=True, slots=True)
class _FallbackSecretPayload:
    data: bytes
    data_crc32c: int | None = None


@dataclass(frozen=True, slots=True)
class _FallbackAddSecretVersionRequest:
    parent: str
    payload: _FallbackSecretPayload


@dataclass(frozen=True, slots=True)
class _FallbackAccessSecretVersionRequest:
    name: str


@dataclass(frozen=True, slots=True)
class _FallbackDestroySecretVersionRequest:
    name: str


@dataclass(frozen=True, slots=True)
class SlackOAuthSecretManagerVault:
    """Store only the token material needed for later revoke/uninstall actions."""

    client: _SecretManagerClient
    project_id: str
    secret_id: str

    def __post_init__(self) -> None:
        _require_project_id(self.project_id)
        _require_secret_id(self.secret_id)

    @property
    def secret_name(self) -> str:
        return f"projects/{self.project_id}/secrets/{self.secret_id}"

    def write_access_token(self, *, access_token: str) -> str:
        payload = _token_payload(_require_text(access_token, "access_token"))
        checksum = _crc32c_int(payload)
        request = _add_secret_version_request(
            parent=self.secret_name,
            payload=payload,
            checksum=checksum,
        )
        try:
            created = cast(Any, self.client.add_secret_version(request))
            name = _require_secret_ref(
                created.name,
                expected_project_id=self.project_id,
                expected_secret_id=self.secret_id,
            )
            if created.client_specified_payload_checksum is not True:
                raise ValueError
            return name
        except SlackOAuthCredentialVaultError:
            raise
        except Exception:
            raise SlackOAuthCredentialVaultError(_FAILED) from None

    def read_access_token(self, *, secret_ref: str) -> str:
        resource = _require_secret_ref(
            secret_ref,
            expected_project_id=self.project_id,
            expected_secret_id=self.secret_id,
        )
        try:
            response = cast(
                Any,
                self.client.access_secret_version(
                    _access_secret_version_request(name=resource)
                ),
            )
            payload = cast(Any, response.payload)
            raw = bytes(payload.data)
            expected_crc = _require_crc32c(payload.data_crc32c)
            if _crc32c_int(raw) != expected_crc:
                raise ValueError
            data = json.loads(raw.decode("utf-8"))
            if type(data) is not dict or frozenset(data) != _TOKEN_PAYLOAD_FIELDS:
                raise ValueError
            return _require_text(data.get("access_token"), "access_token")
        except SlackOAuthCredentialVaultError:
            raise
        except Exception:
            raise SlackOAuthCredentialVaultError(_FAILED) from None

    def destroy_secret_ref(self, *, secret_ref: str) -> None:
        resource = _require_secret_ref(
            secret_ref,
            expected_project_id=self.project_id,
            expected_secret_id=self.secret_id,
        )
        try:
            self.client.destroy_secret_version(
                _destroy_secret_version_request(name=resource)
            )
        except Exception:
            raise SlackOAuthCredentialVaultError(_FAILED) from None


@dataclass(frozen=True, slots=True)
class SlackOAuthHttpProvider:
    """Bounded Slack OAuth adapter using official Slack endpoints only."""

    client: httpx.Client
    credential_vault: SlackOAuthSecretManagerVault
    client_id: str
    client_secret: str
    timeout: httpx.Timeout = field(
        default_factory=lambda: httpx.Timeout(
            timeout=5.0,
            connect=5.0,
            read=5.0,
            write=5.0,
            pool=5.0,
        )
    )
    oauth_access_url: str = _OAUTH_ACCESS_URL
    auth_revoke_url: str = _AUTH_REVOKE_URL
    apps_uninstall_url: str = _APPS_UNINSTALL_URL
    admin_apps_uninstall_url: str = _ADMIN_APPS_UNINSTALL_URL

    def __post_init__(self) -> None:
        _require_exact_url(self.oauth_access_url, expected=_OAUTH_ACCESS_URL)
        _require_exact_url(self.auth_revoke_url, expected=_AUTH_REVOKE_URL)
        _require_exact_url(self.apps_uninstall_url, expected=_APPS_UNINSTALL_URL)
        _require_exact_url(
            self.admin_apps_uninstall_url,
            expected=_ADMIN_APPS_UNINSTALL_URL,
        )
        _require_text(self.client_id, "client_id")
        _require_text(self.client_secret, "client_secret")
        _require_timeout(self.timeout)

    def exchange_installation_code(
        self,
        *,
        code: str,
        redirect_uri: str,
    ) -> SlackOAuthGrant:
        try:
            response = self.client.post(
                self.oauth_access_url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": _require_text(code, "code"),
                    "redirect_uri": _require_text(redirect_uri, "redirect_uri"),
                },
                follow_redirects=False,
                timeout=self.timeout,
            )
            payload = _parse_slack_json_response(response)
            grant = _grant_from_exchange_payload(payload)
            access_token = _bot_token_from_payload(payload)
            try:
                secret_ref = self.credential_vault.write_access_token(
                    access_token=access_token
                )
            except SlackOAuthCredentialVaultError:
                self._best_effort_revoke_token(access_token)
                raise
            return SlackOAuthGrant(
                app_id=grant.app_id,
                team_id=grant.team_id,
                enterprise_id=grant.enterprise_id,
                is_enterprise_install=grant.is_enterprise_install,
                granted_scopes=grant.granted_scopes,
                secret_ref=secret_ref,
            )
        except (SlackOAuthCredentialVaultError, SlackOAuthProviderError):
            raise
        except Exception:
            raise SlackOAuthProviderError(_FAILED) from None

    def revoke_installation(self, *, secret_ref: str) -> None:
        try:
            token = self.credential_vault.read_access_token(secret_ref=secret_ref)
            response = self.client.post(
                self.auth_revoke_url,
                data={"token": token},
                follow_redirects=False,
                timeout=self.timeout,
            )
            parsed = _parse_slack_json_response(response)
            if parsed.get("revoked") is not True:
                raise ValueError
        except (SlackOAuthCredentialVaultError, SlackOAuthProviderError):
            raise
        except Exception:
            raise SlackOAuthProviderError(_FAILED) from None

    def uninstall_installation(
        self,
        *,
        secret_ref: str,
        app_id: str,
        team_id: str,
        enterprise_id: str | None,
        is_enterprise_install: bool,
    ) -> None:
        try:
            token = self.credential_vault.read_access_token(secret_ref=secret_ref)
            if is_enterprise_install:
                raise SlackOAuthProviderError(_FAILED)
            if enterprise_id is not None:
                raise SlackOAuthProviderError(_FAILED)
            _require_text(team_id, "team_id")
            _parse_slack_json_response(
                self.client.post(
                    self.apps_uninstall_url,
                    data={
                        "token": token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    follow_redirects=False,
                    timeout=self.timeout,
                )
            )
        except (SlackOAuthCredentialVaultError, SlackOAuthProviderError):
            raise
        except Exception:
            raise SlackOAuthProviderError(_FAILED) from None

    def _best_effort_revoke_token(self, token: str) -> None:
        try:
            self.client.post(
                self.auth_revoke_url,
                data={"token": token},
                follow_redirects=False,
                timeout=self.timeout,
            )
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class _GrantEnvelope:
    app_id: str
    team_id: str
    enterprise_id: str | None
    is_enterprise_install: bool
    granted_scopes: tuple[str, ...]


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be exact text.")
    return value


def _require_exact_url(value: object, *, expected: str) -> str:
    normalized = _require_text(value, "url")
    if normalized != expected:
        raise ValueError("unexpected Slack endpoint")
    return normalized


def _require_project_id(value: object) -> str:
    normalized = _require_text(value, "project_id")
    if normalized != normalized.lower():
        raise ValueError("project_id is invalid.")
    return normalized


def _require_secret_id(value: object) -> str:
    normalized = _require_text(value, "secret_id")
    if normalized != normalized.lower():
        raise ValueError("secret_id is invalid.")
    return normalized


def _require_secret_ref(
    value: object,
    *,
    expected_project_id: str,
    expected_secret_id: str,
) -> str:
    normalized = _require_text(value, "secret_ref")
    match = _SECRET_VERSION_PATTERN.fullmatch(normalized)
    if match is None:
        raise SlackOAuthCredentialVaultError(_FAILED)
    project_id, secret_id, version = match.groups()
    if project_id != expected_project_id or secret_id != expected_secret_id:
        raise SlackOAuthCredentialVaultError(_FAILED)
    if int(version) < 1:
        raise SlackOAuthCredentialVaultError(_FAILED)
    return normalized


def _require_timeout(value: object) -> httpx.Timeout:
    if type(value) is not httpx.Timeout:
        raise ValueError("timeout must be an httpx.Timeout.")
    for amount in (
        value.connect,
        value.read,
        value.write,
        value.pool,
    ):
        if (
            amount is None
            or not isinstance(amount, (int, float))
            or isinstance(amount, bool)
        ):
            raise ValueError("timeout must be bounded.")
        if not math.isfinite(float(amount)) or float(amount) <= 0:
            raise ValueError("timeout must be positive and bounded.")
        if float(amount) > _MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout exceeds Slack adapter bound.")
    return value


def _parse_slack_json_response(response: httpx.Response) -> dict[str, object]:
    if response.is_redirect or 300 <= response.status_code < 400:
        raise SlackOAuthProviderError(_FAILED)
    if response.status_code != 200:
        raise SlackOAuthProviderError(_FAILED)
    try:
        payload = response.json()
    except Exception:
        raise SlackOAuthProviderError(_FAILED) from None
    if type(payload) is not dict or payload.get("ok") is not True:
        raise SlackOAuthProviderError(_FAILED)
    return cast(dict[str, object], payload)


def _sorted_scopes(value: object) -> tuple[str, ...]:
    scope_csv = _require_text(value, "scope")
    scopes = tuple(scope for scope in scope_csv.split(",") if scope)
    if not scopes:
        raise ValueError("scope must not be empty.")
    if len(set(scopes)) != len(scopes):
        raise ValueError("scope must not contain duplicates.")
    return scopes


def _grant_from_exchange_payload(payload: dict[str, object]) -> _GrantEnvelope:
    team = payload.get("team")
    if type(team) is not dict:
        raise SlackOAuthProviderError(_FAILED)
    enterprise = payload.get("enterprise")
    enterprise_id: str | None = None
    if enterprise is not None:
        if type(enterprise) is not dict:
            raise SlackOAuthProviderError(_FAILED)
        enterprise_id = _require_text(enterprise.get("id"), "enterprise.id")
    is_enterprise_install = payload.get("is_enterprise_install", False)
    if type(is_enterprise_install) is not bool:
        raise SlackOAuthProviderError(_FAILED)
    return _GrantEnvelope(
        app_id=_require_text(payload.get("app_id"), "app_id"),
        team_id=_require_text(team.get("id"), "team.id"),
        enterprise_id=enterprise_id,
        is_enterprise_install=is_enterprise_install,
        granted_scopes=_sorted_scopes(payload.get("scope")),
    )


def _bot_token_from_payload(payload: dict[str, object]) -> str:
    return _require_text(payload.get("access_token"), "access_token")


def _token_payload(access_token: str) -> bytes:
    return json.dumps(
        {"access_token": access_token},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _crc32c_int(payload: bytes) -> int:
    return int.from_bytes(google_crc32c.Checksum(payload).digest(), "big")


def _require_crc32c(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SlackOAuthCredentialVaultError(_FAILED)
    return value


def _add_secret_version_request(
    *,
    parent: str,
    payload: bytes,
    checksum: int,
) -> object:
    try:
        from google.cloud import secretmanager_v1
    except ImportError:
        return _FallbackAddSecretVersionRequest(
            parent=parent,
            payload=_FallbackSecretPayload(data=payload, data_crc32c=checksum),
        )
    return secretmanager_v1.AddSecretVersionRequest(
        parent=parent,
        payload=secretmanager_v1.SecretPayload(
            data=payload,
            data_crc32c=checksum,
        ),
    )


def _access_secret_version_request(*, name: str) -> object:
    try:
        from google.cloud import secretmanager_v1
    except ImportError:
        return _FallbackAccessSecretVersionRequest(name=name)
    return secretmanager_v1.AccessSecretVersionRequest(name=name)


def _destroy_secret_version_request(*, name: str) -> object:
    try:
        from google.cloud import secretmanager_v1
    except ImportError:
        return _FallbackDestroySecretVersionRequest(name=name)
    return secretmanager_v1.DestroySecretVersionRequest(name=name)
