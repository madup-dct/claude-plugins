import os
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_ROOT = REPO_ROOT / "plugins" / "madup-infra-manager" / "infra" / "release"
VERIFY_SH = RELEASE_ROOT / "verify.sh"
INSTALL_HOOKS_SH = RELEASE_ROOT / "install_git_hooks.sh"
GUARD_PY = RELEASE_ROOT / "public_release_guard.py"
HOOK_PATH = REPO_ROOT / ".githooks" / "pre-push"
README_PATH = REPO_ROOT / "README.md"
DENYLIST_RELATIVE = Path("plugins/madup-infra-manager/infra/release/denylist.exact")
ADVISORY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "mim-public-release-advisory.yml"
GATE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "mim-public-release-gate.yml"
TEST_TIMEOUT_SECONDS = 20
CHECKOUT_V7_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_V7_SHA = "5fda3b95a4ea91299a34e894583c3862153e4b97"


def _run_subprocess(*args, **kwargs):
    kwargs.setdefault("timeout", TEST_TIMEOUT_SECONDS)
    kwargs.setdefault("check", False)
    return subprocess.run(*args, **kwargs)


def _require_file(path: Path) -> Path:
    if not path.exists():
        raise AssertionError(f"Missing required file: {path}")
    return path


def _read_text(path: Path) -> str:
    return _require_file(path).read_text(encoding="utf-8")


