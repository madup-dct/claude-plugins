# MIM `madup.app` Edge and Managed-App Gateway Design

## Status and decision

This design is approved for implementation as of 2026-08-05. It supersedes the
Cloudflare hostname, deferred wildcard-domain, and Google-load-balancer sections
of the earlier MIM design documents. The reusable GCP control-plane, deployment,
quota, billing, directory-sync, offboarding, lifecycle, and sensitive-project
boundaries remain in force.

The dedicated `madup.app` zone is authoritative on Cloudflare. MIM will use:

- `mim.madup.app` for the dashboard, HTTP API, GitHub webhook, OAuth callback,
  and remote MCP endpoint;
- `<slug>.madup.app` for user-owned Streamlit and Next.js applications;
- Cloudflare DNS, Access, and one edge Worker as the public entry layer;
- Cloud Run in the reviewed MIM project and fixed region as the compute layer;
- no Google external HTTPS load balancer and no Firebase Hosting proxy.

The Cloudflare account contains other zones. Every plan, token, readback, and
mutation therefore has to be pinned to the exact `madup.app` zone and exact
MIM-named account resources. Public source must not contain real account, zone,
organization, billing, project, application, or token identifiers.

## Goals

- Let an active, approved `@madup.com` employee ask Claude to deploy, schedule,
  inspect, repair, pause, or retire a small internal workload.
- Give each web workload a stable `<slug>.madup.app` address without creating a
  DNS record for every deployment.
- Keep Cloud Run at minimum instances zero and enforce service, schedule,
  secret, and KRW limits per user.
- Preserve exact user ownership and administrator visibility without granting
  employees GCP, Cloudflare, GitHub administration, or shared credentials.
- Keep user workload origins inaccessible without MIM authorization.
- Avoid any authority over non-MIM Cloudflare zones, GCP projects, BigQuery
  datasets, repositories, or personal accounts.

## Non-goals

- The apex `madup.app` is not an application hostname in the first release.
- MIM does not provide stateful databases, arbitrary VPC access, arbitrary
  infrastructure-as-code, service-account keys, or unrestricted containers.
- Slack is not an identity provider for MIM. It remains an optional integration
  configured only after Google Workspace login.
- The platform does not promise a perfectly instantaneous billing hard stop;
  it combines runtime limits, estimated usage, billing export, and emergency
  policy because cloud billing data can lag.

## Public request flow

```text
Employee / Claude
        |
        v
Cloudflare DNS + Access + Worker
        |
        +-- mim.madup.app --------> MIM control plane
        |                            (MCP, dashboard, API, webhooks)
        |
        +-- <slug>.madup.app -----> MIM app gateway
                                      |
                                      | Google-signed ID token
                                      v
                                  private-IAM Cloud Run app
```

Cloudflare Access authenticates employees through the company Google Workspace
identity provider. The Worker removes caller-supplied credential and origin
headers, preserves only reviewed request metadata, and signs the exact method,
public host, path, body digest, timestamp, request ID, and destination class.
The receiving control plane or gateway validates that proof before performing
JWT verification, tenant lookup, logging, or cloud work.

GitHub webhook routes bypass interactive Access only on the exact webhook path.
They still require the existing GitHub signature and installation/repository
admission checks. Machine routes continue to use exact Google service-account
OIDC and never reuse browser authentication.

## DNS, TLS, and hostname rules

Cloudflare remains the authoritative DNS provider. The zone uses full setup, so
Universal SSL supplies the apex and first-level wildcard certificate. Only
first-level application hosts are supported in the initial release.

Reviewed DNS intent:

- one proxied `mim` record whose origin is the control-plane Cloud Run service;
- one proxied wildcard `*` record whose origin is the shared app-gateway Cloud
  Run service;
- specific records always take precedence over the wildcard;
- no application record at the apex;
- no nameserver mutation during release.

The Worker owns exact routes for `mim.madup.app/*` and `*.madup.app/*`. The
specific `mim` route is evaluated before the wildcard route. An unregistered
application hostname resolves at DNS but the gateway returns a generic 404
without revealing whether a workload, user, or historical deployment exists.

Application slugs are lower-case, immutable after allocation, globally unique,
and centrally registered. Reserved names include `mim`, `admin`, `api`, `auth`,
`status`, `www`, `mail`, and infrastructure-defined names. Slugs cannot be raw
user email addresses, repository names without normalization, or values that
contain tenant-identifying secrets.

## Identity and authorization

### Dashboard, API, and MCP

The existing Cloudflare Access Managed OAuth boundary remains the MCP
authorization server. It must continue to satisfy protected-resource metadata,
authorization-server discovery, PKCE S256, constrained dynamic client
registration or reviewed manual-client fallback, refresh, revocation, exact
issuer/audience validation, and Claude callback compatibility.

