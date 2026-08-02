# Madup Writing Plugin Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a tested `madup-writing` plugin to the Madup Claude marketplace for natural Korean Slack messages, reports, proposals, and presentation copy.

**Architecture:** Keep the runtime surface small: one skill selects a writing mode, loads one-level-deep reference material, drafts or rewrites, and runs a dependency-free candidate checker when text is available as a file. Package it as an independent marketplace plugin with the repository's existing privacy-preserving usage beacon. Preserve `humanize-korean` and `copy-tone-gate` as specialized plugins.

**Tech Stack:** Claude Code plugin manifests, Markdown skills, Python 3 standard library, `unittest`, local Claude CLI A/B runs, JSON evaluation artifacts.

---

### Task 1: Lock the plugin contract with failing tests

**Files:**
- Create: `tests/test_madup_writing_plugin.py`
- Create: `evals/evals.json`

**Step 1: Write the failing structure tests**

Create `unittest` coverage that expects:

- `plugins/madup-writing/.claude-plugin/plugin.json` with name `madup-writing`, version `1.0.0`, and MADUP DCT author metadata.
- `plugins/madup-writing/skills/madup-writing/SKILL.md` with only `name` and `description` in YAML frontmatter.
- A matching `madup-writing` entry and version in `.claude-plugin/marketplace.json`.
- A README installation row containing `/plugin install madup-writing@madup`.
- Hook metadata and beacon constants that match the plugin name and version.
- Reference and checker paths named by the design.

Add behavior tests that import `check_korean_style.py` and assert that `analyze_text()`:

- flags empty transitions, inflated claims, translation-like phrases, and excessive formatting;
- does not label text as AI-authored;
- returns no high-confidence finding for a concise factual update;
- emits stable JSON-serializable findings with `rule`, `severity`, `line`, `text`, and `suggestion`.

**Step 2: Write three evaluation prompts**

Store realistic Korean prompts in `evals/evals.json`:

1. Turn a delayed project update into a Slack message with facts, decision, owner, and due date.
2. Turn campaign metrics into a client-facing performance report summary without inventing causes.
3. Rewrite proposal headline and body copy into advertiser language while preserving evidence.

Each case records an expected outcome and objective checks for fact preservation, action clarity, and forbidden wording.

**Step 3: Run tests to verify RED**

Run:

```bash
python3 -m unittest tests/test_madup_writing_plugin.py -v
```

Expected: FAIL because the `madup-writing` plugin and checker do not exist.

**Step 4: Commit tests**

Commit only the contract tests and evaluation definitions with a Lore-formatted message that records the RED result.

### Task 2: Capture the no-skill Claude baseline

**Files:**
- Create: `madup-writing-workspace/iteration-1/<case>/without_skill/outputs/response.md`
- Create: `madup-writing-workspace/iteration-1/<case>/eval_metadata.json`
- Create: `madup-writing-workspace/iteration-1/<case>/without_skill/timing.json`

**Step 1: Run all baseline cases before writing the skill**

Use fresh Claude CLI calls without `--plugin-dir`. Run the three cases independently and save their final text plus timing data.

**Step 2: Record observed failures verbatim**

Summarize concrete baseline defects in each case metadata: generic opening, inflated claim, unnecessary headings, vague action, invented causal explanation, or awkward presentation language. Do not infer authorship from style.

**Step 3: Keep evaluation artifacts out of the plugin package**

Add `madup-writing-workspace/` to `.gitignore`; only commit `evals/evals.json`, not generated model outputs.

### Task 3: Implement the minimal writing skill and checker

**Files:**
- Create: `plugins/madup-writing/skills/madup-writing/SKILL.md`
- Create: `plugins/madup-writing/skills/madup-writing/references/voice-guide.md`
- Create: `plugins/madup-writing/skills/madup-writing/references/examples.md`
- Create: `plugins/madup-writing/skills/madup-writing/scripts/check_korean_style.py`

**Step 1: Write the skill metadata and body**

Use name `madup-writing`. The description starts with `Use when` and covers Korean Slack, email, reports, proposals, presentation copy, executive summaries, drafting, and rewriting. Keep the body concise and instruct Claude to:

1. lock facts and requested action;
2. choose the smallest matching mode;
3. draft with concrete subject and verb;
4. run a single anti-slop pass;
5. return ready-to-use copy first.

**Step 2: Write the voice guide**

Document shared principles and four modes. Explain when formality is appropriate and why short text should not be forced into headings or bullets. Include proposal-specific evidence-before-claim and presentation read-aloud checks.

