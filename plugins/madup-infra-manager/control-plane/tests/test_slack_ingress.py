from __future__ import annotations

import unittest
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote_plus

from mim_control_plane.adapters.fake_identity import (
    FakeActionPolicyAuthorizer,
    FakeIdentityRegistry,
)
from mim_control_plane.adapters.fake_slack import (
    FakeSlackIdentityResolver,
    FakeSlackReplayRegistry,
)
from mim_control_plane.domain.central_identity import (
    ActionIntent,
    ActionName,
    SlackIdentityLink,
    SlackIdentityLinkState,
    SlackSharedInstall,
    SlackSharedInstallState,
)
from mim_control_plane.domain.models import User, UserId
from mim_control_plane.domain.states import UserRole, UserState
from mim_control_plane.ports.slack import (
    SlackIdentityResolution,
    SlackIdentityResolver,
)
from mim_control_plane.security.authorization import IdentityPolicy
from mim_control_plane.security.slack import sign_slack_request
from mim_control_plane.services.central_identity import (
    CentralIdentityDenied,
    CentralIdentityGateway,
    SlackActionRequest,
)
from mim_control_plane.services.slack_ingress import (
    SlackIngressDenied,
    SlackIngressGateway,
    SlackIngressRequest,
    VerifiedSlackIngress,
)

NOW = datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC)
SIGNING_SECRET = b"s" * 32
ALLOWED_COMMANDS = frozenset({"/mim"})
GROUP = "mim-users"
SLACK_DOCS_EXTRAS = (
    ("token", "legacy-verification-token"),
    ("team_domain", "madup"),
    ("channel_id", "C123"),
    ("response_url", "https://hooks.slack.com/commands/1"),
    ("trigger_id", "1337.42"),
    ("api_app_id", "A111"),
)


def encode_fields(items: tuple[tuple[str, str], ...]) -> bytes:
    return "&".join(
        f"{quote_plus(key)}={quote_plus(value)}" for key, value in items
    ).encode("utf-8")


def slash_body(
    *,
    team_id: str = "T123",
    user_id: str = "U123",
    command: str = "/mim",
    text: str = "status",
    enterprise_id: str | None = None,
    extras: tuple[tuple[str, str], ...] = (),
) -> bytes:
    items = [
        ("team_id", team_id),
        ("user_id", user_id),
        ("command", command),
        ("text", text),
    ]
    if enterprise_id is not None:
        items.append(("enterprise_id", enterprise_id))
    items.extend(extras)
    return encode_fields(tuple(items))


def signed_headers(
    body: bytes,
    *,
    timestamp: datetime = NOW,
    key: bytes = SIGNING_SECRET,
    signature: str | None = None,
    signature_name: str = "X-Slack-Signature",
    timestamp_name: str = "X-Slack-Request-Timestamp",
    content_type: str = "application/x-www-form-urlencoded",
) -> tuple[tuple[str, str], ...]:
    if signature is None:
        signature = sign_slack_request(
            body=body,
            timestamp=int(timestamp.timestamp()),
            key=key,
        )
    return (
        (signature_name, signature),
        (timestamp_name, str(int(timestamp.timestamp()))),
        ("Content-Type", content_type),
    )


def user(
    *,
    user_id: str = "usr-1",
    email: str = "person@madup.com",
    role: UserRole = UserRole.USER,
) -> User:
    return User(
        id=UserId(user_id),
        email=email,
        role=role,
        state=UserState.ACTIVE,
        groups=frozenset({GROUP}),
        identity_synced_at=NOW - timedelta(minutes=5),
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(minutes=5),
    )


def shared_install(
    *,
    install_id: str = "ins-1",
    team_id: str = "T123",
    enterprise_id: str | None = None,
    installer_mim_user_id: str = "admin-1",
    installer_email: str = "admin@madup.com",
) -> SlackSharedInstall:
    return SlackSharedInstall(
        install_id=install_id,
        team_id=team_id,
        enterprise_id=enterprise_id,
        granted_scopes=("commands", "chat:write"),
        installer_mim_user_id=UserId(installer_mim_user_id),
        installer_email=installer_email,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(hours=1),
        state=SlackSharedInstallState.ACTIVE,
    )


