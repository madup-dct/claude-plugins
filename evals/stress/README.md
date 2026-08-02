# Madup Writing Stress Suite

This directory contains a deterministic contract corpus for `madup-writing`. It is a tracked, reproducible fixture set, not a live model benchmark.

## What This Is

- A deterministic contract suite of exactly 1,000 scenarios.
- A schema-checked snapshot with fixed ids, prompts, locked output tokens, non-output context tokens, forbidden invention categories, shape rules, style rules, and strata.
- A validator that checks hard contract failures such as locked-token drift, unsupplied business commitments, and output-shape violations.
- A deterministic 96-case stratified sample plan for optional live `claude -p` runs outside tracked tests.

## What This Is Not

- This deterministic contract suite does not prove model quality by itself.
- It is not a substitute for actual `claude -p` model evaluations.
- Passing the corpus only proves that the scenario definitions, snapshot, selector, and oracle stay stable.
- Marker-based invention checks can produce false positives when a safe phrase overlaps a blocked phrase, and false negatives when a model invents the same business move without using one of the tracked markers.

## Release Gates

Release gates are split on purpose:

1. Deterministic contract gate
   - Snapshot count is exactly 1,000.
   - Mode and submode distributions stay fixed.
   - Generator reproduces the tracked snapshot byte-for-byte at the case-object level.
   - Oracle self-tests pass on gold responses and deliberate mutants.
2. Live model gate
   - Run the tracked 96-case stratified sample with `claude -p` separately.
   - Treat any locked-token drift or unsupplied schedule, owner, report promise, budget move, or guarantee as a release blocker. Audience, request-context, and framing labels remain covered by the corpus but are not forced into the output.
   - Advisory style findings are signals, not proof of quality.
   - Re-review false positive and false negative marker behavior when adding or removing forbidden markers.

## Corpus Shape

- Slack: 300
- Email: 150
- Report: 250
- Proposal: 200
- Presentation: 100

## Files

- `generator.py` builds the deterministic case list.
- `golden_cases.jsonl` is the tracked snapshot.
- `oracle.py` validates schema, hard constraints, and advisory style findings.
- `sample_selector.py` returns the fixed 96-case release sample.
- `live_runner.py` builds dry plans by default and can execute a live sampled run when requested.
- `regrade_live_run.py` applies the current tracked oracle to a completed run without overwriting its original evidence.
- `manifest.json` locks the suite metadata and counts.

## Live Runner

Default behavior is dry-plan only. It does not execute `claude -p` unless `--execute` is present.
Live execution requires a working Claude Code subscription OAuth session. Before any call, the runner checks `claude auth status` and requires `authMethod: claude.ai` with an active subscription.
With that subscription authentication, calls consume Claude subscription usage limits rather than API pay-as-you-go credits.
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and configured credential helpers such as `apiKeyHelper` can replace subscription authentication. The runner blocks every detected non-subscription credential, reports only its source, never its value, and exposes no override flag. This guard cannot be bypassed from the runner.
The runner verifies that generated cases still match the tracked `golden_cases.jsonl` snapshot before it plans any call.

Dry plan for the deterministic sample:

```bash
python3 evals/stress/live_runner.py
```

Single-case smoke plan:

```bash
python3 evals/stress/live_runner.py --case-id slack-001 --limit 1
```

Execute the default deterministic sample:

```bash
python3 evals/stress/live_runner.py --execute
```

The default sample is 96 cases x 2 arms = 192 `claude -p` calls and consumes subscription limits. Treat it as a real usage and latency event, not as a local unit test.

Full 1,000-case execution is intentionally guarded:

```bash
python3 evals/stress/live_runner.py --execute --all --confirm-all-1000
```

Without `--confirm-all-1000`, `--execute --all` hard-stops.
The full corpus evaluates 1,000 scenarios across both arms, so it makes 2,000 subscription calls.

Each execution writes to a fresh timestamped directory below ignored `madup-writing-workspace/stress-live/`. The run contains `plan.json`, a summary, and per-arm response, raw-result, timing, debug-evidence, and oracle artifacts. These files can contain prompts and model output; review them before sharing and do not add them to git.

Each call has a 180-second timeout by default. Override it with `--timeout-seconds`, for example:

```bash
python3 evals/stress/live_runner.py --execute --case-id slack-001 --timeout-seconds 240
```

The order of the two arms is counterbalanced by case. A valid `with_plugin` run must prove both inline-plugin loading and exact `madup-writing` skill invocation. A valid `without_plugin` run must prove neither occurred. Command/JSON failures, timeouts, and invocation contamination invalidate a pair. Oracle failures are counted for both arms, but a baseline-only oracle failure is a contract win for the plugin rather than a release blocker.

Live runner execution artifacts are separate from the deterministic oracle. The oracle checks contract compliance; a live run still does not prove overall writing quality by itself.

## Regrading A Completed Run

When the tracked oracle contract changes, regrade saved responses instead of spending another 192 calls. The command writes a fresh timestamped directory inside the source run and leaves every original artifact untouched:

```bash
python3 evals/stress/regrade_live_run.py madup-writing-workspace/stress-live/run-YYYYMMDDTHHMMSSffffffZ
```

Regrading maps cases by stable id to the current tracked snapshot. It refuses incomplete source runs, missing response or invocation artifacts, and non-empty output directories.
