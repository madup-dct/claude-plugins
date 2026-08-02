#!/usr/bin/env python3
"""Advisory checker for common Korean business-writing style smells.

Paired quote spans ("...", '...', “...”, ‘...’, 「...」, 『...』) are masked
before pattern matching: quoted wording is a locked fact under the skill
contract, so the checker never advises rewriting it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


EMPTY_TRANSITION_PATTERNS = (
    re.compile(r"^\s*(먼저|우선),?\s*결론부터 말씀드리겠습니다(?:[.!?]\s*.*)?$"),
    re.compile(r"^\s*결론부터 말씀드리면[,\s]"),
    re.compile(r"^\s*먼저 말씀드리면[,\s]"),
)

INFLATED_CLAIM_PATTERNS = (
    re.compile(r"업계를\s*선도할"),
    re.compile(r"혁신적(?:인|으로)?"),
    re.compile(r"시너지를\s*극대화"),
    re.compile(r"한\s*단계\s*끌어올리"),
    re.compile(r"압도적(?:인|으로)?"),
    re.compile(r"획기적(?:인|으로)?"),
    re.compile(r"시사하는\s*바"),
    re.compile(r"주목할\s*만"),
    re.compile(r"지금이야말로"),
)

TRANSLATION_LIKE_PATTERNS = (
    re.compile(r"본\s*프로젝트를\s*통해"),
    re.compile(r"본\s*제안(?:은|안은)"),
    re.compile(r"(?:가치|인사이트|시사점|경험|솔루션|가능성|기회)[을를]?\s*제공했"),
    re.compile(r"기반을\s*마련"),
    re.compile(r"(?:되어|돼)(?:진다|집니다|집니까|질|졌|지고|지며)"),
    re.compile(r"보여집니"),
)

EXCESSIVE_FORMATTING_PATTERNS = (
    re.compile(r"^\s*#{2,}\s*[^#\s].*?\s*#{2,}\s*$"),
    re.compile(r"[!?]{3,}"),
    re.compile(r"(?:[*_~]){3,}"),
)

CANNED_STRUCTURE_PATTERNS = (
    re.compile(r"(?:단순한|단순히|그저|한낱)\s+[^\n.!?]{0,24}(?:이|가)\s*아니라"),
    re.compile(r"^\s*(?:결론적으로|종합하면|요약하자면)[,\s]"),
)

SENTENCE_INITIAL_CONNECTIVE = re.compile(r"^\s*(?:또한|그리고|하지만|따라서)[,\s]")

QUOTED_SPAN_PATTERNS = (
    re.compile(r"\"[^\"\n]*\""),
    re.compile(r"'[^'\n]*'"),
    re.compile(r"“[^”\n]*”"),
    re.compile(r"‘[^’\n]*’"),
    re.compile(r"「[^」\n]*」"),
    re.compile(r"『[^』\n]*』"),
)


def _mask_quoted_spans(line: str) -> str:
    """Blank out paired quote spans so locked quotations are never flagged."""
    masked = line
    for pattern in QUOTED_SPAN_PATTERNS:
        masked = pattern.sub(lambda match: " " * len(match.group(0)), masked)
    return masked


def _make_finding(rule: str, severity: str, line_no: int, text: str, suggestion: str) -> dict[str, object]:
    return {
        "rule": rule,
        "severity": severity,
        "line": line_no,
        "text": text,
        "suggestion": suggestion,
    }


def _is_regular_markdown_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if re.fullmatch(r"#{1,6}\s+\S.*", stripped) and not re.search(r"\s#{1,6}\s*$", stripped):
        return True
    if stripped in ("```", "~~~"):
        return True
    return False


def analyze_text(text: str) -> list[dict[str, object]]:
    """Return candidate style findings without claiming authorship."""

    findings: list[dict[str, object]] = []
    connective_hits: list[tuple[int, str]] = []
    enumeration_hit: tuple[int, str] | None = None

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        scan_line = _mask_quoted_spans(line)

        if any(pattern.search(scan_line) for pattern in EMPTY_TRANSITION_PATTERNS):
            findings.append(
                _make_finding(
                    "empty_transition",
                    "high",
                    line_no,
                    line,
                    "첫 문장을 배경 설명 대신 현재 사실, 결정, 요청으로 바로 시작하세요.",
                )
            )

        if any(pattern.search(scan_line) for pattern in INFLATED_CLAIM_PATTERNS):
            findings.append(
                _make_finding(
                    "inflated_claim",
                    "medium",
                    line_no,
                    line,
                    "과장 표현을 빼고 근거가 되는 사실이나 관찰을 먼저 쓰세요. 광고주가 확정한 카피나 인용이면 그대로 두세요.",
                )
            )

        if any(pattern.search(scan_line) for pattern in TRANSLATION_LIKE_PATTERNS):
            findings.append(
                _make_finding(
                    "translation_like_phrase",
                    "medium",
                    line_no,
                    line,
                    "번역투 관용구 대신 주어와 동사가 드러나는 자연스러운 문장으로 바꾸세요.",
                )
            )

        if (
            not _is_regular_markdown_line(line)
            and any(pattern.search(scan_line) for pattern in EXCESSIVE_FORMATTING_PATTERNS)
        ):
            findings.append(
                _make_finding(
                    "excessive_formatting",
                    "medium",
                    line_no,
                    line,
                    "강조 기호를 줄이고 문장 자체가 전달하게 두세요.",
                )
            )

        if any(pattern.search(scan_line) for pattern in CANNED_STRUCTURE_PATTERNS):
            findings.append(
                _make_finding(
                    "canned_structure",
                    "medium",
                    line_no,
                    line,
                    "기계적인 구조(부정 병렬, 상투적 마무리)를 풀고 내용이 앞서는 문장으로 쓰세요.",
                )
            )

        if enumeration_hit is None and "첫째," in scan_line:
            enumeration_hit = (line_no, line)

        if SENTENCE_INITIAL_CONNECTIVE.search(scan_line):
            connective_hits.append((line_no, line))

    if enumeration_hit is not None and "둘째," in text:
        findings.append(
            _make_finding(
                "canned_structure",
                "medium",
                enumeration_hit[0],
                enumeration_hit[1],
                "첫째/둘째/셋째 기계 나열 대신 내용 순서가 스스로 드러나는 문장으로 쓰세요.",
            )
        )

    if len(connective_hits) >= 2:
        findings.append(
            _make_finding(
                "sentence_initial_connectives",
                "medium",
                connective_hits[0][0],
                connective_hits[0][1],
                "문장 첫머리 접속사를 줄이고 문장 순서로 흐름을 보여주세요.",
            )
        )

    findings.sort(key=lambda item: item["line"])
    return findings


def _format_text(findings: list[dict[str, object]]) -> str:
    if not findings:
        return "No findings."

    lines = []
    for finding in findings:
        lines.append(
            "[{severity}] line {line} {rule}: {text}\n  -> {suggestion}".format(**finding)
        )
    return "\n".join(lines)


def _read_input(path_arg: str | None) -> str:
    if path_arg:
        return Path(path_arg).read_text(encoding="utf-8")
    return sys.stdin.read()


def _input_error_message(path_arg: str | None, exc: OSError | UnicodeDecodeError) -> str:
    target = path_arg or "stdin"
    if isinstance(exc, UnicodeDecodeError):
        detail = "Invalid UTF-8"
    else:
        detail = exc.strerror or exc.__class__.__name__
    return f"Cannot read input: {target} ({detail})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report advisory Korean business-writing style candidates."
    )
    parser.add_argument("path", nargs="?", help="Optional UTF-8 text file to inspect.")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format. Defaults to json.",
    )
    args = parser.parse_args(argv)

    try:
        text = _read_input(args.path)
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"{_input_error_message(args.path, exc)}\n")
        return 2

    findings = analyze_text(text)

    if args.format == "text":
        output = _format_text(findings)
    else:
        output = json.dumps(findings, ensure_ascii=False, indent=2)

    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")

    return 1 if any(item["severity"] == "high" for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