class TempRepoHarness:
    def __init__(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        self.log_dir = self.root / "stub-logs"
        self.log_dir.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()

    def cleanup(self) -> None:
        self._temp_dir.cleanup()

    def git(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        result = _run_subprocess(
            ["git", *args],
            cwd=self.root,
            text=True,
            input=input_text,
            capture_output=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")
        return result

    def copy_repo_file(self, source: Path) -> Path:
        destination = self.root / source.relative_to(REPO_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    def write_text(self, relative_path: str | Path, content: str, *, mode: int | None = None) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if mode is not None:
            os.chmod(path, mode)
        return path

    def write_stub_python3(self) -> Path:
        script = textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            log_dir="${STUB_LOG_DIR:?}"
            count_file="${log_dir}/count"
            if [[ -f "${count_file}" ]]; then
              call_id="$(( $(cat "${count_file}") + 1 ))"
            else
              call_id=1
            fi
            printf '%s' "${call_id}" > "${count_file}"
            printf '%s\\n' "$@" > "${log_dir}/${call_id}.args"
            cat > "${log_dir}/${call_id}.stdin"
            if [[ -n "${MIM_PUBLIC_RELEASE_DENYLIST_FILE:-}" ]]; then
              printf '%s\n' "${MIM_PUBLIC_RELEASE_DENYLIST_FILE}" > "${log_dir}/${call_id}.denylist_path"
              if [[ -e "${MIM_PUBLIC_RELEASE_DENYLIST_FILE}" ]]; then
                stat -f '%Lp' "${MIM_PUBLIC_RELEASE_DENYLIST_FILE}" > "${log_dir}/${call_id}.denylist_mode"
                wc -c < "${MIM_PUBLIC_RELEASE_DENYLIST_FILE}" | tr -d '[:space:]' > "${log_dir}/${call_id}.denylist_size"
              fi
            fi
            if [[ "${1:-}" == "-m" && "${2:-}" == "unittest" ]]; then
              exit "${STUB_UNITTEST_EXIT:-0}"
            fi
            if [[ "${2:-}" == "pre-push" ]]; then
              exit "${STUB_PRE_PUSH_EXIT:-0}"
            fi
            if [[ "${2:-}" == "verify" && "${3:-}" == "--local" ]]; then
              exit "${STUB_VERIFY_LOCAL_EXIT:-0}"
            fi
            if [[ "${2:-}" == "verify" && "${3:-}" == "--range" ]]; then
              exit "${STUB_VERIFY_RANGE_EXIT:-0}"
            fi
            exit "${STUB_DEFAULT_EXIT:-99}"
            """
        )
        path = self.write_text("bin/python3", script, mode=0o755)
        return path

    def write_stub_claude(self) -> Path:
        script = textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$@" >> "${STUB_LOG_DIR:?}/claude.args"
            exit "${STUB_CLAUDE_EXIT:-0}"
            """
        )
        return self.write_text("bin/claude", script, mode=0o755)

    def write_stub_uv(self) -> Path:
        script = textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$*" >> "${STUB_LOG_DIR:?}/uv.args"
            exit "${STUB_UV_EXIT:-0}"
            """
        )
        return self.write_text("bin/uv", script, mode=0o755)

    def write_stub_npm(self) -> Path:
        script = textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$*" >> "${STUB_LOG_DIR:?}/npm.args"
            exit "${STUB_NPM_EXIT:-0}"
            """
        )
        return self.write_text("bin/npm", script, mode=0o755)

    def write_stub_go(self) -> Path:
        script = textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$*" >> "${STUB_LOG_DIR:?}/go.args"
            exit "${STUB_GO_EXIT:-0}"
            """
        )
        return self.write_text("bin/go", script, mode=0o755)

    def write_stub_docker(self) -> Path:
        script = textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$*" >> "${STUB_LOG_DIR:?}/docker.args"
            exit "${STUB_DOCKER_EXIT:-0}"
            """
        )
        return self.write_text("bin/docker", script, mode=0o755)

    def write_passing_shell_script(self, relative_path: str | Path) -> Path:
        return self.write_text(
            relative_path,
            "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
            mode=0o755,
        )

    def env(self, **extra: str) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}:{env['PATH']}"
        env["STUB_LOG_DIR"] = str(self.log_dir)
        env.update(extra)
        return env

    def run(self, *args: str, env: dict[str, str] | None = None, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return _run_subprocess(
            list(args),
            cwd=self.root,
            text=True,
            input=stdin,
            capture_output=True,
            env=env,
        )

    def read_log_args(self, call_id: int) -> list[str]:
        return (self.log_dir / f"{call_id}.args").read_text(encoding="utf-8").splitlines()

    def read_log_stdin(self, call_id: int) -> str:
        return (self.log_dir / f"{call_id}.stdin").read_text(encoding="utf-8")

    def read_log_text(self, call_id: int, suffix: str) -> str:
        return (self.log_dir / f"{call_id}.{suffix}").read_text(encoding="utf-8").strip()


class TestMimReleaseContracts(unittest.TestCase):
    maxDiff = None

    def assertSamePath(self, actual: str, expected: Path) -> None:
        self.assertEqual(os.path.realpath(actual), os.path.realpath(str(expected)))

    def test_release_wrapper_files_exist_and_are_executable(self):
        required_paths = (VERIFY_SH, INSTALL_HOOKS_SH, HOOK_PATH)
        for path in required_paths:
            with self.subTest(path=path):
                _require_file(path)
                mode = path.stat().st_mode
                self.assertTrue(mode & stat.S_IXUSR, f"{path} must be executable by the owner")

    def test_public_guard_shell_sources_are_strict_and_do_not_mutate_clouds(self):
        for path in (VERIFY_SH, INSTALL_HOOKS_SH, HOOK_PATH):
            content = _read_text(path)
            with self.subTest(path=path):
                self.assertIn("set -euo pipefail", content)
                self.assertNotIn("kubectl", content)
                self.assertNotIn("terraform", content)
                self.assertNotIn("bypass", content.lower())
                if path != VERIFY_SH:
                    self.assertNotIn("gcloud", content)
                    self.assertNotIn("MIM_ENABLE_MUTATIONS", content)

    def test_release_verifier_exposes_every_local_and_staging_gate(self):
        content = _read_text(VERIFY_SH)
        required_fragments = (
            "--local",
            "--staging",
            "plugin-validation",
            "python-lint",
            "python-typecheck",
            "python-unit",
            "python-integration",
            "shell-suites",
            "infra/github/test_preflight.sh",
            "edge-tests",
            "go-app-gateway",
            "container-build",
            "secret-scan",
            "mutation-gate",
            "iam-policy-diff",
            "direct-origin-denial",
            "sensitive-project-denial",
            "runtime-iam-canary",
            "slack-oauth-canary",
            "staging-canary-contract",
            "authenticated-readonly-smoke",
            "public-app-live",
            "MIM_ENABLE_MUTATIONS",
            "MIM_SLACK_ENABLED",
            "MIM_REQUIRE_STAGING_CANARIES",
            "claude plugin validate --strict",
            "uv run ruff check",
            "uv run mypy",
            "python -m unittest discover",
            "docker build",
            "go test ./...",
            "go vet ./...",
            "go test -race ./...",
            "CGO_ENABLED=0 go build ./cmd/mim-app-gateway",
            "mim-app-gateway:release-check",
            "audit_iam.sh",
            "smoke_test.sh",
            "tests.staging.test_runtime_iam_canary",
            "tests.staging.test_slack_oauth_canary",
            "tests.test_staging_canary_contract",
            "--public-app",
            "run_public_app_live",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, content)

        forbidden_fragments = (
            "apply.sh --apply",
            "plan.sh --plan",
            "MIM_ENABLE_MUTATIONS=${MIM_ENABLE_MUTATIONS:-true}",
        )
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, content)

    def test_pre_push_hook_execs_guard_with_exact_remote_arguments_and_untouched_stdin(self):
        _require_file(HOOK_PATH)
        _require_file(GUARD_PY)

        harness = TempRepoHarness()
        try:
            harness.copy_repo_file(HOOK_PATH)
            harness.copy_repo_file(GUARD_PY)
            harness.write_stub_python3()
            harness.write_stub_claude()
            caller_override = harness.write_text("caller-denylist.exact", "opaque-value\n", mode=0o600)
            default_denylist = harness.write_text(DENYLIST_RELATIVE, "default-value\n", mode=0o600)

            hook = harness.root / HOOK_PATH.relative_to(REPO_ROOT)
            stdin_text = (
                "refs/heads/main 1111111111111111111111111111111111111111 "
                "refs/heads/main 0000000000000000000000000000000000000000\n"
                "refs/heads/feature 2222222222222222222222222222222222222222 "
                "refs/heads/feature 3333333333333333333333333333333333333333\n"
            )

            result = harness.run(
                str(hook),
                "origin",
                "https://example.test/repo.git",
                env=harness.env(
                    STUB_PRE_PUSH_EXIT="17",
                    MIM_PUBLIC_RELEASE_DENYLIST_FILE=str(caller_override),
                ),
                stdin=stdin_text,
            )

            self.assertEqual(result.returncode, 17)
            logged_args = harness.read_log_args(1)
            self.assertEqual(logged_args[1:], ["pre-push", "origin", "https://example.test/repo.git"])
            self.assertSamePath(logged_args[0], harness.root / GUARD_PY.relative_to(REPO_ROOT))
            self.assertEqual(harness.read_log_stdin(1), stdin_text)
            self.assertSamePath(
                harness.read_log_text(1, "denylist_path"),
                harness.root / default_denylist.relative_to(harness.root),
            )
            self.assertNotEqual(
                os.path.realpath(harness.read_log_text(1, "denylist_path")),
                os.path.realpath(str(caller_override)),
            )
        finally:
            harness.cleanup()

    def test_pre_push_hook_fails_closed_without_default_denylist_before_any_network_transfer(self):
        _require_file(HOOK_PATH)
        _require_file(GUARD_PY)

        harness = TempRepoHarness()
        try:
            harness.copy_repo_file(HOOK_PATH)
            harness.copy_repo_file(GUARD_PY)
            caller_override = harness.write_text("caller-denylist.exact", "opaque-value\n", mode=0o600)
            hook = harness.root / HOOK_PATH.relative_to(REPO_ROOT)

            result = harness.run(
                str(hook),
                "origin",
                "https://example.test/repo.git",
                env=harness.env(MIM_PUBLIC_RELEASE_DENYLIST_FILE=str(caller_override)),
                stdin="",
            )

            self.assertEqual(result.returncode, 3)
            self.assertIn("denylist", result.stderr.lower())
        finally:
            harness.cleanup()

    def test_install_git_hooks_sets_repo_local_hookspath_and_is_idempotent(self):
        _require_file(INSTALL_HOOKS_SH)
        _require_file(VERIFY_SH)
        _require_file(HOOK_PATH)
        _require_file(GUARD_PY)

        harness = TempRepoHarness()
        try:
            installer = harness.copy_repo_file(INSTALL_HOOKS_SH)
            copied_verify = harness.copy_repo_file(VERIFY_SH)
            copied_hook = harness.copy_repo_file(HOOK_PATH)
            harness.copy_repo_file(GUARD_PY)

            first = harness.run("bash", str(installer))
            second = harness.run("bash", str(installer))

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            config_value = harness.git("config", "--get", "core.hooksPath").stdout.strip()
            self.assertEqual(config_value, ".githooks")
            self.assertTrue(os.access(copied_hook, os.X_OK))
            self.assertTrue(os.access(installer, os.X_OK))
            self.assertTrue(os.access(copied_verify, os.X_OK))
        finally:
            harness.cleanup()

    def test_verify_ci_runs_local_scan_and_contract_tests_and_prints_first_push_disclaimer(self):
        _require_file(VERIFY_SH)
        _require_file(GUARD_PY)

        harness = TempRepoHarness()
        try:
            verify = harness.copy_repo_file(VERIFY_SH)
            harness.copy_repo_file(GUARD_PY)
            harness.write_stub_python3()
            harness.write_stub_claude()
            caller_override = harness.write_text("caller-denylist.exact", "opaque-value\n", mode=0o600)
            default_denylist = harness.write_text(DENYLIST_RELATIVE, "default-value\n", mode=0o600)

            result = harness.run(
                "bash",
                str(verify),
                "--ci",
                env=harness.env(MIM_PUBLIC_RELEASE_DENYLIST_FILE=str(caller_override)),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cannot approve the first public push", result.stdout + result.stderr)
            logged_args = harness.read_log_args(1)
            self.assertEqual(logged_args[1:], ["verify", "--local"])
            self.assertSamePath(logged_args[0], harness.root / GUARD_PY.relative_to(REPO_ROOT))
            denylist_path = Path(harness.read_log_text(1, "denylist_path"))
            self.assertNotEqual(os.path.realpath(str(denylist_path)), os.path.realpath(str(caller_override)))
            self.assertNotEqual(os.path.realpath(str(denylist_path)), os.path.realpath(str(default_denylist)))
            self.assertEqual(harness.read_log_text(1, "denylist_mode"), "600")
            self.assertEqual(harness.read_log_text(1, "denylist_size"), "0")
            self.assertFalse(denylist_path.exists(), "temporary CI denylist must be removed after the scan")
            unittest_args = harness.read_log_args(2)
            self.assertEqual(unittest_args[:3], ["-m", "unittest", "tests/test_public_release_guard.py"])
            self.assertIn("tests/test_mim_release_contract.py", unittest_args)
            self.assertIn("tests/test_madup_infra_manager_plugin.py", unittest_args)
            self.assertIn("tests/test_mim_public_boundary.py", unittest_args)
            self.assertIn("-v", unittest_args)
            self.assertFalse((harness.log_dir / "2.denylist_path").exists())
        finally:
            harness.cleanup()

    def test_verify_local_fails_closed_for_invalid_slack_mode(self):
        _require_file(VERIFY_SH)
        _require_file(GUARD_PY)

        harness = TempRepoHarness()
        try:
            verify = harness.copy_repo_file(VERIFY_SH)
            harness.copy_repo_file(GUARD_PY)

            result = harness.run(
                "bash",
                str(verify),
                "--local",
                env=harness.env(MIM_SLACK_ENABLED="maybe"),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "MIM_SLACK_ENABLED must be exact true or false",
                result.stderr,
            )
            self.assertFalse((harness.log_dir / "claude.args").exists())
            self.assertFalse((harness.log_dir / "uv.args").exists())
        finally:
            harness.cleanup()

    def test_verify_local_skips_slack_canary_when_slack_disabled_and_runs_when_enabled(self):
        _require_file(VERIFY_SH)
        _require_file(GUARD_PY)

        shell_suite_scripts = (
            "plugins/madup-infra-manager/infra/domain/test_preflight.sh",
            "plugins/madup-infra-manager/infra/domain/test_apply_cloud_run.sh",
            "plugins/madup-infra-manager/infra/control-plane/test_prepare_config.sh",
            "plugins/madup-infra-manager/infra/control-plane/test_preflight.sh",
            "plugins/madup-infra-manager/infra/control-plane/test_apply.sh",
            "plugins/madup-infra-manager/infra/billing/test_plan.sh",
            "plugins/madup-infra-manager/infra/billing/test_apply.sh",
            "plugins/madup-infra-manager/infra/github/test_preflight.sh",
            "plugins/madup-infra-manager/infra/github/test_plan_connection.sh",
            "plugins/madup-infra-manager/infra/github/test_apply_connection.sh",
            "plugins/madup-infra-manager/infra/runtime-bootstrap/test_prepare_input.sh",
            "plugins/madup-infra-manager/infra/runtime-bootstrap/test_plan.sh",
            "plugins/madup-infra-manager/infra/runtime-bootstrap/test_apply.sh",
            "plugins/madup-infra-manager/infra/release/test_task18_lib.sh",
            "plugins/madup-infra-manager/infra/release/test_plan.sh",
            "plugins/madup-infra-manager/infra/release/test_apply.sh",
            "plugins/madup-infra-manager/builder/test_builder.sh",
            "plugins/madup-infra-manager/infra/edge/test_plan.sh",
            "plugins/madup-infra-manager/infra/edge/test_apply.sh",
        )

        def prepare_local_verify_harness() -> TempRepoHarness:
            local_harness = TempRepoHarness()
            local_harness.copy_repo_file(VERIFY_SH)
            local_harness.copy_repo_file(GUARD_PY)
            local_harness.write_stub_python3()
            local_harness.write_stub_claude()
            local_harness.write_stub_uv()
            local_harness.write_stub_npm()
            local_harness.write_stub_go()
            local_harness.write_stub_docker()
            for relative_path in shell_suite_scripts:
                local_harness.write_passing_shell_script(relative_path)
            local_harness.write_text(
                "plugins/madup-infra-manager/control-plane/.keep",
                "",
            )
            local_harness.write_text(
                "plugins/madup-infra-manager/app-gateway-go/.keep",
                "",
            )
            local_harness.write_text(
                "plugins/madup-infra-manager/edge/worker/.keep",
                "",
            )
            return local_harness

        disabled_harness = prepare_local_verify_harness()
        try:
            disabled_verify = disabled_harness.root / VERIFY_SH.relative_to(REPO_ROOT)
            disabled = disabled_harness.run(
                "bash",
                str(disabled_verify),
                "--local",
                env=disabled_harness.env(MIM_SLACK_ENABLED="false"),
            )
            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            disabled_uv_log = (disabled_harness.log_dir / "uv.args").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("tests.staging.test_slack_oauth_canary", disabled_uv_log)
        finally:
            disabled_harness.cleanup()

        enabled_harness = prepare_local_verify_harness()
        try:
            enabled_verify = enabled_harness.root / VERIFY_SH.relative_to(REPO_ROOT)
            enabled = enabled_harness.run(
                "bash",
                str(enabled_verify),
                "--local",
                env=enabled_harness.env(MIM_SLACK_ENABLED="true"),
            )
            self.assertEqual(enabled.returncode, 0, enabled.stderr)
            enabled_uv_log = (enabled_harness.log_dir / "uv.args").read_text(
                encoding="utf-8"
            )
            self.assertIn("tests.staging.test_slack_oauth_canary", enabled_uv_log)
        finally:
            enabled_harness.cleanup()

    def test_verify_release_requires_safe_base_ref_and_runs_both_scans_before_tests(self):
        _require_file(VERIFY_SH)
        _require_file(GUARD_PY)

        harness = TempRepoHarness()
        try:
            verify = harness.copy_repo_file(VERIFY_SH)
            harness.copy_repo_file(GUARD_PY)
            harness.write_stub_python3()
            harness.write_stub_claude()
            caller_override = harness.write_text("caller-denylist.exact", "opaque-value\n", mode=0o600)
            default_denylist = harness.write_text(DENYLIST_RELATIVE, "default-value\n", mode=0o600)

            invalid = harness.run("bash", str(verify), "--release", "--bad", env=harness.env())
            self.assertEqual(invalid.returncode, 2)
            self.assertFalse((harness.log_dir / "1.args").exists())

            failing = harness.run(
                "bash",
                str(verify),
                "--release",
                "origin/main",
                env=harness.env(
                    STUB_VERIFY_RANGE_EXIT="12",
                    MIM_PUBLIC_RELEASE_DENYLIST_FILE=str(caller_override),
                ),
            )

            self.assertEqual(failing.returncode, 12)
            local_args = harness.read_log_args(1)
            self.assertEqual(
                local_args[1:],
                ["verify", "--local", "--base-ref", "origin/main", "--require-exact-values"],
            )
            self.assertSamePath(local_args[0], harness.root / GUARD_PY.relative_to(REPO_ROOT))
            self.assertSamePath(
                harness.read_log_text(1, "denylist_path"),
                harness.root / default_denylist.relative_to(harness.root),
            )
            range_args = harness.read_log_args(2)
            self.assertEqual(range_args[1:], ["verify", "--range", "origin/main..HEAD"])
            self.assertSamePath(range_args[0], harness.root / GUARD_PY.relative_to(REPO_ROOT))
            self.assertSamePath(
                harness.read_log_text(2, "denylist_path"),
                harness.root / default_denylist.relative_to(harness.root),
            )
            self.assertNotEqual(
                os.path.realpath(harness.read_log_text(1, "denylist_path")),
                os.path.realpath(str(caller_override)),
            )
            self.assertNotEqual(
                os.path.realpath(harness.read_log_text(2, "denylist_path")),
                os.path.realpath(str(caller_override)),
            )
            self.assertFalse((harness.log_dir / "3.args").exists())
        finally:
            harness.cleanup()

    def test_verify_release_fails_closed_without_default_denylist_even_when_caller_override_exists(self):
        _require_file(VERIFY_SH)
        _require_file(GUARD_PY)

        harness = TempRepoHarness()
        try:
            verify = harness.copy_repo_file(VERIFY_SH)
            harness.copy_repo_file(GUARD_PY)
            caller_override = harness.write_text("caller-denylist.exact", "opaque-value\n", mode=0o600)
            harness.write_text("README.md", "baseline\n")
            harness.git("add", "README.md")
            harness.git("commit", "-m", "baseline")

            result = harness.run(
                "bash",
                str(verify),
                "--release",
                "HEAD",
                env=harness.env(MIM_PUBLIC_RELEASE_DENYLIST_FILE=str(caller_override)),
            )

            self.assertEqual(result.returncode, 3)
            self.assertIn("denylist", result.stderr.lower())
        finally:
            harness.cleanup()

    def test_verify_release_unsets_default_denylist_override_before_contract_tests(self):
        _require_file(VERIFY_SH)
        _require_file(GUARD_PY)

        harness = TempRepoHarness()
        try:
            verify = harness.copy_repo_file(VERIFY_SH)
            harness.copy_repo_file(GUARD_PY)
            harness.write_stub_python3()
            harness.write_stub_claude()
            caller_override = harness.write_text("caller-denylist.exact", "opaque-value\n", mode=0o600)
            harness.write_text(DENYLIST_RELATIVE, "default-value\n", mode=0o600)

            result = harness.run(
                "bash",
                str(verify),
                "--release",
                "origin/main",
                env=harness.env(MIM_PUBLIC_RELEASE_DENYLIST_FILE=str(caller_override)),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((harness.log_dir / "3.denylist_path").exists())
        finally:
            harness.cleanup()

    def test_release_denylist_path_remains_ignored_and_untracked(self):
        result = _run_subprocess(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", str(DENYLIST_RELATIVE)],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_readme_documents_release_guard_installation_and_limitations(self):
        readme = _read_text(README_PATH)
        required_phrases = (
            "install_git_hooks.sh",
            "verify.sh --ci",
            "verify.sh --release origin/main",
            "--require-exact-values",
            "denylist.exact",
            "0600",
            "non-comment",
            "pre-push",
            "first public push",
            "defense-in-depth",
            "advisory",
            "workflow_dispatch",
            "mim-public-release",
            "Git hook",
            "skip",
            "require `verify.sh --release` before first public push",
            "manual exact gate",
            "public repo CI cannot prevent the first leaked commit",
            "private/pre-receive",
            "protected review",
            "branch policy",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)

    def test_advisory_workflow_contract_is_minimal_and_secret_free(self):
        content = _read_text(ADVISORY_WORKFLOW_PATH)
        required_fragments = (
            "name: MIM Public Release Advisory",
            "pull_request:",
            "push:",
            "branches: [main]",
            "contents: read",
            "concurrency:",
            "cancel-in-progress: true",
            f"uses: actions/checkout@{CHECKOUT_V7_SHA}",
            f"uses: actions/setup-python@{SETUP_PYTHON_V7_SHA}",
            "bash plugins/madup-infra-manager/infra/release/verify.sh --ci",
            "tests/test_public_release_guard.py",
            "tests/test_mim_release_contract.py",
        )
        forbidden_fragments = (
            "pull_request_target",
            "upload-artifact",
            "secrets.MIM_PUBLIC_RELEASE_DENYLIST_EXACT",
            "MIM_PUBLIC_RELEASE_DENYLIST_EXACT",
            "actions/checkout@v4",
            "actions/setup-python@v5",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, content)
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, content)

    def test_gate_workflow_contract_uses_trusted_scanner_and_secret_cleanup_before_candidate_code(self):
        content = _read_text(GATE_WORKFLOW_PATH)
        required_fragments = (
            "name: MIM Public Release Gate",
            "workflow_dispatch:",
            "candidate_sha:",
            "required: true",
            "environment: mim-public-release",
            "contents: read",
            "concurrency:",
            "github.ref != 'refs/heads/main'",
            f"uses: actions/checkout@{CHECKOUT_V7_SHA}",
            f"uses: actions/setup-python@{SETUP_PYTHON_V7_SHA}",
            "path: trusted",
            "path: candidate",
            "ref: refs/heads/main",
            "ref: ${{ inputs.candidate_sha }}",
            "fetch-depth: 0",
            "persist-credentials: false",
            "CANDIDATE_SHA: ${{ inputs.candidate_sha }}",
            "MIM_PUBLIC_RELEASE_DENYLIST_EXACT",
            "^[0-9a-f]{40}$",
            "\"${CANDIDATE_SHA}\"",
            "git -C \"$candidate_dir\" rev-parse HEAD",
            "chmod 600",
            "fetch --no-tags",
            "trusted",
            "candidate",
            "umask 077",
            "scanner_path=\"${GITHUB_WORKSPACE}/trusted/plugins/madup-infra-manager/infra/release/public_release_guard.py\"",
            "python3 \"$scanner_path\" verify --local --base-ref origin/main --require-exact-values",
            "python3 \"$scanner_path\" verify --range origin/main..HEAD",
            "rm -f -- \"$denylist_file\"",
            "unset MIM_PUBLIC_RELEASE_DENYLIST_EXACT",
            "trap cleanup EXIT HUP INT TERM",
            "trap - EXIT HUP INT TERM",
            "python3 -m unittest tests/test_public_release_guard.py tests/test_mim_release_contract.py tests/test_madup_infra_manager_plugin.py tests/test_mim_public_boundary.py -v",
        )
        forbidden_fragments = (
            "pull_request_target",
            "upload-artifact",
            "cat <<",
            "echo \"$MIM_PUBLIC_RELEASE_DENYLIST_EXACT\"",
            "persist-credentials: true",
            "actions/checkout@v4",
            "actions/setup-python@v5",
            "ln -s",
            "git worktree add",
            "trusted/plugins/madup-infra-manager/infra/release/public_release_guard.py verify",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, content)
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, content)
        run_bodies = re.findall(r"run: \|\n((?:[ ]{10,}.*\n)+)", content)
        self.assertGreaterEqual(len(run_bodies), 1)
        for body in run_bodies:
            self.assertNotIn("${{ inputs.", body)
        umask_index = content.index("umask 077")
        chmod_index = content.index("chmod 600")
        self.assertLess(umask_index, chmod_index)
        cleanup_index = content.index("rm -f -- \"$denylist_file\"")
        candidate_test_index = content.index("python3 -m unittest")
        self.assertLess(cleanup_index, candidate_test_index)


if __name__ == "__main__":
    unittest.main()
