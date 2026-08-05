from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import OriginRequestId, User, UserId
from mim_control_plane.domain.states import UserRole, UserState
from mim_control_plane.security.authorization import IdentityPolicy
from mim_control_plane.security.identity import (
    AuthenticationRequest,
    IdentityAuthenticator,
    IdentityClaims,
    TokenDenied,
)
from mim_control_plane.security.origin import (
    OriginDenied,
    OriginHmacVerifier,
    OriginRequest,
    canonical_request_target,
    sign_origin_request,
)

NOW = datetime(2026, 8, 2, 1, 2, 3, tzinfo=UTC)
OLD_KEY = b"o" * 32
NEW_KEY = b"n" * 32


class FakeJwtVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self.claims = claims
        self.calls = 0
        self.last_token: str | None = None

    def verify(self, token: str) -> IdentityClaims:
        self.calls += 1
        self.last_token = token
        return self.claims


def claims(*, subject: str = "usr-1") -> IdentityClaims:
    return IdentityClaims(
        subject=subject,
        email="person@madup.com",
        issuer="https://tenant.cloudflareaccess.com",
        audience=("audience-1",),
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def signed_origin(
    *,
    request_id: str,
    key_id: str = "new",
    key: bytes = NEW_KEY,
    body: bytes = b'{"action":"status"}',
    timestamp: datetime = NOW,
    public_host: str = "mim.madup.app",
    destination_class: str = "control-plane",
) -> OriginRequest:
    unsigned = OriginRequest(
        method="POST",
        path="/mcp",
        body=body,
        timestamp=timestamp,
        request_id=OriginRequestId(request_id),
        public_host=public_host,
        destination_class=destination_class,
        key_id=key_id,
        signature=None,
    )
    return dataclasses.replace(
        unsigned,
        signature=sign_origin_request(unsigned, key=key),
    )


class OriginHmacTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.store.create_user(
            User(
                id=UserId("usr-1"),
                email="person@madup.com",
                role=UserRole.USER,
                state=UserState.ACTIVE,
                groups=frozenset({"mim-users"}),
                identity_synced_at=NOW - timedelta(minutes=5),
                created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(minutes=5),
            )
        )
        self.jwt_verifier = FakeJwtVerifier(claims())
        self.authenticator = IdentityAuthenticator(
            origin_verifier=OriginHmacVerifier(
                keys={"old": OLD_KEY, "new": NEW_KEY},
                store=self.store,
                clock=lambda: NOW,
                window=timedelta(seconds=60),
            ),
            jwt_verifier=self.jwt_verifier,
            identity_policy=IdentityPolicy(
                store=self.store,
                issuer="https://tenant.cloudflareaccess.com",
                audience="audience-1",
                company_domain="madup.com",
                required_group="mim-users",
                max_staleness=timedelta(minutes=60),
                clock=lambda: NOW,
            ),
        )

    def authenticate(
        self,
        origin: OriginRequest,
        *,
        headers: tuple[tuple[str, str], ...] | None = None,
        token: str = "opaque-token",
    ):
        return self.authenticator.authenticate(
            AuthenticationRequest(
                origin=origin,
                headers=headers
                if headers is not None
                else (("Cf-Access-Jwt-Assertion", token),),
            )
        )

    def test_valid_user_token_cannot_bypass_worker(self) -> None:
        request = signed_origin(request_id="req-missing")
        request = dataclasses.replace(request, signature=None)

        with self.assertRaises(OriginDenied):
            self.authenticate(request)

        self.assertEqual(self.jwt_verifier.calls, 0)

    def test_body_timestamp_and_replay_are_denied_before_jwt_parsing(self) -> None:
        altered = dataclasses.replace(
            signed_origin(request_id="req-altered"),
            body=b'{"action":"deploy"}',
        )
        stale = signed_origin(
            request_id="req-stale",
            timestamp=NOW - timedelta(seconds=61),
        )

        with self.assertRaises(OriginDenied):
            self.authenticate(altered)
        with self.assertRaises(OriginDenied):
            self.authenticate(stale)
        self.assertEqual(self.jwt_verifier.calls, 0)

        replay = signed_origin(request_id="req-replay")
        principal = self.authenticate(replay)
        self.assertEqual(principal.user_id, UserId("usr-1"))
        self.assertEqual(self.jwt_verifier.calls, 1)
        with self.assertRaises(OriginDenied):
            self.authenticate(replay)
        self.assertEqual(self.jwt_verifier.calls, 1)

    def test_public_host_and_destination_class_are_bound_into_the_signature(
        self,
    ) -> None:
        tampered_host = dataclasses.replace(
            signed_origin(request_id="req-host"),
            public_host="other.madup.app",
        )
        tampered_destination = dataclasses.replace(
            signed_origin(request_id="req-dest"),
            destination_class="app-gateway",
        )

        with self.assertRaises(OriginDenied):
            self.authenticate(tampered_host)
        with self.assertRaises(OriginDenied):
            self.authenticate(tampered_destination)
        self.assertEqual(self.jwt_verifier.calls, 0)

    def test_access_assertion_must_come_from_the_access_header_only(self) -> None:
        request = signed_origin(request_id="req-header-valid")

        self.assertEqual(
            self.authenticate(
                request,
                headers=(("cf-access-jwt-assertion", "opaque-token"),),
            ).user_id,
            UserId("usr-1"),
        )

        denied_headers = (
            (),
            (("Cf-Access-Jwt-Assertion", ""),),
            (
                ("Cf-Access-Jwt-Assertion", "opaque-token"),
                ("cf-access-jwt-assertion", "opaque-token"),
            ),
            (("Cookie", "CF_Authorization=opaque-token"),),
            (("Authorization", "Bearer opaque-token"),),
            (
                ("Cf-Access-Jwt-Assertion", "opaque-token"),
                ("Cookie", "CF_Authorization=opaque-token"),
            ),
            (
                ("Cf-Access-Jwt-Assertion", "opaque-token"),
                ("Proxy-Authorization", "Bearer opaque-token"),
            ),
            (
                ("Cf-Access-Jwt-Assertion", "opaque-token"),
                ("set-cookie", "CF_Authorization=opaque-token"),
            ),
        )
        for index, headers in enumerate(denied_headers):
            with self.subTest(headers=headers):
                denied_request = signed_origin(
                    request_id=f"req-header-denied-{index}"
                )
                with self.assertRaises(TokenDenied):
                    self.authenticate(denied_request, headers=headers)
                self.assertEqual(self.jwt_verifier.calls, 1)

    def test_mixed_assertion_and_cookie_like_headers_never_echo_values(self) -> None:
        request = signed_origin(request_id="req-header-secret")
        headers = (
            ("Cf-Access-Jwt-Assertion", "opaque-token"),
            ("Authorization", "Bearer secret-auth-header"),
            ("Cookie", "CF_Authorization=secret-cookie"),
        )

        with self.assertRaises(TokenDenied) as raised:
            self.authenticate(request, headers=headers)

        self.assertEqual(self.jwt_verifier.calls, 0)
        message = str(raised.exception)
        self.assertNotIn("opaque-token", message)
        self.assertNotIn("secret-auth-header", message)
        self.assertNotIn("secret-cookie", message)

    def test_freshness_is_one_sided_and_claim_expiry_uses_signed_timestamp(
        self,
    ) -> None:
        verifier = OriginHmacVerifier(
            keys={"new": NEW_KEY},
            store=MemoryStore(),
            clock=lambda: NOW,
            window=timedelta(seconds=60),
        )

        accepted = signed_origin(
            request_id="req-boundary",
            timestamp=NOW - timedelta(seconds=60),
        )
        claim = verifier.verify(accepted)
        self.assertEqual(claim.expires_at, accepted.timestamp + timedelta(seconds=60))

        with self.assertRaises(OriginDenied):
            verifier.verify(
                signed_origin(
                    request_id="req-too-old",
                    timestamp=NOW - timedelta(seconds=61),
                )
            )
        with self.assertRaises(OriginDenied):
            verifier.verify(
                signed_origin(
                    request_id="req-future",
                    timestamp=NOW + timedelta(seconds=1),
                )
            )

    def test_request_target_contract_is_ascii_only_and_deterministic(self) -> None:
        self.assertEqual(canonical_request_target("/mcp"), b"/mcp")
        self.assertEqual(
            canonical_request_target("/mcp?view=user&limit=20"),
            b"/mcp?view=user&limit=20",
        )
        self.assertNotEqual(
            canonical_request_target("/mcp?a=1&b=2"),
            canonical_request_target("/mcp?b=2&a=1"),
        )

        rejected = (
            "https://example.com/mcp",
            "//example.com/mcp",
            "/mcp#fragment",
            "/mcp\\status",
            "/mcp?\nview=user",
            "/mcp?view=%7e",
            "/mcp?view=%2F",
            "/mcp?view=%zz",
            "/mcp?view=user#fragment",
        )
        for target in rejected:
            with self.subTest(target=target):
                with self.assertRaises(OriginDenied):
                    canonical_request_target(target)

    def test_overlapping_origin_keys_are_accepted_during_rotation(self) -> None:
        old = signed_origin(request_id="req-old", key_id="old", key=OLD_KEY)
        new = signed_origin(request_id="req-new", key_id="new", key=NEW_KEY)

        self.assertEqual(self.authenticate(old).user_id, UserId("usr-1"))
        self.assertEqual(self.authenticate(new).user_id, UserId("usr-1"))

    def test_signature_and_token_never_appear_in_failure_text(self) -> None:
        request = signed_origin(request_id="req-secret")
        bad_signature = "0" * 64
        request = dataclasses.replace(request, signature=bad_signature)

        with self.assertRaises(OriginDenied) as raised:
            self.authenticate(request, token="do-not-echo-this-token")

        message = str(raised.exception)
        self.assertNotIn(bad_signature, message)
        self.assertNotIn("do-not-echo-this-token", message)


if __name__ == "__main__":
    unittest.main()
