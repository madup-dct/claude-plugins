#!/usr/bin/env python3
"""Regrade a completed live run with the current tracked oracle contract."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ARMS = ("with_plugin", "without_plugin")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


live_runner = _load_module(
    "madup_writing_stress_live_runner_regrade", ROOT / "live_runner.py"
)
oracle = live_runner.oracle


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read JSON artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact is not an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _default_output_root(source_root: Path) -> Path:
    run_id = datetime.now(timezone.utc).strftime("regrade-%Y%m%dT%H%M%S%fZ")
    return source_root / run_id


def _validate_source_run(
    source_root: Path, current_cases: dict[str, dict[str, object]]
) -> list[str]:
    if not (source_root / "summary.json").is_file():
        raise ValueError("incomplete source run: summary.json is missing")
    plan = _load_json(source_root / "plan.json")
    planned_cases = plan.get("cases")
    if not isinstance(planned_cases, list) or not planned_cases:
        raise ValueError("incomplete source run: plan contains no cases")

    case_ids: list[str] = []
    missing: list[str] = []
    for planned_case in planned_cases:
        if not isinstance(planned_case, dict) or not isinstance(planned_case.get("id"), str):
            raise ValueError("incomplete source run: plan contains an invalid case")
        case_id = planned_case["id"]
        if case_id not in current_cases:
            raise ValueError(f"Current tracked corpus does not contain case: {case_id}")
        case_ids.append(case_id)
        for arm in ARMS:
            arm_root = source_root / case_id / arm
            for filename in ("response.md", "oracle_result.json"):
                if not (arm_root / filename).is_file():
                    missing.append(f"{case_id}/{arm}/{filename}")
    if missing:
        preview = ", ".join(missing[:4])
        raise ValueError(f"incomplete source run: missing artifacts: {preview}")
    return case_ids


def regrade_run(
    source_root: Path, *, output_root: Path | None = None
) -> dict[str, object]:
    source_root = source_root.resolve()
    current_cases = {
        str(case["id"]): case for case in live_runner.load_cases()
    }
    case_ids = _validate_source_run(source_root, current_cases)
    output_root = (
        output_root.resolve() if output_root is not None else _default_output_root(source_root)
    )
    if output_root.exists():
        if not output_root.is_dir():
            raise ValueError(f"Output path is not a directory: {output_root}")
        if any(output_root.iterdir()):
            raise ValueError(f"Output directory is not empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    failed_runs = 0
    regraded_runs = 0
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
            "regraded_runs": 0,
            "execution_failed_runs": 0,
            "oracle_passed_runs": 0,
            "oracle_failed_runs": 0,
            "invocation_failed_runs": 0,
            "release_failed_runs": 0,
        }
        for arm in ARMS
    }

    for case_id in case_ids:
        case = current_cases[case_id]
        case_results: dict[str, dict[str, bool]] = {}
        for arm in ARMS:
            source_arm_root = source_root / case_id / arm
            original = _load_json(source_arm_root / "oracle_result.json")
            response_text = (source_arm_root / "response.md").read_text(
                encoding="utf-8", errors="replace"
            )
            execution_succeeded = bool(original.get("execution_succeeded"))
            invocation_result = original.get("invocation_result")
            if not isinstance(invocation_result, dict):
                raise ValueError(
                    f"Invalid invocation_result in {case_id}/{arm}/oracle_result.json"
                )
            invocation_failed = bool(invocation_result.get("evaluated")) and not bool(
                invocation_result.get("passed")
            )
            oracle_evaluated = execution_succeeded
            oracle_result = (
                oracle.evaluate_response(case, response_text)
                if oracle_evaluated
                else live_runner._empty_oracle_result("execution_not_succeeded")
            )
            execution_failed = not execution_succeeded
            oracle_failed = oracle_evaluated and not bool(oracle_result["passed"])
            release_failed = (
                execution_failed
                or invocation_failed
                or (arm == "with_plugin" and oracle_failed)
            )

            regraded_runs += 1
            per_arm[arm]["regraded_runs"] += 1
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

            case_results[arm] = {
                "valid": execution_succeeded and not invocation_failed,
                "oracle_passed": oracle_evaluated and bool(oracle_result["passed"]),
            }
            _write_json(
                output_root / case_id / arm / "oracle_result.json",
                {
                    "oracle_result": oracle_result,
                    "oracle_evaluated": oracle_evaluated,
                    "invocation_result": invocation_result,
                    "execution_succeeded": execution_succeeded,
                    "release_failed": release_failed,
                    "arm": arm,
                    "case_id": case_id,
                    "source_artifact": str(
                        (source_arm_root / "oracle_result.json").relative_to(source_root)
                    ),
                },
            )

        if not all(case_results[arm]["valid"] for arm in ARMS):
            pair_outcomes["invalid_pairs"] += 1
        else:
            with_passed = case_results["with_plugin"]["oracle_passed"]
            without_passed = case_results["without_plugin"]["oracle_passed"]
            if with_passed and without_passed:
                pair_outcomes["both_passed"] += 1
            elif with_passed:
                pair_outcomes["with_plugin_won"] += 1
            elif without_passed:
                pair_outcomes["without_plugin_won"] += 1
            else:
                pair_outcomes["both_failed"] += 1

    summary = {
        "source_run_id": source_root.name,
        "output_root": os.path.relpath(output_root, start=source_root),
        "oracle_contract": live_runner.contract_metadata(),
        "planned_case_count": len(case_ids),
        "regraded_runs": regraded_runs,
        "failed_runs": failed_runs,
        "execution_failed_runs": execution_failed_runs,
        "oracle_failed_runs": oracle_failed_runs,
        "with_plugin_oracle_failed_runs": with_plugin_oracle_failed_runs,
        "baseline_oracle_failed_runs": baseline_oracle_failed_runs,
        "invocation_failed_runs": invocation_failed_runs,
        "release_passed": failed_runs == 0,
        "pair_outcomes": pair_outcomes,
        "per_arm": per_arm,
    }
    _write_json(output_root / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Regrade a completed live run with the current tracked oracle."
    )
    parser.add_argument("source_run", type=Path, help="Completed live-run directory.")
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Fresh output directory. Defaults to a timestamped directory inside the source run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = regrade_run(args.source_run, output_root=args.output_root)
    except ValueError as exc:
        parser.error(str(exc))
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return 0 if summary["release_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
