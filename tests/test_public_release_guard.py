import os
import json
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from types import SimpleNamespace
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = (
    REPO_ROOT
    / "plugins"
    / "madup-infra-manager"
    / "infra"
    / "release"
    / "public_release_guard.py"
)
GUARD_MODULE_NAME = "mim_public_release_guard_test_module"
TEST_TIMEOUT_SECONDS = 15
PUBLIC_FIXTURE_SOURCES = (
    REPO_ROOT
    / "plugins"
    / "madup-infra-manager"
    / "infra"
    / "domain"
    / "test_apply_cloud_run.sh",
    REPO_ROOT / "tests" / "test_mim_public_boundary.py",
    REPO_ROOT / "tests" / "test_public_release_guard.py",
)


def _join(*parts: str) -> str:
    return "".join(parts)


def _secret_token(prefix: str, size: int) -> str:
    return _join(prefix, "A" * size)


SERVICE_ACCOUNT_TYPE = _join("service", "_", "account")
PRIVATE_KEY_JSON_KEY = _join("private", "_", "key")
PRIVATE_KEY_ID_JSON_KEY = _join("private", "_", "key", "_id")
CLIENT_EMAIL_JSON_KEY = _join("client", "_", "email")
PRIVATE_KEY_TEXT = _join(
    "-----BEGIN ",
    "PRIVATE KEY-----\n",
    "MIIE",
    "A" * 24,
    "\n-----END PRIVATE KEY-----\n",
)
SHORT_PRIVATE_KEY_TEXT = _join(
    "-----BEGIN ",
    "PRIVATE KEY-----\n",
    "MIIE",
    "A" * 8,
    "\n-----END PRIVATE KEY-----\n",
)


def _service_account_fields(private_key_text: str) -> dict[str, str]:
    return {
        "type": SERVICE_ACCOUNT_TYPE,
        PRIVATE_KEY_ID_JSON_KEY: "1" * 40,
        PRIVATE_KEY_JSON_KEY: private_key_text,
        CLIENT_EMAIL_JSON_KEY: "robot@example.test",
    }


def _service_account_json() -> str:
    payload = {
        **_service_account_fields(PRIVATE_KEY_TEXT),
        "project_id": "synthetic-project",
    }
    return json.dumps(payload, indent=2)


def _run_app_origin() -> str:
    return _join("https://", "svc-", "alpha123", "-", "uc", ".a.run.app")


def _regional_run_app_origin() -> str:
    return _join("https://", "svc-", "alpha123", "-", "uc", ".run.app")


def _quoted(text: str) -> str:
    return shlex.quote(text)


def _json_field(key: str, value: str) -> str:
    return json.dumps({key: value}, separators=(",", ":"))[1:-1]


