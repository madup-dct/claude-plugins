from __future__ import annotations

import dataclasses
import unittest
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import cast

from mim_control_plane.adapters.fake_identity import (
    FakeActionPolicyAuthorizer,
    FakeIdentityRegistry,
)
from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.central_identity import (
    ActionIntent,
    ActionName,
    SlackIdentityLink,
    SlackIdentityLinkState,
    SlackSharedInstall,
    SlackSharedInstallState,
    VerifiedSlackActor,
)
from mim_control_plane.domain.models import OriginRequestId, User, UserId
from mim_control_plane.domain.states import UserRole, UserState
from mim_control_plane.ports.identity import (
    ActionPolicyAuthorizer,
    ActionPolicyDecision,
)
from mim_control_plane.security.authorization import IdentityPolicy
from mim_control_plane.security.identity import (
    AuthenticationRequest,
    IdentityAuthenticator,
    IdentityClaims,
)
from mim_control_plane.security.origin import (
    OriginHmacVerifier,
    OriginRequest,
    sign_origin_request,
)
from mim_control_plane.services.central_identity import (
    AuthorizedAction,
    BrowserActionRequest,
    CentralIdentityDenied,
    CentralIdentityGateway,
    IdentitySurface,
    SlackActionRequest,
)

NOW = datetime(2026, 8, 3, 9, 0, 0, tzinfo=UTC)
ISSUER = "https://tenant.cloudflareaccess.com"
AUDIENCE = "audience-1"
GROUP = "mim-users"
ORIGIN_KEY = b"k" * 32
INSTALLER_ID = "admin-1"
INSTALLER_EMAIL = "admin@madup.com"
REQUIRED_SCOPES = frozenset({"commands", "chat:write"})


def user(
    *,
    user_id: str = "usr-1",
    email: str = "person@madup.com",
    role: UserRole = UserRole.USER,
    state: UserState = UserState.ACTIVE,
    groups: frozenset[str] = frozenset({GROUP}),
    synced_at: datetime = NOW - timedelta(minutes=5),
) -> User:
    return User(
        id=UserId(user_id),
        email=email,
        role=role,
        state=state,
        groups=groups,
        identity_synced_at=synced_at,
        created_at=NOW - timedelta(days=1),
        updated_at=synced_at,
    )


