import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "madup-infra-manager"
PLUGIN_MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
MCP_CONFIG = PLUGIN_ROOT / ".mcp.json"
SKILL_PATH = PLUGIN_ROOT / "skills" / "madup-infra-manager" / "SKILL.md"
EXAMPLES_PATH = PLUGIN_ROOT / "skills" / "madup-infra-manager" / "references" / "examples.md"
MARKETPLACE_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"
README_PATH = REPO_ROOT / "README.md"
DESIGN_PATH = REPO_ROOT / "docs" / "plans" / "2026-08-01-madup-infra-manager-design.md"
DOMAIN_IMPLEMENTATION_PATH = (
    REPO_ROOT / "docs" / "plans" / "2026-08-01-mim-domain-foundation-implementation.md"
)
CONTROL_PLANE_DESIGN_PATH = (
    REPO_ROOT / "docs" / "plans" / "2026-08-02-mim-control-plane-design.md"
)
CONTROL_PLANE_IMPLEMENTATION_PATH = (
    REPO_ROOT / "docs" / "plans" / "2026-08-02-mim-control-plane-implementation.md"
)

EXPECTED_PLUGIN_NAME = "madup-infra-manager"
EXPECTED_VERSION = "0.2.0"
EXPECTED_AUTHOR_NAME = "MADUP DCT"
EXPECTED_MCP_URL = "https://mim.madup.app/mcp"
EXPECTED_PUBLIC_ORIGIN = "https://mim.madup.app"
LEGACY_PUBLIC_HOST = "madupai.com"
EXPECTED_READ_ONLY_TOOLS = {
    "plan_deploy",
    "plan_schedule",
    "plan_secret_write",
    "plan_secret_attach",
    "get_operation",
    "list_workloads",
    "get_usage",
    "explain_failure",
}
EXPECTED_MUTATION_TOOLS = {
    "deploy_from_plan",
    "attach_secret_from_plan",
    "create_schedule_from_plan",
    "pause_schedule",
    "resume_schedule",
}
PUBLIC_OPERATOR_PLACEHOLDERS = {
    "MIM_OPERATOR_EMAIL",
    "MIM_PROJECT_ID",
    "MIM_ORGANIZATION_ID",
    "MIM_BILLING_ACCOUNT_ID",
}


def _load_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if match is None:
        raise AssertionError("SKILL.md must start with YAML frontmatter")

    frontmatter: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        if not raw_line.strip():
            continue
        key, sep, value = raw_line.partition(":")
        if not sep:
            raise AssertionError(f"Invalid frontmatter line: {raw_line!r}")
        frontmatter[key.strip()] = value.strip()
    return frontmatter


