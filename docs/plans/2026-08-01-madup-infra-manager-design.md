# Madup Infra Manager Design

> The approved conversational control-plane, usage dashboard, cost enforcement,
> GitHub release, offboarding, and inactivity lifecycle are specified in
> `docs/plans/2026-08-02-mim-control-plane-design.md`. This document remains the
> foundation and domain-boundary record.

## Goal

Create a Claude plugin and centrally operated control plane that lets Madup marketers deploy and schedule small internal applications without receiving direct development-infrastructure access.

The system must make the easy path safe: only approved GitHub repositories can be deployed, every workload receives bounded resources and cost controls, users can inspect only their own workloads, and the platform has no authority over Madup's pre-existing sensitive accounts, projects, or BigQuery data.

## Scope

The first release supports:

- `@madup.com` users who are active members of an explicit MIM access group.
- Source repositories owned by the `madupmarketing` GitHub organization and selected for the MIM GitHub App installation.
- Streamlit and Next.js web applications on Cloud Run.
- Scheduled scripts on Cloud Run Jobs triggered by Cloud Scheduler.
- A shared MIM dashboard for users and administrators.
- Direct access to the bootstrap service through Cloud Run's default `run.app` URL protected by direct IAP.
- A production-target `mim.madupai.com` hostname that is reached through Cloudflare Access Managed OAuth only after the remote-MCP OAuth POC and release gate pass.
- Secret metadata and approved application secrets in Secret Manager.
- Deployment, schedule, health, cost, and audit history.

The first release does not grant access to existing GCP projects, internal BigQuery datasets, personal Google accounts, Google Drive or Gmail, AWS accounts, personal GitHub repositories, or arbitrary third-party credentials.

## Trust Boundaries

MIM runs in a dedicated GCP folder or project set created for this platform. Its control-plane, build, and runtime identities receive permissions only on MIM-owned resources. They must not use ambient `gcloud` credentials, user Application Default Credentials, personal access tokens, or long-lived service-account keys.

Each deployed application receives a dedicated runtime service account. Build identities and runtime identities are separate. Workload identities receive only the narrow permissions required for logging and explicitly approved secrets.

Existing sensitive projects remain outside the MIM boundary. Internal BigQuery projects protect data with both IAM and VPC Service Controls. MIM workload principals receive no BigQuery roles, and sensitive projects explicitly deny applicable BigQuery permissions to the MIM principal set. The MIM project may use a dedicated billing-export dataset that contains only MIM cost data.

Users cannot upload service-account JSON, OAuth refresh tokens, cloud access keys, personal access tokens, or equivalent credentials as application secrets. Source and secret admission checks reject these credential classes. The plugin calls the MIM control-plane API with short-lived user authentication; it never shells out with a developer's existing cloud login. Employees never enter GCP project, organization, billing, Cloudflare, or operator values, and they never receive a shared API key. Public tracked configuration keeps only `MIM_OPERATOR_EMAIL`, `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, and `MIM_BILLING_ACCOUNT_ID`; the real values stay in operator-only files outside Git.

## Secret Policy

Secrets are modeled as logical integrations, not arbitrary blobs. Related fields should be bundled into one secret when the application consumes them together and does not need independent rotation.

- Default limit: 5 active integration secrets per user.
- Administrative hard ceiling: 10 active integration secrets per user.
- Application attachment limit: 5 secret resources per app, unless an administrator grants an explicit exception.
- Payload target: 16 KiB or smaller per secret version, well below Secret Manager's 64 KiB content limit.
- Version rule: exactly one enabled version per secret at a time.
- Rotation rule: create a new version, disable the previous version immediately, then destroy the previous version after a 7-day rollback window.
- Access control rule: grant `roles/secretmanager.secretAccessor` on the secret resource itself only to the exact runtime service account that needs it.
- Prohibited material: service-account JSON, OAuth refresh tokens, personal access tokens, cloud access keys, and equivalent credentials are never accepted as secret material.
- Visibility rule: secret metadata may be shown, but secret values are never re-shown after creation.
- Offboarding rule: secret access is locked or transferred during offboarding; secrets are not deleted automatically.

