# madup-writing 1.2.0 — review-driven improvements

Source: 5-lens adversarial review (15 agents, 23 findings, 10 verified real) plus a
live A/B run (6 cases x with/without plugin: 6/6 vs 3/6 oracle pass) and external
research on documented AI-writing tells (Wikipedia "Signs of AI writing", Korean
detection write-ups).

## Shipped in 1.2.0 (plugin)

- Skill description now carries Korean trigger phrases (matching the README ad copy)
  and sibling boundaries: humanize-korean owns style-only de-AI-ing of long prose,
  copy-tone-gate owns tone-gating finished deck headlines. Live probes confirmed the
  intended routing: finished-headline requests fire copy-tone-gate, drafting requests
  fire madup-writing.
- Core Contract names the documented machine-writing tells to strip (negative
  parallelism, mechanical enumeration, canned closers, connective chains,
  free-floating evaluations, double passives); voice-guide has the full list.
- Email submode rules: a brand-new external email may carry one greeting and one
  sign-off; rewrites keep the source register. Slack stays scaffold-free.
- Presentation mode requires speakable endings (~습니다/~하죠) for headlines.
- check_korean_style.py: masks paired quote spans (locked facts are never flagged),
  narrows 제공했 to abstract-noun fillers, stops flagging ellipses, and adds
  detectors for the tells above.
- beacon.sh matches only the invoked skill field (no more over-counting when other
  skills mention madup-writing in args).

## Shipped in oracle 1.5.0 (evals)

- Sentence splitter counts boundaries without trailing whitespace (다.QA / 다.1차)
  while keeping decimals (0.4%p) intact; covered by self-tests.
- Comparison detection accepts 보다-phrasings, so an invented "전월보다" is caught.
- Cause/trend invention checks now also run for report submode.
- Register check: banmal endings with no polite ending anywhere hard-fail email and
  raise an advisory for slack.
- Lines stripped as labels/greetings are recorded as advisory findings so live runs
  can compare scaffolding rates between arms.

## Deferred backlog

- generator.py: extend meta_scaffolding forbidden markers to slack/email cases —
  requires regenerating golden_cases.jsonl (generator version + snapshot bump).
- Promote field-label lines to hard failures for slack/proposal/presentation.
- Positional checks (workstream token in first slack sentence; proposal evidence
  before recommendation).
- voice-guide dedup against SKILL.md (needs pinned-string sync in tests).
- Apply the beacon skill-field patch to the other three plugins.
