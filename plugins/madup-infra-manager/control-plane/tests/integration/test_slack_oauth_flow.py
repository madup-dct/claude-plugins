from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs

import google_crc32c
import httpx

from mim_control_plane.adapters.firestore_slack_oauth import (
    FirestoreSlackOAuthRepository,
)
from mim_control_plane.adapters.slack_oauth import (
    SlackOAuthHttpProvider,
    SlackOAuthSecretManagerVault,
)
from mim_control_plane.domain.models import UserId
from mim_control_plane.domain.slack_oauth import (
    SlackOAuthIdentityLink,
    SlackOAuthIdentityLinkState,
    SlackOAuthInstallState,
    SlackOAuthPendingState,
    SlackOAuthSharedInstall,
    SlackOAuthTenant,
)
from mim_control_plane.ports.slack_oauth import (
    SlackOAuthCredentialVaultError,
    SlackOAuthProviderError,
    SlackOAuthStateRejected,
)
from mim_control_plane.services.slack_oauth import _install_id

NOW = datetime(2026, 8, 4, 4, 0, 0, tzinfo=UTC)
PROJECT_ID = "mim-prod-123456"
SECRET_ID = "mim-slack-oauth"
SECRET_NAME = f"projects/{PROJECT_ID}/secrets/{SECRET_ID}"
SECRET_REF = f"{SECRET_NAME}/versions/1"
OAUTH_CRED = "client-" + "secret"


@dataclass
class FakeSnapshot:
    id: str
    exists: bool
    data: dict[str, object] | None

    def to_dict(self) -> dict[str, object] | None:
        return self.data


class FakeDocument:
    def __init__(self, collection: "FakeCollection", document_id: str) -> None:
        self._collection = collection
        self.id = document_id

    def get(self, *, transaction: object | None = None) -> FakeSnapshot:
        del transaction
        data = self._collection.documents.get(self.id)
        return FakeSnapshot(id=self.id, exists=data is not None, data=data)

    def set(self, data: dict[str, object]) -> None:
        self._collection.documents[self.id] = dict(data)

    def create(self, data: dict[str, object]) -> None:
        if self.id in self._collection.documents:
            raise RuntimeError("duplicate")
        self._collection.documents[self.id] = dict(data)

    def delete(self) -> None:
        self._collection.documents.pop(self.id, None)


class FakeCollection:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, object]] = {}

    def document(self, document_id: str) -> FakeDocument:
        return FakeDocument(self, document_id)


class FakeTransaction:
    def set(self, reference: FakeDocument, data: dict[str, object]) -> None:
        reference.set(data)

    def create(self, reference: FakeDocument, data: dict[str, object]) -> None:
        reference.create(data)

    def delete(self, reference: FakeDocument) -> None:
        reference.delete()


class FakeFirestoreClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def collection(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())

    def run_transaction(self, operation: Any) -> object:
        return operation(FakeTransaction())


@dataclass
class FakeSecretVersion:
    name: str
    client_specified_payload_checksum: bool = True


@dataclass
class FakeSecretPayload:
    data: bytes
    data_crc32c: int


@dataclass
class FakeAccessResponse:
    payload: FakeSecretPayload


class FakeSecretManagerClient:
    def __init__(self) -> None:
        self.versions: dict[str, bytes] = {}
        self.destroyed: tuple[str, ...] = ()
        self.corrupt_reads = False

    def add_secret_version(self, request: object) -> FakeSecretVersion:
        payload = request.payload.data
        version = len(self.versions) + 1
        name = f"{request.parent}/versions/{version}"
        self.versions[name] = payload
        return FakeSecretVersion(name=name)

    def access_secret_version(self, request: object) -> FakeAccessResponse:
        payload = self.versions[request.name]
        checksum = int.from_bytes(google_crc32c.Checksum(payload).digest(), "big")
        if self.corrupt_reads:
            checksum += 1
        return FakeAccessResponse(
            payload=FakeSecretPayload(data=payload, data_crc32c=checksum)
        )

    def destroy_secret_version(self, request: object) -> object:
        self.destroyed = self.destroyed + (request.name,)
        self.versions.pop(request.name, None)
        return object()