Secret Manager charges for active secret versions and access operations, so these caps keep secret storage and rotation bounded. After the free tier, active secret versions are billed at $0.06 per version per location, and access operations are billed at $0.03 per 10,000 operations.

## Identity and Offboarding

Access requires both an `@madup.com` Google Workspace account and membership in an explicit group such as `mim-users@madup.com`. The production employee flow uses browser Cloudflare Access Managed OAuth, then verifies Google Workspace identity and MIM-group membership before Claude receives a reusable token. After the first grant, Claude stores and refreshes the OAuth token and later calls reuse it. The system verifies status at login and performs a periodic directory synchronization.

When an account is suspended or removed from the group:

1. New or renewed Access sessions and new deployments are denied.
2. periodic identity reconciliation quarantines schedules and access, even if an active session has not expired yet.
3. Owned workloads are paused or quarantined according to policy, not deleted.
4. Administrators receive an ownership-transfer task so the platform transfers or retires resources under policy.
5. Secrets remain inaccessible until an administrator reassigns or retires the workload.

The system must not assume instant token death. Release verification includes an active-session latency test so offboarding behavior is measured rather than guessed.

## GitHub Admission

The GitHub App is installed only on selected repositories in `madupmarketing`. Every deployment verifies the GitHub installation ID and exact repository owner. Personal repositories, forks, unapproved repositories, and arbitrary Git URLs are rejected.

Production deployments use an approved branch policy. MIM records the repository, commit SHA, actor, security result, build, deployment, and rollback revision. Repository read access is separated from write or administration permissions; the first release does not need repository write access.

The existing Claude plugin marketplace is hosted separately at `madup-dct/claude-plugins`. That repository contains the MIM interaction surface, but it does not become an allowed application-source organization. Application deployments remain restricted to `madupmarketing`.

## Domain Design

The registered domain remains at Gabia with its existing nameservers. The apex `madupai.com` stays intentionally unconfigured.

The bootstrap phase does not require the employee-facing Cloudflare path to be live. `mim.madupai.com` remains the stable production hostname target, but employee login through that hostname is release-gated behind the remote-MCP OAuth POC. `*.apps.madupai.com` stays a deferred long-term design for a later shared custom-domain ingress decision.

The bootstrap service is accessed through Cloud Run's default `run.app` URL, protected by direct IAP on the Cloud Run service itself. No external load balancer exists in the initial phase, so there is no load-balancer-backed hostname, managed certificate, or nameserver work to complete before launch. That bootstrap surface does not change the later employee login contract: first MIM use in production still opens browser Cloudflare Access Managed OAuth rather than asking the employee for cloud IDs or shared secrets.

Cloud Run automatically distributes requests across ready instances of a service revision. With request-based billing, MIM starts at `min=0` and can scale up only as needed. `max=1` caps the service at a single instance, so Cloud Run cannot spread load across multiple instances in this phase. That setting is a scaling ceiling, not a KRW spending ceiling; spend control still comes from billing export and policy enforcement, not from the instance limit alone.

Reserved labels include `mim`, `admin`, `api`, `auth`, `status`, `www`, and infrastructure-defined names.

## OAuth Integrations

Google Workspace plus the MIM access group is the mandatory employee login path. Slack OAuth is optional and separate: it exists only for Slack-specific integration or notification setup and can never authorize MIM or cloud actions by itself. Madup centrally configures one company Slack app for the exact allowed organization or workspace. Only a MIM administrator may start the supported OAuth authorization URL, and a Slack administrator completes the shared installation; employees never install it or enter a Slack client ID, client secret, workspace ID, redirect URL, scope list, or token. The first release rejects installation paths initiated only from Slack admin surfaces because they do not always return MIM's state. The supported callback validates expiring actor-bound single-use state plus the exact approved Slack tenant, writes the resulting credential directly to Secret Manager, and exposes connection metadata only. Enterprise Grid uses an organization-ready installation; a non-Grid deployment is limited to one centrally approved workspace. Least scopes, rotation, revocation, and administrator-controlled uninstall apply.

## Deployment Classification

Before deployment, MIM detects the framework and workload shape:

