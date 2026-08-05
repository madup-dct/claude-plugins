from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from mim_control_plane.adapters.memory_store import MemoryStore
from mim_control_plane.domain.models import User, UserId
from mim_control_plane.domain.states import UserRole, UserState
from mim_control_plane.security.authorization import (
    AccessDenied,
    IdentityPolicy,
    require_admin,
    require_owner_or_admin,
)
from mim_control_plane.security.identity import (
    CloudflareJwtVerifier,
    IdentityClaims,
    TokenDenied,
)

NOW = datetime(2026, 8, 2, 1, 2, 3, tzinfo=UTC)
ISSUER = "https://tenant.cloudflareaccess.com"
AUDIENCE = "audience-1"


def user(
    *,
    user_id: str = "usr-1",
    email: str = "person@madup.com",
    role: UserRole = UserRole.USER,
    state: UserState = UserState.ACTIVE,
    groups: frozenset[str] = frozenset({"mim-users"}),
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
    issuer: str = ISSUER,
    audience: tuple[str, ...] = (AUDIENCE,),
    issued_at: datetime = NOW - timedelta(minutes=1),
    expires_at: datetime = NOW + timedelta(minutes=10),
) -> IdentityClaims:
    return IdentityClaims(
        subject=subject,
        email=email,
        issuer=issuer,
        audience=audience,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def policy_for(record: User) -> IdentityPolicy:
    store = MemoryStore()
    store.create_user(record)
    return IdentityPolicy(
        store=store,
        issuer=ISSUER,
        audience=AUDIENCE,
        company_domain="madup.com",
        required_group="mim-users",
        max_staleness=timedelta(minutes=60),
        clock=lambda: NOW,
    )


class AuthorizationPolicyTests(unittest.TestCase):
    def test_resolved_user_authorization_reuses_local_user_binding_checks(self) -> None:
        policy = policy_for(user())

        principal = policy.authorize_resolved_user(
            user_id=UserId("usr-1"),
            email=" Person@Madup.com ",
        )

        self.assertEqual(principal.user_id, UserId("usr-1"))
        self.assertEqual(principal.email, "person@madup.com")
        with self.assertRaises(AccessDenied):
            policy.authorize_resolved_user(
                user_id=UserId("usr-1"),
                email="other@madup.com",
            )
        with self.assertRaises(AccessDenied):
            policy.authorize_resolved_user(
                user_id=UserId("missing-user"),
                email="person@madup.com",
            )

    def test_exact_issuer_and_audience_are_required(self) -> None:
        policy = policy_for(user())
        with self.assertRaises(AccessDenied):
            policy.authorize(claims(issuer="https://other.cloudflareaccess.com"))
        with self.assertRaises(AccessDenied):
            policy.authorize(claims(audience=("other-audience",)))

    def test_non_madup_email_group_suspension_and_staleness_are_denied(
        self,
    ) -> None:
        cases = (
            (user(), claims(email="person@example.com")),
            (user(groups=frozenset()), claims()),
            (user(state=UserState.SUSPENDED), claims()),
            (user(synced_at=NOW - timedelta(minutes=61)), claims()),
        )
        for record, asserted_claims in cases:
            with self.subTest(record=record, claims=asserted_claims):
                with self.assertRaises(AccessDenied):
                    policy_for(record).authorize(asserted_claims)

    def test_user_record_must_match_asserted_subject_and_email(self) -> None:
        policy = policy_for(user())
        with self.assertRaises(AccessDenied):
            policy.authorize(claims(subject="missing-user"))
        with self.assertRaises(AccessDenied):
            policy.authorize(claims(email="other@madup.com"))

    def test_future_naive_and_inverted_claim_times_fail_closed(self) -> None:
        policy = policy_for(user())
        invalid_claims = (
            claims(issued_at=NOW + timedelta(seconds=1)),
            claims(expires_at=NOW - timedelta(seconds=1)),
            claims(
                issued_at=datetime(2026, 8, 2, 1, 1, 0),
                expires_at=NOW + timedelta(minutes=10),
            ),
            claims(
                issued_at=NOW - timedelta(minutes=1),
                expires_at=datetime(2026, 8, 2, 1, 12, 3),
            ),
        )
        for candidate in invalid_claims:
            with self.subTest(candidate=candidate):
                with self.assertRaises(AccessDenied):
                    policy.authorize(candidate)

    def test_owner_and_admin_checks_do_not_expand_cloud_authority(self) -> None:
        user_principal = policy_for(user()).authorize(claims())
        require_owner_or_admin(user_principal, UserId("usr-1"))
        with self.assertRaises(AccessDenied):
            require_owner_or_admin(user_principal, UserId("usr-2"))
        with self.assertRaises(AccessDenied):
            require_admin(user_principal)

        admin_record = user(user_id="admin-1", role=UserRole.ADMIN)
        admin_claims = claims(subject="admin-1")
        admin = policy_for(admin_record).authorize(admin_claims)
        require_admin(admin)
        require_owner_or_admin(admin, UserId("usr-2"))

    def test_cloudflare_verifier_uses_cached_jwks_and_exact_claim_options(self) -> None:
        jwks_client = Mock()
        jwks_client.get_signing_key_from_jwt.return_value = Mock(key="public-key")
        verifier = CloudflareJwtVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_client=jwks_client,
        )
        decoded = {
            "sub": "usr-1",
            "email": "person@madup.com",
            "iss": ISSUER,
            "aud": [AUDIENCE],
            "iat": int((NOW - timedelta(minutes=1)).timestamp()),
            "exp": int((NOW + timedelta(minutes=10)).timestamp()),
        }

        with patch(
            "mim_control_plane.security.identity.jwt.decode",
            return_value=decoded,
        ) as decode:
            verified = verifier.verify("opaque-access-assertion")

        self.assertEqual(verified.subject, "usr-1")
        jwks_client.get_signing_key_from_jwt.assert_called_once_with(
            "opaque-access-assertion"
        )
        self.assertEqual(decode.call_args.kwargs["issuer"], ISSUER)
        self.assertEqual(decode.call_args.kwargs["audience"], AUDIENCE)
        self.assertEqual(decode.call_args.kwargs["algorithms"], ["RS256"])

    def test_jwt_errors_fail_closed_without_echoing_the_token(self) -> None:
        jwks_client = Mock()
        jwks_client.get_signing_key_from_jwt.side_effect = ValueError("bad key")
        verifier = CloudflareJwtVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_client=jwks_client,
        )
        token = "secret-token-that-must-not-appear"

        with self.assertRaises(TokenDenied) as raised:
            verifier.verify(token)

        self.assertNotIn(token, str(raised.exception))

    def test_out_of_range_numeric_dates_fail_closed_without_echoing_token(self) -> None:
        jwks_client = Mock()
        jwks_client.get_signing_key_from_jwt.return_value = Mock(key="public-key")
        verifier = CloudflareJwtVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_client=jwks_client,
        )
        token = "overflow-token-that-must-not-appear"
        decoded = {
            "sub": "usr-1",
            "email": "person@madup.com",
            "iss": ISSUER,
            "aud": [AUDIENCE],
            "iat": 10**20,
            "exp": 10**20,
        }

        with patch(
            "mim_control_plane.security.identity.jwt.decode",
            return_value=decoded,
        ):
            with self.assertRaises(TokenDenied) as raised:
                verifier.verify(token)

        self.assertNotIn(token, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
