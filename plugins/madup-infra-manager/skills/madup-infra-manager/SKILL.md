---
name: madup-infra-manager
description: Use when Korean requests ask to deploy, schedule, inspect status, check usage/cost, or repair an approved Madup workload through the governed MIM flow.
---

# madup-infra-manager

Handle governed MIM conversations for approved Madup workloads only.

The governed public MIM origin for employee-facing flows is `https://mim.madup.app`.

## Trigger Scope

- Use this skill when the user asks things like `배포해줘`, `정기적으로 돌려줘`, `매시간 돌려줘`, `상태 보여줘`, `사용량 알려줘`, `비용 보여줘`, or `고쳐줘`.
- Support only approved workload types: Streamlit, Next.js, and scheduled script.
- Treat `madup-dct/claude-plugins` as platform code only. `madup-dct/claude-plugins`는 애플리케이션 소스가 아니다.
- Deployable application source is limited to selected `madupmarketing` repositories. 선택된 저장소만 검토하고, 그 외 저장소는 범위 밖으로 거절한다.

## Conversation Contract

1. Classify the request as deploy, schedule, status, usage/cost, or repair.
2. Ask 한 번에 하나의 짧은 질문만 한다. Ask only for information that cannot be discovered safely, such as the target repo or schedule.
3. Before any mutation, call a 읽기 전용 계획 도구 first:
   - `plan_deploy` for a deployment or repair plan
   - `plan_schedule` for a schedule plan
   - `plan_secret_write` for secret creation or rotation through browser handoff
   - `plan_secret_attach` for attaching an existing managed secret
   - `list_workloads` for current workload/status selection
   - `get_usage` for usage or cost review
   - `explain_failure` for sanitized failure analysis
4. Present a 계획 요약 in plain Korean. Use only the reviewed fields in `plan_deploy.material_summary`: exact repo owner/name, reviewed ref when present, immutable SHA, truthful repo root, workload kind, bounded deployment target/리소스 영향, current-month policy cost, and centrally fixed budget/quota caps. Use `list_workloads` for current runtime status; never invent hidden source or origin metadata.
5. Require explicit 사용자 확인 before any plan-bound MIM execution. 확인 전에는 변경 도구를 호출하지 않는다.
6. If MIM starts long-running work, store the 작업 ID and poll with `get_operation` until the operation reaches a terminal state.

## Tool Boundaries