- Streamlit web application: request-driven Cloud Run service with WebSocket-aware timeouts.
- Next.js web application: Cloud Run service using a verified production build.
- Scheduled script: Cloud Run Job with a Cloud Scheduler trigger.
- Unsupported or ambiguous repository: no deployment until an administrator or repository owner supplies an approved manifest.

Repositories cannot supply unrestricted infrastructure definitions. MIM renders the final Cloud Run configuration from an enforced policy template so application code cannot raise CPU, memory, instance, networking, or identity privileges.

## Cost and Quota Policy

Default user limits are deliberately small and configurable by administrators:

- Two active Cloud Run services.
- Three active schedules.
- Minimum instances `0`.
- Maximum instances `1` per service.
- Default resource size of 1 vCPU and 512 MiB memory, with no user-controlled increase.
- A monthly target allocation of KRW 1,000.
- An administrator-approved ceiling no greater than KRW 10,000 per user.

Cloud Billing budgets are advisory and delayed, so MIM does not treat them as a hard cap. It combines billing export data with Cloud Monitoring usage estimates, resource reservations, and gateway enforcement. The Cloud Run `max=1` limit is a runtime scaling guardrail, not a budget ceiling; it does not replace the KRW controls above.
Secret Manager also contributes to the KRW budget, so the secret limits above are intentionally tight: the platform should stay within the free tier where possible and keep billed active versions and access operations predictable.

- At 70% of allocation, notify the user and administrators.
- At 90%, block new deployments, scale increases, and new schedules.
- At the projected allocation limit, disable user routes and schedules so services scale to zero.
- Preserve a safety margin because billing and monitoring signals can lag.

Administrators can raise a limit for a bounded time with an audit reason. A user cannot raise their own quota.

## Lifecycle and Cleanup

Cost enforcement pauses workloads before deleting anything. Paused services retain an immutable deployment manifest and last healthy revision so an administrator can restore or transfer them.

The platform flags workloads with no traffic or execution for 30 days. The approved control-plane lifecycle in `docs/plans/2026-08-02-mim-control-plane-design.md` supersedes this foundation's application-deletion rule: reproducible stateless Cloud Run services, Jobs, and Scheduler resources may be deleted automatically after the documented warning, quarantine, transfer, and final eligibility checks. Persistent-data deletion and secret deletion still require an administrator-approved retirement action.

Failed deployments roll back to the last healthy revision. Scheduled jobs use bounded retries and stop after the configured failure threshold. The dashboard explains the failure, shows relevant sanitized logs, and offers a redeploy after the source is fixed.

## Dashboard

Users see only their applications, schedules, deployment revisions, health, estimated cost, quota state, approved secret names, and sanitized logs.

Administrators see organization-wide users, active and paused workloads, costs and projections, quota exceptions, deployment volume, schedule success rate, repeated failures, stale resources, offboarding transfers, and the full audit trail.

Secret values are never displayed after creation. Logs and error messages are redacted before display.

## Verification

The first production release must prove:

- The apex `madupai.com` remains without an application route.
- `mim.madupai.com` is reserved but remains unconfigured.
- The default Cloud Run `run.app` URL presents authenticated access only.
- The `*.apps.madupai.com` shared-custom-domain path remains deferred.
- A non-Madup account and a removed group member cannot log in.
- A repository outside `madupmarketing` cannot deploy.
- A MIM runtime identity cannot list or query protected BigQuery resources or existing sensitive projects.
- A user cannot increase resource limits or access another user's workload.
- Cost thresholds block new work and pause routes and schedules as designed.
- Rollback, offboarding, ownership transfer, and restoration preserve audit history.
- The remote-MCP OAuth release gate passes authorization server metadata discovery, RFC 8707 `resource parameter`, PKCE S256, dynamic client registration or manual client ID fallback, callback compatibility, token refresh and reuse, and group-removal/session expiry latency checks before employee login is announced as live.

## Approved Bootstrap Boundary

The public bootstrap contract publishes only the operator placeholders `MIM_OPERATOR_EMAIL`, `MIM_PROJECT_ID`, `MIM_ORGANIZATION_ID`, and `MIM_BILLING_ACCOUNT_ID`. Provisioning must reject every other account, project, organization, and billing account instead of relying on the active global `gcloud` configuration, but the real reviewed values stay in ignored operator files rather than tracked documentation.
