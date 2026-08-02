import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
STRESS_ROOT = REPO_ROOT / "evals" / "stress"
MANIFEST_PATH = STRESS_ROOT / "manifest.json"
README_PATH = STRESS_ROOT / "README.md"
SNAPSHOT_PATH = STRESS_ROOT / "golden_cases.jsonl"
GENERATOR_PATH = STRESS_ROOT / "generator.py"
ORACLE_PATH = STRESS_ROOT / "oracle.py"
SAMPLE_SELECTOR_PATH = STRESS_ROOT / "sample_selector.py"
LIVE_RUNNER_PATH = STRESS_ROOT / "live_runner.py"
LIVE_REGRADER_PATH = STRESS_ROOT / "regrade_live_run.py"

EXPECTED_MODE_COUNTS = {
    "slack_email": 450,
    "report": 250,
    "proposal": 200,
    "presentation_copy": 100,
}

EXPECTED_SUBMODE_COUNTS = {
    "slack": 300,
    "email": 150,
    "report": 250,
    "proposal": 200,
    "presentation": 100,
}


def _load_module(name: str, path: Path):
    assert path.exists(), f"Missing module: {path}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"Unable to load module spec for {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_snapshot() -> list[dict[str, object]]:
    assert SNAPSHOT_PATH.exists(), f"Missing snapshot: {SNAPSHOT_PATH}"
    cases = []
    for line_no, raw_line in enumerate(SNAPSHOT_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        payload = json.loads(raw_line)
        assert isinstance(payload, dict), f"Snapshot line {line_no} is not an object"
        cases.append(payload)
    return cases


class TestMadupWritingStressSuitePackaging(unittest.TestCase):
    def test_stress_suite_files_exist(self):
        for path in (
            MANIFEST_PATH,
            README_PATH,
            SNAPSHOT_PATH,
            GENERATOR_PATH,
            ORACLE_PATH,
            SAMPLE_SELECTOR_PATH,
            LIVE_RUNNER_PATH,
            LIVE_REGRADER_PATH,
        ):
            with self.subTest(path=path):
                self.assertTrue(path.exists(), f"Missing stress-suite file: {path}")

    def test_manifest_declares_exact_corpus_counts(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], "1.1.0")
        self.assertEqual(manifest["generator_version"], "1.3.0")
        self.assertEqual(manifest["oracle_version"], "1.4.0")
        self.assertEqual(manifest["suite_seed"], "madup-writing-stress-v1")
        self.assertEqual(manifest["case_count"], 1000)
        self.assertEqual(manifest["mode_counts"], EXPECTED_MODE_COUNTS)
        self.assertEqual(manifest["submode_counts"], EXPECTED_SUBMODE_COUNTS)
        self.assertEqual(manifest["default_live_sample_size"], 96)

    def test_readme_distinguishes_contract_suite_from_live_model_eval(self):
        readme = README_PATH.read_text(encoding="utf-8")

        self.assertIn("deterministic contract", readme.lower())
        self.assertIn("claude -p", readme)
        self.assertIn("release gates", readme.lower())
        self.assertIn("does not prove model quality", readme.lower())
        self.assertIn("false positive", readme.lower())
        self.assertIn("false negative", readme.lower())
        self.assertIn("96", readme)
        self.assertIn("192", readme)
        self.assertIn("--confirm-all-1000", readme)
        self.assertIn("OAuth", readme)
        self.assertIn("subscription usage limits", readme.lower())
        self.assertIn("ANTHROPIC_API_KEY", readme)
        self.assertIn("ANTHROPIC_AUTH_TOKEN", readme)
        self.assertIn("apiKeyHelper", readme)
        self.assertIn("claude auth status", readme)
        self.assertNotIn("--allow-api-billing", readme)
        self.assertNotIn("$38.40", readme)
        self.assertNotIn("$400", readme)
        self.assertIn("cannot be bypassed", readme.lower())


class TestMadupWritingStressSuiteGeneration(unittest.TestCase):
    def test_generator_reproduces_tracked_snapshot_exactly(self):
        generator = _load_module("madup_writing_stress_generator", GENERATOR_PATH)
        generated_cases = generator.generate_cases()
        snapshot_cases = _load_snapshot()

        self.assertEqual(generated_cases, snapshot_cases)

    def test_generated_snapshot_has_exactly_1000_unique_cases(self):
        snapshot_cases = _load_snapshot()
        case_ids = [case["id"] for case in snapshot_cases]

        self.assertEqual(len(snapshot_cases), 1000)
        self.assertEqual(len(case_ids), len(set(case_ids)))

    def test_generated_snapshot_matches_required_submode_distribution(self):
        snapshot_cases = _load_snapshot()
        submode_counts = Counter(case["submode"] for case in snapshot_cases)
        mode_counts = Counter(case["mode"] for case in snapshot_cases)

        self.assertEqual(dict(submode_counts), EXPECTED_SUBMODE_COUNTS)
        self.assertEqual(dict(mode_counts), EXPECTED_MODE_COUNTS)

    def test_generated_snapshot_has_1000_unique_prompts(self):
        snapshot_cases = _load_snapshot()
        prompts = [case["prompt"] for case in snapshot_cases]

        self.assertEqual(len(prompts), 1000)
        self.assertEqual(len(set(prompts)), 1000)

    def test_generated_snapshot_exercises_every_declared_fact_axis(self):
        generator = _load_module("madup_writing_stress_generator_coverage", GENERATOR_PATH)
        snapshot_cases = _load_snapshot()

        pools_by_submode = {
            "slack": (
                generator.SLACK_FACTS,
                generator.SLACK_CONTEXTS,
                generator.SLACK_NAMES,
                generator.SLACK_WEEKDAYS,
                generator.SLACK_DATES,
                generator.SLACK_FOLLOWUPS,
                generator.SLACK_WORKSTREAMS,
                generator.SLACK_RELEASES,
            ),
            "email": (
                generator.EMAIL_SUBJECTS,
                generator.EMAIL_ACTIONS,
                generator.EMAIL_NAMES,
                generator.EMAIL_WEEKDAYS,
                generator.EMAIL_DATES,
                generator.EMAIL_AUDIENCES,
                generator.EMAIL_CONTEXTS,
            ),
            "report": (
                generator.REPORT_REVENUES,
                generator.REPORT_ROAS,
                generator.REPORT_CONVERSIONS,
                generator.REPORT_CTRS,
                generator.REPORT_CPAS,
                generator.REPORT_WINDOWS,
                generator.REPORT_VERTICALS,
            ),
            "proposal": (
                generator.PROPOSAL_EVIDENCE,
                generator.PROPOSAL_CPA,
                generator.PROPOSAL_WINDOWS,
                generator.PROPOSAL_REDUCTIONS,
                generator.PROPOSAL_VERTICALS,
            ),
            "presentation": (
                generator.PRESENTATION_METRICS,
                generator.PRESENTATION_CONTEXTS,
                generator.PRESENTATION_AUDIENCES,
                generator.PRESENTATION_FRAMINGS,
            ),
        }

        for submode, pools in pools_by_submode.items():
            prompts = "\n".join(
                case["prompt"] for case in snapshot_cases if case["submode"] == submode
            )
            for pool in pools:
                for token in pool:
                    with self.subTest(submode=submode, token=token):
                        self.assertIn(token, prompts)

    def test_each_submode_uses_five_real_prompt_openers(self):
        generator = _load_module("madup_writing_stress_generator_templates", GENERATOR_PATH)
        snapshot_cases = _load_snapshot()

        self.assertEqual(set(generator.PROMPT_OPENERS), set(EXPECTED_SUBMODE_COUNTS))
        for submode, openers in generator.PROMPT_OPENERS.items():
            self.assertEqual(len(openers), 5)
            self.assertEqual(len(set(openers)), 5)
            prompts = [case["prompt"] for case in snapshot_cases if case["submode"] == submode]
            for opener in openers:
                with self.subTest(submode=submode, opener=opener):
                    self.assertTrue(any(prompt.startswith(opener) for prompt in prompts))

    def test_every_case_contains_required_schema_fields(self):
        snapshot_cases = _load_snapshot()
        required_keys = {
            "id",
            "mode",
            "submode",
            "template",
            "seed",
            "prompt",
            "locked_tokens",
            "context_tokens",
            "forbidden_inventions",
            "shape",
            "style",
            "stratum",
        }

        for case in snapshot_cases:
            with self.subTest(case=case["id"]):
                self.assertEqual(set(case), required_keys)
                self.assertIsInstance(case["locked_tokens"], list)
                self.assertTrue(case["locked_tokens"])
                self.assertIsInstance(case["context_tokens"], list)
                self.assertTrue(case["context_tokens"])
                self.assertFalse(
                    set(case["locked_tokens"]) & set(case["context_tokens"]),
                    case["id"],
                )
                self.assertIsInstance(case["forbidden_inventions"], list)
                self.assertTrue(case["forbidden_inventions"])
                self.assertIsInstance(case["shape"], dict)
                self.assertIsInstance(case["style"], dict)

    def test_generated_prompts_use_the_correct_korean_subject_particle_for_names(self):
        cases = _load_snapshot()
        email_case = next(item for item in cases if item["id"] == "email-002")
        slack_case = next(item for item in cases if item["id"] == "slack-001")

        self.assertIn("현우가", email_case["prompt"])
        self.assertNotIn("현우이 ", email_case["prompt"])
        self.assertIn("민지가", slack_case["prompt"])
        self.assertNotIn("민지이고", slack_case["prompt"])

    def test_generated_presentation_prompts_use_natural_korean_particles(self):
        case = next(item for item in _load_snapshot() if item["id"] == "presentation-025")

        self.assertIn("오프닝 카피야", case["prompt"])
        self.assertNotIn("오프닝 카피이야", case["prompt"])
        self.assertIn("ROAS 412%는", case["prompt"])
        self.assertNotIn("ROAS 412%은", case["prompt"])


class TestMadupWritingStressSuiteOracle(unittest.TestCase):
    def test_oracle_accepts_gold_response_with_locked_tokens_preserved(self):
        oracle = _load_module("madup_writing_stress_oracle", ORACLE_PATH)
        snapshot_cases = _load_snapshot()
        case = next(item for item in snapshot_cases if item["id"] == "slack-001")

        result = oracle.evaluate_response(
            case,
            "앱 결제 QA에서 결제 오류 2건이 재현돼 1차 배포는 다음 주 화요일(8월 4일)로 미룹니다. "
            "수정은 민지가 맡고 월요일 오전까지 테스트 결과를 다시 공유하겠습니다.",
        )

        self.assertTrue(result["passed"], result)
        self.assertFalse(result["hard_failures"], result)

    def test_oracle_rejects_response_that_reformats_locked_tokens(self):
        oracle = _load_module("madup_writing_stress_oracle", ORACLE_PATH)
        snapshot_cases = _load_snapshot()
        case = next(item for item in snapshot_cases if item["id"] == "slack-001")

        result = oracle.evaluate_response(
            case,
            "앱 결제 QA에서 결제 오류 2건이 재현돼 1차 배포는 다음 주 화요일(8/4)로 미룹니다. "
            "수정은 민지가 맡고 월요일 오전까지 테스트 결과를 다시 공유하겠습니다.",
        )

        self.assertFalse(result["passed"], result)
        self.assertTrue(
            any(failure["check"] == "locked_tokens" for failure in result["hard_failures"]),
            result,
        )

    def test_email_audience_and_request_context_are_not_required_verbatim(self):
        oracle = _load_module("madup_writing_stress_oracle_email_context", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "email-002")

        result = oracle.evaluate_response(
            case,
            "소재 교체 일정 관련 내부 회신을 오늘 마쳤습니다. "
            "최종본은 현우님이 다음 주 화요일(8월 5일) 오전까지 전달드릴 예정입니다.",
        )

        self.assertTrue(result["passed"], result)

    def test_report_vertical_is_context_not_a_required_verbatim_fact(self):
        oracle = _load_module("madup_writing_stress_oracle_report_context", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "report-001")

        result = oracle.evaluate_response(
            case,
            "7월 검색광고 매출은 1억 2,400만 원, ROAS는 412%, 전환수는 318건입니다. "
            "브랜드 캠페인 CTR은 전주 대비 0.8%p 올랐고 일반 캠페인 CPA는 12% 낮아졌습니다. "
            "신규 소재는 아직 3일치 데이터뿐이라 현재 수치만 공유합니다.",
        )

        self.assertTrue(result["passed"], result)

    def test_proposal_vertical_is_context_not_a_required_verbatim_fact(self):
        oracle = _load_module("madup_writing_stress_oracle_proposal_context", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "proposal-001")

        result = oracle.evaluate_response(
            case,
            "파일럿 3건에서 확인한 CPA 18% 개선을 첫 달 운영에 적용합니다\n"
            "파일럿 3건에서 같은 운영 방식으로 평균 CPA를 18% 낮췄습니다. "
            "첫 달에는 검색광고 구조 개편과 소재 테스트를 함께 진행합니다. "
            "6주 안에 비효율 키워드 20%를 줄이는 목표를 검증합니다.",
        )

        self.assertTrue(result["passed"], result)

    def test_presentation_audience_and_framing_are_not_required_verbatim(self):
        oracle = _load_module("madup_writing_stress_oracle_presentation_context", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "presentation-001")

        result = oracle.evaluate_response(
            case,
            "ROAS 412%, 성과를 숫자로 확인했습니다\n핵심 결과를 한 문장으로 정리했습니다.",
        )

        self.assertTrue(result["passed"], result)

    def test_reference_renderer_uses_natural_korean_name_particles(self):
        oracle = _load_module("madup_writing_stress_oracle_particles", ORACLE_PATH)
        cases = _load_snapshot()
        slack_reference = oracle.render_reference(
            next(item for item in cases if item["id"] == "slack-001")
        )
        email_reference = oracle.render_reference(
            next(item for item in cases if item["id"] == "email-002")
        )

        self.assertIn("민지가 맡고", slack_reference)
        self.assertNotIn("민지이 맡고", slack_reference)
        self.assertIn("현우가", email_reference)
        self.assertNotIn("현우이", email_reference)

    def test_shape_checks_ignore_structural_field_labels(self):
        oracle = _load_module("madup_writing_stress_oracle_structural_labels", ORACLE_PATH)
        cases = _load_snapshot()
        proposal_case = next(item for item in cases if item["id"] == "proposal-006")
        presentation_case = next(
            item for item in cases if item["id"] == "presentation-025"
        )
        proposal_response = (
            "**헤드라인**\n"
            "파일럿 4건에서 평균 CPA 21%를 낮춘 운영 방식, 이번 캠페인에도 동일하게 적용합니다\n\n"
            "**본문**\n"
            "지난 파일럿 4건에서 동일한 운영 방식으로 평균 CPA를 21% 낮췄습니다. "
            "첫 달에는 검색광고 구조 개편과 소재 테스트를 함께 진행합니다. "
            "5주 안에 비효율 키워드 20%를 줄이는 것이 이번 목표입니다."
        )
        presentation_response = (
            "**헤드라인**\n\"ROAS 412%, 이번 캠페인의 성과입니다.\"\n\n"
            "**보조 문장**\n\"광고비 1의 지출로 4배가 넘는 매출을 만들어냈습니다.\""
        )

        self.assertTrue(
            oracle.evaluate_response(proposal_case, proposal_response)["passed"]
        )
        self.assertTrue(
            oracle.evaluate_response(presentation_case, presentation_response)["passed"]
        )

    def test_email_shape_checks_ignore_subject_greeting_and_signoff(self):
        oracle = _load_module("madup_writing_stress_oracle_email_scaffolding", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "email-038")
        response = (
            "제목: 월간 리포트 초안 회신 완료 안내\n\n"
            "안녕하세요,\n\n"
            "월간 리포트 초안 검토 및 내부 회신은 오늘 완료했습니다.\n\n"
            "민지님이 다음 주 목요일(8월 7일) 오전까지 최종본을 보내주실 예정입니다.\n\n"
            "감사합니다."
        )

        self.assertTrue(oracle.evaluate_response(case, response)["passed"])

    def test_email_shape_checks_ignore_named_salutations(self):
        oracle = _load_module("madup_writing_stress_oracle_named_salutations", ORACLE_PATH)
        cases = _load_snapshot()
        responses = {
            "email-064": (
                "제목: 검색어 제외 키워드 최종 확인 안내\n\n"
                "안녕하세요, 담당자님.\n\n"
                "검색어 제외 키워드 정리를 오늘 내부 확정했습니다. "
                "최종본은 현우님이 다음 주 수요일(8월 7일) 오전까지 전달드릴 예정입니다.\n\n"
                "확인 부탁드립니다. 감사합니다."
            ),
            "email-131": (
                "제목: 광고 계정 구조 개편안 내부 검토 완료 안내\n\n"
                "광고주님, 안녕하세요.\n\n"
                "광고 계정 구조 개편안에 대해 오늘 내부 검토를 마쳤습니다. "
                "최종본은 지안이 다음 주 금요일(8월 6일) 오전까지 전달드릴 예정입니다.\n\n"
                "받으신 후 편하실 때 확인 부탁드립니다. 감사합니다."
            ),
        }

        for case_id, response in responses.items():
            case = next(item for item in cases if item["id"] == case_id)
            with self.subTest(case_id=case_id):
                self.assertTrue(oracle.evaluate_response(case, response)["passed"])

    def test_oracle_rejects_unsupplied_calendar_dates(self):
        oracle = _load_module("madup_writing_stress_oracle_unsupplied_date", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "email-038")
        response = (
            "월간 리포트 초안 검토 및 내부 회신은 오늘(8/2) 완료했습니다. "
            "민지님이 다음 주 목요일(8월 7일) 오전까지 최종본을 보내주실 예정입니다."
        )

        result = oracle.evaluate_response(case, response)

        self.assertFalse(result["passed"], result)
        self.assertTrue(
            any(failure["check"] == "unsupplied_dates" for failure in result["hard_failures"]),
            result,
        )

    def test_oracle_rejects_unsupplied_comparison_periods(self):
        oracle = _load_module("madup_writing_stress_oracle_unsupplied_comparison", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "presentation-025")
        response = (
            "ROAS 412%, 이번 캠페인의 성과입니다.\n"
            "지난 분기 대비 광고 효율을 실제 수치로 확인했습니다."
        )

        result = oracle.evaluate_response(case, response)

        self.assertFalse(result["passed"], result)
        self.assertTrue(
            any(
                failure["check"] == "unsupplied_comparisons"
                for failure in result["hard_failures"]
            ),
            result,
        )

    def test_presentation_oracle_rejects_unsupplied_benchmark_cause_and_trend(self):
        oracle = _load_module("madup_writing_stress_oracle_metric_claims", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "presentation-025")
        responses = {
            "benchmark": (
                "ROAS 412%, 이번 캠페인의 성과입니다.\n"
                "업계 평균 수준의 효율을 보여줍니다."
            ),
            "cause": (
                "ROAS 412%, 이번 캠페인의 성과입니다.\n"
                "소재 개선 덕분에 효율이 높아졌습니다."
            ),
            "trend": (
                "ROAS 412%, 이번 캠페인의 성과입니다.\n"
                "상승세가 이어지고 있습니다."
            ),
        }

        for category, response in responses.items():
            result = oracle.evaluate_response(case, response)
            with self.subTest(category=category):
                self.assertFalse(result["passed"], result)
                self.assertTrue(
                    any(
                        failure["check"] == "unsupplied_metric_claims"
                        and category in failure.get("categories", [])
                        for failure in result["hard_failures"]
                    ),
                    result,
                )

    def test_report_without_explicit_sentence_limit_allows_grounded_detail(self):
        oracle = _load_module("madup_writing_stress_oracle_report_detail", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "report-139")
        response = (
            "7월 검색광고 매출은 1억 5,200만 원, ROAS는 436%, 전환수는 291건입니다. "
            "광고비 대비 매출이 4.36배 발생했고, 이 매출은 291건의 전환으로 이어졌습니다.\n"
            "브랜드 캠페인은 전주 대비 CTR이 0.5%p 상승했습니다. 클릭률이 전주보다 소폭 높아졌습니다.\n"
            "일반 캠페인은 CPA가 11% 낮아졌습니다. 전환 1건당 비용이 전주보다 줄었습니다.\n"
            "신규 소재는 아직 3일치 데이터만 쌓여, 같은 수준으로 해석하기는 이릅니다."
        )

        self.assertTrue(oracle.evaluate_response(case, response)["passed"])

    def test_report_with_explicit_four_sentence_limit_rejects_five_sentences(self):
        oracle = _load_module("madup_writing_stress_oracle_report_limit", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "report-001")
        response = (
            f"{oracle.render_reference(case)} "
            "매출은 1억 2,400만 원입니다. 전환수는 318건입니다."
        )

        result = oracle.evaluate_response(case, response)

        self.assertFalse(result["passed"], result)
        self.assertTrue(
            any(
                failure["check"] == "shape" and failure.get("reason") == "max_sentences"
                for failure in result["hard_failures"]
            ),
            result,
        )

    def test_report_rejects_meta_scaffolding_and_unsupported_evaluation(self):
        oracle = _load_module("madup_writing_stress_oracle_report_slop", ORACLE_PATH)
        cases = _load_snapshot()
        samples = {
            "report-232": (
                "7월 검색광고 성과를 report 모드로 정리했습니다.\n"
                "매출 9,800만 원, ROAS 385%, 전환수 318건입니다. "
                "브랜드 캠페인 CTR은 전주 대비 0.9%p 상승했고 일반 캠페인 CPA는 12% 낮아졌습니다. "
                "신규 소재는 아직 5일치 데이터뿐입니다."
            ),
            "report-197": (
                "매출과 수익성, 캠페인 효율 모두 개선된 한 달이었습니다. "
                "매출 9,800만 원, ROAS 447%, 전환수 291건입니다. "
                "브랜드 캠페인 CTR은 전주 대비 0.5%p 상승했고 일반 캠페인 CPA는 15% 낮아졌습니다. "
                "신규 소재는 3일치 데이터뿐입니다."
            ),
        }

        for case_id, response in samples.items():
            case = next(item for item in cases if item["id"] == case_id)
            result = oracle.evaluate_response(case, response)
            with self.subTest(case_id=case_id):
                self.assertFalse(result["passed"], result)
                self.assertTrue(
                    any(
                        failure["check"] == "forbidden_inventions"
                        for failure in result["hard_failures"]
                    ),
                    result,
                )

    def test_report_data_window_locks_unit_surface_without_forcing_word_order(self):
        oracle = _load_module("madup_writing_stress_oracle_data_window", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "report-147")
        response = (
            "7월 검색광고는 매출 9,800만 원, ROAS 447%, 전환수 274건입니다. "
            "브랜드 캠페인 CTR은 전주 대비 0.5%p 상승했고 일반 캠페인 CPA는 12% 낮아졌습니다. "
            "신규 소재는 누적 데이터가 4일치로, 현재 판단은 이릅니다."
        )

        result = oracle.evaluate_response(case, response)

        self.assertFalse(
            any(failure["check"] == "locked_tokens" for failure in result["hard_failures"]),
            result,
        )

    def test_slack_fact_tokens_allow_recomposition_without_dropping_terms(self):
        oracle = _load_module("madup_writing_stress_oracle_slack_recomposition", ORACLE_PATH)
        cases = _load_snapshot()
        responses = {
            "slack-179": (
                "대시보드 수치 건입니다. 어제 운영 점검에서 보고서와 수치 사이 불일치가 2건 확인되어, 오늘 범위를 줄여 "
                "파일럿 배포를 다음 주 금요일(8월 7일)로 미루기로 했습니다. 수정은 민지님이 맡고, "
                "수요일 오전까지 테스트 결과 다시 공유하겠습니다."
            ),
            "slack-254": (
                "브랜드 캠페인 보고서, 어제 교차 확인에서 수치 불일치 2건이 확인됐습니다. "
                "오늘 범위를 줄여 긴급 배포는 다음 주 화요일(8월 4일)로 미룹니다. "
                "수정은 유진님이 담당하며, 화요일 오후까지 테스트 결과를 다시 공유하겠습니다."
            ),
        }

        for case_id, response in responses.items():
            case = next(item for item in cases if item["id"] == case_id)
            result = oracle.evaluate_response(case, response)
            with self.subTest(case_id=case_id):
                self.assertFalse(
                    any(
                        failure["check"] == "locked_tokens"
                        for failure in result["hard_failures"]
                    ),
                    result,
                )

    def test_slack_workstream_requires_exact_surface(self):
        oracle = _load_module("madup_writing_stress_oracle_slack_workstream", ORACLE_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "slack-001")
        response = (
            "앱 관련 1차 배포는 다음 주 화요일(8월 4일)로 미룹니다. "
            "어제 QA에서 결제 오류 2건이 확인됐고 수정은 민지님이 맡습니다. "
            "월요일 오전까지 테스트 결과를 다시 공유하겠습니다."
        )

        result = oracle.evaluate_response(case, response)

        self.assertFalse(result["passed"], result)
        self.assertTrue(
            any(
                failure["check"] == "locked_tokens"
                and "앱 결제" in failure.get("missing_tokens", [])
                for failure in result["hard_failures"]
            ),
            result,
        )

    def test_oracle_rejects_unsupplied_business_commitments(self):
        oracle = _load_module("madup_writing_stress_oracle", ORACLE_PATH)
        snapshot_cases = _load_snapshot()
        case = next(item for item in snapshot_cases if item["id"] == "proposal-001")

        result = oracle.evaluate_response(
            case,
            "이커머스 파일럿 3건에서 평균 CPA를 18% 낮춘 운영 방식을 첫 달 구조 개편에 적용합니다. "
            "첫 달에는 검색광고 구조 개편과 소재 테스트를 함께 진행합니다. "
            "6주 안에 비효율 키워드 20%를 줄이고 예산을 성과가 나는 캠페인으로 재배분하겠습니다.",
        )

        self.assertFalse(result["passed"], result)
        self.assertTrue(
            any(failure["check"] == "forbidden_inventions" for failure in result["hard_failures"]),
            result,
        )

    def test_oracle_self_tests_cover_gold_and_mutants(self):
        oracle = _load_module("madup_writing_stress_oracle", ORACLE_PATH)
        self.assertTrue(oracle.run_self_tests())

    def test_oracle_does_not_flag_safe_recommendation_as_budget_move(self):
        oracle = _load_module("madup_writing_stress_oracle", ORACLE_PATH)
        snapshot_cases = _load_snapshot()
        case = next(item for item in snapshot_cases if item["id"] == "proposal-001")

        result = oracle.evaluate_response(
            case,
            "이커머스 파일럿 3건 평균 CPA 18% 개선을 첫 달 운영 기준으로 바로 적용합니다\n"
            "파일럿 3건에서 평균 CPA를 18% 낮춘 운영 방식을 먼저 확인했습니다. "
            "첫 달에는 검색광고 구조 개편과 소재 테스트를 함께 진행합니다. "
            "6주 안에 비효율 키워드 20%를 줄이는 목표를 같은 방식으로 다시 맞추겠습니다.",
        )

        self.assertTrue(result["passed"], result)

    def test_reference_renderer_passes_oracle_for_all_1000_cases(self):
        oracle = _load_module("madup_writing_stress_oracle", ORACLE_PATH)
        snapshot_cases = _load_snapshot()

        for case in snapshot_cases:
            with self.subTest(case=case["id"]):
                response = oracle.render_reference(case)
                result = oracle.evaluate_response(case, response)
                self.assertTrue(result["passed"], result)

    def test_locked_token_mutant_is_rejected_for_all_1000_cases(self):
        oracle = _load_module("madup_writing_stress_oracle", ORACLE_PATH)
        snapshot_cases = _load_snapshot()

        for case in snapshot_cases:
            with self.subTest(case=case["id"]):
                response = oracle.render_reference(case)
                mutant = oracle.make_locked_token_mutant(case, response)
                result = oracle.evaluate_response(case, mutant)
                self.assertFalse(result["passed"], result)
                self.assertTrue(
                    any(failure["check"] == "locked_tokens" for failure in result["hard_failures"]),
                    result,
                )

    def test_first_forbidden_marker_mutant_is_rejected_for_all_1000_cases(self):
        oracle = _load_module("madup_writing_stress_oracle", ORACLE_PATH)
        snapshot_cases = _load_snapshot()

        for case in snapshot_cases:
            with self.subTest(case=case["id"]):
                response = oracle.render_reference(case)
                mutant = oracle.make_first_forbidden_marker_mutant(case, response)
                result = oracle.evaluate_response(case, mutant)
                self.assertFalse(result["passed"], result)
                self.assertTrue(
                    any(failure["check"] == "forbidden_inventions" for failure in result["hard_failures"]),
                    result,
                )


class TestMadupWritingStressSuiteSampling(unittest.TestCase):
    def test_sample_selector_returns_deterministic_stratified_96_case_plan(self):
        selector = _load_module("madup_writing_stress_sample_selector", SAMPLE_SELECTOR_PATH)
        snapshot_cases = _load_snapshot()

        sample_a = selector.select_cases(snapshot_cases)
        sample_b = selector.select_cases(snapshot_cases)

        self.assertEqual(sample_a, sample_b)
        self.assertEqual(len(sample_a), 96)
        self.assertEqual(len({case["id"] for case in sample_a}), 96)

    def test_sample_selector_covers_every_submode(self):
        selector = _load_module("madup_writing_stress_sample_selector", SAMPLE_SELECTOR_PATH)
        snapshot_cases = _load_snapshot()
        sample = selector.select_cases(snapshot_cases)
        counts = Counter(case["submode"] for case in sample)

        for submode in EXPECTED_SUBMODE_COUNTS:
            with self.subTest(submode=submode):
                self.assertGreater(counts[submode], 0)


class TestMadupWritingStressSuiteCli(unittest.TestCase):
    def test_generator_cli_without_flags_prints_json_suite(self):
        result = subprocess.run(
            [sys.executable, str(GENERATOR_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), 1000)


class TestMadupWritingStressSuiteLiveRunner(unittest.TestCase):
    def test_live_runner_builds_claude_p_argv_with_strict_isolation(self):
        runner = _load_module("madup_writing_stress_live_runner", LIVE_RUNNER_PATH)
        snapshot_cases = _load_snapshot()
        case = snapshot_cases[0]
        argv = runner.build_claude_argv(case=case, arm="with_plugin", debug_log_path=Path("/tmp/debug.log"))

        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--setting-sources", argv)
        self.assertIn("project", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--mcp-config", argv)
        self.assertIn('{"mcpServers":{}}', argv)
        self.assertIn("--model", argv)
        self.assertIn("sonnet", argv)
        self.assertIn("--effort", argv)
        self.assertIn("high", argv)
        self.assertIn("--tools", argv)
        self.assertIn("Skill", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertNotIn("--max-budget-usd", argv)
        self.assertIn("--debug-file", argv)

    def test_live_runner_with_and_without_arms_only_differ_by_plugin_dir(self):
        runner = _load_module("madup_writing_stress_live_runner_arms", LIVE_RUNNER_PATH)
        snapshot_cases = _load_snapshot()
        case = snapshot_cases[0]
        with_argv = runner.build_claude_argv(case=case, arm="with_plugin", debug_log_path=Path("/tmp/with.log"))
        without_argv = runner.build_claude_argv(case=case, arm="without_plugin", debug_log_path=Path("/tmp/without.log"))

        def normalize(argv):
            normalized = list(argv)
            debug_index = normalized.index("--debug-file")
            normalized[debug_index + 1] = "<debug-file>"
            if "--plugin-dir" in normalized:
                plugin_index = normalized.index("--plugin-dir")
                del normalized[plugin_index : plugin_index + 2]
            return normalized

        self.assertIn("--plugin-dir", with_argv)
        self.assertIn(str(REPO_ROOT / "plugins" / "madup-writing"), with_argv)
        self.assertNotIn("--plugin-dir", without_argv)
        self.assertEqual(normalize(with_argv), normalize(without_argv))

    def test_live_runner_debug_evidence_requires_exact_load_and_skill_lines(self):
        runner = _load_module("madup_writing_stress_live_runner_debug", LIVE_RUNNER_PATH)

        generic = runner._classify_debug_evidence(
            "Registry path: /tmp/madup-writing/skills\n"
            "Prompt mentions madup-writing\n"
            "Loading inline plugin from path: madup-writing\n"
            "Failed to load inline plugin madup-writing\n"
            "SkillTool returning result for skill another-skill\n"
        )
        exact = runner._classify_debug_evidence(
            "Loaded inline plugin from path: madup-writing\n"
            "SkillTool returning 3 newMessages for skill madup-writing\n"
        )

        self.assertFalse(generic["plugin_loaded"])
        self.assertFalse(generic["skill_invoked"])
        self.assertTrue(exact["plugin_loaded"])
        self.assertTrue(exact["skill_invoked"])

    def test_live_runner_uses_tracked_snapshot_and_rejects_generator_drift(self):
        runner = _load_module("madup_writing_stress_live_runner_snapshot", LIVE_RUNNER_PATH)
        generated = runner.generator.generate_cases()

        self.assertEqual(runner.load_cases(), _load_snapshot())
        with mock.patch.object(runner.generator, "generate_cases", return_value=generated[:-1]):
            with self.assertRaisesRegex(ValueError, "tracked snapshot"):
                runner.load_cases()

    def test_live_runner_default_plan_uses_96_sample_and_two_arms_without_execution(self):
        runner = _load_module("madup_writing_stress_live_runner_plan", LIVE_RUNNER_PATH)
        with mock.patch("sys.stdout.write") as write_mock:
            result = runner.main([])

        self.assertEqual(result, 0)
        rendered = "".join(call.args[0] for call in write_mock.call_args_list)
        payload = json.loads(rendered)
        self.assertEqual(payload["summary"]["planned_case_count"], 96)
        self.assertEqual(payload["summary"]["planned_call_count"], 192)
        self.assertEqual(payload["arms"], ["with_plugin", "without_plugin"])
        self.assertEqual(payload["contract"]["schema_version"], "1.1.0")
        self.assertEqual(payload["contract"]["generator_version"], "1.3.0")
        self.assertEqual(len(payload["contract"]["snapshot_sha256"]), 64)
        self.assertEqual(payload["contract"]["oracle_version"], "1.4.0")
        self.assertEqual(len(payload["contract"]["oracle_sha256"]), 64)
        self.assertEqual(
            runner.DEFAULT_OUTPUT_ROOT,
            REPO_ROOT / "madup-writing-workspace" / "stress-live",
        )

    def test_live_runner_rejects_execute_all_without_confirmation(self):
        runner = _load_module("madup_writing_stress_live_runner_guard", LIVE_RUNNER_PATH)
        with mock.patch("sys.stderr.write"), self.assertRaises(SystemExit) as ctx:
            runner.main(["--execute", "--all"])
        self.assertEqual(ctx.exception.code, 2)

    def test_live_runner_rejects_unknown_case_id(self):
        runner = _load_module("madup_writing_stress_live_runner_unknown", LIVE_RUNNER_PATH)
        with mock.patch("sys.stderr.write"), self.assertRaises(SystemExit) as ctx:
            runner.main(["--case-id", "not-a-real-case"])
        self.assertEqual(ctx.exception.code, 2)

    def test_live_runner_rejects_case_id_with_all(self):
        runner = _load_module("madup_writing_stress_live_runner_ambiguous", LIVE_RUNNER_PATH)
        with mock.patch("sys.stderr.write"), self.assertRaises(SystemExit) as ctx:
            runner.main(["--case-id", "slack-001", "--all"])
        self.assertEqual(ctx.exception.code, 2)

    def test_live_runner_rejects_non_positive_timeout(self):
        runner = _load_module("madup_writing_stress_live_runner_timeout_guard", LIVE_RUNNER_PATH)
        with mock.patch("sys.stderr.write"), self.assertRaises(SystemExit) as ctx:
            runner.main(["--timeout-seconds", "0"])
        self.assertEqual(ctx.exception.code, 2)

    def test_live_runner_rejects_confirmation_flag_outside_full_execution(self):
        runner = _load_module("madup_writing_stress_live_runner_confirm_guard", LIVE_RUNNER_PATH)
        with mock.patch("sys.stderr.write"), self.assertRaises(SystemExit) as ctx:
            runner.main(["--confirm-all-1000"])
        self.assertEqual(ctx.exception.code, 2)

    def test_live_runner_blocks_api_key_billing_without_explicit_opt_in(self):
        runner = _load_module("madup_writing_stress_live_runner_api_guard", LIVE_RUNNER_PATH)

        with mock.patch.dict(runner.os.environ, {"ANTHROPIC_API_KEY": "secret"}, clear=True):
            with mock.patch.object(runner, "execute_plan") as execute_mock:
                with mock.patch("sys.stderr.write"), self.assertRaises(SystemExit) as ctx:
                    runner.main(["--execute", "--case-id", "slack-001"])

        self.assertEqual(ctx.exception.code, 2)
        execute_mock.assert_not_called()

    def test_live_runner_blocks_auth_token_billing_without_explicit_opt_in(self):
        runner = _load_module("madup_writing_stress_live_runner_auth_token_guard", LIVE_RUNNER_PATH)

        with mock.patch.dict(runner.os.environ, {"ANTHROPIC_AUTH_TOKEN": "secret"}, clear=True):
            with mock.patch.object(runner, "execute_plan") as execute_mock:
                with mock.patch("sys.stderr.write"), self.assertRaises(SystemExit) as ctx:
                    runner.main(["--execute", "--case-id", "slack-001"])

        self.assertEqual(ctx.exception.code, 2)
        execute_mock.assert_not_called()

    def test_subscription_auth_preflight_rejects_api_key_helper(self):
        runner = _load_module("madup_writing_stress_live_runner_auth_preflight", LIVE_RUNNER_PATH)
        completed = subprocess.CompletedProcess(
            args=["claude", "auth", "status"],
            returncode=0,
            stdout=json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "apiKeySource": "apiKeyHelper",
                    "subscriptionType": None,
                }
            ),
            stderr="",
        )

        with mock.patch.object(runner.subprocess, "run", return_value=completed):
            failure = runner._subscription_auth_failure()

        self.assertIn("non-subscription credential", failure)

    def test_subscription_auth_preflight_accepts_claude_ai_subscription(self):
        runner = _load_module("madup_writing_stress_live_runner_subscription_preflight", LIVE_RUNNER_PATH)
        completed = subprocess.CompletedProcess(
            args=["claude", "auth", "status"],
            returncode=0,
            stdout=json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": "max",
                }
            ),
            stderr="",
        )

        with mock.patch.object(runner.subprocess, "run", return_value=completed):
            self.assertIsNone(runner._subscription_auth_failure())

    def test_subscription_auth_preflight_accepts_login_managed_subscription_key(self):
        runner = _load_module("madup_writing_stress_live_runner_managed_subscription", LIVE_RUNNER_PATH)
        completed = subprocess.CompletedProcess(
            args=["claude", "auth", "status"],
            returncode=0,
            stdout=json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "apiKeySource": "/login managed key",
                    "subscriptionType": "max",
                }
            ),
            stderr="",
        )

        with mock.patch.object(runner.subprocess, "run", return_value=completed):
            self.assertIsNone(runner._subscription_auth_failure())

    def test_live_runner_exposes_no_api_billing_escape_hatch(self):
        runner = _load_module("madup_writing_stress_live_runner_no_api_escape", LIVE_RUNNER_PATH)

        self.assertNotIn("--allow-api-billing", runner.build_parser().format_help())

    def test_live_runner_rejects_empty_plan_and_nonempty_output_directory(self):
        runner = _load_module("madup_writing_stress_live_runner_output_guard", LIVE_RUNNER_PATH)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "empty plan"):
                runner.execute_plan({"cases": []}, output_root=root / "empty")

            occupied = root / "occupied"
            occupied.mkdir()
            (occupied / "prior-run.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not empty"):
                runner.execute_plan(
                    runner.build_plan(case_id="slack-001"),
                    output_root=occupied,
                )

    def test_live_runner_counterbalances_arm_order_by_case(self):
        runner = _load_module("madup_writing_stress_live_runner_order", LIVE_RUNNER_PATH)

        self.assertEqual(runner._arm_sequence(0), ("with_plugin", "without_plugin"))
        self.assertEqual(runner._arm_sequence(1), ("without_plugin", "with_plugin"))

    def test_live_runner_supports_case_id_and_limit_dry_plan(self):
        runner = _load_module("madup_writing_stress_live_runner_case", LIVE_RUNNER_PATH)
        with mock.patch("sys.stdout.write") as write_mock:
            exit_code = runner.main(["--case-id", "slack-001", "--limit", "1"])

        self.assertEqual(exit_code, 0)
        rendered = "".join(call.args[0] for call in write_mock.call_args_list)
        payload = json.loads(rendered)
        self.assertEqual(payload["summary"]["planned_case_count"], 1)
        self.assertEqual(payload["cases"][0]["id"], "slack-001")

    def test_live_runner_execute_writes_per_run_artifacts_without_network(self):
        runner = _load_module("madup_writing_stress_live_runner_execute", LIVE_RUNNER_PATH)
        snapshot_cases = _load_snapshot()
        case = next(item for item in snapshot_cases if item["id"] == "slack-001")
        response_json = json.dumps({"result": runner.oracle.render_reference(case), "usage": {"inputTokens": 1, "outputTokens": 2}})

        class FakeCompletedProcess:
            def __init__(self, stdout: str, stderr: str, returncode: int):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        observed_timeouts = []

        def fake_run(argv, cwd, capture_output, text, check, timeout):
            observed_timeouts.append(timeout)
            debug_file = Path(argv[argv.index("--debug-file") + 1])
            if "--plugin-dir" in argv:
                debug_file.write_text(
                    "Loaded inline plugin from path: madup-writing\n"
                    "SkillTool returning 3 newMessages for skill madup-writing\n",
                    encoding="utf-8",
                )
            else:
                debug_file.write_text("No inline plugin loaded\n", encoding="utf-8")
            return FakeCompletedProcess(response_json, "", 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "out"
            with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
                summary = runner.execute_plan(
                    runner.build_plan(case_id="slack-001", limit=1, include_all=False),
                    output_root=output_dir,
                )

            self.assertEqual(observed_timeouts, [180, 180])
            self.assertEqual(summary["failed_runs"], 0)
            self.assertEqual(summary["execution_failed_runs"], 0)
            self.assertEqual(summary["oracle_failed_runs"], 0)
            self.assertEqual(summary["invocation_failed_runs"], 0)
            self.assertTrue(summary["release_passed"])
            self.assertEqual(summary["oracle_contract"]["schema_version"], "1.1.0")
            self.assertEqual(summary["pair_outcomes"]["both_passed"], 1)
            self.assertTrue((output_dir / "plan.json").exists())
            run_dir = output_dir / "slack-001" / "with_plugin"
            self.assertTrue((run_dir / "response.md").exists())
            self.assertTrue((run_dir / "raw_result.json").exists())
            self.assertTrue((run_dir / "timing.json").exists())
            self.assertTrue((run_dir / "oracle_result.json").exists())
            self.assertTrue((run_dir / "debug.log").exists())

    def test_live_runner_counts_oracle_failures_without_treating_baseline_as_release_blocker(self):
        runner = _load_module("madup_writing_stress_live_runner_oracle_gate", LIVE_RUNNER_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "slack-001")
        mutant = runner.oracle.make_locked_token_mutant(case, runner.oracle.render_reference(case))
        response_json = json.dumps({"result": mutant})

        def fake_run(argv, cwd, capture_output, text, check, timeout):
            debug_file = Path(argv[argv.index("--debug-file") + 1])
            debug_text = (
                "Loaded inline plugin from path: madup-writing\n"
                "SkillTool returning result for skill madup-writing\n"
                if "--plugin-dir" in argv
                else ""
            )
            debug_file.write_text(debug_text, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, response_json, "")

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
                summary = runner.execute_plan(
                    runner.build_plan(case_id="slack-001"),
                    output_root=Path(temp_dir) / "out",
                )

        self.assertEqual(summary["oracle_failed_runs"], 2)
        self.assertEqual(summary["with_plugin_oracle_failed_runs"], 1)
        self.assertEqual(summary["baseline_oracle_failed_runs"], 1)
        self.assertEqual(summary["failed_runs"], 1)
        self.assertFalse(summary["release_passed"])
        self.assertEqual(summary["pair_outcomes"]["both_failed"], 1)

    def test_live_runner_baseline_only_oracle_failure_is_a_plugin_contract_win(self):
        runner = _load_module("madup_writing_stress_live_runner_baseline", LIVE_RUNNER_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "slack-001")
        reference = runner.oracle.render_reference(case)
        mutant = runner.oracle.make_locked_token_mutant(case, reference)

        def fake_run(argv, cwd, capture_output, text, check, timeout):
            debug_file = Path(argv[argv.index("--debug-file") + 1])
            with_plugin = "--plugin-dir" in argv
            debug_file.write_text(
                (
                    "Loaded inline plugin from path: madup-writing\n"
                    "SkillTool returning result for skill madup-writing\n"
                    if with_plugin
                    else ""
                ),
                encoding="utf-8",
            )
            response = reference if with_plugin else mutant
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": response}), "")

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
                summary = runner.execute_plan(
                    runner.build_plan(case_id="slack-001"),
                    output_root=Path(temp_dir) / "out",
                )

        self.assertEqual(summary["oracle_failed_runs"], 1)
        self.assertEqual(summary["with_plugin_oracle_failed_runs"], 0)
        self.assertEqual(summary["baseline_oracle_failed_runs"], 1)
        self.assertEqual(summary["failed_runs"], 0)
        self.assertTrue(summary["release_passed"])
        self.assertEqual(summary["pair_outcomes"]["with_plugin_won"], 1)

    def test_live_runner_counts_missing_and_unexpected_invocation_evidence(self):
        runner = _load_module("madup_writing_stress_live_runner_invocation_gate", LIVE_RUNNER_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "slack-001")
        response_json = json.dumps({"result": runner.oracle.render_reference(case)})

        def fake_run(argv, cwd, capture_output, text, check, timeout):
            debug_file = Path(argv[argv.index("--debug-file") + 1])
            debug_text = (
                ""
                if "--plugin-dir" in argv
                else "Loaded inline plugin from path: madup-writing\n"
                "SkillTool returning result for skill madup-writing\n"
            )
            debug_file.write_text(debug_text, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, response_json, "")

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
                summary = runner.execute_plan(
                    runner.build_plan(case_id="slack-001"),
                    output_root=Path(temp_dir) / "out",
                )

        self.assertEqual(summary["invocation_failed_runs"], 2)
        self.assertEqual(summary["failed_runs"], 2)
        self.assertFalse(summary["release_passed"])
        self.assertEqual(summary["pair_outcomes"]["invalid_pairs"], 1)

    def test_live_runner_times_out_and_records_execution_failures(self):
        runner = _load_module("madup_writing_stress_live_runner_timeout", LIVE_RUNNER_PATH)

        def fake_run(argv, cwd, capture_output, text, check, timeout):
            raise subprocess.TimeoutExpired(argv, timeout, output="partial", stderr="timed out")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "out"
            with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
                summary = runner.execute_plan(
                    runner.build_plan(case_id="slack-001"),
                    output_root=output_dir,
                    timeout_seconds=3,
                )

            raw = json.loads(
                (output_dir / "slack-001" / "with_plugin" / "raw_result.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["execution_failed_runs"], 2)
        self.assertEqual(summary["oracle_failed_runs"], 0)
        self.assertEqual(summary["invocation_failed_runs"], 0)
        self.assertEqual(summary["failed_runs"], 2)
        self.assertFalse(summary["release_passed"])
        self.assertEqual(summary["pair_outcomes"]["invalid_pairs"], 1)
        self.assertTrue(raw["timed_out"])
        self.assertIn("timed out after 3 seconds", raw["parse_error"])

    def test_live_runner_records_missing_claude_binary_as_execution_failure(self):
        runner = _load_module("madup_writing_stress_live_runner_missing_cli", LIVE_RUNNER_PATH)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "out"
            with mock.patch.object(
                runner.subprocess,
                "run",
                side_effect=FileNotFoundError("claude executable not found"),
            ):
                summary = runner.execute_plan(
                    runner.build_plan(case_id="slack-001"),
                    output_root=output_dir,
                )
            raw = json.loads(
                (output_dir / "slack-001" / "with_plugin" / "raw_result.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["execution_failed_runs"], 2)
        self.assertEqual(summary["failed_runs"], 2)
        self.assertIn("could not start", raw["parse_error"])


class TestMadupWritingStressSuiteLiveRegrader(unittest.TestCase):
    def test_regrader_uses_current_case_contract_and_preserves_original_artifacts(self):
        regrader = _load_module("madup_writing_stress_live_regrader", LIVE_REGRADER_PATH)
        current_case = next(item for item in _load_snapshot() if item["id"] == "email-002")
        legacy_case = dict(current_case)
        legacy_case.pop("context_tokens")
        legacy_case["locked_tokens"] = [
            "광고주",
            "월간 정리",
            "소재 교체 일정",
            "회신",
            "현우",
            "다음 주 화요일",
            "8월 5일",
        ]
        with_plugin_response = (
            "소재 교체 일정 관련 내부 회신을 오늘 마쳤습니다. "
            "최종본은 현우님이 다음 주 화요일(8월 5일) 오전까지 전달드릴 예정입니다."
        )
        baseline_response = f"{with_plugin_response} 이번 주 안에 다시 확인하겠습니다."
        original_result = {
            "oracle_result": {
                "passed": False,
                "hard_failures": [{"check": "locked_tokens"}],
            },
            "oracle_evaluated": True,
            "debug_evidence": {},
            "invocation_result": {"evaluated": True, "passed": True, "reasons": []},
            "execution_succeeded": True,
            "release_failed": True,
            "arm": "with_plugin",
            "case_id": "email-002",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir) / "source"
            output_root = Path(temp_dir) / "regraded"
            source_root.mkdir()
            (source_root / "plan.json").write_text(
                json.dumps({"cases": [legacy_case]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (source_root / "summary.json").write_text("{}", encoding="utf-8")
            for arm, response in (
                ("with_plugin", with_plugin_response),
                ("without_plugin", baseline_response),
            ):
                arm_root = source_root / "email-002" / arm
                arm_root.mkdir(parents=True)
                (arm_root / "response.md").write_text(response, encoding="utf-8")
                payload = dict(original_result, arm=arm)
                (arm_root / "oracle_result.json").write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )

            original_before = (
                source_root / "email-002" / "with_plugin" / "oracle_result.json"
            ).read_text(encoding="utf-8")
            summary = regrader.regrade_run(source_root, output_root=output_root)
            original_after = (
                source_root / "email-002" / "with_plugin" / "oracle_result.json"
            ).read_text(encoding="utf-8")
            regraded_plugin = json.loads(
                (output_root / "email-002" / "with_plugin" / "oracle_result.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(original_before, original_after)
        self.assertTrue(regraded_plugin["oracle_result"]["passed"])
        self.assertEqual(
            regraded_plugin["source_artifact"],
            "email-002/with_plugin/oracle_result.json",
        )
        self.assertEqual(summary["planned_case_count"], 1)
        self.assertEqual(summary["regraded_runs"], 2)
        self.assertEqual(summary["pair_outcomes"]["with_plugin_won"], 1)
        self.assertEqual(summary["with_plugin_oracle_failed_runs"], 0)
        self.assertEqual(summary["baseline_oracle_failed_runs"], 1)
        self.assertTrue(summary["release_passed"])
        self.assertEqual(summary["oracle_contract"]["schema_version"], "1.1.0")
        self.assertEqual(summary["oracle_contract"]["generator_version"], "1.3.0")
        self.assertEqual(len(summary["oracle_contract"]["snapshot_sha256"]), 64)
        self.assertEqual(summary["oracle_contract"]["oracle_version"], "1.4.0")
        self.assertEqual(len(summary["oracle_contract"]["oracle_sha256"]), 64)
        self.assertEqual(summary["source_run_id"], "source")
        self.assertNotIn(str(Path(temp_dir).resolve()), json.dumps(summary))

    def test_regrader_rejects_incomplete_source_run(self):
        regrader = _load_module("madup_writing_stress_live_regrader_incomplete", LIVE_REGRADER_PATH)
        case = next(item for item in _load_snapshot() if item["id"] == "slack-001")

        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir) / "source"
            source_root.mkdir()
            (source_root / "plan.json").write_text(
                json.dumps({"cases": [case]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "incomplete source run"):
                regrader.regrade_run(source_root, output_root=Path(temp_dir) / "regraded")


if __name__ == "__main__":
    unittest.main()