Access is necessary but not sufficient. Every authorized request also passes
the central identity policy, which requires:

- a verified Google Workspace identity with the hosted domain `madup.com`;
- an active directory record;
- current membership in the approved MIM access group;
- a permitted action on an exact MIM resource;
- ownership for user-scoped resources or the administrator role.

Directory sync and the request-time identity lookup remain authoritative.
Removing or suspending an employee denies refreshed sessions and API actions,
quarantines schedules and workloads, and starts the existing transfer/cleanup
lifecycle. A still-unexpired edge token cannot override an inactive central
identity record.

### Managed application hosts

A wildcard Access application protects user app hosts and uses the same Google
Workspace identity provider. The app gateway verifies its own exact Access
audience, then asks the central policy for permission to view the workload
resolved from the signed public host. The gateway never trusts a slug, email,
owner header, upstream URL, or service name supplied by the browser.

The initial Access plan is capped at the reviewed pilot seat allowance. Release
planning blocks rather than silently incurring per-seat charges when the
approved-user count exceeds the configured plan limit.

## App gateway and private workload origins

User applications are not made unauthenticated. Each workload keeps a dedicated
runtime service account with no project-level roles. Its Cloud Run service grants
`run.invoker` only to the shared MIM app-gateway identity and reviewed break-glass
administrators. Direct browser requests to the default `run.app` address fail IAM
authentication.

When a user attaches a managed secret, the Cloud Run revision references an
exact secret resource and numeric version through a deterministic safe
environment-variable name. Only that workload's dedicated runtime service
account receives resource-level `secretAccessor` on the attached secret. The
control plane accepts a new value only transiently on the authenticated write
path and never reads it back, persists it outside Secret Manager, logs it, or
displays it; the gateway and dashboards never receive it. Detaching, rotating,
offboarding, or retiring removes stale runtime access before resource cleanup.

The app gateway is a small, independently deployed reverse proxy. It:

1. verifies Worker origin proof and freshness locally, then rejects replay when
   its mandatory central authorization call cannot make the one durable request
   claim;
2. verifies the wildcard Access JWT;
3. resolves the signed public hostname through the central workload registry;
4. enforces active user, group, ownership, workload, cost, and lifecycle state;
5. obtains a short-lived Google ID token from the Cloud Run metadata server for
   the exact destination service audience;
6. proxies the request while stripping hop-by-hop, caller credential, internal
   routing, and spoofable forwarding headers;
7. preserves HTTP streaming, WebSocket upgrades, original scheme, and reviewed
   public-host information required by Streamlit and Next.js;
8. emits only redacted request, latency, status, owner-hash, and workload-hash
   telemetry.

The gateway does not receive Firestore, directory, quota, billing, or general
Secret Manager roles. Its only Secret Manager permission is resource-level
`secretAccessor` on the one Worker-proof secret used by its own Cloud Run
revision, whose reference is pinned to an exact numeric version. It cannot list
secrets or access any user/workload secret. It calls one private control-plane
authorization endpoint using its Google service identity. That endpoint owns
the single durable replay claim, applies the existing central policy, and
returns only a short-lived, exact workload-routing decision. The gateway may
perform bounded process-local duplicate suppression, but it never attempts a
second durable claim and never retries a denied authorization. The gateway does
not cache an allow decision in the first release, so offboarding, cost holds,
pause, and ownership changes take effect on the next HTTP request or WebSocket
handshake.

The gateway must not accept an arbitrary origin URL. After a healthy deployment,
the control plane reads the actual `service.uri` from the exact expected Cloud
Run service resource, validates a credential-free HTTPS `.run.app` origin with
no port, path, query, or fragment, and persists that reviewed destination in the
hostname binding. It does not derive the URI from a project-number or region
hostname formula. The gateway accepts only the machine-authenticated routing
decision, independently checks the HTTPS `.run.app` shape, reviewed
`mim-svc-` prefix, and equal URL/audience, and never accepts a browser-supplied
origin. Redirects back to a `run.app` origin are rewritten or denied so the
private endpoint is not leaked.
The gateway sends its metadata-issued identity token in the reviewed Cloud Run
serverless-authorization header and never forwards the Cloudflare JWT,
Cloudflare Access cookies, MIM control-session cookies, or browser authorization
headers to a workload. Host-isolated application cookies required by Streamlit
or Next.js may pass after the edge/control credential names are removed; the
control plane itself issues only host-only cookies and never a `.madup.app`
domain cookie. Workload `Set-Cookie` headers are constrained to the signed public
host, and a `.run.app` or parent-domain cookie is rewritten safely or denied.
The gateway never stores a Google token after the proxied request or WebSocket
connection is established.

