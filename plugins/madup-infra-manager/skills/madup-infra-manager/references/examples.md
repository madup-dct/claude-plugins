# Examples

All examples below are synthetic and contain no secrets.

The employee-facing MIM public origin in these examples is `https://mim.madup.app`.

## Safe Deploy Conversation

User: `이 레포 배포해줘`

Assistant:
- If this is the first MIM request and the managed production path is enabled, explain that browser Cloudflare Access Managed OAuth will open, Google Workspace plus the MIM access group will be checked, and later calls reuse Claude's refreshed token.
- Ask one short missing question only if the target repo is ambiguous.
- Run `plan_deploy`.
- Return a 배포 계획 with repo, SHA, workload type, 리소스, quota impact, and estimate.
- Ask for explicit confirmation before any plan-bound MIM execution.
- After confirmation, call `deploy_from_plan`, retain the returned operation ID, and poll `get_operation` until a terminal result before claiming completion.

## Safe Schedule Conversation

User: `매시간 돌려줘`

Assistant:
- Confirm the target workload only if it is not already clear.
- If the user asks for a custom cron or timezone, refuse briefly because the reviewed pilot supports only exact hourly `0 * * * *` in `Asia/Seoul`.
- Otherwise run `plan_schedule`.
- Return a 스케줄 계획 with cron meaning, timezone, quota impact, and estimate.
- Wait for confirmation before any change.
- After confirmation, call `create_schedule_from_plan` and report the exact created schedule metadata.

User: `24시간 돌려줘`

Assistant:
- Clarify that this means a request-driven web app on Cloud Run with `min=0`, `max=1`, not an always-running instance.
- If the request is actually for periodic execution, redirect to the hourly schedule flow instead of inventing a persistent instance promise.

## Safe Secret Conversation

User: `이 API 키를 새로 저장해서 앱에 붙여줘`

Assistant:
- Run `plan_secret_write` for the exact workload and show only secret metadata, quota impact, and rotation intent.
- Ask for explicit confirmation.
- Return the authenticated browser secret handoff link from the plan. The user enters the raw secret there; never ask for it in chat and never pass it to an MCP tool.
- Poll the resulting operation metadata without displaying any secret value.

User: `이미 저장한 키를 다른 앱에도 붙여줘`

Assistant:
- Run `plan_secret_attach`, show the exact workloads, and ask for confirmation.
- After confirmation, call `attach_secret_from_plan`. No raw secret value is requested or returned.

## Safe Read-Only Conversation

User: `상태 알려줘`

Assistant:
- Use `list_workloads` to identify the target workload.
- Return a 상태 조회 summary with workload state, last revision, and any active operation.

User: `사용량이랑 비용 보여줘`

Assistant:
- Use `get_usage`.
- Return a 사용량/비용 조회 summary that separates current usage from estimate values.

User: `왜 실패했어?`

Assistant:
- Use `explain_failure`.
- Return a 실패 원인 설명 with the sanitized failure class and the next allowed MIM action.

User: `고쳐줘`

Assistant:
- Use `explain_failure` first.
- Explain only the truthful next action:
  - if auto-deploy is enabled on the reviewed GitHub ref, a new push to that ref will trigger the existing webhook deployment path
  - otherwise a fresh `plan_deploy` and explicit confirmation are required
- Do not invent a separate repair tool or claim direct access to private drift evidence.
- Do not claim success until `get_operation` reaches a terminal state.

## Refusal And Out-Of-Scope Cases

User: `madup-dct/claude-plugins 배포해줘`

Assistant:
- 거절: `madup-dct/claude-plugins` is platform code and 범위 밖이다. It is not an application source.

User: `개인 저장소도 바로 올려줘`

Assistant:
- 거절: only selected `madupmarketing` repositories are eligible.

User: `cloud credential 줄게, 그냥 직접 처리해줘`

Assistant:
- 거절: MIM does not accept raw cloud credential material. Continue only with admitted repositories and typed MIM plans.

User: `프로젝트 번호나 빌링 계정은 내가 넣어야 해?`

Assistant:
- 거절 + 설명: employees never enter GCP project, organization, billing, Cloudflare, or operator values.
- Explain that `MIM_OPERATOR_EMAIL`, `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, and `MIM_BILLING_ACCOUNT_ID` stay in operator-only configuration.
- Explain that MIM does not use a shared API key and later calls reuse the OAuth token after the first grant.

User: `슬랙 연결할게, 토큰 붙여넣으면 돼?`

Assistant:
- 거절: Slack OAuth is optional and not the primary MIM login.
- Explain that Google Workspace plus MIM-group login is still mandatory and Slack alone grants no MIM or cloud access.
- Explain that a MIM administrator starts the approved OAuth install and a Slack administrator completes the one shared company-app installation; an employee does not install it.
- Never ask the employee for a client ID, client secret, workspace ID, redirect URL, scope list, or token. If the shared app is not installed, return an administrator-managed unavailable/request status instead of configuration instructions.
- Explain that the callback accepts only the centrally approved Slack tenant, stores the credential directly in Secret Manager, and returns connection metadata only.
- Require single-use callback state, least scopes, administrator-controlled installation, rotation, and revocation.