def claims(
    *,
    subject: str = "usr-1",
    email: str = "person@madup.com",
    issued_at: datetime = NOW - timedelta(minutes=1),
    expires_at: datetime = NOW + timedelta(minutes=10),
) -> IdentityClaims:
    return IdentityClaims(
        subject=subject,
        email=email,
        issuer=ISSUER,
        audience=(AUDIENCE,),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def install(
    *,
    install_id: str = "ins-1",
    team_id: str = "T123",
    enterprise_id: str | None = None,
    state: SlackSharedInstallState = SlackSharedInstallState.ACTIVE,
    revoked_at: datetime | None = None,
    granted_scopes: tuple[str, ...] = ("commands", "chat:write"),
    installer_mim_user_id: str = INSTALLER_ID,
    installer_email: str = INSTALLER_EMAIL,
    updated_at: datetime | None = None,
) -> SlackSharedInstall:
    return SlackSharedInstall(
        install_id=install_id,
        team_id=team_id,
        enterprise_id=enterprise_id,
        granted_scopes=granted_scopes,
        installer_mim_user_id=UserId(installer_mim_user_id),
        installer_email=installer_email,
        created_at=NOW - timedelta(days=2),
        updated_at=updated_at or revoked_at or NOW - timedelta(hours=1),
        state=state,
        revoked_at=revoked_at,
    )


def link(
    *,
    install_id: str = "ins-1",
    team_id: str = "T123",
    slack_user_id: str = "U123",
    mim_user_id: str = "usr-1",
    company_email: str = "person@madup.com",
    state: SlackIdentityLinkState = SlackIdentityLinkState.ACTIVE,
    revoked_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> SlackIdentityLink:
    return SlackIdentityLink(
        install_id=install_id,
        team_id=team_id,
        slack_user_id=slack_user_id,
        mim_user_id=UserId(mim_user_id),
        company_email=company_email,
        verified_at=NOW - timedelta(hours=2),
        created_at=NOW - timedelta(days=2),
        updated_at=updated_at or revoked_at or NOW - timedelta(hours=1),
        state=state,
        revoked_at=revoked_at,
    )


def actor(
    *,
    install_id: str = "ins-1",
    team_id: str = "T123",
    enterprise_id: str | None = None,
    slack_user_id: str = "U123",
    company_email: str = "person@madup.com",
) -> VerifiedSlackActor:
    return VerifiedSlackActor(
        install_id=install_id,
        team_id=team_id,
        enterprise_id=enterprise_id,
        slack_user_id=slack_user_id,
        company_email=company_email,
        verified_at=NOW,
    )


def browser_request() -> AuthenticationRequest:
    unsigned_origin = OriginRequest(
        method="POST",
        path="/mcp",
        body=b"{}",
        timestamp=NOW,
        request_id=OriginRequestId("req-1"),
        public_host="mim.madup.app",
        destination_class="control-plane",
        key_id="test",
        signature=None,
    )
    return AuthenticationRequest(
        origin=dataclasses.replace(
            unsigned_origin,
            signature=sign_origin_request(unsigned_origin, key=ORIGIN_KEY),
        ),
        headers=(("Cf-Access-Jwt-Assertion", "opaque"),),
    )


class StaticJwtVerifier:
    def __init__(self, verified_claims: IdentityClaims) -> None:
        self._verified_claims = verified_claims

    def verify(self, token: str) -> IdentityClaims:
        del token
        return self._verified_claims


class ForcedInstallDirectory:
    def __init__(self, record: SlackSharedInstall) -> None:
        self._record = record

    def get_shared_install(
        self,
        *,
        install_id: str,
        team_id: str,
        enterprise_id: str | None,
    ) -> SlackSharedInstall:
        del install_id, team_id, enterprise_id
        return self._record


class ForcedLinkDirectory:
    def __init__(self, record: SlackIdentityLink) -> None:
        self._record = record

    def get_identity_link(
        self,
        *,
        install_id: str,
        team_id: str,
        slack_user_id: str,
    ) -> SlackIdentityLink:
        del install_id, team_id, slack_user_id
        return self._record


class MalformedActionPolicyAuthorizer:
    def __init__(self, response: object) -> None:
        self._response = response

    def authorize(
        self,
        *,
        principal: object,
        intent: object,
        surface: str,
    ) -> object:
        del principal, intent, surface
        return self._response


def browser_authenticator(
    store: MemoryStore,
    *,
    verified_claims: IdentityClaims,
) -> IdentityAuthenticator:
    return IdentityAuthenticator(
        origin_verifier=OriginHmacVerifier(
            keys={"test": ORIGIN_KEY},
            store=store,
            clock=lambda: NOW,
            window=timedelta(seconds=60),
        ),
        jwt_verifier=StaticJwtVerifier(verified_claims),
        identity_policy=IdentityPolicy(
            store=store,
            issuer=ISSUER,
            audience=AUDIENCE,
            company_domain="madup.com",
            required_group=GROUP,
            max_staleness=timedelta(minutes=60),
            clock=lambda: NOW,
        ),
    )


class CentralIdentityGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(user())
        self.store.create_user(
            user(
                user_id=INSTALLER_ID,
                email=INSTALLER_EMAIL,
                role=UserRole.ADMIN,
            )
        )
        self.identity_policy = IdentityPolicy(
            store=self.store,
            issuer=ISSUER,
            audience=AUDIENCE,
            company_domain="madup.com",
            required_group=GROUP,
            max_staleness=timedelta(minutes=60),
            clock=lambda: NOW,
        )
        self.registry = FakeIdentityRegistry(
            installs=(install(),),
            links=(link(),),
        )
        self.authorizer = FakeActionPolicyAuthorizer()
        self.gateway = CentralIdentityGateway(
            browser_authenticator=browser_authenticator(
                self.store,
                verified_claims=claims(),
            ),
            identity_policy=self.identity_policy,
            shared_install_directory=self.registry,
            identity_link_directory=self.registry,
            action_authorizer=self.authorizer,
            required_slack_scopes=REQUIRED_SCOPES,
            clock=lambda: NOW,
        )

    def test_browser_and_slack_happy_paths_authorize_through_action_policy(
        self,
    ) -> None:
        intent = ActionIntent(
            action=ActionName.VIEW_DASHBOARD,
            resource_id="dashboard:usr-1",
        )

        browser = self.gateway.authorize_browser(
            BrowserActionRequest(
                authentication_request=browser_request(),
                intent=intent,
            )
        )
        slack = self.gateway.authorize_slack(
            SlackActionRequest(
                actor=actor(),
                intent=intent,
            )
        )

        self.assertEqual(browser.principal.user_id, UserId("usr-1"))
        self.assertEqual(browser.surface, IdentitySurface.BROWSER)
        self.assertEqual(slack.principal.user_id, UserId("usr-1"))
        self.assertEqual(slack.surface, IdentitySurface.SLACK)
        self.assertFalse(hasattr(browser, "decision"))
        self.assertFalse(hasattr(slack, "decision"))
        self.assertEqual(len(self.authorizer.calls), 2)
        self.assertEqual(
            [call.surface for call in self.authorizer.calls],
            ["browser", "slack"],
        )

    def test_authenticated_browser_reuses_principal_without_reauthenticating(
        self,
    ) -> None:
        original = self.gateway.authorize_browser(
            BrowserActionRequest(
                authentication_request=browser_request(),
                intent=ActionIntent(
                    action=ActionName.VIEW_DASHBOARD,
                    resource_id="dashboard:usr-1",
                ),
            )
        )

        authorized = self.gateway.authorize_authenticated_browser_for(
            authorized_browser=original,
            intent_factory=lambda principal: ActionIntent(
                action=ActionName.VIEW_USAGE,
                resource_id=f"usage:{principal.user_id}",
            ),
        )

        self.assertEqual(authorized.principal, original.principal)
        self.assertEqual(authorized.surface, IdentitySurface.BROWSER)
        self.assertEqual(authorized.intent.action, ActionName.VIEW_USAGE)
        self.assertEqual(authorized.intent.resource_id, "usage:usr-1")
        self.assertEqual(
            [call.surface for call in self.authorizer.calls],
            ["browser", "browser"],
        )

    def test_authenticated_browser_rejects_forged_or_wrong_surface_context(
        self,
    ) -> None:
        valid = self.gateway.authorize_browser(
            BrowserActionRequest(
                authentication_request=browser_request(),
                intent=ActionIntent(
                    action=ActionName.VIEW_DASHBOARD,
                    resource_id="dashboard:usr-1",
                ),
            )
        )

        with self.assertRaises(CentralIdentityDenied):
            self.gateway.authorize_authenticated_browser_for(
                authorized_browser=cast(AuthorizedAction, object()),
                intent_factory=lambda principal: ActionIntent(
                    action=ActionName.VIEW_USAGE,
                    resource_id=f"usage:{principal.user_id}",
                ),
            )

        with self.assertRaises(CentralIdentityDenied):
            self.gateway.authorize_authenticated_browser_for(
                authorized_browser=dataclasses.replace(
                    valid,
                    surface=IdentitySurface.SLACK,
                ),
                intent_factory=lambda principal: ActionIntent(
                    action=ActionName.VIEW_USAGE,
                    resource_id=f"usage:{principal.user_id}",
                ),
            )

    def test_slack_install_and_link_must_be_active_and_exactly_matched(self) -> None:
        denied_cases = (
            (
                FakeIdentityRegistry(
                    installs=(
                        install(
                            state=SlackSharedInstallState.REVOKED,
                            revoked_at=NOW - timedelta(minutes=2),
                        ),
                    ),
                    links=(link(),),
                ),
                actor(),
            ),
            (FakeIdentityRegistry(installs=(), links=(link(),)), actor()),
            (
                FakeIdentityRegistry(
                    installs=(install(),),
                    links=(
                        link(
                            state=SlackIdentityLinkState.REVOKED,
                            revoked_at=NOW - timedelta(minutes=1),
                        ),
                    ),
                ),
                actor(),
            ),
            (
                FakeIdentityRegistry(
                    installs=(install(team_id="T999"),),
                    links=(link(team_id="T999"),),
                ),
                actor(),
            ),
            (
                FakeIdentityRegistry(
                    installs=(install(),),
                    links=(link(company_email="other@madup.com"),),
                ),
                actor(),
            ),
            (
                FakeIdentityRegistry(
                    installs=(install(),),
                    links=(link(slack_user_id="U999"),),
                ),
                actor(),
            ),
        )

        for registry, verified_actor in denied_cases:
            with self.subTest(registry=registry, actor=verified_actor):
                gateway = CentralIdentityGateway(
                    browser_authenticator=browser_authenticator(
                        self.store,
                        verified_claims=claims(),
                    ),
                    identity_policy=self.identity_policy,
                    shared_install_directory=registry,
                    identity_link_directory=registry,
                    action_authorizer=self.authorizer,
                    required_slack_scopes=REQUIRED_SCOPES,
                    clock=lambda: NOW,
                )
                with self.assertRaises(CentralIdentityDenied):
                    gateway.authorize_slack(
                        SlackActionRequest(
                            actor=verified_actor,
                            intent=ActionIntent(
                                action=ActionName.VIEW_DASHBOARD,
                                resource_id="dashboard:usr-1",
                            ),
                        )
                    )

    def test_browser_and_slack_recheck_current_user_state_every_time(self) -> None:
        denied_users = (
            dataclasses.replace(
                self.store.get_user(UserId("usr-1")),
                state=UserState.SUSPENDED,
                updated_at=NOW,
                version=2,
            ),
            dataclasses.replace(
                self.store.get_user(UserId("usr-1")),
                state=UserState.OFFBOARDED,
                updated_at=NOW,
                version=2,
            ),
        )
        intent = ActionIntent(
            action=ActionName.VIEW_DASHBOARD,
            resource_id="dashboard:usr-1",
        )

        for replacement in denied_users:
            with self.subTest(replacement=replacement):
                store = MemoryStore()
                store.create_user(user())
                gateway = CentralIdentityGateway(
                    browser_authenticator=browser_authenticator(
                        store,
                        verified_claims=claims(),
                    ),
                    identity_policy=IdentityPolicy(
                        store=store,
                        issuer=ISSUER,
                        audience=AUDIENCE,
                        company_domain="madup.com",
                        required_group=GROUP,
                        max_staleness=timedelta(minutes=60),
                        clock=lambda: NOW,
                    ),
                    shared_install_directory=self.registry,
                    identity_link_directory=self.registry,
                    action_authorizer=FakeActionPolicyAuthorizer(),
                    required_slack_scopes=REQUIRED_SCOPES,
                    clock=lambda: NOW,
                )
                gateway.authorize_browser(
                    BrowserActionRequest(
                        authentication_request=browser_request(),
                        intent=intent,
                    )
                )
                original = store.get_user(UserId("usr-1"))
                store.save_user(replacement, expected_version=original.version)

                with self.assertRaises(CentralIdentityDenied):
                    gateway.authorize_browser(
                        BrowserActionRequest(
                            authentication_request=browser_request(),
                            intent=intent,
                        )
                    )
                with self.assertRaises(CentralIdentityDenied):
                    gateway.authorize_slack(
                        SlackActionRequest(
                            actor=actor(),
                            intent=intent,
                        )
                    )

    def test_browser_and_slack_deny_missing_group_and_stale_users(self) -> None:
        denied_records = (
            user(groups=frozenset()),
            user(synced_at=NOW - timedelta(minutes=61)),
        )
        intent = ActionIntent(
            action=ActionName.VIEW_DASHBOARD,
            resource_id="dashboard:usr-1",
        )

        for denied_record in denied_records:
            with self.subTest(denied_record=denied_record):
                store = MemoryStore()
                store.create_user(denied_record)
                gateway = CentralIdentityGateway(
                    browser_authenticator=browser_authenticator(
                        store,
                        verified_claims=claims(),
                    ),
                    identity_policy=IdentityPolicy(
                        store=store,
                        issuer=ISSUER,
                        audience=AUDIENCE,
                        company_domain="madup.com",
                        required_group=GROUP,
                        max_staleness=timedelta(minutes=60),
                        clock=lambda: NOW,
                    ),
                    shared_install_directory=self.registry,
                    identity_link_directory=self.registry,
                    action_authorizer=FakeActionPolicyAuthorizer(),
                    required_slack_scopes=REQUIRED_SCOPES,
                    clock=lambda: NOW,
                )

                with self.assertRaises(CentralIdentityDenied):
                    gateway.authorize_browser(
                        BrowserActionRequest(
                            authentication_request=browser_request(),
                            intent=intent,
                        )
                    )
                with self.assertRaises(CentralIdentityDenied):
                    gateway.authorize_slack(
                        SlackActionRequest(
                            actor=actor(),
                            intent=intent,
                        )
                    )

    def test_admin_scope_and_policy_denials_fail_closed(self) -> None:
        user_intent = ActionIntent(
            action=ActionName.ADMIN_USAGE_OVERVIEW,
            resource_id="admin:overview",
        )
        self.authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.ADMIN_USAGE_OVERVIEW,
            resource_id="admin:overview",
            reason_code="admin_required",
            audit_message="admin_scope_denied",
        )
        self.authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.DEPLOY_WORKLOAD,
            resource_id="repo:denied",
            reason_code="repository_not_admitted",
        )
        self.authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.MANAGE_SCHEDULE,
            resource_id="schedule:blocked",
            reason_code="quota_limit_reached",
        )
        self.authorizer.deny(
            user_id=UserId("usr-1"),
            action=ActionName.VIEW_USAGE,
            resource_id="budget:blocked",
            reason_code="budget_limit_reached",
        )

        denied_intents = (
            user_intent,
            ActionIntent(
                action=ActionName.DEPLOY_WORKLOAD,
                resource_id="repo:denied",
            ),
            ActionIntent(
                action=ActionName.MANAGE_SCHEDULE,
                resource_id="schedule:blocked",
            ),
            ActionIntent(
                action=ActionName.VIEW_USAGE,
                resource_id="budget:blocked",
            ),
        )
        for intent in denied_intents:
            with self.subTest(intent=intent):
                with self.assertRaises(CentralIdentityDenied) as raised:
                    self.gateway.authorize_slack(
                        SlackActionRequest(
                            actor=actor(),
                            intent=intent,
                        )
                    )
                self.assertEqual(
                    str(raised.exception),
                    "Identity is not authorized for MIM.",
                )

    def test_slack_install_alone_never_authorizes_without_an_exact_link(self) -> None:
        gateway = CentralIdentityGateway(
            browser_authenticator=browser_authenticator(
                self.store,
                verified_claims=claims(),
            ),
            identity_policy=self.identity_policy,
            shared_install_directory=FakeIdentityRegistry(
                installs=(install(),),
                links=(),
            ),
            identity_link_directory=FakeIdentityRegistry(
                installs=(install(),),
                links=(),
            ),
            action_authorizer=self.authorizer,
            required_slack_scopes=REQUIRED_SCOPES,
            clock=lambda: NOW,
        )

        with self.assertRaises(CentralIdentityDenied):
            gateway.authorize_slack(
                SlackActionRequest(
                    actor=actor(),
                    intent=ActionIntent(
                        action=ActionName.VIEW_DASHBOARD,
                        resource_id="dashboard:usr-1",
                    ),
                )
            )

    def test_slack_surface_fails_closed_when_disabled(self) -> None:
        gateway = CentralIdentityGateway(
            browser_authenticator=browser_authenticator(
                self.store,
                verified_claims=claims(),
            ),
            identity_policy=self.identity_policy,
            shared_install_directory=self.registry,
            identity_link_directory=self.registry,
            action_authorizer=self.authorizer,
            required_slack_scopes=frozenset(),
            clock=lambda: NOW,
        )

        with self.assertRaises(CentralIdentityDenied):
            gateway.authorize_slack(
                SlackActionRequest(
                    actor=actor(),
                    intent=ActionIntent(
                        action=ActionName.VIEW_DASHBOARD,
                        resource_id="dashboard:usr-1",
                    ),
                )
            )

    def test_slack_rechecks_returned_install_and_link_records(self) -> None:
        intent = ActionIntent(
            action=ActionName.VIEW_DASHBOARD,
            resource_id="dashboard:usr-1",
        )
        denied_gateways = (
            CentralIdentityGateway(
                browser_authenticator=browser_authenticator(
                    self.store,
                    verified_claims=claims(),
                ),
                identity_policy=self.identity_policy,
                shared_install_directory=ForcedInstallDirectory(
                    install(team_id="T999")
                ),
                identity_link_directory=ForcedLinkDirectory(link()),
                action_authorizer=self.authorizer,
                required_slack_scopes=REQUIRED_SCOPES,
                clock=lambda: NOW,
            ),
            CentralIdentityGateway(
                browser_authenticator=browser_authenticator(
                    self.store,
                    verified_claims=claims(),
                ),
                identity_policy=self.identity_policy,
                shared_install_directory=ForcedInstallDirectory(install()),
                identity_link_directory=ForcedLinkDirectory(
                    link(slack_user_id="U999")
                ),
                action_authorizer=self.authorizer,
                required_slack_scopes=REQUIRED_SCOPES,
                clock=lambda: NOW,
            ),
        )

        for gateway in denied_gateways:
            with self.subTest(gateway=gateway):
                with self.assertRaises(CentralIdentityDenied):
                    gateway.authorize_slack(
                        SlackActionRequest(
                            actor=actor(),
                            intent=intent,
                        )
                    )

    def test_public_identity_surfaces_exclude_cloud_credentials_and_secret_fields(
        self,
    ) -> None:
        forbidden_fragments = (
            "token",
            "secret",
            "quota",
            "budget",
            "billing",
            "project",
            "service_account",
            "operator",
        )
        public_types = (
            ActionIntent,
            VerifiedSlackActor,
            SlackSharedInstall,
            SlackIdentityLink,
            AuthorizedAction,
            BrowserActionRequest,
            SlackActionRequest,
        )

        for public_type in public_types:
            field_names = {field.name for field in fields(public_type)}
            with self.subTest(public_type=public_type.__name__, fields=field_names):
                for fragment in forbidden_fragments:
                    self.assertFalse(
                        any(fragment in field_name for field_name in field_names),
                        (
                            f"{public_type.__name__} leaked forbidden "
                            f"field fragment {fragment!r}"
                        ),
                    )

    def test_denials_do_not_echo_verified_actor_values(self) -> None:
        denied_actor = actor(
            slack_user_id="U-SECRET-USER",
            company_email="secret.person@madup.com",
        )

        with self.assertRaises(CentralIdentityDenied) as raised:
            self.gateway.authorize_slack(
                SlackActionRequest(
                    actor=denied_actor,
                    intent=ActionIntent(
                        action=ActionName.VIEW_DASHBOARD,
                        resource_id="dashboard:usr-1",
                    ),
                )
            )

        message = str(raised.exception)
        self.assertNotIn("U-SECRET-USER", message)
        self.assertNotIn("secret.person@madup.com", message)

    def test_action_policy_decision_requires_exact_bool_and_reason_code(self) -> None:
        with self.assertRaises(ValueError):
            ActionPolicyDecision(allowed=1, reason_code="allowed")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ActionPolicyDecision(allowed=True, reason_code=" ")

    def test_gateway_requires_non_empty_immutable_required_scope_set(self) -> None:
        invalid_scope_sets: tuple[object, ...] = (
            set(REQUIRED_SCOPES),
            frozenset({" "}),
        )
        for invalid_scope_set in invalid_scope_sets:
            with self.subTest(invalid_scope_set=invalid_scope_set):
                with self.assertRaises(ValueError):
                    CentralIdentityGateway(
                        browser_authenticator=browser_authenticator(
                            self.store,
                            verified_claims=claims(),
                        ),
                        identity_policy=self.identity_policy,
                        shared_install_directory=self.registry,
                        identity_link_directory=self.registry,
                        action_authorizer=self.authorizer,
                        required_slack_scopes=invalid_scope_set,  # type: ignore[arg-type]
                        clock=lambda: NOW,
                    )

    def test_browser_and_slack_deny_malformed_action_policy_objects(self) -> None:
        malformed_authorizers = (
            MalformedActionPolicyAuthorizer(object()),
            MalformedActionPolicyAuthorizer(
                type("DecisionLike", (), {"allowed": "false", "reason_code": "ok"})()
            ),
            MalformedActionPolicyAuthorizer(
                type("DecisionLike", (), {"allowed": 1, "reason_code": "ok"})()
            ),
        )
        intent = ActionIntent(
            action=ActionName.VIEW_DASHBOARD,
            resource_id="dashboard:usr-1",
        )
        for authorizer in malformed_authorizers:
            gateway = CentralIdentityGateway(
                browser_authenticator=browser_authenticator(
                    self.store,
                    verified_claims=claims(),
                ),
                identity_policy=self.identity_policy,
                shared_install_directory=self.registry,
                identity_link_directory=self.registry,
                action_authorizer=cast(ActionPolicyAuthorizer, authorizer),
                required_slack_scopes=REQUIRED_SCOPES,
                clock=lambda: NOW,
            )
            with self.subTest(authorizer=authorizer):
                with self.assertRaises(CentralIdentityDenied) as browser_denied:
                    gateway.authorize_browser(
                        BrowserActionRequest(
                            authentication_request=browser_request(),
                            intent=intent,
                        )
                    )
                with self.assertRaises(CentralIdentityDenied) as slack_denied:
                    gateway.authorize_slack(
                        SlackActionRequest(
                            actor=actor(),
                            intent=intent,
                        )
                    )
                self.assertEqual(
                    str(browser_denied.exception),
                    "Identity is not authorized for MIM.",
                )
                self.assertEqual(
                    str(slack_denied.exception),
                    "Identity is not authorized for MIM.",
                )

    def test_slack_actor_must_be_fresh_utc_and_not_future(self) -> None:
        denied_actors = (
            actor().__class__(
                install_id="ins-1",
                team_id="T123",
                enterprise_id=None,
                slack_user_id="U123",
                company_email="person@madup.com",
                verified_at=NOW - timedelta(minutes=6),
            ),
            actor().__class__(
                install_id="ins-1",
                team_id="T123",
                enterprise_id=None,
                slack_user_id="U123",
                company_email="person@madup.com",
                verified_at=NOW + timedelta(seconds=1),
            ),
        )
        intent = ActionIntent(
            action=ActionName.VIEW_DASHBOARD,
            resource_id="dashboard:usr-1",
        )
        for denied_actor in denied_actors:
            with self.subTest(denied_actor=denied_actor):
                with self.assertRaises(CentralIdentityDenied):
                    self.gateway.authorize_slack(
                        SlackActionRequest(
                            actor=denied_actor,
                            intent=intent,
                        )
                    )
        with self.assertRaises(ValueError):
            VerifiedSlackActor(
                install_id="ins-1",
                team_id="T123",
                enterprise_id=None,
                slack_user_id="U123",
                company_email="person@madup.com",
                verified_at=datetime(2026, 8, 3, 9, 0, 0),
            )

    def test_revocation_timestamps_and_exact_enum_types_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            ActionIntent(action="view_dashboard", resource_id="dashboard:usr-1")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            install(
                state="active",  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            link(
                state="active",  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            install(
                state=SlackSharedInstallState.REVOKED,
                revoked_at=NOW - timedelta(days=3),
            )
        with self.assertRaises(ValueError):
            install(
                state=SlackSharedInstallState.REVOKED,
                revoked_at=NOW,
                updated_at=NOW - timedelta(hours=1),
            )
        with self.assertRaises(ValueError):
            link(
                state=SlackIdentityLinkState.REVOKED,
                revoked_at=NOW - timedelta(days=3),
            )
        with self.assertRaises(ValueError):
            link(
                state=SlackIdentityLinkState.REVOKED,
                revoked_at=NOW,
                updated_at=NOW - timedelta(hours=1),
            )
        with self.assertRaises(ValueError):
            install(
                state=SlackSharedInstallState.ACTIVE,
                revoked_at=NOW - timedelta(hours=1),
            )

    def test_slack_requires_central_scopes_and_active_admin_installer(self) -> None:
        denied_gateways = (
            CentralIdentityGateway(
                browser_authenticator=browser_authenticator(
                    self.store,
                    verified_claims=claims(),
                ),
                identity_policy=self.identity_policy,
                shared_install_directory=FakeIdentityRegistry(
                    installs=(install(granted_scopes=("commands",)),),
                    links=(link(),),
                ),
                identity_link_directory=FakeIdentityRegistry(
                    installs=(install(granted_scopes=("commands",)),),
                    links=(link(),),
                ),
                action_authorizer=self.authorizer,
                required_slack_scopes=REQUIRED_SCOPES,
                clock=lambda: NOW,
            ),
        )
        for gateway in denied_gateways:
            with self.subTest(gateway=gateway):
                with self.assertRaises(CentralIdentityDenied):
                    gateway.authorize_slack(
                        SlackActionRequest(
                            actor=actor(),
                            intent=ActionIntent(
                                action=ActionName.VIEW_DASHBOARD,
                                resource_id="dashboard:usr-1",
                            ),
                        )
                    )

        installer_denied_records = (
            user(
                user_id=INSTALLER_ID,
                email=INSTALLER_EMAIL,
                role=UserRole.USER,
            ),
            user(
                user_id=INSTALLER_ID,
                email=INSTALLER_EMAIL,
                role=UserRole.ADMIN,
                state=UserState.SUSPENDED,
            ),
            user(
                user_id=INSTALLER_ID,
                email=INSTALLER_EMAIL,
                role=UserRole.ADMIN,
                state=UserState.OFFBOARDED,
            ),
            user(
                user_id=INSTALLER_ID,
                email=INSTALLER_EMAIL,
                role=UserRole.ADMIN,
                groups=frozenset(),
            ),
            user(
                user_id=INSTALLER_ID,
                email=INSTALLER_EMAIL,
                role=UserRole.ADMIN,
                synced_at=NOW - timedelta(minutes=61),
            ),
        )
        for installer_record in installer_denied_records:
            with self.subTest(installer_record=installer_record):
                store = MemoryStore()
                store.create_user(user())
                store.create_user(installer_record)
                identity_policy = IdentityPolicy(
                    store=store,
                    issuer=ISSUER,
                    audience=AUDIENCE,
                    company_domain="madup.com",
                    required_group=GROUP,
                    max_staleness=timedelta(minutes=60),
                    clock=lambda: NOW,
                )
                registry = FakeIdentityRegistry(
                    installs=(install(),),
                    links=(link(),),
                )
                gateway = CentralIdentityGateway(
                    browser_authenticator=browser_authenticator(
                        store,
                        verified_claims=claims(),
                    ),
                    identity_policy=identity_policy,
                    shared_install_directory=registry,
                    identity_link_directory=registry,
                    action_authorizer=FakeActionPolicyAuthorizer(),
                    required_slack_scopes=REQUIRED_SCOPES,
                    clock=lambda: NOW,
                )
                with self.assertRaises(CentralIdentityDenied):
                    gateway.authorize_slack(
                        SlackActionRequest(
                            actor=actor(),
                            intent=ActionIntent(
                                action=ActionName.VIEW_DASHBOARD,
                                resource_id="dashboard:usr-1",
                            ),
                        )
                    )


if __name__ == "__main__":
    unittest.main()
