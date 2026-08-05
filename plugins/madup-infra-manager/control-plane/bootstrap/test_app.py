import json
import os
import sys
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BOOTSTRAP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BOOTSTRAP_DIR))

import app  # noqa: E402


class BootstrapAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_mim_hostname = os.environ.get("MIM_HOSTNAME")
        os.environ["MIM_HOSTNAME"] = "mim.madup.app"
        cls.server = app.build_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        if cls._original_mim_hostname is None:
            os.environ.pop("MIM_HOSTNAME", None)
        else:
            os.environ["MIM_HOSTNAME"] = cls._original_mim_hostname

    def _urlopen(self, path, host=None, headers=None):
        request_headers = dict(headers or {})
        if host is not None:
            request_headers["Host"] = host
        request = Request(f"{self.base_url}{path}", headers=request_headers)
        return urlopen(request)

    def test_healthz_returns_minimal_json_for_reserved_host(self):
        with self._urlopen("/healthz", host="mim.madup.app") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            self.assertEqual(json.load(response), {"status": "ok"})

    def test_root_accepts_reserved_host_with_case_port_and_trailing_dot(self):
        os.environ["MIM_TEST_SECRET"] = "must-not-leak"

        try:
            with self._urlopen(
                "/",
                host="MIM.MADUP.APP.:443",
                headers={"X-MIM-Test-Secret": "header-must-not-leak"},
            ) as response:
                body = response.read().decode("utf-8")
        finally:
            os.environ.pop("MIM_TEST_SECRET", None)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "text/html")
        self.assertIn("Madup Infra Manager", body)
        self.assertNotIn("must-not-leak", body)
        self.assertNotIn("header-must-not-leak", body)

    def test_reserved_host_fails_closed_when_mim_hostname_is_missing(self):
        original_mim_hostname = os.environ.pop("MIM_HOSTNAME", None)

        try:
            with self.assertRaises(HTTPError) as context:
                self._urlopen("/", host="mim.madup.app")
        finally:
            if original_mim_hostname is None:
                os.environ.pop("MIM_HOSTNAME", None)
            else:
                os.environ["MIM_HOSTNAME"] = original_mim_hostname

        self.assertEqual(context.exception.code, 404)

    def test_root_accepts_cloud_run_generated_host_without_hardcoded_hash(self):
        with self._urlopen(
            "/",
            host="MIM-BOOTSTRAP-123456789012.ASIA-NORTHEAST3.RUN.APP.:443",
        ) as response:
            body = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "text/html")
        self.assertIn("Madup Infra Manager", body)

    def test_root_rejects_apex_host(self):
        with self.assertRaises(HTTPError) as context:
            self._urlopen("/", host="madup.app")

        self.assertEqual(context.exception.code, 404)

    def test_root_rejects_an_unexpected_host(self):
        with self.assertRaises(HTTPError) as context:
            self._urlopen("/", host="unassigned.apps.madup.app")

        self.assertEqual(context.exception.code, 404)

    def test_healthz_rejects_an_unexpected_host(self):
        with self.assertRaises(HTTPError) as context:
            self._urlopen("/healthz", host="unassigned.apps.madup.app")

        self.assertEqual(context.exception.code, 404)

    def test_unknown_path_returns_404(self):
        with self.assertRaises(HTTPError) as context:
            urlopen(f"{self.base_url}/unknown")

        self.assertEqual(context.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
