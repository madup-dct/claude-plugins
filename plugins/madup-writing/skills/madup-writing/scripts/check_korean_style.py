#!/usr/bin/env python3
"""Advisory checker for common Korean business-writing style smells."""

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
)

TRANSLATION_LIKE_PATTERNS = (
    re.compile(r"본\s*프로젝트를\s*통해"),
    re.compile(r"본\s*제안(?:은|안은)"),
    re.compile(r"제공했(?:습니다|고)"),
    re.compile(r"기반을\s*마련"),
)

EXCESSIVE_FORMATTING_PATTERNS = (
    re.compile(r"^\s*#{2,}\s*[^#\s].*?\s*#{2,}\s*$"),
    re.compile(r"[!?.]{3,}"),
    re.compile(r"(?:[*_~]){3,}"),
)


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

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if any(pattern.search(line) for pattern in EMPTY_TRANSITION_PATTERNS):
            findings.append(
                _make_finding(
                    "empty_transition",
                    "high",
                    line_no,
                    line,
                    "첫 문장을 배경 설명 대신 현재 사실, 결정, 요청으로 바로 시작하세요.",
                )
            )

        if any(pattern.search(line) for pattern in INFLATED_CLAIM_PATTERNS):
            findings.append(
                _make_finding(
                    "inflated_claim",
                    "medium",
                    line_no,
                    line,
                    "과장 표현을 빼고 근거가 되는 사실이나 관찰을 먼저 쓰세요.",
                )
            )

        if any(pattern.search(line) for pattern in TRANSLATION_LIKE_PATTERNS):
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
            and any(pattern.search(line) for pattern in EXCESSIVE_FORMATTING_PATTERNS)
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
