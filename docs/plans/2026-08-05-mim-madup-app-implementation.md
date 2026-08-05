# MIM `madup.app` Production Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Madup Infra Manager usable from Claude at `https://mim.madup.app/mcp`, publish approved employee-owned Streamlit and Next.js workloads at `<slug>.madup.app`, and preserve exact tenant, cost, lifecycle, GitHub, Cloudflare, GCP, BigQuery, and secret boundaries.

**Architecture:** Cloudflare DNS, Access, and one Worker form the public edge. `mim.madup.app` reaches the existing control plane. First-level application hosts reach a standalone Go app gateway. The gateway validates the Worker proof and the wildcard Access JWT, asks the private schedule-gateway runtime for a fresh host/identity/cost authorization decision using Google OIDC, obtains an exact-audience Google ID token from the metadata server, and reverse proxies to a Cloud Run service whose only normal invoker is the app-gateway service account. App hostname binding material is immutable and is created only after a verified deployment; access state moves explicitly through `ACTIVE`, `DISABLED`, and `RETIRED`.

**Technology:** Python 3.13/FastAPI, Firestore, Google Cloud Run v2, stdlib Go 1.24, Cloudflare Workers JavaScript, Bash/Python infrastructure contracts, Google Cloud CLI, Wrangler, unittest, Node test runner, Docker.

## Non-negotiable boundaries

- Every GCP command includes the exact operator-private MIM project and operator account from the reviewed configuration; ambient defaults never select a target. Real identifiers never enter public source.
- Every Cloudflare discovery, plan, mutation, and readback is pinned to the exact active `madup.app` zone and exact MIM resource names. No nameserver mutation and no other zone mutation are permitted.
- Application source is admitted only from selected repositories owned by `madupmarketing`.
- Employees receive no Cloudflare, GCP, GitHub administrator, billing, organization, or shared secret values.
- No service-account keys are created. Google service identity tokens come from the metadata server and remain in memory.
- The app gateway receives no BigQuery, directory, Cloud Scheduler, Cloud Run Admin, or project-wide role. Its only Secret Manager permission is resource-level `secretAccessor` on its own Worker-proof secret; the Cloud Run revision pins an exact numeric version. It cannot list secrets or access any user/workload secret.
- User application services remain IAM-protected and reject direct unauthenticated `run.app` requests.
- Cloud Run web services remain `min=0`, `max=1`, `1 vCPU`, `512 MiB`. Streamlit uses an exact 3,600-second request bound; Next.js remains at 300 seconds.
- No production apply occurs until local tests, code review, security review, secret/history scan, immutable plan review, and exact target readback pass.
- Never read, print, stage, or copy operator-private `infra/**/config.env`, `protected-projects.exact`, `denylist.exact`, or `.state/**` material.

## Parallel ownership map

The implementation may run in parallel only across these disjoint ownership lanes. A lane must not edit another lane's files, revert concurrent changes, or broaden its scope without handing the conflict to the leader.

| Lane | Exclusive ownership during parallel implementation |
| --- | --- |
| A — public contract | `.mcp.json`, root plugin-contract tests, `config.py` + `test_config.py`, bootstrap/Slack host tests, domain config/tests, GitHub connection scripts/tests, public README/operations/skill examples |
| B — Worker | `plugins/madup-infra-manager/edge/worker/**` |
| C — Go gateway | new `plugins/madup-infra-manager/app-gateway-go/**` |
| D — control authorization | app-host binding model/store files, `security/origin.py`, app authorization service/API, schedule-gateway composition, their focused tests |
| E — workload runtime | desired-state/secret render and persistence, Secret Manager metadata/IAM adapter, Cloud Run adapter, runtime naming/execution ports, deploy worker binding creation, gateway-IAM/lifecycle access effects, focused tests |
| F — infrastructure/release | `infra/edge/**`, `infra/runtime-bootstrap/**`, `infra/control-plane/**`, `infra/release/**`, `tests/test_public_release_guard.py`, staging/release shell contracts |

The leader owns integration-only edits to shared runtime composition, dashboard presentation, global release verification, and final documentation after the six lanes land.

## Cross-language security contracts

### Worker proof v2

The Worker, Python control plane, and Go gateway must render these exact UTF-8 lines, with no trailing newline:

```text
mim-origin-v2
<destination-class: control-plane|app-gateway>
<UPPERCASE METHOD>
<lower-case public host>
<canonical path and optional query>
<lower-case SHA-256 body digest>
<UTC epoch seconds>
<request UUID>
<key id>
```