Separate Worker HMAC keys and Access audiences are used for the control plane
and app gateway. A proof intended for one destination class cannot be replayed
against the other. Key rotation supports current and previous verification keys
for a bounded overlap, then removes the previous key.

The first release deliberately prefers IAM-protected public network endpoints
over a Serverless VPC Access connector. IAM gives the required origin denial
without the fixed connector cost and network complexity. Moving targets to
internal ingress is a later hardening option if scale or policy justifies it.

## Framework behavior

- Streamlit deployments must pass an end-to-end WebSocket canary through Access,
  Worker, gateway, and Cloud Run. The current generic 300-second request timeout
  is not reused: Streamlit receives a reviewed timeout of at most 3,600 seconds,
  and the client must reconnect after the bound. Idle and absolute timeouts stay
  finite so one abandoned session cannot hold capacity indefinitely.
- Next.js deployments use a production build and must pass asset, navigation,
  server-rendering, and forwarded-host canaries.
- Long-running deployment and repair work is never performed inside a public
  request. The control plane validates a reviewed plan, enqueues an immutable
  operation, and returns its operation identifier.
- Scheduled scripts remain Cloud Run Jobs invoked by Cloud Scheduler with an
  exact service account and reviewed hourly-or-coarser policy.

## Cloudflare account and zone safety

The operator configuration supplies exact private values for the company
account, `madup.app` zone, Access team, Access applications, and Worker. Every
Cloudflare command must prove:

- the zone ID resolves to exactly `madup.app` in the expected account;
- the zone is active on the currently authoritative nameservers;
- every planned DNS record name ends at the exact `madup.app` zone boundary;
- Worker routes are exactly the two reviewed MIM patterns;
- Access applications and policies have exact MIM names, audiences, domains,
  callback policy, and Google Workspace constraints;
- no action targets another zone, account, Worker, Access application, DNS
  record, ruleset, or certificate;
- readback after apply matches the reviewed immutable plan.

The DNS token is restricted to the exact `madup.app` zone. Access application
permissions are account-scoped by Cloudflare, so the implementation compensates
with exact resource discovery, stable MIM names, immutable IDs captured in the
plan, no wildcard deletes, no list-and-rewrite behavior, and post-apply readback.
The token value remains in an operator-only environment or secret store and is
never pasted into Claude, written to Git, printed, or exposed to user workloads.

## GCP boundary

Every GCP command supplies the reviewed operator account and MIM project
explicitly; global `gcloud` defaults are irrelevant. Provisioning fails closed
unless the project, organization, billing relationship, fixed region, protected
project denylist, and service identities match operator-only configuration.

The app gateway, control plane, deploy worker, schedule gateway, maintenance
jobs, build identities, and per-workload runtime identities remain separate.
No MIM identity receives permission to enumerate or query unrelated projects or
internal BigQuery data. Billing access remains limited to the sanitized MIM-only
view.

## Cost and capacity policy

- No Google external HTTPS load balancer is provisioned.
- Cloud Run services use request-based billing and minimum instances zero.
- A user app defaults to maximum instances one, 1 vCPU, and 512 MiB.
- Per-user service, schedule, secret, and monthly-KRW limits remain enforced.
- Shared control-plane, gateway, Cloudflare, logging, registry, and billing-view
  costs are shown as platform overhead, not charged against one employee.
- Cloudflare plan and Worker quotas are observed centrally. Crossing a reviewed
  free/pilot allowance blocks admission or requires an audited administrator
  decision; it never silently upgrades a user-funded resource.
- Organization emergency ceilings pause new work and non-exempt workloads while
  leaving the administrative recovery surface available.

## Lifecycle and observability

Deleting an application does not change wildcard DNS. Hostname binding material
is immutable, while its state moves through `ACTIVE`, `DISABLED`, and `RETIRED`.
Offboarding, a cost hold, inactivity, or manual pause moves the binding to
`DISABLED` before any schedule or compute cleanup. Final archive/deletion moves
it to `RETIRED` before compute deletion, and a retired hostname is never reused.
A restore/redeploy may return a disabled binding to `ACTIVE` only after a healthy
verified deployment and fresh user, group, ownership, and cost checks. Unknown,
disabled, and retired hosts all return the same generic 404. The gateway registry
is therefore updated first, access is denied, schedules are paused, and eligible
stateless compute is deleted only after the existing warning and quarantine
checks.