class TestMadupInfraManagerPluginPackaging(unittest.TestCase):
    def test_plugin_manifest_declares_identity_version_author_and_description(self):
        self.assertTrue(PLUGIN_MANIFEST.exists(), f"Missing plugin manifest: {PLUGIN_MANIFEST}")
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], EXPECTED_PLUGIN_NAME)
        self.assertEqual(manifest["version"], EXPECTED_VERSION)
        self.assertEqual(manifest["author"], marketplace["owner"])
        self.assertEqual(manifest["author"]["name"], EXPECTED_AUTHOR_NAME)
        self.assertTrue(manifest["author"]["email"].strip())
        self.assertIn("description", manifest)
        self.assertTrue(manifest["description"].strip())
        self.assertIn("MIM", manifest["description"])

    def test_marketplace_lists_matching_plugin_entry_with_same_version(self):
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        plugin_entry = next(
            (item for item in marketplace["plugins"] if item["name"] == EXPECTED_PLUGIN_NAME),
            None,
        )

        self.assertIsNotNone(plugin_entry, "Marketplace is missing madup-infra-manager")
        self.assertEqual(plugin_entry["source"], "./plugins/madup-infra-manager")
        self.assertEqual(plugin_entry["version"], EXPECTED_VERSION)
        self.assertTrue(plugin_entry["description"].strip())

        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(plugin_entry["version"], manifest["version"])

    def test_mcp_config_exposes_only_one_remote_http_server_without_static_secrets(self):
        self.assertTrue(MCP_CONFIG.exists(), f"Missing MCP config: {MCP_CONFIG}")
        config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))

        self.assertEqual(set(config.keys()), {"mcpServers"})
        self.assertEqual(set(config["mcpServers"].keys()), {EXPECTED_PLUGIN_NAME})

        server = config["mcpServers"][EXPECTED_PLUGIN_NAME]
        self.assertEqual(server["type"], "http")
        self.assertEqual(server["url"], EXPECTED_MCP_URL)
        self.assertNotIn("headers", server)
        self.assertNotIn("env", server)
        self.assertEqual(set(server.keys()), {"type", "url"})

    def test_skill_frontmatter_contains_only_name_and_description(self):
        self.assertTrue(SKILL_PATH.exists(), f"Missing SKILL.md: {SKILL_PATH}")
        frontmatter = _load_frontmatter(SKILL_PATH.read_text(encoding="utf-8"))

        self.assertEqual(set(frontmatter.keys()), {"name", "description"})
        self.assertEqual(frontmatter["name"], EXPECTED_PLUGIN_NAME)
        self.assertTrue(frontmatter["description"].startswith("Use when"))

    def test_readme_documents_install_command(self):
        readme = README_PATH.read_text(encoding="utf-8")
        self.assertIn("/plugin install madup-infra-manager@madup", readme)

    def test_readme_discloses_mim_remote_data_flow_and_retention_boundaries(self):
        readme = README_PATH.read_text(encoding="utf-8")

        required_phrases = (
            "https://mim.madup.app/mcp",
            "authenticated",
            "plan/status/usage/operation",
            "normalized operational metadata",
            "authenticated user",
            "repository/workload reference",
            "commit SHA",
            "normalized action/outcome/timing",
            "resource/quota/cost measurements",
            "raw Claude conversation text",
            "secret values",
            "raw request bodies",
            "auth headers",
            "cookies",
            "client IPs",
            "user agents",
        )
        forbidden_phrases = (
            "프롬프트 내용·작업 내용·파일명은 어떤 것도 수집하지 않는다.",
            "no 작업 내용",
        )

        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)

        for phrase in forbidden_phrases:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, readme)

    def test_active_public_docs_use_madup_app_contract_only(self):
        active_docs = {
            "root README": README_PATH.read_text(encoding="utf-8"),
            "plugin README": (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8"),
            "operations": (PLUGIN_ROOT / "docs" / "operations.md").read_text(encoding="utf-8"),
            "skill": SKILL_PATH.read_text(encoding="utf-8"),
            "examples": EXAMPLES_PATH.read_text(encoding="utf-8"),
        }

        for label, text in active_docs.items():
            with self.subTest(path=label):
                self.assertIn(EXPECTED_PUBLIC_ORIGIN, text)
                self.assertNotIn(LEGACY_PUBLIC_HOST, text)


class TestMadupInfraManagerGuidanceContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = SKILL_PATH.read_text(encoding="utf-8")
        cls.examples = EXAMPLES_PATH.read_text(encoding="utf-8")
        cls.readme = README_PATH.read_text(encoding="utf-8")
        cls.design = DESIGN_PATH.read_text(encoding="utf-8")
        cls.domain_implementation = DOMAIN_IMPLEMENTATION_PATH.read_text(encoding="utf-8")
        cls.control_plane_design = CONTROL_PLANE_DESIGN_PATH.read_text(encoding="utf-8")
        cls.control_plane_implementation = CONTROL_PLANE_IMPLEMENTATION_PATH.read_text(
            encoding="utf-8"
        )

    def test_skill_triggers_on_korean_deploy_schedule_status_usage_and_repair_requests(self):
        trigger_phrases = (
            "배포해줘",
            "정기적으로 돌려줘",
            "매시간 돌려줘",
            "상태",
            "사용량",
            "비용",
            "고쳐줘",
        )
        for phrase in trigger_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_skill_requires_plan_first_single_missing_question_and_confirmation_before_mutation(self):
        required_phrases = (
            "한 번에 하나의 짧은 질문만",
            "읽기 전용 계획 도구",
            "plan_deploy",
            "plan_schedule",
            "사용자 확인",
            "확인 전에는 변경",
            "작업 ID",
            "get_operation",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_skill_limits_surface_to_exact_planning_and_mutation_tool_allowlists(self):
        forbidden_terms = (
            "gcloud",
            "docker",
            "kubectl",
            "terraform",
            "bash",
            "zsh",
            "cloud credentials",
            "service account key",
            "AWS access key",
        )
        planning_line = next(
            (
                line
                for line in self.skill.splitlines()
                if line.startswith("- Exact planning/read-only tool allowlist:")
            ),
            None,
        )
        mutation_line = next(
            (
                line
                for line in self.skill.splitlines()
                if line.startswith("- Exact confirmed-mutation tool allowlist:")
            ),
            None,
        )
        self.assertIsNotNone(planning_line)
        self.assertIsNotNone(mutation_line)
        self.assertEqual(set(re.findall(r"`([^`]+)`", planning_line or "")), EXPECTED_READ_ONLY_TOOLS)
        self.assertEqual(set(re.findall(r"`([^`]+)`", mutation_line or "")), EXPECTED_MUTATION_TOOLS)
        self.assertIn("unexpected tool", self.skill)
        self.assertIn("security configuration mismatch", self.skill)
        self.assertIn("stop", self.skill)
        self.assertIn("secret handoff", self.skill)
        self.assertIn("raw secret", self.skill)

        for term in forbidden_terms:
            with self.subTest(term=term):
                self.assertNotIn(term, self.skill)

    def test_skill_describes_supported_workload_types_and_plan_summary_fields(self):
        required_phrases = (
            "Streamlit",
            "Next.js",
            "scheduled script",
            "repo",
            "SHA",
            "리소스",
            "quota",
            "estimate",
            "계획 요약",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_skill_forbids_treating_plugin_repo_as_application_source_and_limits_repo_scope(self):
        required_phrases = (
            "madup-dct/claude-plugins",
            "애플리케이션 소스가 아니다",
            "madupmarketing",
            "선택된 저장소만",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_skill_does_not_claim_the_remote_server_is_already_live(self):
        forbidden_phrases = (
            "지금 바로 배포된다",
            "이미 운영 중",
            "already live",
            "production is ready",
        )
        for phrase in forbidden_phrases:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.skill)

    def test_examples_cover_safe_conversations_and_refusals_without_credentials(self):
        required_phrases = (
            "배포 계획",
            "스케줄 계획",
            "상태 조회",
            "사용량/비용 조회",
            "실패 원인 설명",
            "거절",
            "범위 밖",
            "madup-dct/claude-plugins",
            "cloud credential",
        )
        forbidden_phrases = (
            "gcloud",
            "docker",
            "service account key",
            "Authorization:",
            "Bearer ",
        )

        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.examples)

        for phrase in forbidden_phrases:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.examples)

    def test_public_docs_explain_centralized_employee_login_without_operator_inputs(self):
        combined = "\n".join(
            (
                self.readme,
                self.skill,
                self.examples,
                self.design,
                self.control_plane_design,
            )
        )
        required_phrases = (
            "employees never enter",
            "GCP project",
            "organization",
            "billing",
            "Cloudflare",
            "operator-only configuration",
            "Cloudflare Access Managed OAuth",
            "Google Workspace",
            "MIM access group",
            "Claude stores and refreshes the OAuth token",
            "first grant",
            "later calls reuse",
            "shared API key",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_public_docs_limit_slack_oauth_to_optional_integration_and_secret_manager(self):
        combined = "\n".join(
            (
                self.readme,
                self.skill,
                self.examples,
                self.design,
                self.control_plane_design,
            )
        )
        required_phrases = (
            "Slack OAuth",
            "optional",
            "not the primary MIM login",
            "Secret Manager",
            "never pasted into Claude",
            "least scopes",
            "rotation",
            "revocation",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_public_docs_describe_offboarding_quarantine_and_session_latency(self):
        combined = "\n".join(
            (
                self.design,
                self.control_plane_design,
                self.control_plane_implementation,
            )
        )
        required_phrases = (
            "group removal",
            "new or renewed Access sessions",
            "periodic identity reconciliation",
            "quarantines schedules and access",
            "transfers or retires resources",
            "active-session",
            "latency test",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_public_docs_define_remote_mcp_oauth_poc_release_gate(self):
        combined = "\n".join((self.control_plane_design, self.control_plane_implementation))
        required_phrases = (
            "authorization server metadata discovery",
            "RFC 8707",
            "resource parameter",
            "PKCE S256",
            "dynamic client registration",
            "manual client ID fallback",
            "callback compatibility",
            "token refresh",
            "group-removal/session expiry latency",
            "POC gate",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_public_docs_use_operator_placeholders_instead_of_legacy_keys(self):
        combined = "\n".join(
            (
                self.readme,
                self.design,
                self.domain_implementation,
                self.control_plane_implementation,
            )
        )
        for placeholder in PUBLIC_OPERATOR_PLACEHOLDERS:
            with self.subTest(placeholder=placeholder):
                self.assertIn(placeholder, combined)

        forbidden_terms = (
            "MIM_ACCOUNT=",
            "MIM_REGION=",
            "MIM_HOSTNAME=",
            "MIM_APEX_ACTION=",
            "MIM_INITIAL_IAP_MEMBER=",
        )
        for term in forbidden_terms:
            with self.subTest(term=term):
                self.assertNotIn(term, combined)

    def test_domain_foundation_doc_uses_generic_protected_project_language(self):
        self.assertIn(
            "Never use any pre-existing sensitive project outside the reviewed MIM boundary.",
            self.domain_implementation,
        )
        self.assertIn("operator-only protected-project denylist", self.domain_implementation)
        self.assertNotIn("Known unrelated projects", self.domain_implementation)


if __name__ == "__main__":
    unittest.main()