Control-plane and app-gateway destinations use different key IDs and secret values. The public host and destination class are signed, so a proof cannot be replayed across a host or boundary.

The Worker strips every caller-supplied `X-MIM-*` header, then sets exactly `X-MIM-Origin-Key-Id`, `X-MIM-Origin-Timestamp`, `X-MIM-Origin-Request-Id`, `X-MIM-Origin-Public-Host`, `X-MIM-Origin-Destination-Class`, and `X-MIM-Origin-Signature`. Receivers reject missing, duplicate, unknown, or conflicting proof headers and recompute the body digest from the received bytes. Only one exact `Cf-Access-Jwt-Assertion` selected from the Access-protected request is forwarded for JWT verification; all other `Cf-Access-*` headers are removed.

### Private app authorization API

`POST /v1/apps/authorize` is served by the existing private schedule-gateway runtime, but it has a route-specific expected caller of `mim-app-gateway@<project>.iam.gserviceaccount.com`. The schedule execution route continues to accept only its current scheduler caller.

Exact request fields:

```json
{
  "schema": "mim.app-authorization.v1",
  "public_host": "sample-a1b2c3d4e5f6.madup.app",
  "method": "GET",
  "request_target": "/path?query=value",
  "access_subject": "opaque-access-subject",
  "access_email": "person@madup.com",
  "edge_request_id": "uuid",
  "edge_timestamp": 1785859200,
  "edge_body_sha256": "64-lower-hex"
}
```

Exact successful response fields:

```json
{
  "schema": "mim.app-authorization.v1",
  "public_host": "sample-a1b2c3d4e5f6.madup.app",
  "workload_id": "opaque-workload-id",
  "upstream_url": "<exact-service-uri-from-cloud-run-readback>",
  "upstream_audience": "<same-exact-service-uri-from-cloud-run-readback>",
  "expires_at": "2026-08-05T12:00:30Z"
}
```

All denials use one generic response and reveal no owner, repository, workload, origin, or lifecycle state.

## Task 0: Preserve the green baseline

**Evidence already collected:** `bash plugins/madup-infra-manager/infra/release/verify.sh --local` exited 0 with 686 Python unit tests, runtime/bootstrap, release, integration, staging contracts, 21 Worker tests, Docker builds, and secret scan passing.

Before each integration checkpoint:

```bash
git status --short
git diff --check
```

Expected: only files owned by active lanes are modified; `git diff --check` prints nothing.

## Task 1: Migrate and centralize the public hostname contract

**Lane A files:**

- Modify: `plugins/madup-infra-manager/.mcp.json`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/config.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_config.py`
- Modify: `plugins/madup-infra-manager/control-plane/bootstrap/app.py`
- Modify: `plugins/madup-infra-manager/control-plane/bootstrap/test_app.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_slack_oauth.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/integration/test_slack_oauth_flow.py`
- Modify: `plugins/madup-infra-manager/infra/domain/config_lib.sh`
- Modify: `plugins/madup-infra-manager/infra/domain/test_apply_cloud_run.sh`
- Modify: `plugins/madup-infra-manager/infra/github/plan_connection.sh`
- Modify: `plugins/madup-infra-manager/infra/github/apply_connection.sh`
- Modify: `plugins/madup-infra-manager/infra/github/test_plan_connection.sh`
- Modify: `plugins/madup-infra-manager/infra/github/test_apply_connection.sh`
- Modify: `tests/test_madup_infra_manager_plugin.py`
- Modify: `README.md`
- Modify: `plugins/madup-infra-manager/README.md`
- Modify: `plugins/madup-infra-manager/docs/operations.md`
- Modify: `plugins/madup-infra-manager/skills/madup-infra-manager/SKILL.md`
- Modify only where active behavior is described: `plugins/madup-infra-manager/skills/madup-infra-manager/references/examples.md`

### Step 1.1: Write failing public-contract expectations

Change tests first to require:

- `PUBLIC_ORIGIN == "https://mim.madup.app"`
- `MCP_URL == "https://mim.madup.app/mcp"`
- `APP_HOST_SUFFIX == "madup.app"`
- the plugin manifest exposes exactly the new MCP URL;
- GitHub webhook is exactly `https://mim.madup.app/v1/webhooks/github`;
- Slack callback is exactly `https://mim.madup.app/slack/oauth/callback`;
- active public docs do not tell employees to use `madupai.com`.

Run:

