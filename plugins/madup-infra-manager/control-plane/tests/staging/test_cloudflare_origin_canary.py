from __future__ import annotations

import unittest

from tests.staging import (
    BASE_URL,
    CONTROL_PLANE_RUN_APP_PATTERN,
    Probe,
    assert_no_secret_echo,
    require_env,
    require_private_file_env,
)


def browser_and_origin_contract() -> dict[str, object]:
    return {
        "base_url": BASE_URL,
        "cookie_env": "MIM_STAGING_CF_AUTHORIZATION_FILE",
        "direct_origin_env": "MIM_STAGING_CONTROL_PLANE_RUN_APP_URL",
        "anonymous_statuses": (302, 303, 307, 308, 401, 403),
        "authenticated_status": 200,
        "direct_origin_denied_statuses": (401, 403),
        "direct_origin_denied_paths": ("/healthz", "/readyz"),
        "browser_surface": "cloudflare-access-browser",
        "origin_surface": "cloudflare-run-app-without-worker-hmac",
    }


def readonly_live_probes() -> tuple[Probe, ...]:
    return (
        Probe(
            name="browser-anonymous-healthz",
            method="GET",
            url=f"{BASE_URL}/healthz",
            expected_status=302,
            surface="cloudflare-access-browser",
        ),
        Probe(
            name="browser-authenticated-healthz",
            method="GET",
            url=f"{BASE_URL}/healthz",
            expected_status=200,
            surface="cloudflare-access-browser",
        ),
        Probe(
            name="browser-authenticated-readyz",
            method="GET",
            url=f"{BASE_URL}/readyz",
            expected_status=200,
            surface="cloudflare-access-browser",
        ),
        Probe(
            name="origin-without-worker-hmac",
            method="GET",
            url="https://mim-control-plane-123456789012.asia-northeast3.run.app/healthz",
            expected_status=403,
            surface="cloudflare-run-app-without-worker-hmac",
        ),
        Probe(
            name="origin-readyz-without-worker-hmac",
            method="GET",
            url="https://mim-control-plane-123456789012.asia-northeast3.run.app/readyz",
            expected_status=403,
            surface="cloudflare-run-app-without-worker-hmac",
        ),
    )


class CloudflareOriginCanaryTests(unittest.TestCase):
    def test_required_mode_demands_private_cookie_and_run_app_url(
        self,
    ) -> None:
        self.assertEqual(require_env("MIM_STAGING_BASE_URL", exact=BASE_URL), BASE_URL)
        cookie_file = require_private_file_env("MIM_STAGING_CF_AUTHORIZATION_FILE")
        direct_origin = require_env(
            "MIM_STAGING_CONTROL_PLANE_RUN_APP_URL",
            pattern=CONTROL_PLANE_RUN_APP_PATTERN,
        )

        rendered = cookie_file.read_text(encoding="utf-8").strip()
        self.assertTrue(rendered.startswith("CF_Authorization="))
        self.assertRegex(direct_origin, CONTROL_PLANE_RUN_APP_PATTERN)
        assert_no_secret_echo(self, "redacted", rendered.partition("=")[2])

    def test_contract_distinguishes_browser_access_from_direct_origin_denial(
        self,
    ) -> None:
        contract = browser_and_origin_contract()
        probes = readonly_live_probes()

        self.assertEqual(contract["base_url"], BASE_URL)
        self.assertEqual(contract["cookie_env"], "MIM_STAGING_CF_AUTHORIZATION_FILE")
        self.assertEqual(
            contract["direct_origin_env"],
            "MIM_STAGING_CONTROL_PLANE_RUN_APP_URL",
        )
        self.assertEqual(probes[0].surface, contract["browser_surface"])
        self.assertEqual(probes[-1].surface, contract["origin_surface"])
        self.assertEqual(
            tuple(
                probe.url.rsplit(".run.app", maxsplit=1)[-1]
                for probe in probes
                if probe.surface == contract["origin_surface"]
            ),
            contract["direct_origin_denied_paths"],
        )
        self.assertIn(403, contract["direct_origin_denied_statuses"])
        self.assertEqual(
            contract["direct_origin_denied_paths"],
            ("/healthz", "/readyz"),
        )

    def test_contract_explicitly_denies_direct_origin_readyz_with_live_probe(
        self,
    ) -> None:
        contract = browser_and_origin_contract()
        probes = readonly_live_probes()

        self.assertEqual(len(probes), 5)
        self.assertEqual(
            contract["direct_origin_denied_paths"],
            ("/healthz", "/readyz"),
        )
        self.assertTrue(
            all(path.startswith("/") for path in contract["direct_origin_denied_paths"])
        )
        self.assertIn(
            "/readyz",
            tuple(
                probe.url.rsplit(".run.app", maxsplit=1)[-1]
                for probe in probes
                if probe.surface == contract["origin_surface"]
            ),
        )

    def test_contract_never_describes_client_injected_cf_access_jwt_headers(
        self,
    ) -> None:
        contract_text = " ".join(
            str(value) for value in browser_and_origin_contract().values()
        )
        probe_text = " ".join(
            f"{probe.method} {probe.url} {probe.surface}"
            for probe in readonly_live_probes()
        )

        self.assertNotIn("Cf-Access-Jwt-Assertion", contract_text)
        self.assertNotIn("Cf-Access-Jwt-Assertion", probe_text)
        self.assertIn("run.app/readyz", probe_text)


if __name__ == "__main__":
    unittest.main()
