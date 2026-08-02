#!/usr/bin/env python3
"""Deterministic validator/oracle for madup-writing stress cases."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ORACLE_VERSION = "1.5.0"
REQUIRED_KEYS = {
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
SENTENCE_SPLIT = re.compile(r"[.!?]\s+|[.!?](?=[A-Z가-힣])|(?<=[가-힣])[.!?](?=\d)|\n+")
NON_EMPTY_LINE = re.compile(r"\S")
CALENDAR_DATE = re.compile(r"(?<!\d)(?:\d{1,2}월\s*\d{1,2}일|\d{1,2}/\d{1,2})(?!\d)")
COMPARISON_PHRASE = re.compile(
    r"(?P<reference>전년|전월|전주|전일|전분기|이전|직전|목표|업계\s*평균|지난\s*분기|지난\s*달|지난\s*주|지난\s*해)\s*(?:대비|보다)"
)
POLITE_ENDING = re.compile(r"(?:습니다|니다|세요|어요|아요|여요|해요|이에요|예요|까요|시죠)")
BANMAL_FINAL_CHARS = "어아게줘봐야"
UNSUPPORTED_METRIC_CLAIM_PATTERNS = {
    "benchmark": re.compile(
        r"(?:업계|시장|동종\s*업계|경쟁사)\s*(?:평균|기준|벤치마크|수준)"
        r"|(?:평균|벤치마크)\s*(?:이상|이하|상회|하회|수준)"
    ),
    "cause": re.compile(
        r"(?:때문에|덕분에|로\s*인해|에\s*힘입어|[이가]\s*견인|[이가]\s*이끌|[을를]\s*통해)"
    ),
    "trend": re.compile(
        r"(?:상승세|하락세|증가세|감소세|개선세|회복세|성장세|추세|모멘텀|"
        r"이어지(?:고|며|는)|지속되(?:고|며|는))"
    ),
}
FIELD_LABEL_LINE = re.compile(
    r"^(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:제목|헤드라인|본문|보조\s*문장)\s*(?:\*\*|__)?\s*:?[：]?\s*$",
    re.IGNORECASE,
)
INLINE_FIELD_LABEL = re.compile(
    r"^(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:헤드라인|본문|보조\s*문장)\s*(?:\*\*|__)?\s*[:：]\s*(.+)$",
    re.IGNORECASE,
)
EMAIL_SUBJECT_LINE = re.compile(
    r"^(?:\*\*|__)?\s*(?:제목|subject)\s*(?:\*\*|__)?\s*[:：].+$",
    re.IGNORECASE,
)
EMAIL_SCAFFOLD_LINE = re.compile(
    r"^(?:(?:[^,\n]{1,30}(?:님|팀|여러분)),\s*)?"
    r"안녕하세요(?:,\s*[^.!?\n]{1,30}(?:님|팀|여러분))?[.!]?$"
    r"|^(?:감사합니다|감사드립니다)\s*[.!]?$"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_style_checker():
    checker_path = (
        ROOT.parent.parent
        / "plugins"
        / "madup-writing"
        / "skills"
        / "madup-writing"
        / "scripts"
        / "check_korean_style.py"
    )
    if not checker_path.exists():
        return None
    try:
        return _load_module("madup_writing_style_checker", checker_path).analyze_text
    except Exception:
        return None


STYLE_CHECKER = _load_style_checker()
GENERATOR = _load_module(
    "madup_writing_stress_generator_for_oracle", ROOT / "generator.py"
)


def _count_sentences(text: str) -> int:
    return len([chunk.strip() for chunk in SENTENCE_SPLIT.split(text.strip()) if chunk.strip()])


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if NON_EMPTY_LINE.search(line)]


def _shape_content_lines(text: str, submode: object) -> list[str]:
    content_lines: list[str] = []
    for line in _non_empty_lines(text):
        if FIELD_LABEL_LINE.fullmatch(line):
            continue
        if submode == "email" and (
            EMAIL_SUBJECT_LINE.fullmatch(line) or EMAIL_SCAFFOLD_LINE.fullmatch(line)
        ):
            continue
        inline_match = INLINE_FIELD_LABEL.fullmatch(line)
        content_lines.append(inline_match.group(1).strip() if inline_match else line)
    return content_lines


def _register_violation(lines: list[str]) -> bool:
    """True when content sentences end in banmal and no polite ending appears."""
    joined = "\n".join(lines)
    if POLITE_ENDING.search(joined):
        return False
    for line in lines:
        for chunk in SENTENCE_SPLIT.split(line):
            chunk = chunk.strip().rstrip(".!?…").rstrip()
            if chunk and chunk[-1] in BANMAL_FINAL_CHARS:
                return True
    return False


def _comparison_categories(text: str) -> set[str]:
    categories: set[str] = set()
    for match in COMPARISON_PHRASE.finditer(text):
        reference = re.sub(r"\s+", "", match.group("reference"))
        if reference in {"전주", "지난주"}:
            categories.add("week")
        elif reference in {"전월", "지난달"}:
            categories.add("month")
        elif reference in {"전분기", "지난분기"}:
            categories.add("quarter")
        elif reference in {"전년", "지난해"}:
            categories.add("year")
        elif reference == "전일":
            categories.add("day")
        elif reference == "업계평균":
            categories.add("industry_benchmark")
        else:
            categories.add(reference)
    return categories


def _unsupported_metric_claim_categories(text: str) -> set[str]:
    return {
        category
        for category, pattern in UNSUPPORTED_METRIC_CLAIM_PATTERNS.items()
        if pattern.search(text)
    }


def validate_case_schema(case: dict[str, object]) -> list[str]:
    errors: list[str] = []
    if set(case) != REQUIRED_KEYS:
        errors.append("required schema keys mismatch")
    if not isinstance(case.get("locked_tokens"), list) or not case.get("locked_tokens"):
        errors.append("locked_tokens must be a non-empty list")
    if not isinstance(case.get("context_tokens"), list) or not case.get("context_tokens"):
        errors.append("context_tokens must be a non-empty list")
    if isinstance(case.get("locked_tokens"), list) and isinstance(case.get("context_tokens"), list):
        overlap = sorted(set(case["locked_tokens"]) & set(case["context_tokens"]))
        if overlap:
            errors.append(f"locked_tokens and context_tokens overlap: {overlap}")
    if not isinstance(case.get("forbidden_inventions"), list) or not case.get("forbidden_inventions"):
        errors.append("forbidden_inventions must be a non-empty list")
    if not isinstance(case.get("shape"), dict):
        errors.append("shape must be an object")
    if not isinstance(case.get("style"), dict):
        errors.append("style must be an object")
    return errors


def render_reference(case: dict[str, object]) -> str:
    tokens = case["locked_tokens"]
    context_tokens = case["context_tokens"]
    submode = case["submode"]

    if submode == "slack":
        workstream, fact_token, release_label, weekday_token, date_token, name, followup_token = tokens
        (context_token,) = context_tokens
        return (
            f"{workstream} {context_token}에서 {fact_token}이 확인돼 {release_label} 배포는 {weekday_token}({date_token})로 미룹니다. "
            f"수정은 {name}{GENERATOR.subject_particle(name)} 맡고 {followup_token}까지 테스트 결과를 다시 공유하겠습니다."
        )
    if submode == "email":
        subject_token, name, weekday_token, date_token = tokens
        audience, email_context, action_token = context_tokens
        return (
            f"{audience} 대상 {email_context} 건입니다. {subject_token}은 오늘 내부 {action_token}까지 마쳤습니다. "
            f"{name}{GENERATOR.subject_particle(name)} {weekday_token}({date_token}) 오전까지 최종본을 보내겠습니다."
        )
    if submode == "report":
        revenue, roas, conversions, ctr, cpa, data_window_token = tokens
        (vertical,) = context_tokens
        return (
            f"{vertical} 광고주의 7월 검색광고 매출은 {revenue}, ROAS는 {roas}, 전환수는 {conversions}입니다. "
            f"브랜드 캠페인 CTR은 전주 대비 {ctr} 올랐고 일반 캠페인 CPA는 {cpa} 낮아졌습니다. "
            f"신규 소재는 아직 {data_window_token} 데이터여서 현재 확보된 수치만 공유합니다."
        )
    if submode == "proposal":
        first_month, structure_token, creative_token, week_token, reduction_token, evidence_token, cpa_token = tokens
        (vertical,) = context_tokens
        return (
            f"{vertical} {evidence_token} 평균 CPA {cpa_token} 개선을 {first_month} 운영 기준으로 먼저 확인합니다\n"
            f"{evidence_token}에서 평균 CPA를 {cpa_token} 낮춘 운영 방식을 먼저 확인했습니다. "
            f"{first_month}에는 {structure_token}과 {creative_token}를 함께 진행합니다. "
            f"{week_token} 안에 비효율 키워드 {reduction_token}를 줄이는 목표를 같은 방식으로 검증하겠습니다."
        )
    if submode == "presentation":
        (metric_token,) = tokens
        audience, framing, context_token = context_tokens
        return f"{audience} {framing}: {context_token}에 {metric_token}를 바로 보여줍니다\n{metric_token}를 첫 문장에 두고 설명은 한 줄만 붙입니다."
    raise ValueError(f"Unsupported submode: {submode}")


def _replacement_for_token(token: str) -> str:
    if re.search(r"\d+월\s*\d+일", token):
        return "8/99"
    if "%" in token or "건" in token or "원" in token or "주" in token:
        return "수치 변경"
    return "다른 값"


def make_locked_token_mutant(case: dict[str, object], response_text: str) -> str:
    token = case["locked_tokens"][0]
    replacement = _replacement_for_token(token)
    if token not in response_text:
        raise ValueError(f"Reference response is missing locked token: {token}")
    return response_text.replace(token, replacement)


def make_first_forbidden_marker_mutant(case: dict[str, object], response_text: str) -> str:
    for rule in case["forbidden_inventions"]:
        markers = rule.get("markers", [])
        if markers:
            return f"{response_text} {markers[0]}"
    raise ValueError(f"No forbidden marker configured for {case['id']}")


def _locked_token_preserved(
    case: dict[str, object], token_index: int, token: object, response_text: str
) -> bool:
    if not isinstance(token, str):
        return False
    if case.get("submode") == "slack" and token_index == 1:
        return all(part in response_text for part in token.split())
    return token in response_text


def evaluate_response(case: dict[str, object], response_text: str) -> dict[str, object]:
    hard_failures: list[dict[str, object]] = []
    advisory_findings: list[dict[str, object]] = []
    schema_errors = validate_case_schema(case)
    if schema_errors:
        hard_failures.append({"check": "schema", "details": schema_errors})

    missing_tokens = [
        token
        for token_index, token in enumerate(case.get("locked_tokens", []))
        if not _locked_token_preserved(case, token_index, token, response_text)
    ]
    if missing_tokens:
        hard_failures.append({"check": "locked_tokens", "missing_tokens": missing_tokens})

    for rule in case.get("forbidden_inventions", []):
        markers = rule.get("markers", []) if isinstance(rule, dict) else []
        hits = [marker for marker in markers if marker in response_text]
        if hits:
            hard_failures.append({"check": "forbidden_inventions", "category": rule.get("category"), "markers": hits})

    supplied_dates = set(CALENDAR_DATE.findall(str(case.get("prompt", ""))))
    unsupplied_dates = sorted(
        set(CALENDAR_DATE.findall(response_text)) - supplied_dates
    )
    if unsupplied_dates:
        hard_failures.append(
            {"check": "unsupplied_dates", "dates": unsupplied_dates}
        )

    supplied_comparisons = _comparison_categories(str(case.get("prompt", "")))
    unsupplied_comparisons = sorted(
        _comparison_categories(response_text) - supplied_comparisons
    )
    if unsupplied_comparisons:
        hard_failures.append(
            {
                "check": "unsupplied_comparisons",
                "categories": unsupplied_comparisons,
            }
        )

    if case.get("submode") in {"presentation", "report"}:
        supplied_metric_claims = _unsupported_metric_claim_categories(
            str(case.get("prompt", ""))
        )
        unsupplied_metric_claims = sorted(
            _unsupported_metric_claim_categories(response_text)
            - supplied_metric_claims
        )
        if unsupplied_metric_claims:
            hard_failures.append(
                {
                    "check": "unsupplied_metric_claims",
                    "categories": unsupplied_metric_claims,
                }
            )

    shape = case.get("shape", {})
    lines = _shape_content_lines(response_text, case.get("submode"))
    shape_text = "\n".join(lines)
    sentence_count = _count_sentences(shape_text)
    max_sentences = shape.get("max_sentences", 999)
    if sentence_count > max_sentences:
        hard_failures.append({"check": "shape", "reason": "max_sentences", "actual": sentence_count, "expected": max_sentences})
    max_non_empty_lines = shape.get("max_non_empty_lines", 999)
    if len(lines) > max_non_empty_lines:
        hard_failures.append({"check": "shape", "reason": "max_non_empty_lines", "actual": len(lines), "expected": max_non_empty_lines})
    if shape.get("requires_headline"):
        if len(lines) < 2:
            hard_failures.append({"check": "shape", "reason": "requires_headline"})
        body_expected = shape.get("body_sentence_count", 0)
        body_text = "\n".join(lines[1:]) if len(lines) > 1 else ""
        body_count = _count_sentences(body_text) if body_text else 0
        if body_expected and body_count != body_expected:
            hard_failures.append({"check": "shape", "reason": "body_sentence_count", "actual": body_count, "expected": body_expected})

    submode = case.get("submode")
    if submode in {"email", "slack"} and _register_violation(lines):
        register_record = {
            "check": "register",
            "reason": "banmal_without_polite_ending",
        }
        if submode == "email":
            hard_failures.append(register_record)
        else:
            advisory_findings.append(
                {
                    "rule": "register_risk",
                    "severity": "medium",
                    "line": 0,
                    "text": lines[0] if lines else "",
                    "suggestion": "격식 문안인데 반말 종결이 섞였습니다. 요청된 레지스터를 유지하세요.",
                }
            )

    for line in _non_empty_lines(response_text):
        if FIELD_LABEL_LINE.fullmatch(line) or INLINE_FIELD_LABEL.fullmatch(line) or (
            submode == "email"
            and (EMAIL_SUBJECT_LINE.fullmatch(line) or EMAIL_SCAFFOLD_LINE.fullmatch(line))
        ):
            advisory_findings.append(
                {
                    "rule": "scaffolding_normalized",
                    "severity": "low",
                    "line": 0,
                    "text": line,
                    "suggestion": "요청에 없는 라벨/인사 스캐폴딩입니다. 계약상 요청 시에만 넣으세요.",
                }
            )

    if STYLE_CHECKER is not None:
        advisory_findings.extend(STYLE_CHECKER(response_text))

    return {
        "passed": not hard_failures,
        "hard_failures": hard_failures,
        "advisory_findings": advisory_findings,
        "schema_errors": schema_errors,
    }


def run_self_tests() -> bool:
    generator = _load_module("madup_writing_stress_generator_runtime", ROOT / "generator.py")
    for case in generator.generate_cases()[:5]:
        reference = render_reference(case)
        assert evaluate_response(case, reference)["passed"]
        assert not evaluate_response(case, make_locked_token_mutant(case, reference))["passed"]
        assert not evaluate_response(case, make_first_forbidden_marker_mutant(case, reference))["passed"]
    assert _count_sentences("공유드립니다.QA에서 확인했습니다.1차 배포는 미룹니다.") == 3
    assert _register_violation(["예산안 검토 마쳤어", "내일 다시 공유할게"])
    assert not _register_violation(["예산안 검토를 마쳤습니다.", "내일 다시 공유하겠습니다."])
    return True


def main() -> int:
    return 0 if run_self_tests() else 1


if __name__ == "__main__":
    raise SystemExit(main())
