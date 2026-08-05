from __future__ import annotations

import threading
import unittest
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Callable
from urllib.parse import parse_qs, urlparse

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import User, UserId
from mim_control_plane.domain.slack_oauth import (
    SlackOAuthGrant,
    SlackOAuthIdentityLink,
    SlackOAuthIdentityLinkState,
    SlackOAuthInstallState,
    SlackOAuthPendingState,
    SlackOAuthSharedInstall,
    SlackOAuthTenant,
)
from mim_control_plane.domain.states import UserRole, UserState
from mim_control_plane.ports.slack_oauth import (
    SlackOAuthCredentialVault,
    SlackOAuthInstallRepository,
    SlackOAuthInstallRepositoryError,
    SlackOAuthProvider,
    SlackOAuthProviderError,
    SlackOAuthStateOwnerMismatch,
    SlackOAuthStateRejected,
    SlackOAuthStateRepository,
    SlackOAuthStateStoreError,
)
from mim_control_plane.security.authorization import IdentityPolicy
from mim_control_plane.security.identity import AuthenticatedPrincipal
from mim_control_plane.services.slack_oauth import (
    SlackOAuthCallbackRequest,
    SlackOAuthCompletionResult,
    SlackOAuthDenied,
    SlackOAuthEmployeeGrantRevokeRequest,
    SlackOAuthFlowError,
    SlackOAuthInstallRevokeRequest,
    SlackOAuthService,
    SlackOAuthStartRequest,
    _install_id,
)

NOW = datetime(2026, 8, 4, 3, 0, 0, tzinfo=UTC)
GROUP = "mim-users"
CLIENT_ID = "111.222"
OAUTH_CRED = "client-" + "secret"
REDIRECT_URI = "https://mim.madup.app/slack/oauth/callback"
AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
REQUIRED_SCOPES = ("chat:write", "commands")
INSTALL_TENANT = SlackOAuthTenant(team_id="T123", enterprise_id="E123")
RAW_STATE = "raw-state-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJKLMN"
SECRET_REF = "projects/mim-prod-123456/secrets/mim-slack-oauth/versions/17"


def user(
    *,
    user_id: str = "admin-1",
    email: str = "admin@madup.com",
    role: UserRole = UserRole.ADMIN,
    state: UserState = UserState.ACTIVE,
    synced_at: datetime = NOW - timedelta(minutes=5),
) -> User:
    return User(
        id=UserId(user_id),
        email=email,
        role=role,
        state=state,
        groups=frozenset({GROUP}),
        identity_synced_at=synced_at,
        created_at=NOW - timedelta(days=1),
        updated_at=synced_at,
    )


def principal(
    *,
    user_id: str = "admin-1",
    email: str = "admin@madup.com",
    role: UserRole = UserRole.ADMIN,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=UserId(user_id),
        email=email,
        role=role,
    )


class InMemoryStateRepository(SlackOAuthStateRepository):
    def __init__(self) -> None:
        self.created_records: list[SlackOAuthPendingState] = []
        self.consumed_records: list[SlackOAuthPendingState] = []
        self._records: dict[str, SlackOAuthPendingState] = {}
        self._lock = threading.Lock()

    def create_pending_state(
        self,
        state: SlackOAuthPendingState,
    ) -> SlackOAuthPendingState:
        with self._lock:
            if state.state_id in self._records:
                raise SlackOAuthStateStoreError("pending state already exists")
            self._records[state.state_id] = state
            self.created_records.append(state)
            return state

    def consume_pending_state(
        self,
        *,
        state_id: str,
        state_hash: str,
        expected_installer_mim_user_id: str,
        expected_installer_email: str,
        expected_tenant: SlackOAuthTenant,
        expected_redirect_uri: str,
        expected_scopes: tuple[str, ...],
        now: datetime,
    ) -> SlackOAuthPendingState:
        with self._lock:
            record = self._records.get(state_id)
            if record is None or record.state_hash != state_hash:
                raise SlackOAuthStateRejected("state rejected")
            if (
                record.installer_mim_user_id != expected_installer_mim_user_id
                or record.installer_email != expected_installer_email.casefold()
            ):
                raise SlackOAuthStateOwnerMismatch("wrong admin")
            if record.install_tenant != expected_tenant:
                raise SlackOAuthStateRejected("wrong tenant")
            if record.redirect_uri != expected_redirect_uri:
                raise SlackOAuthStateRejected("wrong redirect")
            if record.required_scopes != expected_scopes:
                raise SlackOAuthStateRejected("wrong scopes")
            if now >= record.expires_at or record.consumed_at is not None:
                raise SlackOAuthStateRejected("expired or replayed")
            consumed = SlackOAuthPendingState(
                state_id=record.state_id,
                state_hash=record.state_hash,
                installer_mim_user_id=record.installer_mim_user_id,
                installer_email=record.installer_email,
                required_scopes=record.required_scopes,
                redirect_uri=record.redirect_uri,
                install_tenant=record.install_tenant,
                issued_at=record.issued_at,
                expires_at=record.expires_at,
                consumed_at=now,
                version=record.version + 1,
            )
            self._records[state_id] = consumed
            self.consumed_records.append(consumed)
            return consumed


