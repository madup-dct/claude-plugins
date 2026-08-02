# Madup Writing Plugin Design

## Goal

Create a marketplace-installable Claude Code plugin that produces concise, natural Korean for Madup's Slack messages, reports, proposals, and presentation copy without sounding machine-written.

## Context

Writing guidance currently exists in several places:

- `humanize-korean` performs heavyweight post-processing and is broader than daily business writing.
- `copy-tone-gate` covers titles and short presentation copy only.
- `proposal-deck` contains proposal-specific agency tone rules.
- `bi-repo/slides/geo` contains proven proposal headlines and narrative structures.

The new plugin will unify the common judgment rules without deleting existing tools. It becomes the lightweight default for ordinary business writing; specialized tools remain available for strict long-form editing and proposal production.

Dropbox and Slack were requested as additional style sources. In the current Codex session, Dropbox is not mounted and no Slack/Dropbox connector is exposed. The first version therefore derives rules from locally available production artifacts and existing guides. Future source refreshes may add anonymized patterns when an authorized connector or mounted source is available. Raw company messages and confidential document excerpts must not be embedded in the plugin.

## Chosen Approach

Add a new `madup-writing` plugin to the existing `madup` marketplace rather than extending `humanize-korean` or replacing all tone plugins.

This keeps runtime fast, avoids a multi-agent rewrite pipeline for ordinary text, and minimizes regressions. Existing plugins remain available for specialized work.

## Plugin Structure

The version-controlled source lives at:

`plugins/madup-writing/`

The plugin contains:

1. `.claude-plugin/plugin.json` — plugin identity, version, author, and description.
2. `skills/madup-writing/SKILL.md` — concise trigger metadata, mode selection, drafting workflow, and release checklist.
3. `skills/madup-writing/references/voice-guide.md` — common voice principles and mode-specific rules for Slack/email, reports, proposals, and presentation copy.
4. `skills/madup-writing/references/examples.md` — anonymized before/after examples based on recurring local patterns, not copied confidential prose.
5. `skills/madup-writing/scripts/check_korean_style.py` — dependency-free static checks for high-confidence AI-like wording and formatting smells. The script reports candidates; it never rewrites content or treats heuristics as proof of authorship.
6. `evals/evals.json` — realistic evaluation prompts and expected outcomes for baseline and skill-enabled comparisons.
7. `hooks/` — the same privacy-preserving usage-count beacon contract as the other Madup plugins.

The root marketplace manifest and README will list `madup-writing` with its installation command.

## Writing Contract

- Lead with the fact, decision, or requested action.
- Prefer concrete verbs and subjects over abstract nouns.
- Preserve numbers, names, dates, units, citations, and the writer's intended level of formality.
- Use only the structure the reader needs; do not force headings or bullets onto short text.
- Remove translation-like phrases, mechanical parallelism, empty transitions, unexplained English jargon, inflated claims, and decorative metaphors.
- Match the document mode:
  - Slack/email: context, decision, action; short enough to scan.
  - Report: fact, interpretation, implication, next action.
  - Proposal: advertiser language, one message at a time, evidence before claim.
  - Presentation copy: pass the read-aloud test; avoid literary or declarative slogans except where explicitly requested.
- Return ready-to-use copy first. Explain edits only when the user asks.

## Validation

Use evaluation-driven development:

1. Run representative prompts without the skill and capture baseline outputs.
2. Identify observable failures such as inflated claims, generic openings, excessive headings, vague action items, and content drift.
3. Implement the minimal skill that addresses those failures.
4. Run the same prompts with the skill.
5. Apply deterministic style checks to both outputs.
6. Use an independent blind comparison for qualitative judgment.
7. Revise if the skill introduces new problems or fails to improve the outputs.

The evaluation set covers a Slack project update, a performance report summary, and proposal headline/body copy. Success requires factual fidelity, usable tone, clear action, and fewer high-confidence style violations than the baseline.

## Integration

After the plugin passes its evaluations:

- Register `madup-writing` in `.claude-plugin/marketplace.json`.
- Document `/plugin install madup-writing@madup` in the repository README.
- Perform a local clean-install smoke test without publishing or pushing.
- Preserve `humanize-korean` for explicit, long-form, or strict post-editing requests.
- Preserve `copy-tone-gate` until real usage confirms that removing it is safe.
- Keep proposal-specific integration as a separate follow-up unless the plugin evaluation proves the common gate is safe for that harness.

## Risks

- Style heuristics can overcorrect legitimate formal Korean. The checker therefore reports candidates rather than editing automatically.
- Proposal language and Slack language differ. Mode selection and mode-specific references prevent one register from flattening all output.
- Source samples may contain confidential information. Only abstracted rules and anonymized examples enter the plugin.
- Claude model behavior varies. Tests use the locally available Claude CLI model, and verification reports any unavailable multi-model coverage honestly.
