import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_DIR = REPO_ROOT / "plugins" / "madup-infra-manager" / "infra" / "domain"

CONFIG_EXAMPLE = DOMAIN_DIR / "config.example.env"
CONFIG_LIB = DOMAIN_DIR / "config_lib.sh"
PREFLIGHT = DOMAIN_DIR / "preflight.sh"
APPLY = DOMAIN_DIR / "apply_cloud_run.sh"
SNAPSHOT_HELPER = DOMAIN_DIR / "snapshot_private_files.py"
TEST_PREFLIGHT = DOMAIN_DIR / "test_preflight.sh"
TEST_APPLY = DOMAIN_DIR / "test_apply_cloud_run.sh"
GITIGNORE = DOMAIN_DIR / ".gitignore"

ALLOWED_CONFIG_KEYS = {
    "MIM_OPERATOR_EMAIL",
    "MIM_PROJECT_ID",
    "MIM_ORGANIZATION_ID",
    "MIM_BILLING_ACCOUNT_ID",
}
LEGACY_CONFIG_KEYS = {
    "MIM_ACCOUNT",
    "MIM_REGION",
    "MIM_HOSTNAME",
    "MIM_APEX_ACTION",
    "MIM_INITIAL_IAP_MEMBER",
}
SYNTHETIC_OPERATOR = "operator.test@madup.com"
SYNTHETIC_PROJECT = "mim-prod-123456"
SYNTHETIC_ORG = "123456789012"
SYNTHETIC_BILLING = "ABCDEF-123456-7890AB"
FORBIDDEN_GUIDANCE = (
    "gcloud auth login",
    "service account key",
    "cloud credential",
    "cloud credentials",
    "copy it to",
    "plugin user should",
)


def _join(*parts: str) -> str:
    return "".join(parts)


