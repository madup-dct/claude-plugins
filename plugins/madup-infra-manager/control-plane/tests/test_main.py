from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

TEST_ROOT = Path(__file__).resolve().parent
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
for path in (TEST_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def built_app() -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


class MainEntrypointTests(unittest.TestCase):
    def test_module_exposes_runtime_built_app(self) -> None:
        runtime = importlib.import_module("mim_control_plane.runtime")
        with mock.patch.object(
            runtime,
            "build_runtime_app_from_environment",
            return_value=built_app(),
        ):
            module = importlib.import_module("mim_control_plane.main")
            module = importlib.reload(module)

        client = TestClient(module.app)
        self.assertEqual(client.get("/healthz").status_code, 200)

    def test_import_does_not_swallow_runtime_startup_failures(self) -> None:
        runtime = importlib.import_module("mim_control_plane.runtime")
        with mock.patch.object(
            runtime,
            "build_runtime_app_from_environment",
            side_effect=RuntimeError("bootstrap secret parse failed"),
        ):
            if "mim_control_plane.main" in sys.modules:
                del sys.modules["mim_control_plane.main"]
            with self.assertRaisesRegex(RuntimeError, "bootstrap secret parse failed"):
                importlib.import_module("mim_control_plane.main")

    def test_legacy_app_factory_environment_is_not_used(self) -> None:
        runtime = importlib.import_module("mim_control_plane.runtime")
        with mock.patch.dict(
            os.environ,
            {
                "MIM_RUNTIME_MODE": "control-plane",
                "MIM_RUNTIME_BOOTSTRAP_SECRET_VERSION": (
                    "projects/mim-prod-123456/secrets/"
                    "mim-runtime-bootstrap/versions/1"
                ),
                "MIM_ENABLE_MUTATIONS": "false",
                "MIM_CONTROL_PLANE_APP_FACTORY": "tests.test_main:built_app",
            },
            clear=True,
        ):
            with mock.patch.object(
                runtime,
                "build_runtime_app_from_environment",
                return_value=built_app(),
            ) as build_runtime:
                module = importlib.import_module("mim_control_plane.main")
                module = importlib.reload(module)

        self.assertIsInstance(module.app, FastAPI)
        self.assertGreaterEqual(build_runtime.call_count, 1)


if __name__ == "__main__":
    unittest.main()