class SlackOAuthAdapterIntegrationTests(unittest.TestCase):
    def test_firestore_repository_enforces_single_active_install_and_single_use_state(
        self,
    ) -> None:
        client = FakeFirestoreClient()
        repository = FirestoreSlackOAuthRepository(
            client=client,
            transaction_runner=lambda supplied_client, operation: (
                supplied_client.run_transaction(operation)
            ),
        )
        pending = SlackOAuthPendingState(
            state_id="a" * 24,
            state_hash="b" * 64,
            installer_mim_user_id=UserId("admin-1"),
            installer_email="admin@madup.com",
            required_scopes=("chat:write", "commands"),
            redirect_uri="https://mim.madup.app/slack/oauth/callback",
            install_tenant=SlackOAuthTenant(team_id="T123", enterprise_id="E123"),
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        repository.create_pending_state(pending)
        consumed = repository.consume_pending_state(
            state_id="a" * 24,
            state_hash="b" * 64,
            expected_installer_mim_user_id="admin-1",
            expected_installer_email="admin@madup.com",
            expected_tenant=SlackOAuthTenant(team_id="T123", enterprise_id="E123"),
            expected_redirect_uri="https://mim.madup.app/slack/oauth/callback",
            expected_scopes=("chat:write", "commands"),
            now=NOW + timedelta(minutes=1),
        )
        self.assertEqual(consumed.version, 2)
        with self.assertRaises(SlackOAuthStateRejected):
            repository.consume_pending_state(
                state_id="a" * 24,
                state_hash="b" * 64,
                expected_installer_mim_user_id="admin-1",
                expected_installer_email="admin@madup.com",
                expected_tenant=SlackOAuthTenant(team_id="T123", enterprise_id="E123"),
                expected_redirect_uri="https://mim.madup.app/slack/oauth/callback",
                expected_scopes=("chat:write", "commands"),
                now=NOW + timedelta(minutes=2),
            )

        install = SlackOAuthSharedInstall(
            install_id=_install_id(
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
            ),
            app_id="A123",
            team_id="T123",
            enterprise_id="E123",
            is_enterprise_install=True,
            granted_scopes=("chat:write", "commands"),
            secret_ref=SECRET_REF,
            installer_mim_user_id=UserId("admin-1"),
            installer_email="admin@madup.com",
            created_at=NOW,
            updated_at=NOW,
            state=SlackOAuthInstallState.ACTIVE,
        )
        repository.save_shared_install(install)
        active = repository.get_active_install_for_tenant(
            team_id="T123",
            enterprise_id="E123",
        )
        self.assertEqual(active, install)

        with self.assertRaises(Exception):
            repository.save_shared_install(
                SlackOAuthSharedInstall(
                    install_id=_install_id(
                        app_id="A123",
                        team_id="T123",
                        enterprise_id="E123",
                        is_enterprise_install=True,
                    ),
                    app_id="A123",
                    team_id="T123",
                    enterprise_id="E123",
                    is_enterprise_install=True,
                    granted_scopes=("chat:write", "commands"),
                    secret_ref=f"{SECRET_NAME}/versions/2",
                    installer_mim_user_id=UserId("admin-1"),
                    installer_email="admin@madup.com",
                    created_at=NOW + timedelta(minutes=1),
                    updated_at=NOW + timedelta(minutes=1),
                    state=SlackOAuthInstallState.ACTIVE,
                )
            )

        revoked = repository.revoke_shared_install(
            install_id=install.install_id,
            revoked_at=NOW + timedelta(minutes=3),
        )
        self.assertEqual(revoked.state, SlackOAuthInstallState.REVOKED)
        self.assertIsNone(
            repository.get_active_install_for_tenant(
                team_id="T123",
                enterprise_id="E123",
            )
        )

        reinstall = repository.save_shared_install(
            SlackOAuthSharedInstall(
                install_id=install.install_id,
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=("chat:write", "commands"),
                secret_ref=f"{SECRET_NAME}/versions/3",
                installer_mim_user_id=UserId("admin-1"),
                installer_email="admin@madup.com",
                created_at=NOW + timedelta(minutes=4),
                updated_at=NOW + timedelta(minutes=4),
                state=SlackOAuthInstallState.ACTIVE,
            )
        )
        self.assertGreater(reinstall.version, revoked.version)

        link = SlackOAuthIdentityLink(
            install_id=install.install_id,
            team_id="T123",
            slack_user_id="U123",
            mim_user_id=UserId("employee-1"),
            company_email="employee@madup.com",
            created_at=NOW,
            updated_at=NOW,
            state=SlackOAuthIdentityLinkState.ACTIVE,
        )
        repository.save_identity_link(link)
        revoked_link = repository.revoke_identity_link(
            install_id=install.install_id,
            mim_user_id="employee-1",
            revoked_at=NOW + timedelta(minutes=5),
        )
        self.assertEqual(revoked_link.state, SlackOAuthIdentityLinkState.REVOKED)

    def test_secret_manager_vault_validates_exact_resource_and_crc(self) -> None:
        client = FakeSecretManagerClient()
        vault = SlackOAuthSecretManagerVault(
            client=client,
            project_id=PROJECT_ID,
            secret_id=SECRET_ID,
        )

        secret_ref = vault.write_access_token(access_token="bot-token-123")
        self.assertEqual(
            client.versions[secret_ref],
            b'{"access_token":"bot-token-123"}',
        )
        self.assertEqual(
            vault.read_access_token(secret_ref=secret_ref), "bot-token-123"
        )

        client.corrupt_reads = True
        with self.assertRaises(SlackOAuthCredentialVaultError):
            vault.read_access_token(secret_ref=secret_ref)
        client.corrupt_reads = False

        with self.assertRaises(SlackOAuthCredentialVaultError):
            vault.read_access_token(
                secret_ref="projects/other-project/secrets/mim-slack-oauth/versions/1"
            )
        with self.assertRaises(SlackOAuthCredentialVaultError):
            vault.destroy_secret_ref(secret_ref=f"{SECRET_NAME}/versions/0")

    def test_http_provider_uses_bounded_timeouts_and_minimal_secret_payload(
        self,
    ) -> None:
        captured: list[tuple[str, dict[str, list[str]], dict[str, float | None]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = parse_qs(request.content.decode("utf-8"), keep_blank_values=True)
            captured.append(
                (
                    str(request.url),
                    body,
                    cast(dict[str, float | None], request.extensions["timeout"]),
                )
            )
            if str(request.url) == "https://slack.com/api/oauth.v2.access":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "access_token": "bot-token-123",
                        "scope": "chat:write,commands",
                        "app_id": "A123",
                        "team": {"id": "T123", "name": "Madup"},
                        "is_enterprise_install": False,
                    },
                )
            if str(request.url) == "https://slack.com/api/auth.revoke":
                return httpx.Response(200, json={"ok": True, "revoked": True})
            if str(request.url) == "https://slack.com/api/apps.uninstall":
                return httpx.Response(200, json={"ok": True})
            raise AssertionError(f"unexpected url: {request.url}")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        secret_client = FakeSecretManagerClient()
        vault = SlackOAuthSecretManagerVault(
            client=secret_client,
            project_id=PROJECT_ID,
            secret_id=SECRET_ID,
        )
        provider = SlackOAuthHttpProvider(
            client=client,
            credential_vault=vault,
            client_id="111.222",
            client_secret=OAUTH_CRED,
        )

        grant = provider.exchange_installation_code(
            code="oauth-code-123",
            redirect_uri="https://mim.madup.app/slack/oauth/callback",
        )
        self.assertEqual(grant.secret_ref, SECRET_REF)
        self.assertEqual(
            secret_client.versions[grant.secret_ref],
            b'{"access_token":"bot-token-123"}',
        )

        provider.revoke_installation(secret_ref=grant.secret_ref)
        provider.uninstall_installation(
            secret_ref=grant.secret_ref,
            app_id="A123",
            team_id="T123",
            enterprise_id=None,
            is_enterprise_install=False,
        )

        self.assertEqual(captured[0][0], "https://slack.com/api/oauth.v2.access")
        self.assertEqual(captured[0][1]["client_id"], ["111.222"])
        self.assertEqual(captured[0][1]["client_secret"], ["client-secret"])
        self.assertEqual(captured[1][0], "https://slack.com/api/auth.revoke")
        self.assertEqual(captured[2][0], "https://slack.com/api/apps.uninstall")
        self.assertEqual(captured[2][1]["token"], ["bot-token-123"])
        self.assertEqual(captured[2][1]["client_id"], ["111.222"])
        self.assertEqual(captured[2][1]["client_secret"], ["client-secret"])
        for _, _, timeout in captured:
            self.assertEqual(
                timeout,
                {
                    "connect": 5.0,
                    "read": 5.0,
                    "write": 5.0,
                    "pool": 5.0,
                },
            )

    def test_http_provider_rejects_slack_failures_and_unsupported_enterprise_uninstall(
        self,
    ) -> None:
        failing_client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"ok": False, "error": "bad_redirect_uri"},
                )
            )
        )
        vault = SlackOAuthSecretManagerVault(
            client=FakeSecretManagerClient(),
            project_id=PROJECT_ID,
            secret_id=SECRET_ID,
        )
        provider = SlackOAuthHttpProvider(
            client=failing_client,
            credential_vault=vault,
            client_id="111.222",
            client_secret=OAUTH_CRED,
        )

        with self.assertRaises(SlackOAuthProviderError):
            provider.exchange_installation_code(
                code="oauth-code-123",
                redirect_uri="https://mim.madup.app/slack/oauth/callback",
            )

        success_client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "access_token": "bot-token-123",
                        "scope": "chat:write,commands",
                        "app_id": "A123",
                        "team": {"id": "T123", "name": "Madup"},
                        "enterprise": {"id": "E123", "name": "Madup Enterprise"},
                        "is_enterprise_install": True,
                    },
                )
            )
        )
        provider = SlackOAuthHttpProvider(
            client=success_client,
            credential_vault=vault,
            client_id="111.222",
            client_secret=OAUTH_CRED,
        )
        grant = provider.exchange_installation_code(
            code="oauth-code-123",
            redirect_uri="https://mim.madup.app/slack/oauth/callback",
        )
        with self.assertRaises(SlackOAuthProviderError):
            provider.uninstall_installation(
                secret_ref=grant.secret_ref,
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
            )

        non_boolean_client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "access_token": "bot-token-123",
                        "scope": "chat:write,commands",
                        "app_id": "A123",
                        "team": {"id": "T123", "name": "Madup"},
                        "enterprise": {"id": "E123", "name": "Madup Enterprise"},
                        "is_enterprise_install": "false",
                    },
                )
            )
        )
        provider = SlackOAuthHttpProvider(
            client=non_boolean_client,
            credential_vault=vault,
            client_id="111.222",
            client_secret=OAUTH_CRED,
        )
        with self.assertRaises(SlackOAuthProviderError):
            provider.exchange_installation_code(
                code="oauth-code-123",
                redirect_uri="https://mim.madup.app/slack/oauth/callback",
            )


if __name__ == "__main__":
    unittest.main()
