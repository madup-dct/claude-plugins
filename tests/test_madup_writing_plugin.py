import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "madup-writing"
PLUGIN_MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
SKILL_PATH = PLUGIN_ROOT / "skills" / "madup-writing" / "SKILL.md"
HOOKS_PATH = PLUGIN_ROOT / "hooks" / "hooks.json"
BEACON_PATH = PLUGIN_ROOT / "hooks" / "beacon.sh"
VOICE_GUIDE_PATH = PLUGIN_ROOT / "skills" / "madup-writing" / "references" / "voice-guide.md"
EXAMPLES_PATH = PLUGIN_ROOT / "skills" / "madup-writing" / "references" / "examples.md"
CHECKER_PATH = PLUGIN_ROOT / "skills" / "madup-writing" / "scripts" / "check_korean_style.py"
MARKETPLACE_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"
README_PATH = REPO_ROOT / "README.md"

EXPECTED_PLUGIN_NAME = "madup-writing"
EXPECTED_VERSION = "1.2.0"
EXPECTED_AUTHOR = {
    "name": "MADUP DCT",
    "email": "dc_team@madup.com",
}
EXPECTED_BEACON_URL = (
    "https://asia-northeast3-dataconsulting-imagen2-test.cloudfunctions.net/plugin-beacon"
)


def _load_checker_module():
    assert CHECKER_PATH.exists(), f"Missing checker script: {CHECKER_PATH}"
    spec = importlib.util.spec_from_file_location("madup_writing_checker", CHECKER_PATH)
    assert spec is not None and spec.loader is not None, "Unable to create checker module spec"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMadupWritingPluginPackaging(unittest.TestCase):
    def test_plugin_manifest_declares_identity_version_and_author(self):
        self.assertTrue(PLUGIN_MANIFEST.exists(), f"Missing plugin manifest: {PLUGIN_MANIFEST}")
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], EXPECTED_PLUGIN_NAME)
        self.assertEqual(manifest["version"], EXPECTED_VERSION)
        self.assertEqual(manifest["author"], EXPECTED_AUTHOR)
        self.assertIn("description", manifest)
        self.assertTrue(manifest["description"].strip())

    def test_skill_frontmatter_contains_only_name_and_description(self):
        self.assertTrue(SKILL_PATH.exists(), f"Missing SKILL.md: {SKILL_PATH}")
        text = SKILL_PATH.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md must start with YAML frontmatter")

        frontmatter = {}
        for raw_line in match.group(1).splitlines():
            if not raw_line.strip():
                continue
            key, _, value = raw_line.partition(":")
            self.assertTrue(_, f"Invalid frontmatter line: {raw_line!r}")
            frontmatter[key.strip()] = value.strip()

        self.assertEqual(set(frontmatter), {"name", "description"})
        self.assertEqual(frontmatter["name"], EXPECTED_PLUGIN_NAME)
        self.assertTrue(frontmatter["description"].startswith("Use when"))

    def test_marketplace_lists_matching_plugin_entry(self):
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        plugin_entry = next(
            (item for item in marketplace["plugins"] if item["name"] == EXPECTED_PLUGIN_NAME),
            None,
        )

        self.assertIsNotNone(plugin_entry, "Marketplace is missing madup-writing")
        self.assertEqual(plugin_entry["source"], "./plugins/madup-writing")
        self.assertEqual(plugin_entry["version"], EXPECTED_VERSION)
        self.assertTrue(plugin_entry["description"].strip())

    def test_readme_documents_install_command(self):
        readme = README_PATH.read_text(encoding="utf-8")
        self.assertIn("/plugin install madup-writing@madup", readme)

    def test_hooks_and_beacon_match_plugin_contract(self):
        self.assertTrue(HOOKS_PATH.exists(), f"Missing hooks.json: {HOOKS_PATH}")
        self.assertTrue(BEACON_PATH.exists(), f"Missing beacon.sh: {BEACON_PATH}")

        hooks = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            hooks,
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Skill",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/beacon.sh"',
                                    "timeout": 5,
                                }
                            ],
                        }
                    ]
                }
            },
        )

        beacon = BEACON_PATH.read_text(encoding="utf-8")
        self.assertIn(f'SKILL_NAME="{EXPECTED_PLUGIN_NAME}"', beacon)
        self.assertIn(f'VER="{EXPECTED_VERSION}"', beacon)
        self.assertIn(f'URL="{EXPECTED_BEACON_URL}"', beacon)
        self.assertIn("prompt", beacon.lower(), "Beacon comment should preserve the privacy contract")

    def test_reference_and_checker_paths_exist(self):
        for path in (VOICE_GUIDE_PATH, EXAMPLES_PATH, CHECKER_PATH):
            with self.subTest(path=path):
                self.assertTrue(path.exists(), f"Missing design path: {path}")