```bash
plugins/madup-infra-manager/control-plane/.venv/bin/python -m unittest tests.test_madup_infra_manager_plugin
cd plugins/madup-infra-manager/control-plane
.venv/bin/python -m unittest tests.test_config
.venv/bin/python -m unittest discover -s bootstrap -p 'test_app.py'
cd ..
bash infra/domain/test_apply_cloud_run.sh
```

Expected: FAIL only on old hostname expectations.

### Step 1.2: Implement one fixed public-origin source

Add fixed `PUBLIC_HOSTNAME`, `PUBLIC_ORIGIN`, `MCP_URL`, and `APP_HOST_SUFFIX` constants in `config.py`. Shell contracts derive webhook and callback URLs from one fixed `mim.madup.app` constant rather than accepting user input. Historical plan documents remain immutable records; active runtime/plugin/operator documents move to the new contract.

### Step 1.3: Verify focused public contracts

```bash
plugins/madup-infra-manager/control-plane/.venv/bin/python -m unittest tests.test_madup_infra_manager_plugin
cd plugins/madup-infra-manager/control-plane
.venv/bin/python -m unittest tests.test_config tests.test_slack_oauth tests.integration.test_slack_oauth_flow
.venv/bin/python -m unittest discover -s bootstrap -p 'test_app.py'
cd ../../..
bash plugins/madup-infra-manager/infra/domain/test_apply_cloud_run.sh
bash plugins/madup-infra-manager/infra/github/test_plan_connection.sh
bash plugins/madup-infra-manager/infra/github/test_apply_connection.sh
```

Expected: PASS for Lane A-owned plugin, control-plane, OAuth, domain, GitHub, and documentation surfaces. A repository scan may still find `mim.madupai.com` in superseded historical plans and in Lane F-owned release files until Lane F completes; the final integration guard, not this parallel checkpoint, proves no old hostname remains in active plugin/runtime/release files.

## Task 2: Upgrade the Worker to a two-destination edge

**Lane B files:**

- Modify: `plugins/madup-infra-manager/edge/worker/src/index.js`
- Modify: `plugins/madup-infra-manager/edge/worker/test/index.test.js`
- Modify: `plugins/madup-infra-manager/edge/worker/wrangler.jsonc`
- Modify: `plugins/madup-infra-manager/edge/worker/README.md`

### Step 2.1: Add failing Worker tests

Add tests proving:

- only exact `mim.madup.app` routes go to the control origin;
- only one-label, non-reserved `<slug>.madup.app` hosts go to the app-gateway origin;
- apex, nested hosts, Unicode/punycode surprises, ports, userinfo, reserved names, and other suffixes are denied;
- caller-provided `Authorization`, `Proxy-Authorization`, `X-MIM-*`, `Forwarded`, and `X-Forwarded-*` headers never reach either origin; every `Cf-Access-*` header is removed except the one exact non-empty Access JWT assertion that the receiving boundary verifies, and duplicates are denied;
- only exact GitHub webhook route bypasses Access on the control host;
- all app routes require an Access assertion;
- app requests support GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS but deny CONNECT and TRACE;
- control and app requests use different secrets and emit the exact v2 proof including host and destination class;
- host-isolated app-local cookies survive, but Cloudflare Access/MIM credential cookies and browser authorization headers are removed; control-plane session cookies are host-only and never scoped to `.madup.app`;
- WebSocket upgrade headers and streaming response semantics survive;
- request bodies beyond the reviewed limit fail before origin fetch.

Run:

```bash
cd plugins/madup-infra-manager/edge/worker
node --test
```

Expected: FAIL on the new wildcard/destination/proof cases.

### Step 2.2: Implement two exact configurations

Replace the single `MIM_PUBLIC_HOSTNAME`, `MIM_ORIGIN`, key, and route configuration with exact control-host and app-suffix settings, two origins, two key IDs, and two Worker secrets. Do not allow a request header, path, or query parameter to choose an origin.

### Step 2.3: Verify Worker and dry-run bundle

```bash
cd plugins/madup-infra-manager/edge/worker
node --test
npx wrangler deploy --dry-run
```

Expected: all tests pass and the dry run needs no live mutation.

## Task 3: Add immutable application hostname bindings and central authorization

**Lane D files:**

- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/domain/models.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/ports/store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/memory_store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/firestore_store.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/security/origin.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/security/identity.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/api.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/app_hostname.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/app_gateway_authorization.py`
- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/app_gateway_api.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/machine_api.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_origin_hmac.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_api_readonly.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_central_identity_gateway.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_dashboard_views.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_deployment_api.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_mcp_contract.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_mcp_http.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_app_hostname.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_app_gateway_authorization.py`
- Create: `plugins/madup-infra-manager/control-plane/tests/test_app_gateway_api.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_machine_api.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/integration/test_firestore_operations.py`