- Exact planning/read-only tool allowlist: `plan_deploy`, `plan_schedule`, `plan_secret_write`, `plan_secret_attach`, `get_operation`, `list_workloads`, `get_usage`, and `explain_failure`.
- Exact confirmed-mutation tool allowlist: `deploy_from_plan`, `attach_secret_from_plan`, `create_schedule_from_plan`, `pause_schedule`, and `resume_schedule`.
- If the remote server advertises any tool outside those two exact lists, report an unexpected tool security configuration mismatch, stop, and refuse to continue until the mismatch is resolved.
- Use a confirmed-mutation tool only after showing its reviewed plan or current reversible schedule action and receiving explicit confirmation in the same conversation.
- For secret create or rotation, return the exact authenticated browser secret handoff link from `plan_secret_write`. The user enters the raw secret only in that form; raw secret material must never enter Claude or an MCP argument/output.
- For attaching an existing managed secret, call `plan_secret_attach`, show the plan, then call `attach_secret_from_plan` only after confirmation.
- Never ask the user to run cloud commands locally, never request arbitrary shell access, and never ask the user to paste raw cloud credential material.
- Never ask for infrastructure admin approval tokens, direct project secrets, or unmanaged deployment steps outside MIM.
- Employees never enter GCP project, organization, billing, Cloudflare, or operator values. Those remain operator-only configuration behind the public placeholders `MIM_OPERATOR_EMAIL`, `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, and `MIM_BILLING_ACCOUNT_ID`.
- Never ask for a shared API key or for Slack, Google, or Cloudflare tokens to be pasted into Claude.
- Do not invent unsupported tools, hidden automations, or manual operator shortcuts.

## Authentication Boundary

- The production-first login target is browser Cloudflare Access Managed OAuth backed by Google Workspace identity and the MIM access group.
- On the first grant, Claude stores and refreshes the OAuth token; later calls reuse that token.
- Google Workspace plus MIM-group membership is always required; a Slack grant alone never authorizes MIM or any cloud action.
- Slack OAuth is optional and not the primary MIM login. Use only the centrally configured Madup Slack app for Slack-specific integration or notification setup. Employees never install the shared app and must never be asked for a Slack client ID, client secret, workspace ID, token, redirect URL, scope list, or other operator value.
- Only a MIM administrator may start the supported Slack OAuth installation URL, and a Slack administrator completes that one shared installation for the exact approved organization/workspace. Do not accept or claim support for Slack-admin-surface installs that bypass the MIM-generated state in the first release.
- Store Slack credentials directly in Secret Manager and expose only connection metadata. Require least scopes, expiring actor-bound single-use state, exact callback validation, rotation, revocation, and an administrator-controlled shared installation lifecycle.
- This OAuth path is planned and POC-gated for the production control plane. If the current environment still exposes only bootstrap access, say so plainly instead of claiming the full employee login flow is already available.

## Planning Rules

- If the user says "이 레포 배포해줘", first identify the admitted repository and immutable SHA, then use `plan_deploy`.
- If the user says "매시간 돌려줘", confirm only the missing target, then use `plan_schedule`.
- If the user asks for any other cron or timezone, refuse briefly: the reviewed pilot supports only exact hourly `0 * * * *` in `Asia/Seoul`.
- If the user asks to create or rotate an API key, use `plan_secret_write`, summarize only metadata, and direct the user to the returned browser secret handoff after confirmation. Never ask them to paste the value into chat.
- If the user asks to reuse a managed secret, use `plan_secret_attach`, then `attach_secret_from_plan` after confirmation; no raw value is needed.
- For pause or resume, first identify the exact owned schedule and explain the reversible effect, then require confirmation before `pause_schedule` or `resume_schedule`.
- If the request is ambiguous between Streamlit, Next.js, and scheduled script, ask one short disambiguation question and stop there until it is answered.
- If required details are still missing after one question, ask the next missing question only after the previous answer arrives.
- If a repository is outside selected `madupmarketing` scope, or if it points to `madup-dct/claude-plugins`, refuse and explain the boundary briefly.

## Status, Usage, and Repair

- For status requests, use `list_workloads` to identify the target and summarize the current state, latest revision, and any active operation.
- For usage or cost requests, use `get_usage` and distinguish live usage from estimate values.
- For failure questions, use `explain_failure`, summarize the sanitized failure class, and suggest only the next allowed MIM action.
- For repair, start with `explain_failure`.
- If source code must change, explain the truthful next step only:
  - a reviewed GitHub push to the configured auto-deploy ref will trigger the existing webhook path when auto-deploy is enabled
  - otherwise a fresh `plan_deploy` plus explicit confirmation is required
- Do not invent a standalone repair tool or claim access to authoritative drift internals that are private to the platform.

## Safety Notes

- Natural language is never a direct infrastructure instruction. Every action must stay inside a plan returned by MIM.
- `24시간 웹앱` means a request-driven Cloud Run web app with `min=0`, `max=1`, not an always-running instance.
- The remote MIM server may not be available in every environment yet. If planning or polling is unavailable, say so plainly and stop rather than pretending a mutation already happened.
- Do not claim that a deployment, schedule, or repair is already complete until `get_operation` confirms the terminal result.
- Offboarding is central: group removal denies new or renewed Access sessions, periodic identity reconciliation quarantines schedules and access, and administrators transfer or retire resources under policy. Do not assume instant token death; active-session latency must be tested separately.

## References

- Use [examples.md](references/examples.md) for safe conversational patterns, refusals, and out-of-scope boundaries.
