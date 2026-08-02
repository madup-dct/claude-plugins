#!/usr/bin/env python3
"""Deterministic 1,000-case stress corpus generator for madup-writing."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path


SUITE_SEED = "madup-writing-stress-v1"
SCHEMA_VERSION = "1.1.0"
GENERATOR_VERSION = "1.3.0"
DEFAULT_SNAPSHOT_PATH = Path(__file__).resolve().parent / "golden_cases.jsonl"

MODE_SPECS = (
    {"mode": "slack_email", "submode": "slack", "count": 300, "prefix": "slack"},
    {"mode": "slack_email", "submode": "email", "count": 150, "prefix": "email"},
    {"mode": "report", "submode": "report", "count": 250, "prefix": "report"},
    {"mode": "proposal", "submode": "proposal", "count": 200, "prefix": "proposal"},
    {"mode": "presentation_copy", "submode": "presentation", "count": 100, "prefix": "presentation"},
)

SLACK_FACTS = ["결제 오류 2건", "랜딩 페이지 오류 3건", "소재 검수 이슈 2건", "태깅 누락 1건", "보고서 수치 불일치 2건"]
SLACK_CONTEXTS = ["QA", "리뷰", "운영 점검", "교차 확인"]
SLACK_NAMES = ["민지", "서준", "하린", "도윤", "지우", "유진"]
SLACK_WEEKDAYS = ["다음 주 화요일", "다음 주 수요일", "다음 주 목요일", "다음 주 금요일"]
SLACK_DATES = ["8월 4일", "8월 5일", "8월 6일", "8월 7일"]
SLACK_FOLLOWUPS = ["월요일 오전", "화요일 오후", "수요일 오전", "목요일 오후"]
SLACK_WORKSTREAMS = ["앱 결제", "회원가입", "브랜드 캠페인", "리포트 자동화", "소재 승인", "태그 정합성", "랜딩 속도", "CRM 연동", "검색어 관리", "대시보드 수치"]
SLACK_RELEASES = ["1차", "2차", "파일럿", "부분", "긴급"]

EMAIL_SUBJECTS = ["광고 계정 구조 개편안", "소재 교체 일정", "월간 리포트 초안", "검색어 제외 키워드 정리", "브랜드 캠페인 예산안"]
EMAIL_ACTIONS = ["검토", "확정", "확인", "회신", "정리"]
EMAIL_NAMES = ["민지", "현우", "수빈", "지안", "태윤", "예린"]
EMAIL_WEEKDAYS = ["다음 주 화요일", "다음 주 수요일", "다음 주 목요일", "다음 주 금요일"]
EMAIL_DATES = ["8월 4일", "8월 5일", "8월 6일", "8월 7일"]
EMAIL_AUDIENCES = ["광고주", "내부 운영팀", "세일즈팀", "디자인팀", "개발팀", "대표님", "캠페인 PM", "분석 담당자"]
EMAIL_CONTEXTS = ["월간 정리", "수정 요청", "최종 확인", "회신 요청", "검토 공유"]

REPORT_REVENUES = ["1억 2,400만 원", "9,800만 원", "8,600만 원", "1억 5,200만 원", "7,400만 원"]
REPORT_ROAS = ["412%", "385%", "436%", "398%", "447%"]
REPORT_CONVERSIONS = ["318건", "274건", "356건", "291건", "402건"]
REPORT_CTRS = ["0.8%p", "0.5%p", "1.1%p", "0.6%p", "0.9%p"]
REPORT_CPAS = ["12%", "9%", "15%", "11%", "8%"]
REPORT_WINDOWS = ["3일치 데이터", "4일치 데이터", "5일치 데이터"]
REPORT_VERTICALS = ["리테일", "교육", "보험", "앱 서비스"]

PROPOSAL_EVIDENCE = ["파일럿 3건", "파일럿 4건", "테스트 5건", "시범 운영 3건"]
PROPOSAL_CPA = ["18%", "16%", "21%", "14%"]
PROPOSAL_WINDOWS = ["6주", "5주", "8주", "7주"]
PROPOSAL_REDUCTIONS = ["20%", "18%", "22%", "15%"]
PROPOSAL_VERTICALS = ["이커머스", "B2B SaaS", "교육", "뷰티"]

PRESENTATION_METRICS = ["ROAS 412%", "CPA 18% 개선", "전환수 318건", "CTR 0.8%p 상승"]
PRESENTATION_CONTEXTS = ["첫 달 운영 기준", "보고용 핵심 수치", "제안서 첫 장 메시지", "캠페인 진단 한 줄"]
PRESENTATION_AUDIENCES = ["대표 보고", "광고주 공유", "주간 스탠드업", "제안 발표", "성과 회고"]
PRESENTATION_FRAMINGS = ["한 줄 요약", "첫 화면 메시지", "오프닝 카피", "설명 전 헤드라인", "숫자 강조 문구"]

PROMPT_OPENERS = {
    "slack": [
        "슬랙에 바로 붙여 넣을 운영 업데이트로 써줘.",
        "일정 변경을 팀 채널에 짧게 공유해줘.",
        "결정과 담당자가 먼저 보이는 슬랙 문안으로 정리해줘.",
        "아래 사실만 써서 한 단락 슬랙 공지를 만들어줘.",
        "진행 상황과 다음 액션이 한 번에 읽히는 슬랙 메시지로 바꿔줘.",
    ],
    "email": [
        "광고주에게 바로 보낼 짧은 이메일로 정리해줘.",
        "현재 상태가 첫 문장에 오는 정중한 메일을 써줘.",
        "아래 사실만 유지한 확인 메일 문안을 만들어줘.",
        "추가 약속 없이 간결한 업무 이메일로 바꿔줘.",
        "수신자가 바로 판단할 수 있는 메일 본문으로 써줘.",
    ],
    "report": [
        "광고주 보고용 요약을 4문장 안으로 써줘.",
        "한 줄 결론이 먼저 보이는 광고주 성과 요약으로 정리해줘.",
        "수치와 데이터 한계를 함께 보여주는 보고 문안으로 써줘.",
        "원인 추정 없이 현재 의미만 설명하는 보고 요약을 만들어줘.",
        "직접 측정 수치를 먼저 두는 광고주 보고 문장으로 정리해줘.",
    ],
    "proposal": [
        "광고주 제안서 카피로 다듬어줘.",
        "파일럿 근거가 먼저 보이는 제안 문안으로 써줘.",
        "한 줄 포지셔닝 뒤에 근거와 목표가 이어지는 제안 카피로 정리해줘.",
        "근거가 받치는 범위에서만 대비를 쓰는 제안 문안으로 바꿔줘.",
        "광고주가 바로 판단할 수 있는 헤드라인과 본문으로 써줘.",
    ],
    "presentation": [
        "발표 슬라이드 카피로 다듬어줘.",
        "소리 내어 읽히는 슬라이드 헤드라인으로 바꿔줘.",
        "핵심 수치가 먼저 들리는 발표 문안으로 써줘.",
        "근거가 있는 한 줄 대비로 슬라이드 메시지를 정리해줘.",
        "발표자가 그대로 읽을 수 있는 헤드라인과 보조 문장으로 써줘.",
    ],
}


def _seed_int(submode: str, index: int) -> int:
    digest = hashlib.sha256(f"{SUITE_SEED}:{submode}:{index}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _seed_hex(submode: str, index: int) -> str:
    return f"{_seed_int(submode, index):016x}"


def _mixed_pick(index: int, pools: list[list[str]], *, step: int) -> list[str]:
    space = math.prod(len(pool) for pool in pools)
    if math.gcd(step, space) != 1:
        raise ValueError(f"Mixed-radix step must be coprime with space: {space}")
    ordinal = ((index - 1) * step) % space
    picks: list[str] = []
    for pool in pools:
        picks.append(pool[ordinal % len(pool)])
        ordinal //= len(pool)
    return picks


def _forbidden(category: str, markers: list[str]) -> dict[str, object]:
    return {"category": category, "markers": markers}


def _last_hangul_has_final_consonant(text: str) -> bool | None:
    if not text:
        raise ValueError("text must not be empty")
    for character in reversed(text):
        codepoint = ord(character)
        if 0xAC00 <= codepoint <= 0xD7A3:
            return (codepoint - 0xAC00) % 28 != 0
    return None


def subject_particle(word: str) -> str:
    has_final_consonant = _last_hangul_has_final_consonant(word)
    return "이" if has_final_consonant is not False else "가"


def informal_copula(word: str) -> str:
    return "이야" if _last_hangul_has_final_consonant(word) else "야"


def topic_particle(word: str) -> str:
    return "은" if _last_hangul_has_final_consonant(word) else "는"


def _base_style() -> dict[str, object]:
    return {
        "advisory_rules": [
            "no_empty_transition",
            "no_inflated_claim",
            "no_translation_like_phrase",
            "no_excessive_formatting",
        ],
        "reuse_existing_checker": True,
        "does_not_prove_model_quality": True,
    }


def _build_slack_case(index: int) -> dict[str, object]:
    fact_token, context_token, name, weekday_token, date_token, followup_token, workstream, release_label = _mixed_pick(
        index,
        [
            SLACK_FACTS,
            SLACK_CONTEXTS,
            SLACK_NAMES,
            SLACK_WEEKDAYS,
            SLACK_DATES,
            SLACK_FOLLOWUPS,
            SLACK_WORKSTREAMS,
            SLACK_RELEASES,
        ],
        step=1283,
    )
    template_index = (index - 1) % 5
    opener = PROMPT_OPENERS["slack"][template_index]
    stratum = ["surface_lock_date", "invented_owner_risk", "invented_schedule_risk", "brevity_shape_risk", "anti_slop_risk"][template_index]
    prompt = (
        f"{opener} {workstream} 건인데, 어제 {context_token}에서 {fact_token}이 확인됐고, "
        f"오늘 범위를 줄여 {release_label} 배포를 {weekday_token}({date_token})로 미루기로 했어. "
        f"수정은 {name}{subject_particle(name)} 맡고, {followup_token}까지 테스트 결과를 다시 공유해야 해. 짧고 바로 읽히게 써줘."
    )
    return {
        "id": f"slack-{index:03d}",
        "mode": "slack_email",
        "submode": "slack",
        "template": f"slack_update_v{template_index + 1}",
        "seed": _seed_hex("slack", index),
        "prompt": prompt,
        "locked_tokens": [workstream, fact_token, release_label, weekday_token, date_token, name, followup_token],
        "context_tokens": [context_token],
        "forbidden_inventions": [
            _forbidden("schedule", ["8/4", "8/5", "8/6", "8/7", "다음주", "금주 내"]),
            _forbidden("owner", ["담당자는 제가", "다른 팀에서", "외주사가"]),
            _forbidden("report_promise", ["별도 보고드리겠습니다", "정리해서 다시 보고하겠습니다"]),
            _forbidden("budget_move", ["예산 재배분", "광고비를 옮기겠습니다"]),
            _forbidden("guarantee", ["보장", "확실히", "반드시 성공"]),
        ],
        "shape": {"max_sentences": 3, "requires_headline": False, "body_sentence_count": 0, "max_non_empty_lines": 4},
        "style": _base_style(),
        "stratum": stratum,
    }


def _build_email_case(index: int) -> dict[str, object]:
    subject_token, action_token, name, weekday_token, date_token, audience, email_context = _mixed_pick(
        index,
        [
            EMAIL_SUBJECTS,
            EMAIL_ACTIONS,
            EMAIL_NAMES,
            EMAIL_WEEKDAYS,
            EMAIL_DATES,
            EMAIL_AUDIENCES,
            EMAIL_CONTEXTS,
        ],
        step=641,
    )
    template_index = (index - 1) % 5
    opener = PROMPT_OPENERS["email"][template_index]
    stratum = ["surface_lock_name", "invented_report_promise_risk", "formal_register_risk", "invented_schedule_risk", "anti_slop_risk"][template_index]
    prompt = (
        f"{opener} {audience}에게 보내는 {email_context} 메일이야. "
        f"{subject_token}은 오늘 내부 {action_token}까지 마쳤고, {name}{subject_particle(name)} {weekday_token}({date_token}) 오전까지 최종본을 보낼 예정이야. "
        "추가 약속은 만들지 말고, 짧고 정중하게 써줘."
    )
    return {
        "id": f"email-{index:03d}",
        "mode": "slack_email",
        "submode": "email",
        "template": f"email_followup_v{template_index + 1}",
        "seed": _seed_hex("email", index),
        "prompt": prompt,
        "locked_tokens": [subject_token, name, weekday_token, date_token],
        "context_tokens": [audience, email_context, action_token],
        "forbidden_inventions": [
            _forbidden("schedule", ["8/4", "8/5", "8/6", "8/7", "이번 주 안", "내일 중"]),
            _forbidden("owner", ["운영팀 전체", "외부 파트너가"]),
            _forbidden("report_promise", ["주간 리포트로", "월간 보고서에서 다시"]),
            _forbidden("budget_move", ["예산을 조정", "광고비를 이동"]),
            _forbidden("guarantee", ["보장", "문제없습니다", "확실하게"]),
        ],
        "shape": {"max_sentences": 4, "requires_headline": False, "body_sentence_count": 0, "max_non_empty_lines": 6},
        "style": _base_style(),
        "stratum": stratum,
    }


def _build_report_case(index: int) -> dict[str, object]:
    revenue, roas, conversions, ctr, cpa, data_window, vertical = _mixed_pick(
        index,
        [REPORT_REVENUES, REPORT_ROAS, REPORT_CONVERSIONS, REPORT_CTRS, REPORT_CPAS, REPORT_WINDOWS, REPORT_VERTICALS],
        step=151,
    )
    template_index = (index - 1) % 5
    opener = PROMPT_OPENERS["report"][template_index]
    stratum = ["surface_lock_metric", "invented_report_promise_risk", "one_line_framing", "decision_gate_boundary", "evidence_tiering"][template_index]
    prompt = (
        f"{opener} 아래 수치만 근거로 하고, 없는 후속 조치나 결정 기준은 만들지 마. {vertical} 광고주의 7월 검색광고 매출은 {revenue}, "
        f"ROAS는 {roas}, 전환수는 {conversions}이야. 브랜드 캠페인은 전주 대비 CTR이 {ctr} 올랐고, "
        f"일반 캠페인은 CPA가 {cpa} 낮아졌어. 다만 신규 소재는 아직 {data_window}뿐이야. 원인 분석을 지어내지 말고, 해석은 숫자 범위 안에서만 해줘."
    )
    return {
        "id": f"report-{index:03d}",
        "mode": "report",
        "submode": "report",
        "template": f"report_summary_v{template_index + 1}",
        "seed": _seed_hex("report", index),
        "prompt": prompt,
        "locked_tokens": [revenue, roas, conversions, ctr, cpa, data_window.removesuffix(" 데이터")],
        "context_tokens": [vertical],
        "forbidden_inventions": [
            _forbidden("schedule", ["다음 주", "다음달", "이번 주 후반"]),
            _forbidden("owner", ["민지가", "담당자가", "운영팀이 다시"]),
            _forbidden("report_promise", ["별도 보고드리겠습니다", "추가 리포트로 공유드리겠습니다"]),
            _forbidden("budget_move", ["예산을 확대", "예산 재배분", "광고비를 옮기겠습니다"]),
            _forbidden("guarantee", ["보장", "확실합니다", "성과가 이어질 것입니다"]),
            _forbidden("meta_scaffolding", ["정리했습니다", "작성했습니다", "report 모드", "리포트 모드"]),
            _forbidden("unsupported_evaluation", ["안정적인 성과", "견조한 성과", "모두 개선", "전반적으로 개선", "성과를 유지"]),
        ],
        "shape": {
            "max_sentences": 4 if template_index == 0 else 8,
            "requires_headline": False,
            "body_sentence_count": 0,
            "max_non_empty_lines": 5 if template_index == 0 else 8,
        },
        "style": _base_style(),
        "stratum": stratum,
    }


def _build_proposal_case(index: int) -> dict[str, object]:
    evidence_token, cpa_token, week_token, reduction_token, vertical = _mixed_pick(
        index,
        [PROPOSAL_EVIDENCE, PROPOSAL_CPA, PROPOSAL_WINDOWS, PROPOSAL_REDUCTIONS, PROPOSAL_VERTICALS],
        step=5,
    )
    template_index = (index - 1) % 5
    opener = PROMPT_OPENERS["proposal"][template_index]
    stratum = ["evidence_first_risk", "budget_move_risk", "controlled_contrast", "one_line_positioning", "evidence_tiering"][template_index]
    prompt = (
        f"{opener} {vertical} 광고주용이고, 근거는 유지해야 해. "
        f"사실은 이거야: 첫 달에는 검색광고 구조 개편과 소재 테스트를 같이 돌리고, {week_token} 안에 비효율 키워드 {reduction_token}를 줄이는 게 목표야. "
        f"지난 {evidence_token}에서는 동일한 운영 방식으로 평균 CPA를 {cpa_token} 낮췄어. 헤드라인 1개와 본문 3문장만 줘."
    )
    return {
        "id": f"proposal-{index:03d}",
        "mode": "proposal",
        "submode": "proposal",
        "template": f"proposal_copy_v{template_index + 1}",
        "seed": _seed_hex("proposal", index),
        "prompt": prompt,
        "locked_tokens": ["첫 달", "검색광고 구조 개편", "소재 테스트", week_token, reduction_token, evidence_token, cpa_token],
        "context_tokens": [vertical],
        "forbidden_inventions": [
            _forbidden("schedule", ["런칭 일정", "배포 일정", "주간 보고 약속"]),
            _forbidden("owner", ["담당자는", "누가 책임지고"]),
            _forbidden("report_promise", ["매주 보고드리겠습니다", "월간 보고를 약속드립니다"]),
            _forbidden("budget_move", ["예산 재배분", "재배분", "광고비를 성과가 나는 캠페인으로", "예산을 옮기겠습니다"]),
            _forbidden("guarantee", ["보장", "확실히 달성", "반드시 개선"]),
        ],
        "shape": {"max_sentences": 4, "requires_headline": True, "body_sentence_count": 3, "max_non_empty_lines": 4},
        "style": _base_style(),
        "stratum": stratum,
    }


def _build_presentation_case(index: int) -> dict[str, object]:
    metric_token, context_token, audience, framing = _mixed_pick(
        index,
        [PRESENTATION_METRICS, PRESENTATION_CONTEXTS, PRESENTATION_AUDIENCES, PRESENTATION_FRAMINGS],
        step=7,
    )
    template_index = (index - 1) % 5
    opener = PROMPT_OPENERS["presentation"][template_index]
    stratum = ["read_aloud_risk", "controlled_contrast", "surface_lock_metric", "audience_view", "shape_compression_risk"][template_index]
    prompt = (
        f"{opener} {audience}용 {framing}{informal_copula(framing)}. {context_token}에 들어갈 짧은 헤드라인과 보조 문장 1개만 필요해. "
        f"{metric_token}{topic_particle(metric_token)} 그대로 두고, 문학적인 슬로건이나 과장 표현은 빼줘."
    )
    return {
        "id": f"presentation-{index:03d}",
        "mode": "presentation_copy",
        "submode": "presentation",
        "template": f"presentation_copy_v{template_index + 1}",
        "seed": _seed_hex("presentation", index),
        "prompt": prompt,
        "locked_tokens": [metric_token],
        "context_tokens": [audience, framing, context_token],
        "forbidden_inventions": [
            _forbidden("schedule", ["다음 주", "이번 분기"]),
            _forbidden("owner", ["담당자는", "운영팀이"]),
            _forbidden("report_promise", ["추가 보고드리겠습니다", "별도 리포트"]),
            _forbidden("budget_move", ["예산 재배분", "광고비 이동"]),
            _forbidden("guarantee", ["보장", "확실한 성공", "반드시"]),
        ],
        "shape": {"max_sentences": 2, "requires_headline": True, "body_sentence_count": 1, "max_non_empty_lines": 2},
        "style": _base_style(),
        "stratum": stratum,
    }


BUILDERS = {
    "slack": _build_slack_case,
    "email": _build_email_case,
    "report": _build_report_case,
    "proposal": _build_proposal_case,
    "presentation": _build_presentation_case,
}


def generate_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for spec in MODE_SPECS:
        builder = BUILDERS[spec["submode"]]
        for index in range(1, spec["count"] + 1):
            case = builder(index)
            assert case["mode"] == spec["mode"]
            assert case["submode"] == spec["submode"]
            cases.append(case)
    return cases


def write_snapshot(path: Path = DEFAULT_SNAPSHOT_PATH) -> Path:
    path.write_text("\n".join(json.dumps(case, ensure_ascii=False) for case in generate_cases()) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic madup-writing stress cases.")
    parser.add_argument("--write-snapshot", action="store_true", help="Write golden_cases.jsonl.")
    parser.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT_PATH, help="Snapshot output path.")
    args = parser.parse_args(argv)

    if args.write_snapshot:
        write_snapshot(args.output.resolve())
        return 0

    sys.stdout.write(json.dumps(generate_cases(), ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
