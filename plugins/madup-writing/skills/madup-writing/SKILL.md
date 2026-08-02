---
name: madup-writing
description: Use when drafting or rewriting Korean Slack, email, report, proposal, presentation, headline, or executive-summary copy that must sound natural while preserving supplied facts and format.
---

# madup-writing

Ship ready-to-use business copy with the point first, the facts intact, and no extra scaffolding.

Priority: locked facts and explicit output shape override mode defaults; mode defaults override style cleanup.

## Core Contract

- Treat supplied facts as locked in drafting and rewrite tasks; copy supplied date/number/unit/name tokens verbatim. No abbreviation or reformat: `8월 4일` must not become `8/4`.
- Treat a named topic or workstream as a locked fact, not background context; include its exact words in the final copy.
- Do not fact-check, reinterpret, or override provided dates, calendars, names, numbers, units, quotes, acronyms, or commitments unless the user explicitly asks for verification.
- Do not turn a result/target into a new schedule, owner, reporting promise, budget move, or guarantee unless supplied or explicitly requested.
- Treat an exact sentence, line, paragraph, or item count as a hard output contract.
- Keep the requested register. Formal input stays formal. Brief chat stays brief.
- Lead with the fact, decision, ask, or implication. Prefer a concrete subject and verb over abstract nouns and inflated claims.
- Do not add field labels such as `제목`, `헤드라인`, or `본문` unless requested.
- Do not add a greeting, sign-off, quote marks, preface, or afterword unless requested.
- Return ready-to-use copy only. Do not add process narration, labels, questions, or explanations unless the user asks or the task truly needs clarification.

## Workflow

1. Build a silent must-keep checklist before drafting: named topic or workstream, facts, requested action, dates, numbers, names, and exact wording constraints.
2. Pick the smallest matching mode: `slack_email`, `report`, `proposal`, or `presentation_copy`.
3. Draft once, then remove empty transitions, translation-like phrasing, inflated claims, and decorative formatting.
4. Before returning, compare the draft against the must-keep checklist; restore anything missing and compress wording rather than dropping a required item.

## Mode Rules

### Slack / Email

- In Slack, place the named topic or workstream in the first sentence.
- If a date or owner is supplied, preserve it exactly, including the written surface form.
- Do not resolve `오늘`, `내일`, or `다음 주` into a new calendar date.
- For a short, one-paragraph, or paste-ready message, use plain text; use bullets only for multiple independent actions or owners.
- For a one-paragraph Slack request, use no blank lines and default to three sentences or fewer.

### Report

- Use a one-line conclusion or `risk -> action -> status -> decision gate` only when the source supplies the needed parts.
- Report notes should be fact -> interpretation -> implication, with next action only when supplied or requested.
- Never invent a follow-up timing window, owner, or reporting promise.
- Do not begin with meta text such as `정리했습니다`, `작성했습니다`, or `report 모드`.
- Without a supplied comparison, do not claim that performance was maintained, improved, worsened, or stable.
- Do not call overall performance `안정적`, `견조`, or `개선` without a matching baseline for that subject.

### Proposal

- Use advertiser language, not internal workshop language.
- Put the strongest evidence first, then make one supported recommendation; label weaker evidence or hypotheses.
- Use a controlled contrast only when the source supports both sides.
- Preserve phase and time anchors verbatim; never change `첫 달` to `이번 달`.
- Cut unsupported abstract benefit claims.

### Presentation Copy

- Every line must pass the read-aloud test.
- Use a headline and one short support line only when needed.
- When the prompt says a metric must stay unchanged, preserve the complete metric span verbatim.
- Do not invent a comparison period, benchmark, cause, or trend to support a lone metric.
- When at least one usable fact is supplied, write the requested copy instead of asking for more context.
- Split copy by audience view only when requested or when the source already separates the readers.

## References

- Consult [voice-guide.md](references/voice-guide.md) only when a mode needs nuance not covered above.
- Consult [examples.md](references/examples.md) only when an example pattern would help.
- Use [check_korean_style.py](scripts/check_korean_style.py) only as an advisory smell check when text is available. Its findings are candidates, not proof.
