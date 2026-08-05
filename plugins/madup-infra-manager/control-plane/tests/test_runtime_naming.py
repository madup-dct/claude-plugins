from __future__ import annotations

import unittest

from mim_control_plane.services.runtime_naming import provider_secret_id


class ProviderSecretIdTests(unittest.TestCase):
    def test_provider_secret_id_prefixes_exact_stable_secret_id(self) -> None:
        self.assertEqual(
            provider_secret_id("sec-0123456789abcdefabcd"),
            "mim-sec-0123456789abcdefabcd",
        )

    def test_provider_secret_id_rejects_malformed_secret_ids(self) -> None:
        for secret_id in (
            "",
            "sec-1",
            "sec-0123456789ABCDEFABCD",
            "sec_0123456789abcdefabcd",
            "mim-sec-0123456789abcdefabcd",
            "sec-0123456789abcdefabcg",
        ):
            with self.subTest(secret_id=secret_id):
                with self.assertRaises(ValueError):
                    provider_secret_id(secret_id)


if __name__ == "__main__":
    unittest.main()