def identity_link(
    *,
    install_id: str = "ins-1",
    team_id: str = "T123",
    slack_user_id: str = "U123",
    mim_user_id: str = "usr-1",
    company_email: str = "person@madup.com",
) -> SlackIdentityLink:
    return SlackIdentityLink(
        install_id=install_id,
        team_id=team_id,
        slack_user_id=slack_user_id,
        mim_user_id=UserId(mim_user_id),
        company_email=company_email,
        verified_at=NOW - timedelta(hours=2),
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(hours=1),
        state=SlackIdentityLinkState.ACTIVE,
    )


class MalformedResolutionResolver:
    def resolve_identity(
        self,
        *,
        team_id: str,
        enterprise_id: str | None,
        slack_user_id: str,
    ) -> SlackIdentityResolution:
        del team_id, enterprise_id, slack_user_id
        return cast(SlackIdentityResolution, object())


class SlackIngressGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.replays = FakeSlackReplayRegistry()
        self.resolver = FakeSlackIdentityResolver(
            resolutions={
                ("T123", None, "U123"): SlackIdentityResolution(
                    install_id="ins-1",
                    company_email="person@madup.com",
                ),
                ("T123", "E123", "U123"): SlackIdentityResolution(
                    install_id="ins-enterprise",
                    company_email="enterprise@madup.com",
                ),
            }
        )
        self.gateway = SlackIngressGateway(
            allowed_commands=ALLOWED_COMMANDS,
            signing_secret=SIGNING_SECRET,
            replay_registry=self.replays,
            identity_resolver=self.resolver,
            clock=lambda: NOW,
        )

    def ingress(
        self,
        body: bytes,
        *,
        headers: tuple[tuple[str, str], ...] | None = None,
    ) -> VerifiedSlackIngress:
        return self.gateway.ingress(
            SlackIngressRequest(
                headers=headers if headers is not None else signed_headers(body),
                body=body,
            )
        )

    def test_known_valid_signature_derives_exact_actor_command_and_text(self) -> None:
        body = slash_body(text="deploy weekly")

        result = self.ingress(body)

        self.assertEqual(result.actor.install_id, "ins-1")
        self.assertEqual(result.actor.team_id, "T123")
        self.assertIsNone(result.actor.enterprise_id)
        self.assertEqual(result.actor.slack_user_id, "U123")
        self.assertEqual(result.actor.company_email, "person@madup.com")
        self.assertEqual(result.actor.verified_at, NOW)
        self.assertEqual(result.command, "/mim")
        self.assertEqual(result.text, "deploy weekly")
        self.assertEqual(len(self.replays.claims), 1)

    def test_docs_shaped_signed_payload_ignores_standard_slack_fields(self) -> None:
        body = slash_body(text="deploy weekly", extras=SLACK_DOCS_EXTRAS)

        result = self.ingress(body)

        self.assertEqual(result.actor.install_id, "ins-1")
        self.assertEqual(result.command, "/mim")
        self.assertEqual(result.text, "deploy weekly")
        self.assertEqual(self.replays.claims[-1].expires_at, NOW + timedelta(minutes=5))
        sensitive_values = {
            "legacy-verification-token",
            "https://hooks.slack.com/commands/1",
            "1337.42",
            "A111",
        }
        for value in sensitive_values:
            with self.subTest(value=value):
                self.assertNotIn(value, repr(result))
                self.assertNotIn(value, repr(self.replays.claims[-1]))
        for field_name in ("token", "response_url", "trigger_id", "api_app_id"):
            with self.subTest(field_name=field_name):
                self.assertNotIn(field_name, repr(result))
                self.assertNotIn(field_name, repr(self.replays.claims[-1]))

    def test_header_signature_freshness_and_clock_rules_are_fail_closed(self) -> None:
        body = slash_body()
        valid_headers = signed_headers(body)
        duplicate_signature_headers = valid_headers + (
            ("x-slack-signature", valid_headers[0][1]),
        )
        duplicate_timestamp_headers = valid_headers + (
            ("x-slack-request-timestamp", str(int(NOW.timestamp()))),
        )
        cases = (
            (
                "tampered-body",
                signed_headers(b"team_id=T999"),
                body,
                SlackIngressDenied,
            ),
            (
                "wrong-version",
                (
                    ("X-Slack-Signature", "v1=" + "0" * 64),
                    ("X-Slack-Request-Timestamp", str(int(NOW.timestamp()))),
                ),
                body,
                SlackIngressDenied,
            ),
            (
                "uppercase-version",
                (
                    ("X-Slack-Signature", "V0=" + "0" * 64),
                    ("X-Slack-Request-Timestamp", str(int(NOW.timestamp()))),
                ),
                body,
                SlackIngressDenied,
            ),
            (
                "duplicate-signature",
                duplicate_signature_headers,
                body,
                SlackIngressDenied,
            ),
            (
                "duplicate-timestamp",
                duplicate_timestamp_headers,
                body,
                SlackIngressDenied,
            ),
            (
                "missing-signature",
                (("X-Slack-Request-Timestamp", str(int(NOW.timestamp()))),),
                body,
                SlackIngressDenied,
            ),
            (
                "missing-timestamp",
                (("X-Slack-Signature", valid_headers[0][1]),),
                body,
                SlackIngressDenied,
            ),
            (
                "malformed-timestamp",
                (
                    ("X-Slack-Signature", valid_headers[0][1]),
                    ("X-Slack-Request-Timestamp", "10.5"),
                ),
                body,
                SlackIngressDenied,
            ),
            (
                "wrong-content-type",
                signed_headers(body, content_type="application/json"),
                body,
                SlackIngressDenied,
            ),
            (
                "duplicate-content-type",
                signed_headers(body)
                + (("content-type", "application/x-www-form-urlencoded"),),
                body,
                SlackIngressDenied,
            ),
            (
                "missing-content-type",
                valid_headers[:2],
                body,
                SlackIngressDenied,
            ),
        )

        accepted_past = self.ingress(
            body,
            headers=signed_headers(body, timestamp=NOW - timedelta(minutes=5)),
        )
        self.assertEqual(accepted_past.actor.verified_at, NOW)
        accepted_future = self.ingress(
            slash_body(text="future-ok"),
            headers=signed_headers(
                slash_body(text="future-ok"),
                timestamp=NOW + timedelta(minutes=5),
            ),
        )
        self.assertEqual(accepted_future.text, "future-ok")

        for name, headers, request_body, error_type in cases:
            with self.subTest(case=name):
                with self.assertRaises(error_type):
                    self.ingress(request_body, headers=headers)

        with self.assertRaises(SlackIngressDenied):
            self.ingress(
                slash_body(text="stale"),
                headers=signed_headers(
                    slash_body(text="stale"),
                    timestamp=NOW - timedelta(minutes=5, seconds=1),
                ),
            )
        with self.assertRaises(SlackIngressDenied):
            self.ingress(
                slash_body(text="future"),
                headers=signed_headers(
                    slash_body(text="future"),
                    timestamp=NOW + timedelta(minutes=5, seconds=1),
                ),
            )
        naive_clock_gateway = SlackIngressGateway(
            allowed_commands=ALLOWED_COMMANDS,
            signing_secret=SIGNING_SECRET,
            replay_registry=FakeSlackReplayRegistry(),
            identity_resolver=self.resolver,
            clock=lambda: datetime(2026, 8, 3, 10, 0, 0),
        )
        with self.assertRaises(SlackIngressDenied):
            naive_clock_gateway.ingress(
                SlackIngressRequest(headers=signed_headers(body), body=body)
            )

    def test_ignored_slack_fields_do_not_change_result_but_remain_distinct_requests(
        self,
    ) -> None:
        baseline_registry = FakeSlackReplayRegistry()
        baseline_gateway = SlackIngressGateway(
            allowed_commands=ALLOWED_COMMANDS,
            signing_secret=SIGNING_SECRET,
            replay_registry=baseline_registry,
            identity_resolver=self.resolver,
            clock=lambda: NOW,
        )
        mutated_registry = FakeSlackReplayRegistry()
        mutated_gateway = SlackIngressGateway(
            allowed_commands=ALLOWED_COMMANDS,
            signing_secret=SIGNING_SECRET,
            replay_registry=mutated_registry,
            identity_resolver=self.resolver,
            clock=lambda: NOW,
        )
        baseline_body = slash_body(
            text="deploy weekly",
            extras=SLACK_DOCS_EXTRAS,
        )
        mutated_body = slash_body(
            text="deploy weekly",
            extras=(
                ("token", "another-legacy-token"),
                ("team_domain", "other-team"),
                ("channel_id", "C999"),
                ("response_url", "https://hooks.slack.com/commands/2"),
                ("trigger_id", "9999.77"),
                ("api_app_id", "A222"),
            ),
        )

        baseline = baseline_gateway.ingress(
            SlackIngressRequest(
                headers=signed_headers(baseline_body),
                body=baseline_body,
            )
        )
        mutated = mutated_gateway.ingress(
            SlackIngressRequest(
                headers=signed_headers(mutated_body),
                body=mutated_body,
            )
        )

        self.assertEqual(baseline.actor, mutated.actor)
        self.assertEqual(baseline.command, mutated.command)
        self.assertEqual(baseline.text, mutated.text)
        self.assertNotEqual(
            baseline_registry.claims[-1].fingerprint,
            mutated_registry.claims[-1].fingerprint,
        )

    def test_exact_now_minus_five_minutes_claim_stays_active_and_duplicate_is_blocked(
        self,
    ) -> None:
        body = slash_body(text="boundary")

        self.ingress(
            body,
            headers=signed_headers(body, timestamp=NOW - timedelta(minutes=5)),
        )

        claim = self.replays.claims[-1]
        self.assertGreater(claim.expires_at, claim.claimed_at)
        self.assertEqual(claim.expires_at, NOW + timedelta(minutes=5))
        with self.assertRaises(SlackIngressDenied):
            self.ingress(
                body,
                headers=signed_headers(body, timestamp=NOW - timedelta(minutes=5)),
            )

    def test_exact_replay_is_denied_but_distinct_valid_request_is_allowed(self) -> None:
        body = slash_body(text="same")

        first = self.ingress(body)
        self.assertEqual(first.text, "same")
        with self.assertRaises(SlackIngressDenied):
            self.ingress(body)

        second_body = slash_body(text="different")
        second = self.ingress(
            second_body,
            headers=signed_headers(second_body, timestamp=NOW + timedelta(seconds=1)),
        )
        self.assertEqual(second.text, "different")

    def test_invalid_utf8_size_form_shape_credentials_and_command_are_denied(
        self,
    ) -> None:
        cases = (
            ("invalid-utf8", b"\xff\xfe\xfd", signed_headers(b"\xff\xfe\xfd")),
            (
                "too-large",
                b"a" * (64 * 1024 + 1),
                signed_headers(b"a" * (64 * 1024 + 1)),
            ),
            (
                "duplicate-team",
                encode_fields(
                    (
                        ("team_id", "T123"),
                        ("team_id", "T999"),
                        ("user_id", "U123"),
                        ("command", "/mim"),
                        ("text", "x"),
                    )
                ),
                None,
            ),
            (
                "missing-text",
                encode_fields(
                    (
                        ("team_id", "T123"),
                        ("user_id", "U123"),
                        ("command", "/mim"),
                    )
                ),
                None,
            ),
            ("bad-percent", b"team_id=T123&user_id=U123&command=%ZZ&text=x", None),
            (
                "percent-invalid-utf8",
                b"team_id=T123&user_id=U123&command=/mim&text=%FF",
                None,
            ),
            (
                "unexpected-extra",
                slash_body(extras=(("project_id", "proj-123"),)),
                None,
            ),
            (
                "credential-like",
                slash_body(extras=(("access_token", "secret-token"),)),
                None,
            ),
            (
                "duplicate-token",
                slash_body(
                    extras=(
                        ("token", "legacy-1"),
                        ("token", "legacy-2"),
                    )
                ),
                None,
            ),
            (
                "duplicate-trigger-id-casefolded",
                slash_body(
                    extras=(
                        ("trigger_id", "1337.1"),
                        ("TRIGGER_ID", "1337.2"),
                    )
                ),
                None,
            ),
            (
                "duplicate-response-url-casefolded",
                slash_body(
                    extras=(
                        ("response_url", "https://hooks.slack.com/commands/1"),
                        ("Response_Url", "https://hooks.slack.com/commands/2"),
                    )
                ),
                None,
            ),
            (
                "too-many-fields",
                slash_body(extras=tuple((f"x{i}", "1") for i in range(20))),
                None,
            ),
            ("unapproved-command", slash_body(command="/other"), None),
        )

        for name, body, headers in cases:
            with self.subTest(case=name):
                with self.assertRaises(SlackIngressDenied):
                    self.ingress(body, headers=headers or signed_headers(body))

    def test_resolution_failures_are_denied_and_downstream_exact_boundary_rechecks(
        self,
    ) -> None:
        body = slash_body()
        missing_gateway = SlackIngressGateway(
            allowed_commands=ALLOWED_COMMANDS,
            signing_secret=SIGNING_SECRET,
            replay_registry=FakeSlackReplayRegistry(),
            identity_resolver=FakeSlackIdentityResolver(resolutions={}),
            clock=lambda: NOW,
        )
        with self.assertRaises(SlackIngressDenied):
            missing_gateway.ingress(
                SlackIngressRequest(headers=signed_headers(body), body=body)
            )

        malformed_gateway = SlackIngressGateway(
            allowed_commands=ALLOWED_COMMANDS,
            signing_secret=SIGNING_SECRET,
            replay_registry=FakeSlackReplayRegistry(),
            identity_resolver=cast(
                SlackIdentityResolver,
                MalformedResolutionResolver(),
            ),
            clock=lambda: NOW,
        )
        with self.assertRaises(SlackIngressDenied):
            malformed_gateway.ingress(
                SlackIngressRequest(headers=signed_headers(body), body=body)
            )

        replay_registry = FakeSlackReplayRegistry()
        downstream_gateway = SlackIngressGateway(
            allowed_commands=ALLOWED_COMMANDS,
            signing_secret=SIGNING_SECRET,
            replay_registry=replay_registry,
            identity_resolver=FakeSlackIdentityResolver(
                resolutions={
                    ("T123", None, "U123"): SlackIdentityResolution(
                        install_id="ins-malicious",
                        company_email="person@madup.com",
                    )
                }
            ),
            clock=lambda: NOW,
        )
        verified = downstream_gateway.ingress(
            SlackIngressRequest(headers=signed_headers(body), body=body)
        )

        identity_store = FakeIdentityRegistry(
            installs=(shared_install(install_id="ins-1"),),
            links=(identity_link(install_id="ins-1"),),
        )
        users = {
            "usr-1": user(),
            "admin-1": user(
                user_id="admin-1",
                email="admin@madup.com",
                role=UserRole.ADMIN,
            ),
        }
        central_identity = CentralIdentityGateway(
            browser_authenticator=None,  # type: ignore[arg-type]
            identity_policy=IdentityPolicy(
                store=type(
                    "UserStore",
                    (),
                    {"get_user": lambda self, user_id: users[str(user_id)]},
                )(),
                issuer="https://unused.example.com",
                audience="unused",
                company_domain="madup.com",
                required_group=GROUP,
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
            shared_install_directory=identity_store,
            identity_link_directory=identity_store,
            action_authorizer=FakeActionPolicyAuthorizer(),
            required_slack_scopes=frozenset({"commands", "chat:write"}),
            clock=lambda: NOW,
        )
        with self.assertRaises(CentralIdentityDenied):
            central_identity.authorize_slack(
                SlackActionRequest(
                    actor=verified.actor,
                    intent=ActionIntent(
                        action=ActionName.VIEW_DASHBOARD,
                        resource_id="dashboard",
                    ),
                )
            )

    def test_reprs_errors_and_replay_records_do_not_echo_sensitive_values(self) -> None:
        body = slash_body(
            text="top secret deploy",
            extras=(("access_token", "live-secret-token"),),
        )

        with self.assertRaises(SlackIngressDenied) as raised:
            self.ingress(body)

        message = str(raised.exception)
        self.assertNotIn("top secret deploy", message)
        self.assertNotIn("live-secret-token", message)
        self.assertNotIn(SIGNING_SECRET.decode("ascii"), message)

        clean_body = slash_body(
            text="private text",
            extras=SLACK_DOCS_EXTRAS,
        )
        result = self.ingress(
            clean_body,
            headers=signed_headers(clean_body, timestamp=NOW + timedelta(seconds=2)),
        )
        self.assertNotIn("private text", repr(result))
        self.assertNotIn("response_url", repr(result))
        self.assertNotIn("https://hooks.slack.com/commands/1", repr(result))
        self.assertNotIn("private text", repr(self.replays.claims[0]))
        self.assertNotIn("private text", repr(self.replays.claims[-1]))

    def test_public_contract_excludes_secret_and_cloud_knobs(self) -> None:
        with self.assertRaises(ValueError):
            SlackIngressGateway(
                allowed_commands=ALLOWED_COMMANDS,
                signing_secret=b"short",
                replay_registry=FakeSlackReplayRegistry(),
                identity_resolver=self.resolver,
                clock=lambda: NOW,
            )

        forbidden = {
            "signing_secret",
            "api_key",
            "token",
            "project_id",
            "billing_account_id",
            "quota_limit",
            "cloud",
        }
        public_types = (
            SlackIngressRequest,
            VerifiedSlackIngress,
            SlackIdentityResolution,
        )
        for public_type in public_types:
            with self.subTest(public_type=public_type.__name__):
                self.assertTrue(
                    forbidden.isdisjoint({field.name for field in fields(public_type)})
                )