class OffsetVersionStateRepository(InMemoryStateRepository):
    def consume_pending_state(
        self,
        *,
        state_id: str,
        state_hash: str,
        expected_installer_mim_user_id: str,
        expected_installer_email: str,
        expected_tenant: SlackOAuthTenant,
        expected_redirect_uri: str,
        expected_scopes: tuple[str, ...],
        now: datetime,
    ) -> SlackOAuthPendingState:
        consumed = super().consume_pending_state(
            state_id=state_id,
            state_hash=state_hash,
            expected_installer_mim_user_id=expected_installer_mim_user_id,
            expected_installer_email=expected_installer_email,
            expected_tenant=expected_tenant,
            expected_redirect_uri=expected_redirect_uri,
            expected_scopes=expected_scopes,
            now=now,
        )
        shifted = SlackOAuthPendingState(
            state_id=consumed.state_id,
            state_hash=consumed.state_hash,
            installer_mim_user_id=consumed.installer_mim_user_id,
            installer_email=consumed.installer_email,
            required_scopes=consumed.required_scopes,
            redirect_uri=consumed.redirect_uri,
            install_tenant=consumed.install_tenant,
            issued_at=consumed.issued_at,
            expires_at=consumed.expires_at,
            consumed_at=consumed.consumed_at,
            version=7,
        )
        self._records[state_id] = shifted
        self.consumed_records[-1] = shifted
        return shifted


class InMemoryInstallRepository(SlackOAuthInstallRepository):
    def __init__(self) -> None:
        self.shared_installs: dict[str, SlackOAuthSharedInstall] = {}
        self.identity_links: dict[tuple[str, str], SlackOAuthIdentityLink] = {}
        self.saved_installs: list[SlackOAuthSharedInstall] = []
        self.revoked_installs: list[SlackOAuthSharedInstall] = []
        self.revoked_links: list[SlackOAuthIdentityLink] = []
        self.fail_on_save = False
        self._lock = threading.Lock()

    def get_active_install_for_tenant(
        self,
        *,
        team_id: str,
        enterprise_id: str | None,
    ) -> SlackOAuthSharedInstall | None:
        with self._lock:
            return self._find_active_install_for_tenant(
                team_id=team_id,
                enterprise_id=enterprise_id,
            )
        return None

    def _find_active_install_for_tenant(
        self,
        *,
        team_id: str,
        enterprise_id: str | None,
    ) -> SlackOAuthSharedInstall | None:
        for install in self.shared_installs.values():
            if (
                install.team_id == team_id
                and install.enterprise_id == enterprise_id
                and install.state is SlackOAuthInstallState.ACTIVE
            ):
                return install
        return None

    def save_shared_install(
        self,
        install: SlackOAuthSharedInstall,
    ) -> SlackOAuthSharedInstall:
        with self._lock:
            if self.fail_on_save:
                raise SlackOAuthInstallRepositoryError("save failed")
            existing = self._find_active_install_for_tenant(
                team_id=install.team_id,
                enterprise_id=install.enterprise_id,
            )
            if existing is not None and existing.install_id != install.install_id:
                raise SlackOAuthInstallRepositoryError("active install exists")
            current = self.shared_installs.get(install.install_id)
            if current is not None and current.state is SlackOAuthInstallState.ACTIVE:
                raise SlackOAuthInstallRepositoryError("active install exists")
            self.shared_installs[install.install_id] = install
            self.saved_installs.append(install)
            return install

    def get_shared_install(self, *, install_id: str) -> SlackOAuthSharedInstall:
        try:
            return self.shared_installs[install_id]
        except KeyError:
            raise SlackOAuthInstallRepositoryError("missing install") from None

    def revoke_shared_install(
        self,
        *,
        install_id: str,
        revoked_at: datetime,
    ) -> SlackOAuthSharedInstall:
        with self._lock:
            current = self.get_shared_install(install_id=install_id)
            revoked = SlackOAuthSharedInstall(
                install_id=current.install_id,
                app_id=current.app_id,
                team_id=current.team_id,
                enterprise_id=current.enterprise_id,
                is_enterprise_install=current.is_enterprise_install,
                granted_scopes=current.granted_scopes,
                secret_ref=current.secret_ref,
                installer_mim_user_id=current.installer_mim_user_id,
                installer_email=current.installer_email,
                created_at=current.created_at,
                updated_at=revoked_at,
                state=SlackOAuthInstallState.REVOKED,
                revoked_at=revoked_at,
                version=current.version + 1,
            )
            self.shared_installs[install_id] = revoked
            self.revoked_installs.append(revoked)
            return revoked

    def save_identity_link(
        self,
        link: SlackOAuthIdentityLink,
    ) -> SlackOAuthIdentityLink:
        self.identity_links[(link.install_id, link.mim_user_id)] = link
        return link

    def revoke_identity_link(
        self,
        *,
        install_id: str,
        mim_user_id: str,
        revoked_at: datetime,
    ) -> SlackOAuthIdentityLink:
        key = (install_id, mim_user_id)
        try:
            current = self.identity_links[key]
        except KeyError:
            raise SlackOAuthInstallRepositoryError("missing link") from None
        revoked = SlackOAuthIdentityLink(
            install_id=current.install_id,
            team_id=current.team_id,
            slack_user_id=current.slack_user_id,
            mim_user_id=current.mim_user_id,
            company_email=current.company_email,
            created_at=current.created_at,
            updated_at=revoked_at,
            state=SlackOAuthIdentityLinkState.REVOKED,
            revoked_at=revoked_at,
            version=current.version + 1,
        )
        self.identity_links[key] = revoked
        self.revoked_links.append(revoked)
        return revoked