def _load_guard_module():
    spec = importlib.util.spec_from_file_location(GUARD_MODULE_NAME, GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[GUARD_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _run_subprocess(*args, **kwargs):
    kwargs.setdefault("timeout", TEST_TIMEOUT_SECONDS)
    kwargs.setdefault("check", False)
    return subprocess.run(*args, **kwargs)


class GitRepo:
    def __init__(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")

    def cleanup(self) -> None:
        self._temp_dir.cleanup()

    def git(self, *args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = _run_subprocess(
            ["git", *args],
            cwd=self.root,
            text=True,
            input=input_text,
            capture_output=True,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")
        return result

    def write_text(self, relative_path: str, content: str) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def write_bytes(self, relative_path: str, content: bytes) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def commit_all(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def stage(self, relative_path: str) -> None:
        self.git("add", relative_path)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

class GuardHarness:
    def __init__(self, repo: GitRepo):
        self.repo = repo

    def denylist(self, *values: str, mode: int = 0o600, symlink_to: str | None = None) -> Path:
        path = self.repo.root / "denylist.exact"
        if path.exists() or path.is_symlink():
            path.unlink()
        if symlink_to is not None:
            path.symlink_to(symlink_to)
            return path
        body = "# comment\n\n" + "\n".join(values) + "\n"
        path.write_text(body, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def run(
        self,
        *args: str,
        stdin: str = "",
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["python3", str(GUARD), *args]
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return _run_subprocess(
            command,
            cwd=self.repo.root,
            text=True,
            input=stdin,
            capture_output=True,
            env=merged_env,
        )


class TestPublicReleaseGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = GitRepo()
        self.harness = GuardHarness(self.repo)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def _assert_rendered_fields(self, stderr: str, *, has_commit: bool) -> None:
        line = stderr.strip().splitlines()[0]
        fields = line.split(" ")
        expected_length = 4 if has_commit else 3
        self.assertEqual(len(fields), expected_length, line)
        self.assertTrue(fields[0].startswith("rule="), line)
        self.assertTrue(fields[1].startswith("scope="), line)
        self.assertTrue(fields[2].startswith("path="), line)
        if has_commit:
            self.assertTrue(fields[3].startswith("commit="), line)
        self.assertNotIn("fp=", line)

    def test_display_path_escapes_each_unsafe_codepoint_once(self):
        module = _load_guard_module()
        rendered = module._display_path("tab\t newline\n esc\x1b snow\u2603")
        self.assertEqual(
            rendered,
            r"tab\x09\x20newline\x0a\x20esc\x1b\x20snow\u2603",
        )
        self.assertNotIn(r"\\x09", rendered)

    def test_open_regular_bytes_requests_nofollow_and_nonblock_when_available(self):
        module = _load_guard_module()
        captured: dict[str, int] = {}
        fake_stat = SimpleNamespace(st_mode=stat.S_IFREG | 0o600)
        original_open = module.os.open
        original_fstat = module.os.fstat
        original_read = module.os.read
        original_close = module.os.close
        try:
            def fake_open(path, flags):
                captured["flags"] = flags
                return 99

            def fake_fstat(fd):
                self.assertEqual(fd, 99)
                return fake_stat

            read_chunks = [b"ok", b""]

            def fake_read(fd, _size):
                self.assertEqual(fd, 99)
                return read_chunks.pop(0)

            def fake_close(fd):
                self.assertEqual(fd, 99)

            module.os.open = fake_open
            module.os.fstat = fake_fstat
            module.os.read = fake_read
            module.os.close = fake_close

            data, metadata = module._open_regular_bytes(
                Path("ignored"),
                error_code=module.EXIT_GIT,
                label="ignored",
                limit=8,
            )
        finally:
            module.os.open = original_open
            module.os.fstat = original_fstat
            module.os.read = original_read
            module.os.close = original_close

        self.assertEqual(data, b"ok")
        self.assertIs(metadata, fake_stat)
        self.assertTrue(captured["flags"] & module.os.O_RDONLY == module.os.O_RDONLY)
        if hasattr(module.os, "O_NOFOLLOW"):
            self.assertTrue(captured["flags"] & module.os.O_NOFOLLOW)
        if hasattr(module.os, "O_NONBLOCK"):
            self.assertTrue(captured["flags"] & module.os.O_NONBLOCK)

    def test_diff_commands_disable_ext_diff_and_textconv(self):
        module = _load_guard_module()
        original_run_git = module._run_git
        seen: list[tuple[str, ...]] = []
        try:
            def fake_run_git(_repo_root, *args, check=True):
                seen.append(tuple(args))
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            module._run_git = fake_run_git
            module._parse_name_status_between(REPO_ROOT, "parent", "commit")
            module._per_change_patch(
                REPO_ROOT,
                "parent",
                "commit",
                module.Change(status="M", path="tracked.txt"),
            )
        finally:
            module._run_git = original_run_git

        self.assertGreaterEqual(len(seen), 2)
        for args in seen:
            self.assertIn("--no-ext-diff", args)
            self.assertIn("--no-textconv", args)

    def test_public_fixture_sources_do_not_embed_publishable_secret_signatures(self):
        module = _load_guard_module()
        scanner = module.Scanner(REPO_ROOT, [])
        blocked_rules = {"generated-run-app-origin", "service-account-json"}

        for path in PUBLIC_FIXTURE_SOURCES:
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                matches = {
                    match.rule_id
                    for match in scanner.scan_bytes(path.read_bytes())
                    if match.rule_id in blocked_rules
                }
                self.assertEqual(matches, set())

    def test_local_verify_blocks_exact_match_in_worktree(self):
        denied = _join("tenant-", "project-", "alpha")
        self.harness.denylist(denied)
        self.repo.write_text("tracked.txt", "safe\n")
        self.repo.commit_all("baseline")
        self.repo.write_text("tracked.txt", f"safe\n{denied}\n")

        result = self.harness.run(
            "verify",
            "--local",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(self.repo.root / "denylist.exact")},
        )

        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertIn("scope=local", result.stderr)
        self.assertIn("rule=exact-denylist", result.stderr)
        self.assertIn("path=tracked.txt", result.stderr)
        self.assertNotIn(denied, result.stderr)
        self._assert_rendered_fields(result.stderr, has_commit=False)

    def test_local_verify_blocks_exact_match_only_in_index(self):
        denied = _join("billing-", "acct-", "secret")
        self.harness.denylist(denied)
        self.repo.write_text("tracked.txt", "clean\n")
        self.repo.commit_all("baseline")
        self.repo.write_text("tracked.txt", f"{denied}\n")
        self.repo.stage("tracked.txt")
        self.repo.write_text("tracked.txt", "clean again\n")

        result = self.harness.run(
            "verify",
            "--local",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(self.repo.root / "denylist.exact")},
        )

        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertIn("scope=index", result.stderr)
        self.assertNotIn(denied, result.stderr)

    def test_local_verify_allows_missing_default_denylist_for_generic_only_ci_path(self):
        self.repo.write_text("tracked.txt", "normal@example.com\n1234567890\nmim.madup.app\n")
        self.repo.commit_all("baseline")
        self.repo.write_text("tracked.txt", "normal@example.com\n1234567890\nmadupmarketing\nmadup.com\n")

        result = self.harness.run("verify", "--local")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_local_verify_fails_when_explicit_denylist_path_is_missing(self):
        self.repo.write_text("tracked.txt", "safe\n")
        self.repo.commit_all("baseline")
        self.repo.write_text("tracked.txt", "still safe\n")

        result = self.harness.run(
            "verify",
            "--local",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(self.repo.root / "missing.exact")},
        )

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("denylist", result.stderr.lower())

    def test_local_verify_rejects_comment_only_exact_denylist_when_flagged_strict(self):
        self.repo.write_text("tracked.txt", "safe\n")
        self.repo.commit_all("baseline")
        self.repo.write_text("tracked.txt", "still safe\n")
        self.harness.denylist()

        result = self.harness.run(
            "verify",
            "--local",
            "--require-exact-values",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(self.repo.root / "denylist.exact")},
        )

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("exact denylist", result.stderr.lower())
        self.assertIn("non-comment", result.stderr.lower())

    def test_local_verify_allows_comment_only_exact_denylist_without_strict_flag(self):
        self.repo.write_text("tracked.txt", "safe\n")
        self.repo.commit_all("baseline")
        self.repo.write_text("tracked.txt", "still safe\n")
        self.harness.denylist()

        result = self.harness.run(
            "verify",
            "--local",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(self.repo.root / "denylist.exact")},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_range_verify_blocks_older_commit_and_deletion_line_even_when_head_is_clean(self):
        denied = _join("operator@", "private.", "test")
        denylist = self.harness.denylist(denied)
        self.repo.write_text("tracked.txt", "safe\n")
        baseline = self.repo.commit_all("baseline")
        self.repo.write_text("tracked.txt", f"{denied}\n")
        self.repo.commit_all("introduce")
        self.repo.write_text("tracked.txt", "safe again\n")
        self.repo.commit_all("remove")

        result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn("scope=outbound-blob", result.stderr)
        self.assertIn("scope=outbound-diff", result.stderr)
        self.assertNotIn(denied, result.stderr)

    def test_range_verify_detects_add_modify_rename_copy_delete_and_binary_blob(self):
        deny_value = _join("org-", "999", "-", "secret")
        denylist = self.harness.denylist(deny_value)
        self.repo.write_text("notes.txt", "baseline\n")
        baseline = self.repo.commit_all("baseline")
        self.repo.write_text("secrets.txt", f"{deny_value}\n")
        self.repo.commit_all("add secret")
        self.repo.write_text("secrets.txt", f"{deny_value}\nsecond line\n")
        self.repo.commit_all("modify secret")
        self.repo.git("mv", "secrets.txt", "renamed.txt")
        self.repo.commit_all("rename secret")
        copied_content = (self.repo.root / "renamed.txt").read_text(encoding="utf-8")
        self.repo.write_text("copied.txt", copied_content)
        self.repo.stage("copied.txt")
        self.repo.commit_all("copy secret")
        binary = b"\x89PNG\r\n\x1a\n" + deny_value.encode("utf-8") + b"\x00tail"
        self.repo.write_bytes("artifact.bin", binary)
        self.repo.commit_all("binary secret")
        self.repo.git("rm", "copied.txt")
        self.repo.commit_all("delete copied")

        result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn("scope=outbound-blob", result.stderr)
        self.assertIn("path=artifact.bin", result.stderr)
        self.assertNotIn(deny_value, result.stderr)

    def test_range_verify_detects_generic_private_material_without_exact_denylist_literal(self):
        denylist = self.harness.denylist(_join("unused-", "exact"))
        self.repo.write_text("tracked.txt", "safe\n")
        baseline = self.repo.commit_all("baseline")
        self.repo.write_text("creds.json", _service_account_json())
        self.repo.commit_all("service account")

        result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn("rule=service-account-json", result.stderr)
        self.assertNotIn("private_key", result.stderr)
        self._assert_rendered_fields(result.stderr, has_commit=True)

    def test_scanner_detects_service_account_json_in_any_key_order_and_near_misses_stay_clean(self):
        module = _load_guard_module()
        scanner = module.Scanner(REPO_ROOT, [])
        field_map = {
            "type": _json_field("type", SERVICE_ACCOUNT_TYPE),
            "private_key": _json_field(PRIVATE_KEY_JSON_KEY, SHORT_PRIVATE_KEY_TEXT),
            "client_email": _json_field(CLIENT_EMAIL_JSON_KEY, "robot@example.test"),
        }
        ordered_payloads = (
            ["type", "private_key", "client_email"],
            ["client_email", "type", "private_key"],
            ["private_key", "client_email", "type"],
        )
        for order in ordered_payloads:
            with self.subTest(order=order):
                body = "{" + ",".join(field_map[key] for key in order) + "}"
                matches = scanner.scan_bytes(body.encode("utf-8"))
                self.assertIn("service-account-json", {match.rule_id for match in matches})

        near_miss_orders = (
            ["type", "private_key"],
            ["type", "client_email"],
            ["private_key", "client_email"],
        )
        for order in near_miss_orders:
            with self.subTest(near_miss=order):
                body = "{" + ",".join(field_map[key] for key in order) + "}"
                matches = scanner.scan_bytes(body.encode("utf-8"))
                self.assertNotIn("service-account-json", {match.rule_id for match in matches})

    def test_scanner_detects_both_run_app_families_and_keeps_placeholders_clean(self):
        module = _load_guard_module()
        scanner = module.Scanner(REPO_ROOT, [])
        positives = (_run_app_origin(), _regional_run_app_origin())
        negatives = (
            "https://SERVICE-PLACEHOLDER.run.app",
            "https://*.run.app",
            "https://MIM.MADUP.APP",
            "mim.madup.app",
            "https://madup.com",
        )

        for value in positives:
            with self.subTest(positive=value):
                matches = scanner.scan_bytes(value.encode("utf-8"))
                self.assertIn("generated-run-app-origin", {match.rule_id for match in matches})

        for value in negatives:
            with self.subTest(negative=value):
                matches = scanner.scan_bytes(value.encode("utf-8"))
                self.assertNotIn("generated-run-app-origin", {match.rule_id for match in matches})

    def test_range_verify_keeps_stable_public_values_and_normal_emails_clean(self):
        denylist = self.harness.denylist(_join("unused-", "exact"))
        self.repo.write_text(
            "doc.txt",
            "mim.madup.app\nmadupmarketing\nmadup.com\nnormal@example.com\n1234567890\n",
        )
        baseline = self.repo.commit_all("baseline")
        self.repo.write_text(
            "doc.txt",
            "mim.madup.app\nmadupmarketing\nmadup.com\nmarketing@example.com\n0987654321\n",
        )
        self.repo.commit_all("docs")

        result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_range_verify_requires_strict_denylist_file_properties(self):
        cases = (
            ("missing", None, 3),
            ("wrong_mode", 0o644, 3),
        )
        self.repo.write_text("tracked.txt", "safe\n")
        baseline = self.repo.commit_all("baseline")
        self.repo.write_text("tracked.txt", "still safe\n")
        self.repo.commit_all("change")

        for case_name, mode, expected_code in cases:
            with self.subTest(case=case_name):
                if mode is None:
                    denylist_path = self.repo.root / f"{case_name}.exact"
                else:
                    denylist_path = self.harness.denylist(_join(case_name, "-value"), mode=mode)
                result = self.harness.run(
                    "verify",
                    "--range",
                    f"{baseline}..HEAD",
                    env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist_path)},
                )
                self.assertEqual(result.returncode, expected_code, result.stderr)

        real_target = self.repo.root / "real-denylist.exact"
        real_target.write_text("x\n", encoding="utf-8")
        os.chmod(real_target, 0o600)
        symlink_path = self.harness.denylist("x", symlink_to=str(real_target))
        result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(symlink_path)},
        )
        self.assertEqual(result.returncode, 3, result.stderr)

    def test_range_verify_supports_clean_and_secret_merge_commits(self):
        denylist = self.harness.denylist(_join("merge-", "secret-", "value"))
        self.repo.write_text("app.txt", "base\n")
        baseline = self.repo.commit_all("baseline")

        self.repo.git("checkout", "-b", "feature-clean")
        self.repo.write_text("feature.txt", "clean feature\n")
        self.repo.commit_all("feature clean")
        self.repo.git("checkout", "main")
        self.repo.write_text("main.txt", "clean main\n")
        self.repo.commit_all("main clean")
        self.repo.git("merge", "--no-ff", "--no-edit", "feature-clean")

        clean_result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )
        self.assertEqual(clean_result.returncode, 0, clean_result.stderr)

        self.repo.git("checkout", "-b", "feature-dirty", "main")
        self.repo.write_text("app.txt", "feature branch line\n")
        self.repo.commit_all("feature dirty")
        self.repo.git("checkout", "main")
        self.repo.write_text("app.txt", "main branch line\n")
        self.repo.commit_all("main dirty")
        merge_attempt = self.repo.git("merge", "--no-ff", "feature-dirty", check=False)
        self.assertNotEqual(merge_attempt.returncode, 0)
        self.repo.write_text("app.txt", _join("merged line\n", "merge-secret-value\n"))
        self.repo.git("add", "app.txt")
        self.repo.git("commit", "-m", "merge resolution")

        dirty_result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )
        self.assertEqual(dirty_result.returncode, 11, dirty_result.stderr)
        self.assertIn("scope=outbound-blob", dirty_result.stderr)
        self.assertIn("scope=outbound-diff", dirty_result.stderr)
        self._assert_rendered_fields(dirty_result.stderr, has_commit=True)

    def test_range_verify_fails_closed_on_git_plumbing_error(self):
        denylist = self.harness.denylist(_join("unused-", "exact"))
        self.repo.write_text("tracked.txt", "safe\n")
        self.repo.commit_all("baseline")
        self.repo.write_text("tracked.txt", "change\n")
        self.repo.commit_all("change")

        result = self.harness.run(
            "verify",
            "--range",
            "missing..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 4, result.stderr)

    def test_verify_range_rejects_malformed_and_option_like_inputs(self):
        denylist = self.harness.denylist(_join("unused-", "exact"))
        self.repo.write_text("tracked.txt", "safe\n")
        baseline = self.repo.commit_all("baseline")
        self.repo.write_text("tracked.txt", "change\n")
        self.repo.commit_all("change")

        for range_expr in (
            f"{baseline}...HEAD",
            "..HEAD",
            f"{baseline}..",
            f"{baseline}..HEAD..main",
            "--all..HEAD",
            f"{baseline}..--all",
        ):
            with self.subTest(range_expr=range_expr):
                result = self.harness.run(
                    "verify",
                    "--range",
                    range_expr,
                    env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
                )
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_verify_range_rejects_missing_git_objects_after_shape_validation(self):
        denylist = self.harness.denylist(_join("unused-", "exact"))
        self.repo.write_text("tracked.txt", "safe\n")
        self.repo.commit_all("baseline")
        missing = "a" * len(self.repo.head())

        for range_expr in (f"{missing}..HEAD", f"HEAD..{missing}"):
            with self.subTest(range_expr=range_expr):
                result = self.harness.run(
                    "verify",
                    "--range",
                    range_expr,
                    env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
                )
                self.assertEqual(result.returncode, 4, result.stderr)

    def test_range_verify_suppresses_same_path_same_fingerprint_from_baseline_but_blocks_new_path(self):
        denied = _join("project-", "prod-", "alpha")
        denylist = self.harness.denylist(denied)
        self.repo.write_text("existing.txt", f"{denied}\n")
        baseline = self.repo.commit_all("baseline")
        self.repo.write_text("existing.txt", f"{denied}\nextra line\n")
        self.repo.commit_all("touch same path")

        clean_result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )
        self.assertEqual(clean_result.returncode, 0, clean_result.stderr)

        self.repo.write_text("new-path.txt", f"{denied}\n")
        self.repo.commit_all("copy denied")
        dirty_result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )
        self.assertEqual(dirty_result.returncode, 11, dirty_result.stderr)
        self.assertIn("path=new-path.txt", dirty_result.stderr)

    def test_range_verify_blocks_changed_fingerprint_on_same_path(self):
        denylist = self.harness.denylist(_join("project-", "prod-", "alpha"))
        self.repo.write_text("existing.txt", "project-prod-alpha\n")
        baseline = self.repo.commit_all("baseline")
        self.repo.write_text("existing.txt", "project-prod-beta\n")
        self.repo.commit_all("change denied")
        updated = self.repo.root / "denylist.exact"
        updated.write_text("# comment\nproject-prod-alpha\nproject-prod-beta\n", encoding="utf-8")
        os.chmod(updated, 0o600)

        result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(updated)},
        )

        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn("path=existing.txt", result.stderr)

    def test_range_verify_prefers_blob_exit_code_when_blob_and_diff_findings_both_exist(self):
        denied = _join("protected-", "project-", "x")
        denylist = self.harness.denylist(denied)
        self.repo.write_text("tracked.txt", "safe\n")
        baseline = self.repo.commit_all("baseline")
        self.repo.write_text("delete-me.txt", f"{denied}\n")
        self.repo.commit_all("add delete me")
        self.repo.git("rm", "delete-me.txt")
        self.repo.write_bytes("artifact.bin", denied.encode("utf-8") + b"\x00")
        self.repo.commit_all("delete and binary")

        result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn("scope=outbound-diff", result.stderr)
        self.assertIn("scope=outbound-blob", result.stderr)

    def test_local_verify_sanitizes_control_heavy_paths_without_missing_findings(self):
        denied = _join("tenant-", "project-", "alpha")
        denylist = self.harness.denylist(denied)
        weird_path = 'dir name/quo"te back\\slash \tline\nesc\x1b.txt'
        self.repo.write_text(weird_path, "safe\n")
        self.repo.commit_all("baseline")
        self.repo.write_text(weird_path, denied + "\n")

        result = self.harness.run(
            "verify",
            "--local",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertIn(r"path=dir\x20name/quo\x22te\x20back\x5cslash\x20\x09line\x0aesc\x1b.txt", result.stderr)
        self.assertNotIn(weird_path, result.stderr)

    def test_range_verify_handles_space_quote_and_control_paths(self):
        denied = _join("operator@", "private.", "test")
        denylist = self.harness.denylist(denied)
        weird_path = 'dir name/quo"te \tline\nesc\x1b.txt'
        self.repo.write_text("safe.txt", "safe\n")
        baseline = self.repo.commit_all("baseline")
        self.repo.write_text(weird_path, denied + "\n")
        self.repo.commit_all("weird path")

        result = self.harness.run(
            "verify",
            "--range",
            f"{baseline}..HEAD",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn(r"path=dir\x20name/quo\x22te\x20\x09line\x0aesc\x1b.txt", result.stderr)
        self.assertNotIn(weird_path, result.stderr)
        self.assertNotIn(denied, result.stderr)

    def test_local_verify_fails_closed_for_invalid_utf8_path(self):
        module = _load_guard_module()
        with self.assertRaises(module.GuardError) as raised:
            module._decode_z_paths(b"bad-\xff.txt\0")
        self.assertEqual(raised.exception.code, 4)
        self.assertIn("utf-8", str(raised.exception).lower())

    def test_local_verify_scans_symlink_target_text_not_private_target_contents(self):
        denylist = self.harness.denylist(_join("hidden-", "private-", "value"))
        self.repo.write_text("safe.txt", "safe\n")
        self.repo.commit_all("baseline")
        private_target = self.repo.root / "private-target.txt"
        private_target.write_text(_join("hidden-", "private-", "value") + "\n", encoding="utf-8")
        symlink_path = self.repo.root / "safe.txt"
        symlink_path.unlink()
        symlink_path.symlink_to("private-target.txt")

        result = self.harness.run(
            "verify",
            "--local",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(self.repo.root / "denylist.exact")},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_local_verify_fails_closed_for_fifo_without_hanging(self):
        denylist = self.harness.denylist(_join("unused-", "exact"))
        self.repo.write_text("stream.txt", "safe\n")
        self.repo.commit_all("baseline")
        fifo_path = self.repo.root / "stream.txt"
        fifo_path.unlink()
        os.mkfifo(fifo_path)

        result = self.harness.run(
            "verify",
            "--local",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("non-regular", result.stderr.lower())

    def test_pre_push_handles_existing_ref_new_ref_deletion_force_push_and_multiple_updates(self):
        remote_temp = tempfile.TemporaryDirectory()
        remote_path = Path(remote_temp.name) / "remote.git"
        _run_subprocess(
            ["git", "init", "--bare", str(remote_path)],
            text=True,
            capture_output=True,
        )

        seed = GitRepo()
        seed.git("remote", "add", "origin", str(remote_path))
        seed.write_text("tracked.txt", "base\n")
        seed.commit_all("baseline")
        seed.git("push", "-u", "origin", "main")

        clone_temp = tempfile.TemporaryDirectory()
        clone_path = Path(clone_temp.name) / "clone"
        _run_subprocess(
            ["git", "clone", str(remote_path), str(clone_path)],
            text=True,
            capture_output=True,
        )
        _run_subprocess(
            ["git", "config", "user.name", "Test User"],
            cwd=clone_path,
            text=True,
            capture_output=True,
        )
        _run_subprocess(
            ["git", "config", "user.email", "test@example.com"],
            cwd=clone_path,
            text=True,
            capture_output=True,
        )
        denylist = clone_path / "denylist.exact"
        denied = _join("quota-", "limit-", "secret")
        denylist.write_text(f"{denied}\n", encoding="utf-8")
        os.chmod(denylist, 0o600)

        def clone_git(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
            return _run_subprocess(
                ["git", *args],
                cwd=clone_path,
                text=True,
                input=input_text,
                capture_output=True,
            )

        remote_sha = clone_git("rev-parse", "refs/remotes/origin/main").stdout.strip()
        (clone_path / "tracked.txt").write_text("clean change\n", encoding="utf-8")
        clone_git("add", "tracked.txt")
        clone_git("commit", "-m", "clean change")
        local_sha = clone_git("rev-parse", "HEAD").stdout.strip()

        clean_result = _run_subprocess(
            ["python3", str(GUARD), "pre-push", "origin", str(remote_path)],
            cwd=clone_path,
            text=True,
            input=f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n",
            capture_output=True,
            env={**os.environ, "MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )
        self.assertEqual(clean_result.returncode, 0, clean_result.stderr)

        clone_git("checkout", "-b", "feature/test")
        (clone_path / "feature.txt").write_text("safe branch\n", encoding="utf-8")
        clone_git("add", "feature.txt")
        clone_git("commit", "-m", "feature")
        feature_sha = clone_git("rev-parse", "HEAD").stdout.strip()
        new_ref_result = _run_subprocess(
            ["python3", str(GUARD), "pre-push", "origin", str(remote_path)],
            cwd=clone_path,
            text=True,
            input=f"refs/heads/feature/test {feature_sha} refs/heads/feature/test {'0' * 40}\n",
            capture_output=True,
            env={**os.environ, "MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )
        self.assertEqual(new_ref_result.returncode, 0, new_ref_result.stderr)

        malformed_result = _run_subprocess(
            ["python3", str(GUARD), "pre-push", "origin", str(remote_path)],
            cwd=clone_path,
            text=True,
            input="broken line\n",
            capture_output=True,
            env={**os.environ, "MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )
        self.assertEqual(malformed_result.returncode, 2, malformed_result.stderr)

        clone_git("checkout", "--orphan", "rewrite")
        for entry in clone_path.iterdir():
            if entry.name == ".git":
                continue
            if entry.is_dir():
                _run_subprocess(["rm", "-rf", str(entry)])
            else:
                entry.unlink()
        (clone_path / "rewritten.txt").write_text(f"{denied}\n", encoding="utf-8")
        denylist.write_text(f"{denied}\n", encoding="utf-8")
        os.chmod(denylist, 0o600)
        clone_git("add", "rewritten.txt")
        clone_git("commit", "-m", "rewrite history")
        rewrite_sha = clone_git("rev-parse", "HEAD").stdout.strip()
        force_result = _run_subprocess(
            ["python3", str(GUARD), "pre-push", "origin", str(remote_path)],
            cwd=clone_path,
            text=True,
            input=f"refs/heads/main {rewrite_sha} refs/heads/main {remote_sha}\n",
            capture_output=True,
            env={**os.environ, "MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )
        self.assertEqual(force_result.returncode, 11, force_result.stderr)

        multi_result = _run_subprocess(
            ["python3", str(GUARD), "pre-push", "origin", str(remote_path)],
            cwd=clone_path,
            text=True,
            input=(
                f"refs/heads/deleted {'0' * 40} refs/heads/deleted {feature_sha}\n"
                f"refs/heads/main {rewrite_sha} refs/heads/main {remote_sha}\n"
            ),
            capture_output=True,
            env={**os.environ, "MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )
        self.assertEqual(multi_result.returncode, 11, multi_result.stderr)

        remote_temp.cleanup()
        clone_temp.cleanup()
        seed.cleanup()

    def test_pre_push_new_ref_fails_closed_without_remote_namespace(self):
        denylist = self.harness.denylist(_join("unused-", "exact"))
        self.repo.write_text("tracked.txt", "safe\n")
        self.repo.commit_all("baseline")
        local_sha = self.repo.head()

        result = self.harness.run(
            "pre-push",
            "origin",
            "https://example.test/repo.git",
            stdin=f"refs/heads/main {local_sha} refs/heads/main {'0' * 40}\n",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 4, result.stderr)

    def test_pre_push_rejects_malformed_and_missing_object_ids(self):
        denylist = self.harness.denylist(_join("unused-", "exact"))
        self.repo.write_text("tracked.txt", "safe\n")
        self.repo.commit_all("baseline")
        local_sha = self.repo.head()
        oid_len = len(local_sha)
        zero_oid = "0" * oid_len
        short_oid = local_sha[:-1]
        non_hex = "g" * oid_len
        missing = "a" * oid_len

        cases = (
            ("--all", zero_oid),
            (short_oid, zero_oid),
            (non_hex, zero_oid),
            (missing, zero_oid),
            (local_sha, missing),
        )

        for bad_local, bad_remote in cases:
            with self.subTest(local=bad_local, remote=bad_remote):
                result = self.harness.run(
                    "pre-push",
                    "origin",
                    "https://example.test/repo.git",
                    stdin=f"refs/heads/main {bad_local} refs/heads/main {bad_remote}\n",
                    env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
                )
                self.assertEqual(result.returncode, 4, result.stderr)

    def test_pre_push_rejects_invalid_ref_line_shape_as_usage(self):
        denylist = self.harness.denylist(_join("unused-", "exact"))
        self.repo.write_text("tracked.txt", "safe\n")
        self.repo.commit_all("baseline")
        local_sha = self.repo.head()

        cases = (
            "too few fields\n",
            f"-bad {local_sha} refs/heads/main {'0' * len(local_sha)}\n",
        )
        for stdin_text in cases:
            with self.subTest(stdin_text=stdin_text.strip()):
                result = self.harness.run(
                    "pre-push",
                    "origin",
                    "https://example.test/repo.git",
                    stdin=stdin_text,
                    env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
                )
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_pre_push_uses_generic_run_app_rule_without_revealing_literal(self):
        remote_temp = tempfile.TemporaryDirectory()
        remote_path = Path(remote_temp.name) / "remote.git"
        _run_subprocess(["git", "init", "--bare", str(remote_path)], capture_output=True)
        self.repo.git("remote", "add", "origin", str(remote_path))
        self.repo.write_text("tracked.txt", "base\n")
        self.repo.commit_all("baseline")
        self.repo.git("push", "-u", "origin", "main")
        self.repo.write_text("tracked.txt", _run_app_origin() + "\n")
        self.repo.commit_all("dirty")
        remote_sha = self.repo.git("rev-parse", "refs/remotes/origin/main").stdout.strip()
        local_sha = self.repo.head()
        denylist = self.harness.denylist(_join("unused-", "exact"))

        result = self.harness.run(
            "pre-push",
            "origin",
            str(remote_path),
            stdin=f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n",
            env={"MIM_PUBLIC_RELEASE_DENYLIST_FILE": str(denylist)},
        )

        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn("rule=generated-run-app-origin", result.stderr)
        self.assertNotIn(_run_app_origin(), result.stderr)
        remote_temp.cleanup()

if __name__ == "__main__":
    unittest.main()