### Step 3.1: Lock the binding model with failing tests

Add `AppHostnameBinding` as a separate record whose routing material is immutable. It contains exact public hostname, workload/owner IDs, workload kind, exact Cloud Run service resource, read-back upstream URL/audience, `ACTIVE|DISABLED|RETIRED` state, timestamps, and version. The hostname is the logical store key. Only the state/version/timestamps may change after creation.

Tests must prove:

- slug is deterministic from immutable workload name and ID, DNS-label safe, at most 63 bytes, globally collision-resistant through a 12-hex hash, and never reserved;
- only first-level `*.madup.app` hostnames are accepted;
- the upstream is the actual `service.uri` read from the exact expected Seoul Cloud Run service resource after health verification, not a derived hostname formula;
- the upstream is credential-free HTTPS, has no explicit port/path/query/fragment, ends in `.run.app`, begins with the expected `mim-svc-<12hex>-` service prefix, and exactly equals its audience;
- create is idempotent only for identical binding material;
- host, workload, owner, kind, origin, and audience cannot be rebound by an update;
- only reviewed state transitions are permitted: healthy deploy creates `ACTIVE`; pause/offboarding/cost/inactivity moves `ACTIVE` to `DISABLED`; final archive/delete moves `ACTIVE|DISABLED` to `RETIRED`; a healthy verified restore may move only `DISABLED` back to `ACTIVE`; `RETIRED` is terminal and its host is never reused;
- Firestore logical IDs cannot escape the collection or alias another host.

Run the focused tests and observe failure before implementation.

### Step 3.2: Implement exact storage and proof v2

Extend the MemoryStore and FirestoreStore through their existing generic record-spec pattern. Upgrade Python origin canonicalization to the v2 message and require exact `public_host` and `destination_class="control-plane"` for the control plane. Update both public request-construction paths in `api.py` plus every `OriginRequest`-building test helper; a legacy method/path/body-only proof must fail closed on all dashboard, MCP, GitHub, deployment, and read-only routes.

### Step 3.3: Add the app authorization policy

The service must, in this order:

1. validate the exact host/request material and freshness;
2. atomically make the one durable `edge_request_id` claim in the existing replay store on behalf of the gateway;
3. resolve the immutable binding and workload;
4. resolve and reauthorize the active, fresh, required-group user through `IdentityPolicy.authorize_resolved_user`;
5. require owner or administrator;
6. require an active web workload;
7. build the current UTC-month cost snapshot and deny `pause` or `emergency_stop` decisions;
8. return a maximum-30-second routing decision with no owner/repository/private configuration fields.

The Go gateway validates signature/freshness before this call and may suppress a duplicate within its own process, but it never writes the durable replay store. This endpoint is the only durable replay claimant. A duplicate claim denies, and the gateway must not retry it.

Unregistered, `DISABLED`, `RETIRED`, removed-group, stale-directory, cross-owner, paused, failed, quarantined, archived, scheduled-job, cost-paused, and malformed requests all produce the same generic 404/denial without revealing which condition matched.

### Step 3.4: Mount a route-specific machine endpoint

The existing schedule-gateway runtime serves `/v1/apps/authorize`, but the route must authenticate only the app-gateway service account. `/v1/schedules/execute` must continue to authenticate only its existing scheduler caller. Never accept either caller on the other route.

Run:

```bash
cd plugins/madup-infra-manager/control-plane
.venv/bin/python -m unittest tests.test_origin_hmac tests.test_api_readonly tests.test_central_identity_gateway tests.test_dashboard_views tests.test_deployment_api tests.test_mcp_contract tests.test_mcp_http tests.test_app_hostname tests.test_app_gateway_authorization tests.test_app_gateway_api tests.test_machine_api tests.integration.test_firestore_operations
```

Expected: PASS.

## Task 4: Build the standalone stdlib Go application gateway

**Lane C files:**

- Create: `plugins/madup-infra-manager/app-gateway-go/go.mod`
- Create: `plugins/madup-infra-manager/app-gateway-go/cmd/mim-app-gateway/main.go`
- Create: `plugins/madup-infra-manager/app-gateway-go/gateway/config.go`
- Create: `plugins/madup-infra-manager/app-gateway-go/gateway/edgeproof.go`
- Create: `plugins/madup-infra-manager/app-gateway-go/gateway/accessjwt.go`
- Create: `plugins/madup-infra-manager/app-gateway-go/gateway/authz_client.go`
- Create: `plugins/madup-infra-manager/app-gateway-go/gateway/idtoken.go`
- Create: `plugins/madup-infra-manager/app-gateway-go/gateway/proxy.go`
- Create: `plugins/madup-infra-manager/app-gateway-go/gateway/server.go`
- Create focused `*_test.go` files beside each unit
- Create: `plugins/madup-infra-manager/app-gateway-go/Dockerfile`
- Create: `plugins/madup-infra-manager/app-gateway-go/.dockerignore`
- Create: `plugins/madup-infra-manager/app-gateway-go/README.md`