**Step 3: Write anonymized examples**

Include one strong before/after example per mode. Preserve realistic Korean rhythm but exclude real client names, confidential metrics, private messages, and copied company prose.

**Step 4: Implement the checker**

Use Python standard library only. Provide:

```python
def analyze_text(text: str) -> list[dict[str, object]]:
    """Return candidate style findings without claiming authorship."""
```

Add a CLI that reads a file or stdin, prints JSON by default, supports `--format text`, exits `0` when no high-severity candidates exist, and exits `1` when high-severity candidates exist. Findings are advisory and never rewrite input.

**Step 5: Run unit tests to verify GREEN**

Run:

```bash
python3 -m unittest tests/test_madup_writing_plugin.py -v
```

Expected: checker behavior tests pass; packaging tests may still fail until Task 4.

### Task 4: Package the Claude marketplace plugin

**Files:**
- Create: `plugins/madup-writing/.claude-plugin/plugin.json`
- Create: `plugins/madup-writing/hooks/beacon.sh`
- Create: `plugins/madup-writing/hooks/hooks.json`
- Modify: `.claude-plugin/marketplace.json`
- Modify: `README.md`
- Modify: `.gitignore`

**Step 1: Add plugin metadata**

Mirror the repository's existing manifest convention with version `1.0.0` and MADUP DCT author metadata.

**Step 2: Reuse the telemetry contract**

Copy the existing beacon structure, changing only `SKILL_NAME` and `VER`. Preserve its privacy contract: skill name, plugin version, and anonymous UID only; never prompt, file, or task content.

**Step 3: Register the marketplace entry and README row**

Append `madup-writing` to the marketplace without reordering existing entries. Add a concise Korean description and the exact install command.

**Step 4: Validate packaging**

Run:

```bash
claude plugin validate --strict plugins/madup-writing
claude plugin validate --strict .
python3 -m unittest tests/test_madup_writing_plugin.py -v
```

Expected: all commands pass with no warnings or failures.

**Step 5: Commit the plugin implementation**

Commit plugin, manifest, README, tests, eval definitions, and `.gitignore` with a Lore-formatted message. Do not publish or push.

### Task 5: Run skill-enabled A/B evaluation

**Files:**
- Create: `madup-writing-workspace/iteration-1/<case>/with_skill/outputs/response.md`
- Create: `madup-writing-workspace/iteration-1/<case>/with_skill/timing.json`
- Create: `madup-writing-workspace/iteration-1/benchmark.json`
- Create: `madup-writing-workspace/iteration-1/review.html`

**Step 1: Run paired fresh Claude calls**

For each case, run one Claude CLI call with `--plugin-dir plugins/madup-writing` and one without it in the same batch. Preserve the preliminary RED baseline separately and use the paired batch for fair timing/comparison.

**Step 2: Apply deterministic checks**

Run `check_korean_style.py` against every response. Grade fact-preservation assertions programmatically where possible; leave rhythm, tone, and usability to qualitative review.

**Step 3: Run an independent blind comparison**

Give anonymized A/B outputs to an independent reviewer without identifying the skill arm. Require a winner or tie plus evidence for factual fidelity, naturalness, brevity, and action clarity.

**Step 4: Generate the review artifact**

Use the installed skill-creator review generator to produce a standalone HTML report with outputs, grades, and benchmark summary. Do not write a custom viewer.

**Step 5: Refactor once if needed**

If the skill loses a case or changes facts, update the smallest relevant rule and rerun all three paired cases. Do not add broad rules for a single wording preference.

### Task 6: Final verification and handoff

**Files:**
- Modify only if verification exposes a defect.

**Step 1: Run the complete local verification**

```bash
claude plugin validate --strict plugins/madup-writing
claude plugin validate --strict .
python3 -m unittest discover -s tests -v
python3 plugins/madup-writing/skills/madup-writing/scripts/check_korean_style.py --help
git diff --check
git status --short
```

**Step 2: Perform a local load smoke test**

Use `claude --plugin-dir plugins/madup-writing -p` with a short Korean rewrite request and confirm the plugin skill is discoverable. Do not alter the installed GitHub marketplace, publish a release, or push without a separate user request.

**Step 3: Review the final diff**

Check that no confidential content, tokens, raw Slack messages, Dropbox documents, generated evaluation outputs, or unrelated worktree changes are committed.

**Step 4: Commit verification fixes if any**

Use a Lore-formatted commit that lists exact validation evidence and remaining multi-model/source-corpus gaps.
