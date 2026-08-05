# Madup Infra Manager Control Plane Design

## Status

Approved for implementation on 2026-08-02.

The existing `mim-bootstrap` service proves only the initial GCP, Cloud Run, IAP, IAM, and cost boundary. It does not yet implement the Claude interaction surface, deployment API, schedules, dashboard, usage accounting, lifecycle automation, or GitHub integration described here.

## Goal

Madup Infra Manager (MIM) lets a marketer operate small internal applications by talking to Claude instead of learning cloud infrastructure. A user can say “이 레포 배포해줘”, “매시간 돌려줘”, “왜 실패했어?”, or “고쳐진 버전 다시 올려줘”. Claude gathers only the missing information, shows the proposed action, and calls a small set of structured MIM tools.

MIM, not Claude, is the security boundary. The control plane independently checks identity, repository admission, workload type, quota, estimated cost, secret policy, and the exact cloud mutation before it performs an action. Claude never receives a general shell, `gcloud` credentials, project administration rights, or access to Madup's pre-existing GCP, AWS, BigQuery, Google Workspace data, or personal accounts.

## Fixed Product Decisions

- The initial pilot is limited to at most 50 authorized users.
- The plugin and platform source live in `https://github.com/madup-dct/claude-plugins` and are updated on `main`.
- Deployable application source must belong to `madupmarketing` and must be selected in the MIM GitHub App or Cloud Build GitHub connection. The plugin repository is not an application-source exception.
- MIM cloud resources live only inside the reviewed operator boundary named by `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, and `MIM_BILLING_ACCOUNT_ID`, with a fixed runtime region policy.
- The operator and break-glass administrator is supplied through `MIM_OPERATOR_EMAIL` in ignored operator configuration, not in public tracked files.
- The original owner account and all existing sensitive projects remain outside MIM's authority.
- Web applications use Cloud Run. Scheduled scripts use Cloud Run Jobs, gated by MIM and triggered by Cloud Scheduler.
- Default workload resources are 1 vCPU, 512 MiB, minimum instances `0`, maximum instances `1`, and request-based CPU allocation.
- A user may own at most two active services, three active schedules, and five active logical integration secrets by default.
- The monthly target is KRW 1,000 per user. An administrator may grant a time-bounded exception, but never above KRW 10,000 per user without changing this policy in Git.
- Custom application subdomains remain a later ingress phase. The first application links use direct Cloud Run IAP on the generated `run.app` URLs: `ingress=all`, `iap_enabled=true`, no unauthenticated invoker, and resource-level IAP access for only the workload owner plus MIM administrators. `mim.madupai.com` is the stable MIM dashboard and MCP hostname.

## User Conversation Contract

Claude is the conversational interface, not the policy engine.

1. On the first production MIM call, Claude opens browser Cloudflare Access Managed OAuth, the user proves Google Workspace identity plus MIM access group membership, and Claude stores and refreshes the OAuth token after the first grant so later calls reuse it. This Google gate is mandatory; Slack is never an alternative MIM login.
2. Claude recognizes an intent such as deploy, schedule, status, repair, pause, resume, rotate secret, or cost inquiry.
3. Claude asks one short question at a time only for required information that cannot be discovered safely, such as which approved repository or the desired schedule.
4. Claude calls a read-only planning tool first. MIM returns the pinned commit SHA, detected workload type, resources, access list, schedule, quota impact, and estimated monthly cost.
5. Claude summarizes that plan in plain Korean. The subsequent mutating tool is marked destructive and must carry the plan identifier and hash returned by MIM.
6. MIM revalidates all inputs and current policy at execution time. A stale, altered, expired, or already-consumed plan is rejected.
7. Long-running work returns an operation identifier. Claude polls status and explains progress without exposing raw credentials or unsanitized logs.

Examples:

- “이 레포 배포해줘” → inspect the current Git remote, resolve an admitted `madupmarketing` repository and immutable SHA, classify it, show a deployment plan, then deploy after confirmation.
- “매시간 돌려줘” → confirm the target script only if ambiguous, default to minute zero in `Asia/Seoul`, enforce a minimum one-hour interval, show the schedule plan, then create a gated Cloud Scheduler trigger and Cloud Run Job.
- “메인에 고치면 알아서 다시 올려줘” → ask once whether automatic default-branch deployment should be enabled for that workload. If enabled, a verified GitHub push creates a new deployment operation under the same admission, quota, and rollback rules.
- “왜 실패했어?” → return the sanitized failure class, the last successful revision, and only the repair actions allowed for the user's role.

Natural language never becomes an arbitrary shell command, Docker option, IAM binding, cron target URL, or infrastructure template. The server accepts only typed fields and enumerated actions. Employees never enter GCP project, organization, billing, Cloudflare, or operator values, and they never receive a shared API key.

## Architecture

### Claude and edge

The `madup-infra-manager` Claude plugin contains a skill and a remote Streamable HTTP MCP definition. Claude Code can invoke the skill automatically from natural-language requests or explicitly through the plugin command.

`mim.madupai.com` is protected by Cloudflare Access Managed OAuth. Cloudflare uses the Madup Google identity provider and an allow policy for the MIM access group. Managed OAuth lets Claude and other non-browser MCP clients complete a standard browser login instead of carrying a shared API key. Employees never type operator configuration into Claude; those values remain operator-only configuration. After the first grant, Claude stores and refreshes the OAuth token so later calls reuse it. The free Access plan is sufficient only while the pilot stays at or below 50 users.

A small fail-closed Cloudflare Worker forwards authenticated MIM traffic to the generated Cloud Run origin. The origin validates the Cloudflare Access JWT, exact application audience, issuer, expiry, email, and `@madup.com` domain on every request.

The Cloudflare token alone is not sufficient at the origin. The Worker also signs the method, canonical path, body digest, timestamp, and request identifier with a dedicated HMAC origin key held only as a Cloudflare Worker secret and a GCP Secret Manager version available to the control-plane runtime. The origin accepts only a 60-second signature window and claims the request identifier through a create-only Firestore TTL record before it evaluates the user token, so a duplicate identifier is rejected. The origin key is not a user integration secret, never enters application workloads or logs, and supports overlapping old/new keys for rotation. A caller who legitimately owns a Cloudflare user token still cannot bypass the Worker by calling `run.app` directly. The Worker route is configured to fail closed if its free-plan request limit is reached.

The existing direct-IAP bootstrap remains a separate break-glass surface until the production control plane is proven. It is not reused as the programmatic MCP authentication mechanism. The employee login announcement is therefore POC-gated: if managed OAuth is not proven in the current environment, Claude must say so instead of pretending the production employee flow already exists.

### GCP control plane

The production control plane is split by responsibility:

- `mim-control-plane`: Cloud Run API, MCP tools, dashboard, authorization, planning, and read models. It uses `min=0` and `max=1` for the pilot.
- Firestore: authoritative metadata for users, repositories, workloads, deployment plans, operations, schedules, secret metadata, quotas, desired manifests, and lifecycle state.
- Cloud Tasks: authenticated, idempotent delivery of deployment, reconcile, and repair operations to private worker endpoints.
- `mim-deploy-worker`: a private Cloud Run worker identity allowed to apply only MIM-rendered Cloud Run service/job configuration inside the dedicated project.
- Cloud Build: builds pinned GitHub source with a dedicated build service account. MIM supplies the build template; repositories cannot supply unrestricted `cloudbuild.yaml`, Terraform, or deployment commands.
- Artifact Registry: stores content-addressed workload images. Deployments use image digests, not mutable tags.
- Per-workload runtime service accounts: no project roles and no access to other workloads. Exact secret access is granted at the secret resource only.
- Cloud Scheduler and `mim-schedule-gateway`: Scheduler calls the private authenticated MIM HTTP gateway with an OIDC token. The gateway checks user state, quota, cost state, overlap locks, and schedule state before invoking a Cloud Run Job with its dedicated machine identity. A future design that calls the Google `run.googleapis.com/.../jobs/...:run` API directly from Scheduler must use an OAuth access token instead of OIDC; the two authentication modes are not interchangeable.
- `mim-identity-sync`: a scheduled, read-only Cloud Identity group sync that records only the stable subject, email, MIM-group membership, and suspension state needed for authorization. Every API request also checks the local lifecycle state; if identity data is older than the allowed sync window, mutating operations fail closed.
- Cloud Logging and Monitoring: operational signals, sanitized user logs, request counts, execution results, CPU/memory estimates, and alert inputs.
- MIM-only billing export: final GCP cost attribution without granting workloads or users access to internal BigQuery datasets.

Control-plane, build, deploy, scheduler, and runtime identities are separate. None use a human's ambient `gcloud` login, personal access token, downloaded service-account key, or OAuth refresh token supplied by a user.

### User workload ingress

User-facing Streamlit and Next.js services use Cloud Run's direct IAP integration, which became generally available in 2026. Direct IAP protects the default `run.app` endpoint without an external load balancer and without an added IAP charge for standard Google Cloud-hosted application protection. It therefore avoids the idle forwarding-rule cost that made a shared Google load balancer unsuitable for this pilot. The release contract follows the official Cloud Run shape: `INGRESS_TRAFFIC_ALL`, `iap_enabled=true`, no `allUsers` or broad employee invoker binding, and `roles/run.invoker` only for the exact IAP service agent. See [Configure IAP for Cloud Run](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run) and [IAP pricing](https://cloud.google.com/iap/pricing).

IAP authorization is resource-scoped per service. The workload owner and the reviewed MIM administrator group receive `roles/iap.httpsResourceAccessor`; ordinary MIM users do not receive project-level IAP access. A deployment is not healthy until IAP is enabled, the IAP service agent is the exact Cloud Run invoker, the owner/admin access policy is exact, and an unauthenticated request is denied. Offboarding or quarantine removes the owner's IAP binding before compute cleanup. Cloud Run Jobs, deploy workers, schedule gateways, and other machine-only services remain private and do not use the browser IAP path.

This direct-IAP workload path is separate from Cloudflare Access on `mim.madupai.com`. Cloudflare remains the browser/MCP identity boundary for the control plane; it is not asked to mint Google identity tokens for workload origins. Future `*.apps.madupai.com` routing must preserve the same IAP or an independently reviewed origin-auth boundary and remains out of the first release.

## GitHub and Release Flow

### MIM platform changes

The design, plugin, control plane, infrastructure policies, tests, and release automation are committed to `madup-dct/claude-plugins` on `main`. Commits preserve the repository's Lore trailers. A reviewed release workflow deploys the MIM control plane from an exact commit and records its image digest and Cloud Run revision.

Pushing a commit to Git is not itself authorization to change arbitrary infrastructure. Release automation may update only the reviewed MIM resources named by the operator-only approved boundary and must pass policy, test, and IAM-drift checks first.

### User application changes

The GitHub integration verifies the webhook signature, installation or connection identifier, repository owner, exact selected repository, branch policy, and commit SHA before any build. Personal repositories, forks, arbitrary URLs, repository redirects, and unselected `madupmarketing` repositories are rejected before source is fetched.

An application's desired manifest records whether automatic deployment from its approved default branch is enabled. When enabled, a verified push creates a new operation for the new SHA. MIM repeats classification, source admission, secret scanning, quota checks, and build policy. A healthy revision receives traffic; a failed revision leaves the last healthy revision active and appears in Claude and the dashboard. For a scheduled job, the job template is updated to the new healthy image digest and the next gated execution uses that digest.

Automatic deployment is configured per workload through a conversation and can be disabled by the owner or an administrator. It is never inferred from the presence of a workflow file.

## Workload Admission and Build Policy

MIM supports three explicit workload types:

- `streamlit`: a Cloud Run web service using a trusted Streamlit launch template and WebSocket-compatible settings.
- `nextjs`: a Cloud Run web service requiring a locked production build and start command.
- `scheduled-script`: a Cloud Run Job with an explicit entry point, timeout, and schedule policy.

An optional `mim.yaml` manifest may remove ambiguity but cannot raise resources, select identities, add networking, change the target project, or inject arbitrary infrastructure. Safe autodetection may propose a type; conflicting or ambiguous evidence fails closed and asks the user to choose from the supported types.

The build service account can read the admitted source snapshot, write only to the MIM Artifact Registry namespace, and write build logs. It cannot deploy, administer IAM, read application secrets, query BigQuery, or impersonate runtime identities. The deploy worker accepts only an internal desired manifest rendered by MIM and deploys an immutable image digest.

## State and Audit Model

Firestore is the system of record for metadata. Secret values remain only in Secret Manager.

Core records include:

- `users`: normalized email, stable identity subject, role, access-group state, lifecycle state, quota policy, and last authenticated activity.
- `repositories`: GitHub installation/connection, owner, repository ID, allowed branches, admission state, and last verified time.
- `workloads`: owner, type, source repository, desired SHA and manifest, runtime identity, active revision, access list, lifecycle state, and auto-deploy preference.
- `deployment_plans`: immutable proposed action, policy version, estimate, expiry, confirmation state, and content hash.
- `operations`: actor, action, idempotency key, state, timestamps, build, deployment, rollback, and sanitized failure.
- `schedules`: owner, job, normalized cron, timezone, enabled state, last attempt, last success, consecutive failures, and lease state.
- `secrets`: metadata only, logical integration type, owner, attached workloads, active version number, rotation state, and lifecycle state.
- `usage_ledger`: labeled resource measurements, estimated cost, finalized billing cost, attribution confidence, and collection timestamp.
- `activity_events`: authenticated user subject, surface (`dashboard`, `mcp`, or operator), normalized action, target reference, outcome, latency bucket, correlation ID, and timestamp. It never stores raw prompts, request bodies, authorization headers, cookies, client IP addresses, user agents, or secret values.
- `daily_usage_aggregates`: privacy-minimized per-user and platform counts for active users, authenticated dashboard visits, MCP actions, deployments, schedule executions, successes, failures, and policy denials.
- `audit_events`: append-only actor, target, policy decision, before/after reference, request correlation ID, and outcome.
- `lifecycle_actions`: warning, quarantine, transfer, deletion eligibility, execution, and restoration metadata.
- `origin_request_ids`: create-only, short-lived replay claims for Worker-signed origin requests, removed automatically by Firestore TTL.

Every mutating API call has an idempotency key and audit event. Audit events do not contain request bodies that may include secret values.

## Dashboard and Usage Visibility

### User view

An ordinary user sees only resources they own or that were explicitly shared with them:

- active, paused, failed, quarantined, and archived applications and jobs;
- repository, deployed commit, revision or execution, URL, health, and last activity;
- schedules, timezone, next run, last run, duration, and sanitized failure;
- request counts, job executions, build minutes, approximate CPU/memory use, and estimated monthly cost;
- their authenticated dashboard visits, Claude/MCP action counts, deployment attempts, schedule runs, successes, failures, and policy denials by day and action type;
- finalized cost when billing export catches up, clearly distinguished from the estimate;
- service, schedule, secret, and monthly KRW quota consumption;
- recent deployments, allowed repairs, lifecycle warnings, and automatic-deploy state;
- counts and outcomes of MIM actions initiated through Claude, recorded as normalized tool actions without retaining the user's raw conversation text;
- secret names, integration types, attachment state, and rotation dates, never secret values.

### Administrator view

The MIM administrator sees the full MIM project, but no data from external sensitive projects:

- total current and projected monthly cost and finalized billed cost;
- cost and resource use by user, workload, type, and service category;
- each user's target, hard policy ceiling, temporary exception, and remaining allowance;
- active users over 24-hour, 7-day, and 30-day windows; authenticated dashboard visitors and Claude/MCP users; denied users; offboarding state; ownership-transfer queue; and last activity;
- services, jobs, schedules, secrets, builds, revisions, failures, stale resources, and deletion queue;
- deployment volume, schedule success rate, repeated failures, rollback rate, and policy denials;
- Claude-initiated MIM action counts and outcomes by user and action type, without raw prompts or secret-bearing request bodies;
- daily usage trends for dashboard visits, MCP calls, deployments, builds, schedule executions, successes, failures, policy denials, and sanitized latency buckets;
- IAM, orphan-resource, label, desired-state, and runtime-configuration drift;
- append-only audit history with actor and correlation ID.

Admin access to this dashboard is an application role. It does not grant the administrator's browser session or the MIM service authority over the administrator's other cloud accounts.

Detailed activity events are retained only for the configured operational audit window and then reduced to daily aggregates. Usage collection is purpose-limited: it records authenticated MIM activity needed for operations, cost attribution, support, and security, but never retains conversation text or browser fingerprinting data.

## Quota and Cost Enforcement

All MIM resources carry stable labels for owner, workload, environment, and platform. The usage ledger combines immediate Cloud Monitoring signals and operation estimates with delayed billing export. The dashboard labels estimates and finalized charges separately.

The per-user KRW target covers direct marginal workload cost only: that user's builds, Cloud Run service requests, Cloud Run Job executions, Scheduler jobs, retained workload images, and secret versions/accesses. Build submissions carry an operation identifier so their cost can be joined to the workload owner even where a native billing label is unavailable. Shared MIM control plane, identity sync, lifecycle workers, billing export, monitoring, Cloudflare, and other platform overhead are shown in a separate administrator-only platform-cost bucket. Shared overhead is not divided arbitrarily among users and does not consume the KRW 1,000 user target. The organization-wide emergency ceiling includes both direct user cost and platform overhead.

Budgets are alerts, not hard caps. Enforcement therefore uses multiple layers:

1. Admission prevents a user from creating more than two active services, three active schedules, or the approved secret limit.
2. Policy templates force `min=0`, `max=1`, 1 vCPU, 512 MiB, request-based CPU, bounded timeouts, job parallelism `1`, task count `1`, bounded retries, and no user-selected accelerator or VPC connector.
3. Every build, service, job, schedule, image, and secret is attributed before creation. Unattributed resources are quarantined as drift.
4. MIM reserves part of each user's allocation for billing lag and estimates the impact of a new action before accepting it.
5. At 70% of the allocation, MIM notifies the user and administrators.
6. At 90%, MIM rejects new deployments, new schedules, and new secrets while allowing safe pause, rollback, and deletion actions.
7. At the projected allocation limit, MIM removes end-user application access, disables schedules, rejects manual job runs, and lets services scale to zero. It does not delete data.
8. An administrator may set a lower limit or a time-bounded exception with a reason. Users cannot change their own limits.

The platform also has an organization-wide emergency stop. When the overall MIM ceiling or an abnormal usage-rate threshold is crossed, it blocks all new work, disables schedule gates, and pauses non-exempt workloads while preserving the control plane and audit trail.

## Secret Policy

Users create logical integration secrets through an authenticated MIM form or conversational handoff that prevents Claude from echoing the value. Values are written directly to Secret Manager and never returned after creation.

The existing limits remain: five active integrations per user by default, administrative hard maximum ten, five attachments per app, payload target at most 16 KiB, and one enabled version. Rotation creates a new version, disables the old version immediately, and destroys the old version after seven days. Exact runtime service accounts receive `roles/secretmanager.secretAccessor` on exact secret resources only.

MIM rejects service-account JSON, user-supplied OAuth refresh tokens, personal access tokens, GCP/AWS access keys, and equivalent general cloud credentials. A dedicated centrally approved OAuth callback may write its own integration credential directly to Secret Manager; that credential can never enter the generic secret form, Claude, logs, or API output. Offboarding or workload quarantine removes runtime access and locks the secret metadata; it does not automatically delete secret material.

Slack OAuth is optional and separate. It never grants MIM or cloud authorization. MIM uses one centrally configured Madup Slack app and accepts callbacks only for the exact approved Enterprise organization/workspaces or, outside Enterprise Grid, one exact approved workspace. Only a MIM administrator can start the supported OAuth URL, and a Slack administrator completes the shared app installation. Employees cannot install it or supply or override client IDs, client secrets, tenant IDs, redirect URLs, scopes, or tokens. The first release does not import installations initiated only from Slack admin surfaces because those paths do not always return the application-generated state. The supported callback uses expiring single-use state bound to the authenticated Google administrator, exchanges the code server-side, writes credentials directly to Secret Manager, and returns only installation metadata. The first release keeps Slack as a confidential server-side client instead of enabling Slack's irreversible public-client PKCE mode; Cloudflare Managed OAuth still requires its separate PKCE release gate. Only administrators can revoke or replace the shared installation. Offboarding removes the employee's MIM-side Slack use grant without uninstalling the company-wide app. Least scopes, rotation, revocation, and exact-secret IAM remain mandatory.

## Offboarding and Inactivity Lifecycle

Deletion targets only reproducible, stateless compute resources. User applications may not rely on an ephemeral Cloud Run filesystem for persistent data. This compute-only lifecycle is the user's 2026-08-02 policy decision and supersedes the earlier foundation rule that required an administrator retirement action for every application deletion. Automatic deletion never applies to secrets, persistent data, audit history, or admitted source.

### Offboarding

When the identity sync observes that a user is suspended or removed from the MIM access group:

1. Immediately deny new or renewed Access sessions, plans, deployments, secret changes, and manual executions.
2. Periodic identity reconciliation quarantines schedules and access, cancels queued operations that have not mutated infrastructure, removes end-user application access, and revokes runtime secret bindings.
3. Mark owned workloads `quarantined`, retain their desired manifests and last healthy image digests, and notify administrators of a seven-day ownership-transfer window.
4. If an administrator transfers ownership, re-run admission, quota, access, and secret-attachment checks for the new owner before resuming; otherwise administrators retire resources under policy.
5. If no transfer occurs after seven days, delete the Cloud Run services, Cloud Run Jobs, and Cloud Scheduler jobs. Keep the source SHA, desired manifest, operation history, and audit history.
6. Retain the last healthy image digest for 30 additional days for recovery, then garbage-collect it if no active workload references it.

Secrets and any explicitly approved persistent data are locked and placed in an administrator retirement queue; they are never automatically destroyed merely because a user leaves. The release process must include an active-session latency test because the system must not assume that group removal kills every active session instantly.

### Inactivity

For a web service, meaningful activity is an authenticated request reaching the service. For a job, an execution attempt is activity; disabled jobs with no executions can become inactive. A repeatedly failing schedule follows the failure policy instead of being misclassified as unused.

- At 23 consecutive inactive days, notify the owner and administrators and show the planned deletion date.
- Any authenticated request, job execution, explicit keep action, redeploy, or ownership transfer resets the inactivity clock.
- At 30 consecutive inactive days, delete reproducible Cloud Run service/job and Scheduler resources, archive the desired manifest, and retain the last healthy image for 30 more days.
- An archived workload remains visible in usage and audit history and can be recreated through a new deployment plan while its admitted source still exists.

The lifecycle worker is idempotent and checks current activity, ownership, legal hold, admin exemption, and resource references immediately before deletion. A stale queue message cannot delete a reactivated workload.

## Failure, Repair, and Reconciliation

Operations use the states `planned`, `queued`, `building`, `deploying`, `verifying`, `succeeded`, `failed`, `rolled_back`, `cancelled`, and `quarantined`.

- Admission, authentication, quota, and policy failures happen before a build and create a sanitized audit event.
- A build failure preserves the active revision and returns the failing stage without leaking source tokens or environment values.
- A deploy must pass startup and authenticated health checks before traffic changes. A failed check restores the last healthy revision.
- Three consecutive scheduled failures disable the schedule, notify the owner, and offer log inspection, retry once, redeploy current SHA, or deploy a newer admitted SHA.
- The reconcile worker compares Firestore desired state with Cloud Run, IAM, Secret Manager attachment, Scheduler, and Artifact Registry state. It repairs safe drift or quarantines unexpected privilege expansion.
- User repair actions are limited to retry, redeploy an admitted SHA, rollback to the last retained healthy revision, pause, resume, and disable a schedule. Ownership transfer and policy exceptions are administrator-only.
- Logs are allowlisted and redacted before display. Raw authorization headers, cookies, secret-like values, source tokens, and environment values never enter user-visible errors.

## Security Boundaries

- MIM identities receive permissions only in the dedicated MIM project and only for the resource types they own.
- Deployed workloads receive no BigQuery, Cloud Resource Manager, IAM administration, billing, network administration, or service-account token permissions.
- Sensitive projects grant no roles to the MIM principal set and may add organization policy or VPC Service Controls defense in depth.
- Runtime workloads cannot select a VPC connector, custom service account, custom IAM, arbitrary domain, arbitrary build step, privileged container, or writable host mount.
- User-facing services cannot disable direct IAP, add `allUsers`, request project-level IAP access, or replace the exact owner/admin resource policy. Machine-only services cannot opt into public browser ingress.
- GitHub credentials are GitHub App or managed connection credentials held by the integration service; users cannot submit personal access tokens.
- Every Cloudflare assertion, webhook signature, plan hash, task identity token, and GCP resource name is verified at its trust boundary.
- Admin UI access changes MIM application state only; it is not a proxy for arbitrary GCP API calls.

## Verification and Acceptance

### Conversational and plugin contract

- Natural-language deploy, schedule, status, cost, pause, resume, and repair requests select the MIM skill and map only to documented MCP tools.
- Missing information produces one short question at a time.
- Mutating tools cannot run without a valid, unexpired plan hash and the MCP client's destructive-action confirmation.
- The plugin package, marketplace entry, MCP configuration, README examples, and update path validate with the Claude plugin validator.

### Identity and isolation

- Non-Madup identities, users outside the MIM group, suspended users, and expired Cloudflare tokens are denied.
- A valid Cloudflare user token sent directly to the MIM control plane's private `run.app` origin is denied without a fresh Worker origin signature; missing, invalid, stale, and replayed origin signatures are denied and never logged. User workload `run.app` URLs use the separate direct-IAP policy described above.
- A normal user cannot list or mutate another user's workload or quota.
- A normal user cannot open another user's IAP-protected `run.app` workload; unauthenticated requests are denied and only the exact owner/admin resource policy is present.
- An administrator can see all MIM usage without gaining access to any non-MIM project.
- Runtime, build, schedule, and deploy identities fail attempts to query protected BigQuery data, list sensitive projects, change IAM outside their role, or read unattached secrets.

### Deployment and scheduling

- An admitted Streamlit or Next.js repository deploys from an exact SHA to a bounded direct-IAP Cloud Run service with `ingress=all`, `iap_enabled=true`, no unauthenticated invoker, and an exact owner/admin IAP policy.
- An admitted scheduled script deploys to a bounded Cloud Run Job, and an hourly Asia/Seoul schedule executes through the MIM gate without overlap.
- Non-`madupmarketing`, unselected, forked, redirected, or mutable source references fail before build.
- `madup-dct/claude-plugins`, its release artifacts, and every other platform repository are explicitly rejected by user-workload admission and automatic deployment even though they are trusted sources for the MIM platform release path.
- Automatic default-branch deployment revalidates every push and preserves the last healthy revision on failure.

### Cost, usage, and lifecycle

- User and administrator views show the same directly attributed workload measurements with role-appropriate scope, distinguish estimated from finalized cost, and never charge shared platform overhead against a user's KRW target.
- The administrator view separately reports direct user cost, shared platform overhead, and the combined organization-wide cost used by the emergency ceiling.
- Service, schedule, secret, and KRW thresholds produce the documented 70%, 90%, and projected-limit actions.
- Overall emergency stop blocks new work and pauses non-exempt workloads without disabling MIM administration.
- Offboarding immediately quarantines resources, permits a seven-day transfer, and deletes only eligible compute after rechecking state.
- A 23-day inactive warning and 30-day compute deletion are idempotent, auditable, and cancelled by reactivation.
- Secret values never appear in API output, logs, dashboard HTML, audit events, or Claude responses.

### Operational verification

- Unit tests cover policy, state transitions, authorization, redaction, plan hashing, idempotency, cost thresholds, and lifecycle eligibility.
- Contract tests cover MCP tools, GitHub admission, Cloud Tasks identity, Cloud Build templates, GCP desired manifests, and billing attribution.
- Emulator or fake-client integration tests cover Firestore state, queued operations, rollback, schedules, offboarding, and inactivity cleanup.
- Staging canaries prove Cloudflare authentication, direct-origin rejection, direct-IAP break glass, Cloud Run limits, IAM exactness, denied sensitive-project access, and active-session latency after group removal before production rollout.
- Production release requires lint, type checking, unit/integration tests, container scanning, plugin validation, policy-diff review, and a post-deploy authenticated smoke test.
- The remote-MCP OAuth POC gate must pass authorization server metadata discovery, RFC 8707 `resource parameter`, PKCE S256, dynamic client registration or manual client ID fallback, callback compatibility, token refresh and reuse, and group-removal/session expiry latency before `mim.madupai.com` is documented as the live employee login.

## Deferred Work

- Wildcard user application domains such as `*.apps.madupai.com` and their origin-auth proxy.
- More than 50 active Cloudflare Access users or a paid identity tier.
- Stateful databases supplied to user workloads.
- Sub-hour schedules, parallel jobs, user-selectable compute sizes, GPUs, VPC connectors, arbitrary containers, or arbitrary infrastructure-as-code.
- Multi-project tenant isolation unless scale or security review proves the dedicated single-project boundary insufficient.
