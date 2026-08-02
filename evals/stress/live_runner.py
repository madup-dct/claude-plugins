#!/usr/bin/env python3
"""Dry-plan and execution helper for live madup-writing stress evaluations."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "madup-writing"
DEBUG_PLUGIN_PATTERN = re.compile(
    r"(?:^|\[DEBUG\]\s+)(?:loaded|enabled)\s+inline plugin\b.*\bmadup-writing\b",
    re.IGNORECASE,
)
DEBUG_SKILL_PATTERN = re.compile(
    r"SkillTool returning .*?\bskill madup-writing\b", re.IGNORECASE
)
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "madup-writing-workspace" / "stress-live"
DEFAULT_SAMPLE_SIZE = 96
DEFAULT_TIMEOUT_SECONDS = 180
ARMS = ("with_plugin", "without_plugin")
SNAPSHOT_PATH = ROOT / "golden_cases.jsonl"
API_BILLING_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
AUTH_STATUS_TIMEOUT_SECONDS = 10


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_module("madup_writing_stress_generator_live", ROOT / "generator.py")
oracle = _load_module("madup_writing_stress_oracle_live", ROOT / "oracle.py")
sample_selector = _load_module("madup_writing_stress_sample_selector_live", ROOT / "sample_selector.py")


def load_cases() -> list[dict[str, object]]:
    snapshot: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(
        SNAPSHOT_PATH.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        payload = json.loads(raw_line)
        if not isinstance(payload, dict):
            raise ValueError(f"Tracked snapshot line {line_number} is not an object")
        snapshot.append(payload)

    generated = generator.generate_cases()
    if generated != snapshot:
        raise ValueError(
            "Generated stress cases differ from the tracked snapshot; regenerate and review golden_cases.jsonl first"
        )
    return snapshot


def contract_metadata() -> dict[str, str]:
    return {
        "schema_version": generator.SCHEMA_VERSION,
        "generator_version": generator.GENERATOR_VERSION,
        "oracle_version": oracle.ORACLE_VERSION,
        "snapshot_sha256": hashlib.sha256(SNAPSHOT_PATH.read_bytes()).hexdigest(),
        "oracle_sha256": hashlib.sha256((ROOT / "oracle.py").read_bytes()).hexdigest(),
    }


def build_plan(
    *,
    case_id: str | None = None,
    limit: int | None = None,
    include_all: bool = False,
) -> dict[str, object]:
    if case_id is not None and include_all:
        raise ValueError("--case-id cannot be combined with --all")
    if limit is not None and limit <= 0:
        raise ValueError("--limit must be greater than zero")

    cases = load_cases()
    if case_id is not None:
        selected = [case for case in cases if case["id"] == case_id]
        if not selected:
            raise ValueError(f"Unknown case id: {case_id}")
        sample_source = "single_case"
    elif include_all:
        selected = cases
        sample_source = "all_1000"
    else:
        selected = sample_selector.select_cases(cases)
        if len(selected) != DEFAULT_SAMPLE_SIZE:
            raise ValueError(
                f"Expected {DEFAULT_SAMPLE_SIZE} sampled cases, got {len(selected)}"
            )
        sample_source = "deterministic_96_sample"
    if limit is not None:
        selected = selected[:limit]
    return {
        "mode": "dry_plan",
        "contract": contract_metadata(),
        "include_all": include_all,
        "sample_source": sample_source,
        "arms": list(ARMS),
        "cases": selected,
        "summary": {
            "planned_case_count": len(selected),
            "planned_call_count": len(selected) * len(ARMS),
        },
    }


def build_claude_argv(
    *, case: dict[str, object], arm: str, debug_log_path: Path
) -> list[str]:
    argv = [
        "claude",
        "-p",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--model",
        "sonnet",
        "--effort",
        "high",
        "--tools",
        "Skill",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--debug-file",
        str(debug_log_path),
    ]
    if arm == "with_plugin":
        argv.extend(["--plugin-dir", str(PLUGIN_DIR)])
    elif arm != "without_plugin":
        raise ValueError(f"Unsupported arm: {arm}")
    argv.append(str(case["prompt"]))
    return argv


def _classify_debug_evidence(debug_text: str) -> dict[str, object]:
    plugin_lines: list[str] = []
    skill_lines: list[str] = []
    for raw_line in debug_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if DEBUG_PLUGIN_PATTERN.search(line):
            plugin_lines.append(line)
        if DEBUG_SKILL_PATTERN.search(line):
            skill_lines.append(line)
    return {
        "plugin_loaded": bool(plugin_lines),
        "skill_invoked": bool(skill_lines),
        "plugin_lines": plugin_lines,
        "skill_lines": skill_lines,
    }


def _parse_stdout(stdout_text: str) -> tuple[dict[str, object] | None, str | None]:
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        return None, f"Malformed Claude JSON: {exc}"
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
        return None, "Malformed Claude JSON: missing string result"
    return payload, None


def _timing_payload(
    *,
    argv: list[str],
    wall_ms: int,
    return_code: int,
    parse_error: str | None,
    payload: dict[str, object] | None,
    timed_out: bool,
) -> dict[str, object]:
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    return {
        "command": argv,
        "return_code": return_code,
        "wall_ms": wall_ms,
        "success": return_code == 0 and parse_error is None and not timed_out,
        "timed_out": timed_out,
        "parse_error": parse_error,
        "usage": usage,
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _invocation_result(
    *, arm: str, debug_evidence: dict[str, object], execution_succeeded: bool
) -> dict[str, object]:
    if not execution_succeeded:
        return {
            "evaluated": False,
            "passed": False,
            "reasons": ["execution_not_succeeded"],
        }

    reasons: list[str] = []
    plugin_loaded = bool(debug_evidence["plugin_loaded"])
    skill_invoked = bool(debug_evidence["skill_invoked"])
    if arm == "with_plugin":
        if not plugin_loaded:
            reasons.append("plugin_load_not_proven")
        if not skill_invoked:
            reasons.append("skill_invocation_not_proven")
    elif arm == "without_plugin":
        if plugin_loaded:
            reasons.append("plugin_unexpectedly_loaded")
        if skill_invoked:
            reasons.append("skill_unexpectedly_invoked")
    else:
        raise ValueError(f"Unsupported arm: {arm}")
    return {"evaluated": True, "passed": not reasons, "reasons": reasons}


def _arm_sequence(case_index: int) -> tuple[str, str]:
    return ARMS if case_index % 2 == 0 else tuple(reversed(ARMS))


def _new_run_output_root(base_root: Path) -> Path:
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")
    return base_root / run_id


def _active_api_billing_overrides() -> list[str]:
    return [name for name in API_BILLING_ENV_VARS if os.environ.get(name)]


def _subscription_auth_failure() -> str | None:
    try:
        completed = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=AUTH_STATUS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "Claude Code authentication status check timed out"
    except OSError:
        return "Claude Code authentication status could not be checked"

    if completed.returncode != 0:
        return "Claude Code authentication status check failed"
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return "Claude Code authentication status was not valid JSON"
    if not isinstance(status, dict) or not status.get("loggedIn"):
        return "Claude Code is not logged in"

    auth_method = status.get("authMethod")
    api_key_source = status.get("apiKeySource")
    subscription_type = status.get("subscriptionType")
    if auth_method != "claude.ai" or not subscription_type:
        safe_details = [f"authMethod={auth_method or 'unknown'}"]
        if api_key_source:
            safe_details.append(f"apiKeySource={api_key_source}")
        return "non-subscription credential detected (" + ", ".join(safe_details) + ")"
    return None


def _empty_oracle_result(reason: str) -> dict[str, object]:
    return {
        "passed": False,
        "hard_failures": [{"check": "execution", "reason": reason}],
        "advisory_findings": [],
        "schema_errors": [],
    }


def execute_plan(
    plan: dict[str, object],
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    cases = plan.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Cannot execute an empty plan")
    if output_root.exists():
        if not output_root.is_dir():
            raise ValueError(f"Output path is not a directory: {output_root}")
        if any(output_root.iterdir()):
            raise ValueError(f"Output directory is not empty: {output_root}")

    output_root.mkdir(parents=True, exist_ok=True)
    _write_json(output_root / "plan.json", plan)
    failed_runs = 0
    executed_runs = 0
    execution_failed_runs = 0
    oracle_failed_runs = 0
    with_plugin_oracle_failed_runs = 0
    baseline_oracle_failed_runs = 0
    invocation_failed_runs = 0
    pair_outcomes = {
        "with_plugin_won": 0,
        "without_plugin_won": 0,
        "both_passed": 0,
        "both_failed": 0,
        "invalid_pairs": 0,
    }
    per_arm = {
        arm: {
            "executed_runs": 0,
            "execution_failed_runs": 0,
            "oracle_passed_runs": 0,
            "oracle_failed_runs": 0,
            "invocation_failed_runs": 0,
            "release_failed_runs": 0,
        }
        for arm in ARMS
    }

    for case_index, case in enumerate(cases):
        case_results: dict[str, dict[str, object]] = {}
        for arm in _arm_sequence(case_index):
            run_dir = output_root / str(case["id"]) / arm
            run_dir.mkdir(parents=True, exist_ok=True)
            debug_path = run_dir / "debug.log"
            _write_text(debug_path, "")
            argv = build_claude_argv(case=case, arm=arm, debug_log_path=debug_path)
            started = time.perf_counter()
            timed_out = False
            try:
                with tempfile.TemporaryDirectory(prefix="madup-writing-live-") as temp_cwd:
                    completed = subprocess.run(
                        argv,
                        cwd=temp_cwd,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=timeout_seconds,
                    )
                return_code = completed.returncode
                stdout_text = _coerce_text(completed.stdout)
                stderr_text = _coerce_text(completed.stderr)
                payload, parse_error = (
                    _parse_stdout(stdout_text)
                    if return_code == 0
                    else (None, f"Claude command failed with return code {return_code}")
                )
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                return_code = 124
                stdout_text = _coerce_text(exc.stdout)
                stderr_text = _coerce_text(exc.stderr)
                payload = None
                parse_error = f"Claude command timed out after {timeout_seconds} seconds"
            except OSError as exc:
                return_code = 127
                stdout_text = ""
                stderr_text = str(exc)
                payload = None
                parse_error = f"Claude command could not start: {exc}"
            wall_ms = int((time.perf_counter() - started) * 1000)
            result_text = payload.get("result", "") if payload else ""
            execution_succeeded = return_code == 0 and parse_error is None and not timed_out
            debug_text = (
                debug_path.read_text(encoding="utf-8", errors="replace")
                if debug_path.exists()
                else ""
            )
            debug_evidence = _classify_debug_evidence(debug_text)
            invocation_result = _invocation_result(
                arm=arm,
                debug_evidence=debug_evidence,
                execution_succeeded=execution_succeeded,
            )
            oracle_evaluated = execution_succeeded
            oracle_result = (
                oracle.evaluate_response(case, result_text)
                if oracle_evaluated
                else _empty_oracle_result("execution_not_succeeded")
            )

            execution_failed = not execution_succeeded
            oracle_failed = oracle_evaluated and not bool(oracle_result["passed"])
            invocation_failed = bool(invocation_result["evaluated"]) and not bool(
                invocation_result["passed"]
            )
            release_failed = (
                execution_failed
                or invocation_failed
                or (arm == "with_plugin" and oracle_failed)
            )
            if execution_failed:
                execution_failed_runs += 1
                per_arm[arm]["execution_failed_runs"] += 1
            if oracle_failed:
                oracle_failed_runs += 1
                per_arm[arm]["oracle_failed_runs"] += 1
                if arm == "with_plugin":
                    with_plugin_oracle_failed_runs += 1
                else:
                    baseline_oracle_failed_runs += 1
            elif oracle_evaluated:
                per_arm[arm]["oracle_passed_runs"] += 1
            if invocation_failed:
                invocation_failed_runs += 1
                per_arm[arm]["invocation_failed_runs"] += 1
            if release_failed:
                failed_runs += 1
                per_arm[arm]["release_failed_runs"] += 1
            executed_runs += 1
            per_arm[arm]["executed_runs"] += 1
            case_results[arm] = {
                "valid": execution_succeeded and not invocation_failed,
                "oracle_passed": oracle_evaluated and bool(oracle_result["passed"]),
            }
            _write_text(run_dir / "response.md", result_text)
            _write_json(
                run_dir / "raw_result.json",
                {
                    "argv": argv,
                    "return_code": return_code,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "parse_error": parse_error,
                    "timed_out": timed_out,
                },
            )
            _write_json(
                run_dir / "timing.json",
                _timing_payload(
                    argv=argv,
                    wall_ms=wall_ms,
                    return_code=return_code,
                    parse_error=parse_error,
                    payload=payload,
                    timed_out=timed_out,
                ),
            )
            _write_json(
                run_dir / "oracle_result.json",
                {
                    "oracle_result": oracle_result,
                    "oracle_evaluated": oracle_evaluated,
                    "debug_evidence": debug_evidence,
                    "invocation_result": invocation_result,
                    "execution_succeeded": execution_succeeded,
                    "release_failed": release_failed,
                    "arm": arm,
                    "case_id": case["id"],
                },
            )

        if not all(bool(case_results[arm]["valid"]) for arm in ARMS):
            pair_outcomes["invalid_pairs"] += 1
        else:
            with_passed = bool(case_results["with_plugin"]["oracle_passed"])
            without_passed = bool(case_results["without_plugin"]["oracle_passed"])
            if with_passed and without_passed:
                pair_outcomes["both_passed"] += 1
            elif with_passed:
                pair_outcomes["with_plugin_won"] += 1
            elif without_passed:
                pair_outcomes["without_plugin_won"] += 1
            else:
                pair_outcomes["both_failed"] += 1

    summary = {
        "oracle_contract": plan.get("contract", contract_metadata()),
        "planned_case_count": len(cases),
        "executed_runs": executed_runs,
        "failed_runs": failed_runs,
        "execution_failed_runs": execution_failed_runs,
        "oracle_failed_runs": oracle_failed_runs,
        "with_plugin_oracle_failed_runs": with_plugin_oracle_failed_runs,
        "baseline_oracle_failed_runs": baseline_oracle_failed_runs,
        "invocation_failed_runs": invocation_failed_runs,
        "release_passed": failed_runs == 0,
        "pair_outcomes": pair_outcomes,
        "per_arm": per_arm,
        "timeout_seconds": timeout_seconds,
        "output_root": str(output_root),
    }
    _write_json(output_root / "summary.json", summary)
    return summary


def _render_plan(plan: dict[str, object]) -> dict[str, object]:
    return {
        "mode": "dry_plan",
        "contract": plan["contract"],
        "sample_source": plan["sample_source"],
        "arms": plan["arms"],
        "summary": plan["summary"],
        "cases": [
            {"id": case["id"], "mode": case["mode"], "submode": case["submode"]}
            for case in plan["cases"]
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-plan or execute live madup-writing stress evaluations."
    )
    parser.add_argument("--case-id", help="Single case id to plan or execute.")
    parser.add_argument(
        "--limit", type=int, help="Limit number of planned cases after selection."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Use all 1000 deterministic cases instead of the default 96-case sample.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the selected plan. Default is dry-plan only.",
    )
    parser.add_argument(
        "--confirm-all-1000",
        action="store_true",
        help="Required with --execute --all to avoid accidental 2000-call runs.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Base directory for timestamped execution artifacts.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-call timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than zero")
    if args.case_id and args.all:
        parser.error("--case-id cannot be combined with --all")
    if args.execute and args.all and not args.confirm_all_1000:
        parser.error("--execute --all requires --confirm-all-1000")
    if args.confirm_all_1000 and not (args.execute and args.all):
        parser.error("--confirm-all-1000 is only valid with --execute --all")
    api_billing_overrides = _active_api_billing_overrides()
    if args.execute and api_billing_overrides:
        parser.error(
            "API-billing environment override detected "
            f"({', '.join(api_billing_overrides)}); unset it to use subscription OAuth"
        )
    if args.execute:
        auth_failure = _subscription_auth_failure()
        if auth_failure:
            parser.error(
                f"{auth_failure}; sign in with a Claude subscription"
            )

    try:
        plan = build_plan(case_id=args.case_id, limit=args.limit, include_all=args.all)
    except ValueError as exc:
        parser.error(str(exc))
    if args.execute:
        summary = execute_plan(
            plan,
            output_root=_new_run_output_root(args.output_root.resolve()),
            timeout_seconds=args.timeout_seconds,
        )
        sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return 0 if summary["failed_runs"] == 0 else 1

    sys.stdout.write(json.dumps(_render_plan(plan), ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