class TestMadupWritingGuidanceContracts(unittest.TestCase):
    def test_skill_and_voice_guide_lock_surface_form_tokens(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")
        voice_guide = VOICE_GUIDE_PATH.read_text(encoding="utf-8")

        for text in (skill, voice_guide):
            with self.subTest(source="skill" if text == skill else "voice_guide"):
                self.assertIn("copy supplied date/number/unit/name tokens verbatim", text)
                self.assertIn("8월 4일", text)
                self.assertIn("8/4", text)

    def test_report_guidance_only_allows_next_action_when_supplied_or_requested(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")
        voice_guide = VOICE_GUIDE_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "Report notes should be fact -> interpretation -> implication, with next action only when supplied or requested.",
            skill,
        )
        self.assertIn(
            "Keep the order factual: fact -> interpretation -> implication, then next action only when supplied or requested.",
            voice_guide,
        )
        self.assertNotIn("fact -> interpretation -> implication -> next action", skill)
        self.assertNotIn("Order: fact -> interpretation -> implication -> next action.", voice_guide)

    def test_proposal_guidance_forbids_invented_business_commitments(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")
        voice_guide = VOICE_GUIDE_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "Do not turn a result/target into a new schedule, owner, reporting promise, budget move, or guarantee unless supplied or explicitly requested.",
            skill,
        )
        self.assertIn(
            "Do not convert proof into an unsupplied budget move, rollout promise, or guarantee.",
            voice_guide,
        )

    def test_eval_contracts_cover_surface_form_and_no_fabricated_checkpoint(self):
        evals = json.loads((REPO_ROOT / "evals" / "evals.json").read_text(encoding="utf-8"))

        self.assertEqual(len(evals["cases"]), 3)
        for case in evals["cases"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(len(case["objective_checks"]), 4)

        slack_case = next(case for case in evals["cases"] if case["id"] == "slack-delayed-project-update")
        report_case = next(
            case for case in evals["cases"] if case["id"] == "client-performance-report-summary"
        )
        proposal_case = next(
            case for case in evals["cases"] if case["id"] == "proposal-advertiser-headline-and-body"
        )

        self.assertIn(
            "표면형 그대로",
            slack_case["objective_checks"][0],
        )
        self.assertIn("8월 4일", slack_case["objective_checks"][0])
        self.assertIn("8/4", slack_case["objective_checks"][0])

        report_checks = "\n".join(report_case["objective_checks"])
        self.assertNotIn("다음 확인 포인트", report_checks)
        self.assertIn("후속 일정, 담당자, 보고 약속", report_checks)

        proposal_checks = "\n".join(proposal_case["objective_checks"])
        self.assertIn("예산 재배분", proposal_checks)
        self.assertIn("보장", proposal_checks)

    def test_guidance_supports_corpus_backed_structures_without_inventing_them(self):
        guidance = "\n".join(
            (
                SKILL_PATH.read_text(encoding="utf-8"),
                VOICE_GUIDE_PATH.read_text(encoding="utf-8"),
            )
        ).lower()

        expected_patterns = (
            r"one-line (?:conclusion|positioning)",
            r"risk\s*->\s*action\s*->\s*status",
            r"strongest evidence first",
            r"controlled contrast",
            r"only when (?:the source|requested)",
        )
        for pattern in expected_patterns:
            with self.subTest(pattern=pattern):
                self.assertRegex(guidance, pattern)

    def test_skill_treats_requested_output_shape_as_a_hard_contract(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "Treat an exact sentence, line, paragraph, or item count as a hard output contract.",
            skill,
        )
        self.assertIn(
            "Do not add field labels such as `제목`, `헤드라인`, or `본문` unless requested.",
            skill,
        )
        self.assertIn(
            "Do not add a greeting, sign-off, quote marks, preface, or afterword unless requested.",
            skill,
        )

    def test_presentation_guidance_forbids_unsupplied_comparisons_and_clarification_loops(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "Do not invent a comparison period, benchmark, cause, or trend to support a lone metric.",
            skill,
        )
        self.assertIn(
            "When at least one usable fact is supplied, write the requested copy instead of asking for more context.",
            skill,
        )

    def test_mode_guidance_closes_live_failure_patterns(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")

        expected_rules = (
            "Build a silent must-keep checklist before drafting: named topic or workstream, facts, requested action, dates, numbers, names, and exact wording constraints.",
            "Before returning, compare the draft against the must-keep checklist; restore anything missing and compress wording rather than dropping a required item.",
            "For a short, one-paragraph, or paste-ready message, use plain text; use bullets only for multiple independent actions or owners.",
            "Treat a named topic or workstream as a locked fact, not background context; include its exact words in the final copy.",
            "In Slack, place the named topic or workstream in the first sentence.",
            "For a one-paragraph Slack request, use no blank lines and default to three sentences or fewer.",
            "Do not resolve `오늘`, `내일`, or `다음 주` into a new calendar date.",
            "Do not begin with meta text such as `정리했습니다`, `작성했습니다`, or `report 모드`.",
            "Without a supplied comparison, do not claim that performance was maintained, improved, worsened, or stable.",
            "Do not call overall performance `안정적`, `견조`, or `개선` without a matching baseline for that subject.",
            "Preserve phase and time anchors verbatim; never change `첫 달` to `이번 달`.",
            "When the prompt says a metric must stay unchanged, preserve the complete metric span verbatim.",
        )
        for rule in expected_rules:
            with self.subTest(rule=rule):
                self.assertIn(rule, skill)

    def test_skill_prioritizes_hard_contracts_and_loads_references_only_when_needed(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "Priority: locked facts and explicit output shape override mode defaults; mode defaults override style cleanup.",
            skill,
        )
        self.assertIn(
            "Consult [voice-guide.md](references/voice-guide.md) only when a mode needs nuance not covered above.",
            skill,
        )
        self.assertIn(
            "Consult [examples.md](references/examples.md) only when an example pattern would help.",
            skill,
        )


class TestMadupWritingCheckerBehavior(unittest.TestCase):
    def test_analyze_text_flags_empty_transitions_and_inflated_phrases(self):
        checker = _load_checker_module()
        findings = checker.analyze_text(
            "먼저, 결론부터 말씀드리겠습니다.\n"
            "이번 제안은 업계를 선도할 혁신적인 솔루션입니다.\n"
            "본 프로젝트를 통해 시너지를 극대화할 수 있습니다.\n"
            "### 핵심 포인트 ###\n"
        )

        self.assertEqual(
            {item["rule"] for item in findings},
            {
                "empty_transition",
                "inflated_claim",
                "translation_like_phrase",
                "excessive_formatting",
            },
        )

    def test_analyze_text_masks_locked_quotes_and_allows_plain_business_sentences(self):
        checker = _load_checker_module()
        clean_cases = (
            '광고주 확정 카피는 "업계를 선도할 프리미엄 캠페인"입니다. 이 문구는 그대로 유지해주세요.',
            "정부의 '혁신적 포용국가' 기조에 맞춘 캠페인입니다.",
            "지난주 광고주에 4월 성과 리포트를 제공했습니다.",
            "일정이 좀 밀릴 것 같습니다... 내일 다시 공유드릴게요.",
            "첫째 주 성과는 둘째 주와 비슷했습니다.",
        )
        for case in clean_cases:
            with self.subTest(case=case):
                self.assertEqual(checker.analyze_text(case), [])

    def test_analyze_text_flags_documented_ai_tells(self):
        checker = _load_checker_module()
        flagged_cases = (
            ("다양한 시사점을 제공했습니다.", "translation_like_phrase"),
            ("성과가 점진적으로 개선되어질 것으로 보여집니다.", "translation_like_phrase"),
            ("이번 테스트 결과는 시사하는 바가 큽니다.", "inflated_claim"),
            ("압도적인 성과와 획기적인 개선을 확인했습니다.", "inflated_claim"),
            ("단순한 광고가 아니라 브랜드 경험입니다.", "canned_structure"),
            ("결론적으로, 이번 분기 목표는 달성 가능합니다.", "canned_structure"),
            ("첫째, 예산을 정리합니다. 둘째, 소재를 교체합니다.", "canned_structure"),
            (
                "또한 예산을 늘렸습니다.\n그리고 소재를 교체했습니다.\n따라서 성과가 났습니다.",
                "sentence_initial_connectives",
            ),
        )
        for text, rule in flagged_cases:
            with self.subTest(rule=rule, text=text):
                rules = {item["rule"] for item in checker.analyze_text(text)}
                self.assertIn(rule, rules)

    def test_analyze_text_never_claims_ai_authorship(self):
        checker = _load_checker_module()
        findings = checker.analyze_text(
            "먼저, 결론부터 말씀드리겠습니다.\n"
            "본 프로젝트를 통해 시너지를 극대화할 수 있습니다.\n"
        )

        rendered = json.dumps(findings, ensure_ascii=False).lower()
        forbidden_phrases = [
            "ai-authored",
            "written by ai",
            "authored by ai",
            "ai가 쓴",
            "인공지능이 작성",
        ]
        for phrase in forbidden_phrases:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, rendered)

    def test_one_line_empty_transition_is_flagged(self):
        checker = _load_checker_module()
        findings = checker.analyze_text(
            "먼저, 결론부터 말씀드리겠습니다. 일정이 다소 조정되었습니다."
        )

        self.assertTrue(
            any(item["rule"] == "empty_transition" for item in findings),
            findings,
        )

    def test_regular_markdown_heading_and_fence_are_not_flagged(self):
        checker = _load_checker_module()
        findings = checker.analyze_text(
            "# 주간 업데이트\n"
            "```\n"
            "CTR 1.8%\n"
            "```\n"
        )

        self.assertFalse(
            any(item["rule"] == "excessive_formatting" for item in findings),
            findings,
        )

    def test_concise_factual_update_has_no_high_confidence_finding(self):
        checker = _load_checker_module()
        findings = checker.analyze_text(
            "개발 일정이 이틀 밀렸습니다. 오늘 범위를 줄이기로 결정했고, 수정안은 민지가 8월 4일까지 공유합니다."
        )

        self.assertFalse(any(item["severity"] == "high" for item in findings), findings)

    def test_findings_are_json_serializable_and_use_stable_fields(self):
        checker = _load_checker_module()
        findings = checker.analyze_text(
            "먼저, 결론부터 말씀드리겠습니다.\n"
            "본 프로젝트를 통해 시너지를 극대화할 수 있습니다.\n"
        )

        serialized = json.dumps(findings, ensure_ascii=False)
        self.assertEqual(json.loads(serialized), findings)

        for finding in findings:
            with self.subTest(finding=finding):
                self.assertEqual(
                    set(finding),
                    {"rule", "severity", "line", "text", "suggestion"},
                )
                self.assertIsInstance(finding["rule"], str)
                self.assertTrue(finding["rule"])
                self.assertIsInstance(finding["severity"], str)
                self.assertIn(finding["severity"], {"low", "medium", "high"})
                self.assertIsInstance(finding["line"], int)
                self.assertGreaterEqual(finding["line"], 1)
                self.assertIsInstance(finding["text"], str)
                self.assertTrue(finding["text"])
                self.assertIsInstance(finding["suggestion"], str)
                self.assertTrue(finding["suggestion"])

    def test_cli_missing_file_exits_2_without_traceback(self):
        missing_path = REPO_ROOT / "tests" / "__missing_input__.txt"
        result = subprocess.run(
            [sys.executable, str(CHECKER_PATH), str(missing_path)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Cannot read input:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_invalid_utf8_file_exits_2_without_traceback(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"\xff\xfe\x80invalid")
            invalid_path = Path(handle.name)

        try:
            result = subprocess.run(
                [sys.executable, str(CHECKER_PATH), str(invalid_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            invalid_path.unlink(missing_ok=True)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Cannot read input:", result.stderr)
        self.assertIn("Invalid UTF-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