class BarrierInstallRepository(InMemoryInstallRepository):
    def __init__(self, *, barrier: threading.Barrier) -> None:
        super().__init__()
        self._barrier = barrier

    def save_shared_install(
        self,
        install: SlackOAuthSharedInstall,
    ) -> SlackOAuthSharedInstall:
        self._barrier.wait(timeout=2)
        return super().save_shared_install(install)


class InMemoryCredentialVault(SlackOAuthCredentialVault):
    def __init__(self) -> None:
        self.destroyed_refs: tuple[str, ...] = ()

    def destroy_secret_ref(self, *, secret_ref: str) -> None:
        self.destroyed_refs = self.destroyed_refs + (secret_ref,)


class InMemoryProvider(SlackOAuthProvider):
    def __init__(self) -> None:
        self.grants_by_code: dict[str, SlackOAuthGrant] = {}
        self.exchange_calls: tuple[tuple[str, str], ...] = ()
        self.revoked_refs: tuple[str, ...] = ()
        self.uninstalled_refs: tuple[str, ...] = ()
        self.app_uninstalled_refs: tuple[str, ...] = ()

    def register_code(self, code: str, grant: SlackOAuthGrant) -> None:
        self.grants_by_code[code] = grant

    def exchange_installation_code(
        self,
        *,
        code: str,
        redirect_uri: str,
    ) -> SlackOAuthGrant:
        self.exchange_calls = self.exchange_calls + ((code, redirect_uri),)
        try:
            return self.grants_by_code[code]
        except KeyError:
            raise SlackOAuthProviderError("exchange failed") from None

    def revoke_installation(self, *, secret_ref: str) -> None:
        self.revoked_refs = self.revoked_refs + (secret_ref,)

    def uninstall_installation(
        self,
        *,
        secret_ref: str,
        app_id: str,
        team_id: str,
        enterprise_id: str | None,
        is_enterprise_install: bool,
    ) -> None:
        del app_id, team_id
        if is_enterprise_install:
            if enterprise_id is None:
                raise SlackOAuthProviderError("unsupported enterprise uninstall")
            raise SlackOAuthProviderError("unsupported enterprise uninstall")
        if enterprise_id is not None:
            raise SlackOAuthProviderError("non-enterprise uninstall mismatch")
        self.uninstalled_refs = self.uninstalled_refs + (secret_ref,)
        self.app_uninstalled_refs = self.app_uninstalled_refs + (secret_ref,)


class SlackOAuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.admin = self.store.create_user(user())
        self.identity_policy = IdentityPolicy(
            store=self.store,
            issuer="https://tenant.cloudflareaccess.com",
            audience="audience-1",
            company_domain="madup.com",
            required_group=GROUP,
            max_staleness=timedelta(minutes=60),
            clock=lambda: NOW,
        )
        self.state_repo = InMemoryStateRepository()
        self.install_repo = InMemoryInstallRepository()
        self.vault = InMemoryCredentialVault()
        self.provider = InMemoryProvider()
        self.service = self.make_service()

    def make_service(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        state_factory: Callable[[], str] | None = None,
        state_repo: InMemoryStateRepository | None = None,
        install_repo: InMemoryInstallRepository | None = None,
        vault: InMemoryCredentialVault | None = None,
        provider: InMemoryProvider | None = None,
    ) -> SlackOAuthService:
        return SlackOAuthService(
            identity_policy=self.identity_policy,
            state_repository=state_repo or self.state_repo,
            install_repository=install_repo or self.install_repo,
            credential_vault=vault or self.vault,
            provider=provider or self.provider,
            client_id=CLIENT_ID,
            client_secret=OAUTH_CRED,
            authorize_url=AUTHORIZE_URL,
            redirect_uri=REDIRECT_URI,
            required_scopes=REQUIRED_SCOPES,
            install_tenant=INSTALL_TENANT,
            clock=clock or (lambda: NOW),
            state_factory=state_factory or (lambda: RAW_STATE),
        )

    def start(self, *, actor: AuthenticatedPrincipal | None = None):
        return self.service.start_installation(
            SlackOAuthStartRequest(
                principal=actor or principal(email="ADMIN@madup.com"),
            )
        )

    def complete(
        self,
        *,
        state: str,
        code: str,
        actor: AuthenticatedPrincipal | None = None,
    ) -> SlackOAuthCompletionResult:
        return self.service.complete_installation(
            SlackOAuthCallbackRequest(
                principal=actor or principal(email="ADMIN@madup.com"),
                state=state,
                code=code,
            )
        )

    def test_start_requires_current_active_admin_and_persists_hashed_state_only(
        self,
    ) -> None:
        result = self.start()

        parsed = urlparse(result.authorization_url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "slack.com")
        self.assertEqual(parsed.path, "/oauth/v2/authorize")
        query = parse_qs(parsed.query, strict_parsing=True)
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["scope"], [",".join(REQUIRED_SCOPES)])
        self.assertEqual(query["state"], [RAW_STATE])
        self.assertEqual(query["team"], [INSTALL_TENANT.team_id])
        self.assertNotIn("code_challenge", query)
        self.assertNotIn("client_secret", query)
        self.assertNotIn(RAW_STATE, repr(result))

        stored = self.state_repo.created_records[0]
        self.assertEqual(stored.installer_mim_user_id, self.admin.id)
        self.assertEqual(stored.installer_email, "admin@madup.com")
        self.assertEqual(stored.required_scopes, REQUIRED_SCOPES)
        self.assertEqual(stored.redirect_uri, REDIRECT_URI)
        self.assertEqual(stored.install_tenant, INSTALL_TENANT)
        self.assertNotEqual(stored.state_hash, RAW_STATE)
        self.assertNotIn(RAW_STATE, repr(stored))

    def test_start_denies_non_admin_disabled_or_stale_principal(self) -> None:
        cases = (
            ("non-admin", principal(role=UserRole.USER), user(role=UserRole.USER)),
            ("disabled", principal(), user(state=UserState.SUSPENDED)),
            (
                "stale",
                principal(),
                user(synced_at=NOW - timedelta(hours=2)),
            ),
        )
        for label, actor, persisted_user in cases:
            with self.subTest(label=label):
                isolated_store = MemoryStore()
                isolated_store.create_user(persisted_user)
                service = SlackOAuthService(
                    identity_policy=IdentityPolicy(
                        store=isolated_store,
                        issuer="https://tenant.cloudflareaccess.com",
                        audience="audience-1",
                        company_domain="madup.com",
                        required_group=GROUP,
                        max_staleness=timedelta(minutes=60),
                        clock=lambda: NOW,
                    ),
                    state_repository=InMemoryStateRepository(),
                    install_repository=InMemoryInstallRepository(),
                    credential_vault=InMemoryCredentialVault(),
                    provider=InMemoryProvider(),
                    client_id=CLIENT_ID,
                    client_secret=OAUTH_CRED,
                    authorize_url=AUTHORIZE_URL,
                    redirect_uri=REDIRECT_URI,
                    required_scopes=REQUIRED_SCOPES,
                    install_tenant=INSTALL_TENANT,
                    clock=lambda: NOW,
                    state_factory=lambda: RAW_STATE,
                )
                with self.assertRaises(SlackOAuthDenied):
                    service.start_installation(SlackOAuthStartRequest(principal=actor))

    def test_public_surfaces_exclude_tenant_overrides_and_raw_tokens(self) -> None:
        request_fields = {field.name for field in fields(SlackOAuthStartRequest)} | {
            field.name for field in fields(SlackOAuthCallbackRequest)
        }
        completion_fields = {field.name for field in fields(SlackOAuthCompletionResult)}
        grant_fields = {field.name for field in fields(SlackOAuthGrant)}

        forbidden_request_fields = {
            "client_id",
            "client_secret",
            "redirect_uri",
            "required_scopes",
            "install_tenant",
            "access_token",
            "refresh_token",
            "secret_payload",
            "state_hash",
        }
        forbidden_grant_fields = {"access_token", "refresh_token", "authed_user"}
        self.assertTrue(forbidden_request_fields.isdisjoint(request_fields))
        self.assertTrue(forbidden_grant_fields.isdisjoint(grant_fields))
        self.assertEqual(
            completion_fields,
            {
                "install_id",
                "app_id",
                "team_id",
                "enterprise_id",
                "is_enterprise_install",
                "granted_scopes",
                "secret_ref",
            },
        )

    def test_complete_persists_metadata_and_secret_ref_after_valid_exchange(
        self,
    ) -> None:
        self.start()
        self.provider.register_code(
            "code-1",
            SlackOAuthGrant(
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=REQUIRED_SCOPES,
                secret_ref=SECRET_REF,
            ),
        )

        result = self.complete(state=RAW_STATE, code="code-1")

        self.assertEqual(
            result,
            SlackOAuthCompletionResult(
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
                granted_scopes=REQUIRED_SCOPES,
                secret_ref=SECRET_REF,
            ),
        )
        saved = self.install_repo.saved_installs[0]
        self.assertEqual(saved.secret_ref, SECRET_REF)
        self.assertEqual(saved.state, SlackOAuthInstallState.ACTIVE)
        self.assertEqual(saved.installer_email, "admin@madup.com")
        self.assertEqual(self.provider.revoked_refs, ())
        self.assertEqual(self.vault.destroyed_refs, ())
        self.assertNotIn("token", repr(result).lower())

    def test_complete_accepts_repository_managed_consumed_versions(self) -> None:
        state_repo = OffsetVersionStateRepository()
        service = self.make_service(
            state_repo=state_repo,
            state_factory=lambda: "raw-state-4-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJK",
        )
        service.start_installation(SlackOAuthStartRequest(principal=principal()))
        self.provider.register_code(
            "code-versioned",
            SlackOAuthGrant(
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=REQUIRED_SCOPES,
                secret_ref=SECRET_REF,
            ),
        )

        result = service.complete_installation(
            SlackOAuthCallbackRequest(
                principal=principal(),
                state="raw-state-4-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJK",
                code="code-versioned",
            )
        )

        self.assertEqual(result.team_id, "T123")
        self.assertEqual(state_repo.consumed_records[-1].version, 7)

    def test_complete_rejects_duplicate_active_install_before_exchange(self) -> None:
        existing_install = SlackOAuthSharedInstall(
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
            granted_scopes=REQUIRED_SCOPES,
            secret_ref="projects/mim-prod-123456/secrets/mim-slack-oauth/versions/1",
            installer_mim_user_id=self.admin.id,
            installer_email=self.admin.email,
            created_at=NOW,
            updated_at=NOW,
            state=SlackOAuthInstallState.ACTIVE,
        )
        self.install_repo.shared_installs[existing_install.install_id] = (
            existing_install
        )
        self.start()
        self.provider.register_code(
            "code-duplicate",
            SlackOAuthGrant(
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=REQUIRED_SCOPES,
                secret_ref=SECRET_REF,
            ),
        )

        with self.assertRaises(SlackOAuthFlowError):
            self.complete(state=RAW_STATE, code="code-duplicate")

        self.assertEqual(self.provider.exchange_calls, ())
        self.assertEqual(self.vault.destroyed_refs, ())

    def test_complete_rejects_missing_expired_replayed_or_cross_admin_state(
        self,
    ) -> None:
        self.provider.register_code(
            "code-1",
            SlackOAuthGrant(
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=REQUIRED_SCOPES,
                secret_ref=SECRET_REF,
            ),
        )
        with self.assertRaises(SlackOAuthFlowError):
            self.complete(state="missing-state", code="code-1")

        self.start()
        expired_service = self.make_service(clock=lambda: NOW + timedelta(minutes=10))
        with self.assertRaises(SlackOAuthFlowError):
            expired_service.complete_installation(
                SlackOAuthCallbackRequest(
                    principal=principal(),
                    state=RAW_STATE,
                    code="code-1",
                )
            )

        second_state = "raw-state-2-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJK"
        second_service = self.make_service(
            state_repo=InMemoryStateRepository(),
            state_factory=lambda: second_state,
        )
        second_service.start_installation(SlackOAuthStartRequest(principal=principal()))
        with self.assertRaises(SlackOAuthDenied):
            second_service.complete_installation(
                SlackOAuthCallbackRequest(
                    principal=principal(
                        user_id="admin-2",
                        email="admin2@madup.com",
                    ),
                    state=second_state,
                    code="code-1",
                )
            )

        replay_state = "raw-state-3-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJK"
        replay_service = self.make_service(
            state_repo=InMemoryStateRepository(),
            state_factory=lambda: replay_state,
        )
        replay_service.start_installation(SlackOAuthStartRequest(principal=principal()))
        replay_service.complete_installation(
            SlackOAuthCallbackRequest(
                principal=principal(),
                state=replay_state,
                code="code-1",
            )
        )
        with self.assertRaises(SlackOAuthFlowError):
            replay_service.complete_installation(
                SlackOAuthCallbackRequest(
                    principal=principal(),
                    state=replay_state,
                    code="code-1",
                )
            )

    def test_complete_rejects_cross_tenant_or_under_scoped_grants(self) -> None:
        self.start()
        self.provider.register_code(
            "wrong-tenant",
            SlackOAuthGrant(
                app_id="A123",
                team_id="T999",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=REQUIRED_SCOPES,
                secret_ref=SECRET_REF,
            ),
        )
        with self.assertRaises(SlackOAuthFlowError):
            self.complete(state=RAW_STATE, code="wrong-tenant")

        second_service = self.make_service(
            state_repo=InMemoryStateRepository(),
            state_factory=lambda: "raw-state-2-abcdefghijklmnopqrstuvwxyz-ABCDE",
        )
        second_service.start_installation(SlackOAuthStartRequest(principal=principal()))
        self.provider.register_code(
            "under-scoped",
            SlackOAuthGrant(
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=("commands",),
                secret_ref=SECRET_REF,
            ),
        )
        with self.assertRaises(SlackOAuthFlowError):
            second_service.complete_installation(
                SlackOAuthCallbackRequest(
                    principal=principal(),
                    state="raw-state-2-abcdefghijklmnopqrstuvwxyz-ABCDE",
                    code="under-scoped",
                )
            )

    def test_complete_metadata_failure_revokes_slack_token_and_destroys_secret(
        self,
    ) -> None:
        self.install_repo.fail_on_save = True
        self.start()
        self.provider.register_code(
            "code-1",
            SlackOAuthGrant(
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=REQUIRED_SCOPES,
                secret_ref=SECRET_REF,
            ),
        )

        with self.assertRaises(SlackOAuthFlowError):
            self.complete(state=RAW_STATE, code="code-1")

        self.assertEqual(self.provider.revoked_refs, (SECRET_REF,))
        self.assertEqual(self.vault.destroyed_refs, (SECRET_REF,))

    def test_complete_race_conflict_compensates_losing_secret_and_keeps_one_active(
        self,
    ) -> None:
        barrier = threading.Barrier(2)
        shared_repo = BarrierInstallRepository(barrier=barrier)
        service_a = self.make_service(
            state_repo=InMemoryStateRepository(),
            install_repo=shared_repo,
            state_factory=lambda: "raw-state-a-abcdefghijklmnopqrstuvwxyz-ABCDEFGH",
        )
        service_b = self.make_service(
            state_repo=InMemoryStateRepository(),
            install_repo=shared_repo,
            state_factory=lambda: "raw-state-b-abcdefghijklmnopqrstuvwxyz-ABCDEFGH",
        )
        service_a.start_installation(SlackOAuthStartRequest(principal=principal()))
        service_b.start_installation(SlackOAuthStartRequest(principal=principal()))
        self.provider.register_code(
            "code-a",
            SlackOAuthGrant(
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=REQUIRED_SCOPES,
                secret_ref="projects/mim-prod-123456/secrets/mim-slack-oauth/versions/101",
            ),
        )
        self.provider.register_code(
            "code-b",
            SlackOAuthGrant(
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=REQUIRED_SCOPES,
                secret_ref="projects/mim-prod-123456/secrets/mim-slack-oauth/versions/102",
            ),
        )
        successes: list[str] = []
        failures: list[type[BaseException]] = []

        def worker(service: SlackOAuthService, state: str, code: str) -> None:
            try:
                result = service.complete_installation(
                    SlackOAuthCallbackRequest(
                        principal=principal(),
                        state=state,
                        code=code,
                    )
                )
                successes.append(result.secret_ref)
            except BaseException as exc:  # pragma: no cover - thread path
                failures.append(type(exc))

        threads = [
            threading.Thread(
                target=worker,
                args=(
                    service_a,
                    "raw-state-a-abcdefghijklmnopqrstuvwxyz-ABCDEFGH",
                    "code-a",
                ),
            ),
            threading.Thread(
                target=worker,
                args=(
                    service_b,
                    "raw-state-b-abcdefghijklmnopqrstuvwxyz-ABCDEFGH",
                    "code-b",
                ),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(len(successes), 1)
        self.assertEqual(failures, [SlackOAuthFlowError])
        active_refs = {
            install.secret_ref
            for install in shared_repo.shared_installs.values()
            if install.state is SlackOAuthInstallState.ACTIVE
        }
        self.assertEqual(active_refs, set(successes))
        losing_ref = (
            "projects/mim-prod-123456/secrets/mim-slack-oauth/versions/102"
            if successes[0]
            == "projects/mim-prod-123456/secrets/mim-slack-oauth/versions/101"
            else "projects/mim-prod-123456/secrets/mim-slack-oauth/versions/101"
        )
        self.assertIn(losing_ref, self.provider.revoked_refs)
        self.assertIn(losing_ref, self.vault.destroyed_refs)

    def test_concurrent_double_callback_allows_exactly_one_success(self) -> None:
        self.start()
        self.provider.register_code(
            "code-1",
            SlackOAuthGrant(
                app_id="A123",
                team_id="T123",
                enterprise_id="E123",
                is_enterprise_install=True,
                granted_scopes=REQUIRED_SCOPES,
                secret_ref=SECRET_REF,
            ),
        )
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        failures: list[type[BaseException]] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=2)
                outcomes.append(
                    self.complete(state=RAW_STATE, code="code-1").install_id
                )
            except BaseException as exc:  # pragma: no cover - exercised in test
                failures.append(type(exc))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(failures, [SlackOAuthFlowError])
        self.assertEqual(len(self.provider.exchange_calls), 1)

    def test_revoke_installation_is_admin_only_and_cleans_up_secret(self) -> None:
        self.store.create_user(
            user(
                user_id="employee-2",
                email="employee2@madup.com",
                role=UserRole.USER,
            )
        )
        install = SlackOAuthSharedInstall(
            install_id=_install_id(
                app_id="A123",
                team_id="T123",
                enterprise_id=None,
                is_enterprise_install=False,
            ),
            app_id="A123",
            team_id="T123",
            enterprise_id=None,
            is_enterprise_install=False,
            granted_scopes=REQUIRED_SCOPES,
            secret_ref=SECRET_REF,
            installer_mim_user_id=self.admin.id,
            installer_email=self.admin.email,
            created_at=NOW,
            updated_at=NOW,
            state=SlackOAuthInstallState.ACTIVE,
        )
        self.install_repo.save_shared_install(install)

        with self.assertRaises(SlackOAuthDenied):
            self.service.revoke_installation(
                SlackOAuthInstallRevokeRequest(
                    principal=principal(
                        user_id="employee-2",
                        email="employee2@madup.com",
                        role=UserRole.USER,
                    ),
                    install_id=install.install_id,
                )
            )

        revoked = self.service.revoke_installation(
            SlackOAuthInstallRevokeRequest(
                principal=principal(),
                install_id=install.install_id,
            )
        )
        self.assertEqual(revoked.state, SlackOAuthInstallState.REVOKED)
        self.assertEqual(self.provider.uninstalled_refs, (SECRET_REF,))
        self.assertEqual(self.vault.destroyed_refs, (SECRET_REF,))

    def test_revoke_installation_rejects_unsupported_enterprise_uninstall(self) -> None:
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
            granted_scopes=REQUIRED_SCOPES,
            secret_ref=SECRET_REF,
            installer_mim_user_id=self.admin.id,
            installer_email=self.admin.email,
            created_at=NOW,
            updated_at=NOW,
            state=SlackOAuthInstallState.ACTIVE,
        )
        self.install_repo.shared_installs[install.install_id] = install

        with self.assertRaises(SlackOAuthFlowError):
            self.service.revoke_installation(
                SlackOAuthInstallRevokeRequest(
                    principal=principal(),
                    install_id=install.install_id,
                )
            )

        self.assertEqual(self.provider.uninstalled_refs, ())
        self.assertEqual(self.vault.destroyed_refs, ())

    def test_revoke_employee_grant_never_uninstalls_shared_app(self) -> None:
        install_id = _install_id(
            app_id="A123",
            team_id="T123",
            enterprise_id="E123",
            is_enterprise_install=True,
        )
        install = SlackOAuthSharedInstall(
            install_id=install_id,
            app_id="A123",
            team_id="T123",
            enterprise_id="E123",
            is_enterprise_install=True,
            granted_scopes=REQUIRED_SCOPES,
            secret_ref=SECRET_REF,
            installer_mim_user_id=self.admin.id,
            installer_email=self.admin.email,
            created_at=NOW,
            updated_at=NOW,
            state=SlackOAuthInstallState.ACTIVE,
        )
        self.install_repo.save_shared_install(install)
        self.install_repo.save_identity_link(
            SlackOAuthIdentityLink(
                install_id=install_id,
                team_id="T123",
                slack_user_id="U123",
                mim_user_id=UserId("employee-1"),
                company_email="employee@madup.com",
                created_at=NOW,
                updated_at=NOW,
                state=SlackOAuthIdentityLinkState.ACTIVE,
            )
        )

        revoked = self.service.revoke_employee_grant(
            SlackOAuthEmployeeGrantRevokeRequest(
                principal=principal(),
                install_id=install_id,
                mim_user_id=UserId("employee-1"),
            )
        )

        self.assertEqual(revoked.state, SlackOAuthIdentityLinkState.REVOKED)
        self.assertEqual(self.provider.uninstalled_refs, ())
        self.assertEqual(self.provider.revoked_refs, ())
        self.assertEqual(self.vault.destroyed_refs, ())

    def test_constructor_rejects_non_official_or_query_bearing_urls(self) -> None:
        with self.assertRaises(ValueError):
            SlackOAuthService(
                identity_policy=self.identity_policy,
                state_repository=self.state_repo,
                install_repository=self.install_repo,
                credential_vault=self.vault,
                provider=self.provider,
                client_id=CLIENT_ID,
                client_secret=OAUTH_CRED,
                authorize_url="https://slack.com/oauth/authorize",
                redirect_uri=REDIRECT_URI,
                required_scopes=REQUIRED_SCOPES,
                install_tenant=INSTALL_TENANT,
                clock=lambda: NOW,
            )
        with self.assertRaises(ValueError):
            SlackOAuthService(
                identity_policy=self.identity_policy,
                state_repository=self.state_repo,
                install_repository=self.install_repo,
                credential_vault=self.vault,
                provider=self.provider,
                client_id=CLIENT_ID,
                client_secret=OAUTH_CRED,
                authorize_url=AUTHORIZE_URL,
                redirect_uri=f"{REDIRECT_URI}?next=/bad",
                required_scopes=REQUIRED_SCOPES,
                install_tenant=INSTALL_TENANT,
                clock=lambda: NOW,
            )

    def test_domain_surfaces_do_not_expose_open_redirects_or_credentials(self) -> None:
        self.assertNotIn(
            "access_token", {field.name for field in fields(SlackOAuthGrant)}
        )
        self.assertNotIn(
            "refresh_token", {field.name for field in fields(SlackOAuthGrant)}
        )
        result = self.start()
        self.assertTrue(result.authorization_url.startswith(AUTHORIZE_URL))
        self.assertNotIn("http://", result.authorization_url)
        self.assertNotIn(OAUTH_CRED, repr(result))


if __name__ == "__main__":
    unittest.main()