### Step 4.1: Test configuration and edge proof first

Tests reject missing/unknown environment material, bad suffixes, non-Seoul or cross-project control-plane audiences, keys shorter than 32 bytes, duplicate current/previous key IDs, stale/future timestamps, body/path/host/class drift, and invalid request IDs. The gateway verifies proof and freshness locally; any bounded in-process duplicate cache is only defense in depth and is not the durable replay owner.

### Step 4.2: Test Access JWT verification first

Implement stdlib-only RS256/JWKS verification. Tests use generated RSA keys and an `httptest` JWKS server to prove exact algorithm, key ID, signature, issuer, single expected audience, subject, `@madup.com` email, `iat`/`nbf`/`exp`, cache refresh, and fail-closed network/JSON behavior. Tokens and claims are never logged.

### Step 4.3: Test private auth and metadata token clients first

Inject HTTP clients/token sources in tests. Production metadata calls use the fixed metadata host and `Metadata-Flavor: Google`. Auth calls use `Authorization: Bearer <Google ID token>` with the exact schedule-gateway audience. The authorization endpoint performs the sole durable request claim; do not retry a denied, timed-out, or ambiguous request with the same edge request ID.

### Step 4.4: Test proxy behavior first

Use `httptest` upstreams to prove:

- authorization runs before token minting and proxying;
- the returned host matches the requested host and the decision is not expired;
- production upstream validation accepts only a credential-free HTTPS URL with no explicit port/path/query/fragment, a `.run.app` suffix, the expected `mim-svc-<12hex>-` prefix, and equal audience; the control-plane decision is machine-authenticated and the browser can never supply the origin;
- `X-Serverless-Authorization` carries the Google ID token;
- `Authorization`, Cloudflare Access assertion/cookies, origin proof, internal routing, and spoofable forwarding headers are stripped;
- app-local framework cookies, streaming, HEAD, and WebSocket upgrade work, while workload `Set-Cookie` is constrained to the signed public host and any `.run.app` or parent-domain cookie is safely rewritten or denied;
- `X-Forwarded-Host` is the signed public host and `X-Forwarded-Proto` is `https`;
- a `run.app` redirect is rewritten to the public host or denied, never leaked;
- upstream errors and panics return generic responses without URL/token material.

### Step 4.5: Verify the module and container

```bash
cd plugins/madup-infra-manager/app-gateway-go
go test ./...
go test -race ./...
CGO_ENABLED=0 go build ./cmd/mim-app-gateway
docker build -t mim-app-gateway:local .
```

Expected: PASS and a non-root, read-only-friendly, distroless/static runtime image.

## Task 5: Move user web services from direct IAP to gateway-only IAM

**Lane E files:**