Users see only their own hostnames, workloads, revisions, schedules, cost,
quota, secret metadata, health, and sanitized failures. Administrators see
organization totals, shared overhead, Access-seat use, Worker usage, gateway
health, deployment volume, schedule success, offboarding, inactivity, and audit
events. Secret values, raw OAuth material, HMAC keys, Cloudflare identifiers,
Google tokens, private origins, and exception text never appear in either view.

## Migration from the current repository

Implementation must:

- replace public `mim.madupai.com` constants with one reviewed public-origin
  contract for `mim.madup.app` and one app-host suffix contract;
- preserve the current central policy, Cloudflare JWT, Worker HMAC, machine OIDC,
  quota, billing, lifecycle, GitHub, and Slack separation where behavior matches;
- extend the Worker and edge plan/apply/readback tests for the exact zone,
  specific host, wildcard host, and separate gateway origin;
- add the app gateway, its exact IAM boundary, workload registry lookup, HTTP and
  WebSocket proxy contracts, and end-to-end canaries;
- change desired-state rendering from direct-IAP-only user URLs to gateway-only
  public hostnames while retaining private-IAM origins and break glass;
- update MCP discovery, callback, webhook, docs, release gates, and staging
  canaries to `madup.app`;
- prove tracked and outbound Git history contains no private operator values.

No existing Cloudflare or GCP resource is deleted as part of migration unless an
exact MIM-owned resource is first discovered, captured in a reviewed plan, and
explicitly approved for retirement. The current production boundary is not live,
so the rollout does not depend on serving traffic from the old hostname.

## Alternatives rejected

- **Google external HTTPS load balancer:** production-capable but adds shared
  forwarding-rule cost even with no traffic and duplicates the already active
  dedicated Cloudflare edge.
- **Firebase Hosting:** avoids the forwarding-rule charge but imposes a 60-second
  proxy timeout and does not provide the required wildcard managed-app boundary.
- **Cloud Run domain mapping:** limited/preview, unavailable in the fixed Seoul
  region, and unsuitable for automatic wildcard application hosts.
- **Public user-app origins with only hidden URLs:** origin URLs are not secrets
  and would bypass tenant authorization.
- **One service-account key stored in the Worker:** violates the keyless GCP
  boundary and creates a long-lived cross-cloud credential.
- **One Access allow rule as the sole app authorization:** authenticates an
  employee but does not prove that employee owns the requested workload.

## Release acceptance criteria

Release is blocked until tests and staging prove:

- only `mim.madup.app` and registered first-level app hosts are served;
- apex and reserved/unregistered hosts do not expose an application;
- non-Madup, inactive, removed-group, and cross-owner requests are denied;
- direct control-plane and app-gateway origins reject missing or replayed Worker
  proof, and direct user-app origins reject callers without exact Google IAM;
- Claude completes OAuth discovery, PKCE login, token refresh, and MCP calls;
- Streamlit WebSockets and Next.js assets work through the complete edge path;
- GitHub webhook and Google machine routes retain their independent signatures;
- per-user and organization cost/quota stops work as documented;
- offboarding and inactivity transitions remove access before compute cleanup;
- the Cloudflare apply plan cannot name or mutate another zone or application;
- the GCP apply plan cannot name or mutate another project;
- lint, type checking, unit, integration, shell, Worker, container, security,
  secret-history, staging, and post-deploy smoke gates pass.

## Operator and user handoff

Employees install the plugin and sign in with Google when Claude opens Access;
they never enter domain, Cloudflare, GCP, billing, organization, or API values.

The operator supplies no DNS values manually because Cloudflare is authoritative.
After local verification, MIM produces a reviewed exact-zone plan, applies the
two proxied records, Access applications, Worker routes, secrets, and Cloud Run
services, then verifies public DNS, TLS, OAuth, MCP, gateway, and application
canaries. Any interactive Google or Cloudflare security challenge remains a
human confirmation step; secrets and MFA codes are never requested in chat.

## Primary references

- Cloudflare wildcard DNS records:
  <https://developers.cloudflare.com/dns/manage-dns-records/reference/wildcard-dns-records/>
- Cloudflare Universal SSL coverage:
  <https://developers.cloudflare.com/ssl/edge-certificates/universal-ssl/enable-universal-ssl/>
- Cloudflare Access public self-hosted applications:
  <https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/>
- Cloudflare Access authorization cookies and wildcard behavior:
  <https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/>
- Cloudflare Worker WebSocket proxy behavior:
  <https://developers.cloudflare.com/workers/runtime-apis/websockets/>
- Cloudflare API token permission scopes:
  <https://developers.cloudflare.com/fundamentals/api/reference/permissions/>
- Google Cloud Run service-to-service authentication:
  <https://cloud.google.com/run/docs/authenticating/service-to-service>