def _run_service_url(
    label: str = "synthetic-bootstrap-12345",
    *,
    scheme: str = "https://",
    suffix: str = "",
    regional: bool = False,
) -> str:
    family = _join(".run", ".app") if regional else _join(".a.run", ".app")
    return _join(scheme, label, family, suffix)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_bash(script: str, *, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", script],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


class TestMimPublicBoundary(unittest.TestCase):
    def test_config_example_uses_only_four_placeholders_and_central_boundary_language(self):
        lines = [
            line.strip()
            for line in _read(CONFIG_EXAMPLE).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        pairs = dict(line.split("=", 1) for line in lines)

        self.assertEqual(set(pairs), ALLOWED_CONFIG_KEYS)
        self.assertEqual(len(pairs), 4)
        for value in pairs.values():
            self.assertRegex(value, r"^<[^>]+>$")

        text = _read(CONFIG_EXAMPLE).lower()
        self.assertIn("centrally", text)
        self.assertIn("never", text)
        self.assertIn("employee", text)
        self.assertIn("plugin user", text)

    def test_config_library_accepts_the_public_boundary_and_derives_iap_member(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.env"
            config_path.write_text(
                "\n".join(
                    (
                        f"MIM_OPERATOR_EMAIL={SYNTHETIC_OPERATOR}",
                        f"MIM_PROJECT_ID={SYNTHETIC_PROJECT}",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(config_path, 0o600)

            result = _run_bash(
                f"""
                set -euo pipefail
                . {shlex.quote(str(CONFIG_LIB))}
                mim_load_config {shlex.quote(str(config_path))}
                printf '%s\\n' \
                  "$MIM_OPERATOR_EMAIL" \
                  "$MIM_PROJECT_ID" \
                  "$MIM_ORGANIZATION_ID" \
                  "$MIM_BILLING_ACCOUNT_ID" \
                  "$(mim_derive_iap_member)"
                """
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                SYNTHETIC_OPERATOR,
                SYNTHETIC_PROJECT,
                SYNTHETIC_ORG,
                SYNTHETIC_BILLING,
                f"user:{SYNTHETIC_OPERATOR}",
            ],
        )
        self.assertEqual(result.stderr, "")

    def test_config_library_rejects_invalid_config_without_echoing_supplied_values(self):
        cases = (
            (
                "rejects_legacy_key",
                "\n".join(
                    (
                        f"MIM_OPERATOR_EMAIL={SYNTHETIC_OPERATOR}",
                        f"MIM_PROJECT_ID={SYNTHETIC_PROJECT}",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                        "MIM_REGION=asia-northeast3",
                    )
                )
                + "\n",
                "Deprecated config key: MIM_REGION",
                "asia-northeast3",
            ),
            (
                "rejects_unknown_key",
                "\n".join(
                    (
                        f"MIM_OPERATOR_EMAIL={SYNTHETIC_OPERATOR}",
                        f"MIM_PROJECT_ID={SYNTHETIC_PROJECT}",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                        "MIM_UNEXPECTED=secret-token-value",
                    )
                )
                + "\n",
                "Unknown config key: MIM_UNEXPECTED",
                "secret-token-value",
            ),
            (
                "rejects_duplicate_key",
                "\n".join(
                    (
                        f"MIM_OPERATOR_EMAIL={SYNTHETIC_OPERATOR}",
                        f"MIM_PROJECT_ID={SYNTHETIC_PROJECT}",
                        f"MIM_PROJECT_ID={SYNTHETIC_PROJECT}b",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                    )
                )
                + "\n",
                "Duplicate config key: MIM_PROJECT_ID",
                f"{SYNTHETIC_PROJECT}b",
            ),
            (
                "rejects_missing_required_key",
                "\n".join(
                    (
                        f"MIM_OPERATOR_EMAIL={SYNTHETIC_OPERATOR}",
                        f"MIM_PROJECT_ID={SYNTHETIC_PROJECT}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                    )
                )
                + "\n",
                "Missing required setting: MIM_ORGANIZATION_ID",
                SYNTHETIC_ORG,
            ),
            (
                "rejects_wrong_operator_domain",
                "\n".join(
                    (
                        "MIM_OPERATOR_EMAIL=operator.test@example.com",
                        f"MIM_PROJECT_ID={SYNTHETIC_PROJECT}",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                    )
                )
                + "\n",
                "Invalid MIM_OPERATOR_EMAIL: placeholder values are not allowed",
                "operator.test@example.com",
            ),
            (
                "rejects_placeholder_value",
                "\n".join(
                    (
                        f"MIM_OPERATOR_EMAIL={SYNTHETIC_OPERATOR}",
                        "MIM_PROJECT_ID=<set-private-project-id>",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                    )
                )
                + "\n",
                "Invalid MIM_PROJECT_ID: placeholder values are not allowed",
                "<set-private-project-id>",
            ),
            (
                "rejects_unsafe_data_without_execution",
                "\n".join(
                    (
                        f"MIM_OPERATOR_EMAIL={SYNTHETIC_OPERATOR}",
                        "MIM_PROJECT_ID=$(touch should-not-run)",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                    )
                )
                + "\n",
                "Invalid MIM_PROJECT_ID:",
                "$(touch should-not-run)",
            ),
        )

        for case_name, body, expected_error, redacted_value in cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    config_path = Path(temp_dir) / "config.env"
                    config_path.write_text(body, encoding="utf-8")
                    os.chmod(config_path, 0o600)
                    pwned_path = Path(temp_dir) / "should-not-run"

                    result = _run_bash(
                        f"""
                        set -euo pipefail
                        cd {shlex.quote(temp_dir)}
                        . {shlex.quote(str(CONFIG_LIB))}
                        mim_load_config {shlex.quote(str(config_path))}
                        """
                    )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertNotIn(redacted_value, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(pwned_path.exists())

    def test_config_library_rejects_embedded_placeholder_markers_case_insensitively(self):
        cases = (
            (
                "project marker",
                "\n".join(
                    (
                        f"MIM_OPERATOR_EMAIL={SYNTHETIC_OPERATOR}",
                        "MIM_PROJECT_ID=mim-PlaceHolder-123",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                    )
                )
                + "\n",
                "Invalid MIM_PROJECT_ID: placeholder values are not allowed",
                "mim-PlaceHolder-123",
            ),
            (
                "email marker",
                "\n".join(
                    (
                        "MIM_OPERATOR_EMAIL=operator.test+ChangeMe@madup.com",
                        f"MIM_PROJECT_ID={SYNTHETIC_PROJECT}",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                    )
                )
                + "\n",
                "Invalid MIM_OPERATOR_EMAIL: placeholder values are not allowed",
                "operator.test+ChangeMe@madup.com",
            ),
        )

        for case_name, body, expected_error, redacted_value in cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    config_path = Path(temp_dir) / "config.env"
                    config_path.write_text(body, encoding="utf-8")
                    os.chmod(config_path, 0o600)
                    result = _run_bash(
                        f"""
                        set -euo pipefail
                        . {shlex.quote(str(CONFIG_LIB))}
                        mim_load_config {shlex.quote(str(config_path))}
                        """
                    )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertNotIn(redacted_value, result.stderr)

    def test_private_file_provenance_and_run_url_guards_are_enforced_without_echoing_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "config.env"
            protected_path = temp_path / "protected-projects.exact"
            config_path.write_text(
                "\n".join(
                    (
                        f"MIM_OPERATOR_EMAIL={SYNTHETIC_OPERATOR}",
                        f"MIM_PROJECT_ID={SYNTHETIC_PROJECT}",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            protected_path.write_text("other-prod-654321\n", encoding="utf-8")
            os.chmod(config_path, 0o600)
            os.chmod(protected_path, 0o600)

            valid_result = _run_bash(
                f"""
                set -euo pipefail
                . {shlex.quote(str(CONFIG_LIB))}
                mim_load_config {shlex.quote(str(config_path))}
                mim_assert_project_not_protected {shlex.quote(SYNTHETIC_PROJECT)} {shlex.quote(str(protected_path))}
                """
            )
            self.assertEqual(valid_result.returncode, 0, valid_result.stderr)

            os.chmod(config_path, 0o644)
            mode_result = _run_bash(
                f"""
                set -euo pipefail
                . {shlex.quote(str(CONFIG_LIB))}
                mim_load_config {shlex.quote(str(config_path))}
                """
            )
            self.assertNotEqual(mode_result.returncode, 0)
            self.assertIn("Config file must use mode 0600", mode_result.stderr)
            self.assertNotIn(str(config_path), mode_result.stderr)

            os.chmod(config_path, 0o600)
            unreadable_path = temp_path / "unreadable.env"
            unreadable_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
            os.chmod(unreadable_path, 0o000)
            unreadable_result = _run_bash(
                f"""
                set -euo pipefail
                . {shlex.quote(str(CONFIG_LIB))}
                mim_load_config {shlex.quote(str(unreadable_path))}
                """
            )
            self.assertNotEqual(unreadable_result.returncode, 0)
            self.assertIn("Config file is missing or unreadable", unreadable_result.stderr)
            self.assertNotIn(str(unreadable_path), unreadable_result.stderr)
            os.chmod(unreadable_path, 0o600)

            symlink_path = temp_path / "config-link.env"
            symlink_path.symlink_to(config_path)
            symlink_result = _run_bash(
                f"""
                set -euo pipefail
                . {shlex.quote(str(CONFIG_LIB))}
                mim_load_config {shlex.quote(str(symlink_path))}
                """
            )
            self.assertNotEqual(symlink_result.returncode, 0)
            self.assertIn("Config file must not be a symlink", symlink_result.stderr)
            self.assertNotIn(str(symlink_path), symlink_result.stderr)

            protected_mode_path = temp_path / "protected-mode.exact"
            protected_mode_path.write_text("other-prod-654321\n", encoding="utf-8")
            os.chmod(protected_mode_path, 0o644)
            protected_mode_result = _run_bash(
                f"""
                set -euo pipefail
                . {shlex.quote(str(CONFIG_LIB))}
                mim_assert_project_not_protected {shlex.quote(SYNTHETIC_PROJECT)} {shlex.quote(str(protected_mode_path))}
                """
            )
            self.assertNotEqual(protected_mode_result.returncode, 0)
            self.assertIn("Protected project file must use mode 0600", protected_mode_result.stderr)
            self.assertNotIn(str(protected_mode_path), protected_mode_result.stderr)

        safe_urls = (
            _run_service_url(),
            _run_service_url(suffix="/"),
            _run_service_url("a.b", regional=True),
        )
        unsafe_urls = (
            _run_service_url(scheme="http://"),
            _run_service_url("Synthetic-bootstrap-12345"),
            _join("https://user:pass@", _run_service_url().removeprefix("https://")),
            _run_service_url(suffix=":443"),
            _run_service_url(suffix="/path"),
            _run_service_url(suffix="?query=value"),
            _run_service_url(suffix="#fragment"),
            _run_service_url(suffix="/?query=value"),
            _run_service_url(suffix="/#fragment"),
            _join("https://", "run", ".app"),
            _join("https://", ".", "run", ".app"),
            _run_service_url("bad.", suffix="/").removesuffix("/"),
            _run_service_url("-bad"),
            _run_service_url("bad-"),
            _run_service_url("a" * 64, regional=True),
            _join("https://", ".".join(["abcde"] * 42), ".run", ".app"),
            _run_service_url("bad", regional=True, suffix="//"),
        )
        for url in safe_urls:
            with self.subTest(url=url):
                result = _run_bash(
                    f"""
                    set -euo pipefail
                    . {shlex.quote(str(CONFIG_LIB))}
                    mim_is_safe_run_service_url {shlex.quote(url)}
                    """
                )
                self.assertEqual(result.returncode, 0, url)
        for url in unsafe_urls:
            with self.subTest(url=url):
                result = _run_bash(
                    f"""
                    set -euo pipefail
                    . {shlex.quote(str(CONFIG_LIB))}
                    mim_is_safe_run_service_url {shlex.quote(url)}
                    """
                )
                self.assertNotEqual(result.returncode, 0, url)

    def test_snapshot_helper_enforces_descriptor_based_private_file_copy_contract(self):
        helper_source = _read(SNAPSHOT_HELPER)
        self.assertIn("O_NOFOLLOW", helper_source)
        self.assertIn("os.fstat", helper_source)
        self.assertIn("os.read", helper_source)
        self.assertNotIn("open(", helper_source.replace("os.open(", ""))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            src = temp_path / "config.env"
            dst = temp_path / "snapshot.env"
            src.write_text(
                "\n".join(
                    (
                        f"MIM_OPERATOR_EMAIL={SYNTHETIC_OPERATOR}",
                        f"MIM_PROJECT_ID={SYNTHETIC_PROJECT}",
                        f"MIM_ORGANIZATION_ID={SYNTHETIC_ORG}",
                        f"MIM_BILLING_ACCOUNT_ID={SYNTHETIC_BILLING}",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(src, 0o600)

            success = subprocess.run(
                [
                    "python3",
                    str(SNAPSHOT_HELPER),
                    "--snapshot",
                    "Config file",
                    str(src),
                    str(dst),
                    "4096",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(dst.read_text(encoding="utf-8"), src.read_text(encoding="utf-8"))
            self.assertEqual(dst.stat().st_mode & 0o777, 0o600)
            self.assertEqual(success.stderr, "")

            existing_dst = temp_path / "existing.env"
            existing_dst.write_text("existing\n", encoding="utf-8")
            os.chmod(existing_dst, 0o600)
            existing_result = subprocess.run(
                [
                    "python3",
                    str(SNAPSHOT_HELPER),
                    "--snapshot",
                    "Config file",
                    str(src),
                    str(existing_dst),
                    "4096",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(existing_result.returncode, 0)
            self.assertIn("Config file snapshot destination already exists", existing_result.stderr)
            self.assertNotIn(str(existing_dst), existing_result.stderr)

            os.chmod(src, 0o644)
            mode_result = subprocess.run(
                [
                    "python3",
                    str(SNAPSHOT_HELPER),
                    "--snapshot",
                    "Config file",
                    str(src),
                    str(temp_path / "mode.env"),
                    "4096",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(mode_result.returncode, 0)
            self.assertIn("Config file must use mode 0600", mode_result.stderr)
            self.assertNotIn(str(src), mode_result.stderr)
            os.chmod(src, 0o600)

            symlink_src = temp_path / "config-link.env"
            symlink_src.symlink_to(src)
            symlink_result = subprocess.run(
                [
                    "python3",
                    str(SNAPSHOT_HELPER),
                    "--snapshot",
                    "Config file",
                    str(symlink_src),
                    str(temp_path / "symlink.env"),
                    "4096",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(symlink_result.returncode, 0)
            self.assertIn("Config file must not be a symlink", symlink_result.stderr)
            self.assertNotIn(str(symlink_src), symlink_result.stderr)

            oversized_src = temp_path / "oversized.env"
            oversized_src.write_text("x" * 32, encoding="utf-8")
            os.chmod(oversized_src, 0o600)
            oversized_result = subprocess.run(
                [
                    "python3",
                    str(SNAPSHOT_HELPER),
                    "--snapshot",
                    "Config file",
                    str(oversized_src),
                    str(temp_path / "oversized-copy.env"),
                    "8",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(oversized_result.returncode, 0)
            self.assertIn("Config file exceeds the maximum allowed size", oversized_result.stderr)
            self.assertNotIn(str(oversized_src), oversized_result.stderr)

            directory_result = subprocess.run(
                [
                    "python3",
                    str(SNAPSHOT_HELPER),
                    "--snapshot",
                    "Config file",
                    str(temp_path),
                    str(temp_path / "dir-copy.env"),
                    "4096",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(directory_result.returncode, 0)
            self.assertIn("Config file must be a regular file", directory_result.stderr)
            self.assertNotIn(str(temp_path), directory_result.stderr)

    def test_protected_project_boundary_is_data_only_and_fail_closed(self):
        cases = (
            (
                "accepts_other_project",
                "other-prod-654321\n",
                0,
                "",
            ),
            (
                "rejects_selected_project",
                f"{SYNTHETIC_PROJECT}\n",
                1,
                "Selected project is protected",
            ),
            (
                "rejects_duplicate_entries",
                "other-prod-654321\nother-prod-654321\n",
                1,
                "Protected project file entry is duplicated",
            ),
            (
                "rejects_invalid_entries",
                "invalid project value\n",
                1,
                "Protected project file entry is invalid",
            ),
            (
                "rejects_empty_file",
                "",
                1,
                "Protected project file must contain at least one project",
            ),
        )

        for case_name, protected_body, expected_code, expected_error in cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    protected_path = Path(temp_dir) / "protected-projects.exact"
                    protected_path.write_text(protected_body, encoding="utf-8")
                    os.chmod(protected_path, 0o600)
                    result = _run_bash(
                        f"""
                        set -euo pipefail
                        . {shlex.quote(str(CONFIG_LIB))}
                        mim_assert_project_not_protected \
                          {shlex.quote(SYNTHETIC_PROJECT)} \
                          {shlex.quote(str(protected_path))}
                        """
                    )

                self.assertEqual(result.returncode, expected_code, result.stderr)
                if expected_error:
                    self.assertIn(expected_error, result.stderr)
                self.assertNotIn(SYNTHETIC_PROJECT, result.stderr)

        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing.exact"
            result = _run_bash(
                f"""
                set -euo pipefail
                . {shlex.quote(str(CONFIG_LIB))}
                mim_assert_project_not_protected \
                  {shlex.quote(SYNTHETIC_PROJECT)} \
                  {shlex.quote(str(missing_path))}
                """
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Protected project file is missing or unreadable", result.stderr)

    def test_tracked_boundary_uses_external_operator_files_and_contains_no_end_user_cloud_credential_guidance(self):
        ignored = {
            line.strip()
            for line in _read(GITIGNORE).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("config.env", ignored)
        self.assertIn("protected-projects.exact", ignored)

        preflight_text = _read(PREFLIGHT)
        apply_text = _read(APPLY)
        self.assertIn("MIM_PROTECTED_PROJECTS_FILE", preflight_text)
        self.assertIn("protected-projects.exact", preflight_text)
        self.assertNotIn('case "$MIM_PROJECT_ID"', preflight_text)
        self.assertNotIn("EXPECTED_", preflight_text)
        self.assertNotIn("EXPECTED_", apply_text)

        combined = "\n".join(
            _read(path).lower()
            for path in (CONFIG_EXAMPLE, CONFIG_LIB, PREFLIGHT, APPLY, TEST_PREFLIGHT, TEST_APPLY)
        )
        for phrase in FORBIDDEN_GUIDANCE:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, combined)

        for text in (_read(TEST_PREFLIGHT), _read(TEST_APPLY)):
            self.assertIn(SYNTHETIC_OPERATOR, text)
            self.assertIn(SYNTHETIC_PROJECT, text)
            self.assertIn(SYNTHETIC_ORG, text)
            self.assertIn(SYNTHETIC_BILLING, text)


if __name__ == "__main__":
    unittest.main()