- Create: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/runtime_naming.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/services/render.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/firestore_desired_state.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/secret_manager.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/ports/execution.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/fake_execution.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/cloud_run.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/workers/deploy.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/adapters/lifecycle_effects.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/jobs/lifecycle.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/jobs/usage_ingest.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_deploy_plan.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_firestore_desired_state_adapter.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_secret_manager_adapter.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_cloud_run_adapter.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_private_workers.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_lifecycle_effects.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_lifecycle_job.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_usage_ingest_job.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_cost_enforcement_adapter.py` only if the protocol name changes

### Step 5.1: Add failing desired-state tests

Require web services to render `GATEWAY_IAM`/`PUBLIC_IAM`, `allow_unauthenticated=false`, `custom_domain=false`, and exact gateway invoker identity. Require Streamlit timeout 3,600 and Next.js timeout 300. Scheduled jobs remain machine-only and unchanged.

### Step 5.2: Add failing Cloud Run boundary tests

Require:

- `iap_enabled=false` and `invoker_iam_disabled=false` for user web services;
- exact service-level `roles/run.invoker` members consisting of the app-gateway service account and normalized reviewed break-glass members, with no public/IAP service agent/owner member;
- direct unauthenticated access denied;
- rollback and drift verification use the same gateway-IAM boundary;
- jobs retain their exact schedule-gateway invoker;
- the exact service resource name is generated by one shared naming function, while the origin is accepted only from verified Cloud Run `service.uri` readback.

### Step 5.3: Persist bindings only after healthy deployment

Extend the runtime port to return the exact service resource and actual `service.uri` observed from that resource. After `verify_health` succeeds, validate the read-back URI contract and have the deploy worker create an `ACTIVE` `AppHostnameBinding` before marking the operation succeeded. An identical replay is idempotent; a conflicting binding quarantines the operation. Scheduled jobs never get a hostname binding.

### Step 5.4: Inject attached workload secrets without exposing values

Extend `DesiredStateSecretAttachment` with a deterministic environment name: `MIM_SECRET_<UPPERCASE_SECRET_NAME_WITH_HYPHENS_AS_UNDERSCORES>`. The existing managed secret-name grammar makes this mapping injective and shell-safe. Render only the exact managed secret resource and an exact numeric version; `latest`, aliases, arbitrary resource paths, duplicate environment names, and value-like material are denied.

Remove the Cloud Run adapter's blanket rejection of non-empty `secret_attachments`. For each attachment, render one Cloud Run secret-backed environment variable using the exact managed resource and numeric version. The workload's dedicated runtime service account keeps resource-level `roles/secretmanager.secretAccessor` only on secrets attached to that workload; no project-level access, listing, another workload's secret, or gateway/control-plane runtime access is allowed. The existing control-plane version-manager role remains separate and cannot read payloads.

Apply/readback must prove the exact environment name, secret resource, numeric version, and exact IAM member set. Detach, rotation, offboarding, disable, and retirement remove stale runtime accessor bindings before later secret/compute cleanup. Tests deny an extra plain environment variable, `latest`, a cross-workload secret, an unexpected or broad accessor role, stale attachment access, and any payload/token material in desired state, logs, errors, or dashboard serialization.

### Step 5.5: Replace lifecycle IAP removal with central route denial verification

Offboarding, cost pause, inactivity, and manual pause move the binding to `DISABLED` before schedule, secret-access, or compute cleanup. Final archive/delete moves it to terminal `RETIRED` before compute deletion; a retired host is never reused. A restore/redeploy may move only a `DISABLED` binding to `ACTIVE`, and only after a healthy verified deployment plus fresh user/group/ownership/cost checks. Replace production IAP-owner mutation with a gateway-access effect that verifies this ordered transition and records sanitized audit evidence. It must not call IAP or grant/revoke user IAM. Unknown, disabled, and retired hosts share the generic 404 behavior. Existing cleanup of Cloud Run, schedules, secrets, and images follows the access-denial transition.

Run:

```bash
cd plugins/madup-infra-manager/control-plane
.venv/bin/python -m unittest tests.test_deploy_plan tests.test_firestore_desired_state_adapter tests.test_secret_manager_adapter tests.test_cloud_run_adapter tests.test_private_workers tests.test_lifecycle_effects tests.test_lifecycle_job tests.test_usage_ingest_job tests.test_cost_enforcement_adapter
```

Expected: PASS.

## Task 6: Provision the keyless gateway and exact IAM/bootstrap contract

**Lane F files:**

- Modify: `plugins/madup-infra-manager/infra/control-plane/iam/contract.py`
- Modify: `plugins/madup-infra-manager/infra/control-plane/apply.sh`
- Modify matching `infra/control-plane/test_*.sh`
- Modify: `plugins/madup-infra-manager/infra/runtime-bootstrap/bootstrap_contract.py`
- Modify: `plugins/madup-infra-manager/infra/runtime-bootstrap/bootstrap-input.template.json`
- Modify matching runtime-bootstrap tests
- Modify: `plugins/madup-infra-manager/infra/release/plan.sh`
- Modify: `plugins/madup-infra-manager/infra/release/apply.sh`
- Modify: `plugins/madup-infra-manager/infra/release/task18_lib.sh`
- Modify: `plugins/madup-infra-manager/infra/release/verify.sh`
- Modify: `plugins/madup-infra-manager/infra/release/smoke_test.sh`
- Modify if the active-host scan contract requires it: `plugins/madup-infra-manager/infra/release/public_release_guard.py`
- Modify: `tests/test_public_release_guard.py`
- Modify matching release tests

### Step 6.1: Add failing IAM/bootstrap tests

Require a managed `mim-app-gateway` service account with:

- no project-level role;
- release identity `serviceAccountUser` only on that exact account;
- resource-level `secretAccessor` only on the app Worker-proof secret, with no list permission and an exact numeric version pinned in the Cloud Run revision;
- exact resource-level `run.invoker` on the private schedule-gateway and managed app services;
- no BigQuery, directory, scheduler, runtime-admin, workload-identity-creation, or cross-project role.

Bootstrap adds exact app Access issuer/audience, app-gateway identity, private authorization URL/audience, current/previous app proof key references, public suffix, project number, and region. Unknown/mismatched values fail closed.

### Step 6.2: Add a third reviewed build artifact

Release planning validates one successful commit-pinned Cloud Build and digest-pinned Artifact Registry image named `app-gateway` in the central platform repository. A tag without a digest, mismatched commit/build identity, additional image result, or missing artifact is a blocker.

### Step 6.3: Add exact gateway service desired state

The reviewed service is:

- name `mim-app-gateway`;
- region `asia-northeast3`;
- gateway service account;
- digest-pinned gateway image;
- `min=0`, bounded `max`, request-based CPU, no VPC connector;
- `ingress=all` and service-level unauthenticated invoker because Cloudflare cannot mint Google tokens;
- all application authorization enforced by Worker proof + Access JWT + central policy;
- one exact gateway-proof secret resource reference pinned to a numeric version, plus non-secret public config only; no user/workload secret reference.

Release apply reads back image digest, service account, scaling, ingress, env names, secret versions, IAM, revision readiness, and URL. It never prints secret values.

## Task 7: Extend exact-zone Cloudflare plan/apply/readback

**Lane F files:**

- Modify: `plugins/madup-infra-manager/infra/edge/cloudflare_api.sh`
- Modify: `plugins/madup-infra-manager/infra/edge/plan.sh`
- Modify: `plugins/madup-infra-manager/infra/edge/apply.sh`
- Modify: `plugins/madup-infra-manager/infra/edge/preflight.sh`
- Modify: `plugins/madup-infra-manager/infra/edge/test_plan.sh`
- Modify: `plugins/madup-infra-manager/infra/edge/test_apply.sh`

### Step 7.1: Add failing exact-zone tests

Require:

- active zone name exactly `madup.app`, expected account ID, and currently authoritative nameservers;
- one proxied `mim` CNAME to the control-plane `run.app` target;
- one proxied `*` CNAME to the app-gateway `run.app` target;
- exact Worker routes `mim.madup.app/*` and `*.madup.app/*` only;
- one exact `mim.madup.app` Access app with Managed OAuth;
- one exact `*.madup.app` Access app with its own audience and required Google group policy;
- exact GitHub webhook bypass app/path only;
- current Access seat count within the reviewed pilot limit;
- both Worker secrets present by name without inspecting values;
- no apex, nameserver, certificate, other zone, other Worker, or non-MIM Access action.

Test ambiguous duplicates, other-zone IDs, account mismatch, wrong wildcard, route shadowing, public DNS records, app/policy drift, seat overflow, and list-and-rewrite/delete proposals as blockers.

### Step 7.2: Implement immutable plan actions

Each action captures exact discovered IDs and before-state hashes. Apply refuses a changed/expired plan, performs only create/update of exact MIM resources, and immediately reads back the exact resource. There are no wildcard deletes or account-wide rewrites.

### Step 7.3: Verify shell contracts

```bash
bash plugins/madup-infra-manager/infra/edge/test_plan.sh
bash plugins/madup-infra-manager/infra/edge/test_apply.sh
```

Expected: PASS without network or production mutations.

## Task 8: Integrate dashboard, staging canaries, release gates, and employee guidance

**Leader integration files:**

- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/runtime.py`
- Modify: `plugins/madup-infra-manager/control-plane/tests/test_runtime.py`
- Modify: `plugins/madup-infra-manager/control-plane/src/mim_control_plane/dashboard.py`
- Modify matching dashboard tests/static UI only as needed
- Add/modify: `plugins/madup-infra-manager/control-plane/tests/staging/**`
- Modify: `plugins/madup-infra-manager/infra/release/verify.sh`
- Modify: `plugins/madup-infra-manager/infra/release/smoke_test.sh`
- Update active operator/user docs after behavior is proven

### Step 8.1: Wire runtime with failing tests first

Schedule-gateway composition receives two route-specific expected callers and the app authorization service. Deploy-worker composition receives gateway identity and app-binding store support. Unknown runtime/bootstrap material remains rejected.

### Step 8.2: Expose safe application links and health

User dashboard views include only owned public host, workload state, latest operation/health, schedule state, secret metadata, quota, and cost. Admin views add aggregate gateway/Access/Worker usage and failures. Neither view includes origins, account/zone IDs, Access audiences, JWTs, HMAC key IDs/values, Google tokens, or exception text.

### Step 8.3: Add staging contract and live-canary hooks

Required canaries:

- unregistered/reserved/apex/nested host generic denial;
- active owner allow, cross-owner/non-Madup/inactive/removed-group/stale-directory deny;
- missing/stale/replayed/wrong-class Worker proof deny;
- direct app-gateway origin deny without proof;
- direct user-app origin deny without Google IAM;
- Streamlit WebSocket through the full edge and reconnect after bounded closure;
- Next.js root, asset, navigation, SSR, forwarded-host, and redirect behavior;
- cost pause/emergency stop and offboarding take effect on next HTTP/WebSocket handshake;
- existing GitHub signature, scheduler OIDC, lifecycle, BigQuery, protected-project, secret, and release-history denials remain green.

### Step 8.4: Run full local verification

```bash
bash plugins/madup-infra-manager/infra/release/verify.sh --local
```

Expected: exit 0, including Go unit/race/build, gateway container build, Worker tests, all Python/shell/integration tests, lint, type checking, Docker builds, and secret/history scan.

## Task 9: Independent review and corrective loop

Run three independent read-only reviews against the integrated diff:

1. specification/architecture review;
2. security review covering auth bypass, JWT/JWKS, HMAC replay and host/class binding, SSRF/upstream pinning, header/cookie/token leakage, IAM, exact project/zone, BigQuery, secrets, cost, lifecycle, and production mutation safety;
3. verification review covering test adequacy and release evidence.

Fix every critical/high issue and every medium issue that can affect authorization, data exposure, cloud scope, cost stops, or operational recovery. Repeat focused and full verification after fixes.

## Task 10: Exact production rollout

Production rollout is sequential and fail-closed:

1. run the public release guard against worktree, index, outbound commits, and history;
2. use read-only GCP and Cloudflare discovery to capture exact project/account/zone state;
3. obtain/use an exact-zone Cloudflare DNS token and exact named account-resource authorization without pasting or printing it;
4. generate IAM/bootstrap/release/edge plans and record their hashes;
5. review that the plans name only the operator-private exact MIM project, `madup.app`, and exact MIM resources;
6. apply GCP identities/secrets/builds/services first and read back every resource;
7. apply Cloudflare Access apps, Worker/secrets, DNS, and routes and read back every resource;
8. install/configure the central GitHub App only on selected `madupmarketing` repositories and verify exact webhook delivery;
9. run public DNS/TLS/OAuth/MCP/dashboard/app/WebSocket/Next.js/IAM/cost/offboarding canaries;
10. merge the verified feature branch, run the release guard again, push `main`, and record the deployed commit/digests/plan hashes without secrets.

Any MFA, security challenge, consent screen, or GitHub App installation confirmation is completed interactively by the authenticated administrator. No token, private key, recovery code, or MFA value is requested in chat.

## Final evidence checklist

- [ ] Active plugin/runtime/docs use `mim.madup.app`; historical plans are clearly superseded.
- [ ] `mim.madup.app/mcp` completes Claude OAuth discovery, PKCE login, refresh, and authenticated MCP calls.
- [ ] Registered `<slug>.madup.app` works; unregistered/reserved/apex/nested hosts reveal nothing.
- [ ] Host binding material is immutable; `DISABLED` denies before cleanup, `RETIRED` is terminal, and a retired host is never reused.
- [ ] Streamlit WebSocket and Next.js asset/SSR/forwarded-host canaries pass.
- [ ] Direct app origins reject unauthenticated callers; gateway is the only normal invoker.
- [ ] Active/group/owner/admin/cost/lifecycle decisions are enforced on every request or WebSocket handshake.
- [ ] Per-user service/schedule/secret/KRW limits and organization emergency ceiling remain green.
- [ ] Attached secrets use deterministic safe env names, exact numeric versions, and workload-only resource IAM; values never enter Git, desired state, logs, errors, or dashboards.
- [ ] Offboarding and 30-day inactivity deny access before compute cleanup.
- [ ] Gateway has no BigQuery, cross-project, scheduler, admin, service-account-key, or user/workload-secret authority; its sole secret access is its own proof secret resource.
- [ ] Cloudflare plan/apply cannot mutate any zone or resource outside exact MIM targets.
- [ ] GCP plan/apply cannot mutate any project outside the exact MIM project.
- [ ] Full local verification, independent reviews, live canaries, secret/history scan, merge, and push all pass.
