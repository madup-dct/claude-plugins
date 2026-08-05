import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MIM_ROOT = REPO_ROOT / "plugins" / "madup-infra-manager"
DOCKERFILES = {
    Path("plugins/madup-infra-manager/builder/Dockerfile"): {
        "froms": (
            "docker.io/library/docker:29.6.2-cli@sha256:be132a9f282288de4afaf63379dff75711fda0147c6b72a9df44e51841402144",
        ),
        "runtime_user": "root",
    },
    Path("plugins/madup-infra-manager/control-plane/Dockerfile"): {
        "froms": (
            "ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded",
            "docker.io/library/python:3.13.14-slim-trixie@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251",
        ),
        "runtime_user": "appuser",
    },
    Path("plugins/madup-infra-manager/control-plane/bootstrap/Dockerfile"): {
        "froms": (
            "docker.io/library/python:3.13.14-slim-trixie@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251",
        ),
        "runtime_user": "10001:10001",
    },
    Path("plugins/madup-infra-manager/app-gateway-go/Dockerfile"): {
        "froms": (
            "docker.io/library/golang:1.26.5-alpine@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2",
            "gcr.io/distroless/static-debian12:nonroot@sha256:f5b485ea962d9bd1186b2f6b3a061191539b905b82ec395de78cbfae51f20e35",
        ),
        "runtime_user": "nonroot:nonroot",
    },
}
FROM_RE = re.compile(r"^FROM\s+(?P<image>\S+)(?:\s+AS\s+\S+)?$", re.MULTILINE)
USER_RE = re.compile(r"^USER\s+(?P<user>\S+)$", re.MULTILINE)
PINNED_FROM_RE = re.compile(r"^FROM\s+\S+@sha256:[0-9a-f]{64}(?:\s+AS\s+\S+)?$", re.MULTILINE)
FORBIDDEN_SNIPPETS = (
    "golang:1.24.4",
    "docker:27.5.1",
    "python:3.13-slim",
    "pip install --no-cache-dir uv==",
)


class TestMimContainerSupplyChain(unittest.TestCase):
    maxDiff = None

    def test_every_mim_dockerfile_is_accounted_for(self) -> None:
        discovered = {
            path.relative_to(REPO_ROOT)
            for path in MIM_ROOT.rglob("Dockerfile")
            if path.is_file()
        }
        self.assertEqual(discovered, set(DOCKERFILES))

    def test_every_from_uses_the_approved_digest_pinned_manifest(self) -> None:
        for relative_path, expected in DOCKERFILES.items():
            content = self._read_text(relative_path)
            from_lines = [line for line in content.splitlines() if line.startswith("FROM ")]
            for from_line in from_lines:
                with self.subTest(path=relative_path, from_line=from_line):
                    self.assertRegex(from_line, PINNED_FROM_RE)
            self.assertEqual(FROM_RE.findall(content), list(expected["froms"]))

    def test_legacy_toolchain_tags_are_absent(self) -> None:
        for relative_path in DOCKERFILES:
            content = self._read_text(relative_path)
            for forbidden in FORBIDDEN_SNIPPETS:
                with self.subTest(path=relative_path, forbidden=forbidden):
                    self.assertNotIn(forbidden, content)

    def test_runtime_user_contracts_are_preserved(self) -> None:
        for relative_path, expected in DOCKERFILES.items():
            users = USER_RE.findall(self._read_text(relative_path))
            self.assertTrue(users, f"{relative_path} must declare at least one USER")
            self.assertEqual(users[-1], expected["runtime_user"])

    def test_control_plane_sources_uv_from_pinned_official_image(self) -> None:
        content = self._read_text(Path("plugins/madup-infra-manager/control-plane/Dockerfile"))
        self.assertIn("COPY --from=uv /uv /uvx /usr/local/bin/", content)
        self.assertNotIn("pip install", content)

    def test_bootstrap_user_has_no_shell_or_home(self) -> None:
        content = self._read_text(Path("plugins/madup-infra-manager/control-plane/bootstrap/Dockerfile"))
        self.assertIn("--no-create-home", content)
        self.assertIn("--home-dir /nonexistent", content)
        self.assertIn("--shell /usr/sbin/nologin", content)
        self.assertNotIn("--create-home", content)

    def test_go_runtime_stage_keeps_cgo_disabled(self) -> None:
        content = self._read_text(Path("plugins/madup-infra-manager/app-gateway-go/Dockerfile"))
        self.assertIn("CGO_ENABLED=0", content)

    def _read_text(self, relative_path: Path) -> str:
        return (REPO_ROOT / relative_path).read_text(encoding="utf-8")
