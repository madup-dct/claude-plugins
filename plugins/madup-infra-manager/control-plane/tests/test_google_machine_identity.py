from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mim_control_plane.security.google_machine_identity import (  # noqa: E402
    GoogleOidcMachineAuthenticator,
    MachineRequestDenied,
)

AUDIENCE = "https://mim-deploy-worker-123456789012.asia-northeast3.run.app"
SERVICE_ACCOUNT_EMAIL = (
    "mim-deploy-worker@mim-prod-123456.iam.gserviceaccount.com"
)


class FakeVerifier:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, object, str]] = []

    def __call__(self, token: str, request: object, audience: str) -> dict[str, object]:
        self.calls.append((token, request, audience))
        return dict(self.payload)


def claims(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "sub": "1133557799",
        "aud": AUDIENCE,
        "email": SERVICE_ACCOUNT_EMAIL,
        "email_verified": True,
        "iat": 1_754_272_800,
        "exp": 1_754_273_160,
    }
    payload.update(overrides)
    return payload


class GoogleMachineIdentityTests(unittest.TestCase):
    def test_accepts_exact_single_bearer_for_dedicated_service_account(self) -> None:
        verifier = FakeVerifier(claims())
        authenticator = GoogleOidcMachineAuthenticator(
            audience=AUDIENCE,
            service_account_email=SERVICE_ACCOUNT_EMAIL,
            token_verifier=verifier,
            transport_request=object(),
        )

        principal = authenticator.authenticate(
            (
                ("Authorization", "Bearer trusted-token"),
                ("X-CloudTasks-QueueName", "mim-private-workers"),
            )
        )

        self.assertEqual(principal.email, SERVICE_ACCOUNT_EMAIL)
        self.assertEqual(principal.subject, "1133557799")
        self.assertEqual(principal.audience, AUDIENCE)
        self.assertEqual(
            principal.issued_at,
            datetime.fromtimestamp(1_754_272_800, tz=UTC),
        )
        self.assertEqual(len(verifier.calls), 1)
        self.assertEqual(verifier.calls[0][0], "trusted-token")
        self.assertIs(verifier.calls[0][1], authenticator.transport_request)
        self.assertEqual(verifier.calls[0][2], AUDIENCE)

    def test_rejects_missing_duplicate_or_non_bearer_authorization(self) -> None:
        authenticator = GoogleOidcMachineAuthenticator(
            audience=AUDIENCE,
            service_account_email=SERVICE_ACCOUNT_EMAIL,
            token_verifier=FakeVerifier(claims()),
            transport_request=object(),
        )
        cases = (
            (),
            (
                ("Authorization", "Bearer one"),
                ("authorization", "Bearer two"),
            ),
            (("Authorization", "Basic abc"),),
            (("Authorization", "bearer abc"),),
            (("Authorization", "Bearer "),),
        )

        for headers in cases:
            with self.subTest(headers=headers):
                with self.assertRaises(MachineRequestDenied):
                    authenticator.authenticate(headers)

    def test_rejects_cookie_cloudflare_origin_and_browser_headers(self) -> None:
        authenticator = GoogleOidcMachineAuthenticator(
            audience=AUDIENCE,
            service_account_email=SERVICE_ACCOUNT_EMAIL,
            token_verifier=FakeVerifier(claims()),
            transport_request=object(),
        )
        bad_headers = (
            ("Cookie", "session=1"),
            ("Cf-Access-Jwt-Assertion", "edge-token"),
            ("X-MIM-Origin-Signature", "abc"),
            ("Sec-Fetch-Site", "same-origin"),
            ("Origin", "https://mim.madup.app"),
        )

        for name, value in bad_headers:
            with self.subTest(name=name):
                with self.assertRaises(MachineRequestDenied):
                    authenticator.authenticate(
                        (
                            ("Authorization", "Bearer trusted-token"),
                            (name, value),
                        )
                    )

    def test_rejects_claim_drift_for_audience_email_and_email_verified(self) -> None:
        cases = (
            claims(aud="https://other.run.app"),
            claims(aud=(AUDIENCE, "https://other.run.app")),
            claims(email="other@mim-prod-123456.iam.gserviceaccount.com"),
            claims(email_verified=False),
            claims(sub=""),
        )

        for payload in cases:
            with self.subTest(payload=payload):
                authenticator = GoogleOidcMachineAuthenticator(
                    audience=AUDIENCE,
                    service_account_email=SERVICE_ACCOUNT_EMAIL,
                    token_verifier=FakeVerifier(payload),
                    transport_request=object(),
                )
                with self.assertRaises(MachineRequestDenied):
                    authenticator.authenticate((("Authorization", "Bearer ok"),))

    def test_rejects_invalid_numeric_dates(self) -> None:
        for value in (True, "1", None):
            with self.subTest(value=value):
                authenticator = GoogleOidcMachineAuthenticator(
                    audience=AUDIENCE,
                    service_account_email=SERVICE_ACCOUNT_EMAIL,
                    token_verifier=FakeVerifier(claims(iat=value)),
                    transport_request=object(),
                )
                with self.assertRaises(MachineRequestDenied):
                    authenticator.authenticate((("Authorization", "Bearer ok"),))
